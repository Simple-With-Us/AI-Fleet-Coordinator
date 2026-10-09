# Claude Code Cloud environments (fleet)

How to attach every fleet GitHub repo to [Claude Code on the web](https://claude.ai/code)
and give each one a setup script that actually starts.

## Why existing environments fail

Claude Code Cloud runs the **Setup script** from the **parent** of the clone
(`/home/user`), not the repo root. `git clone` creates a subdirectory named after
the repo (`Socratic.Trade/`, `Congress.Trade/`, …).

A Setup script field of `bash scripts/cloud-setup.sh` therefore exits **127**:

```
bash: scripts/cloud-setup.sh: No such file or directory
```

Socratic.Trade documented this on 2026-07-06
(`docs/rollouts/2026-07-06-cloud-setup-script-cwd-fix.md`). Congress.Trade and
later apps still shipped the bare command.

`.devcontainer/devcontainer.json` `postCreateCommand` does **not** need the `cd`.
Devcontainers already set `workspaceFolder` to the repo root.

## Canonical Setup script (paste this in every environment)

Use the same locator for every app. It works whether cwd is the clone or the
parent of the clone:

```bash
set -euo pipefail
if [ -f scripts/cloud-setup.sh ]; then
  exec bash scripts/cloud-setup.sh
fi
shopt -s nullglob
matches=(*/scripts/cloud-setup.sh)
if [ "${#matches[@]}" -eq 1 ]; then
  exec bash "${matches[0]}"
fi
echo "ERROR: scripts/cloud-setup.sh not found from $(pwd)" >&2
ls -la >&2
exit 1
```

Equivalent one-liner if you prefer the repo name:

```bash
cd <RepoName> && bash scripts/cloud-setup.sh
```

Do **not** point Claude Code Cloud at `startup.sh`. Those files are Cursor Cloud
start helpers (`cd /workspace` + background `npm run dev`) and will fail or hang
in a Claude setup script.

## Network and env vars

| Field | Fleet default |
|-------|----------------|
| Network access | **Full** (Infisical, Coolify, private package fetches, `mac.jays.services`) |
| Environment variables | Non-secret selectors only. No API keys. Cloud environments have no secrets store; anyone who can open the dialog can read the values. |
| Base branch | `main` |

Do not paste Infisical client secrets, Zulip bot keys, or `MAC_COLLAB_TOKEN` into
the environment dialog. Apps that need Infisical stay keyless in cloud and
resolve secrets only when identities are injected by a later, private path.

## Fleet recall in a cloud sandbox

`scripts/cloud-setup.sh` in `AI-Fleet-Coordinator` also wires the sandbox into the
shared fleet corpus, so a cloud seat stops concluding that the corpus is
unreachable when it meets the Cloudflare Access login page.

When an Infisical machine identity is present **in the environment**
(`INFISICAL_SHARED_CLIENT_ID` / `INFISICAL_SHARED_CLIENT_SECRET`, or the
`INFISICAL_AUTOMATION_*` pair), setup performs the same universal-auth login the
Mac-side `recall` CLI performs and writes `RECALL_API_TOKEN`,
`CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` to a `0600`
`~/.fleet-recall.env`.  It then registers the `fleet-recall` remote MCP server
(`https://recall.jays.services/mcp`) in `~/.claude.json` with `${...}` header
**placeholders** — never values.  Load the file before starting the agent:

```bash
set -a; . ~/.fleet-recall.env; set +a
bash scripts/cloud-setup.sh --check     # names and booleans only
```

This does **not** change the rule above: the identity still never goes in the
environment-variables dialog, which anyone who can open it can read.  With no
identity, setup says so, points at
[RECALL-ACCESS-CHECK.md](RECALL-ACCESS-CHECK.md), and still exits 0 — a docs/infra
repo must not fail a sandbox.

## Repos to add on claude.ai/code

Add each GitHub repo as a Claude Code project, then create (or edit) a named
cloud environment. Suggested environment name = repo name.

| Repo | `~/Code` | Setup does |
|------|----------|------------|
| `Simple-With-Us/Socratic-Trade` | Socratic.Trade | `npm ci` at repo root |
| `Simple-With-Us/Congress.Trade` | Congress.Trade | `npm ci --include=dev` in `app/` |
| `Simple-With-Us/Usage-Monitor` | Usage-Monitor | `npm ci --include=dev` |
| `Simple-With-Us/DealDex` | DealDex | `npm ci --include=dev` |
| `Simple-With-Us/Personal-Site` | Personal-Site | `npm ci --include=dev` in `site/` |
| `Simple-With-Us/Autorotate` | Autorotate | `npm ci --include=dev` in `apps/web/` |
| `Simple-With-Us/ContactLogo` | ContactLogo | `npm ci --include=dev` in `web/` |
| `Simple-With-Us/AI-Fleet-Coordinator` | AI-Fleet-Coordinator | registry check, then fleet-recall credentials + remote MCP (see below) |
| `Simple-With-Us/congress-trading-shared` | congress-trading-shared | `npm ci --include=dev` + build |
| `Simple-With-Us/fleet-ops` | fleet-ops | docs-only no-op |

`Pionex` under `~/Code` is not a git repo. Skip it.

## How to add / edit in the UI

There is no settings URL. On [claude.ai/code](https://claude.ai/code):

1. Connect the GitHub repo if it is missing from the repo picker.
2. Open the cloud-environment selector (cloud icon above the message box).
3. **Add cloud environment**, or hover an existing one and open the settings icon.
4. Name it after the repo. Set network to **Full**. Paste the locator above into
   **Setup script**. Save.
5. Archive stale environments that still use `bash scripts/cloud-setup.sh` after
   you have a working replacement — do not delete (archive only).

CLI `/remote-env` only **selects** an environment. It cannot create or edit one.

## New-app onboarding

`scripts/onboard-new-app.sh` should leave `scripts/cloud-setup.sh` in the new
repo. Then add the GitHub repo on claude.ai/code and paste the locator. See
[ONBOARDING-NEW-APP.md](ONBOARDING-NEW-APP.md).
