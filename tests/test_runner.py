import io
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from frevana_runner.auth import ApiError, Credentials, RunnerError
from frevana_runner.packs import prepare, share_code, unpack
from frevana_runner.relay import forwarding


class MemoryKeyring:
    def __init__(self):
        self.value = None

    def get_password(self, *args):
        return self.value

    def set_password(self, service, user, value):
        self.value = value

    def delete_password(self, *args):
        self.value = None


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 60
        preflight = patch("frevana_runner.auth.check_resource")
        preflight.start()
        self.addCleanup(preflight.stop)
        discovery = patch(
            "frevana_runner.auth.discover",
            return_value={"token_endpoint": "https://api.frevana.com/oauth/token"},
        )
        discovery.start()
        self.addCleanup(discovery.stop)
        self.backend = MemoryKeyring()
        self.creds = Credentials(
            root=self.tmp.name, backend=self.backend, clock=lambda: self.now
        )
        self.creds._save(
            {
                "access_token": "first",
                "refresh_token": "secret-renew",
                "expires_in": 900,
                "grant_id": "grant-a",
                "token_type": "Bearer",
            },
            "grant-a",
        )

    def test_single_long_child_queries_at_minute_18_without_repeating_submission(self):
        events = []
        owner = self

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                events.append(
                    (
                        self.command,
                        self.path,
                        self.headers["Authorization"],
                        self.headers["Idempotency-Key"],
                        self.rfile.read(int(self.headers["Content-Length"])),
                    )
                )
                owner.now = 18 * 60
                self.send_response(202)
                self.end_headers()
                self.wfile.write(b"job1")

            def do_GET(self):
                events.append((self.command, self.path, self.headers["Authorization"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: complete\n\n")

        with ThreadingHTTPServer(("127.0.0.1", 0), Api) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.creds.api = f"http://127.0.0.1:{server.server_port}"
                fresh = {
                    "access_token": "second",
                    "refresh_token": "second-renew",
                    "expires_in": 900,
                    "grant_id": "grant-a",
                    "token_type": "Bearer",
                }
                with (
                    patch(
                        "frevana_runner.auth.request_json", return_value=fresh
                    ) as exchange,
                    forwarding(self.creds, "grant-a", "first") as base,
                ):
                    code = """import os,urllib.request as u
base=os.environ['FREVANA_API_BASE_URL']
headers={'Authorization':'Bearer '+os.environ['FREVANA_TOKEN'],'Idempotency-Key':'unique'}
assert u.urlopen(u.Request(base+'/submit', data=b'payload',headers=headers)).read()==b'job1'
assert u.urlopen(u.Request(base+'/jobs/job1',headers=headers)).read()==b'data: complete\\n\\n'
"""
                    subprocess.run(
                        [sys.executable, "-c", code],
                        env={
                            **os.environ,
                            "FREVANA_API_BASE_URL": base,
                            "FREVANA_TOKEN": "first",
                        },
                        check=True,
                        timeout=10,
                    )
                    exchange.assert_called_once()
                self.assertEqual(
                    events,
                    [
                        (
                            "POST",
                            "/agent-app-external-run/submit",
                            "Bearer first",
                            "unique",
                            b"payload",
                        ),
                        ("GET", "/agent-app-external-run/jobs/job1", "Bearer second"),
                    ],
                )
            finally:
                server.shutdown()
                thread.join()

    def test_lost_submit_response_is_never_replayed(self):
        events = []

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                events.append(self.path)
                # The supplier accepted the job, then the connection closed without a response.
                self.close_connection = True

            def do_GET(self):
                events.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"complete")

        with ThreadingHTTPServer(("127.0.0.1", 0), Api) as upstream:
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                self.creds.api = f"http://127.0.0.1:{upstream.server_port}"
                with forwarding(self.creds, "grant-a", "first") as base:
                    headers = {"Authorization": "Bearer first"}
                    submit = urllib.request.Request(
                        base + "/submit", data=b"work", headers=headers
                    )
                    with self.assertRaises(urllib.error.HTTPError) as lost:
                        urllib.request.urlopen(submit)
                    self.assertEqual(lost.exception.code, 502)
                    query = urllib.request.Request(
                        base + "/jobs/saved", headers=headers
                    )
                    with urllib.request.urlopen(query) as response:
                        self.assertEqual(response.read(), b"complete")
                self.assertEqual(
                    events,
                    [
                        "/agent-app-external-run/submit",
                        "/agent-app-external-run/jobs/saved",
                    ],
                )
            finally:
                upstream.shutdown()
                thread.join()

    def test_concurrent_refresh_replaces_one_pair_and_rejects_switched_account(self):
        self.now = 1000
        with patch(
            "frevana_runner.auth.request_json",
            return_value={
                "access_token": "new",
                "refresh_token": "next",
                "expires_in": 900,
                "grant_id": "grant-a",
                "token_type": "Bearer",
            },
        ) as exchange:
            with ThreadPoolExecutor(max_workers=4) as pool:
                others = [
                    Credentials(
                        root=self.tmp.name, backend=self.backend, clock=lambda: self.now
                    )
                    for _ in range(4)
                ]
                self.assertEqual(
                    [s["access_token"] for s in pool.map(lambda c: c.token(), others)],
                    ["new"] * 4,
                )
            exchange.assert_called_once()
            with self.assertRaises(RunnerError):
                self.creds.token(expected_grant="other")

    def test_share_policy_rechecked_and_ref_removed_after_revocation(self):
        desc = {
            "share_id": "share-id",
            "agent_app_id": "test-app",
            "snapshot_id": "s1",
            "sha256": "ab",
        }
        reqs = []

        def descriptor(*args, **kwargs):
            reqs.append(kwargs)
            if len(reqs) == 3:
                raise ApiError(403, "DOWNLOAD_NOT_ALLOWED")
            return desc

        def fixture(_desc, path):
            with zipfile.ZipFile(path, "w") as f:
                f.writestr("bundle/skillpack.json", '{"name":"test"}')
                f.writestr(
                    "bundle/job.json", '{"jobs":[{"id":"audit","name":"Audit"}]}'
                )

        with (
            patch("frevana_runner.packs.request_json", side_effect=descriptor),
            patch("frevana_runner.packs.download", side_effect=fixture) as dl,
        ):
            first = prepare(self.creds, "share")
            self.assertEqual(prepare(self.creds, "share"), first)
            self.assertEqual(first["jobs"][0]["id"], "audit")
            with self.assertRaises(ApiError):
                prepare(self.creds, "share")
            dl.assert_called_once()
        self.assertTrue(all(r["headers"] == {"x-policy-version": "2"} for r in reqs))
        self.assertFalse(list(self.creds.root.glob("grants/*/shares/share.json")))

    def test_expired_signed_url_renews_descriptor_once(self):
        desc = {
            "share_id": "share-id",
            "agent_app_id": "a",
            "snapshot_id": "1",
            "sha256": "a",
        }

        def expired(*args):
            raise urllib.error.HTTPError(
                "https://example.test", 403, "expired", None, None
            )

        with (
            patch("frevana_runner.packs.request_json", return_value=desc) as request,
            patch("frevana_runner.packs.download", side_effect=expired),
        ):
            with self.assertRaisesRegex(RunnerError, "403"):
                prepare(self.creds, "test")
            self.assertEqual(request.call_count, 2)

    def test_checksum_mismatch_never_installs(self):
        desc = {
            "share_id": "share-id",
            "agent_app_id": "a",
            "snapshot_id": "1",
            "sha256": "bad",
            "download_url": "https://example.test/signed",
        }
        with (
            patch("frevana_runner.packs.request_json", return_value=desc),
            patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")),
            self.assertRaisesRegex(RunnerError, "SHA-256"),
        ):
            prepare(self.creds, "test")
        self.assertFalse(list(self.creds.root.glob("grants/*/shares/test.json")))

    def test_archive_rejects_traversal_and_symlinks(self):
        for filename, mode in [
            ("../outside", 0),
            ("nested/link", stat.S_IFLNK | 0o777),
            ("nested/NUL.txt", 0),
        ]:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as f:
                entry = zipfile.ZipInfo(filename)
                entry.external_attr = mode << 16
                f.writestr(entry, "destination")
            with self.assertRaises(RunnerError):
                unpack(archive, Path(self.tmp.name))

    def test_share_url_environment(self):
        self.assertEqual(
            share_code(
                "https://www.frevana.com/zh-CN/agentapp/s/Abc/preview", "production"
            ),
            "Abc",
        )
        for suffix in ("", "/preview", "/preview/"):
            self.assertEqual(
                share_code(
                    "https://www.frevana.com/agentapp/s/Abc" + suffix, "production"
                ),
                "Abc",
            )
        with self.assertRaises(RunnerError):
            share_code("https://www.frevana.com/agentapp/s/Abc", "development")
