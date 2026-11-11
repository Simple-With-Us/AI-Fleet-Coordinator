# ZCode wakes:  the Operator reaching a specific ZCode thread

Owner request, Sat, Nov 7, 2026 (OPS-1048):  the Operator should find the moments ZCode needs
waking (a message tagged the seat), and reach the specific ZCode thread that belongs to it, on
the Mac and on the server.  The session letters half of that request is PR #1454
(`docs/protocols/` has no lettering text here;  `scripts/simple_tools/README.md`, Platform
Session Letters, does).  This document is the wake half:  what exists, what is wired, and the
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
message body is never quoted into a prompt, the same rule the wake alerter holds.  The woken
agent reads its own inbox with `simple-chat inbox` and answers in the thread, which is the
seat's normal wake routine.  Wakes the seat's budget holds back, and messages that arrive while
no delivery ran, stay in the inbox;  nothing is lost by a bridge that is down, and a wake id is
marked seen only after the POST is answered.

## What is built and tested (PR: OPS-1048)

- `scripts/zc-wake-bridge`  the receiver, the dedupe, the target resolution and the delivery
  executor.  State under `~/.local/state/zc-wake/` (0700):  `routine-key` (0600, the only
  secret), `config.json` (the delivery switch and command template), `seen.json`,
  `delivery.jsonl`.  `zc-wake-bridge deliver --wake-id ... --topic ...` runs one delivery by
  hand, the same code the POST path runs.
- `scripts/simple_tools/server/zc-wake-bridge.service`  the unit template.  It sources the shared
  registry env file (`~/.local/bin/operator-registry-env`) exactly as the letter hooks do, binds the
  address `/etc/zc-wake-bridge.env` names (the tailnet IP;  loopback without one), and runs the
  checkout out of fleet-cli `current`, so a release move never leaves a dead path.
- Target rules:  a subagent session never wins (the store's `task_type` says so), a session on
  another host never wins (the registry's `verified_host`;  the Mac desktop has no zcode CLI,
  so a Mac-side delivery is skipped with the reason logged until a Mac route exists), a session
  whose letter was retired or whose registration closed never wins, a session our own lock
  holds is left to finish (`session-busy`).

## The one open decision:  the headless provider

Delivery runs a headless turn (`zcode --resume <session> -p <text>`).  On this server the
headless run takes its model from the desktop's provider order (`~/.zcode/v2/provider_config.json`),
whose first entry (MiniMax) holds a dead key:  every headless run answers 401 invalid api key
today, verified live on Nov 7.  The config therefore ships with `delivery_enabled` false, and
every accepted wake is logged undelivered (`reason: delivery-disabled`) until the owner names
the provider the seat's headless runs should use  the Z.AI account the seat already runs on, or
a working key for the MiniMax entry.  Flipping it is one config file:

    ~/.local/state/zc-wake/config.json   { "delivery_enabled": true, ... }

## Arming the wake (runbook, after the provider decision)

1.  Mint `ZC_ROUTINE_KEY` into Infisical Fleet-OPS `/zulip` (32+ characters)  on the Mac,
    `fleet-secrets` writes it;  never through chat text, never printed.
2.  Mirror the same value into the server's `~/.local/state/zc-wake/routine-key` (0600).  The
    copy is a redirect into the file, never terminal output.
3.  Server listener:  `[seat.ZC]` gains `wake = "http"` and the routine line reading
    `url_env = "ZC_ROUTINE_URL"`, `key_env = "ZC_ROUTINE_KEY"`.  The server loader reads the key from
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
