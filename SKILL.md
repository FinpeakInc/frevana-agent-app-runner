---
name: frevana-agent-app-runner
description: Prepare a Frevana Agent App share link and run its packaged jobs inside Codex or Claude Code using Frevana login and credits.
---

# Run a Frevana Agent App

Use the official repository at `https://github.com/FinpeakInc/frevana-agent-app-runner`. Use the commit selected for this run. If one has yet to be selected, resolve `main` once. Check out that exact commit and read its `SKILL.md` before running its CLI. Keep that checkout for the conversation. Use Python 3.10+ in an isolated virtual environment, install the checkout with `python -m pip install .` and use that environment's `frevana-runner` executable.

When the user explicitly requests **App-token test mode**, use the same checkout's `tests/manual_app_token.py` with the virtual environment's Python for each `prepare`, `exec`, and `result` command below. It selects the API environment from the share URL. The tester runs its `login` command once in their terminal per environment to store their App access token. If it is missing or expired, give the tester that command and resume after setup. Keep the token out of the conversation. The test relay supports the Frevana business endpoints currently enabled for Runner. Use this mode with trusted packages.

1. Run `frevana-runner prepare "<share URL>"`. The first run opens the official Frevana consent page. If the browser stays closed, give the user the authorization URL printed by the CLI. The callback returns to this computer. The CLI keeps renewal credentials in the system credential store. A developer share uses `--environment development` before the command.
2. Use the returned `pack_root`, App and snapshot IDs, and job list. Read `skillpack.json` for context and `job.json` for the job's actual instructions. Follow its references inside `skills/`, including referenced skills omitted from the manifest list. `app.html` is the original UI. Files under `data/` and the local job results preserve outputs. Select a job from what the user asks and collect its required inputs. Preparing the share does not start a paid job.
3. Explain any Frevana credit usage before starting a paid job. Run the package's commands through `frevana-runner exec "<share URL>" -- <program> <args...>`. The working directory is the package root. When desktop job instructions refer to `../../../skills/...`, use the corresponding `skills/...` path under the returned `pack_root`. The CLI supplies the access token and API addresses to the child process. Keep credentials out of prompts, command arguments, output and reports. A package that hardcodes its API origin needs to support `FREVANA_API_BASE_URL` or `FREVANA_API_BASE` for transparent renewal. Check required local programs and any named external tools before starting paid work.
4. For `read_agent_result` use `frevana-runner result "<share URL>" "<job id>"`. To implement `save_agent_result`, pass the complete updated data object as a JSON file with `--save <file>`. The CLI returns the saved file's absolute path. Return the requested result and a link to the local output. Reuse saved results for follow-up questions.

For AEO, use its original `fast` command once, keep the returned `workDir` and run `deep --work-dir` with that same directory until the job reports completion. If the outcome of a billed request is unknown, inspect the existing job ID or work directory before resuming. A status query can continue an existing submission. Keep paid submissions out of automatic retries.

Use `frevana-runner logout` to revoke this device's grant, or disconnect it in Frevana account settings under **Agent App Runner devices**.
