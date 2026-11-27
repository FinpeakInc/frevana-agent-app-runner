---
name: frevana-agent-app-runner
description: Prepare a Frevana Agent App share link and run its packaged jobs inside Codex or Claude Code using Frevana login and credits.
---

# Run a Frevana Agent App

If this chat cannot run commands on the user's computer (for example, a web chat with only a hosted sandbox), stop before installation. In the user's language, ask them to paste the same prompt into a desktop session with local command access, such as ChatGPT Desktop or Claude Code on desktop. Do not ask them to paste this guide or upload App files.

## Install or reuse the runner

Use Python 3.10 or newer on the user's computer. Keep the runner in a user-owned virtual environment at `~/.frevana/agent-app-runner/venvs/0.1.0` (under the user's home directory on Windows too). If this environment already has `frevana-agent-app-runner` version `0.1.0` and its CLI works, reuse it. Otherwise create the environment if needed and install the pinned package from PyPI. The wheel supplies the CLI and its Python modules.

On macOS or Linux, choose a Python 3.10+ interpreter for `python3`:

```sh
runner_env="$HOME/.frevana/agent-app-runner/venvs/0.1.0"
python3 -m venv "$runner_env"
"$runner_env/bin/python" -m pip install "frevana-agent-app-runner==0.1.0"
"$runner_env/bin/frevana-runner" --help
```

On Windows, use PowerShell with a Python 3.10+ interpreter:

```powershell
$runnerEnv = Join-Path $HOME '.frevana/agent-app-runner/venvs/0.1.0'
py -3 -m venv $runnerEnv
& "$runnerEnv/Scripts/python.exe" -m pip install "frevana-agent-app-runner==0.1.0"
& "$runnerEnv/Scripts/frevana-runner.exe" --help
```

Use that environment's full executable path wherever the steps below say `frevana-runner`. If Python or package installation is unavailable, tell the user which prerequisite failed before continuing.

## Prepare the App and run a task

1. Run `frevana-runner prepare "<share URL>"`. The first run opens the official Frevana consent page. If the browser stays closed, give the user the authorization URL printed by the CLI. Complete authorization within 10 minutes; if it expires, rerun `prepare` for a new link rather than refreshing the old page. The callback returns to this computer. The CLI keeps renewal credentials in the system credential store. The `/agentapp/s/<share_code>` URL, with or without `/preview` or a language prefix, selects the environment automatically: `dev-www.frevana.com` uses dev; `www.frevana.com` and `frevana.com` use production. Keep the browser on this computer. The preflight checks the `agent_app_external_run` scenario; the server checks download permissions on the actual request. If the scenario is unavailable or the download returns HTTP 403, report the CLI error and wait for the environment administrator to restore access. Keep the existing authorization and rerun `prepare` afterward; newly assigned API permissions do not require another login.
2. A successful `prepare` makes the App ready to use. It downloads and verifies the shared App snapshot when needed and returns its local `pack_root`, share, App and snapshot IDs, and job list. Read `skillpack.json`, `job.json`, and referenced skills and scripts from that local directory. Use the manifest for context and examples and the enabled jobs for their actual instructions. Follow relevant references inside `skills/`, including skills omitted from the manifest list. `app.html` is the original UI. Files under `data/` and the local job results preserve outputs.
3. Identify the user's task from the conversation before executing a job:
   - If the task is clear, select the matching job and ask only for missing required inputs or prerequisites. If everything is available, continue without showing a task menu or asking the user to repeat their request.
   - If the user only asked to install the App or pasted the share prompt, tell them the App is ready after `prepare` succeeds. When no concrete task can be inferred, briefly explain what the App can do in the user's language. Offer up to three concrete tasks, each with its expected output and required inputs or saved data, then ask the user to choose or describe another goal. Ground suggestions in enabled jobs, their instructions and examples; combine duplicate internal jobs by user-facing outcome rather than listing technical job names. Wait for the user's choice. A share link or a generic request to use the App does not select the first job or authorize a paid task.
   - If the request is outside the App's capabilities, explain the limitation and offer the closest supported tasks, if any. Wait for the user to choose; do not invent capabilities or silently substitute a different task.
4. Once the task is selected and its required inputs are available, explain any Frevana credit usage before starting a paid job. Run the package's commands through `frevana-runner exec "<share URL>" -- <program> <args...>`. The working directory is the package root. When desktop job instructions refer to `../../../skills/...`, use the corresponding `skills/...` path under the returned `pack_root`. The CLI supplies the access token and API addresses to the child process. Keep credentials out of prompts, command arguments, output and reports. A package that hardcodes its API origin needs to support `FREVANA_API_BASE_URL` or `FREVANA_API_BASE` for transparent renewal. Check required local programs and any named external tools before starting paid work.
5. For `read_agent_result` use `frevana-runner result "<share URL>" "<job id>"`. To implement `save_agent_result`, pass the complete updated data object as a JSON file with `--save <file>`. The CLI returns the saved file's absolute path. Return the requested result and a link to the local output. Reuse saved results for follow-up questions.

For example, when introducing AEO to a first-time user, recommend a website audit and ask for the website URL; do not start it without a concrete request. Explaining audit findings requires a saved report, carrying out a suggested fix requires a saved action, and drafting an article requires a saved brief. State these prerequisites when suggesting those tasks. Numbered Action or Content Writer jobs are duplicate entry points, not separate suggestions.

For a full AEO audit, use its original `fast` command once, keep the returned `workDir` and run `deep --work-dir` with that same directory until the job reports completion. If the outcome of a billed request is unknown, inspect the existing job ID or work directory before resuming. A status query can continue an existing submission. Keep paid submissions out of automatic retries.

Use `frevana-runner logout` to clear local production credentials, or `frevana-runner --environment development logout` for dev. This prints the grant ID and the correct environment's **Authorized apps** account page, where the user can revoke the server authorization. Local logout does not revoke it. A new authorization has its own cache; existing output files remain on disk. Refresh credentials are never passed to packaged scripts.
