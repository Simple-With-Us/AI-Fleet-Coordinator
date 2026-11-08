# Antigravity wakes: the Operator reaching a specific Antigravity conversation

Owner request, Sun, Nov 8, 2026 (AFC-665):  configure the Operator's session letters, catalog
discovery and HTTP wake bridge so Antigravity (`AG`) works equally well on the server and on the
Mac alongside all other platforms.

## The chain, end to end

    Zulip tag @AG or DM ag-bot
      -> the listener that holds the seat records the message in the seat inbox
      -> [seat.AG] wake = "http" POSTs agent-sync-wake/1, bearer-signed with AG_ROUTINE_KEY
           -> ag-wake-bridge on the target host (tailnet or loopback address, port 8792)
                validates the contract, remembers the wake id (dedupe),
                resolves the lettered target conversation from the Operator registry
                (topic words over the title, else the most recent live conversation on that host),
                and runs the delivery command in that conversation
                    agy --conversation <uuid> -p "<[AG·wake] ... thread link ...>"

The delivered prompt carries metadata only (channel, topic, the Zulip link, the wake id):  a
message body is never quoted into a prompt, matching the wake alerter and `zc-wake-bridge`
discipline.  The woken agent reads its own inbox with `simple-chat inbox --peek` and answers in
the thread.  Wakes held back by the seat's budget, and messages that arrive while no delivery ran,
stay in the inbox; nothing is lost when the bridge is down, and a wake id is marked seen only
after the POST is handled.

## What is built and tested (AFC-665)

- `scripts/ag-wake-bridge`:  the HTTP receiver, the dedupe store, the target resolver and the
  delivery executor.  State lives under `~/.local/state/ag-wake/` (0700):  `routine-key` (0600,
  the only secret), `config.json` (the delivery switch, `target_host` and command template),
  `seen.json` and `delivery.jsonl`.  `ag-wake-bridge deliver --wake-id ... --topic ...` runs one
  delivery by hand using the same code path as the POST handler.
- `scripts/agent_sync/server/ag-wake-bridge.service`:  the systemd unit template for the server
  (port 8792).  On the Mac, `ag-wake-bridge serve --bind 127.0.0.1 --port 8792` runs with
  `target_host` set to `"mac"` in `~/.local/state/ag-wake/config.json`.
- Cross-platform registry environment loading:  `platform_letter.with_registry_env` automatically
  loads `SIMPLE_TOOLS_OPERATOR_REGISTRY_URL` and `SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY` from
  `$HOME/.local/bin/mm-registry-env` when they are not already in the hook process environment,
  so Antigravity, Codex, OpenCode, Muse and ZCode hooks reach the Operator registry API on both
  the server and the Mac without requiring custom shell wrappers.
- Target rules:  target IDs must match a strict UUID regex (`TARGET_RE`), sub-agent conversations
  (`parent_conversation_id` or `nesting_depth > 0` in `conversation_summaries.db`) and killed
  conversations (`killed != 0`) are skipped, sessions on another host (`verified_host`) are
  skipped, and a conversation held by an active delivery lock answers `session-busy`.

## Arming the wake

1. Mint `AG_ROUTINE_KEY` into Infisical Fleet-OPS `/zulip` (32+ characters) via `fleet-secrets`.
2. Write the same value to `~/.local/state/ag-wake/routine-key` (mode `0600`).
3. Configure `~/.local/state/ag-wake/config.json` with `{"delivery_enabled": true, "target_host": "server"}` on the server (or `"target_host": "mac"` on the Mac).
4. In the listener config, set `[seat.AG]` `wake = "http"`, `url_env = "AG_ROUTINE_URL"` and `key_env = "AG_ROUTINE_KEY"`.
5. Start `afc-ag-wake-bridge.service` and verify `curl -s http://127.0.0.1:8792/health`.
