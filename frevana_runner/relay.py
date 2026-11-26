"""Forward one execution's Frevana requests, preserving upstream billing semantics."""

import contextlib
import hmac
import http.client
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from keyring.errors import KeyringError

from .auth import ApiError, RunnerError

HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
BLOCK = {
    "host",
    "authorization",
    "cookie",
    "x-api-key",
    "forwarded",
    "expect",
    "content-length",
}


def body_chunks(handler, length, chunked):
    if chunked:
        while True:
            line = handler.rfile.readline(256)
            if not line.endswith(b"\r\n"):
                raise ValueError("Invalid chunk header")
            count = int(line.split(b";", 1)[0].strip(), 16)
            if count < 0:
                raise ValueError("Invalid chunk size")
            if not count:
                while True:
                    trailer = handler.rfile.readline(8192)
                    if trailer == b"\r\n":
                        return
                    if not trailer or not trailer.endswith(b"\r\n"):
                        raise ValueError("Invalid trailer")
            yield from body_chunks(handler, count, False)
            if handler.rfile.read(2) != b"\r\n":
                raise ValueError("Incomplete chunk")
    while length:
        data = handler.rfile.read(min(65536, length))
        if not data:
            raise ValueError("Incomplete request")
        length -= len(data)
        yield data


@contextlib.contextmanager
def forwarding(credentials, grant, initial_token, *, allow_request=None):
    prefix = "/" + secrets.token_urlsafe(32)
    upstream = urlsplit(credentials.business_api)

    class Handler(BaseHTTPRequestHandler):
        timeout = 60

        def log_message(self, *_):
            pass

        def forward(self):
            local = f"127.0.0.1:{server.server_port}"
            if (
                self.headers.get("host") != local
                or not self.path.startswith(prefix + "/")
                or not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(),
                    ("Bearer " + initial_token).encode(),
                )
            ):
                self.send_error(403)
                return
            target = self.path[len(prefix) :]
            if target.startswith("//") or urlsplit(target).netloc:
                self.send_error(400)
                return
            if allow_request is not None and not allow_request(self.command, target):
                self.send_error(403, "Endpoint outside the Runner test scope")
                return
            try:
                token = credentials.token(expected_grant=grant)["access_token"]
            except (ApiError, OSError, KeyringError):
                self.send_error(503, "Runner could not refresh the credential")
                return
            except RunnerError:
                self.send_error(401, "Runner authorization unavailable")
                return
            connection_type = (
                http.client.HTTPSConnection
                if upstream.scheme == "https"
                else http.client.HTTPConnection
            )
            conn = connection_type(upstream.hostname, upstream.port, timeout=600)
            started = False
            try:
                hop = HOP | {
                    s.strip().lower()
                    for s in self.headers.get("Connection", "").split(",")
                }
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in (hop | BLOCK)
                    and not k.lower().startswith("x-forwarded-")
                }
                headers["Authorization"] = "Bearer " + token
                size = self.headers.get("Content-Length")
                encoding = self.headers.get("Transfer-Encoding", "").lower()
                if (
                    encoding not in ("", "chunked")
                    or (encoding and size is not None)
                    or (size is not None and not size.isdigit())
                ):
                    self.send_error(400)
                    return
                if size is not None:
                    headers["Content-Length"] = size
                body = (
                    body_chunks(self, int(size or 0), bool(encoding))
                    if size is not None or encoding
                    else None
                )
                conn.request(
                    self.command,
                    upstream.path.rstrip("/") + target,
                    body=body,
                    headers=headers,
                    encode_chunked=bool(encoding),
                )
                response = conn.getresponse()
                self.send_response_only(response.status, response.reason)
                skip = (
                    HOP
                    | {"set-cookie"}
                    | {
                        s.strip().lower()
                        for s in response.getheader("Connection", "").split(",")
                    }
                )
                for k, v in response.getheaders():
                    if k.lower() not in skip:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                started = True
                if self.command != "HEAD":
                    while data := response.read1(65536):
                        self.wfile.write(data)
                        self.wfile.flush()
            except (OSError, ValueError, http.client.HTTPException):
                if not started:
                    self.send_error(
                        502,
                        "Upstream request did not complete; check existing job status",
                    )
            finally:
                self.close_connection = True
                conn.close()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = forward

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}{prefix}"
        finally:
            server.shutdown()
            thread.join()
