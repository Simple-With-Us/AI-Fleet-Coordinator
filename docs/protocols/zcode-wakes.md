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
the server holds 83 wakes, and not one of them was delivered.  The bridge's path is not the
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
    other seat follows.  The bridge's own URL and its own key stay where the seat already kept
    them, in Fleet-OPS `/zulip` under `ZC_ROUTINE_URL` (the bridge's TLS front,
    `https://<server tailnet host>:8793/wake/ZC`) and under `ZC_ROUTINE_KEY`, so a rollback has
    a value to point at and the seat's own name never serves the router's front and the bridge
    at the same time.  The loader also allows a bridge's own name (`<SEAT>_BRIDGE_ROUTINE_URL`)
    in prod `/zulip`, but this seat's bridge URL is kept in the other project, so the container
    carries no name for it and a rollback copies the URL from Fleet-OPS `/zulip`.  The
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
    Fleet-OPS `/zulip` holds the bridge's own URL under `ZC_ROUTINE_URL` (the bridge's TLS
    front) and the bridge's own key under `ZC_ROUTINE_KEY`, and neither was ever withdrawn.  The
    rollback puts the seat back on the shape it had before the cutover, which is the shape the
    bridge answers:  `wake = "http"` with
    `routine = { url_env = "ZC_ROUTINE_URL", key_env = "ZC_ROUTINE_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and `ZC_ROUTINE_URL` in AFC prod `/zulip` is re-pointed at the bridge's URL that Fleet-OPS
    `/zulip` holds, which is the one write the rollback needs outside the server.  Both names
    are the seat's own, so the settings-edit path writes that routine, and the daemon picks it
    up on the reload that write ends with.  The sample has to come back with it:  the sample
    still carries the router's binding for this seat, so the next apply would move the seat
    back to the router,
    and the sample's `[seat.ZC]` block is edited to the bridge's binding before that can
    happen.  The container is restarted with it:  the environment is read once at the start, so
    a reload alone would leave the routine unset and the seat would not be woken at all.  The
    capture the seat had before the cutover comes back without any of that:  `wake = "inbox"`
    and a reload alone put the seat back on the live file's own binding, and it too is undone
    by the next apply until the sample says the same.
Only after step 5 does the legacy bridge (`scripts/zc-wake-bridge`, the unit
`zc-wake-bridge.service`) retire:  it holds the same per-session lock, so the two paths can
never run one session at the same time, and its dedupe ledger is replaced by the router's own
receipts.  Until then it stays as it is, with the bridge's own URL and its own key in
Fleet-OPS `/zulip` under `ZC_ROUTINE_URL` and `ZC_ROUTINE_KEY`, and the sample keeps carrying
the router's binding for this seat.

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
  `delivery.jsonl`.  `zc-wake-bridge deliver --wake-id ... --topic ...` runs one delivery by
  hand, the same code the POST path runs.
- `scripts/simple_tools/server/zc-wake-bridge.service`  the unit template.  It sources the shared
  registry env file (`~/.local/bin/operator-registry-env`) itself (the letter hooks no longer read it:  they get the same
  settings from the host's `~/.local/bin/platform-letter` wrapper, which sources an env script in shell), binds the
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

1.  Mint `ZC_ROUTINE_KEY` into Infisical Fleet-OPS `/zulip` (32+ characters)  on the Mac,
    `fleet-secrets` writes it;  never through chat text, never printed.
2.  Mirror the same value into the server's `~/.local/state/zc-wake/routine-key` (0600).  The
    copy is a redirect into the file, never terminal output.
3.  Server listener:  the seat's block moves to the bridge's own binding, which is the shape
    the seat had before the cutover:  `wake = "http"` with
    `routine = { url_env = "ZC_ROUTINE_URL", key_env = "ZC_ROUTINE_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and `ZC_ROUTINE_URL` in AFC prod `/zulip` holds the bridge's tailnet URL,
    `http://<jay-server tailnet ip>:8791/wake/ZC`.  The bridge's own URL is also kept in
    Fleet-OPS `/zulip` under `ZC_ROUTINE_URL` (its TLS front on the same bridge), so a
    rollback has a URL to copy.  The change is made in the sample, the file of record, and
    `apply_live_config.py --apply` writes it into the live file.  The server loader reads the
    key from Fleet-OPS `/zulip` (its secondary read, `INFISICAL_LISTENER_READ_PROJECT_ID` in
    `server/infisical_env.py`, whose name set `_secondary_names` builds) and the URL from AFC
    `prod` `/zulip` once at the container's start, so a restart (a redeploy) picks them up and a
    `daemon reload` does not.
4.  Server:  install the unit (`/etc/zc-wake-bridge.env` carries `ZC_WAKE_BIND=<tailnet ip>`),
    enable it, `curl -s http://127.0.0.1:8791/health`.
5.  Prove it:  mention @ZC in a test topic;  the server listener's wake ledger (`simple-chat wakes` inside the container) records the POST,
    and `~/.local/state/zc-wake/delivery.jsonl` carries the receipt with the resolved session.

## Verification to date

- Bridge suite:  10 tests (`cd scripts && python3 -m unittest simple_tools.tests.test_zc_wake_bridge`):
  the contract and seat checks, bearer and key-file discipline (a group-readable key is a 503),
  dedupe, delivery-disabled, topic-match over recency, the subagent/off-host/closed skips, and
  one delivery through the CLI path with the launcher mark set.
- `zcode --resume <session> -p` runs a real turn into an existing session (verified live,
  Nov 7), which is what the delivery executor runs;  it fails today only on the dead provider
  key above.
