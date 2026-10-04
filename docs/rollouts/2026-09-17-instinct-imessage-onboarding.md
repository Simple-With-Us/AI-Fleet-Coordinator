# 2026-09-17 — Onboarding prompt for INSTINCT, the iMessage interface seat

PR #251.  Branch `claude/instinct-imessage-onboarding-n6u9oh` (Claude Code cloud, seat CLAUDE).

## Context

The owner asked what instructions to give a new iMessage-based interface agent called Instinct so it
knows how to interact with the team.  The fleet already has a paste-ready prompt pattern
(`docs/FX-ONBOARDING-PROMPT.md`, `docs/CODEX-FLEET-RAG-PROMPT.md`) and a standing seat procedure
(`docs/ONBOARDING-NEW-AGENT.md`), so the answer is a third prompt doc on that pattern rather than a
one-off join.

## What changed

- `docs/INSTINCT-ONBOARDING-PROMPT.md` (new).  Owner-facing preamble (assumed defaults, what Instinct
  needs before its first session), the prompt itself, the Agent Seat row text, a "how to tell it
  took" list, and the existing iMessage jobs the prompt builds on.
- `STATUS.md` and `docs/EFFORT-LOG.md` rows for this unit.
- Follow-up section for a browser-only runtime (added the same day after Instinct reported a form-only vault and no MCP client): board by browser session and by GitHub issue, no Slack write until a Mac-side bridge exists, no recall or seat-mcp, revised first unit.  Facts checked in `scripts/mac-collab/mac-collab-server.py`, `scripts/mac-collab/sync_board.py`, and `scripts/agent-sync-push/daemon.js`.

## Decisions

- Instinct is an **interface seat**: it files, wakes, drives (seat-mcp), and reports; it owns no
  coding lane unless the owner assigns one.  So no `fleet-apps.json` entry yet (that file drives
  worktrees, branch prefixes, and the digest legend).
- Tag `INSTINCT` by default; `BF-INSTINCT` if it runs as a BotFleet bot.  Everything else holds.
- Transport is bound by `AGENT-SYNC.md` § Process 10: send only from the `agents` macOS account
  through the one authorized listener and sender; never a second `chat.db` reader or a sender
  under `jay` (the 2026-09-02 echo loop is the precedent).  The prompt tells Instinct to file a
  board item and stop if it needs more; only the owner amends Process 10.
- New Slack body field `owner-relay:` for verbatim owner quotes with a Central Time stamp, so peers
  can tell relayed owner words from Instinct's own inference.  Documented in the seat row.
- The owner's phone gets watcher noise discipline: forward Slack only on a tag / app / `->FLEET` /
  HALT match.

## Verification

- Sentence-gap check on the new doc: no single-space sentence boundaries.
- `python3 scripts/check-fleet-registry.py` (no registry file changed).

## Not done here

- Board item and Slack claim: this sandbox has no `MAC_COLLAB_TOKEN` and its `SLACK_BOT_TOKEN`
  returns `invalid_auth`.  A Mac seat files the board item when the owner confirms the tag.
- Seat row not landed in `AGENT-SYNC.md` (either copy) until the owner confirms the tag; the row
  text is in the doc.

## Board `/login` form and GitHub outbox bridge (same day, owner: "Ok let's do that")

Instinct cannot set an `Authorization` header and has no MCP client, so the board, the Slack relay,
and seat-mcp were all out of reach.  Instead of making a frontier seat its hands by chat, two
deterministic pieces:

- `scripts/mac-collab/mac-collab-server.py`: `GET /login` serves an HTML form (username field for
  password-manager autofill, `token` password field); `POST /login` runs the same `token_matches`
  check as Basic and Bearer, mints the same 30-day HttpOnly `mac_collab_session` cookie with the same
  identity mapping (`MAC_COLLAB_TOKEN_<SEAT>=` gives the seat identity), and 303s to `/board`.
  Failures share the Bearer rate limit (`AUTH_FAIL_MAX` per `AUTH_FAIL_WINDOW_S`, 429 after), are
  never echoed, and audit as `action=login name= ok=0` with no value.  The `/board` 401 body now
  links `/login` for a browser that cancels the native dialog, and the in-page token bar mentions it.
  Basic, Bearer, and the existing cookie path are unchanged.  Tests:
  `scripts/mac-collab/test_login_form.py` (8).
- `scripts/github-outbox-bridge.py` + `scripts/launchd/com.jay.github-outbox-bridge.plist`: every
  120 s, post new comments on a private outbox issue to `#agent-sync` through the loopback relay
  as `username=<SEAT>` after a header-shape check (rocket = posted; confused + reply = rejected),
  and mirror skim matches back as `<!-- outbox-bridge:slack -->` comments marked as data.  Tokens
  from `~/.secrets/agent-sync.env` only.  Relay failures keep the comment queued and note the outage
  on the issue once after three ticks.  Single-flight `flock`.  Stdlib only; compiles and tests on
  Python 3.10 (the Mac runs 3.9; no 3.10+ syntax used).  Tests: `scripts/test_github_outbox_bridge.py`
  (14).

### Install (Mac seat; cloud sessions do not install LaunchAgents)

```bash
# board: tracked copy -> live copy, then restart
cp ~/Code/AI-Fleet-Coordinator/scripts/mac-collab/mac-collab-server.py ~/apps/mac-collab/mac-collab-server.py
python3 ~/Code/AI-Fleet-Coordinator/scripts/mac-collab/test_login_form.py
pm2 restart mac-collab
curl -sS -o /dev/null -w '%{http_code}\n' https://mac.jays.services/login   # expect 200

# seat token for Instinct (value never printed); file is canonical, no restart
# append: MAC_COLLAB_TOKEN_INSTINCT="<new value>"   to ~/.secrets/mac-collab.env

# bridge: outbox issue first, then config, dry-run, then the LaunchAgent
# open Simple-With-Us/fleet-ops issue "[INSTINCT] Slack outbox"; note its number N
cp ~/Code/AI-Fleet-Coordinator/scripts/github-outbox-bridge.py ~/apps/github-outbox-bridge.py
printf '{"seats":[{"seat":"INSTINCT","repo":"Simple-With-Us/fleet-ops","issue":N}]}\n' > ~/apps/github-outbox-bridge.json
python3 ~/apps/github-outbox-bridge.py --once --dry-run
cp ~/Code/AI-Fleet-Coordinator/scripts/launchd/com.jay.github-outbox-bridge.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jay.github-outbox-bridge.plist
launchctl print gui/$(id -u)/com.jay.github-outbox-bridge | head -5
```

Then, in the same unit: the live `~/apps/MAC-LOCAL-PROCESSES.md` row (flip the tracked row from
"Not yet installed" to its live status), `~/apps/apple-notes-coding.sh --update "⭐️ Background Jobs
Master List"`, the live `~/apps/AGENT-SYNC.md` § THE BOARD paragraph, and the outbox issue number
on Instinct's registration item.  Rotate the outbox issue when it grows long: close it, open a new
one, update the JSON.

### Verification (this sandbox)

```bash
python3 scripts/mac-collab/test_login_form.py          # 8 OK
python3 scripts/mac-collab/test_token_staleness.py      # 10 OK
python3 scripts/mac-collab/test_bind_reclaim.py         # 11 OK
python3 scripts/mac-collab/test_write_back.py           # 4 OK
python3 scripts/test_github_outbox_bridge.py            # 14 OK (also under /usr/bin/python3.10)
python3 -m py_compile scripts/mac-collab/mac-collab-server.py scripts/github-outbox-bridge.py
```
