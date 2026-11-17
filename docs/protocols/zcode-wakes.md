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

## The one wake router, and the cutover that is still open

The ZC seat now has a platform module (`scripts/simple_tools/zcode_wake.py`), so the one shared
wake router (`wake_service`, `simple-chat wake-router serve`) serves it like every other seat
with a module:  it picks the lettered session, rechecks the registry before the delivery, and
delivers over the one fixed route.  A chat on the Mac is reached over ssh through
`claude_wake.deliver_remote`, and the Mac's own `simple-chat wake-deliver` dispatches to
`deliver_request` in the module.

The sample `scripts/simple_tools/server/listener.toml` still routes ZC at its own bridge, and
that is deliberate.  A seat moves to the router only after its own path is deployed and
verified live, and until then it keeps the binding it has:  the working path is never removed
before the replacement has answered a real wake.

The cutover, when it is done, is these five steps, in this order.  The legacy bridge stays the
seat's live path until the router's own front has answered a real wake, so a cutover that
does not work never leaves the seat without a route.

1.  Free the seat's own convention for the router.  A seat's `routine.url_env` is always
    `<SEAT>_ROUTINE_URL`, and the sample's partition check holds every seat that is routed at
    the router to that name, so the router's front for this seat is `ZC_ROUTINE_URL` and the
    legacy bridge moves to `ZC_BRIDGE_ROUTINE_URL`.  The bridge's URL value does not change,
    only the name it is read under:  mint `ZC_BRIDGE_ROUTINE_URL` in AFC prod `/zulip` with the
    bridge's URL, and mint `ZC_ROUTINE_URL` with the router's front URL,
    `https://<server tailnet host>:8795/wake/ZC`.  One name never serves the router's front and
    the bridge at the same time, and the seat keeps the one convention every other seat
    follows.  The loader exports the bridge's name for every server seat
    (`<SEAT>_BRIDGE_ROUTINE_URL`), so the container carries it once it runs the loader that
    reads it, and the environment is read once at the container's start, so the container is
    restarted after the names are minted.  The seat's own binding is named here, so the
    operator knows what the cutover starts from:  the sample's `[seat.ZC]` block declares
    `wake = "http"` with the bridge's routine, and the live file on the server still holds
    `wake = "inbox"`, which is the capture the seat has today.  Nothing in the live
    listener changes in this step.
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
4.  Only then change the live listener's `[seat.ZC]` block to `wake = "http"` with
    `routine = { url_env = "ZC_ROUTINE_URL", key_env = "WAKE_ROUTER_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    and restart the container:  the routine's URL comes from the environment, which is read
    once at the container's start, so a reload alone would keep the old URL.  The bearer is
    now the router's own key, so the key the bridge used is no longer accepted by the router,
    and the URL names the front.
5.  Prove it end to end:  mention @ZC in a test topic, and the router's receipt shows the
    picked session and a delivered or an explicitly unsettled outcome.  A front that
    misbehaves here is a rollback, and the values a rollback needs are still in place:
    `ZC_BRIDGE_ROUTINE_URL` in AFC prod `/zulip` and `ZC_ROUTINE_KEY` in Fleet-OPS `/zulip`
    are the bridge's own values and were never withdrawn, so the live `[seat.ZC]` block goes
    back to `wake = "http"` with `routine = { url_env = "ZC_BRIDGE_ROUTINE_URL", key_env = "ZC_ROUTINE_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`,
    which is the legacy path the sample declares, and the container is restarted:  the
    environment is read once at the start, so a reload alone would leave the routine unset and
    the seat would not be woken at all.  The capture the seat has today comes back without
    any of that:  `wake = "inbox"` and a reload alone put the seat back on the live file's
    own binding.
Only after step 5 does the legacy bridge (`scripts/zc-wake-bridge`, the unit
`zc-wake-bridge.service`) retire:  it holds the same per-session lock, so the two paths can
never run one session at the same time, and its dedupe ledger is replaced by the router's own
receipts.  Until then it stays as it is, with `ZC_BRIDGE_ROUTINE_URL` in AFC prod `/zulip` and
`ZC_ROUTINE_KEY` in Fleet-OPS `/zulip`.

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

## The one open decision:  the headless provider

Delivery runs a headless turn (`zcode --resume <session> -p <text>`).  On this server the
headless run takes its model from the desktop's provider order (`~/.zcode/v2/provider_config.json`),
whose first entry (MiniMax) holds a dead key:  every headless run answers 401 invalid api key
today, verified live on Nov 7.  The config therefore ships with `delivery_enabled` false, and
every accepted wake is logged undelivered (`reason: delivery-disabled`) until the owner names
the provider the seat's headless runs should use  the Z.AI account the seat already runs on, or
a working key for the MiniMax entry.  Flipping it is one config file:

    ~/.local/state/zc-wake/config.json   { "delivery_enabled": true, ... }

## Arming the wake (runbook, after the provider decision)

1.  Mint `ZC_ROUTINE_KEY` into Infisical Fleet-OPS `/zulip` (32+ characters)  on the Mac,
    `fleet-secrets` writes it;  never through chat text, never printed.
2.  Mirror the same value into the server's `~/.local/state/zc-wake/routine-key` (0600).  The
    copy is a redirect into the file, never terminal output.
3.  Server listener:  `[seat.ZC]` gains `wake = "http"` and the routine line reading
    `url_env = "ZC_ROUTINE_URL"`, `key_env = "ZC_ROUTINE_KEY"`.  The server loader reads the key from
    Fleet-OPS `/zulip` (its secondary read, `LISTENER_READ_CODES` in `server/infisical_env.py`) and the URL from AFC
    `prod` `/zulip` once at the container's start, so a restart (a redeploy) picks them up and a `daemon reload` does not.
    `ZC_ROUTINE_URL` is the bridge's tailnet URL, `http://<jay-server tailnet ip>:8791/wake/ZC`.4.  Server:  install the unit (`/etc/zc-wake-bridge.env` carries `ZC_WAKE_BIND=<tailnet ip>`),
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
