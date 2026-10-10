# AI Fleet Coordinator

Coordination repo for Jay Wedgeworth's multi-agent coding fleet.  This is not a product app.  It holds the binding protocols, onboarding scripts, CI templates, Mac process inventory, and the daily digest site.

GitHub About should match this file.  Do not invent seats, hosts, or jobs that are not listed here or in `fleet-apps.json`.

## How the fleet works now

1. **THE BOARD first** — `https://mac.jays.services/board` (pm2 `mac-collab` on the Mac, public via Jay's Tunnel; short link `https://board.jays.services`).  Identify, claim, resolve, and comment here before guessing from six effort-log files.  Cloud agents use the same board.  Canonical: `AGENT-SYNC.md` § THE BOARD.
2. **Zulip `#agent-sync`** — realtime claims and closeouts, realm `https://simplewithus.zulipchat.com`.  Every post is channel + topic, and the topic is the thread.  Post with the `agent-sync` CLI (`~/.local/bin/agent-sync`):  `post` for a new topic, `reply` inside one.  Credentials: `~/.secrets/Zulip/<file code>-zuliprc`, mode 600 (cloud seats use env `ZULIP_EMAIL` / `ZULIP_API_KEY` / `ZULIP_SITE`).  Slack, the pm2 `agent-sync-push` relay, and `POST https://agent-sync.jays.services/post` are retired (owner 2026-10-07).  Canonical: `AGENT-SYNC.md`; mechanics in `docs/protocols/zulip-fleet-guide.md` and at https://fleetlink.online/zulip/zulip-fleet-guide.md.
3. **Per-app effort boards** — live Mac copies (`~/apps/*-EFFORT-LOG.md`) plus each repo's `docs/EFFORT-LOG.md` and GitHub Issues.  Two-way with THE BOARD: `mac-collab-sync` (files+issues → board) and `mac-collab-writeback` (board writes → live files + Issues).  Writeback does not push git; land `docs/EFFORT-LOG.md` in the app PR.  Protocol: `EFFORT-LOG-PROTOCOL.md` and `docs/BOARD-WRITEBACK-PROTOCOL.md`.
4. **Mac always-on** — Shellular (phone → this Mac), `mac-collab`, `grok-leader` / `grok-acp`, scout, and the rest of the inventory.  Master list: [`docs/MAC-LOCAL-PROCESSES.md`](docs/MAC-LOCAL-PROCESSES.md).  Do not invent LaunchAgents from a cloud session.
5. **Seat worktrees** — each coding seat works in `~/apps/<prefix>-<suffix>` on its own branch prefix.  Never edit in `~/Code/<App>` (the human integration tree).
6. **No app-specific Grok Bot seats.**  Grok Bot seats implement through **Cursor cloud agents**.  Their tags are `[GB-<NAME>]` — never `[GROK-BOT]`, `[CURSOR]`, or `[GROK]`, and never `GB-COMPILE`.  The live persona roster is the guide's *Who's Here*; GB personas are owner-managed, hold no fleet zuliprc, and post through the Grok Bot platform, so do not assume one is listening.  This coordinator/ops system self-id is **`AFC`**.  A fleet-wide wake is a post in `#agent-sync` › `fleet` that @-mentions each bot that must act; it is not Mac Grok (`GROK`), not Grok Build (`GROK-BUILD`), and not a per-app lane in `fleet-apps.json`.  Do not add `~/apps/<app>-grok-bot` seats.

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

Active coding seats in that file: `CLAUDE`, `CODEX`, `AG`, `CURSOR`, `GROK`, `CLUTCH`, `FX`, `MM`, `MA`, `MC`, `OPENCODE`.  It also carries retired entries (`MONET`, `DSH`, `HARNESS`, `RENOIR`, `KIMI`) and the `GROK-BUILD` alias, a retired alias of `GROK` (owner 2026-10-08).  The live seat list, with each seat's Zulip bot, is `AGENT-SYNC.md` § Seats and Identity and the guide's *Who's Here*.

## Core protocols (still binding)

1. **Agent lanes:** dedicated persistent git worktrees.  They never overwrite each other's uncommitted work.
2. **Triple claim / triple closeout:** THE BOARD + effort-board/GitHub issue + a Zulip post in the `#agent-sync` work topic at start and end of every real unit.
3. **Safe landings:** do not push directly to `main`.  Feature branch → verify → PR → merge when CI is green (`scripts/land.sh` where the app uses it).
4. **Fleet daily digest + calendars:** day-by-day HTML/Markdown of public-repo merged PRs and issue churn, plus two ICS feeds.  Hosted on GitHub Pages (see below).
5. **Apple Notes for owner review:** plans, designs, reviews, and completion notes go in folder **`Coding`** (local folder on this Mac, intentionally non-iCloud), pinned.  Title `[APP, Agent] short topic`.  Helper: `scripts/apple-notes-coding.sh`.  Full rule in `AGENT-SYNC.md`.
6. **Prior messages stay in scope:** new owner messages **add** work; they do **not** cancel earlier asks unless the owner explicitly contradicts, cancels, or clearly redirects.
7. **Secrets:** Infisical is the sole source of truth for **app runtime** secrets.  `~/.secrets/global-api-keys` is handoff-only (names-only inspectable via `GET https://mac.jays.services/files/key-names` with Bearer `$MAC_COLLAB_TOKEN`).  Never mix `COOLIFY_AGENTS` into app Infisical as `COOLIFY_API_TOKEN`.  Never bare `infisical secrets`.
8. **Fleet UI copy:** Title Case headings/buttons; sentence-case values; lowercase compact money; inline iOS nav titles; nothing truncated without a full-value hover.  See `FLEET-UI-COPY.md`.
9. **App versioning & TestFlight:** `1.0.N` patch versions.  TestFlight notes use Central Time and **no** internal agent names.
10. **Fleet Skills:** Per-seat catalog in `skills/` and `docs/fleet-skills/` (`fleet-coordination`, `session-start`, `board-ops`, `secret-handoff`, `sentence-gap`, `apple-notes`, `unstick-pr`, `land-lane`, `closeout`, `deploy-verify`, `codex-triage`, `pickup-seat`, `owner-copy`, `fleet-infra`, `dns-and-registrars`, `mac-cleanup`).  `ios-ship` is omitted from every seat — Compiler / `GB-COMPILER` owns GitHub-hosted `macos-latest` iOS ship; do not teach a local Mac runner.  DealDex's hosted Actions ship stays — do not disable it.  Kimi is not installed to `~/.kimi`.  Renoir is not installed to `~/.renoir/skills` until the seat is active.  Sync via `python3 scripts/install-fleet-skills.py`.

## Setup

1. **Create the Agent Lanes:**
   Run `./scripts/setup-agent-lanes.sh <base_path>` to create the isolated Git worktrees for your agents.
2. **Initialize Zulip Chat:**
   Put each seat's bot credential at `~/.secrets/Zulip/<file code>-zuliprc`, mode 600, then subscribe the bot once:  `agent-sync subscribe --channel agent-sync --channel builds --must-exist`.  Cloud seats use env `ZULIP_EMAIL`, `ZULIP_API_KEY`, `ZULIP_SITE` instead.  Slack Sync (`./scripts/setup-slack-sync.sh`) is retired (owner 2026-10-07); do not run it.
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
| Binding protocol (board + Zulip + model economics) | [`AGENT-SYNC.md`](AGENT-SYNC.md) § THE BOARD, § Delegation & model economics | — |
| Cursor chats on desktop + iOS (Grok Bot / Shellular) | [`docs/CURSOR-CHAT-SURFACES.md`](docs/CURSOR-CHAT-SURFACES.md) | `scripts/cursor_chat_surfaces.py` |
| Universal fleet-ops skills catalog | [`docs/fleet-skills/README-add-in-app.md`](docs/fleet-skills/README-add-in-app.md) | `scripts/install-fleet-skills.py` |
| **Fresh cloud sandbox / lost CLI toolchain** | [`docs/CLOUD-AGENT-BOOTSTRAP.md`](docs/CLOUD-AGENT-BOOTSTRAP.md) ([GitHub](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/CLOUD-AGENT-BOOTSTRAP.md)) | `bash /workspace/.bootstrap.sh` |

## Fleet daily digest (HTML + Markdown + ICS)

Day-by-day outline of public repository work: **merged PRs** and **issues opened/closed**.  Built by
`scripts/build-fleet-daily-digest.py` and refreshed every 6 hours by
`.github/workflows/fleet-activity-site.yml`, which also rebuilds the per-commit
activity ICS and deploys the site to **GitHub Pages**.  The public feeds use an explicit repository allowlist and check current GitHub visibility before fetching activity.  Internal effort boards remain private inputs to separate operator workflows.

### Hosted URLs

| Artifact | URL |
|----------|-----|
| **HTML site** | https://simple-with-us.github.io/AI-Fleet-Coordinator/ |
| **Markdown** | https://simple-with-us.github.io/AI-Fleet-Coordinator/digest.md |
| **ICS — daily outline** (all-day “what shipped”) | https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/daily-digest.ics |
| **ICS — per-commit activity** | https://simple-with-us.github.io/AI-Fleet-Coordinator/calendar/agent-activity.ics |
| **THE BOARD** (auth) | https://mac.jays.services/board |
| **THE BOARD short link** | https://board.jays.services (302 → `/board`) |

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
