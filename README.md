# AI Fleet Coordinator

AI-Fleet-Coordinator (AFC) is a collection of tools, scripts, configuration, protocols, and documentation that support Jay Wedgeworth's multi-agent coding fleet.  It is separate from Muster, the Fleet Admin app.

Website: [AI Fleet Coordinator](https://simplewithus.com/ai-fleet-coordinator/)

GitHub About should match this file.  Do not invent seats, hosts, or jobs that are not listed here or in `fleet-apps.json`.

## How the fleet works now

1. **Linear first** — `https://linear.app/simple-with-us`, one team per app, ids like `BF-123`, used by agents through the `simple-board` CLI (`scripts/linear-board/`).  Identify, claim, resolve, and comment here.  Cloud agents run the same CLI from a checkout.  Canonical: `FLEET-PROTOCOL.md` § Linear and the Claim Lifecycle.
2. **Zulip `#Work-In-Progress`** — realtime claims and closeouts, realm `https://simplewithus.zulipchat.com`.  Every post is channel + topic, and the topic is the thread.  Post with the `simple-chat` CLI (`~/.local/bin/simple-chat`):  `post` for a new topic, `reply` inside one.  Credentials: Infisical Fleet-OPS `/zulip` (`<BOT>_ZULIP_EMAIL` / `_KEY` / `_SITE`, read by the CLI through `fleet-secrets`;  a cloud seat reads the same Fleet-OPS `/zulip` names through `fleet-secrets`).  Slack, the pm2 `agent-sync-push` relay, and `POST https://agent-sync.jays.services/post` are retired (owner 2026-10-07).  Canonical: `FLEET-PROTOCOL.md`; mechanics in `docs/protocols/zulip-fleet-guide.md` and at https://fleetlink.online/zulip/zulip-fleet-guide.md?raw=1.
3. **Effort logs: archived.**  The live Mac copies (`~/apps/*-EFFORT-LOG.md`) and each repo's `docs/EFFORT-LOG.md` carry an "Archived" header; every item was imported into Linear.  `mac-collab-sync`, `mac-collab-writeback` and the `effort-issues-sync` workflows are retired.  See `EFFORT-LOG-PROTOCOL.md` (a stub) and `docs/BOARD-WRITEBACK-PROTOCOL.md` (retired).
4. **Mac always-on** — Shellular (phone → this Mac), `grok-leader` / `grok-acp`, scout, and the rest of the inventory.  Master list: [`docs/MAC-LOCAL-PROCESSES.md`](docs/MAC-LOCAL-PROCESSES.md).  Do not invent LaunchAgents from a cloud session.
5. **Seat worktrees** — each coding seat works in `~/apps/<prefix>-<suffix>` on its own branch prefix.  Never edit in `~/Code/<App>` (the human integration tree).
6. **No app-specific Grok Bot seats.**  Grok Bot seats implement through **Cursor cloud agents**.  Their tags are `[GB-<NAME>]` — never `[GROK-BOT]`, `[CURSOR]`, or `[GROK]`, and never `GB-COMPILE`.  The live persona roster is the guide's *Who's Here*; GB personas are owner-managed, hold no fleet Zulip credential, and post through the Grok Bot platform, so do not assume one is listening.  This coordinator/ops system self-id is **`AFC`**.  A fleet-wide wake is a post in `#Work-In-Progress` › `fleet` that @-mentions each bot that must act; it is not Mac Grok (`GROK`), not Grok Build (`GROK-BUILD`), and not a per-app lane in `fleet-apps.json`.  Do not add `~/apps/<app>-grok-bot` seats.

## Apps and coding seats

Inventory: [`fleet-apps.json`](fleet-apps.json).  After any join, `python3 scripts/check-fleet-registry.py` must exit 0.

| App | Acronym | Kind |
|-----|---------|------|
| Socratic.Trade | ST | product |
| Congress.Trade | CT | product |
| Usage-Monitor | UM | product |
| congress-trading-shared | CTS | library |
| DealDex | DD | product |
| Personal-Site | PS | product |
| AI-Fleet-Coordinator | AFC | infra |

Active coding seats in that file: `CLAUDE`, `CODEX`, `AG`, `CURSOR`, `GROK`, `CLUTCH`, `FX`, `MM`, `MA`, `MC`, `OPENCODE`.  It also carries retired entries (`MONET`, `DSH`, `HARNESS`, `RENOIR`, `KIMI`) and the `GROK-BUILD` row, an alias of `GROK` (owner 2026-10-08;  any spelling of it is accepted as a seat name and resolves to `GROK`, owner 2026-10-10).  The live seat list, with each seat's Zulip bot, is `FLEET-PROTOCOL.md` § Seats and Identity and the guide's *Who's Here*.

## Core protocols (still binding)

1. **Agent lanes:** dedicated persistent git worktrees.  They never overwrite each other's uncommitted work.
2. **Claim and closeout:** the Linear issue (`simple-board claim`, `simple-board status`) + a Zulip post in the `#Work-In-Progress` work topic at start and end of every real unit, and a PR that says `Fixes <ID>` or `Refs <ID>`.
3. **Safe landings:** do not push directly to `main`.  Feature branch → verify → PR → merge when CI is green (`scripts/land.sh` where the app uses it).
4. **Fleet daily digest + calendars:** day-by-day HTML/Markdown of public-repo merged PRs and issue churn, plus two ICS feeds.  Hosted on GitHub Pages (see below).
5. **Apple Notes for owner review:** plans, designs, reviews, and completion notes go in folder **`Coding`** (local folder on this Mac, intentionally non-iCloud), pinned.  Title `[TEAM_KEY, Agent] Topic`.  Helper: `scripts/apple-notes-coding.sh`.  Full rule in `FLEET-PROTOCOL.md`.
6. **Prior messages stay in scope:** new owner messages **add** work; they do **not** cancel earlier asks unless the owner explicitly contradicts, cancels, or clearly redirects.
7. **Secrets:** Infisical is the sole source of truth for **app runtime** secrets.  Fleet keys live in Infisical Fleet-OPS and are read with `fleet-secrets` (nothing reads `~/.secrets`, AFC-359) (`fleet-secrets get NAME`, or `fleet-secrets run --only A,B -- CMD`);  names only are readable with `fleet-secrets names`  (the old `files/key-names` endpoint was retired on 2026-10-19;  cloud seats ask a Mac seat or the owner).  Never mix `COOLIFY_AGENTS` into app Infisical as `COOLIFY_API_TOKEN`.  Never bare `infisical secrets`.
8. **Fleet UI copy:** Title Case headings/buttons; sentence-case values; lowercase compact money; inline iOS nav titles; nothing truncated without a full-value hover.  See `FLEET-UI-COPY.md`.
9. **App versioning & TestFlight:** `1.0.N` patch versions.  TestFlight notes use Central Time and **no** internal agent names.
10. **Fleet Skills:** Per-seat catalog in `skills/` and `docs/fleet-skills/` (`fleet-coordination`, `session-start`, `board-ops`, `secret-handoff`, `sentence-gap`, `apple-notes`, `unstick-pr`, `land-lane`, `closeout`, `deploy-verify`, `codex-triage`, `pickup-seat`, `owner-copy`, `fleet-infra`, `dns-and-registrars`, `mac-cleanup`, `pr-sweep`).  `ios-ship` is omitted from every seat — Compiler / `GB-COMPILER` owns GitHub-hosted `macos-latest` iOS ship; do not teach a local Mac runner.  DealDex's hosted Actions ship stays — do not disable it.  Kimi is not installed to `~/.kimi`.  Renoir is not installed to `~/.renoir/skills` until the seat is active.  Sync via `python3 scripts/install-fleet-skills.py`.

## Setup

1. **Create the Agent Lanes:**
   Run `./scripts/setup-agent-lanes.sh <base_path>` to create the isolated Git worktrees for your agents.
2. **Initialize Zulip Chat:**
   Keep each seat's bot credential in Infisical Fleet-OPS `/zulip` (`<BOT>_ZULIP_EMAIL` / `_KEY` / `_SITE`), then subscribe the bot once:  `simple-chat subscribe --channel work-in-progress --channel builds --must-exist`.  A cloud seat reads the same credentials from the same Infisical Fleet-OPS `/zulip` path through `fleet-secrets` (`<BOT>_ZULIP_EMAIL` / `_KEY` / `_SITE`), so every seat's credential comes from the one approved source.  Slack Sync (`./scripts/setup-slack-sync.sh`) is retired (owner 2026-10-07); do not run it.
3. **Install Fleet Skills:**
   Run `python3 scripts/install-fleet-skills.py` to sync the catalog into Cursor, Antigravity, Claude Code, Codex, Grok, Grok Build, Renoir, DeepSeek, Kimi (retired), Desktop Monet, and `docs/fleet-skills/by-seat/` (Claude/Grok Bot upload packs).  Each copy is rewritten to that seat's tag, Notes name, branch prefix, and worktree.  Grok Bot copies use `[GB-<NAME>]` role tags, not `[GROK-BOT]`.  Omit a skill on a seat when it is not appropriate.  Do not leave Monet identity in another seat's folder.
4. **Apply the Rules:**
   Copy `TEMPLATE-AGENTS.md` to your own project's `AGENTS.md` and customize it.  Ensure all agents are instructed to read it.
5. **Setup GitHub Actions:**
   Copy the contents of `github-workflows-template/workflows` to your project's `.github/workflows` folder to enable automatic PR updating and (optionally) Sentry CI reporting.
6. **(Optional) Run Fleet Monitor:**
   Use the `fleet-sentry-monitor` PM2 ecosystem to track the health of your agent background processes.
7. **Subscribe to the fleet daily digest / calendars** (optional):
   See the next section.

## Onboard a new app or a new agent

Standing procedure (policy + checklist + scripts).  Do not invent a one-off join.  Do not invent a per-app Grok Bot seat.

| What | Doc | Script |
|------|-----|--------|
| New GitHub repo / `~/Code` folder joining the fleet | [`docs/ONBOARDING-NEW-APP.md`](docs/ONBOARDING-NEW-APP.md) ([GitHub](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/ONBOARDING-NEW-APP.md)) | `scripts/onboard-new-app.sh` |
| DNS / registrars (new app zone on **account** Usage.Jays.Services, not hostname `usage.jays.services`) | [`docs/DNS-AND-REGISTRARS.md`](docs/DNS-AND-REGISTRARS.md) ([GitHub](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/DNS-AND-REGISTRARS.md)) | — |
| New coding seat (Claude, Grok, Codex, …) | [`docs/ONBOARDING-NEW-AGENT.md`](docs/ONBOARDING-NEW-AGENT.md) ([GitHub](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/ONBOARDING-NEW-AGENT.md)) | `scripts/onboard-new-agent.sh` |
| Binding protocol (Linear + Zulip + model economics) | [`FLEET-PROTOCOL.md`](FLEET-PROTOCOL.md) § Linear and the Claim Lifecycle, § Delegation & model economics | — |
| Cursor chats on desktop + iOS (Grok Bot / Shellular) | [`docs/CURSOR-CHAT-SURFACES.md`](docs/CURSOR-CHAT-SURFACES.md) | `scripts/cursor_chat_surfaces.py` |
| Universal fleet-ops skills catalog | [`docs/fleet-skills/README-add-in-app.md`](docs/fleet-skills/README-add-in-app.md) | `scripts/install-fleet-skills.py` |
| Sweep and land the open PRs from any platform (Claude, Codex, Cursor, Grok, ...) | [`docs/PR-SWEEP.md`](docs/PR-SWEEP.md) | `scripts/pr-sweep` |
| **Fresh cloud sandbox / lost CLI toolchain** | [`docs/CLOUD-AGENT-BOOTSTRAP.md`](docs/CLOUD-AGENT-BOOTSTRAP.md) ([GitHub](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/CLOUD-AGENT-BOOTSTRAP.md)) | `bash /workspace/.bootstrap.sh` |

## Fleet daily digest (HTML + Markdown + ICS)

Day-by-day outline of public repository work: **merged PRs** and **issues opened/closed**.  Built by
`scripts/build-fleet-daily-digest.py` and refreshed every 6 hours by
`.github/workflows/fleet-activity-site.yml`, which also rebuilds the per-commit
activity ICS and deploys the site to **GitHub Pages**.  The public feeds use an explicit repository allowlist and check current GitHub visibility before fetching activity.  Internal Linear teams remain private inputs to separate operator workflows.

### Hosted URLs

| Artifact | URL |
|----------|-----|
| **HTML site** | https://simple-with-us.github.io/AI-Fleet-Coordinator/ |
| **Markdown** | https://simple-with-us.github.io/AI-Fleet-Coordinator/digest.md |
| **ICS — daily outline** (all-day “what shipped”) | https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/daily-digest.ics |
| **ICS — per-commit activity** | https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/agent-activity.ics |
| **Linear** (auth) | https://linear.app/simple-with-us |
| **Board short link** | https://board.jays.services (Cloudflare redirect to Linear;  the old THE BOARD archive was retired 2026-10-19) |

Raw-from-`main` fallbacks (no Pages required):

```text
https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/calendar/daily-digest.ics
https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/calendar/agent-activity.ics
https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/main/site/digest.md
```

### Subscribe (Apple / Google / Outlook)

**Recommended:** daily outline ICS (one all-day event per day with public PR/issue summary).

**Apple Calendar (iOS):** Calendar → Calendars → Add Calendar → Add Subscription Calendar → paste HTTPS URL → Find.

**Google Calendar (web):** Other calendars → + → From URL → paste URL.

**Google Calendar (iOS):** Google's mobile "Add Subscription Calendar" is picky; if it says *Validation failed*, add the same URL on [calendar.google.com](https://calendar.google.com) (web) instead — mobile will then show the subscribed calendar.

CDN alternate for the commit feed (sometimes validates more cleanly on mobile):

```text
https://cdn.jsdelivr.net/gh/Simple-With-Us/AI-Fleet-Coordinator@main/calendar/agent-activity.ics
```

### Enable GitHub Pages (one-time)

Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
The workflow uses `actions/deploy-pages`.  First successful run after that publishes
the site URL above.

### Local rebuild

```bash
export GITHUB_TOKEN="$(gh auth token)"
python3 scripts/build-agent-calendar.py
python3 scripts/build-fleet-daily-digest.py
# outputs: site/index.html, site/digest.md, calendar/daily-digest.ics
```

ICS line-folding uses `scripts/ics_utils.py` (RFC 5545 octet folding) so Apple/
Google accept long DESCRIPTION lines.

## License
Apache-2.0
