# mac-collab (tracked copy)

> **Archive after the Linear cutover (owner 2026-10-10).**  Linear replaced THE BOARD.  `mac-collab-sync` and `mac-collab-writeback` are stopped and removed, the server stays as a read-only archive with `mac-collab-litestream` for 14 days, and it is retired at about day 30.  `board-legacy` and `BOARD_BACKEND=mac-collab` (the rollback) still run the old CLI against it.  The server has no read-only switch, so after the cutover no seat is told to write to it.  See `scripts/linear-board/README.md` and `AGENT-SYNC.md` § Linear and the Claim Lifecycle.

Live process: `~/apps/mac-collab/` under pm2 `mac-collab` (HTTP), `mac-collab-sync`
(files+issues → board), and `mac-collab-writeback` (board writes → live effort logs
+ GitHub Issues).  This directory is the git copy.  Edit live, then copy here
before landing.

Do not serve any secret store. Names-only: `GET /files/key-names`.

THE BOARD (`/board`) sets an HttpOnly `mac_collab_session` cookie after a successful
Basic (or returning cookie) login so the owner is not asked for `MAC_COLLAB_TOKEN`
on every load.  Cookie max-age is 30 days.  JS `fetch` uses `credentials: include`
and only shows the in-page token bar on 401.  Agents still use Bearer.
`GET /login` is an HTML form for browsers whose password manager cannot fill the native
Basic dialog (browser-only seats such as Instinct): `POST /login` with a form field `token`
runs the same token check, mints the same 30-day cookie, and 303s to `/board`.  A
`MAC_COLLAB_TOKEN_<SEAT>` key in Infisical Fleet-OPS `/mac-collab` keeps that seat's writes
attributed to itself.  Failures share the Bearer rate limit and are never echoed or logged.
`board show` / `board status` accept unique 8-char id prefixes.
`mac-collab-sync` snapshots `findings.db` under `~/apps/mac-collab/backups/` (14-day keep).
Writeback: `write_back.py --loop`.  Protocol: `docs/BOARD-WRITEBACK-PROTOCOL.md`.

## Tests

```bash
python3 scripts/mac-collab/test_bind_reclaim.py
python3 scripts/mac-collab/test_token_staleness.py
python3 scripts/mac-collab/test_write_back.py
python3 scripts/mac-collab/test_login_form.py
```

`test_bind_reclaim.py` covers the 2026-08-20 EADDRINUSE outage path (stale
board server SIGTERM, SIGKILL fallback, refuse to kill a healthy sibling or
an unrelated process, lsof-timeout -> ps fallback).  Board `cd895d6f`.

## Token rotation (board `029f6346`)

Infisical Fleet-OPS `/mac-collab` is canonical (AFC-359).  `load_tokens()` re-reads it
through `fleet-secrets run --path /mac-collab` at most once every `MAC_COLLAB_TOKEN_TTL`
seconds (60 by default), so a rotation takes effect without a server restart;  a failed
read keeps the last good tokens.  Process-env `MAC_COLLAB_TOKEN*` is ignored when
Infisical has tokens (cloud seats with none still use env).  Authenticated `GET /health`
includes `token_source` (`infisical`, `env` or `missing`), `token_count`,
`vault_refreshed_at`, `pid`, and `restart_hint` — never the token value.  A 401 body
carries the same restart hint.  `board` CLI reads `fleet-secrets get MAC_COLLAB_TOKEN
--path /mac-collab` first and prints that hint on 401.

After rotating the token, long-running processes that exported the old token
at start still need a new shell.  `pm2 restart mac-collab --update-env` if
the server process env itself still holds a stale token (Infisical-canonical
auth does not need that restart).
