# 2026-08-21 — Cursor chats from Grok Bot / Shellular on desktop + iOS

## Context

Owner asked that chats begun by Grok Bot (Cursor cloud) and Shellular
(terminal Cursor / CLI) show in desktop Cursor and be remotely
controllable from iOS Cursor.

## What is native

Grok Bot.app (`com.anysphere.sand`) already launches Cloud Agents.  Those
already appear in the desktop Agents Window, cursor.com/agents, and iOS
Cursor.  Local `cursor-agent acp` (Shellular's built-in Cursor spawn) does
not.  cursaves only copies local IDE SQLite; it cannot put CLI threads on
iOS.

## What landed

- `docs/CURSOR-CHAT-SURFACES.md` — native vs gap vs how to use.
- `scripts/cursor_chat_surfaces.py` — status / list / open / watch / handoff / install.
- `scripts/cursor_acp_cloud_bridge.py` — ACP server that creates Cloud Agents.
- `scripts/cursor-machine-worker.sh` — optional My Machines worker.
- Live install path: `~/apps/cursor-chat-surfaces/`.
- Shellular override: custom id `cursor` → bridge; `cursor-local` keeps stock ACP;
  `deepseek` → `~/apps/dsh-runtime/dsh-acp.sh` (no API keys in `agents.json`).
- Auth for the Cursor bridge: `CURSOR_SYNC_API_KEY` in `~/.secrets/global-api-keys`.

## Verification

```bash
python3 scripts/test_cursor_chat_surfaces.py
python3 scripts/cursor_chat_surfaces.py status
```
