# Muster Review: Displays, Controls And Protocol Coverage

Seat ZC (ZCode), November 26, 2026. Scope: the Muster macOS app, the Operator and Notes protocols it reads, and the server-side data behind them.  This is both the review record and the change record: the fixes marked **fixed here** are in this change; the rest are stated with the evidence that blocks them.

## How This Review Was Done

- Read `muster/README.md` (every panel row and its prose), the Swift sources for all 18 destinations, the settings and Operator models, and the Muster Python checks in `muster/script/`.
- Read the protocol documents behind the panel data: `docs/protocols/operator-role.md`, `operator-notes.md`, `simple-tools-listener.md`, `eavesdropper-server.md`, `simple-tools-guide.md`, `fleet-compliance-rules.md`, and `docs/muster-operator-zulip.md`, `docs/muster-resource-controls.md`.
- Inspected the live Operator's configuration and status (names, booleans and counts only, never a credential): its listener sections, `[notes_queue]`, `[listener_api]`, and the status keys it actually writes.
- Ran the Muster Python suite: 111 tests, 4 skipped, all passing.

The live facts that matter below:

| Fact | Live value |
| --- | --- |
| `[notes_queue] enabled` | true |
| `worker_seats` | 29 names, including every server seat, every hosted seat and every enabled GB bot |
| `server_seats` | CLAUDE, CODEX, ZC |
| Overlap | CLAUDE, CODEX and ZC are in both lists |
| `[listener_api]` | On (the queue is served) |
| Status blocks | `linear_comments`, `registry_sweep_schedule` and `notes_queue` are absent from the live status document |
| Listener seat sections | 43, including JET, MA, the 12 BF bots and the 12 GB bots; JET-CHAT has no section |

## The Reported Problem: The Notes Queue Rows

The panel read **Enabled / Worker Seats / Server Seats** under a disclosure titled **Notes Queue**.  Those are title-cased machine keys.  Nothing said what the queue is, what either list is for, or why one seat can appear in both.  The two lists are not seat rosters: they are the per-key permission lists for the Operator's Apple Notes queue.  `worker_seats` says which seats the hosted **Worker** key may act for; `server_seats` says which seats the **server** key may act for.  The queue accepts a request only when the signing principal may act for the seat the request names.

The screenshot also showed the Worker list ending at `GB-COMPILE`.  That was not a wrong value: the one-line text field cut the 29-name list off at the field's edge.

**Fixed here.**

- The group now says what the queue is and which key each list belongs to.
- The rows read **Seats Through The Worker Key** and **Seats Through The Server Key**, each with a sentence naming its holder.
- The field wraps, so all 29 names are readable, and a count sits under it.
- A name in both lists is named as such ("Also through the other key: ...").
- A name with no `[seat.X]` section in the server's listener config is named ("No listener section on this server: ..."), and `OPERATOR` is called out as a service the queue can never act for.
- The group now shows the queue's own health: queue and Listener API state, the seat counts, whether the two lists are empty, and the doctor's `notes:queue`, `notes:runner` and `notes:action-log` rows, with a stale or missing report reading as not read rather than as healthy.

## What Muster Connects With

Muster's 18 destinations and their sources are documented in `muster/README.md`.  The connections that carry Operator, protocol or fleet state are:

| Surface | Source | Coverage before this change |
| --- | --- | --- |
| Compliance Grid, Drift History | `fleet_config status`, the manifest, the Mac's drift file | Covered |
| Apps & Work | `fleet_view status`, `fleet-apps.json`, lanes, PRs, the doctor report | Covered |
| Seats | Mac listener status and config, the seat partition, `simple-chat muster-service`, server `eavesdropper-status` | Covered; two prose claims were wrong (below) |
| Services, Background Agents | pm2, `launchctl`, BotFleet APIs, `mac-services.json`, the allowlist | Covered |
| Server Status, Server Updates, Resource Limits | server report, allowlist, `server resource-status`/`resource-policy` | Covered |
| Operator | four read verbs plus the settings get/save, and the allowlist | Partial: see below |
| Tools And Worker Health | doctor `report.json`, `worker-health.json`, `api-health.json` | Covered; `api-health.json` was undocumented |
| Controls | knob registry, live Infisical read, the Mac's last-known-good copy | Covered, read-only where no write token exists |
| Bearers | Worker owner API via the owner's Access login | Covered |
| Action History | `ops-audit.jsonl` | Covered |

### Operator coverage, item by item

| Capability | Shown | Notes |
| --- | --- | --- |
| Eavesdropper state, last Zulip event | Yes | The headline overclaimed "Listening" when pause was unreported; **fixed here** |
| Wake router health and per-seat readiness | Yes | Only the router's own `health` fields the helper reduces; route-check and Mac-route fields are not decoded |
| Letter registry, live and gone counts | Yes | Warnings undercounted when the list was truncated or the sweep unread; **fixed here** |
| Seats And Chats | Yes, 30 first with Show All | "Gone" and the title column were promised in prose but cannot be produced by the live route; prose **fixed here** |
| Recent Wakes | Yes, newest 15 | Unread/truncated are already stated |
| Held Back and owner queue | Yes | A seat whose wake record was unreadable could drop out silently; **fixed here** |
| Pause and resume, Test Wake | Yes | Covered |
| Settings | Yes | Autosave, not the plan/Touch ID flow the README described; `applyEnabled` was not shown before an edit; Notes Queue had no purpose or health.  **fixed here** |
| Linear comment mirror, linkifier, Listener API | Toggles only | No health.  The live status has no `linear_comments` block, so a health panel would say "not reported" until the Operator image ships it |
| Registry sweep schedule | No | The live status has no `registry_sweep_schedule` block yet (AFC-833) |
| Session and thread sheet exports | No | No status surface exists in the status document |
| Notes queue principals and backlog | Partial | Backlog and runner come from the doctor report; the queue's own `/notes/v1/health` principal list is not reachable without a queue key |
| Sentry heartbeat, wake canary | No | Neither is in a file or verb Muster reads |
| Fleet-wide compliance and unimplemented rules | Yes | The grid shows both |

## Other Accuracy Defects And Omissions

1. **`applyEnabled` was invisible before an edit.**  The store already held the flag; no view read it.  A refused save therefore looked like a server fault.  **Fixed here:** the panel says applying is off, or unstated, before the first edit.
2. **The Eavesdropper headline claimed more than the server said.**  With a `null` pause block, the card said "Listening" and only its colour and a line said pause was unreported.  **Fixed here:** the headline now reads "Listening, Pause Not Reported".
3. **The registry card could read as a clean bill of health.**  With the status not read, sweep warnings were skipped and the card still said "No warnings"; a truncated session list was not mentioned.  **Fixed here:** the card says "No warnings in what was read", calls a counted warning list a floor, and names a cut-short list or an unread sweep.
4. **Held Back could omit a seat silently.**  The model carries `unreadable` and `truncated`, but the section showed neither.  **Fixed here:** both are stated under the rows, even when the row list is empty.
5. **"Review Fix" was dead code.**  `ApplyFixButton` was defined in AFC-391 and had no call site in any view, while the README promised a red cell's fix.  **Fixed here:** a failing rule in the cell detail offers Review Fix, which opens the existing plan sheet, re-reads the plan, and applies only the reviewed plan; the panel itself still writes nothing.
6. **The Muster README described an older app.**  It said Settings changes only a seat's `enabled` and `wake` through Show Plan and Touch ID, never mentioned the Notes Queue at all, omitted Bearers from the sidebar prose, omitted `api-health.json`, and said Resource Limits reads only on open and Refresh.  **Fixed here:** the Operator row, the sidebar list, the Tools row and the Resource Limits row now match the code.
7. **Seats prose promised values the server cannot send.**  It said every session shows a title; a server-held session's title never leaves the server.  It also listed "Gone" as a row state the live route never produces, and said "at most 30" while Show All shows up to 300. **Fixed here.**
8. **The notes queue had no purpose anywhere.**  The settings types alone said "a list of seat names"; the Doctor's Notes rows were hidden by the default Problems Only filter.  **Fixed here** through the group caption, the row titles, the per-list checks and the health block.

## What Is Not Fixed Here, And Why

These are real gaps, but each needs a server-side status field, a deployed image, or an owner decision; a client-only display would invent a state.

1. **Linear comment mirror, project linkifier and Listener API health.**  The live status document carries none of these blocks (`linear_comments` is absent, not merely off).  The repository's `reduced_status.py` can reduce `linear_comments`, but the running image does not send it.  Build the status block and the helper reduction first; then Muster can show state, last success and refusal code.
2. **The registry sweep schedule.**  It is an owner-gated status field (AFC-833).  Muster can show the last pass today; the next scheduled pass needs the block.
3. **Sheet export state.**  No status field exists for the sessions, events or thread drains.  A panel that reads only the SQLite registry cannot show a stuck or refused sheet write.
4. **Notes queue principal matrix.**  `/notes/v1/health` returns only principal names, not which seats each may act for, and it is signed.  Muster holds no queue key by design.  A doctor field (principal names plus the queue's backlog, already partly present) would close this without giving Muster a credential.
5. **Server-side seat-list validation.**  The settings editor accepts any syntactically valid uppercase name; the queue refuses the seat only when a request names it.  Muster now marks names with no listener section, but the server should return a non-blocking warning in `get`/`plan` so every client sees it.
6. **The live seat lists themselves.**  `worker_seats` includes `JET`, which the committed Worker config marks events-only, so it cannot use the Notes tools; `JET-CHAT` is the active login.  `KIMI` and the 12 `BF-*` bots are valid Worker-capable identities but are not listed.  Whether to add, remove or leave them is an owner/Worker-config decision, not a Muster change.
7. **Protocol documents that now lag the code.**  `docs/protocols/operator-role.md` (items 9 and the settings table) and `docs/protocols/simple-tools-listener.md` §9 still say Muster cannot write the Eavesdropper's settings, the notes queue is not on Muster, and the compliance grid is a later phase.  Those statements are stale; they should be corrected as a documentation change with the deployment record, not silently in a client PR.
8. **Two router-card implementations.**  `OperatorPanelPresentation.router` is the older readiness-only card, still public and tested, while the view uses `OperatorRouter.routerCard`.  The older one can drift.  Removing it is a cleanup with test churn.
9. **Legacy models with no view.**  `OperatorOptions` is referenced only for `seatTag`; `WakeSettings`/`WakeSettingsReader` have no non-test callers.  Their copy still names the retired "Server Eavesdropper Settings" panel.
10. **Notes queue documentation arithmetic.**  The protocol says the queue holds at most 1,000 waiting requests; the code also caps a single seat at 200.  A full answer can therefore be 200 for one seat, not 1,000 globally.  The doc should say both.

## Proposed Follow-Ups

- Operator status: add `linear_comments`, `registry_sweep_schedule`, listener-API health and sheet-drain state to the status document and the helper reduction; then surface each in the Operator panel.
- Notes queue: add a non-blocking seat-list warning to the settings `get`/`plan` answer; add the queue's principal names to the doctor row.
- Protocol docs: correct the stale "not built" statements and the queue's two caps.
- Muster cleanup: remove the older router card and the unused legacy models once their tests move.

## Verification For This Change

- `python3 -m unittest discover -s muster/script -t muster/script`: 111 tests, 4 skipped, OK.
- The pull request's **Muster macOS build** job compiles the app, runs the whole unit-test bundle (`xcodebuild -scheme Muster -configuration Debug ... test`) and renders every panel light and dark; this change is proven by that job, including the new `NotesQueueHealthTests` and the seat-list and card-state assertions.
- Live checks were read-only: the listener configuration's seat sections and `[notes_queue]`/`[listener_api]` values, and the status document's key names.  No credential, token or private host was read or written.