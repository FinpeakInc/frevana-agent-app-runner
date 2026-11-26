import io
import json
import shlex
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import manual_app_token as runner

from frevana_runner.auth import ORIGINS, ApiError, RunnerError


class SharePromptTests(unittest.TestCase):
    def test_one_login_runs_two_packages_from_their_job_instructions(self):
        jobs = {"paint": "draw", "numbers": "sum"}
        vault = {}
        store = Mock()
        store.get_password.side_effect = lambda service, api: vault.get((service, api))
        store.set_password.side_effect = lambda service, api, value: vault.update({(service, api): value})
        store.delete_password.side_effect = lambda service, api: vault.pop((service, api))

        def descriptor(url, *, method, headers, bearer):
            self.assertEqual((method, headers, bearer), ("POST", {"x-policy-version": "2"}, "tester"))
            share = url.split("/")[-2]
            return {"share_id": share, "agent_app_id": share, "snapshot_id": "v1", "sha256": "sample"}

        def download(desc, path):
            job = jobs[desc["agent_app_id"]]
            script = f"skills/{job}.py"
            with zipfile.ZipFile(path, "w") as pack:
                pack.writestr("skillpack.json", json.dumps({"name": desc["agent_app_id"]}))
                pack.writestr("job.json", json.dumps({"jobs": [{"id": job, "prompt": f"python3 {script}"}]}))
                pack.writestr(
                    script,
                    "import json, os\nfrom pathlib import Path\n"
                    "assert os.environ['FREVANA_TOKEN'] != 'tester'\n"
                    "out = Path(os.environ['AEO_DATA_ROOT']) / 'output.json'\n"
                    f"out.write_text(json.dumps({{'job': '{job}'}}))\n",
                )

        def call(*args):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runner.main(list(args)), 0)
            return output.getvalue()

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(Path, "home", return_value=Path(temp)),
            patch.object(runner, "system_keyring", return_value=store),
            patch.object(runner, "read_access_token", return_value="tester") as prompt,
            patch.object(runner, "request_json", return_value={"id": "test-user"}) as profile,
            patch("frevana_runner.packs.request_json", side_effect=descriptor),
            patch("frevana_runner.packs.download", side_effect=download),
        ):
            call("login")
            self.assertEqual(vault, {(runner.TEST_SERVICE, ORIGINS["production"]): "tester"})
            shares = {}
            for share, job in jobs.items():
                url = f"https://www.frevana.com/agentapp/s/{share}/preview"
                info = json.loads(call("prepare", url))
                self.assertEqual(info["jobs"][0]["id"], job)
                definition = json.loads((Path(info["pack_root"]) / "job.json").read_text())["jobs"][0]
                _, *arguments = shlex.split(definition["prompt"])
                call("exec", url, "--", sys.executable, "-I", *arguments)
                data = Path(info["data_root"])
                call("result", url, job, "--save", str(data / "output.json"))
                shares[job] = (url, data)
            self.assertNotEqual(shares["draw"][1], shares["sum"][1])
            for job, (url, _) in shares.items():
                self.assertEqual(json.loads(call("result", url, job)), {"job": job})
            prompt.assert_called_once()
            profile.assert_called_with(ORIGINS["production"] + "/users/me", bearer="tester")
            with self.assertRaisesRegex(RunnerError, "development login"):
                call("prepare", "https://dev-www.frevana.com/agentapp/s/paint")
            profile.side_effect = ApiError(401, "unauthorized")
            with self.assertRaisesRegex(RunnerError, "fresh token"):
                call("prepare", shares["draw"][0])
            call("logout")
            self.assertFalse(vault)

    def test_test_relay_limits_upstream_app_token_to_runner_routes(self):
        self.assertTrue(runner.allowed_request("POST", "/openai/v1/responses"))
        self.assertTrue(runner.allowed_request("GET", "/geo/ai-search/jobs/job-1?lang=en"))
        self.assertFalse(runner.allowed_request("GET", "/users/me"))
        self.assertFalse(runner.allowed_request("POST", "/auth/refresh"))
        self.assertFalse(runner.allowed_request("DELETE", "/geo/ai-search/jobs/job-1"))
