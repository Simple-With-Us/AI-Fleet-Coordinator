---
name: secret-handoff
description: >-
  Fleet secret handling — handoff file, Zulip bot credentials, Infisical, Coolify token split, grep trap, leak response. Load BEFORE any command that might touch a credential, before reading ~/.secrets, Infisical, .env, or vault output, and before debugging auth. Trigger even when the user does not say "secret."
---

# Secret handoff (CURSOR)

> **This install is for `CURSOR`.**  Chat tag `[CURSOR·session8]`.  Notes `Cursor`.  Branches `cursor/`.  Worktrees `~/apps/<app>-cursor`.  Do not inherit another seat's tag from a shared template.  Zulip bot `cursor-bot@simplewithus.zulipchat.com`, credential file `~/.secrets/Zulip/Cursor-zuliprc` (mode 600).  Session tag `[CURSOR·session8]`, and the `agent-sync` CLI writes it for you.

> **Runtime fork (Cursor).** Local Cursor IDE / Auto on this Mac is `[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, your chat tag is `[GB-<NAME>]` (GB-CONDUCTOR, GB-MONITOR, GB-FIXER, GB-DEPLOYER, GB-COMPILER, GB-NURSE, GB-HOUSEKEEPER, GB-ACCOUNTANT, GB-ORACLE) — not `[GROK-BOT]`, not `[CURSOR]`, and not `[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.


Load `~/.cursor/skills/secret-safety/SKILL.md` as well when that file exists.  This skill is the fleet overlay.

**Redact in the same command that touches the secret.**  If you are deciding whether output is safe to show, you are already too late — it is in the transcript.

## BotFleet Secret Request Card (2026-09-01)

If you are operating within a BotFleet environment and need an API key or custom token (e.g. `GITHUB_TOKEN`, `AWS_ACCESS_KEY_ID`), you MUST use the `request_credential` MCP tool.

- The tool accepts `credential_id` or `custom_secret_name` and an optional `reason`.
- This triggers an interactive UI card (`SecretRequestCard`) where the user can securely type the key.
- The key is securely saved to `~/.botfleet/vault.json` and automatically injected into your tool environment variables.
- NEVER ask the user to paste a secret directly into the chat transcript.

## One canonical handoff file

`/Users/jay/.secrets/global-api-keys` (no extension).  There is no `global-api-keys.env` to use.  The superseded sibling is a recovery net, not a source.

Cloud: `GET https://mac.jays.services/files/key-names` with the same Bearer as `/files` (`MAC_COLLAB_TOKEN`).  Use names-only lookup; runtime keys are managed via Infisical per app.

Infisical (the app's own project, prod) is the source of truth for **deployed app runtime** secrets.  The handoff file is operator convenience and may go stale.  Copy cross-app keys into the consuming Infisical project (store-to-store), do not teach the app to read the handoff file.

Owner drops a `chmod 600` file and gives you the path.  Never print the value.  Prefer scoped, revocable credentials.  Remind the owner they can revoke when the task is done.

## Zulip bot credentials

Each seat bot's key is its own credential and nobody else's.  Mac seats:  `~/.secrets/Zulip/<file code>-zuliprc`, mode 600 (`Claude-zuliprc`, `Codex-zuliprc`, `MM-zuliprc`, …).  Cloud seats with no Mac filesystem:  env `ZULIP_EMAIL`, `ZULIP_API_KEY`, `ZULIP_SITE`.  `agent-sync` picks the source:  `--rc PATH`, then `ZULIP_RC`, then the seat file, then the env triple; `agent-sync whoami` shows which source it used, never the key.

Never print, `cat`, echo, or paste a zuliprc or a key, never put one in a message, a DM, a commit, or a log, and never type one literally on a command line.  Never use another seat's key, and never handle Jay's personal API key.  If a key ever lands in a message:  delete the message (do not edit — Zulip keeps edit history), have the owner rotate it in Settings → Bots, update Infisical first, then the seat file, and note the leak on THE BOARD without the value.

## Handoff-file grep trap (2026-08-14)

A tool result that contains even one `KEY=value` line has leaked.

Forbidden:

```bash
cat ~/.secrets/global-api-keys
grep '^[A-Z0-9_]+=' ~/.secrets/global-api-keys
grep '^ADMIN' ~/.secrets/global-api-keys
rg TOKEN ~/.secrets/
# Read / open-file / less / bat on that path
```

Allowed:

```bash
grep -oE '^[A-Z][A-Z0-9_]*' ~/.secrets/global-api-keys | sort -u

TOKEN="$(grep -m1 '^SOME_KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
some-command 2>&1 | sed "s/${TOKEN}/[REDACTED]/g"
```

Your file-read tool is `cat` for this purpose.  Do not "just look" with Read because grep quoting confused you.

## Loaded-key byte dumps (2026-09-17)

A variable that already holds a key is still a secret.  `od` / `xxd` /
`hexdump` / `hd` / `strings` / `base64` print every byte.  `cut -c` prints a
prefix.  Last-command `printf %s` / `%q` of `$NAME` (NAME looks like KEY /
TOKEN / SECRET / PASSWORD / DSN) prints the live value.  Same class as `cat`
of the handoff file.

Forbidden:

```bash
od -c <<< "$SILICONFLOW_API_KEY"
printf '%s' "$TOKEN" | xxd
echo "$API_KEY" | hexdump -C
cut -c1-4 <<< "$API_KEY"
printf '%q' "$SECRET"
```

Allowed:

```bash
[ -n "$TOKEN" ]
echo ${#TOKEN}
[[ $TOKEN == *'"'* ]] && echo quoted || echo clean
printf '%s' "$TOKEN" | wc -c
```

`~/.claude/hooks/secret-guard-pretooluse.py` denies the forbidden forms on
Claude Code Bash.  Follow the rule on every seat even when that hook is absent.

## Infisical

Never:

```bash
infisical secrets
infisical secrets --output json
infisical secrets get KEY --plain    # unless immediately reduced to length
```

Prefer `bash scripts/infisical-secrets-safe.sh {set|has|names} --projectId … --env prod`.  Verify writes by key presence and **length**, never by printing the value.

## Coolify

| Key | Allowed use |
|-----|-------------|
| `COOLIFY_SERVER_STATS` | Read-only metrics / server-stats UI |
| `COOLIFY_AGENTS` | Agent deploy/admin only |

Never put `COOLIFY_AGENTS` into Infisical as app `COOLIFY_API_TOKEN`.  Prefer env vars over `--token` flags (`ps` and debug loggers).

## Cloudflare "dead token" (2026-08-13)

Do not declare a Cloudflare credential dead without a real resource call.

1. `/user/tokens/verify` only understands **user-owned** tokens.  Account-owned tokens 401 there by design — use `/accounts/{id}/tokens/verify` or `GET /zones`.
2. Global API Key "9103 Unknown X-Auth-Key" can mean the **wrong email**, not a dead key.  This fleet has multiple Cloudflare logins.  Try the pairing in AGENT-SYNC / the handoff file's CLOUDFLARE section (names only) before concluding.

Empty `success:true` on a filtered Bearer call means "valid, not scoped to that filter," not dead.  Prefer `CLOUDFLARE_FLEET_API_TOKEN` for ordinary work.

## If it leaks

1. Stop.
2. Name exactly which credential (the owner already has it).
3. Tell the owner to rotate.
4. Delete scratch files / Zulip messages / GitHub comments you can still edit.  In Zulip, delete rather than edit — edit history is kept.  Do **not** force-push git history.  You cannot edit a prior chat turn — say so.
5. Change the technique, not "be more careful."

## Canon

- `/Users/jay/apps/AGENT-SYNC.md` § Secret handoff, Infisical, Coolify, grep trap, loaded-key byte dumps, leak response
- `<YOUR_SKILLS_DIR>/secret-safety/SKILL.md`
- Coordinator `docs/rollouts/2026-08-15-secret-file-grep-ban.md`
- Coordinator `docs/rollouts/2026-09-18-secret-guard-od.md`
