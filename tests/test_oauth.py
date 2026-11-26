import base64
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from frevana_runner import cli
from frevana_runner.auth import (
    DOWNLOAD_SCOPE,
    RESOURCE_PATH,
    VAULT,
    ApiError,
    Credentials,
    RunnerError,
    authorize,
    check_resource,
    discover,
)
from frevana_runner.packs import share_environment


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value


class OAuthFixture:
    """HTTP contract peer: verifies native callbacks, PKCE and single-use refresh tokens."""

    def __init__(self):
        self.requests = []
        self.clients = {}
        self.codes = {}
        self.refreshes = {}
        self.serial = 0
        self.denied = False
        self.revoked = False
        self.scopes = [DOWNLOAD_SCOPE]
        self.browser_errors = []
        self.callback_statuses = []
        self.callback_attacks = False
        self.threads = []
        self.access_tokens = set()
        self.resource_handler = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, body, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                fixture.requests.append(("GET", parsed.path, None))
                if self.resource_request("GET", b""):
                    return
                if parsed.path == "/.well-known/oauth-authorization-server":
                    self.reply(fixture.metadata)
                elif (
                    parsed.path
                    == "/.well-known/oauth-protected-resource" + RESOURCE_PATH
                ):
                    self.reply(
                        {
                            "resource": fixture.api + RESOURCE_PATH,
                            "authorization_servers": [fixture.api],
                            "scopes_supported": fixture.scopes,
                        }
                    )
                elif parsed.path == "/oauth/authorize":
                    params = {
                        k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
                    }
                    registered = fixture.clients.get(params.get("client_id"))
                    redirect = urllib.parse.urlsplit(params["redirect_uri"])
                    if (
                        not registered
                        or redirect.hostname != "127.0.0.1"
                        or redirect.path != "/callback"
                    ):
                        self.reply({"error": "invalid_client"}, 400)
                        return
                    if (
                        params.get("resource") != fixture.api + RESOURCE_PATH
                        or params.get("code_challenge_method") != "S256"
                        or "scope" in params
                    ):
                        self.reply({"error": "invalid_request"}, 400)
                        return
                    fixture.serial += 1
                    code = "code-" + str(fixture.serial)
                    fixture.codes[code] = params
                    query = {"state": params["state"], "iss": fixture.api}
                    query.update(
                        {"error": "access_denied"} if fixture.denied else {"code": code}
                    )
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        params["redirect_uri"] + "?" + urllib.parse.urlencode(query),
                    )
                    self.end_headers()
                else:
                    self.reply({"error": "not_found"}, 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.resource_request("POST", body):
                    return
                if self.path == "/oauth/register":
                    data = json.loads(body)
                    fixture.requests.append(("POST", self.path, data))
                    if (
                        data.get("application_type") != "native"
                        or data.get("token_endpoint_auth_method") != "none"
                        or data.get("redirect_uris") != ["http://127.0.0.1/callback"]
                    ):
                        self.reply({"error": "invalid_client_metadata"}, 400)
                        return
                    client = "client-" + str(len(fixture.clients) + 1)
                    fixture.clients[client] = data
                    self.reply({"client_id": client}, 201)
                    return
                data = {
                    k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()
                }
                fixture.requests.append(("POST", self.path, data))
                if self.path != "/oauth/token":
                    self.reply({"error": "not_found"}, 404)
                    return
                if (
                    fixture.revoked
                    or data.get("resource") != fixture.api + RESOURCE_PATH
                ):
                    self.reply({"error": "invalid_grant"}, 400)
                    return
                if data.get("grant_type") == "authorization_code":
                    original = fixture.codes.pop(data.get("code"), None)
                    challenge = (
                        base64.urlsafe_b64encode(
                            hashlib.sha256(
                                data.get("code_verifier", "").encode()
                            ).digest()
                        )
                        .rstrip(b"=")
                        .decode()
                    )
                    if (
                        not original
                        or original["client_id"] != data.get("client_id")
                        or original["redirect_uri"] != data.get("redirect_uri")
                        or original["code_challenge"] != challenge
                    ):
                        self.reply({"error": "invalid_grant"}, 400)
                        return
                    grant = "grant-" + data["code"]
                else:
                    previous = fixture.refreshes.pop(data.get("refresh_token"), None)
                    if not previous or previous[0] != data.get("client_id"):
                        self.reply({"error": "invalid_grant"}, 400)
                        return
                    grant = previous[1]
                fixture.serial += 1
                refresh = "refresh-" + str(fixture.serial)
                fixture.refreshes[refresh] = (data["client_id"], grant)
                fixture.access_tokens.add("Bearer access-" + str(fixture.serial))
                self.reply(
                    {
                        "access_token": "access-" + str(fixture.serial),
                        "refresh_token": refresh,
                        "grant_id": grant,
                        "expires_in": 900,
                        "token_type": "Bearer",
                        "scope": " ".join(fixture.scopes),
                    }
                )

            def resource_request(self, method, body):
                if not self.path.startswith(RESOURCE_PATH + "/"):
                    return False
                if (
                    fixture.revoked
                    or self.headers.get("Authorization") not in fixture.access_tokens
                ):
                    self.reply({"error": "unauthorized"}, 401)
                elif fixture.resource_handler:
                    fixture.requests.append((method, self.path, body))
                    result = fixture.resource_handler(
                        method, self.path, body, self.headers
                    )
                    self.reply(result)
                else:
                    self.reply({"error": "not_found"}, 404)
                return True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api = f"http://127.0.0.1:{self.server.server_port}"
        self.metadata = {
            "issuer": self.api,
            "authorization_endpoint": self.api + "/oauth/authorize",
            "token_endpoint": self.api + "/oauth/token",
            "registration_endpoint": self.api + "/oauth/register",
        }
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        for thread in self.threads:
            thread.join(3)
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def browser(self, url):
        def visit():
            try:
                if self.callback_attacks:
                    params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    redirect, state = params["redirect_uri"][0], params["state"][0]
                    queries = [
                        {"state": "wrong", "code": "attack"},
                        {"state": [state, state], "code": "attack"},
                        {"state": state, "iss": "https://wrong.test", "code": "attack"},
                        {"state": state},
                        {"state": state, "code": ["one", "two"]},
                    ]
                    for query in queries:
                        try:
                            urllib.request.urlopen(
                                redirect
                                + "?"
                                + urllib.parse.urlencode(query, doseq=True),
                                timeout=3,
                            )
                        except urllib.error.HTTPError as error:
                            self.callback_statuses.append(error.code)
                with urllib.request.urlopen(url, timeout=3) as response:
                    response.read()
            except (OSError, ValueError, KeyError) as error:
                self.browser_errors.append(error)

        thread = threading.Thread(target=visit, daemon=True)
        self.threads.append(thread)
        thread.start()
        return True


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.fixture = OAuthFixture()
        self.addCleanup(self.fixture.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Vault()
        self.now = 10
        self.creds = self.credentials()
        self.output = io.StringIO()
        log = contextlib.redirect_stderr(self.output)
        log.__enter__()
        self.addCleanup(log.__exit__, None, None, None)
        browser = patch(
            "frevana_runner.auth.authorize",
            side_effect=lambda api, metadata, client: authorize(
                api, metadata, client, self.fixture.browser, timeout=3
            ),
        )
        browser.start()
        self.addCleanup(browser.stop)

    def credentials(self):
        creds = Credentials(
            "development",
            root=self.temp.name,
            backend=self.vault,
            clock=lambda: self.now,
        )
        creds.api = self.fixture.api
        return creds

    def test_discovery_registration_pkce_reuse_and_rotating_refresh(self):
        first = self.creds.token(login=True)
        self.assertEqual(self.creds.token(login=True), first)
        self.assertEqual(len(self.fixture.clients), 1)
        self.now += 900
        with ThreadPoolExecutor(max_workers=4) as pool:
            renewed = list(pool.map(lambda _: self.credentials().token(), range(4)))
        self.assertTrue(all(p == renewed[0] for p in renewed))
        self.assertEqual(first["grant_id"], renewed[0]["grant_id"])
        self.assertNotEqual(first["refresh_token"], renewed[0]["refresh_token"])
        self.assertNotEqual(first["access_token"], renewed[0]["access_token"])
        refreshes = [
            r
            for r in self.fixture.requests
            if r[1] == "/oauth/token" and r[2]["grant_type"] == "refresh_token"
        ]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(self.creds._read(), renewed[0])
        self.assertEqual(self.fixture.browser_errors, [])
        for value in (
            first["access_token"],
            first["refresh_token"],
            renewed[0]["refresh_token"],
        ):
            self.assertNotIn(value, self.output.getvalue())
        exchange = next(r[2] for r in self.fixture.requests if r[1] == "/oauth/token")
        self.assertNotIn(exchange["code_verifier"], self.output.getvalue())

    def test_bad_callbacks_do_not_consume_the_valid_authorization(self):
        self.fixture.callback_attacks = True
        self.assertTrue(self.creds.token(login=True)["access_token"])
        self.assertEqual(self.fixture.callback_statuses, [400] * 5)
        self.assertFalse(self.fixture.browser_errors)

    def test_independent_processes_share_one_rotating_refresh(self):
        first = self.creds.token(login=True)
        vault = Path(self.temp.name) / "test-vault.json"
        vault.write_text(json.dumps(first))
        program = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from frevana_runner.auth import Credentials
class TestVault:
    def get_password(self, *_):
        return Path(sys.argv[5]).read_text()
    def set_password(self, service, account, value):
        Path(sys.argv[5]).write_text(value)
creds = Credentials('development', root=sys.argv[2], backend=TestVault(),
                    clock=lambda: float(sys.argv[4]))
creds.api = sys.argv[3]
print(json.dumps(creds.token()))
"""
        command = [
            sys.executable,
            "-I",
            "-c",
            program,
            str(Path(__file__).resolve().parents[1]),
            self.temp.name,
            self.fixture.api,
            str(self.now + 900),
            str(vault),
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: subprocess.run(
                        command, capture_output=True, text=True, timeout=15, check=True
                    ),
                    range(4),
                )
            )
        renewed = [json.loads(result.stdout) for result in results]
        self.assertTrue(all(pair == renewed[0] for pair in renewed))
        self.assertEqual(renewed[0]["grant_id"], first["grant_id"])
        self.assertNotEqual(renewed[0]["refresh_token"], first["refresh_token"])
        self.assertEqual(json.loads(vault.read_text()), renewed[0])
        refreshes = [
            r
            for r in self.fixture.requests
            if r[1] == "/oauth/token" and r[2]["grant_type"] == "refresh_token"
        ]
        self.assertEqual(len(refreshes), 1)

    def test_denial_does_not_exchange_a_code(self):
        self.fixture.denied = True
        with self.assertRaisesRegex(RunnerError, "declined"):
            self.creds.token(login=True)
        self.assertNotIn("access_token", self.creds._read())
        self.assertFalse(any(r[1] == "/oauth/token" for r in self.fixture.requests))

    def test_timeout_does_not_store_tokens(self):
        with self.assertRaisesRegex(RunnerError, "timed out"):
            authorize(
                self.fixture.api,
                self.fixture.metadata,
                "client",
                browser=lambda _: False,
                timeout=0.01,
            )
        self.assertFalse(self.creds._read())

    def test_preflight_refuses_missing_download_before_registration(self):
        self.fixture.scopes = []
        with self.assertRaisesRegex(RunnerError, "downloads are not enabled"):
            self.creds.token(login=True)
        self.assertFalse(self.fixture.clients)
        with (
            patch(
                "frevana_runner.auth.request_json",
                side_effect=ApiError(404, "not_found"),
            ),
            self.assertRaisesRegex(RunnerError, "not enabled"),
        ):
            check_resource("https://api.frevana.com")

    def test_revoked_refresh_clears_tokens_without_starting_new_login(self):
        first = self.creds.token(login=True)
        self.now += 900
        self.fixture.revoked = True
        with self.assertRaisesRegex(RunnerError, "prepare"):
            self.creds.token(expected_grant=first["grant_id"])
        self.assertEqual(self.creds._read(), {"client_id": first["client_id"]})
        self.assertEqual(len(self.fixture.clients), 1)

    def test_logout_preserves_registration_but_next_consent_gets_a_new_grant(self):
        first = self.creds.token(login=True)
        result = self.creds.logout()
        self.assertEqual(result["grant_id"], first["grant_id"])
        self.assertEqual(
            result["authorization_management_url"],
            "https://dev-www.frevana.com/account/authorized-apps",
        )
        second = self.creds.token(login=True)
        self.assertEqual(second["client_id"], first["client_id"])
        self.assertNotEqual(second["grant_id"], first["grant_id"])
        with self.assertRaisesRegex(RunnerError, "authorization changed"):
            self.creds.token(expected_grant=first["grant_id"])

    def test_discovery_never_sends_credentials_to_another_environment(self):
        self.fixture.metadata["token_endpoint"] = "https://api.frevana.com/oauth/token"
        with self.assertRaisesRegex(RunnerError, "invalid endpoint"):
            discover(self.fixture.api)
        self.assertFalse(self.fixture.clients)

    def test_invalid_token_pair_does_not_overwrite_saved_credentials(self):
        first = self.creds.token(login=True)
        for changes in (
            {"refresh_token": ""},
            {"token_type": "DPoP"},
            {"expires_in": 0},
            {"grant_id": "other"},
        ):
            pair = {**first, "expires_in": 900, "token_type": "Bearer", **changes}
            with self.assertRaises(RunnerError):
                self.creds._save(pair, first["client_id"], previous=first)
            self.assertEqual(self.creds._read(), first)

    def test_environment_isolation_and_explicit_conflicts(self):
        for host, env in [
            ("dev-www.frevana.com", "development"),
            ("www.frevana.com", "production"),
            ("frevana.com", "production"),
        ]:
            url = f"https://{host}/zh-CN/agentapp/s/same/preview?source=test"
            self.assertEqual(share_environment(url), env)
            self.assertEqual(share_environment(url, env), env)
        with patch("frevana_runner.cli.Credentials") as credentials:
            self.assertEqual(
                cli.main(
                    [
                        "--environment",
                        "production",
                        "prepare",
                        "https://dev-www.frevana.com/agentapp/s/same",
                    ]
                ),
                1,
            )
            credentials.assert_not_called()
        for bad in [
            "https://evil.test/agentapp/s/same",
            "https://www.frevana.com.evil.test/agentapp/s/same",
            "https://www.frevana.com/agentapp/p/same",
        ]:
            with self.assertRaises(RunnerError):
                share_environment(bad)
        dev = Credentials("development", root=self.temp.name, backend=self.vault)
        prod = Credentials("production", root=self.temp.name, backend=self.vault)
        dev._write({"client_id": "dev"})
        prod._write({"client_id": "prod"})
        self.assertNotEqual(dev.root, prod.root)
        self.assertEqual(dev._read()["client_id"], "dev")
        self.assertEqual(prod._read()["client_id"], "prod")
        self.assertEqual(len([key for key in self.vault.values if key[0] == VAULT]), 2)

    def test_cli_oauth_download_long_job_and_result_across_processes(self):
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("skillpack.json", '{"name":"OAuth fixture"}')
            archive.writestr(
                "job.json", '{"jobs":[{"id":"run","prompt":"python skills/run.py"}]}'
            )
            archive.writestr(
                "skills/run.py",
                """import json, os, urllib.request as u
from pathlib import Path
base=os.environ['FREVANA_API_BASE_URL']
assert base == os.environ['FREVANA_API_BASE']
headers={'Authorization': 'Bearer '+os.environ['FREVANA_TOKEN']}
job=json.load(u.urlopen(u.Request(base+'/service/submit', data=b'{}', headers=headers)))
result=json.load(u.urlopen(u.Request(base+'/service/status/'+job['id'], headers=headers)))
(Path(os.environ['AEO_DATA_ROOT'])/'output.json').write_text(json.dumps(result))
""",
            )
        package_bytes = package.getvalue()
        descriptor = {
            "share_id": "share-one",
            "agent_app_id": "app-one",
            "snapshot_id": "snapshot-one",
            "sha256": hashlib.sha256(package_bytes).hexdigest(),
            "download_url": "https://packages.example.test/app.zip",
        }
        calls = []

        def resource(method, path, body, headers):
            calls.append((method, path))
            if path.endswith("/download"):
                self.assertEqual(headers["x-policy-version"], "2")
                return descriptor
            if path.endswith("/submit"):
                self.now += 18 * 60
                return {"id": "existing-job"}
            return {"answer": "done", "id": "existing-job"}

        self.fixture.resource_handler = resource
        actual_open = urllib.request.urlopen
        downloads = []

        def object_download(url, *args, **kwargs):
            if url == descriptor["download_url"]:
                downloads.append(url)
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {"timeout": 120})
                return io.BytesIO(package_bytes)
            return actual_open(url, *args, **kwargs)

        def command(*args):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(list(args)), 0)
            return output.getvalue()

        share = "https://dev-www.frevana.com/zh-CN/agentapp/s/fixture/preview"
        with (
            patch(
                "frevana_runner.cli.Credentials",
                side_effect=lambda env: self.credentials(),
            ),
            patch("urllib.request.urlopen", side_effect=object_download),
        ):
            info = json.loads(command("prepare", share))
            self.assertEqual(info["share_id"], "share-one")
            self.assertEqual(json.loads(command("prepare", share)), info)
            job = json.loads((Path(info["pack_root"]) / "job.json").read_text())[
                "jobs"
            ][0]
            self.assertEqual(job["id"], "run")
            command("exec", share, "--", sys.executable, "-I", "skills/run.py")
            saved = json.loads(
                command(
                    "result",
                    share,
                    "run",
                    "--save",
                    str(Path(info["data_root"]) / "output.json"),
                )
            )
            self.assertTrue(Path(saved["path"]).is_file())
            self.assertEqual(
                json.loads(command("result", share, "run")),
                {"answer": "done", "id": "existing-job"},
            )
            self.assertEqual(
                len([r for r in self.fixture.requests if r[1] == "/oauth/token"]), 2
            )
            command("--environment", "development", "logout")
            new_info = json.loads(command("prepare", share))
            self.assertNotEqual(new_info["data_root"], info["data_root"])
            self.assertEqual(json.loads(command("result", share, "run")), {})
            self.assertEqual(
                json.loads(Path(saved["path"]).read_text()),
                {"answer": "done", "id": "existing-job"},
            )
        self.assertEqual(len(downloads), 2)
        self.assertEqual(len(self.fixture.clients), 1)
        self.assertEqual(calls.count(("POST", RESOURCE_PATH + "/service/submit")), 1)
        self.assertIn(("GET", RESOURCE_PATH + "/service/status/existing-job"), calls)
        self.assertEqual(
            len([r for r in self.fixture.requests if r[1] == "/oauth/token"]), 3
        )
