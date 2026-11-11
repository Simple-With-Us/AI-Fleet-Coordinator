# agy-acp-runtime

Tracked copies for the live Mac install at `~/apps/agy-acp-runtime`.

## Pipes (do not mix)

1. **pm2 `:8765` hop** — `start.sh` → `stdio-to-ws` → `agy-acp-turbo.sh` → `/usr/local/bin/agy-acp --skip-naration`.  Loopback bind is `bind-loopback.cjs`.  Do not point this hop at the list wrapper.  Do not touch `grok-acp` `:12419` or re-enable launchd.
2. **Shellular id `agy`** — today `~/.local/bin/agy-acp-turbo` (same turbo policy).  That binary has `loadSession` / `sessionCapabilities` strings but no `session/list` method.  Shellular 0.0.56 custom agents then throw `Agent "agy" does not support session/list`.

`agy-acp-list-wrapper` is only for pipe 2.

## What the list wrapper does

`agy-acp-list-wrapper.sh` execs `agy-acp-list-wrapper.cjs`, a Node NDJSON JSON-RPC proxy.

- Spawns sibling `agy-acp-turbo.sh`, or `/usr/local/bin/agy-acp --skip-naration` if the turbo script is missing.  Override with `AGY_ACP_CHILD`.
- Proxies every JSON-RPC line (`session/new`, `session/prompt`, `session/resume`, `session/load`, …) to that child unchanged.
- On a successful child `initialize` **result**, sets `agentCapabilities.sessionCapabilities.list = true` and keeps any existing `loadSession`.
- If the child `initialize` returns an error, or the child cannot be spawned, the wrapper fail-closes.  It does not invent a ready agent.
- Handles `session/list` and `sessions/list` locally.  It does **not** forward those methods to `agy-acp` (v0.1.0 has no list method).

Listing uses the live Mac CLI layout (verified 2026-08-25).  If `last_conversations.json` is missing, the wrapper returns `[]` and does not crash the seat.

| Path | Role |
| --- | --- |
| `~/.gemini/antigravity-cli/cache/last_conversations.json` | **Primary list.**  Shape `{ "<cwd>": "<uuid>" }` only. |
| `~/.gemini/antigravity-cli/conversation_summaries.db` | Title/preview lookup for an id already in the map.  Not a list source (July-frozen on the live Mac). |
| `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` | First `USER_INPUT` title fallback, then last `created_at` |
| `~/.gemini/antigravity-cli/conversations/<id>.db` | `updatedAt` from mtime for an id already in the map. |

Title waterfall: summaries title/preview if that id exists → first transcript `USER_INPUT` → `Untitled`.  Transcripts are also checked under `antigravity/` and `antigravity-ide/` brain trees for title/`created_at` only.  Do not use `cache/conversation_metadata.json`, `~/.antigravity/sessions`, or `~/.shellular/agy` as a list.

v1 lists only `last_conversations.json` pairs so the seat cannot go stale.  `AGY_ACP_LIST_EXTRA_DBS=1` optionally adds other `conversations/*.db` (CLI, then GUI) after that cache file exists.

Override with `AGY_ACP_HOME` / `ANTIGRAVITY_CLI_ROOT`, `AGY_ACP_LAST_CONVERSATIONS`, `AGY_ACP_BRAIN_DIR`, `AGY_ACP_CONVERSATIONS_DIR`, `AGY_ACP_SUMMARIES_DB`.

This is not a JSONL-only standalone agent.  Prompts still go through live `agy-acp`.

## Point Shellular at the wrapper

Do **not** commit `~/.shellular/agents.json` (live Mac file).  After copying the tracked files to `~/apps/agy-acp-runtime/`, point the custom `agy` command at the wrapper:

```json
{
  "id": "agy",
  "command": "/Users/jay/apps/agy-acp-runtime/agy-acp-list-wrapper.sh"
}
```

Or symlink `~/.local/bin/agy-acp-turbo` to that `.sh`.  The wrapper still execs sibling `agy-acp-turbo.sh` as the child, so turbo flags stay on the real adapter.

```bash
cp scripts/agy-acp-runtime/agy-acp-list-wrapper.sh \
   scripts/agy-acp-runtime/agy-acp-list-wrapper.cjs \
   scripts/agy-acp-runtime/README.md \
   ~/apps/agy-acp-runtime/
chmod +x ~/apps/agy-acp-runtime/agy-acp-list-wrapper.sh \
         ~/apps/agy-acp-runtime/agy-acp-list-wrapper.cjs
```

Do not change `start.sh` for this.  Do not `pm2 restart agy-acp` unless you are also refreshing turbo / bind files from PR 117.

## Tests

```bash
bash scripts/test-agy-acp-runtime.sh
bash scripts/test-agy-acp-list-wrapper.sh
```
