# Onboarding a new app

Policy + steps for joining an existing or brand-new GitHub repo to this fleet
so agents can work the way we already work on Socratic.Trade, Congress.Trade,
Usage-Monitor, and DealDex.

**GitHub:** https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/ONBOARDING-NEW-APP.md  
**Sibling (new seat):** [ONBOARDING-NEW-AGENT.md](ONBOARDING-NEW-AGENT.md) · https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/ONBOARDING-NEW-AGENT.md  
**Protocol:** `/Users/jay/apps/AGENT-SYNC.md` · https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/AGENT-SYNC.md  
Copy `TEMPLATE-AGENTS.md` into the new repo (includes Delegation & model economics + this start-here table).

**Run the script first, then finish the checklist.** The script does the
mechanical clone, live board bootstrap, and `fleet-apps.json` registration.  It
creates no lane (Phase 2).  The checklist guides adding AGENTS.md, repo effort log
mirror, CI, and owner-only dashboard steps.

```bash
# from an AI-Fleet-Coordinator worktree (never ~/Code/AI-Fleet-Coordinator)
./scripts/onboard-new-app.sh \
  --repo DealDex \
  --acronym DD \
  --code-dir DealDex \
  --worktree-prefix dealdex \
  --board DEALDEX-EFFORT-LOG.md \
  --slack-repo DealDex
```

`--help` lists every flag. After the script: `python3 scripts/check-fleet-registry.py`.

Canonical inventory: [`../fleet-apps.json`](../fleet-apps.json).

---

## Why this exists

A new folder under `~/Code` is **not** a fleet app yet. Peers will not see it
on the effort board, the daily digest, Zulip skim-match, Apple Notes acronyms,
or iOS ship tooling. Agents will also work in `~/Code/<App>` and collide with
the integration tree. This procedure is the self-propagation rule from
`AGENT-SYNC.md`, expanded so the next app is not a scavenger hunt.

Equally important: once an app **is** onboarded, no further top-level folder
may be added to `~/Code/` — *except* the integration tree for another
brand-new fleet app.  Per-seat worktrees, lane checkouts, scratch clones,
and `*-wt-*` directories all belong in `~/apps/`, never under `~/Code/`.
`code-main-keeper.sh` (PM2 daemon) scans `~/Code/*` every ~60s and logs a
`STRAY-WORKTREE` line to `~/apps/logs/code-main-keeper.log` for any linked
worktree it finds there.  See `AGENT-SYNC.md` § *"Hard rule: `~/Code/` is
for integration trees only"* (owner ruling 2026-09-25).

---

## Hard rules

1. **`~/Code/<App>` is the human integration tree.**  It stays on `origin/main`.
   Agents work in a lane at `~/apps/lanes/<worktreePrefix>/<seat>-<slug>`, made
   with `~/apps/lane new <app> <slug>` (`docs/protocols/lane-map.md`; flat
   `~/apps/<worktreePrefix>-<seat>` lanes already on disk retire in place).
   **No new top-level folder
   may be added to `~/Code/`** unless it is the integration tree for another
   brand-new fleet app being onboarded via this procedure — strays are
   detected and logged by `code-main-keeper.sh` and pruned by the owner.
2. **THE BOARD, then the effort board, then code.**  Create the live board +
   repo mirror in the first commit.  Claim on `https://mac.jays.services/board` (short link `https://board.jays.services`)
   and move the effort row to In Progress before substantial edits.
3. **Triple claim / triple closeout:** THE BOARD + live effort board + GitHub
   issue (via effort-issues-sync after the mirror lands) + `#agent-sync`.
   Do **not** add a per-app Grok Bot seat for the new app.  `GROK-BOT` is
   fleet-wide (Cursor cloud), not a lane in `fleet-apps.json`.
4. **Do not wait for the owner to say commit.** Feature branch → PR → merge
   when CI is green.
5. **Never paste secrets.** Handoff files live in `~/.secrets/` (`chmod 600`).
   Runtime secrets go to Infisical (the app's own project, prod env).
   Never `grep` / `rg` a handoff file without `-o` — `grep '^[A-Z0-9_]+='`
   prints **values**.  Names only: `grep -oE '^[A-Z][A-Z0-9_]*'`.  Never
   `cat` or Read `~/.secrets/global-api-keys`.  Never `od` / `xxd` /
   `hexdump` a loaded `$KEY` / `$TOKEN` / `$SECRET` variable.
6. **Light theme is the product default.** Dark is opt-in.
7. **Two spaces between sentences** in every human-facing string, including
   App Store listing and review notes.  See `AGENT-SYNC.md` § Two spaces and
   `FLEET-UI-COPY.md`.
8. **New DNS-only app zones go on Cloudflare account Usage.Jays.Services.**
   That is an account name, not the hostname `usage.jays.services`.  Create a
   new zone for the app apex (e.g. `contactlogo.com`).  Do not put new-app
   records on `usage.jays.services` or `jays.services`.  Do not mint a
   Cloudflare account, buy a domain, or enable paid Cloudflare products
   without Jay.  Playbook: [DNS-AND-REGISTRARS.md](DNS-AND-REGISTRARS.md).
9. **Default License is Apache 2.0.**  All repositories in this fleet use the
   Apache License 2.0 by default (`Copyright 2026 Jay Wedgeworth`), except
   `Fleet-OPS` which is private and proprietary (`Private.  All rights reserved.`).

---

## Phase 0 — name the app (2 minutes)

Pick and write these down. They never change casually.

| Field | Example | Notes |
|-------|---------|--------|
| GitHub repo | `DealDex` | `Simple-With-Us/<repo>` |
| `~/Code` folder | `DealDex` | Same spelling as the repo when possible |
| Zulip `repo:` | `DealDex` | `repo:` value in every #agent-sync status block |
| Acronym | `DD` | Apple Notes `[DD, Grok] …` |
| Live board file | `DEALDEX-EFFORT-LOG.md` | Lives in `/Users/jay/apps/` |
| Worktree prefix | `dealdex` | Lanes at `~/apps/lanes/dealdex/grok-<slug>` |
| Visibility | private / public | Match the product |

Post a Planned row on **fleet-infra** (`FLEET-INFRA-EFFORT-LOG.md`) *and* on
the new app board before you start.

---

## Phase 1 — GitHub + local integration tree

GitHub user accounts **cannot** attach org-wide rulesets to future repos
(there is no org).  `onboard-new-app.sh` therefore upserts
`default-main-protection` on every new app: no deleting/force-pushing
`main`, PRs required, conversation resolution on, zero required approvals
(solo owner), **not** "strict up to date".  After Phase 3 lands a `verify`
job, add it as a required check:

```bash
python3 scripts/apply-github-ruleset.py \
  --repo Simple-With-Us/<repo> --kind product --checks verify
```

Kinds: `product` / `site` / `library` (same PR gate; pass `--checks` when CI
exists) · `infra` (PR gate only — do not require digest/publish jobs).

1. If the GitHub repo does not exist:
   `gh repo create Simple-With-Us/<repo> --private --description "…"`.
2. If `~/Code/<App>` is empty or missing:
   `git clone https://github.com/Simple-With-Us/<repo>.git ~/Code/<App>`.
3. If `~/Code/<App>` already has uncommitted product work, **do not**
   `git init` on top of it. Commit or move that work first.
4. `code-main-keeper` auto-discovers new `~/Code/*` git repos. No edit needed
   unless the folder should be denylisted (`code-main-keeper.sh` `SKIP_NAMES`).
5. Do **not** create `~/Code/copilot-worktrees/<App>/` or any other new folder under
   `~/Code`.  Lanes are per task and live under `~/apps/lanes` (Phase 2).

---

## Phase 2 — agent lane + first branch

Never edit in `~/Code/<App>` after the clone.  A lane is made per task, by the
seat that is doing the task (`docs/protocols/lane-map.md`):

```bash
export AGENT_SEAT=<your seat tag>         # for example GROK; lane refuses if it is unset
~/apps/lane new <App> fleet-onboard       # lands at ~/apps/lanes/<prefix>/<seat>-fleet-onboard
cd "$(~/apps/lane path <App> fleet-onboard)"
```

The branch is your seat's registry branch prefix plus the slug (`grok/fleet-onboard`).
Do not pre-create lanes for seats that are not working yet, and do not pre-create six
`node_modules` trees.

`~/apps/lane` reads the registry copy in `~/apps/lane-tools`, so it does not know a
brand-new app until its `fleet-apps.json` row has merged and `install_tools apply
tools` has refreshed that copy.  For the onboarding PR itself, make the first lane from
your AI-Fleet-Coordinator checkout, which already holds the new row:

```bash
cd <AFC checkout>/scripts
AGENT_SEAT=<your seat tag> FLEET_APPS_JSON="$PWD/../fleet-apps.json" \
  python3 -m fleet_lanes.lane new <App> fleet-onboard
```

`scripts/setup-agent-lanes.sh` is retired (it prints a pointer here and exits 2); it
made idle lanes for every agent, which the Lane Map does not allow.

---

## Phase 3 — repo files (first commit on the feature branch)

Copy from any already-bootstrapped app (Usage-Monitor and DealDex are the
smallest references) **or** let `onboard-new-app.sh` drop the stubs.

Required:

| Path | What |
|------|------|
| `AGENTS.md` | Worktree keepout, Zulip coordination stanza, effort board path, verify commands, product traps |
| `CLAUDE.md` | Symlink to `AGENTS.md` |
| `docs/EFFORT-LOG.md` | Board mirror (template in `EFFORT-LOG-PROTOCOL.md`) |
| `/Users/jay/apps/<BOARD>.md` | Live board, same content |
| `STATUS.md` | Snapshot |
| `docs/rollouts/YYYY-MM-DD-fleet-onboard.md` | This join |
| `scripts/sync-effort-issues.py` | **Verbatim** from an existing app |
| `.github/workflows/effort-issues-sync.yml` | **Verbatim** except cron minute |
| `.github/workflows/ci.yml` | At least lint/typecheck/test on `ubuntu-latest` |
| `scripts/cloud-setup.sh` | Idempotent Claude Code Cloud / Codespaces install. Setup script cwd is the parent of the clone — see [CLAUDE-CODE-CLOUD-ENVIRONMENTS.md](CLAUDE-CODE-CLOUD-ENVIRONMENTS.md) |

If the repo's `.gitignore` ignores `AGENTS.md` (Grok / Replit leftover),
**remove that line**. The fleet pointer must be tracked.

`AGENTS.md` must include the Inter-agent coordination stanza from
`AGENT-SYNC.md` § "Onboarding a new app/repo", plus the DealDex-style
keepout table with **this** app's paths.

Optional but expected before the app is "done" as a fleet citizen:

- `sentry-ci-report.yml` + `scripts/sentry-ci-report.py` once a `CI` workflow
  exists and `SENTRY_FLEET_DSN` is on the repo.  Fingerprints stay
  `[app, workflow]` only — never branch or SHA.  Do **not** create a Sentry
  *app* project for Personal-Site (Datadog only), congress-trading-shared
  (library; consuming apps report), or fleet-ops (no runtime).
- `auto-update-prs.yml` from `github-workflows-template/`.
- `scripts/infisical-secrets-safe.sh` if the app will use Infisical.
- iOS entry in `/Users/jay/apps/ios-fleet/apps.json` if there is a native app.
- Native iOS onboarding file (`ios/CLAUDE.md`, `clients/ios/CLAUDE.md`, or
  `native/ios/CLAUDE.md`) with bundle ID, scheme, annotated file tree, and a
  pointer to AGENT-SYNC § iOS agent build loop (`xcodebuild` via bash is
  pre-approved; do not stand up Xcode MCP).
- Claude iOS write-block hook: copy
  `scripts/block-xcode-project-writes.py` → `.claude/hooks/` and merge
  `github-workflows-template/claude-ios-settings.json` into
  `.claude/settings.json`.

---

## Phase 4 — registries (the list every agent must update)

`onboard-new-app.sh` patches these. If you do it by hand, miss none:

### Live machine (`/Users/jay/apps` — not a git repo)

- `EFFORT-LOG-PROTOCOL.md` — Board registry table
- `AGENT-SYNC.md` — intro app list, Apple Notes acronym table, Zulip
  `repo:` canonical names
- `AGENT-COORDINATION-QUICKSTART.md` — effort-log table
- `FLEET-UI-COPY.md` — binding apps + theme paragraph
- `TEMPLATE-AGENTS.md` — only if you are changing the template itself
- `FLEET-INFRA-EFFORT-LOG.md` — claim / closeout row
- `/Users/jay/apps/<BOARD>.md` — new live board
- `ios-fleet/apps.json` + `ios-fleet/README.md` — if native iOS

### This repo (`AI-Fleet-Coordinator`)

- `fleet-apps.json` — **add the row first**
- `EFFORT-LOG-PROTOCOL.md`, `AGENT-SYNC.md`, `FLEET-UI-COPY.md`,
  `TEMPLATE-AGENTS.md` (keep in lockstep with `~/apps`)
- `scripts/build-fleet-daily-digest.py` — `DEFAULT_REPOS`,
  `LIVE_EFFORT_FILES`, `REPO_BADGE`, `REPO_APP_ICON`, `REPO_STRIP_ALIASES`,
  CSS color, HTML legend
- `scripts/build-agent-calendar.py` — `DEFAULT_REPOS`
- `agent-logos/app-<acronym>.png` + `agent-logos/README.md` (product apps)
- `README.md` if the new app changes setup instructions

**Google Drive + GitHub source backups.** Do **not** edit a hardcoded repo
list. `scripts/backup-fleet-to-gdrive.py` (Mac launchd
`com.jay.fleet-gdrive-backup`, daily 06:00 local) and
`.github/workflows/backup-repos.yml` both read `fleet-apps.json`. Adding the
row in Phase 4 is enough. Extra git checkouts directly under `~/Code` are
also zipped to Drive (same skip list as `code-main-keeper`:
`copilot-worktrees`, `data`, `Icons - Logos`, `Pionex`). The old
Personal-Site `backup-repos.yml` did not write Drive and is retired.

### Per-app `AGENTS.md` on **other** repos

Only if they hardcode the sibling-app list (rare). Prefer pointing at
`fleet-apps.json` / this doc.

Run `python3 scripts/check-fleet-registry.py` until it is clean.

---

## Phase 5 — Zulip + Notes

1. Read `#agent-sync` (`agent-sync inbox`, `agent-sync topics --limit 30`), then post a
   claim in a work topic such as `AFC <board8> onboard <App>`:

   ```
   [GROK] repo:  <repo>, AI-Fleet-Coordinator, fleet-infra  |  CLAIMED
   claim:  <branch>
   claimed:  <Day, Mon D, YYYY>
   work:  onboard <App> as a fleet app
   ```

2. After merge: closeout in Zulip (`DONE`, then `agent-sync resolve`), move both boards to Completed, write /
   update Apple Notes `[<ACRONYM>, FLEET, Grok] onboard <App>` in folder
   **Coding** via `/Users/jay/apps/apple-notes-coding.sh`.

---

## Phase 6 — owner dashboard (script cannot do these)

Do **not** invent these. Ask the owner or stop after listing them.

| Surface | When |
|---------|------|
| Infisical project (prod env) | Before any deployed secret |
| Coolify app + domain | Before production web |
| DNS zone (new DNS-only) | New zone for the app apex on Cloudflare **account** Usage.Jays.Services — not on `usage.jays.services`.  See [DNS-AND-REGISTRARS.md](DNS-AND-REGISTRARS.md) |
| `SENTRY_FLEET_DSN` repo secret | Before sentry-ci-report is useful |
| `FLEET_GITHUB_TOKEN` if the repo is private | So digest/calendar see it |
| App Store Connect record + bundle id | Before TestFlight |
| UptimeRobot / PagerDuty | When there is a prod URL to watch |
| Usage-Monitor producer wiring | Only if this app will push telemetry |

Coolify token split still applies: never put `COOLIFY_AGENTS` into Infisical
as app `COOLIFY_API_TOKEN`. See `AGENT-SYNC.md`.

---

## Phase 7 — land

1. PR on the **new app** (bootstrap files).
2. PR on **AI-Fleet-Coordinator** (registries + this doc if you changed it).
3. Merge both when CI is green.
4. `workflow_dispatch` Effort Issues Sync on the new app so the first board
   row becomes a GitHub issue.
5. Confirm `~/Code/<App>` fast-forwards to `main` (code-main-keeper or
   `git -C ~/Code/<App> pull --ff-only`).

---

## Offboarding An App

The inverse of onboarding.  Do these in order, each through a `claude/*` lane and PR in its owning repo.  Leave historical records intact (dated effort-log rows, closed board rows, merged PR text, old notes).

1. Land a "Retired" banner in the repo README that points at the successor, then close open issues and PRs with a one-line comment and archive the repo (`gh repo archive`).  Disable its workflows and remove its Actions secrets.
2. Stop and unload any LaunchAgent or pm2 job, and mark its row **Retired** in `MAC-LOCAL-PROCESSES.md`, then refresh the Apple Note.
3. Move local checkouts and worktrees to the Trash (never `rm -rf`), after pushing any unpushed work to an `archive/<name>` branch.
4. Remove the row from `fleet-apps.json`, the `DEFAULT_REPOS` and `FLEET_REPOS` lists, the ios-fleet `apps.json`, and `ios-app-versions.json`, then run `check-fleet-registry.py`.
5. Close open board rows with a resolution that names the successor, and add a final RETIRED row to the live effort board.
6. List App Store Connect, Sentry, and Infisical cleanup as owner follow-ups.  ASC app records cannot be deleted once builds exist.

---

## Definition of done

- [ ] `~/Code/<App>` is a git checkout of `Simple-With-Us/<repo>` on `main`
- [ ] The onboarding PR was made from a lane under `~/apps/lanes/<prefix>/`, not from `~/Code/<App>`
- [ ] `AGENTS.md` is tracked and forbids working in `~/Code/<App>`
- [ ] Live board + `docs/EFFORT-LOG.md` exist and are in the Board registry
- [ ] Effort Issues Sync workflow is on `main`
- [ ] `fleet-apps.json` has the row
- [ ] Digest + calendar `DEFAULT_REPOS` include the repo
- [ ] Apple Notes acronym table includes the acronym
- [ ] Zulip `repo:` name is in `AGENT-SYNC.md`
- [ ] `check-fleet-registry.py` exits 0
- [ ] Zulip claim + closeout posted
- [ ] Remaining owner dashboard items are listed, not silently skipped
