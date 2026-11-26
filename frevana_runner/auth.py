"""The native public client. Only this module accesses the system credential store."""

import base64
import hashlib
import hmac
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from filelock import FileLock

CLIENT = "frevana-agent-app-runner"
VAULT = CLIENT + ".oauth"
RESOURCE_PATH = "/agent-app-external-run"
SCENARIO_SCOPE = "agent_app_external_run"
ORIGINS = {
    "production": "https://api.frevana.com",
    "development": "https://api-dev.frevana.com",
}
WEB_ORIGINS = {
    "production": "https://www.frevana.com",
    "development": "https://dev-www.frevana.com",
}


class RunnerError(Exception):
    pass


class ApiError(RunnerError):
    def __init__(self, status, code):
        super().__init__(f"Frevana returned HTTP {status} ({code})")
        self.status, self.code = status, code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request_json(
    url, *, form=None, payload=None, bearer=None, method=None, headers=None
):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=45) as res:
            raw = res.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(8192))
            code = body.get("code") or body.get("error") or "request_failed"
        except (ValueError, AttributeError):
            code = "request_failed"
        raise ApiError(
            e.code,
            code if isinstance(code, str) and code.isidentifier() else "request_failed",
        ) from None
    return json.loads(raw) if raw else {}


def system_keyring():
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
    elif sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Keyring
    else:
        from keyring.backends.SecretService import Keyring
    return Keyring()


def discover(api):
    data = request_json(api + "/.well-known/oauth-authorization-server")
    if not isinstance(data, dict) or data.get("issuer") != api:
        raise RunnerError("OAuth discovery returned a different environment.")
    for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        value = data.get(field)
        if not isinstance(value, str):
            raise RunnerError("OAuth discovery is missing an endpoint.")
        url = urllib.parse.urlsplit(value)
        if (
            not value.startswith(api + "/")
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise RunnerError("OAuth discovery returned an invalid endpoint.")
    return data


def check_resource(api):
    try:
        data = request_json(
            api + "/.well-known/oauth-protected-resource" + RESOURCE_PATH
        )
    except ApiError as error:
        if error.status == 404:
            raise RunnerError(
                f"Agent App external running is not enabled at {api}."
            ) from None
        raise
    if (
        not isinstance(data, dict)
        or data.get("resource") != api + RESOURCE_PATH
        or data.get("authorization_servers") != [api]
    ):
        raise RunnerError(
            "Agent App resource metadata does not match this environment."
        )
    scopes = data.get("scopes_supported")
    if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
        raise RunnerError(
            "Agent App resource metadata has invalid scopes_supported."
        )
    if SCENARIO_SCOPE not in scopes:
        raise RunnerError(
            f"Agent App external running is not enabled at {api}. "
            "Ask an administrator to enable agent_app_external_run."
        )


def authorize(api, metadata, client_id, browser=webbrowser.open, *, timeout=600):
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *_):
            pass

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            states = query.get("state", [])
            if (
                url.path != "/callback"
                or self.headers.get("Host") != server.server_name_port
                or len(states) != 1
                or not hmac.compare_digest(states[0].encode(), state.encode())
                or query.get("iss", [api]) != [api]
            ):
                self.send_error(400)
                return
            field = "error" if "error" in query else "code"
            values = query.get(field, [])
            if (
                len(values) != 1
                or not values[0]
                or ("error" in query and "code" in query)
            ):
                self.send_error(400)
                return
            result[field] = values[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Return to your coding agent to continue.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.server_name_port = f"127.0.0.1:{server.server_port}"
        redirect = f"http://{server.server_name_port}/callback"
        params = {
            "client_id": client_id,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "redirect_uri": redirect,
            "state": state,
            "resource": api + RESOURCE_PATH,
        }
        url = metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
        print(
            "Authorize this device in your browser (complete within 10 minutes):\n" + url,
            file=sys.stderr,
            flush=True,
        )
        try:
            browser(url)
        except (OSError, webbrowser.Error):
            pass  # The printed URL also works when no browser can be launched.
        end = time.monotonic() + timeout
        while not result and time.monotonic() < end:
            server.timeout = min(1, max(0, end - time.monotonic()))
            server.handle_request()
    if "error" in result:
        raise RunnerError("Authorization was declined. Run prepare again when ready.")
    if not result:
        raise RunnerError(
            "Authorization timed out. Run prepare again to open a new link; "
            "refreshing the old authorization page will not restart it."
        )
    return request_json(
        metadata["token_endpoint"],
        form={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "resource": api + RESOURCE_PATH,
            "code": result["code"],
        },
    )


class Credentials:
    def __init__(self, env="production", *, root=None, backend=None, clock=time.time):
        self.environment = env
        self.api = ORIGINS[env]
        self.root = (
            Path(root) if root else Path.home() / ".frevana" / "agent-app-runner"
        ) / env
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backend = backend if backend is not None else system_keyring()
        self.lock = FileLock(str(self.root / "credentials.lock"), timeout=620)
        self.clock = clock
        self._metadata = None

    @property
    def business_api(self):
        return self.api + RESOURCE_PATH

    def metadata(self):
        if self._metadata is None:
            self._metadata = discover(self.api)
        return self._metadata

    def _read(self):
        raw = self.backend.get_password(VAULT, self.api)
        return json.loads(raw) if raw else {}

    def _write(self, value):
        self.backend.set_password(VAULT, self.api, json.dumps(value))
        return value

    def token(self, *, login=False, expected_grant=None):
        if login:
            check_resource(self.api)
        with self.lock:
            saved = self._read()
            if expected_grant and saved.get("grant_id") != expected_grant:
                raise RunnerError(
                    "Runner authorization changed. Prepare the App again."
                )
            if saved.get("access_token") and saved["expires_at"] > self.clock() + 45:
                return saved
            if saved.get("refresh_token"):
                try:
                    pair = request_json(
                        self.metadata()["token_endpoint"],
                        form={
                            "client_id": saved["client_id"],
                            "grant_type": "refresh_token",
                            "resource": self.business_api,
                            "refresh_token": saved["refresh_token"],
                        },
                    )
                except ApiError as e:
                    if e.code not in ("invalid_grant", "invalid_client"):
                        raise
                    saved = self._write(
                        {"client_id": saved["client_id"]}
                        if e.code == "invalid_grant"
                        else {}
                    )
                else:
                    return self._save(pair, saved["client_id"], previous=saved)
            if not login:
                raise RunnerError("Authorize the device again with prepare.")
            metadata = self.metadata()
            if not saved.get("client_id"):
                client = request_json(
                    metadata["registration_endpoint"],
                    payload={
                        "client_name": "Frevana Agent App Runner",
                        "application_type": "native",
                        # Native loopback redirect matching ignores the ephemeral port.
                        "redirect_uris": ["http://127.0.0.1/callback"],
                        "response_types": ["code"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                if (
                    not isinstance(client, dict)
                    or not isinstance(client.get("client_id"), str)
                    or not client["client_id"]
                ):
                    raise RunnerError("OAuth registration did not return a client_id.")
                saved = self._write({"client_id": client["client_id"]})
            pair = authorize(self.api, metadata, saved["client_id"])
            return self._save(pair, saved["client_id"])

    def _save(self, pair, client_id, *, previous=None):
        if (
            not isinstance(pair, dict)
            or any(
                not isinstance(pair.get(k), str) or not pair[k]
                for k in ("access_token", "refresh_token", "grant_id")
            )
            or str(pair.get("token_type", "")).lower() != "bearer"
            or type(pair.get("expires_in")) is not int
            or pair["expires_in"] <= 0
        ):
            raise RunnerError("OAuth returned an incomplete token pair.")
        if previous and pair["grant_id"] != previous["grant_id"]:
            raise RunnerError(
                "OAuth refresh changed the authorization. Prepare the App again."
            )
        scope = pair.get(
            "scope", previous.get("scope", SCENARIO_SCOPE) if previous else SCENARIO_SCOPE
        )
        if not isinstance(scope, str):
            raise RunnerError("OAuth returned an invalid scope.")
        saved = {
            "client_id": client_id,
            "access_token": pair["access_token"],
            "refresh_token": pair["refresh_token"],
            "expires_at": self.clock() + int(pair["expires_in"]),
            "grant_id": pair["grant_id"],
            "scope": scope,
        }
        return self._write(saved)

    def logout(self):
        with self.lock:
            saved = self._read()
            if saved:
                self._write({"client_id": saved["client_id"]})
            return {
                "local_credentials_cleared": True,
                "grant_id": saved.get("grant_id"),
                "authorization_management_url": WEB_ORIGINS[self.environment]
                + "/account/authorized-apps",
                "message": "To revoke this authorization on the server, open the account page.",
            }
