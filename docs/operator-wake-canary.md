# Claude Wake Router:  Install And Bounded Canary

Refs AFC-694 and OPS-1048.  This is the runbook for putting Claude's wake router on the server and proving one wake end to end.  Nothing here is armed by merging it:  the installer and the canary are two scripts someone runs by hand, and every root step and every Infisical write below is the owner's.

The two scripts live beside the unit template in `scripts/simple_tools/server/`:

| Script | What it does |
|---|---|
| `install-claude-wake-router.sh` | Checks the host, then installs `claude-wake-router.service` as `/etc/systemd/system/afc-claude-wake-router.service`.  It never writes a secret, never changes an Infisical value and never starts the router unless `--start` is passed.  `--dry-run` prints the whole plan and changes nothing. |
| `claude-wake-canary.sh` | Sends exactly one `simple-tools-wake/1` body to the router, addressed to one letter, then prints the wake id, the pick and the receipt, and stops.  `--test` asks the router for its pick and delivers nothing;  `--dry-run` sends nothing. |

## Facts this runbook relies on

These were checked read-only on both hosts by Claude AA on November 11 (Zulip, topic `AFC-694 Operator cross-platform coordination`), and they override the older notes in the unit template:

- One router instance per seat, all sharing `scripts/simple_tools/wake_router.py`.  This one is Claude's.
- The router listens on port 8794 by default.  Port 8793 belongs to `zc-wake-tls.service`, and the installer refuses it.
- The listener refuses plain http to a non-loopback address, so the router needs a TLS front on the tailnet address (for example socat on 8795, reusing the ZC certificate).  That front is not part of these scripts.
- The routine key is `CLAUDE_ROUTINE_KEY` in AI Fleet Coordinator prod `/zulip`.  `CLAUDE_ROUTINE_URL` does not exist yet.
- The delivery knobs live in AI Fleet Coordinator prod `/` and are read with a read-only service token (proposed name `CLAUDE-wake-router-ro`).
- The Mac route needs its own ssh key, because the existing key serves other jobs:  a `mac-wake` Host block on the server and one forced-command line in the Mac's `authorized_keys`.

## Owner steps, in order

Each step is the owner's, or needs the owner's yes before a seat does it.

1. **Mint the read-only knob token** for AI Fleet Coordinator prod `/`, and put it in `/etc/claude-wake-router.env` on the server, root-owned, mode 0600, as `FLEET_KNOBS_INFISICAL_TOKEN=...`.  Optional non-secret lines in the same file:  `CLAUDE_WAKE_BIND`, `CLAUDE_WAKE_PORT` and `CLAUDE_WAKE_KEY_FILE`.
2. **Write the routine key** to `/home/jay/.local/state/claude-wake/routine-key`, owned by `jay`, mode 0600.
3. **Set the knobs** in Infisical:  `WAKE_ROUTER_DELIVERY_SEATS=CLAUDE`, `WAKE_ROUTER_MAC_SSH=mac-wake` and `WAKE_ROUTER_DELIVERY_ENABLED=false`.  The installer refuses to run while either delivery knob is unset, so the router never starts on defaults nobody chose.
4. **Add the Mac key**:  a dedicated key pair on the server, the `mac-wake` Host block, and on the Mac one `authorized_keys` line with `from=` the server's tailnet address and `command="/Users/jay/.local/bin/simple-chat wake-ssh-gate",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding`.
5. **Install**, from a clean checkout at `origin/main`:

   ```sh
   bash scripts/simple_tools/server/install-claude-wake-router.sh --dry-run
   sudo bash scripts/simple_tools/server/install-claude-wake-router.sh --start
   ```

   The dry run names every problem a real run would refuse and exits 0.  With delivery still `false`, a started router picks and receipts every wake and delivers nothing.
6. **Point the listener** at the router's TLS front with the routine key.  That edit is Claude BB's (the live listener config), followed by a readback.

## The canary

Run it on the server as `jay`, against the router's loopback port.

1. Post one canary message in the work topic, addressed to the letter you mean to wake, and note its message id.
2. Ask for the pick without delivering:

   ```sh
   bash scripts/simple_tools/server/claude-wake-canary.sh --letter AA --message-id <id> --test
   ```

   The answer must pick the named letter.
3. Only after the owner says so, set `WAKE_ROUTER_DELIVERY_ENABLED=true` for one wake, then send it:

   ```sh
   bash scripts/simple_tools/server/claude-wake-canary.sh --letter AA --message-id <id>
   ```

   It waits up to 1100 seconds for a final receipt.  Exit 0 means delivered, 3 held, 4 dead, 5 still pending.
4. Set `WAKE_ROUTER_DELIVERY_ENABLED=false` again unless the owner wants delivery left on.

## The evidence for AFC-694

Acceptance is the same wake id in every link of the chain, posted in the AFC-694 topic:

- the canary's output:  `wake_id`, the pick (`picked`, `picked_letter`, `picked_generation`, `session_id`) and the receipt;
- the router's own record, `~/.local/state/claude-wake/done/<wake_id>.json`, and `GET /receipt/<wake_id>`;
- the woken chat, whose prompt names the wake id and the message id.  This one is read in that chat, by that chat, and command success alone does not prove it.

A wake sent by the canary goes straight to the router, so it proves the router and the delivery, not the listener's capture.  The listener's capture is proved separately by tagging the letter in Zulip once the listener points at the router, and reading the same `wake_id` in the listener's log and the receipt.
