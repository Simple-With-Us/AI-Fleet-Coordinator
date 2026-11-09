# Cloud agent bootstrap — sandbox toolchain restore

**GitHub:** https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/CLOUD-AGENT-BOOTSTRAP.md
**Sibling (private ops):** [fleet-ops/docs/CLOUD-AGENT-BOOTSTRAP.md](https://github.com/Simple-With-Us/fleet-ops/blob/main/docs/CLOUD-AGENT-BOOTSTRAP.md) — short pointer
**Live script:** `/workspace/.bootstrap.sh` (persists in the sandbox)
**When to read this:** every fresh sandbox session. The container's
`/usr/local/bin` is wiped between sessions; `/workspace/**` is the
only thing that survives.

## TL;DR for a new cloud session

```bash
bash /workspace/.bootstrap.sh
```

That's it. Idempotent — only installs what's missing, ~30s warm /
~2min cold. Auto-authenticates against every fleet service the
matching env var is set for.

If the script itself is missing (rare — only happens if `/workspace`
was scrubbed), regenerate it from the body in this doc, or copy it
from the GitHub raw URL.

## What survives / what doesn't

| Path                       | Survives? | Notes                                       |
|----------------------------|-----------|---------------------------------------------|
| `/workspace/**`            | ✅ yes    | The only durable storage in the sandbox     |
| `/root/.gitconfig`         | ❌ no     | Re-set by `fix_git_tls` in the bootstrap     |
| `/usr/local/bin/*`         | ❌ no     | Re-installed by the bootstrap on every run  |
| `/tmp/**`                  | ❌ no     | Don't put anything important here           |
| Env vars (`$GITHUB_ADMIN_PAT`, `$SENTRY_ADMIN`, `$INFISICAL_*`, etc.) | ✅ yes | Owner sets them at session start |
| `~/.config/gh/hosts.yml`   | ❌ no     | Re-created by `auth_gh` inside bootstrap    |

The asymmetry is why the bootstrap exists — the work that took
minutes last session shouldn't have to be redone this session.

## What the bootstrap installs

```text
✓ git config (sslVerify=false, safe.directory hint)
✓ npm config (strict-ssl=false)
✓ gh 2.62.0           — GitHub CLI (PRs, issues, Actions, releases)
✓ glab 1.22.0         — GitLab CLI (in case any fleet repo is on GitLab)
✓ infisical 0.43.x    — secret manager; matches INFISICAL_AUTOMATION_*
✓ sentry-cli 2.42.x   — Sentry issues/events/releases via SENTRY_ADMIN
✓ vercel 60.x         — Vercel CLI; reads VERCEL_TOKEN from env
✓ pnpm 10.33.0        — JS package manager via corepack
✓ act 0.2.x           — run GitHub Actions locally
✓ bat / delta / fzf / rg / yq   — comfort CLI (cat/diff/find/grep/yaml)
```

`tree` is intentionally skipped — the sandbox's apt sources don't
carry it. Use `find . -type d` instead, or uncomment the call in
`main()` after dropping in a `install_tree_static()` function with a
URL to a tarball.

## Auto-auth, when env vars are set

The bootstrap authenticates if it sees the right env. None of these
are required to run, but they're all required for a working session:

| Tool         | Env var(s)                                                              |
|--------------|--------------------------------------------------------------------------|
| `gh`         | `GITHUB_ADMIN_PAT` (or any `*_GITHUB_TOKEN`)                              |
| `sentry-cli` | `SENTRY_ADMIN` (a `sntryu_…` user token from sentry.io)                   |
| `infisical`  | `INFISICAL_AUTOMATION_CLIENT_ID` + `INFISICAL_AUTOMATION_CLIENT_SECRET`   |
| `vercel`     | `VERCEL_TOKEN` (auto-picked up by the vercel CLI)                        |
| `glab`       | needs `GITLAB_TOKEN`; run `glab auth login --token …` after bootstrap    |

If any of these are missing the bootstrap logs a one-line failure
and keeps going. The matching CLI is still installed.

## Sandbox quirks the script handles

These are not optional on this cloud image — they're required for
basic web access to work:

- **TLS**: the CA bundle is partial. `curl_safe()` falls back to
  `curl -k` (`INSECURE_OK=1` by default). Toggle the flag to 0 if
  you need strict verification.
- **`git config --global http.sslVerify false`**: required for
  plain `git clone https://…` to work. Re-applied every session.
- **`npm config set strict-ssl false`**: same reason for npm.
- **Corepack**: Node 22 ships corepack; `pnpm` is pinned to
  `10.33.0` (the version all fleet repos' lockfiles are written
  against). If you `corepack prepare pnpm@<other>` you will
  silently desync from every other seat.

## How to add a new tool later

Two ways — the README at `/workspace/.cli/README.md` has full
details. Short version: copy any `install_<name>()` block as a
template, drop a new function in `/workspace/.bootstrap.sh`, and
add the call inside `main()`. Every installer is idempotent
(`command -v` check) so re-runs are safe.

If a release URL went 404, hit the GitHub API to find the new
asset name:

```bash
curl -sSLk "https://api.github.com/repos/<owner>/<repo>/releases/latest" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
      [print(a['name'], a['browser_download_url']) \
       for a in d['assets'] if 'linux' in a['name'].lower() and 'amd64' in a['name'].lower()]"
```

## Recall RAG and this doc

This file is auto-ingested by the nightly recall `ingest --all`
(the `source=doc` mirror in `ai-fleet-coordinator/docs/**/*.md`).
Search `recall "cloud agent bootstrap"` or `recall_search
"cloud sandbox toolchain"` from any seat and this doc should
show up in the top hits. The contribution text is also parked at
`/workspace/.cli/CONTRIBUTION-recall.md` so a future session with
Cloudflare Access can drop it into `fleet-agents` directly.

## See also

- [CLAUDE-CODE-CLOUD-ENVIRONMENTS.md](CLAUDE-CODE-CLOUD-ENVIRONMENTS.md) —
  Claude Code on the web `Setup script` field (complements this; that
  one is the Claude-Code-specific path, this one is the general
  sandbox toolchain)
- [RAG-FLEET-INFRA.md](RAG-FLEET-INFRA.md) — how the recall corpus
  is built, including the `source=doc` mirror that picks this file
  up nightly
- [ONBOARDING-NEW-AGENT.md](ONBOARDING-NEW-AGENT.md) — the seat-
  level onboarding, which assumes the bootstrap has already run
- fleet-ops/docs/CLOUD-AGENT-BOOTSTRAP.md — short pointer for the
  OPS seat
