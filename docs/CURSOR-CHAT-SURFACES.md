# Cursor chats on desktop + iOS (Grok Bot and Shellular)

How Cursor conversations started from **Grok Bot.app** or **Shellular** show up in
the desktop Cursor app and in **iOS Cursor** remote control.

Two spaces between sentences in this file.

## Native product (already true — do not reinvent)

| How the chat started | Desktop Cursor | iOS Cursor remote control |
|---|---|---|
| **Grok Bot.app** (`com.anysphere.sand`) — Cursor cloud agents | **Yes.**  Same backend as [cursor.com/agents](https://cursor.com/agents).  Open **Agents Window** (Command Palette → Open Agents Window) or the Cloud Agents panel.  If the row is missing, Filter → Source → SDK (SDK-created cloud agents are hidden by default). | **Yes.**  Same inbox.  Pull to refresh. |
| Cloud Agent from web, Slack, GitHub, API, iOS, Automations | **Yes.** | **Yes.** |
| Desktop **Agents Window** session + `/remote-control` | Stays on this Mac; tools run locally. | **Yes**, if Remote Control is on (Cursor 3.9.8+, paid Cloud Agents, Settings → Agents, git remote, Mac awake). |
| Classic editor sidebar chat (not Agents Window) | Local to this machine. | Only after **Move to Cloud** or Remote Control from Agents Window. |
| `agent` / `cursor-agent` CLI, including Shellular's stock `cursor-agent acp` | **No.**  CLI history is `agent ls` / `--resume` only.  `--workspace` does not attach the thread to the desktop chat list. | **No.**  Prepend `&` in interactive CLI to hand off to a Cloud Agent, or use the fleet bridge below. |

Official CLI handoff (interactive `agent` only, not ACP): prepend `&` to a message.  Then pick it up at [cursor.com/agents](https://cursor.com/agents), desktop Agents Window, and iOS.

There is **no** `cursor://` URL that opens a specific chat.  Cloud agents use `https://cursor.com/agents/bc-…`.

### What cursaves is not

[cursaves](https://github.com/Callum-Ward/cursaves) copies **local IDE** chat SQLite between machines or workspaces.  It does not:

- put CLI / ACP threads into the desktop chat list (different store)
- make chats appear on iOS Cursor (iOS uses the Cloud Agents backend)
- enable iOS remote control

Do not install cursaves for this goal.

## What this fleet process adds

Grok Bot chats are already Cloud Agents.  The gap is **Shellular Cursor**, which spawns local `cursor-agent acp`.

`scripts/cursor_acp_cloud_bridge.py` is an ACP server Shellular can spawn instead.  The first prompt creates a Cloud Agent (`POST /v1/agents`).  Follow-ups hit `POST /v1/agents/{id}/runs`.  That `bc-` id is what desktop and iOS already know how to list and control.

If `CURSOR_API_KEY` / `CURSOR_SYNC_API_KEY` is missing, the bridge **execs stock `cursor-agent acp`** so Shellular still works, but those chats stay local.

Live copy after install: `~/apps/cursor-chat-surfaces/`.

```bash
# from this worktree, or after install:
python3 scripts/cursor_chat_surfaces.py install --shellular
pm2 restart shellular

python3 ~/apps/cursor-chat-surfaces/cursor_chat_surfaces.py status
python3 ~/apps/cursor-chat-surfaces/cursor_chat_surfaces.py open inbox
python3 ~/apps/cursor-chat-surfaces/cursor_chat_surfaces.py list-cloud
```

### One-time API key

Cloud Agents API is **not** the CLI login cookie.  Prefer **`CURSOR_SYNC_API_KEY`**
from Infisical Fleet-OPS (`fleet-secrets get CURSOR_SYNC_API_KEY`;  the flat
`global-api-keys` file is retired).  Also accepted:
`CURSOR_API_KEY` in the environment, or a chmod-600 `~/.secrets/cursor-api-key`.

Never `cat` or `grep` those files in a transcript.  Names only.

Until one of those exists, `status` exits 2 and Shellular Cursor stays local ACP.

### How to use after install

**Grok Bot:** keep using Grok Bot.app.  On the Mac, open **Agents Window** (not only the classic editor chat list).  On the phone, open **Cursor for iOS** — the same agents are in the inbox.  `open inbox` is a shortcut to [cursor.com/agents](https://cursor.com/agents).  Optional: `watch --once` lists new API-visible agents (needs the key).  SDK-sourced rows need Filter → Source → SDK.

**Shellular DeepSeek:** pick **DeepSeek** (id `deepseek`).  That spawn is
`~/apps/dsh-runtime/dsh-acp.sh` (pinned Harness ACP, not `npx` and not `dsh acp`).
Auth comes from `~/.dsh/.credentials.yaml` or `DEEPSEEK_API_KEY` from
`fleet-secrets get DEEPSEEK_API_KEY` — never from `agents.json`.

**Shellular Cursor:** pick **Cursor** / **Cursor Cloud** (id `cursor`).  That spawn is the bridge.  The first message creates a Cloud Agent, prints its `https://cursor.com/agents/bc-…` URL into the Shellular thread, and opens that URL on the Mac.  Desktop Agents Window and iOS can follow and send follow-ups on the **same** agent.  **Cursor local** (id `cursor-local`) is stock `cursor-agent acp` for when you explicitly want a phone-only CLI session.

**Old local ACP sessions:** they cannot be injected into the desktop chat list.  Closest: `handoff-acp` creates a **new** Cloud Agent pointed at that workspace (new conversation, not a transcript import).

```bash
python3 ~/apps/cursor-chat-surfaces/cursor_chat_surfaces.py handoff-acp
```

### Optional: run Cloud Agent tools on this Mac

Default bridge target is a Cursor-hosted VM (`env.type=cloud`).  To keep edits on this Mac, start a My Machines worker (on-demand, **not** one of the 14 pm2 always-on jobs):

```bash
bash ~/apps/cursor-chat-surfaces/cursor-machine-worker.sh
CURSOR_BRIDGE_ON_MAC=1   # then restart Shellular so the bridge inherits it
```

If the worker is down, the bridge ignores `CURSOR_BRIDGE_ON_MAC` and uses a cloud VM so the prompt does not queue forever.

### Force local ACP anyway

```bash
CURSOR_ACP_LOCAL=1
```

## Remaining manual steps

1. Confirm `CURSOR_SYNC_API_KEY` is in `fleet-secrets names` if `status` says MISSING.
2. Run `install --shellular` once per machine, then `pm2 restart shellular`.
3. On desktop, use **Agents Window** for cloud threads.  The classic per-workspace chat history will not list CLI/ACP chats.
4. On iOS, install Cursor, sign in as `mail@jaywedgeworth.com` (same account as CLI/`agent status`).
5. If a Grok Bot cloud agent is missing from the default list, Filter → Source → SDK.
6. Remote Control of a **desktop** session still requires `/remote-control` in Agents Window; it does not apply to Shellular ACP.

## Tests

```bash
python3 scripts/test_cursor_chat_surfaces.py
```
