# Frevana Agent App Runner

Paste the following into Codex or Claude Code and replace `<AGENT_APP_URL>` with a Frevana Agent App share link. Append a task or input if you already know what you want the App to do. Otherwise, the agent first reads the App's tasks and suggests what you can do.

```text
Help me use this Agent App:
<AGENT_APP_URL>

Follow SKILL.md at https://github.com/FinpeakInc/frevana-agent-app-runner.
```

## Browser authorization

Use Python 3.10+ and install the checkout in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/frevana-runner prepare "https://dev-www.frevana.com/agentapp/s/<share_code>/preview"
```

The share URL selects dev or production automatically; an explicit `--environment` must match it. On first use, open the printed authorization URL in a browser on this computer, sign in and choose Allow. The Runner receives the callback, stores credentials in the system credential store and renews them automatically. Preparing only downloads the package; follow [SKILL.md](SKILL.md) to select and execute a job.

Complete the browser authorization within 10 minutes. If the page says authorization could not be completed, run `prepare` again to open a fresh link; refreshing an expired page does not restart authorization.

Each environment has its own registration, credentials and cached results. Existing legacy Runner credentials are not imported. Copies from the same grant reuse saved results; a new grant starts a separate cache without deleting old files.

`frevana-runner logout` clears local production credentials. For dev use `frevana-runner --environment development logout`. To revoke the server authorization too, open the Authorized apps URL printed by logout and locate its grant ID.

## Deployment prerequisites

Enable the `agent_app_external_run` scenario and assign `POST /agent-app-shares/:share_code/download` through the existing OAuth scenario admin page, along with the business operations required by the package. The Runner checks the public resource metadata before asking the user to authorize. Newly assigned operations require new consent. Publish this Runner version before enabling `NEXT_PUBLIC_AGENT_APP_RUNNER_ENABLED=true` in the Web build, independently for each environment.

Publish the updated `SKILL.md` task guidance before releasing the simplified Web copy prompt.
