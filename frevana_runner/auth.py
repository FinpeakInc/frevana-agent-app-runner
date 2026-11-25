"""The native public client. Only this module accesses the system credential store."""

import base64
import hashlib
import hmac
import json
import platform
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
SCOPES = "agent-app:download agent-app:run"
ORIGINS = {
    "production": "https://api.frevana.com",
    "development": "https://api-dev.frevana.com",
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


def request_json(url, *, form=None, bearer=None, method=None, headers=None):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
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


def authorize(api, browser=webbrowser.open):
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
            query = urllib.parse.parse_qs(url.query)
            if (
                url.path != "/callback"
                or self.headers.get("Host") != server.server_name_port
                or not hmac.compare_digest(query.get("state", [""])[0], state)
            ):
                self.send_error(400)
                return
            result["code"] = query.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to your coding agent to continue.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.server_name_port = f"127.0.0.1:{server.server_port}"
        redirect = f"http://{server.server_name_port}/callback"
        params = {
            "client_id": CLIENT,
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "redirect_uri": redirect,
            "scope": SCOPES,
            "state": state,
            "resource": api + "/runner",
            "prompt": "consent",
            "device_name": platform.node()[:100] or "Frevana Runner",
        }
        url = api + "/runner/oauth/authorize?" + urllib.parse.urlencode(params)
        print(
            "Authorize this device in your browser:\n" + url,
            file=sys.stderr,
            flush=True,
        )
        browser(url)
        server.timeout = 1
        end = time.monotonic() + 600
        while not result and time.monotonic() < end:
            server.handle_request()
    if not result.get("code"):
        raise RunnerError("Authorization was declined or expired.")
    return request_json(
        api + "/runner/oauth/token",
        form={
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "resource": api + "/runner",
            "code": result["code"],
        },
    )


class Credentials:
    def __init__(self, env="production", *, root=None, backend=None, clock=time.time):
        self.api = ORIGINS[env]
        self.root = (
            Path(root) if root else Path.home() / ".frevana" / "agent-app-runner"
        ) / env
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.backend = backend if backend is not None else system_keyring()
        self.lock = FileLock(str(self.root / "credentials.lock"), timeout=620)
        self.clock = clock

    def token(self, *, login=False, expected_user=None):
        with self.lock:
            raw = self.backend.get_password(CLIENT, self.api)
            saved = json.loads(raw) if raw else None
            if expected_user and (not saved or saved["user"] != expected_user):
                raise RunnerError("Runner account changed. Prepare the App again.")
            if saved and saved["expires_at"] > self.clock() + 45:
                return saved
            if saved:
                try:
                    pair = request_json(
                        self.api + "/runner/oauth/token",
                        form={
                            "client_id": CLIENT,
                            "grant_type": "refresh_token",
                            "resource": self.api + "/runner",
                            "refresh_token": saved["refresh_token"],
                        },
                    )
                except ApiError as e:
                    if e.code != "invalid_grant":
                        raise
                    self.backend.delete_password(CLIENT, self.api)
                    saved = None
                else:
                    return self._save(pair, saved["user"])
            if not login:
                raise RunnerError("Authorize the device again with prepare.")
            pair = authorize(self.api)
            user = request_json(self.api + "/runner/me", bearer=pair["access_token"])[
                "id"
            ]
            return self._save(pair, user)

    def _save(self, pair, user):
        saved = {
            "access_token": pair["access_token"],
            "refresh_token": pair["refresh_token"],
            "expires_at": self.clock() + int(pair["expires_in"]),
            "user": user,
        }
        self.backend.set_password(CLIENT, self.api, json.dumps(saved))
        return saved

    def logout(self):
        with self.lock:
            raw = self.backend.get_password(CLIENT, self.api)
            if raw:
                request_json(
                    self.api + "/runner/oauth/revoke",
                    form={
                        "token": json.loads(raw)["refresh_token"],
                        "client_id": CLIENT,
                    },
                )
                self.backend.delete_password(CLIENT, self.api)
