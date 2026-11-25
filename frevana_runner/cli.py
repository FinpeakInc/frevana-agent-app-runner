import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from keyring.errors import KeyringError

from .auth import ORIGINS, Credentials, RunnerError
from .packs import key, prepare, prepared, share_code, write_json
from .relay import forwarding


def main(argv=None):
    parser = argparse.ArgumentParser(prog="frevana-runner")
    parser.add_argument("--environment", choices=ORIGINS, default="production")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare").add_argument("share")
    run = sub.add_parser("exec")
    run.add_argument("share")
    run.add_argument("command", nargs=argparse.REMAINDER)
    result = sub.add_parser("result")
    result.add_argument("share")
    result.add_argument("job_id")
    result.add_argument("--save", type=Path, help="Save the JSON result from this file")
    sub.add_parser("logout")
    args = parser.parse_args(argv)
    try:
        credentials = Credentials(args.environment)
        if args.action == "logout":
            credentials.logout()
            return 0
        code = share_code(args.share, args.environment)
        if args.action == "prepare":
            print(json.dumps(prepare(credentials, code), ensure_ascii=False))
            return 0
        session = credentials.token()
        app = prepared(credentials, code, session["user"])
        if args.action == "result":
            if not any(job["id"] == args.job_id for job in app["jobs"]):
                raise RunnerError("Unknown job in this snapshot.")
            path = Path(app["data_root"]) / "results" / (key(args.job_id) + ".json")
            if args.save:
                write_json(path, json.loads(args.save.read_text(encoding="utf8")))
                print(json.dumps({"path": str(path)}))
            else:
                print(path.read_text(encoding="utf8") if path.exists() else "{}")
            return 0
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("exec needs a command after --")
        with forwarding(credentials, session["user"], session["access_token"]) as base:
            env = dict(
                os.environ,
                PACK_ROOT=app["pack_root"],
                AEO_DATA_ROOT=app["data_root"],
                FREVANA_TOKEN=session["access_token"],
                FREVANA_API_BASE=base,
                FREVANA_API_BASE_URL=base,
            )
            return subprocess.run(
                command, cwd=app["pack_root"], env=env, check=False
            ).returncode
    except KeyringError:
        print(
            "frevana-runner: Open your system credential store and try again.",
            file=sys.stderr,
        )
        return 1
    except (RunnerError, OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        print(f"frevana-runner: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
