# DSH "Load failed" on every thread (2026-09-17)

AG leftover chat `0c60d4c8`.  Board `eef4315d`.  Branch `grok/dsh-load-error`.

## What the UI showed

Harness listed every thread as `Failed to load history: Load failed (internal)` and the model picker stuck on "Refreshing model list".  The owner guessed the fleet-recall Cordis preset.

## Actual causes

1. **`~/apps/dsh-runtime/dsh.sh` exec'd itself.**  A 2026-09-16 PATH wrapper (`exec /Users/jay/apps/dsh-runtime/dsh.sh "$@"`) was copied into `dsh.sh`.  bash busy-looped exec; nothing bound `:3080`; two leftover processes burned CPU for hours.
2. **`node_modules` was empty.**  `@deepseek-ai/dsh` and the transitive `@deepseek-ai/dsh-tools` package the fleet-recall preset imports were gone.  Restore with `npm ci` in `~/apps/dsh-runtime` (never `npx`).
3. **Dock `pingHarness` was 2s.**  Under CPU load the loopback GET timed out, `ensure-web.sh` ran `pm2 start --update-env`, and `start-web.sh` reclaim-killed the still-healthy server.  WebKit then surfaces `TypeError: Load failed`.  The fleet-recall mount failure is isolated and was not this UI string.

## Fix

- Track `scripts/dsh-runtime/dsh.sh`: exec `node_modules/.bin/dsh` only, refuse any other bin path.
- `HarnessWindow.swift` ping 8s / 8.5s wait.
- `ensure-web.sh` curl 8s; if `:3080` is already listening, skip pm2 restart.
- `start-web.sh` HTTP-probes before reclaim; a healthy dsh holder exits 0 (does not start a second copy).

Copy those four files into `~/apps/dsh-runtime/` after merge, `chmod +x dsh.sh`, rebuild the Dock app with the live `install-dock-app.sh` (MiniMax rebrand: `Harness.app`).

## Verify

```bash
bash scripts/test-dsh-load-guards.sh
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://127.0.0.1:3080/
```
