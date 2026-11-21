# ZCode wakes:  the Operator reaching a specific ZCode thread

Owner request, Sat, Nov 7, 2026 (OPS-1048):  the Operator should find the moments ZCode needs
waking (a message tagged the seat), and reach the specific ZCode thread that belongs to it, on
the Mac and on the server.  The session letters half of that request is PR #1454
(`docs/protocols/` has no lettering text here;  `scripts/simple_tools/README.md`, Platform
Session Letters, does).  This document is the wake half:  what exists, what is wired, and the
one decision still open.

## The chain, end to end

    Zulip tag @ZC or DM zc-bot
      -> the listener that holds the seat (partition:  ZC = "server", the one server listener since Nov 10)
           records the message in the seat inbox   (works since Nov 6, OPS-1039)
      -> [seat.ZC] wake = "http"  POSTs simple-tools-wake/1 to the one wake router's front,
           bearer-signed with WAKE_ROUTER_KEY
           -> afc-wake-router on jay-server (loopback 127.0.0.1:8794, front on the tailnet, port 8795)
                validates the contract, remembers the wake id (the router's own receipts),
                resolves the lettered target session from the Operator registry
                (the topic's letter, else the most recent live server session),
                and hands the wake to the seat's platform module
                    zcode --cwd <workspace> --resume <session> -p "<[ZC·wake] ... thread link ...>"

The legacy bridge chain, which is the fallback and not the live path any more:

    Zulip tag @ZC or DM zc-bot
      -> [seat.ZC] wake = "http"  POSTs simple-tools-wake/1, bearer-signed with ZC_ROUTINE_KEY
           -> afc-zc-wake-bridge on jay-server (tailnet address only, port 8791)
                validates the contract, remembers the wake id (dedupe),
                resolves the lettered target session from the Operator registry
                (topic words over the title, else the most recent live server session),
                and runs the delivery command in that session
                    zcode --resume <session> -p "<[ZC·wake] ... thread link ...>"

The delivered prompt carries metadata only (channel, topic, the Zulip link, the wake id):  a
message body is never quoted into a prompt, the same rule the wake alerter holds.  The woken
agent reads its own inbox with `simple-chat inbox` and answers in the thread, which is the
seat's normal wake routine.  Wakes the seat's budget holds back, and messages that arrive while
no delivery ran, stay in the inbox;  nothing is lost by a bridge that is down, and a wake id is
marked seen only after the POST is answered.

## The one wake router, and the cutover

The ZC seat now has a platform module (`scripts/simple_tools/zcode_wake.py`), so the one shared
wake router (`wake_service`, `simple-chat wake-router serve`) serves it like every other seat
with a module:  it picks the lettered session, rechecks the registry before the delivery, and
delivers over the one fixed route.  A chat on the Mac is reached over ssh through
`claude_wake.deliver_remote`, and the Mac's own `simple-chat wake-deliver` dispatches to
`deliver_request` in the module.

The sample `scripts/simple_tools/server/listener.toml` declares the seat's binding, and the
live file is moved to the sample's block by `apply_live_config.py --apply` once the image carries
it:  `wake = "http"` with `routine = { url_env = "ZC_ROUTINE_URL", key_env = "WAKE_ROUTER_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
the one bearer the router checks.  That apply is the cutover, and it is not this seat's alone:
the apply writes the sample's block for every seat it manages, and this seat and CODEX are both
among them, so the first apply after the image carries the new sample moves this seat to the
router whatever the operator meant to move.  The three gates below are therefore checked before
that apply runs and not after it, and a seat that fails any of them keeps the binding it has
only for as long as no apply has moved it.  The container is restarted with it, because the
environment is read once at the container's start.

The three gates, and what each of them answered on the day of the cutover.

- Reviewed:  the ZC seat's platform module landed on main in pull request #2283 (squash
  commit `11618d157`) after Kody's code review, and every finding on it was answered, fixed
  and resolved before the merge.
- Live:  the router's release on the server carries `zcode_wake.py`, `/health/ZC` answers
  `healthy` with `delivery` on, and ZC is in `WAKE_ROUTER_DELIVERY_SEATS`.
- Verified with a real wake:  a canary wake posted to the router's front with the router's
  own bearer was picked by the seat's letter, bound to the session's own store row, and
  handed to the ZCode runtime, which then failed at the seat's own model credential
  (`project_token_login_failed` for the zai family).

The binding that the change removed had delivered nothing:  the legacy bridge's own ledger on
the server holds 52 wakes, and not one of them was delivered.  That count is a recorded
result and not a claim, so it comes with the command that produced it:  the bridge's own
receipt ledger on the server, `/home/jay/.local/state/zc-wake/wakes.jsonl`, read with
`python3 -c "import json;rows=[json.loads(l) for l in open('/home/jay/.local/state/zc-wake/wakes.jsonl') if l.strip()];ids=[r.get('wake_id') for r in rows];print(json.dumps({'lines':len(rows),'distinct':len(set(ids)),'delivered_true':sum(1 for r in rows if r.get('delivered') is True),'delivered_false':sum(1 for r in rows if r.get('delivered') is False),'delivered_null':sum(1 for r in rows if r.get('delivered') is None)}))"`, which on Nov 17, 2026 answered
`{"lines": 83, "distinct": 52, "delivered_true": 0, "delivered_false": 31, "delivered_null": 52}`:  the 83 rows are 52 routing rows and 31 finalized rows for the
wakes the bridge attempted, and not one row records a delivery.  The bridge's path is not the
module's path:  the bridge resumes the session with its own command template and never
checks the directory the session reports, while the module runs the CLI with the
workspace the deployment names and refuses a session whose recorded directory differs
from it (`workspace-mismatch`), so the router can refuse a delivery the bridge would
have attempted.  Both reach the same ZCode runtime and the same model
credential, so both stop at the provider today.  The bridge stays up and keeps its
own values, and it retires only after a real wake is delivered end to end.

The cutover was done in these five steps, in this order.  The legacy bridge stays up as
the seat's fallback until the router's own front has answered a real wake end to end, so a
cutover that does not work never leaves the seat without a route.

1.  Free the seat's own convention for the router.  A seat's `routine.url_env` in the live
    listener is one of the two names the seat's own code derives, `<SEAT>_ROUTINE_URL` or
    `ZULIP_ALERT_<SEAT>_ENDPOINT`:  the settings-edit path refuses any other name for that
    sub-key (`env_name_not_allowed`), so the router's front for this seat is `ZC_ROUTINE_URL`.
    `ZC_ROUTINE_URL` is minted in AFC prod `/zulip` with the router's front URL,
    `https://<server tailnet host>:8795/wake/ZC`, and the seat keeps the one convention every
    other seat follows.  The bridge's own URL is governed in AFC prod `/zulip` under
    `ZC_BRIDGE_ROUTINE_URL`, so the seat's own name never serves the router's front and the
    bridge at the same time, and the operator reads that one through the approved route,
    `fleet-secrets get ZC_BRIDGE_ROUTINE_URL --path /zulip --project ai-fleet-coordinator`,
    captured into the write and never echoed.  The explicit project selector is part of that
    route and not an extra on it:  the reader's default project is Fleet-OPS, the name is not
    there, and the same read without the selector exits 3 with "key ZC_BRIDGE_ROUTINE_URL is
    not in Infisical at /zulip" (checked on Nov 17, 2026), because the name lives in the AI
    Fleet Coordinator project.  The bridge's own key is already in the loader's
    secondary read, so a rollback has a value to point at.  The
    environment is read once at the container's start, so the container is restarted after the
    name is minted.  The seat's own binding is named here, so the
    operator knows what the cutover started from:  the live file on the server held
    `wake = "inbox"`, which was the capture the seat had, and nothing in the live listener
    changes in this step.
2.  Deploy the router release that carries `zcode_wake.py` to the server, and prove the seat
    answers there:  `curl -s https://<server tailnet host>:8795/health/ZC` answers `healthy`
    with `delivery` on, and the seat is in `WAKE_ROUTER_DELIVERY_SEATS`.
3.  Prove the router's front and the router's own key with a real wake, while the live
    listener still holds the seat's own binding:  post one wake to
    `https://<server tailnet host>:8795/wake/ZC` with the router's bearer, and read the
    receipt (`/receipt/ZC/<wake id>`).  The receipt shows the picked session and a delivered
    or an explicitly unsettled outcome.  Nothing in the live listener changes in this step, so
    the seat's own capture path is untouched and the seat keeps receiving its messages if the
    router's front does not.
4.  Only then deploy the image that carries the new sample and run
    `apply_live_config.py --apply`, which is the step that performs the cutover.  The apply
    reads the sample from the image's own copy of it, so an image that still carries the old
    sample writes the old block, and the image comes first.  The sample declares the binding
    this seat takes:  `wake = "http"` with
    `routine = { url_env = "ZC_ROUTINE_URL", key_env = "WAKE_ROUTER_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and the apply writes the sample's block into the live file for every seat it manages, this
    seat and CODEX among them, so one apply moves both of them to the router.  A hand edit of
    the live file is not the way to do it:  the next apply writes the sample's block over it.
    The container is restarted with the apply, because the routine's URL comes from the
    environment, which is read once at the container's start, so a reload alone would keep the
    old URL.  The bearer is now the router's own key, so the key the bridge used is no longer
    accepted by the router, and the URL names the front.
5.  Prove it end to end:  mention @ZC in a test topic, and the router's receipt shows the
    picked session and a delivered or an explicitly unsettled outcome.  A front that
    misbehaves here is a rollback, and the values a rollback needs are still in place:
    the bridge's own URL is governed in AFC prod `/zulip` under `ZC_BRIDGE_ROUTINE_URL`, and its
    own key is already in the loader's secondary read, and neither was ever withdrawn.  The
    rollback puts the seat back on the shape it had before the cutover, which is the shape the
    bridge answers:  `wake = "http"` with
    `routine = { url_env = "ZC_ROUTINE_URL", key_env = "ZC_ROUTINE_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and `ZC_ROUTINE_URL` in AFC prod `/zulip` is re-pointed at the bridge's own URL, which the
    operator reads from `ZC_BRIDGE_ROUTINE_URL` in the same folder and writes into that name
    without echoing it, and that is the one write the rollback needs outside the server:  that
    name carries the router's front today, so the write is what moves the seat back and not a
    read of what the name holds.  That write has one named route, and it is not
    `fleet-secrets set`:  the allowlist in `scripts/fleet_infisical.py` is code, and it covers
    only `CACHE_*` names under `/linear` and the CLI-login names under `/cli-logins`, and the
    writer writes the Fleet-OPS project, so `fleet-secrets set ZC_ROUTINE_URL --path /zulip`
    exits 2 on the allowlist and could not reach this project anyway.  The write goes through
    the approved scoped Infisical consumer for that setting:  one service token whose grant
    is exactly the AI Fleet Coordinator project, `prod`, `/zulip`, with write permission, and
    the grant is proved from the token's own metadata before anything is written.  The write
    itself is the Infisical raw write for that name in that folder with the token as the
    bearer, the value read from a 0600 file and never in an argument, a log or a transcript,
    and the file deleted afterwards.  The project's id is not written down here, because this
    repository is public:  it is resolved at runtime from
    `INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID` or the first line of
    `~/.simple-tools/ai-fleet-coordinator-project-id`, and a run with neither stops and names
    that variable.  The token is minted under the `service-tokens` skill, one project, one
    environment, one folder, read-write, an expiry, and a row in the service-token registry.
    The token this rollback uses was minted on Nov 17, 2026 and its registered expiry is
    `2027-02-16T02:20:42Z`, and a re-mint moves that timestamp and the registry row with
    it, and the token lives in the consumer's own store;  an operator's Infisical login session is not
    that route and is refused for writes.  The route was exercised on Nov 17, 2026:  the
    scoped token's own metadata proved the project, the environment, the folder and the write
    permission, a write with the value the name already held answered HTTP 200, the name
    read back unchanged through the approved reader, and the value file was deleted.  Both names
    are the seat's own, so the settings-edit path writes that routine, and the daemon picks it
    up on the reload that write ends with.  The sample has to come back with it:  the sample
    still carries the router's binding for this seat, so the next apply would move the seat
    back to the router, and the sample's `[seat.ZC]` block is edited to the bridge's binding
    in the repo's `scripts/simple_tools/server/listener.toml`, the file of record, and an
    image carrying that edit is deployed, before that can happen:  the apply reads the sample
    from the image's own copy of it, so an edit inside the running container is lost at the
    next image refresh and the next apply writes the router's binding back.
    The container is restarted with it:  the environment is read once at the start, so
    a reload alone would leave the routine unset and the seat would not be woken at all.  The
    capture the seat had before the cutover comes back without any of that:  `wake = "inbox"`
    and a reload alone put the seat back on the live file's own binding, and it too is undone
    by the next apply until the sample says the same.
Only after step 5 does the legacy bridge (`scripts/zc-wake-bridge`, the unit
`zc-wake-bridge.service`) retire:  it holds the same per-session lock, so the two paths can
never run one session at the same time, and its dedupe ledger is replaced by the router's own
receipts.  Until then it stays as it is, with the bridge's own URL governed in AFC prod
`/zulip` under `ZC_BRIDGE_ROUTINE_URL` and its own key already in the loader's secondary read,
and the sample keeps carrying the router's binding for this seat.

## What the module guarantees

The delivery contract is the one the sibling platforms use, with the parts that are ZCode's own
made explicit.

- The session's own store row is the binding.  The row carries the exact session id and the
  directory that session reports about itself, and both are compared with the requested
  session and the workspace the deployment names before anything runs.  The tasks index is a
  derived view over the same sessions, so it never authorizes a delivery:  a session whose
  reported directory is not the workspace the deployment names is never picked, and a
  delivery into another project's directory is refused (`workspace-mismatch`).
- The externally authored fields are checked before they reach a prompt:  a field of the wrong
  type is refused, a field that carries credential material is refused by name
  (`secret-field`, and the value is never echoed), and the note in front of the data is only the
  router's own sentence.
- A workspace path that begins with a dash is refused (`invalid-workspace`), so it can never be
  read as an option by the CLI.
- The three outcomes of a turn are kept apart.  A timeout is `unknown`:  the turn may have run,
  so the wake is neither recorded as delivered nor retried, and the receipt keeps it visible.  A
  turn that fails before any model response (the runtime's own documented
  `project_token_login_failed`) is `failed` even when the process exits 0, so it is never
  recorded as a delivery.  An exit 0 with nothing to show is `sent` with an unconfirmed
  acknowledgement, and the receipt never claims a confirmed delivery for it.
- The turn never outlives its delivery.  The module keeps the child handle and stops and
  reaps it on every path, including a timeout and an interruption, so a turn can never keep
  running behind a session lock the delivery already holds.
- The request is bounded before it is used.  The transport caps the raw body before it is
  parsed:  `simple-chat wake-deliver` reads at most 16 KiB of stdin and refuses anything
  over it, so an over-long body never reaches the parser.  The handler here then bounds the
  parsed request before any field of it is read:  an absolute byte cap on the request as it
  was parsed, a depth bound and a node-count bound, and a request over any of them is
  refused.

## What is built and tested (PR: OPS-1048)

- `scripts/zc-wake-bridge`  the receiver, the dedupe, the target resolution and the delivery
  executor.  State under `~/.local/state/zc-wake/` (0700):  `routine-key` (0600, the only
  secret), `config.json` (the delivery switch and command template), `seen.json`,
  `wakes.jsonl` (the receipt ledger:  a routing row per wake, plus a finalized receipt row
  per wake the bridge attempted, with `delivered` true or false;  count distinct `wake_id`
  values and not lines).  `zc-wake-bridge deliver --wake-id ... --topic ...` runs one delivery
  by hand, the same code the POST path runs.
- `scripts/simple_tools/server/zc-wake-bridge.service`  the unit template.  It sources the shared
  registry env file (`~/.local/bin/operator-registry-env`) itself.  This unit is a separate consumer from the
  letter client, whose [dedicated registry-only bootstrap](../rollouts/afc-785-letter-bootstrap.md) replaces
  env-helper sourcing; verify each consumer's own approved binding before activation.  The unit binds the
  address `/etc/zc-wake-bridge.env` names (the tailnet IP;  loopback without one), and runs the
  checkout out of fleet-cli `current`, so a release move never leaves a dead path.
- Target rules:  a subagent session never wins (the store's `task_type` says so), a session on
  another host never wins (the registry's `verified_host`;  the Mac desktop has no zcode CLI,
  so a Mac-side delivery is skipped with the reason logged until a Mac route exists), a session
  whose letter was retired or whose registration closed never wins, a session our own lock
  holds is left to finish (`session-busy`).

## The one open decision:  the seat's model credential

Delivery runs a headless turn (`zcode --resume <session> -p <text>`).  The router's delivery for
this seat is armed:  `WAKE_ROUTER_DELIVERY_ENABLED` is true and ZC is named in
`WAKE_ROUTER_DELIVERY_SEATS`, both in the router's own Infisical folder `/wake-router`, and the
router's `/health/ZC` answers `healthy` with delivery on.  The one local file for the whole
router, `~/.local/state/wake-router/config.json`, can only narrow that, never widen it, and the
seat's own per-seat file is `~/.local/state/wake-router/zc/config.json`.

What is still open is the seat's model credential on the server.  The headless run asks the
Z.AI project-access token for the `zai` family, and the answer is
`project_token_login_failed` (stage `resolve`), so the turn fails before any model response and
the router's receipt says the wake was not delivered (`resume-exit-1`, status dead).  The fix
is the seat's own login on the server (`zcode login zai`, in a browser), which is the owner's
to do.  The older note about a dead MiniMax key in the desktop's provider order no longer
describes what the headless run does:  the run asks the Z.AI account, and it never gets as far
as a model.

The legacy bridge's own switch is a different file on a different path:
`~/.local/state/zc-wake/config.json` carries `delivery_enabled` for the bridge only, and the
router never reads it.

## Arming the legacy bridge (runbook, after the provider decision)

This is the legacy bridge's own setup, not the seat's routine:  the seat's routine is the
one wake router's, and the names below are the bridge's.

1.  Provision `ZC_ROUTINE_KEY` in Infisical Fleet-OPS `/zulip` (32+ characters) through an
    approved Infisical admin session (`fleet-secrets` only writes `/linear` and `/cli-logins`);
    never through chat text, never printed.
2.  Mirror the same value into the server's `~/.local/state/zc-wake/routine-key` (0600).  The
    copy is a redirect into the file, never terminal output.
3.  Server listener:  the seat's block moves to the bridge's own binding, which is the shape
    the seat had before the cutover:  `wake = "http"` with
    `routine = { url_env = "ZC_ROUTINE_URL", key_env = "ZC_ROUTINE_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and `ZC_ROUTINE_URL` in AFC prod `/zulip` holds the bridge's tailnet URL,
    `http://<jay-server tailnet ip>:8791/wake/ZC`, the bridge's own tailnet address and the
    bridge's own port.  That value is the one governed in AFC prod `/zulip` under
    `ZC_BRIDGE_ROUTINE_URL`, so a rollback has a URL to copy.  The change is made in the repo's
    `scripts/simple_tools/server/listener.toml`, the file of record, and
    `apply_live_config.py --apply` writes it into the live file.  The server loader reads the
    key from its secondary project (`INFISICAL_LISTENER_READ_PROJECT_ID` and
    `_secondary_names` in `server/infisical_env.py`) and the URL from AFC `prod` `/zulip`,
    both once at the container's start, so a restart (a redeploy) picks them up and a
    `daemon reload` does not.
4.  Server:  install the unit (`/etc/zc-wake-bridge.env` carries `ZC_WAKE_BIND=<tailnet ip>`),
    enable it, `curl -s http://127.0.0.1:8791/health`.
5.  Prove it:  mention @ZC in a test topic;  the server listener's wake ledger (`simple-chat wakes` inside the container) records the POST,
    and `~/.local/state/zc-wake/wakes.jsonl` carries the receipt with the resolved session.

## Verification to date

- Bridge suite:  10 tests (`cd scripts && python3 -m unittest simple_tools.tests.test_zc_wake_bridge`):
  the contract and seat checks, bearer and key-file discipline (a group-readable key is a 503),
  dedupe, delivery-disabled, topic-match over recency, the subagent/off-host/closed skips, and
  one delivery through the CLI path with the launcher mark set.
- `zcode --resume <session> -p` runs a real turn into an existing session (verified live,
  Nov 7), which is what the delivery executor runs;  it fails today only on the dead provider
  key above.
