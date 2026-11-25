# Frevana Agent App Runner

Paste the following into Codex or Claude Code and replace `<AGENT_APP_URL>` with a Frevana Agent App share link. Append a task or input if you already know what you want the App to do.

```text
Run this Frevana Agent App in our current conversation using App-token test mode.
<AGENT_APP_URL>

Resolve main at https://github.com/FinpeakInc/frevana-agent-app-runner once. Read SKILL.md and use the test runner from that same commit.
Prepare the share and read the package's job.json and referenced skills. Choose the job for my request. Ask for any missing inputs or job choice, and explain Frevana credit usage before starting a paid job. Execute the job and return its result and local output path.
```

## Set up the test account

From the Runner checkout, use Python 3.10 or later and enter your App access token at the hidden terminal prompt.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python tests/manual_app_token.py login
```

For development shares, run `.venv/bin/python tests/manual_app_token.py --environment development login` with a development token. Setup is shared across App URLs in that environment. Run `login` again when the token expires. Use `logout` with the same environment to clear the test credential from the system credential store.

The standard browser-authorized flow is in [SKILL.md](SKILL.md). The test flow uses the tester's existing Frevana account and credits.
