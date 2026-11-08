# Muster For iPhone And iPad:  Design

Status: design only, revision 9.  Nothing in this document has been built.  Written by the CLAUDE seat (session AA) on November 7, 2026, first against AI-Fleet-Coordinator `main` at `f0498ab31` (PR 1439) and last checked against `main` at `7b681284`, with BB's live facts about the server route (PR 1460, head `dc36630`), BB's answers from building the helper, BB's reduced eavesdropper-status shape, and the Muster PRs as read on November 7:  1457 merged at 4:50 PM, 1465 and 1466 at 4:57 PM, 1469 at 5:35 PM (`7b681284`);  1477 is open.  The server route's exact contract is `server-route-spec.md` (version 5) beside this file;  where the two differ, the spec wins.  **Milestones 1 to 4 are ready to build.  The trust list's reopen rule and its recovery machinery are OPEN and gate Milestone 5** (spec section 17A).  Revisions 2 to 9 answer EE's eight reviews;  the Review Response section gives every item of the eighth look its outcome, and Open Items lists everything still undecided, with an owner.  Anything marked "not verified" was not checked against a live system or a file I could read.

## Read This First:  What The Phone Can And Cannot Protect

Three facts, each checked on the server or in the code:

1. **Every server seat is already root.**  Every agent session on the server runs as jay, and jay has `(ALL : ALL) NOPASSWD: ALL` in sudo (BB, checked on the server) and is in the `docker` group (I read `/etc/group`).  No file permission, account split or check on the server can stop a server seat from doing anything, forging any reading, or editing any log.
2. **The server's control helper runs as jay.**  The `fleet-ops-ctl` key's forced command is in `/home/jay/.ssh/authorized_keys` and runs the root-owned `/usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-ssh` as jay.  Its state is in `/home/jay/.local/state/fleet-tools/` (0700), writable by every server seat and by no other account.
3. **Every Mac seat runs as the owner's account, and Muster's code comes from `main`.**  `muster-release.yml` signs and publishes Muster on every push to `main` under `muster/`, and Muster installs updates hourly "without a prompt" (`AutoUpdate.swift:7-8`).

What follows:

- **In scope:**  the internet;  the hub, which runs on the server in an internet-facing container and is treated as hostile;  an attacker who exploits `muster-pub` or `muster-exec` through the hub;  the Operator container, for the one reading it writes;  a stolen or lost phone.
- **The guarantee against them:**  no change runs on either machine without a fresh Face ID approval of that exact plan from a paired device, checked by the code that runs it;  no Mac data can be read or forged;  no result, event or widget summary the phone shows inside the app can be forged or replayed, and the phone says "Refused, nothing ran" only when a signed answer proves it.  Four exceptions, stated wherever they matter:  an attacker who exploits `muster-exec` gains the helper's read verbs and plans (rate-limited) and sees the plans and `logs` text the owner asks for;  one who exploits `muster-pub` sees every server reading in the clear;  the same attacker can forge or suppress server readings and the events and widget summary built from them, all of which the phone labels "reported by the server";  and whoever holds the push credential can put any text on the lock screen and move a Live Activity between its in-progress stages, though never show an outcome inside the app.
- **Out of scope:**  agent seats.  A server seat is root and can do every server action without the phone;  a Mac seat runs as the owner and can run the Mac helpers directly and land code in Muster.  Server readings therefore have no integrity against anyone with a shell on the server, and the phone labels them "reported by the server".
- **Owner decisions that would bring seats into scope** are wider than iOS:  narrowing jay's sudo and docker access (Question 5.2) and gating Muster's releases (Question 5.3).

## The Recommendation In Brief

Build a small owner-only service, the Muster Hub, as the one thing the phone talks to, and treat it as hostile:  it stores and relays messages that are sealed to the owner's devices and signed by the process that made them, each bound to its job and approval.  The trust root is an owner key in the Mac's Secure Enclave, usable only by Muster and only with Touch ID, pinned on the phone from the pairing QR code and on the server in a root-owned file whose full fingerprint the owner compares at the server console.  Every verifier applies the same written rules for accepting a new list of trusted keys, and the server also checks a permanent root-owned owner-key history.  Reopening a closed list (after losing the last phone) is OPEN and arrives no earlier than Milestone 5, when the phone first gains any control.  The server's helper gets a separate entry for the phone's executor that plans for it, checks the Face ID approval before taking its lock, then the plan hash and a durable nonce ledger under the lock, rate-limits every request, and signs its own answers.  Pushes go through a relay that can send only two fixed shapes.  Mac readings are signed by Muster;  server readings are labelled as the server's own claims.  Both apps decode through one shared Swift package, extracted in the first milestone.  The first milestone is a read-only iPhone app showing health, services and seats, with no executor installed anywhere.

## What I Read

- The Mac app:  `muster/README.md`, `muster/project.yml`, `MusterApp.swift`, `AutoUpdate.swift`, `Model/MusterNavigation.swift`, `Model/OpsControl.swift`, `Model/EngineRunner.swift`, `Model/ListenerStatus.swift`, `Model/WakeSettings.swift`, `Model/WakeSettingsStore.swift`, `Model/PlaceWatch.swift`, `Model/ServerStatusReport.swift`, `Model/SeatActivity.swift`, `Model/SeatActivityStore.swift`, `Model/FailureReason.swift`, `Model/TrustedCheckout.swift`, `FleetServices/FleetHost.swift`, `FleetServices/FleetSecrets.swift`, `Views/OwnerCheck.swift`, `Views/WakeSettingsView.swift`, `Views/SeatActivityView.swift`, and sweeps of every control, process launch and network call under `Muster/`.
- PR 1439 and `docs/muster-operator-zulip.md`;  PRs 1457, 1465, 1466, 1469 and 1477 read through the fleet's GitHub tools on November 7 (state, branches, merge times and descriptions, and 1477's file list).
- `scripts/fleet-ops-control.py`, `scripts/fleet_resource_control.py` (by EE's citations), `scripts/fleet_knobs/setter.py`, `scripts/fleet_rag/infisical_settings.py`, `scripts/fleet-server-status.py`, `scripts/systemd/fleet-server-status.service`, `scripts/fleet_view/collectors.py`, `docs/protocols/fleet-tool-parity.md`, `docs/operator-listener-hosting.md`, `docs/protocols/agent-sync-partition.toml`, `INFISICAL.md`, `.github/workflows/muster-release.yml`.
- Live, read-only, on the server:  jay's `authorized_keys` forced commands, `/home/jay/.local/state/fleet-tools/`, `/usr/local/lib/fleet-resource-control/`, `/etc/group`, the system and jay user units, and the line in session BB's transcript that records the owner's answer to Question 5.4.  The sudo rule, the running jay user units and the live report's disk rows are EE's and BB's checks.
- Both research passes, and every item of EE's six reviews.

## The Mac App Today

### Places And Panels

Muster's window has an Overview and fifteen panels in three places, each named once in `MusterDestination`:

- The Mac:  Services, Background Agents, Compliance Grid, Drift History, Wake & Listener, Seat Setup.
- The Server:  Server Status, Resource Limits, Server Updates.
- Across The Fleet:  Apps & Work, Seats, Controls, Tools And Worker Health, Virtual Machines, Action History.

Beside the window there is a menu bar item (`Mac OK · Server OK`), a Settings window, an apply sheet, an operations sheet, drift notifications, a service reconciler (Keep Services Running), and Sparkle updates.

### When Each Panel Reads

Services reads every two minutes app-wide;  the engine runs at launch, on watched-file changes and every ten minutes;  the doctor, Worker Health, server-status copy, mac-services and Hog Hunter files are written by their own agents.  Seats, Apps & Work, Wake & Listener and Controls read only while their panel is on screen, and Seats only while Muster is also in front (`SeatActivityStore.swift:13-14`, `:44-48`).  PR 1457, merged on November 7, holds Seat Setup's inventory readings for five minutes and polls Resource Limits only while Muster is in front.  PRs 1465 and 1466 merged at 4:57 PM:  1465 changed the Seats roster, `ListenerStatus`, `SeatActivityReader` and `FleetRegistry` (merge `3c98eaa7`), and 1466 added `FleetViewStore` and edited `ListenerStatus` and `KnobRegistry` (merge `1e823b01`).  1469 merged at 5:35 PM (`7b681284`) and changed Wake & Listener and Seat Activity again.  Still open:  1477 (on `claude/muster-grid-truth`) changes `FleetGrid`, `FleetConfigContract`, `MiniMaxSurface` and `MusterNavigation`, all of which `MusterKit` moves, plus the config reader, `FleetStore` and the doctor's checks.  `operator_voice` is already written by `daemon.py` on `main`, so 1459 is not a dependency.

### Where Every Reading Comes From

| Source | Kind | Runs Where | Feeds |
| --- | --- | --- | --- |
| `/usr/bin/python3 -m fleet_config status --json --no-doctor`, `apply SEAT KIND --plan/--yes` | Local command | The Mac, from the trusted checkout | Overview, Compliance Grid, Seat Setup, Drift History |
| `fleet-view status --json` (git in each lane, `gh`, `simple-board list`) | Local command, reaches GitHub and Linear | The Mac | Apps & Work |
| `simple-chat muster-service users` and `last-post`, as the Operator bot | Local command;  each run fetches the Operator credential from Infisical `/zulip_operator`, then sends `users/me` and one read | The Mac | Seats (last post) |
| `simple-chat daemon status --json`, `pause`, `resume`, `reload`, `check-config`, `test-wake` | Local command, no plan mode | The Mac | Wake & Listener |
| `python3 -m fleet_knobs status --json --values`, `set`, `unset` | Local command, reaches Infisical at `/`.  Muster passes neither `FLEET_KNOBS_WRITE_TOKEN` nor `FLEET_KNOBS_INFISICAL_TOKEN` (`EngineRunner.swift:150-162`), so from Muster a status read resolves to the last-known-good copy and a write refuses | The Mac | Controls |
| `fleet-secrets names`, `doctor --json`, `get NAME` | Local command, reaches Infisical | The Mac | Controls, Recall Settings, a BotFleet host token |
| `node` (pm2), `/bin/launchctl list/print` | Local command | The Mac | Services, menu |
| `python3 scripts/fleet-ops-control.py mac|pm2 ...` | Local command | The Mac | Services, Background Agents, menu |
| `python3 scripts/fleet_seat_inventory.py` | Local command | The Mac | Seat Setup |
| `~/.agent-sync/listener/status.json`, `listener.toml`, `pause.json`, wake pins, `wakes.jsonl` | Local file | The Mac | Seats (sessions, listener state, quota), Wake & Listener |
| `~/.agent-sync/listener/knobs.jsonl` | Local file, the knob writer's audit | The Mac | Nothing in Muster today |
| `~/.local/state/fleet-tools/report.json`, `worker-health.json` | Local file | The Mac | Tools And Worker Health |
| `~/.local/state/fleet-tools/server-status.json` | Local file, copied every 15 minutes from jay's report on the server | The Mac | Server Status, Overview, menu |
| `~/.local/state/fleet-tools/vms/*.json` | Local file, unscheduled pull | The Mac | Virtual Machines |
| `~/.local/state/fleet-tools/mac-services.json`, `hoghunter-mac.json` | Local file | The Mac | Background Agents |
| `~/.local/state/fleet-tools/ops-audit.jsonl` | Local file | The Mac | Action History |
| `~/.local/state/fleet-knobs/lkg.json`, the `INFISICAL.md` registry | Local file | The Mac | Controls |
| `~/Library/Application Support/Muster/drift-history.json`, `owner-stops.json` | Local file, Muster's own | The Mac | Drift History, Services |
| `scripts/fleet-ops-control.json` (the Mac checkout's copy) | Local file | The Mac | Verbs per agent, pm2 app and unit;  Server Updates |
| `docs/protocols/agent-sync-partition.toml` | Local file | The Mac | Seats grouping;  which seats Wake & Listener may change |
| `fleet-seats.json`, `fleet-apps.json`, the manifest's files, `~/.minimax` | Local file | The Mac | Grid, Seat Setup |
| BotFleet Server control API and BotFleet MCP Server health on `127.0.0.1` | Network, loopback | The Mac | Services |
| Recall `GET /admin/settings`, with the admin token fetched by name for each read | Network, https | The Mac to the server | Controls (masked rows) |
| Sparkle feed | Network, https | The Mac | Updates |
| `ssh fleet-ops-ctl -- server ...` | Server over SSH as jay, forced command, root-owned helper | The server, jay's state | Server Status, Resource Limits, Server Updates, Seat Setup's server side, Ask The Server Now |
| The doctor's `fleet-tools-ro` key | Server over SSH, read only | The server | Tools And Worker Health, the server-status copy |

Muster stores no credential.  It reads two by key name for one request each:  the recall admin token and a BotFleet host token.

## The Central Problem

Almost everything Muster shows is a local command or file on the Mac, and every action is a local command or a call over one forced-command key.  A phone can do none of that.  Parity needs somewhere off the Mac to hold readings without being trusted with them, a way to ask a machine to act that binds the owner's approval to what runs even if the relay and the relaying process are hostile, an owner identity that the hub and fleet tokens cannot impersonate and that survives losing the phone, and an honest statement of what all that protects.

## Backend Options Compared

| | A.  New Muster Hub (recommended) | B.  Extend the Operator | C.  The Mac app serves the phone through a relay | D.  Extend ST-MCP | E.  Extend the Fleet-OPS admin panel |
| --- | --- | --- | --- | --- | --- |
| Works while the Mac sleeps | Yes for server data and server actions | Yes | No, not even server data | Yes | Yes |
| Reaching the machines | Executors pull jobs outbound | Same pull pattern exists | Inbound tunnel to the owner's Mac | Executors pull | Executors pull |
| Owner identity | Owner key in the Mac's Secure Enclave, pinned on the phone and the server | Would be added beside seat principals | Phone talks to the Mac | Browser admin page only | Vercel login plus a shared token |
| What a compromise of it gives | Metadata and the ability to withhold;  no push credential if the relay holds it (Question 4.1) | The Operator's realm-admin Zulip key and every seat's routine key | A network server inside Muster | The surface every seat uses | The owner's web dashboard |
| Main risk | One more service | Owner control coupled to the listener | Mac exposure, nothing while it sleeps | Coupled to the seats' tools | Two dashboards drifting |

The Operator bot is a realm-admin service identity the owner approved for Muster's reads (AFC-654, PR 1439).  That approval covers reading Zulip on the Mac, not shipping what it reads;  Question 1.3 asks about the exact fields the phone would get.  The hub never holds the Operator's credential.

## 1.  Parity Matrix

Same means the phone shows and does what the Mac does.  Adapted means the same result in a phone-shaped form.  Read-only means the phone shows it and deliberately cannot change it.  New means the Mac does not do it today.  Not on iOS means it has no meaning off the Mac.  The milestone that brings each screen is in brackets.

### Panels

| Mac Panel | On iOS | How, And Why |
| --- | --- | --- |
| Overview (M1) | Same, adapted layout | Both place cards, verdict, grid and drift counts, from `MusterKit`.  The server card says "reported by the server".  |
| Compliance Grid (M3) | Read-only, then Send To Mac (M7) | Fixes go through the engine's Touch ID gate on the Mac.  |
| Apps & Work (M3) | Same, read-only | The phone can ask the Mac to read again (`refresh-fleet-view`), at most once per ten minutes counted from the last attempt.  The server publishes its lanes only, so pull requests and claims always come from the Mac.  Lanes merge from both hosts.  |
| Seats (M1) | Adapted for the Mac's seats;  the server's seats are New (M3) | One definition:  the `seat-activity` projection, built by Muster's own Swift decoding of the Mac's `status.json` and its `muster-service` reads, with exactly the fields in Readings And Projections.  Session titles never leave the machine they are read on, in any milestone and on any transport.  |
| Drift History (M3) | Read-only | Clear History stays on the Mac.  |
| Services (M1) | Adapted | Read-only in M1;  pm2 and allowlisted LaunchAgents with Face ID from M6.  BotFleet Server's direct route and unlisted agents stay read-only.  Login items are not on iOS.  |
| Wake & Listener (M3) | Adapted | Remote from M6:  Pause Wakes, Resume, the numeric caps.  Mac-only:  wake type, model, binaries, fallback, container rule, Test Wake, Pin.  These are the listener's own settings as the panel shows them today;  under the owner's November 7 ruling a seat wake runs no model (Wakes Are Delivery, section 2), so the phone shows them as configuration only.  |
| Controls (M3) | Read-only until a knob token decision | Knobs, last-known-good values and ages, secret names, doctor booleans, recall masked rows.  Muster cannot write a knob today (Question 6.3).  |
| Seat Setup (M3) | Same, read-only | The Mac's engine and inventory;  the server's inventory from the spool.  |
| Server Status (M1) | Same | Jay's report, projected and sealed;  Start, Stop, Restart, Logs and Redeploy with Face ID from M5, only for verbs the server helper's own allowlist names.  |
| Resource Limits (M3) | Same | `resource-status` every 15 minutes;  one field with Face ID from M5.  |
| Server Updates (M3) | Same | Run Update with Face ID from M5.  |
| Tools And Worker Health (M1) | Same | The Mac's report plus the server's own doctor run every three hours;  newer half shown.  |
| Virtual Machines (M3) | Same, read-only | |
| Background Agents (M3) | Same | LaunchAgents with Face ID from M6;  Hog Hunter read-only.  |
| Action History (M1) | Same, and wider | Both machines' `ops-audit.jsonl`, the Mac's `knobs.jsonl`, the server's `resource-policy-audit.jsonl` and the hub's job rows.  From Milestone 1 the Mac receives the server's audit tails and heartbeats from the hub as a recipient.  |

### The Menu Bar, Settings And Behaviour

| Mac Item | On iOS | How, And Why |
| --- | --- | --- |
| `Mac OK · Server OK` | Adapted (M4) | Widgets, same words, same code.  |
| Check The Mac Again | Adapted (M1) | Ask The Mac Now:  a `refresh-cheap` read request.  |
| Ask The Server Now | Adapted | Until M5, when the server last answered, from the `resource-status` spool reading.  From M5, the Mac's probe (`resource-status --plan`) on request.  |
| Timers On The Server, Fetch From The Server Again | Same (M5, M6) | With Face ID.  |
| Folders, Keep Services Running | Read-only | About This Mac.  |
| Theme | Same, the phone's own | System by default.  |
| This Build | Adapted | The phone's build, Muster's and its checkout's commit, the hub's contract.  |
| Short Menu Bar Text, Check For Updates, Launch At Login, Open Muster | Not on iOS | |
| New on the Mac:  Settings ▸ iPhone | New (M1) | Bridge on and off, pairing, devices, trust history, server install values, revocation delivery state;  reopen delivery state from M5, with reopening.  |
| Drift notifications, apply results | Adapted (M4) | Signed, sealed attention events.  |
| One action at a time | Adapted | Muster's `OpsController`;  on the server, the helper's `ops-control.lock`.  |
| Quit warning, Sparkle install gate | Mac change (M6) | Both count a phone-started run.  |
| No URL scheme, AppleScript, App Intents or Shortcuts | Kept for actions | One read-only intent is Question 4.3.  |

## 2.  Architecture

### Components

1. **Muster Hub**, `scripts/muster_hub/`:  stdlib Python, SQLite on a Coolify volume, its own Coolify application and hostname behind Cloudflare.  It stores sealed messages and the chain of signed trust lists (from M5, once spec section 17A is settled, also any pending reopen, kept apart until acknowledged), routes requests and jobs, and hands sealed events to the push relay.  It runs as a non-root user with a read-only root filesystem, no Docker socket, no host mounts other than its volume, and its own Coolify network with no route to other containers.
2. **Push relay** (M4), if the owner chooses it in Question 4.1:  a one-purpose container, hardened like the hub, that holds the APNs key and sends only the two fixed payload shapes in Push Notifications.
3. **Muster on the Mac (the bridge)**:  off until the owner turns it on with Touch ID.  Everything that touches the hub's bytes runs in a sandboxed XPC service inside Muster with no file access, which passes Muster only verified, decoded messages.  Muster holds the keys, publishes, handles read requests (M1) and runs Mac jobs (M6).
4. **Server publisher**, account `muster-pub` (M1):  unprivileged, no shell, not in `sudo`, `docker` or `fleet`, with its own home `/var/lib/muster-pub` (0700, its own), which is not the spool's parent.  It reads the spool, projects every source through a field allowlist, scrubs, signs, seals and uploads, stores and verifies trust lists (rules First, A and B;  rule C only once spec section 17A is settled, from Milestone 5;  re-reading `/etc/muster` for every verification), and sends a signed heartbeat every five minutes that doubles as its acknowledgement.
5. **Server executor**, account `muster-exec` (M5, not installed before):  unprivileged, no shell, not in `sudo`, `docker` or `fleet`, home `/var/lib/muster-exec` (0700, its own).  It long-polls the hub for server jobs and reaches the helper only through its own forced-command key.
6. **Spool producers**, as jay:  an `ExecStartPost=-` on jay's `fleet-server-status.service` and a jay user timer, `muster-spool.timer`.  They write raw files into the spool;  projection happens in `muster-pub`.
7. **Muster for iOS and iPadOS**, `muster/ios/`, with a Notification Service Extension and a widget extension, on `muster/MusterKit/`.

The two server agents, `muster_spool.py` and the shared `muster_trust_files.py` are installed root-owned in `/usr/local/lib/muster-agent/` by a reviewed installer, never run from jay's checkout;  the same `muster_trust_files.py` is installed byte-identical beside the helper.  The accounts exist so that a bug exploited through the hub lands in an account that is not root.  What each one then gives away is in the Threat Model.

```
 iPhone / iPad ──device-signed, sealed──▶ Muster Hub (hostile, hardened) ──sealed event──▶ push relay ──fixed shapes──▶ APNs
      ▲                                   ▲    ▲    ▲
      │                                   │    │    └── Muster on the Mac (XPC-isolated parsing):  readings, read requests, Mac jobs
      └───────────────────────────────────┘    │
                       muster-pub ─────────────┘  spool (root-owned, written by jay) → allowlist projection → scrub → sign → seal
                       muster-exec ── ssh jay@localhost, own forced command ──▶ fleet-ops-control.py --from-muster (plans, checks, signs)
```

### The Server Route (Milestone 5)

`server-route-spec.md` (version 5) is the exact contract.  Version 3 settled the five choices BB marked OPEN-1 to OPEN-5;  version 5's section 0 lists what BB's slice needs since version 4, including BB's own answers from the build (plan commands, audit fields, the clock, fault codes), and which parts wait for section 17A.  The owner answered Question 5.4, approving this key and entry, by relay (section 8).

- `muster-exec` gets its own line in jay's `authorized_keys`, `restrict,from="127.0.0.1,::1"`, with the forced command `/usr/bin/python3 -I /usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-muster`.  `fleet-ops-ctl`'s `--from-ssh` entry and parser do not change, every new check sits behind a fail-closed `require_approval` flag, and `cryptography` is imported only in the new branch.
- The command line is four words, `SCOPE VERB TARGET MODE`;  everything else comes on standard input (at most 4,096 bytes, or 65,536 for a trust-list chain, which a sender splits).
- **Every request that parses is rate-limited** before any signature check (requests refused while parsing are not counted, and reach no lock, signature check or action):  plans one per verb and target in 5 seconds;  runs one per verb and target in 2 seconds;  trust-list chains one in 10 seconds;  trust-list status one in 2 seconds;  and, per class across every verb and target, at most 120 plans, 60 runs, 30 chains and 600 status reads in any sliding hour (spec section 12).
- **Audit rows cannot be flooded by replays.**  An approval gets a row only once its nonce is spent, so at most one row per approval, ever, and replaying an approval (expired or not) adds none.  A chain gets a row only when accepted;  rows for refused chains are part of the OPEN recovery machinery (spec section 17A, item A2).
- **Plans are exactly what `plan_request` on `main` returns** (spec section 4):  `systemctl` for start, stop and restart and `journalctl -u UNIT -n 200 --no-pager` for logs, each with `--user` only for a user-scope unit;  `sudo -n /usr/bin/systemctl start --no-block fleet-cli-git-update.service` for the one system update;  a `["coolify","GET",…]` entry for redeploy, which the helper sends as an HTTP request and never runs;  and `fleet-resource-plan/1` for resource policy.  `argv[0]` is resolved on the server, so the phone signs the plan hash the server's own `--plan` returned, never one it computed.  The planner refuses an allowlist miss, a stale server report, a busy update or missing sudo rights with `plan_refused`.
- **Fails closed on time and faults:**  an approval is refused `clock_unsynced` when a read-only `adjtimex` call fails or reports the clock unsynchronised or more than 1 second uncertain (spec section 7;  chrony keeps this server synchronised today, as BB measured on November 7);  a fault before the nonce is spent is `internal_error`, and after it a signed result with outcome `failed`.
- **Plans without an approval** for every allowlisted verb, from the one function the run path hashes, signed by the helper.
- **Runs only with an approval:**  the helper checks the device's approval key on the stored list and the two-minute window before it takes `ops-control.lock`;  then, under the lock, it re-reads the list (refusing `trust_list_changed` if it moved), records the approval nonce durably, and runs only if its own plan hashes to the approved hash.
- **Signs every answer** with its own key (made in Milestone 5).  Results and verified refusals name the job, both nonces, the device and the plan hash, so each answer is bound to one approval.  A refusal carries job fields as verified only after the approval's signature has checked, and otherwise only as `claimed`.

Every server seat can still run the `--from-ssh` entry, read the helper key, or rewrite the trust list and ledger in jay's state.  That is the out-of-scope class;  none of the in-scope attackers is jay.

### Which Server Report Is Canonical

Jay's:  the only live writer is jay's `fleet-server-status.timer` user unit every 15 minutes, to the file the helper trusts and the doctor copies to the Mac.  The design adds one `ExecStartPost=-` and no second writer;  an upload carries the report's own `generatedAt`.  The collector's docstring still names `/root`;  Milestone 1 corrects it.

### Coolify UUIDs

UUIDs reach the phone inside the seal.  The hub never sees them unless `muster-pub` or `muster-exec` is exploited.  The redeploy plan names the application and the uuid, and the approval's hash covers both.  Event text never carries a uuid, a container name, an image name or a path.

### The Spool

`/var/lib/muster/spool/` is owned by `root:root` with mode `0770` and a POSIX ACL:  `user::rwx`, `user:jay:rwx`, `user:muster-pub:r-x`, `group::---`, `mask::rwx`, `other::---`.  Its default ACL is `default:user::rw-`, `default:user:jay:rw-`, `default:user:muster-pub:r--`, `default:group::---`, `default:mask::rw-`, `default:other::---`.  `muster-pub` can read the directory and its files but cannot create, rename or delete anything in it or change its ACL, so an exploited `muster-pub` cannot plant entries.  **The parent matters too:**  `/var/lib/muster` is `root:root` 0755, holds only `spool/`, and is nobody's home (the accounts' homes are `/var/lib/muster-pub` and `/var/lib/muster-exec`), so no account but root can rename `spool/` away or put a directory or link in its place.  `muster_spool.py` opens the spool with `O_DIRECTORY | O_NOFOLLOW`, checks with `fstat` that it is the root-owned directory described here, and does everything else relative to that descriptor (`openat`, `fchmod`, `renameat`).  It writes each file through a temporary file with a random name, created `O_CREAT | O_EXCL | O_NOFOLLOW` with mode 0640 and then `fchmod`ed to 0640 (so the mask is `r--` and `muster-pub`'s entry is effective), then renamed over the target;  it never copies with `cp` and never opens an existing name for writing.  Nothing restarts, and jay's running user units are untouched.  Every source in the spool is reported by the server, and any server seat can write it.

| Server Source | Producer | Cadence And Cost |
| --- | --- | --- |
| `server-status` | `ExecStartPost=-` on jay's status unit writes the report into the spool as above | Every 15 minutes, no new reads |
| `server-ops-audit-tail`, `resource-policy-audit-tail` | `muster-spool.timer`, last 50 rows | Every 5 minutes, local |
| `resource-status` | `muster-spool.timer` runs `server resource-status coding --plan --json` as jay | Every 15 minutes:  one Infisical read each run, 96 a day |
| `server-seat-inventory/<platform>` | `muster-spool.timer` runs `server seat-inventory PLATFORM --plan --json` | Every 6 hours, local |
| `doctor-report` | `muster-spool.timer` runs `/usr/bin/python3 -I /home/jay/.local/share/fleet-cli/repo/scripts/fleet-tools-doctor.py --json` | Every 3 hours;  its unauthenticated GitHub calls share the server's 60-an-hour budget.  PR 1477 (open) changes which runtime rows the doctor checks (the Mac's `simple-tools-runtime` path and its `com.jay.simple-tools-runtime-sync` LaunchAgent);  the projection copies tool rows by field, never by name, so the rename needs no projection change, but the golden fixtures must come from a doctor run after 1477 lands |
| `fleet-view-lanes` (M3) | A jay timer runs `fleet-view status --json --github-max 0 --linear-max 0` with `FLEET_LANES_ROOT` set | Hourly, local git only |
| `eavesdropper-status` (M3) | BB's Operator transport, in a host folder `muster-pub` reads | As the Operator writes it;  labelled "reported by the Operator" |
| `ops-allowlist` | `muster-pub` reads the helper's root-owned `fleet-ops-control.json` | Hourly |
| `heartbeat` | `muster-pub`:  its held trust-list version and hash, whether it is signed by the current owner key, and `finalRefusals` (always empty until spec section 17A is settled), signed (spec section 13).  From M5, `muster-exec` also publishes the helper-signed acknowledgement itself;  `muster-pub` never relays it | Every 5 minutes, and at once after any trust-list change or refusal |

### Eavesdropper-Status:  BB's Reduced Shape (Milestone 3)

BB builds one reducer, `scripts/agent_sync/reduced_status.py`, with a read-only `server eavesdropper-status` helper verb for the Mac (on `fleet-ops-ctl`'s entry, not on `--from-muster`) and the Operator writing the same output to a host folder for `muster-pub`.  It enters the design in Milestone 3, for the server's seats, and replaces Muster's own Mac-side decoding only if its output carries every field of the `seat-activity` projection.  Draft schema `reduced_status v1`:

```
{"schema":1,"generated_at":ISO8601Z,"instance":"server"|"mac",
 "operator_voice":{"available":bool|null,"code":str|null,"since":ISO|null},
 "pause":{"paused":bool,"since":ISO|null},
 "seats":[{"seat":"MA","connected":bool,"wake":"inbox"|"muse"|"http"|"claude"|"openrouter","paused":bool,
           "wakes_24h":int,"held_back":int,"last_event":ISO|null,
           "leases":[{"letter":"EF"|null,"platform":str,"alive":bool,"pending":int}]}]}
```

Requirements:  no `bot` address and no user id, ever;  and **the reducer emits no titles**, for either instance, on either transport, with no flag to turn them on.  BB's reducer (PR 1479) drops lease titles entirely, the title scrub goes with them, and a test checks that no title-like source string can appear in its output.  The cost:  on the Mac, a server seat's session shows its letter but no title;  titles for server sessions would be a separate, later decision (a verb-only `include_titles` with its own scrub review).  The Mac's own panel keeps reading its own titles from Muster's existing decoding, which never leaves the Mac.  Fields the Seats panel shows that the draft lacks:  per seat, `running` (three states) and `jobs`;  per lease, `verified_host` and `why`;  per seat, wakes-only pause and who set it;  skipped and disabled seats with reasons;  quota `known`, `exhausted`, `remaining`, `reset_at`, `source`, `reason`;  and `mcode` and `fallback` in `wake`.  BB's helper verb lets every holder of `fleet-ops-ctl`, including every reader of Infisical `/`, read this reduced status (Question 3.1).

### Wakes Are Delivery

The owner ruled on November 7:  "Seat wake doesn't need an agent."  Waking a seat is deterministic delivery of the message to the seat's own session, and runs no model.  The only model run in the wake path is the Operator's own, sometimes, when its code cannot decide, through the owner's OpenRouter preset built on Muse spark 1.3 contributor.  Nothing in this design drafts, suggests or sends a reply for a seat, on either machine or the phone:  the phone shows wake state, counts and history, and its only wake actions are Pause Wakes, Resume and the numeric caps (Milestone 6).  BB's draft schema still lists wake kinds `claude` and `openrouter` and the panel still shows a model setting;  the phone shows whatever the listener reports, labelled as the listener's configuration, and whether those values stay after the ruling is BB's (Open Items).

### Readings And Projections

Each reading is an envelope:  contract, host, source, schema, `collectedAt`, `publishedAt`, the publisher's trust-list version, recipients, ciphertext, and an ECDSA P-256 signature by the publisher over all of it.  Sealing is HPKE with CryptoKit's predefined `P256_SHA256_AES_GCM_256` suite, which the server's Python builds from `cryptography`'s primitives and checks against shared vectors.  Every signed or hashed object uses the canonical bytes of spec section 2:  printable ASCII strings, `"` and `\` the only escapes, `/` never escaped, keys sorted by bytes, and every `null` member written (Swift writes them with `encodeNil`, never the synthesized `encodeIfPresent`).

**Every projection is an allowlist.**  For each source and each schema version, a projection names the fields it copies;  any other field is dropped, and a source or schema version without a projection is refused, not passed through.  Projections live in `MusterKit` (Swift, for Mac sources) and in `muster-pub` (Python, for every server source, including the Operator's eavesdropper-status and the allowlist).  Golden tests check both.

**Then a scrub.**  After projection and path rewriting, the final serialised bytes pass a fail-closed scrub.  A path token is a `/` or `~/` at the start of a string or after any character other than a letter, a digit, `.`, `_`, `~` or `-`, so `key=/mnt/…`, `(/mnt/…)`, `"/var/…"` and `file:/…` all count, as do JSON-escaped `\/` forms and paths cut short by truncation.  The token runs to the next character outside `A-Za-z0-9._~/@%+-`, and is replaced with `[path]`.  The one exception is a URL whose scheme is `https` and whose host is `github.com` or `linear.app`, which Apps & Work needs.  Reason strings go through the same rewriter and scrub and are capped at 600 characters.

**Path rewriting** maps paths under known roots to rooted names (`~`, `$CHECKOUT`, `$LANES`, `$CODE`) before the scrub.  Disks are never named by mount point:  `muster-pub` maps known mounts to labels (`System Volume`, `Data Volume`) and any other to `Volume 1`, `Volume 2` in report order.

**What each Milestone 1 source carries:**

| Source | Fields |
| --- | --- |
| `seat-activity` | Per seat:  seat name, holder (`mac`, `server`, `none`), connected, paused and whether wakes-only, a wake running (yes, no, not said), lease count and each lease's letter, platform and alive flag, `isAtQuota`, quota reset time, last public post time and channel;  and the Operator's availability.  No titles, bot names, addresses, user ids or post bodies.  From Milestone 3 the server's seats come from BB's reducer, which emits no titles.  |
| `server-status` | Load, memory, disks by label, units (name, scope, states), Coolify resources (label name, uuid, state, cpu, memory), busiest processes by executable name, drifted config files by rooted name.  |
| `fleet-services` | Each Mac service:  id, title, state, policy, owner stop and time, what Muster did, allowed verbs.  |
| `place-summary` | Each place's health and the menu's lines as Muster writes them.  |
| `doctor-report`, `worker-health` | Host, tool, state, versions, basis words;  no paths.  |
| `ops-audit-tail`, `knobs-audit-tail`, `resource-policy-audit-tail` | Time, host, verb, target name, result, `via`, job id;  no command output.  |
| `ops-allowlist` | Units, agents, pm2 apps and updates with their verbs.  |
| `heartbeat`, `build-identity` | Versions, hashes, times, commits.  |

Paths, even rooted, appear only in readings sealed to the reading key (complete protection), never in events, the widget summary or anything in the clear.

### Read Requests

The phone asks the Mac to read again with a read request:  device-signed with the request key, a single-use nonce, `issuedAt` and `expiresAt` (two minutes), sealed to the Mac.  Muster handles it from a table of read verbs, which grows by milestone:

| Milestone | Read Verbs |
| --- | --- |
| 1 | `refresh-seats`, `refresh-cheap` (services, listener files, place summary) |
| 3 | `refresh-fleet-view`, `refresh-controls` (`fleet_knobs status`, `fleet-secrets names` and `doctor`), `refresh-recall` |

Only such an explicit request counts as the owner asking:  not the phone's ETag poll, a widget refresh, Background App Refresh, or anything the hub reports.  The server gets no requests before Milestone 5.

### Mac Read Budgets

| Read | Today | With The Bridge |
| --- | --- | --- |
| Seats (`muster-service`) | Only while the panel is open and Muster is in front | Only on `refresh-seats`, at most one read per 15 minutes and four per request.  BB's single-invocation `muster-service` is a precondition, so a read with about 45 seats costs 1 Infisical fetch and about 47 Zulip requests.  `SeatActivityStore`'s bridge mode records who started a read, so `setActive(false)` cancels only panel-started reads;  it shares one in-flight read with the panel and holds an App Nap activity while it runs.  |
| Apps & Work (`fleet-view`, M3) | On open and on Refresh | On `refresh-fleet-view`, through `FleetViewStore.refreshIfOlder`, with the bridge's own ten-minute floor counted from the last attempt.  PR 1466 (merged) clears `lastSuccessAt` after a failed read and adds `lastFailureAt` with a 60-second `failureRetryAfter` floor;  the bridge's floor does not depend on either.  |
| Listener files | While the panel is open, `daemon status` each minute | Files every 5 minutes while the bridge is on;  `daemon status` only on `refresh-cheap` |
| `fleet_knobs status` (M3) | On open | On `refresh-controls` only, because each run rewrites the last-known-good copy and a 60-second failure marker |
| `fleet-secrets names`, `doctor` (M3) | On open | Hourly and on `refresh-controls` |
| Recall settings (M3) | On open, fetching the admin token | On `refresh-recall` only |
| Services, engine, file sources | As today | As today |

### Attention Events And The Widget Summary

Publishers compute alerts from one rules file, `muster/contract/attention-rules.json`, and one golden set covering state transitions, asserted in Swift and Python.  Every event and every widget summary carries a random 128-bit `eventId` and its `createdAt`, is signed by its publisher's key, and is then sealed to the notification key.  The phone drops an event whose `eventId` it has seen in the last 24 hours or whose `createdAt` is more than 15 minutes old, and keeps a widget summary only if it is newer than the one it shows.  Outcome events ("Pause Wakes finished") are written only by the phone itself, from a verified, helper- or executor-signed result for an approval it was waiting for.  Events the hub originates (a publisher silent, a job queued) are labelled "Reported by the hub" and never state an outcome.  Event text names units by unit name, applications by Coolify label name and disks by label, never a container or image name, a mount point or a path.  The Mac's going-to-sleep marker travels signed and unsealed (it says only "asleep since T"), so the hub can suppress its own silence hint, and the phone and Muster decide silence themselves from signed heartbeats.

Default events (Question 4.2):  a place turning from OK to needing attention or not known;  a compliance cell turning red;  a server unit failing;  a disk or memory at 85% or more;  every seat's wakes paused;  a phone-started action finishing, failing or refused;  a server not acknowledging the latest trust list for 15 minutes.

### Push Notifications

iOS shows a plain alert push without running any app code, and applies a Live Activity's pushed state without any app code either.  So signing events protects what the app shows, not what the lock screen shows, and the push credential's holder controls the lock screen.  The relay therefore sends exactly two fixed shapes and nothing else:

1. **The alert shape:**  `alert` title `Muster`, body `Muster has an update`, `mutable-content: 1`, `content-available: 1`, the device's token, and one field holding the sealed, signed event as an opaque blob.  The relay accepts from the hub only the blob (at most 4 KB), the device id and a priority.  The Notification Service Extension replaces the fixed text with the verified event's text, and leaves the fixed text if verification fails.
2. **The Live Activity shape:**  push type `liveactivity`, event `update` only (never `end`, never `start`), the activity's push token, and `content-state` holding exactly two fields:  `stage`, one of `queued`, `planning`, `awaitingApproval`, `running`;  and `at`, a time.  No `alert` field, ever.  The relay accepts from the hub only the activity token, a stage from that list and a time, and refuses anything else.  The app ends each activity when it verifies the signed result, which needs the app to run.  For the case where nothing wakes it, the app starts each activity with a `staleDate` 30 minutes out and stale content reading "Open Muster to see the result", and no update shape carries a later `staleDate`.  Whether ActivityKit shows that stale content on the lock screen without the app running, and how long the system keeps an activity that is never ended, is not verified (Open Items).  So a push can move an activity between in-progress stages, but cannot show an outcome or any text.

If the hub holds the key instead (the other answer to Question 4.1), the hub can put any text on the lock screen, including a false "finished", and the threat model says so.  Whether Apple lets an APNs key be restricted to one topic is not verified (Open Items).

### Device Keys And Lock-Screen Access

| Key | Where | Accessibility | Used For |
| --- | --- | --- | --- |
| Approval key (ECDSA P-256) | Secure Enclave, `.biometryCurrentSet` | When unlocked, this device only | Face ID approvals and trust-list countersignatures, and nothing else |
| Request key (ECDSA P-256) | Secure Enclave | When unlocked, this device only | Signing requests and jobs;  never accepted as an approval |
| Reading key (P-256 agreement) | Secure Enclave where CryptoKit's HPKE accepts it (not verified), else keychain | When unlocked, this device only | Readings and results;  the cache uses complete protection |
| Notification key (P-256 agreement) | Keychain, shared with the extensions | After first unlock, this device only | Events and the widget summary |

The Mac's publisher and executor signing keys are ECDSA P-256 in the Secure Enclave;  its agreement key is a P-256 keychain item in Muster's group.  The server's `muster-pub` and `muster-exec` keys are ECDSA P-256 files, 0600, in their own homes;  `muster-exec` also has a P-256 agreement key.  The helper key is made in Milestone 5 (spec section 13).

### The Trust List

The exact format, rules and records are spec sections 8 to 10;  this is the design.

**Anchors.**  The owner key is ECDSA P-256 in the Mac's Secure Enclave, `.biometryAny`, in Muster's own keychain group (an entitlement and a Developer ID provisioning profile, not verified).  The phone pins it from the pairing QR code.  On the server, `muster-agent install` writes it to root-owned `/etc/muster/owner.pub` and starts the root-owned owner history `/etc/muster/owner-history.jsonl`, then prints the full 64-character fingerprint, which the owner compares with Muster and, later, with each phone.  Muster makes the owner key first (Milestone 1 step 3);  Settings ▸ iPhone ▸ Server Install shows it as text and a QR code for the installer.  The installer also prints `muster-pub`'s fingerprint (and, at Milestone 5, `muster-exec`'s and the helper's), compared the same way before Muster enrols them.  The helper, `muster-pub` and `muster-exec` read `/etc/muster` through one shared module with the same safety checks, afresh for every verification (never cached, so a code appended after `muster-pub` started is seen);  the phone and Muster cannot read it.  `muster-agent trust-status` prints each stored list's version and hash for a console comparison.

**The list** is versioned, chained by hash, signed by the owner key, and lists each device's four public keys and name and the Mac's and the server's service keys.  It has no expiry.  The hub stores every accepted version.

**Acceptance rules,** applied by every verifier walking the chain from the version it holds, or from version 1 when it holds nothing:

| Rule | A Step Is Accepted When |
| --- | --- |
| First | Version 1, signed by the owner key, at least one device, no countersignatures.  |
| A.  Normal change | Owner-signed and countersigned by the approval key of a device on the previous list;  every countersignature must verify.  |
| B.  Close | Owner-signed, no devices, no countersignatures, every service key unchanged or removed, and at least one server key (`serverHelper` or `serverPublisher`) kept if the previous list had one.  |
| C.  Reopen | After a closed list:  owner-signed, service keys as in B, every new device countersigns, and a tag made with a one-time code from the server console's permanent record, whose newest line for that version names the one device the list may hold.  The server's verifiers check the tag and the device;  the phone and Muster, which cannot read the record, apply a reopen only once a signed server acknowledgement covers it.  **OPEN:**  this is the proposal, not built before Milestone 5;  until then every verifier refuses a reopen.  |

So:  no Touch-ID-only change keeps or adds a device or a service key.  Muster offers a close only once a list names a server key (from Milestone 1, `muster-pub`'s), and never signs a change that removes both server keys (rule A does not forbid it yet:  spec 17A, item A8).

**Reopening, step by step (OPEN, gates Milestone 5).**  This is the version 4 proposal (spec section 9), kept so the open problems have a text to point at.  EE's eighth look found four majors in it, listed unsolved in spec section 17A:  retiring codes by line order breaks verifiers that walk the chain later;  final chain refusals can be minted without the owner key, through extra countersignatures and ECDSA s/n−s malleability;  a forged `muster-pub` heartbeat can freeze Muster with no exit;  and the newest-8 refusal window, against one line per entry, can wedge a reopen.  Nothing below is built before they are settled.

1. The new phone starts pairing and shows its device id (`d-` and 24 hex characters, from its approval key).  Muster shows the version it will open, N, the same device id, and the exact command:  `sudo muster-agent reopen --version N --device d-…`.
2. At the console, the owner checks that the device id on the phone in his hand matches, then runs the command.  It appends a 128-bit code for version N and that device to root-owned `/etc/muster/reopen-codes.jsonl`, which is never pruned, and prints the code as eight groups of four with a four-character check group computed over the version, the device id and the code.  A newer line for the same version retires every older one.
3. The owner types the code and check group into Muster with Touch ID.  Muster recomputes the check group for its own N and the device it is pairing, and refuses to sign on any difference, so a typo, a wrong version or a wrong device is caught before anything is signed.
4. The new phone countersigns, and Muster signs the reopen list for version N with that one device, stores it durably, and only then uploads it.  It is **pending**:  the hub keeps it apart, and neither Muster nor the phone applies it.
5. When a verified acknowledgement covers it (the helper's once the list Muster and the phone have applied names `serverHelper`, never the pending list's, otherwise `muster-pub`'s heartbeat), Muster and the phone apply it and pairing completes.
6. **Recovery** (spec section 9):  Muster discards the pending list only when a verified answer names that exact signed list, by the hash of the whole entry, as refused for a final reason, one that can never become an acceptance (a wrong tag, a wrong device, a bad countersignature, changed services).  Out-of-order refusals and an unrecorded code are not final, so they change nothing.  If an acknowledgement shows the server holds a different version N, or a later version, Muster fetches the server's chain and walks it instead of asking for a redo.  Only after a real discard does Muster ask the owner to run step 1 again for the same N, and the new line retires the old code.

**The proposal's intent, pending spec section 17A, not working properties:**  there is no time window, and a code is meant to open only the version and device it names, so that clock skew or a slow pairing could not strand a reopen;  but a second `reopen` for an accepted version retires its code, so a later walker can refuse it for good (17A, item A1).  The helper bootstrapping at Milestone 5 would walk the whole chain from version 1 against the same permanent record.  **What the code is meant to stop:**  anyone who can get an owner-key signature but has no shell on the server;  and, by naming one device, a Muster build enrolling some other device with the code the owner typed.  The second does not hold yet, because nothing ties a device entry's id to its own approval key (17A, item A7).  It binds no seat:  any server seat can run `reopen` under passwordless sudo, so while Questions 5.2 and 5.3 are open a Muster build a seat landed can close the list, have a seat run `reopen` for its own device, and enrol it.  Against the in-scope attackers it is redundant, because they cannot sign with the owner key.

**When an acknowledgement contradicts what a side holds.**  Settled for Milestones 1 to 4:  nothing is ever pending and Muster signs one list per version, so an acknowledgement or heartbeat naming an applied version with another hash can only come from a forged or broken signer.  The phone and Muster then show "The server reports a different trust list", keep working, and keep every control, the close included;  they never stop.  The version 4 "stop and adopt the server's chain" rule belongs to the OPEN reopen machinery, which has no exit yet (spec 17A, item A3).

**The last device.**  Removing the last device needs that device's Face ID (rule A).  If it is lost, Muster closes the list with Touch ID (rule B), which every verifier accepts:  the helper stops accepting the lost phone's approvals and `muster-pub` stops sealing to it as soon as the list reaches them.  A removed phone that is still running shows that it was removed.  A sole phone whose approval key died when Face ID was re-enrolled recovers the same way:  close, then reopen with a fresh pairing.  **Until reopening arrives (Milestone 5 at the earliest), a closed list stays closed.**  The phone is read-only until then, so losing the last phone costs readings on a new phone, not control.

**Revocation delivery.**  Muster applies a close or a normal change at once.  The server learns it from the hub;  `muster-pub`'s signed heartbeat (from M1) and the helper-signed acknowledgement that `muster-exec` publishes (from M5) say which version and hash each holds and whether it is signed by the current owner key.  The Mac shows "Revocation not yet delivered to the server" until both catch up, and a local notification, scheduled by Muster and the phone themselves and cancelled only by a verified acknowledgement, fires after 15 minutes, so the withholding hub cannot suppress it.  The hub can withhold a list:  that is detected, not prevented.  The emergency step is in a root shell:  `systemctl stop muster-pub muster-exec` and removing `muster-exec`'s `authorized_keys` line.

**Rotation and recovery.**  Muster offers a rotation only when the server has acknowledged every version it signed, then shows N, its highest version plus one, and from that moment signs nothing with the old key;  it signs version N with the new key only after an acknowledgement shows the server's list at N − 1 as no longer signed by the current key, which proves `repin` ran.  `repin` and `trust-status` run as root and check each stored list's expected owner (jay's for the helper's, `muster-pub`'s for its own), not their own uid.  On the server, `muster-agent repin --from-version N` at the console, after a full-fingerprint comparison of the new key, refuses unless every stored list (the helper's and `muster-pub`'s) holds the same version K with the same hash and N is K + 1;  if the server lags, the owner delivers the chain first.  It records K and K's body hash in the history, so no fork signed by the retired key can be walked, and writes the history and then `owner.pub`, each made durable (file and directory `fsync`) before the next, so an interruption leaves only the new history with the old pin, which P1 refuses and `repin --resume` finishes by rewriting `owner.pub` from the history's open entry.  On the phone and the Mac the retired key is recorded in the keychain;  how a phone accepts a rotation is an open item (AA).  If the owner key is lost, every phone re-pairs and the server is re-pinned the same way.

### Approvals Bound To What Runs

1. The phone builds a job:  a closed-set action, target, a job nonce, `issuedAt`, `expiresAt`;  it signs it with the request key and seals it to the machine's executor.
2. The executor checks the device signature, records the job nonce durably, and decides from its own verb table whether the action is a read or a change.
3. For a change, it obtains the canonical plan bytes:  on the server, from the helper's signed plan;  on the Mac, from the helper's `--plan` or Muster's listener plan, signed by Muster's executor key.  The signed plan names the claimed job fields, host, action, target, the plan bytes and their hash.  It is sealed to the device.
4. The phone checks the signature and that every named field matches the job it sent, renders Plan Review from the bytes, hashes exactly those bytes, and signs an approval with its own approval nonce under Face ID (spec section 6).  A job may need a second approval, after a plan that no longer matched;  each has its own nonce.
5. On the server, `muster-exec` checks the approval as an early refusal and passes it to the helper (spec section 12).  On the Mac, Muster checks the approval with the approval key only, then, under `OpsController`, records the approval nonce in its own approval ledger (separate from its job ledger), plans again and compares the hash, and for listener actions compares its canonical plan under `WakeSettingsStore`'s serialisation.
6. The result is signed (by the helper on the server, by Muster's executor key on the Mac) and names the job, both nonces, the device, host, action, target, plan hash, outcome, audit id and trust-list version.  The phone refuses a result that does not match the approval it is waiting for.

**How the phone reads a refusal.**  Only a refusal that is verified (the helper checked the approval's signature first) and whose code is `plan_mismatch` shows as "Refused, nothing ran":  that approval's nonce was fresh and spent at the ledger just before, so no other use of it can ever run.  Every other refusal shows as "Not known whether it ran", because each can follow a run of the same approval that an exploited `muster-exec` withheld:

| Refusal | Why It Proves Nothing |
| --- | --- |
| Unverified (`claimed`):  `bad_signature`, `unknown_device`, `field_mismatch`, or anything before the signature check (`bad_input`, `rate_limited`, `no_trust_list`, `state_unsafe`, `pin_unsafe`, `helper_key_missing`), or `internal_error` | The helper never established which approval this was, or failed before it could say.  |
| `clock_unsynced`, `expired`, `not_yet_valid`, `window_too_long` | The window is checked before the ledger, so a resubmission after a run is refused here without reaching the ledger, and the ledger forgets nonces 180 seconds after expiry.  (`window_too_long` cannot follow a run of the same approval, but one reading rule is kept for all three.)  |
| `replayed_nonce` | The approval was used before;  its outcome was not delivered.  |
| `action_busy`, `trust_list_changed`, `ledger_corrupt` | Taken before or at the ledger;  a corrupt ledger cannot tell whether the nonce was used.  |

The phone then asks for the job's result again;  a helper-signed result for that approval settles it.  A signed result reads by its outcome:  `done`, `submitted` ("Started;  the service finishes it"), `failed` (including a fault after the nonce was spent), or `unknown` ("Started;  result not known").  `plan_refused` arrives only before Plan Review, with its reason (not on the allowlist, server report too old, update already running, sudo rights missing), and no approval is asked for.

**Canonical plan objects.**  Spec section 13:  `fleet-ops-plan/1` for the helper's verbs, `fleet-resource-plan/1` for resource policy (with `old` and `new` from `fleet_resource_control`'s `before` and `after`), `fleet-knobs-plan/1` for knobs if Question 6.3 says yes, and `muster-listener-plan/1` built and compared in Swift.

**Knob writes, if the owner wants them.**  Muster would read the write token and a read token by key name through `fleet-secrets get` for the one run and pass them in the child environment.  `fleet_knobs set` would take a local lock, call `verify_writer`, re-read the live value and refuse unless the read has `source == "infisical"`, is not stale and equals the plan's `old`;  write;  then read back live and report "unverified" if that read is not live (never through `refresh_lkg`).  The race covers every other writer at `/`, on any host.

### Caching, Offline And Refresh

The phone draws from its cache first, with each reading's own time, and never shows a reading past its window as OK or zero.  While a screen is open it asks the hub for new sealed readings every 60 seconds with an ETag.  Silent pushes and Background App Refresh refresh the widget summary.

### How The Mac App And The Phone Never Drift

1. The same decoders and projections (`MusterKit`), extracted in Milestone 1, tested both ways.
2. The same collectors:  the bridge shares `SeatActivityStore` and `FleetViewStore`.
3. The same actions and audit:  Mac jobs through Muster's controller;  server jobs through the same helper, lock and audit files.
4. The same server data:  the Mac receives the server's readings from the hub as a recipient.
5. The same trust list:  one list per version, signed by Muster and applied by both at once;  a server report of a different list raises a warning on both.  Reopening, and its rule for keeping both sides in step, is OPEN (Milestone 5).
6. The same rules:  the attention rules and golden tests, the canonical-byte vectors, and an iOS column in the README's Every Panel table with a test.

### Infisical And Policy

The trust chain uses no Infisical secret or knob.  Infisical holds the hub's tunables (`/muster-hub`, `MUSTER_HUB_READ_TOKEN`) and, depending on Question 4.1, the push key's read token.  Device, Mac and server keys are made on their machines and never leave them (Question 1.5).

### Hub Audit Rows

Each row:  time, device id, request or job id, host, class (read request, read job, change job, trust list), state, and durations.  Never an action, a target, a plan or a result.

### Threat Model

| Threat | Control |
| --- | --- |
| Someone on the internet calls the hub | Every useful path needs a key on the trust list.  |
| The hub is controlled by an attacker who is not root on the server | It sees metadata.  It cannot read Mac data or results, forge or replay a reading, event, summary, plan or result the app shows, seed a verifier's trust list, make the phone or Muster apply a list the owner did not sign (reopening, and what a hostile hub could do to it, is OPEN for Milestone 5:  spec 17A), invent or replay a request or job, or approve anything.  It can withhold, which the acknowledgements and local notifications show.  If it holds the push key (Question 4.1), it can also put any text on the lock screen.  |
| The push relay is compromised | It can put any text on the lock screen and move a Live Activity between in-progress stages;  it cannot change what the app shows.  |
| `muster-pub` is exploited through the hub | It can forge server readings and its heartbeat, and send every server reading to the hub in the clear:  server-status with Coolify UUIDs and rooted config names, audit tails, inventories, the doctor report, lane names, the Operator's eavesdropper-status.  It can forge or suppress the events and widget summary built from server readings (a false "server unit failing" or a missing "disk at 85%"), all labelled "reported by the server".  It cannot plant files in the spool or replace the spool directory.  In Milestones 1 to 4 a forged heartbeat naming another list only raises a warning;  nothing stops and nothing is applied.  What a forged heartbeat could do to the OPEN reopen machinery (including freezing Muster) is spec 17A, items A3 and A5.  |
| `muster-exec` is exploited through the hub | Within the rate limits (spec section 12:  per verb and target, plans one in 5 seconds and runs one in 2;  per class in any sliding hour, 120 plans, 60 runs, 30 chains, 600 status reads), it can run the helper's read verbs and plan any allowlisted verb, each `resource-policy` or `resource-status` plan costing one Infisical read;  it can hold `ops-control.lock` briefly with approvals that verify;  it can spend a class's hourly budget, which delays the owner's own requests (no more than withholding them, which it can do anyway);  and it sees the plans and the `logs` text the owner asks for.  It cannot run a change, replay an approval, add an audit row by replaying one (rows are written only once a nonce is spent, and a refused chain writes none until spec 17A is settled), or forge an outcome or the helper's acknowledgement;  any refusal it delivers alone, other than a verified `plan_mismatch`, shows as "Not known whether it ran".  |
| The Operator container is compromised | It can forge the server's eavesdropper-status and the events built from it, labelled "reported by the Operator".  It runs the server's listener daemon, so it already holds every server lease title;  what the design ensures is that the reducer emits no titles, so nothing that leaves for the hub or the Mac carries one.  |
| The Mac bridge is fed a malicious message | Parsing and checks run in a sandboxed XPC service with no file access.  |
| A replayed approval, request, result or event | Distinct job and approval nonces in durable ledgers, written before running;  approvals bound to job, host, action, target, plan hash, device and time;  results bound to their approval;  events deduplicated by id and time.  |
| The run differs from the plan | One shared plan function;  the helper and `fleet_resource_control` compare the hash under their locks;  Muster compares under its own.  |
| A stolen phone | Approvals need the enrolled face on the approval key;  the request key is never accepted as an approval;  readings need the phone unlocked;  events and the widget summary are readable after first unlock by design;  the owner closes the list from the Mac, and every verifier accepts the close.  |
| Agent seats | Out of scope;  see Read This First and Questions 5.2 and 5.3.  |

## 3.  The Shared Swift Package:  MusterKit

`muster/MusterKit/Package.swift`, a local package tested with `swift test`, for macOS 14 and iOS 18.  Its first half is extracted in Milestone 1, because the first iOS screens need it;  the rest follows in Milestone 2.  Both halves move code with no Mac behaviour change.

**Milestone 1 half:**  places and words (`FleetHost`, `FleetPlace`, `MusterDestination`, `PlaceHealthWords`, `SidebarBadges`, `RunVerdict`, `OverviewCounts`, `PassMeaning`, `ActionHistoryWords`, `ThemeChoice`);  formatting (`FleetTime`, `FailureReason`, one `SentenceGap`);  the decoders the Milestone 1 screens need (`ServerStatusReport` with `ServerPanelMode` and `ServerLastKnown`, `FleetToolsReport`, `WorkerHealthReport`, `MacServicesReport`, `OpsAuditLog`, `knobs.jsonl`, `resource-policy-audit.jsonl`, `MachineStatus`'s summary, the `SeatActivity` decoding as it stands on `main` after PRs 1465 and 1469, both merged, the partition reader);  the Milestone 1 projections, rewriter and scrub;  the canonical-byte encoder, writing every null member.

**Milestone 2 half:**  `GridStateFilter`, `FleetViewEnvelope`, `FleetConfigContract`, `FleetGrid`, `SeatManifest`, `SeatInventory`, `MiniMaxSurface`'s decoder, `VmStatusReport`, `HogHunterMacReport`, `ListenerStatus` and the pure halves of `WakeSettings`, `WakeLedger` and `KnobRegistry`, `ResourcePolicy`, `BuildIdentity`, the remaining projections, the action vocabulary and verb tables.  It moves `ListenerStatus`, `KnobRegistry` and `WakeSettings` as PRs 1465, 1466 and 1469 (all merged) left them.  PR 1477 (open) edits `FleetGrid`, `FleetConfigContract` and `MiniMaxSurface` in this half and `MusterNavigation` (`MusterDestination`) in the first, so those moves wait for 1477 to merge or close.

**MusterRemote** (Foundation and CryptoKit, Milestone 1):  the trust list and acceptance rules First, A and B (a reopen is refused `reopen_unavailable` until Milestone 5 and spec section 17A), envelopes, requests, jobs, approvals, signed plans and results, canonical texts, HPKE, signing, the hub client, and the spec's vectors, checked in Swift and Python.

**Stays in the Mac app:**  process runners, loopback clients, `DriftWatcher`, `PlaceWatch`, `FleetAdmin`, `OwnerCheck`, `AutoUpdate`, `ServiceReconciler`, `OwnerStopStore`, the wake stores, `ApplyCoordinator`, `SeatActivityStore`, `FleetViewStore`, the XPC service, and every view.

## 4.  iOS Information Architecture

### Tabs And Navigation

1. **Overview**:  both place cards, the verdict, counts, recent drift, action in flight, revocation delivery state;  a gear opens Settings.
2. **The Mac**:  Services, Background Agents, Compliance Grid, Drift History, Wake & Listener, Seat Setup, About This Mac.
3. **The Server**:  Server Status, Resource Limits, Server Updates.
4. **The Fleet**:  Apps & Work, Seats, Controls, Tools And Worker Health, Virtual Machines.
5. **Actions**:  in flight, approvals waiting, trust changes waiting, Action History.

Each tab is a `NavigationStack`;  every screen carries the Mac's place badge and source line, with "Reported by the server", "Reported by the Operator" or "Reported by the hub" where it applies.  On iPad, `TabView` with `.sidebarAdaptable`.  Screens arrive by milestone (section 1).

### Screens

| Screen | Shows |
| --- | --- |
| Overview | Place cards, verdict, counts, drift, report times, action in flight, revocation delivery.  |
| Services, Background Agents | States, policies, owner stops, the allowlist's verbs;  Hog Hunter.  |
| Compliance Grid | Seats with kind chips;  the full grid on iPad;  a cell's proof, rule, rooted file name and age.  |
| Drift History | The turns.  |
| Wake & Listener | Run state, Mac seats' wakes, usage, pin state, problems, remote actions.  |
| Seat Setup | Seat and host pickers, six kinds, What Is Installed.  |
| About This Mac | Muster's build and checkout, folders, Keep Services Running, the bridge's last report.  |
| Server Status, Resource Limits, Server Updates | As the Mac, from the server's projected readings, disks by label.  |
| Apps & Work | Apps, then per seat:  lanes from both hosts, pull requests and claims from the Mac.  |
| Seats | By holder:  sessions by letter and platform, listener state, quota, last post time and channel, the Operator's availability.  |
| Controls | Knobs, values and ages, secret names, doctor, recall rows.  |
| Tools And Worker Health, Virtual Machines | As the Mac.  |
| Actions | In flight, approvals, trust confirmations, history, and "Not known whether it ran" until a signed result settles it.  |
| Plan Review (sheet) | The plan rendered, place badge, risk line, Approve With Face ID, Cancel.  |
| Trust Change (sheet) | Keys added and removed, names, full fingerprints, Confirm With Face ID, Reject.  |
| Settings | Theme, app lock, notifications, this device's fingerprint, trust list version, This Build.  |
| Pairing | Scan, compare the owner's and the device's full fingerprints;  after a reopen, "Waiting for the server to accept the new list" until it does.  |

All text follows `owner-copy` and `FLEET-UI-COPY.md`:  Title Case headings and buttons, sentence case body, U+00A0 plus a space between sentences, 12-hour times with no zone label, system theme by default, full text on tap, no secret in any label.

### Widgets, Live Activities, Shortcuts

Widgets read the signed widget summary (place words and counts, no names or paths), sealed to the notification key.  Live Activities (Milestone 5, with the first phone-started action):  one per phone-started action, titled on the phone, in-progress stages and times by the relay's fixed update shape, ended by the app after it verifies the result, and stale after 30 minutes by its `staleDate` (Push Notifications).  No Control Center controls.  No action intent or URL;  one read-only Fleet Status intent is Question 4.3.

## 5.  Guarded Controls On iOS

| Class | Actions | On iOS |
| --- | --- | --- |
| Read request to the Mac (M1, M3) | Ask The Mac Now, Seats refresh;  from M3 Apps & Work, Controls and recall refreshes | One tap, device-signed, budgets above.  |
| Read on the server (M5) | Ask The Server Now probe, `seat-inventory`, `update-status` | One tap;  the helper forces `--plan` and rate-limits it.  |
| Read with sensitive text (M5) | `server logs UNIT` | Face ID, approval checked by the helper;  sealed, never cached.  |
| Change on the server (M5) | Start, Stop, Restart, Redeploy, Run Update, one resource field | Face ID;  the helper plans, checks the approval and window before its lock and the nonce and plan hash under it, and signs the result.  Stop and Redeploy get a second tap (Question 5.5).  |
| Change on the Mac (M6) | pm2 and allowlisted LaunchAgents, Fetch From The Server Again | Face ID;  Muster checks the approval, nonce and plan hash.  |
| Listener, remote by default (M6) | Pause Wakes, Resume, numeric caps | Face ID and Muster's canonical plan.  |
| Knob writes | Show Plan, Reset To Default | Only if Question 6.3 says yes.  |
| Not remote | Compliance fixes, BotFleet Server's direct route, unlisted agents, login items, wake type, model, binaries, fallback, container rule, Test Wake, Pin, settings, Clear History, secrets | Read-only with the reason.  |

### Audit

| Action Class | Rows Written |
| --- | --- |
| Server change except resource policy | The helper's row in jay's `ops-audit.jsonl` (its existing fields plus `route: from-muster`, `via: muster-ios`, job, device and approval ids and plan hash;  an update's route moves to `updateRoute`;  spec section 14), written only once the approval's nonce is spent, so once per approval;  `muster-exec`'s row;  the hub's row.  |
| Resource policy | The helper's one row as above, and `fleet_resource_control`'s own rows in `resource-policy-audit.jsonl` with the same ids and `source: muster-ios`;  `muster-exec`'s row;  the hub's row.  |
| Mac helper change | The helper's row in the Mac's `ops-audit.jsonl`;  Muster's `via: muster-ios` row with the job, device and approval ids;  the hub's row.  |
| Listener change | Muster's own row, `via: muster-ios`;  the hub's row.  |
| Trust list delivered to the server | The helper's `trust-list` row when it accepts the chain;  rows for refused chains are OPEN (spec 17A);  the hub's row.  |
| Refused approval | The helper's row only for `plan_mismatch` (the one refusal after the nonce is spent), the executor's row, and the hub's row.  |
| Fault after the nonce is spent | The helper's row with outcome `failed`.  |

Server logs are jay-writable, so they are records, not evidence, against server seats.

## 6.  Build And Ship

- **Project:**  `muster/ios/project.yml` (XcodeGen):  `MusterIOS`, `MusterIOSWidgets`, `MusterIOSNotifications`, `MusterIOSTests`, and `MusterKit`.  iOS 18.0.  Entitlements only through `project.yml`.  `ITSAppUsesNonExemptEncryption` (whether sealing changes the export answer is not verified), `PrivacyInfo.xcprivacy`.
- **The Mac app** gains the XPC service, a `keychain-access-groups` entitlement and a Developer ID provisioning profile (not verified).  `muster-release.yml` gains a launch check before it publishes the appcast:  the signed, notarized build is launched on the runner with a flag that exits after it confirms it started and can create and delete a test keychain item in its group.  The entitlement lands alone, ahead of the bridge.
- **Bundle ids (proposed):**  `services.jays.Muster.ios`, `.ios.widgets`, `.ios.notifications`, app group `group.services.jays.Muster`, team `CC8UTF7ATG`.
- **CI:**  `.github/workflows/muster-ios.yml` on GitHub-hosted `macos-latest`, never a local Mac runner:  `swift test`, the spec's vectors in Swift and Python, a stale-project check, an unsigned simulator build, unit tests, screenshots.
- **Screenshots:**  a Screenshots configuration (never Release) loads `MusterTests/Fixtures/`;  a test asserts Release has no fixture path.  `xcrun simctl io booted screenshot` on discovered iPhone and iPad simulators, light theme, baselines.
- **TestFlight:**  `.github/workflows/muster-ios-ship.yml` on CodeCaps' pattern, `scripts/ios-fleet/ship-testflight.sh` vendored byte-identical, rows in `apps.json` and `testflight-apps.json`, internal testers only.
- **Versions:**  `0.1.0`, the fleet's paired merge-timestamp builds with `pairedBundleId: services.jays.Muster`;  the ship pull request reconciles the Mac README's UTC rule with the paired America/Chicago rule.

## 7.  Phased Plan

**Milestones 1 to 4 are ready to build:**  they use only frozen parts of the spec (canonical bytes, rules First, A and B, the owner pin and history, `install`, `repin` and `trust-status`, the shared module), plus the owner questions and open items each names.  Milestone 5 also waits for spec section 17A, the reopen machinery.

Sizes:  S under 400 changed lines, M 400 to 1,500, L over 1,500.  Each milestone names the owner questions that gate it.  `muster-exec`, its key and the helper key are not made before Milestone 5, and no Mac job path exists before Milestone 6.

### Milestone 1:  Read-Only Health And Seats On The Phone (Questions 1.1 to 1.6)

Depends on BB's single-invocation `muster-service`.  PRs 1465 and 1469, which it used to wait for, are merged.  Moving `MusterDestination` and taking the doctor fixtures wait for PR 1477 (open), which edits `MusterNavigation` and the doctor's runtime rows;  the rest of the extraction does not.  It does not depend on BB's reducer.

1. `MusterKit`'s Milestone 1 half, extracted with no Mac behaviour change;  `MusterRemote` with rules First, A and B, refusing a reopen with `reopen_unavailable` (vector T23), and the spec's frozen vectors in Swift and Python;  the canonical-byte encoder;  the Milestone 1 projections, rewriter and scrub.  L.
2. Hub (hardened container):  trust-list chain, sealed readings, read requests, devices, pairing, heartbeats, audit, health.  L.
3. Muster on the Mac, part one:  the entitlement first, with the release launch check;  then the XPC service, the owner key, Settings ▸ iPhone with the Server Install values.  M.
4. Server:  the `/usr/local/lib/muster-agent/` installer (`install`, `repin` with `--resume`, `trust-status`), the shared `muster_trust_files.py` from its one source in the repository, `muster-pub` with its allowlist projections, trust-list verification (rules First, A and B) and acknowledging heartbeat, the root-owned `/var/lib/muster` and spool with its ACLs, `ExecStartPost=-`, `muster-spool.timer`;  correct the collector's docstring.  M.
5. Muster on the Mac, part two:  the trust list with rules First, A and B, the close, the rotation freeze, the warning when the server reports a different list, pairing, the publisher, `refresh-seats` and `refresh-cheap`, `SeatActivityStore`'s bridge mode, the Mac as a recipient of the server's heartbeats and audit tails, the local revocation notification, `knobs.jsonl` and the server resource audit in Action History.  L.
6. iOS app shell:  pairing, keys, cache, Overview, Services, Server Status, Seats, Tools And Worker Health, Action History, Settings, Trust Change;  CI and screenshots;  TestFlight.  L.

### Milestone 2:  MusterKit, Second Half

The rest of the extraction with no Mac behaviour change;  `FleetGrid`, `FleetConfigContract` and `MiniMaxSurface` move after PR 1477 merges or closes.  Attention rules and golden tests.  M, M.

### Milestone 3:  Every Panel, Read-Only (Question 3.1)

The remaining panels, the iPad sidebar, `refresh-fleet-view`, `refresh-controls`, `refresh-recall`, the server's lanes-only `fleet-view` timer, BB's reducer with both transports (the reducer emits no titles;  PR 1479), the README's iOS column.  M, L.

### Milestone 4:  Alerts And Widgets (Questions 4.1 to 4.3)

Signed, deduplicated attention events and widget summary;  the push relay with its two fixed shapes (the Live Activity shape is built and tested here, and first used in Milestone 5), or the hub key, as chosen;  the Notification Service Extension;  widgets;  Background App Refresh;  the Mac receives every server reading from the hub.  M, M.

### Milestone 5:  Guarded Server Controls (Questions 5.1 to 5.5;  5.4 answered by relay)

**Also gated by spec section 17A** (the reopen machinery, OPEN):  rule C, `muster-agent reopen`, recovery, the final-refusal store, and Muster's and the phone's reopen flow are built here only once it is settled.  Helper (BB), to `server-route-spec.md` version 5:  the `--from-muster` entry, its plan commands, check orders and rate limits, the clock check, nonce ledger, helper key and signed answers, the trust-list chain, the `require_approval` flag and `fleet_resource_control`'s interface.  Server:  `muster-exec`, its key and job ledger, the helper key, the live same-lock-same-audit test.  Hub job queue.  iOS:  Plan Review with the phone's plan check (spec section 13), approvals, refusal reading, Live Activities.  L, M, M.

### Milestone 6:  Guarded Mac Controls (Questions 6.1 to 6.3)

The Mac executor in Muster with its job and approval ledgers and canonical listener plan;  the quit warning and Sparkle gate count phone runs;  knob writes only if Question 6.3 says yes.  M, L.

### Milestone 7:  Send To Mac

Send To Mac for compliance fixes;  App Attest on every request.  M.

### Linear Issues To File

Not filed.  Team AFC assumed.

| Title | One Line |
| --- | --- |
| Muster iOS:  owner decisions by milestone | Record the answers to section 8, milestone by milestone.  |
| MusterKit:  first-half extraction for Milestone 1 | Move the decoders the first iOS screens need, with no Mac behaviour change.  |
| MusterRemote:  trust rules, canonical bytes, sealing, signing and vectors | Rules First, A and B, `reopen_unavailable` for a reopen, and the frozen vectors, checked in Swift and Python.  |
| MusterKit:  allowlist projections, rewriter and scrub | A field allowlist per source and version, and a fail-closed path scrub.  |
| Muster Hub:  hardened service with readings, read requests and pairing | Stdlib Python in a non-root, read-only container.  Pending reopens are added in Milestone 5.  |
| Server:  muster-agent install, repin and trust-status | Owner pin and history, repin gated on the server's held version with root-reader owner checks, durable renames and resume.  `reopen` waits for spec 17A.  |
| Server:  shared muster_trust_files module | One module for the helper, muster-pub and muster-exec, held byte-identical by a test.  |
| Server:  muster-pub, root-owned spool and producers | Projection and trust verification in the publisher;  safe temporary files in the producers.  |
| fleet-server-status:  correct the docstring's home | Name jay's state as the canonical report.  |
| Muster:  keychain entitlement and release launch check | Land the entitlement alone and check every release launches.  |
| Muster:  XPC isolation for the bridge | Parse and verify hub bytes in a sandboxed service.  |
| Muster:  owner key, trust list rules and closed state | Touch ID, Face ID countersignatures, rules First, A and B, the rotation freeze, a warning when the server reports another list.  |
| Spec 17A:  settle the reopen machinery | Rule C, the record, recovery and the refusal store, against EE's eighth-look majors;  gates Milestone 5.  |
| Muster:  bridge publisher, read requests and local revocation alert | Projections, budgets, sleep markers, heartbeats from the server.  |
| SeatActivityStore:  bridge mode | Who-started tracking, shared in-flight read, App Nap activity.  |
| simple-chat:  single-invocation muster-service | One credential fetch and one users/me for all seats.  |
| Muster:  knobs.jsonl and resource audit in Action History | Read every audit file a Muster action can write.  |
| Muster iOS:  app shell, pairing and read-only health | The first screens, keys, cache.  |
| Muster iOS:  CI, screenshots and TestFlight | Workflows, vendored ship scripts, paired builds.  |
| MusterKit:  second-half extraction | The remaining decoders and the action vocabulary.  |
| Muster:  shared attention rules and golden tests | One rules file, both languages, transitions included.  |
| Muster iOS:  every panel read-only and the iPad sidebar | The remaining panels and the README's iOS column.  |
| Server:  lanes-only fleet-view timer | Hourly server lanes, merged on the phone.  |
| agent_sync:  reduced eavesdropper-status and its transports | BB's reducer (PR 1479), verb and Operator host folder;  the reducer emits no titles, bot or user id.  |
| Muster:  push relay, signed events and notification extension | Two fixed payload shapes, deduplicated signed events.  |
| Muster iOS:  widgets | From the signed widget summary.  |
| fleet-ops-control:  the --from-muster entry (spec v5) | Plan commands, check orders, rate limits, clock check, ledger, helper key, trust-list chain;  the reopen parts after spec 17A.  |
| fleet_resource_control:  approval interface and source label | Plan hash and revision under its lock, `source` as a parameter.  |
| Server:  muster-exec and its key | The executor, its job ledger, the live lock and audit test.  |
| Muster iOS:  Plan Review, approvals, refusal reading and Live Activities | The approval flow, results bound to approvals.  |
| Muster:  run phone-approved Mac jobs | The Mac executor with its two ledgers.  |
| Muster:  Send To Mac for compliance fixes | Open the Apply sheet on the Mac from the phone.  |

## 8.  Open Questions For The Owner, By The Milestone They Gate

### Milestone 1:  Read-Only Health And Seats

1.1 **The bridge.**  Muster deliberately has no outside entry point (`test_no_external_entry_points.py`).  Do you approve an outbound bridge that publishes readings, and that lets your phone ask the Mac to read again, which starts `muster-service` (with the Operator's realm-admin credential) on the Mac?

1.2 **Reading while nobody looks.**  `SeatActivityStore` reads nothing while nobody is looking, by design.  The bridge reads Seats when your phone asks, with nobody at the Mac:  about 47 Zulip requests and one Infisical fetch per read, at most four reads per ask.  Do you accept that?

1.3 **Seats data off the Mac.**  AFC-654 approved the Operator reading Zulip for Muster on the Mac.  May this exact set leave the Mac, sealed to your phone:  per seat, its holder, connected and paused state, whether a wake is running, each session's letter, platform and alive flag, whether it is at quota and when quota resets, and its last public post's time and channel;  and the Operator's availability?  Session titles, bot names, addresses, user ids and post bodies never leave the machine they are read on, in any milestone.

1.4 **The hub's address.**  A public hostname behind Cloudflare with every request signed (recommended), tailnet-only (the phone must run Tailscale), or Cloudflare Access in front too?  Under which domain?

1.5 **Identity and keys.**  May the app use `services.jays.Muster.ios` under `CC8UTF7ATG`, who creates its App Store Connect record, may Muster gain a keychain access group entitlement with a Developer ID provisioning profile, and do you accept device, Mac and server keys that are made on their machines and never stored in Infisical?

1.6 **The root-shell install and the console.**  The server install (two accounts, the root-owned spool and its ACLs, `/usr/local/lib/muster-agent/`, `/etc/muster/owner.pub` and its history) needs a root shell, and the full 64-character fingerprint comparison needs you at the server console.  No running jay service is restarted.  Rotating the owner key needs `muster-agent repin` there too, once the server holds every list Muster signed.  Until Milestone 5 at the earliest a closed list cannot be reopened, so if you lose your only phone and close the list, a new phone gets no readings until reopening is built (it will need you at the console as well).  Because every seat has passwordless sudo, a seat could run any of these commands, but only your own fingerprint comparison gives the pin its meaning.  Will you do the comparison yourself, and may a seat run the install steps for you to check?

### Milestone 3:  Every Panel

3.1 **The Operator's host folder.**  BB's second transport adds a host mount to the production Operator app, and its first adds a `server eavesdropper-status` read verb to the helper that every holder of `fleet-ops-ctl` can call.  Do you approve both?  Your message relayed under 5.4 settles the name `eavesdropper-status` but not this question, which stays open.

### Milestone 4:  Alerts And Widgets

4.1 **The push key.**  Should Muster have its own APNs key in a separate one-purpose push relay that can send only two fixed payload shapes (recommended), or should the internet-facing hub hold the fleet's shared APNs key?  Whoever holds the key can put any text on your lock screen;  with the relay that is only the relay, and the app itself never shows unverified text.

4.2 **Alerts.**  Should Muster's pushes sit beside Pushover as informational?  The proposed default list is in section 2, Attention Events;  which may break through Focus?

4.3 **The locked phone.**  May widgets and notifications be readable on a locked phone (they use a key kept after first unlock, and show no names or paths), and may the phone have one read-only Fleet Status intent?

### Milestone 5:  Guarded Server Controls

5.1 **What the phone's approvals protect against.**  The helper plans, checks your Face ID approval, rate-limits every request and signs every outcome, so an attacker who controls the hub or exploits the executor cannot run a change or fake its result.  Such an attacker can still run the helper's read verbs, see the plans and logs you ask for, and use up the hourly request limits;  the phone says "Not known whether it ran" for any refusal that cannot prove otherwise.  One who exploits the server's publisher can also forge or hide server readings and the alerts built from them (a false "unit failing", a missing "disk full"), always labelled as reported by the server, but cannot approve or run anything.  Agent seats stay out of reach entirely.  Do you accept that guarantee for remote actions?

5.2 **Narrow jay's sudo and docker access on the server** (wider than iOS).  If seats ran without `NOPASSWD: ALL` and outside `docker` (docker is root-equivalent unless the daemon is configured otherwise, not verified), and the helper's state moved to an account no seat runs as, then server readings could carry real integrity, the audit logs would become evidence, the reopen code would bind seats, and the helper's approval check would bind seats too.  Do you want that change, which touches every server seat and service?

5.3 **Gate Muster's releases** (wider than iOS).  Any seat that lands a change under `muster/` gets code into a signed Muster on your Mac within about an hour, with no prompt, which already sets the strength of Muster's Touch ID prompts.  Options:  a protected GitHub environment so every Muster release waits for you;  your code-owner review of every change under `muster/`;  or no silent install while the bridge is on.  Which, and should it cover the iOS ship too?

5.4 **The executor's key and the helper entry.**  `muster-exec` needs its own key in jay's `authorized_keys`, from localhost only, with its own `--from-muster` forced command, and the helper gains that entry as `server-route-spec.md` version 5 specifies.  BB's note, from revision 4, follows as BB wrote it, though its list of helper changes predates this design:  "BB, who owns the helper, asks that you weigh one fact here:  jay has passwordless sudo for any command on this server, so a second key into the same helper is a second path into an account that already equals root.  The boundary is only the forced command, the allowlist, the plan-then-run hash and the audit log, so each must be tight before remote actions are added, and BB will not start the helper changes (`--expect-plan`, `--job`, the parser change, the plan hash, `fleet_resource_control`'s check) or this key without your approval."  Do you approve the key and the `--from-muster` entry?

**Answered by relay.**  The owner gave this answer in a direct message to session BB, in BB's Claude Code conversation (session `bf1c02bd-6a8b-4403-8e71-599fea530496`), on November 7, 2026 at 4:09 PM:  "Can do the muster exec key and fix eavesdropper-status (should be instead of listener)".  No Zulip message or Linear comment exists, so there is no public link;  the only record is that session's transcript on the server, `/home/jay/.claude/projects/-mnt-fleet-extra-Code-AI-Fleet-Coordinator/bf1c02bd-6a8b-4403-8e71-599fea530496.jsonl`, which the owner can read, and where I found that exact message.  BB is building the entry against `server-route-spec.md`.  A relayed quote is not final approval for installing the key, which the owner installs himself in a root shell in any case;  this design asks him to confirm it in writing here, under 5.4.

5.5 **Which server actions may run from the phone.**  Should stopping a production unit or Redeploy stay Mac-only, and should Stop and Redeploy need a second tap after Face ID?

### Milestone 6:  Guarded Mac Controls

6.1 **The bridge runs Mac actions.**  Do you approve Muster running phone-approved Mac actions (services, background agents, Fetch From The Server Again, Pause Wakes, Resume, the numeric caps)?

6.2 **Which Mac actions stay Mac-only.**  Resume and raising `usdPerDay` can both raise spending;  under your November 7 ruling a seat wake runs no model, so the only model spend left is the Operator's own occasional runs (what `usdPerDay` bounds after the ruling is BB's open item).  Should either stay Mac-only?

6.3 **Knob writes.**  Muster cannot write a knob today, because it passes `fleet_knobs` no token.  Should Muster read the knob write token and a read token by name through `fleet-secrets get` for each write, so the Mac and the phone can change knobs with a live compare-and-set, or should knob writes stay off Muster?

Changed in revision 9:  1.6 says a closed list cannot be reopened before Milestone 5;  5.4 names spec version 5;  6.2 reflects the wake ruling.  No question was added or dropped.

## Open Items

Everything still undecided or unchecked, in one place, each with an owner (AA is this session;  BB owns the helper, the listener and the eavesdropper;  EE reviews;  the owner is Jay) and the milestone it gates.

| # | Item | Owner | Gates |
| --- | --- | --- | --- |
| 1 | **The reopen machinery (spec 17A):**  rule C, the reopen record, recovery, the final-refusal store and `finalRefusals`, against EE's four eighth-look majors (late walkers, minted refusals, the heartbeat freeze, the newest-8 window) and its minors | AA (design), BB (helper) | M5 |
| 3 | Two unverified points about `clock_unsynced` (spec 17B.3):  how it behaves after a long chrony outage, and whether anything else steps this server's clock | BB | M5 |
| 4 | Whether Swift reproduces the canonical-form vector, null members included (spec 17B.4) | AA | M1 |
| 5 | How a phone accepts an owner-key rotation (spec 17B.5) | AA | M1 |
| 6 | Whether a Developer ID Mac app can use a keychain access group with a provisioning profile in Muster's pipeline | AA | M1 |
| 7 | Whether CryptoKit's HPKE accepts a Secure Enclave agreement key | AA | M1 |
| 8 | Whether sealing changes the export-compliance answer | AA | M1 |
| 9 | The single-invocation `muster-service` | BB | M1 |
| 11 | PR 1477 merging or closing (open, on `claude/muster-grid-truth`, Refs AFC-616):  it edits `MusterNavigation`, `FleetGrid`, `FleetConfigContract` and `MiniMaxSurface`, which `MusterKit` moves, and the doctor's runtime rows, from which Milestone 1's fixtures are taken.  I did not verify which session owns it;  its description names none | EE (as the coordinator named it) | M1 (`MusterDestination`, fixtures), M2 |
| 12 | BB's reducer (PR 1479, no titles), its missing fields, its helper verb and the Operator host folder's path and mode | BB | M3 |
| 13 | Whether Apple lets an APNs key be restricted to one topic | AA | M4 |
| 14 | Which build-number rule the Mac release uses in practice | AA | M1 (ship pipeline) |
| 15 | Whether `MusterKit` compiles on Linux (cost only;  CI runs on `macos-latest`) | AA | None |
| 16 | The exact number of GitHub calls a server doctor run makes | AA | M1 (cadence) |
| 17 | Whether the Docker daemon makes `docker` group membership root-equivalent | AA | None (moot while sudo is unrestricted) |
| 18 | The owner questions in section 8 | The owner | As listed there |
| 19 | Whether a Live Activity's `staleDate` shows its stale content on the lock screen without the app running, and how long iOS keeps an activity the app never ends | AA | M5 |
| 20 | The paths of `muster-pub`'s stored list and `muster-agent trust-status`'s output (spec 17B.6) | AA | M1 |
| 21 | After the wake ruling, whether the listener keeps its model setting and the `claude` and `openrouter` wake kinds, and what `usdPerDay` bounds | BB | M3 (display), M6 (caps) |

Closed since revision 8:  BB's answers settle the plan commands and audit field names (old rows 1 and 2, now spec sections 4 and 14);  PR 1469 merged at 5:35 PM (old row 10).  Earlier:  PRs 1465 and 1466 merged at 4:57 PM and PR 1457 at 4:50 PM.  Row numbers are kept so earlier reviews still point at the right row.

## Not Verified

Everything not verified is an open item above, with its owner.  Beyond them, nothing live was checked except the read-only checks under What I Read.

## Review Response

Earlier rounds:  revisions 2 to 8 answered EE's first seven reviews.  EE's eighth look found both seventh-look majors fixed, the approval route holding for a third round, and every version 4 vector checking, and recommended stopping the rounds with the recovery machinery marked OPEN.  The coordinator agreed.  So revision 9 solves nothing inside that machinery:  it marks it OPEN, lists EE's findings unsolved in spec section 17A, keeps it out of Milestones 1 to 4, and fixes what lies outside it.  Spec-only items are answered in `server-route-spec.md` version 5, section 18.

| Item | Outcome |
| --- | --- |
| Major:  retiring reopen codes by line order breaks late walkers (and lets a hub split `muster-pub` from the helper) | OPEN, spec 17A item A1, gating Milestone 5.  Nothing in Milestones 1 to 4 reopens a list.  |
| Major:  final chain refusals minted without the owner key (extra countersignatures, ECDSA s/n−s malleability, `owner_signature`, check-2 `first_rule`) | OPEN, 17A item A2.  Version 5 records and reports no refusal as final, and a refused chain writes no audit row, so the audit claims now hold for what is built.  |
| Major:  a forged `muster-pub` heartbeat freezes Muster with no exit | OPEN for the reopen machinery (17A item A3).  For Milestones 1 to 4 a contradicting heartbeat only warns, and the close is always available.  |
| Major:  newest-8 window against one line per entry | OPEN, 17A item A4.  `finalRefusals` is always empty until it is settled.  |
| Minors inside the machinery (unbounded `muster-pub` store, a close undone by a pending reopen, device binding on an id string, rule A removing both server keys, `finalRefusals` with a null version, a dead pending entry, applied versus pending list) | OPEN, 17A items A5 to A11.  The applied-list wording is fixed in Reopening step 5.  Until A8 is settled Muster refuses to sign any change that removes both server keys.  |
| `repin`:  root fails the owner check;  no signing freeze during rotation | Fixed:  root readers check the expected account;  Muster signs nothing with the old key once it shows N, until an acknowledgement proves `repin` ran.  |
| PR status:  1469 and 1477 | 1469 merged at 5:35 PM (`7b681284`), which is `main` now.  1477 is open and edits four files `MusterKit` moves, so `MusterDestination` (M1) and `FleetGrid`, `FleetConfigContract` and `MiniMaxSurface` (M2) wait for it, and open item 11 says so.  I could not verify who owns 1477.  |
| "Every request is rate-limited" while parse refusals are not counted | Narrowed to every request that parses, with the reason it is safe.  |
| BB's answers from the helper build | Folded in as normative text:  plan commands (corrected to `main`'s `plan_request` after Sentry's finding, which BB confirmed), audit fields with `updateRoute` and the ledger-repair row, `clock_unsynced`, `plan_refused`, `internal_error`, outcome `unknown`, rate keys and boundaries, the exact window, the state directory, the empty reopen record, and how the shared module is held equal.  The `clock_unsynced` test is BB's `adjtimex` check;  two unverified points remain, open item 3 (BB).  |
| EE's check of PR 1484:  rule C leaking into Milestone 1 | Fixed before merge:  Milestone 1's `MusterRemote` only refuses a reopen with `reopen_unavailable` (spec vector T23);  the hub's pending reopens and Settings' reopen delivery state move to Milestone 5;  the reopen code's properties are stated as the proposal's intent, pending spec 17A items A1 and A7.  |
| The owner's wake ruling | A new Wakes Are Delivery section:  a seat wake runs no model, nothing in the design drafts a seat's reply, and the only model run is the Operator's own.  Wake & Listener and Question 6.2 say so, and open item 21 asks BB what remains of the model setting and the caps.  |
| BB's reducer emits no titles (PR 1479) | Eavesdropper-Status, the projection table, Milestone 3, the Operator threat row and the Linear row now say "the reducer emits no titles", with no flag, and state the cost.  |
| Every other eighth-look item | Held;  unchanged.  |
