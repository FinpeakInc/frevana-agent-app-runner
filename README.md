# Frevana Agent App Runner

Paste the following into Codex or Claude Code and replace `<AGENT_APP_URL>` with a Frevana Agent App share link. Append a task or input if you already know what you want the App to do. Otherwise, the agent first reads the App's tasks and suggests what you can do.

```text
Help me install this Agent App:
<AGENT_APP_URL>

Follow the instructions at https://frevana.com/SKILL.md
```

## Browser authorization

The agent uses Python 3.10+ to install the pinned PyPI package in a user-owned virtual environment, then runs `frevana-runner prepare "<share URL>"`. Platform-specific commands and task instructions are in the [installation guide](https://frevana.com/SKILL.md).

The share URL selects dev or production automatically; an explicit `--environment` must match it. On first use, open the printed authorization URL in a browser on this computer, sign in and choose Allow. The Runner receives the callback, stores credentials in the system credential store and renews them automatically. Preparing downloads the App snapshot and makes its jobs and scripts available locally. The agent uses `frevana-runner exec` when you choose a task.

Complete the browser authorization within 10 minutes. If the page says authorization could not be completed, run `prepare` again to open a fresh link; refreshing an expired page does not restart authorization.

Each environment has its own registration, credentials and cached results. Existing legacy Runner credentials are not imported. Copies from the same grant reuse saved results; a new grant starts a separate cache without deleting old files.

`frevana-runner logout` clears local production credentials. For dev use `frevana-runner --environment development logout`. To revoke the server authorization too, open the Authorized apps URL printed by logout and locate its grant ID.

## Deployment prerequisites

Enable the `agent_app_external_run` scenario and assign `POST /agent-app-shares/:share_code/download` through the existing OAuth scenario admin page, along with the business operations required by the package. Before asking the user to authorize, the Runner checks that the public resource metadata advertises the `agent_app_external_run` scope. The server checks current download and business permissions on each request; adding an operation does not require new consent. Existing credentials, including older route-hash scopes, are reused and renewed without changing their grant or cache. A download rejected with HTTP 403 leaves the authorization intact so it can be reused after access is restored. Publish this Runner version before enabling `NEXT_PUBLIC_AGENT_APP_RUNNER_ENABLED=true` in the Web build, independently for each environment.

## Publish the package and update the website

1. For a new runner version, set the version in `pyproject.toml` and update the package pin and virtual environment paths in `SKILL.md`. From a clean checkout, run `python -m build` and `python -m twine check --strict dist/*`, then upload that release's wheel and source distribution to PyPI with Frevana's publishing account using `python -m twine upload dist/*`.
2. From outside the checkout, use a fresh virtual environment to install the exact version from PyPI as described in `SKILL.md`. Verify `frevana-runner --help` and prepare a test share.
3. Whenever `SKILL.md` changes, manually copy the complete file to `frevana-web/public/SKILL.md` in the corresponding Web change. For sibling checkouts, run `cp SKILL.md ../frevana-web/public/SKILL.md` and verify with `cmp SKILL.md ../frevana-web/public/SKILL.md`. Documentation-only edits can use the already published version.
4. Deploy the guide and verify that `https://frevana.com/SKILL.md` serves the same Markdown after redirects. When introducing a new prompt link, switch the Web copy prompt after its destination is live.
