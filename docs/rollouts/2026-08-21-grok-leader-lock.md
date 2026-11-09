# grok-leader lock-held restart storm (2026-08-21)

## Why

`ms` showed pm2 `grok-leader` `errored` with 355 restarts.  This Grok TUI
(pid 12360, started 02:11) spawned a leader child (pid 76260 at 15:43) that
already bound `~/.grok/leader.sock`.  pm2 kept starting `leader.sh`, which
exited immediately: "Another process holds the leader lock".

`mac-process-watch.sh` already had a lock-held skip, but launchd ran it
with `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`.  `lsof` lives
only at `/usr/sbin/lsof`.  Bare `lsof` was a no-op, so every 120s tick
logged `DOWN pm2:grok-leader` and tried `pm2 restart` until backoff.

Same class as the Shellular `ioreg: command not found` PATH hole.

`com.jay.ios-ship-now` `exit-1` is the 2026-08-13 login one-shot leftover.
Do not kickstart it.  `grok-acp` 13 restarts are historical; it is
listening on `127.0.0.1:12419`.

## What changed

- Watch uses `/usr/sbin/lsof` (and a `pgrep` fallback).  Lock-held is
  `SKIP`, not `DOWN`.  An `errored` lock-held job is `pm2 stop`ped.
- LaunchAgent PATH includes `/usr/sbin:/sbin`.
- `leader.sh` exits 75 when the socket is bound.  Ecosystem
  `stop_exit_codes: [75]`.
- `ms` annotates a live TUI socket and the ios-ship-now leftover.
- Live: `pm2 stop grok-leader`.  Do not `pm2 save` while it is stopped.

## Verify

```bash
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin command -v lsof; echo exit:$?
# expected: not found, exit 1
bash ~/apps/fleet-grok-leader-lock/scripts/test-mac-process-watch-lock.sh
bash ~/apps/grok-acp-runtime/leader.sh; echo exit:$?
# expected: lock held, exit 75
MAC_PROCESS_WATCH_RESTART=0 bash ~/apps/mac-process-watch.sh
grep 'grok-leader' ~/Library/Logs/mac-process-watch.log | tail
# expected: SKIP lock-held, not DOWN
bash ~/apps/mac-status.sh
```

Start pm2 `grok-leader` only after the TUI exits and
`/usr/sbin/lsof ~/.grok/leader.sock` is empty.

Board `0095ae36`.  Branch `grok/leader-lock`.
