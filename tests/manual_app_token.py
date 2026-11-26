"""Run a share prompt for a trusted Frevana App with a tester's App access token."""

import argparse
import getpass
import re
import secrets
import sys
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from keyring.errors import KeyringError

from frevana_runner import cli
from frevana_runner.auth import (
    ORIGINS,
    ApiError,
    RunnerError,
    request_json,
    system_keyring,
)
from frevana_runner.packs import share_environment
from frevana_runner.relay import forwarding

TEST_SERVICE = "frevana-agent-app-runner.app-token-test"

# The normal Runner token is scoped by the server. The test relay receives an App
# login token, so it mirrors the server's RunnerScope-marked business endpoints.
RUN_POST_PATHS = {
    "/openai/v1/chat/completions", "/openai/v1/responses",
    "/openrouter/v1/chat/completions", "/openrouter/v1/responses", "/openrouter/v1/decisions",
    "/geo/ai-search",
    "/dataforseo/google-ads-search-volume", "/service/google-ads-search-volume",
    "/service/google-search", "/service/serpapi/google-search",
    "/service/google-ai-overview", "/service/serpapi/google-ai-overview",
    "/firecrawl/web/scrape", "/firecrawl/web/start-scrape",
    "/firecrawl/web/get-scrape-status", "/firecrawl/web/map",
    "/service/web/scrape", "/service/web/start-scrape",
    "/service/web/get-scrape-status", "/service/web/map",
}


def allowed_request(method, target):
    path = urlsplit(target).path.removesuffix("/")
    if method == "POST":
        return path in RUN_POST_PATHS or bool(
            re.fullmatch(r"/agent-app-shares/[A-Za-z0-9_-]+/download", path)
        )
    return method in ("GET", "HEAD") and (
        path == "/geo/ai-search/runs/latest"
        or bool(re.fullmatch(r"/geo/ai-search/jobs/[A-Za-z0-9_-]+", path))
    )


@dataclass(frozen=True)
class TestSession:
    api: str
    root: Path
    user: str
    access_token: str = field(repr=False)

    @property
    def business_api(self):
        return self.api

    def token(self, *, login=False, expected_grant=None):
        if expected_grant is not None and expected_grant != self.user:
            raise RunnerError("Runner account changed. Prepare the App again.")
        return {"grant_id": self.user, "access_token": self.access_token}


def read_access_token(api):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        raw = getpass.getpass(f"App access token for {api} (hidden): ").strip()
    access_token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not access_token or not access_token.isascii() or not access_token.isprintable():
        raise RunnerError("Enter a valid App access token.")
    return access_token


def app_session(environment, access_token):
    api = ORIGINS[environment]
    try:
        user = request_json(api + "/users/me", bearer=access_token)["id"]
    except ApiError as error:
        if error.status == 401:
            raise RunnerError(
                f"App access token rejected. Run --environment {environment} login "
                "with a fresh token."
            ) from None
        raise
    root = Path.home() / ".frevana" / "agent-app-runner" / "app-token-test" / environment
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return TestSession(api, root, user, access_token)


def run_cli(environment, upstream, action, arguments):
    # The child gets a per-run bearer. Only the relay uses the App token upstream.
    session = upstream
    if action == "exec":
        session = replace(upstream, access_token=secrets.token_urlsafe(32))

    def relay_with_app_session(_credentials, user, child_token):
        return forwarding(upstream, user, child_token, allow_request=allowed_request)

    with (
        patch.object(cli, "Credentials", return_value=session),
        patch.object(cli, "forwarding", relay_with_app_session),
    ):
        return cli.main(["--environment", environment, action, *arguments])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment", choices=ORIGINS, help="Defaults to the share URL's environment"
    )
    parser.add_argument(
        "action", help="login, logout, prepare, exec, result, or a share URL for a one-off check"
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action in ("login", "logout"):
        if args.arguments:
            parser.error(f"{args.action} takes no arguments")
        environment = args.environment or "production"
        api = ORIGINS[environment]
        store = system_keyring()
        if args.action == "login":
            token = read_access_token(api)
            app_session(environment, token)
            store.set_password(TEST_SERVICE, api, token)
            print(f"App access token saved to the system credential store for {environment}.")
        elif store.get_password(TEST_SERVICE, api) is not None:
            store.delete_password(TEST_SERVICE, api)
        return 0

    one_off = args.action.startswith("https://")
    if not one_off and (
        args.action not in ("prepare", "exec", "result") or not args.arguments
    ):
        parser.error("Expected prepare, exec or result followed by a share URL")
    share = args.action if one_off else args.arguments[0]
    environment = share_environment(share, args.environment)
    api = ORIGINS[environment]
    if not one_off:
        token = system_keyring().get_password(TEST_SERVICE, api)
        if not token:
            raise RunnerError(
                f"Set up this test in your terminal with --environment {environment} login."
            )
        return run_cli(
            environment, app_session(environment, token), args.action, args.arguments
        )

    session = app_session(environment, read_access_token(api))
    result = run_cli(environment, session, "prepare", [share])
    if result:
        return result
    sys.stdout.flush()
    command = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if not command:
        command = [
            sys.executable,
            "-I",
            "-c",
            (
                "import json\nfrom pathlib import Path\n"
                "json.loads(Path('skillpack.json').read_text(encoding='utf-8-sig'))\n"
                "print('Runner exec OK')"
            ),
        ]
    return run_cli(environment, session, "exec", [share, "--", *command])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except getpass.GetPassWarning:
        sys.exit("Open an interactive terminal for hidden token entry.")
    except KeyringError:
        sys.exit("Open your system credential store and try again.")
    except (EOFError, KeyError, OSError, RunnerError, ValueError) as error:
        sys.exit(f"App token test failed: {error}")
