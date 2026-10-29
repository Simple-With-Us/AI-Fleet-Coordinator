---
name: secret-handoff
description: >-
  Fleet secret handling — fleet-secrets, Zulip bot credentials, Infisical, Coolify token split, grep trap, leak response.  Load BEFORE any command that might touch a credential, before reading ~/.secrets, Infisical, .env, or vault output, and before debugging auth.  Trigger even when the user does not say "secret."
---

# Secret handoff (CODEX)

> **This install is for `CODEX`.**  Chat tag `[CODEX·session8]`.  Notes `Codex`.  Branches `codex/`.  Lanes `~/apps/lanes/<Repo>/codex-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot for seat `CODEX`, credential in Infisical Fleet-OPS `/zulip` (`CODEX_ZULIP_EMAIL`, `CODEX_ZULIP_KEY`, `CODEX_ZULIP_SITE`).  Session tag `[CODEX·session8]`, and the `simple-chat` CLI writes it for you.


Load `~/.codex/skills/secret-safety/SKILL.md` as well when that file exists.  This skill is the fleet overlay.

**Redact in the same command that touches the secret.**  If you are deciding whether output is safe to show, you are already too late — it is in the transcript.

## BotFleet Secret Request Card (2026-09-01)

If you are operating within a BotFleet environment and need an API key or custom token (e.g.  `GITHUB_TOKEN`, `AWS_ACCESS_KEY_ID`), you MUST use the `request_credential` MCP tool.

- The tool accepts `credential_id` or `custom_secret_name` and an optional `reason`.
- This triggers an interactive UI card (`SecretRequestCard`) where the user can securely type the key.
- The key is securely saved to `~/.botfleet/vault.json` and automatically injected into your tool environment variables.
- NEVER ask the user to paste a secret directly into the chat transcript.

## Fleet keys come from `fleet-secrets`

The flat file `/Users/jay/.secrets/global-api-keys` is retired in favor of Infisical Fleet-OPS via `fleet-secrets` (OPS-818).  Nothing may read the flat file; there is no `global-api-keys.env` either, and the superseded sibling is a recovery net, not a source.

On the Mac:

- `fleet-secrets get NAME` prints one value (no trailing newline, no quote-wrapping).  Exit 3 means the key is absent.
- `fleet-secrets run --only A,B -- CMD` runs a command with just those keys as environment variables.
- `fleet-secrets names` lists key names only.  `fleet-secrets doctor` prints booleans and counts only.

Cloud: there is no key-names endpoint now (the old Mac key-names endpoint was retired 2026-10-19); ask a Mac seat or the owner.  Runtime keys are managed via Infisical per app.

Infisical (the app's own project, prod) is the source of truth for **deployed app runtime** secrets.  Every app reads only its own project's keys, so a credential another app owns is never that app's credential: a shared service gets its own scoped token in the consuming app's own project instead of a copy of another app's key, and no app is taught to read `fleet-secrets` or the retired flat file.

When the owner hands you a new secret, it arrives as the path to a `chmod 600` file (never chat text).  Never print the value.  Prefer scoped, revocable credentials.  Remind the owner they can revoke when the task is done.

## GitHub App tokens (git-relay)

`git-relay` is the only path in the fleet that mints per-bot GitHub App tokens.  A seat never handles an App id, a private key, or the one-hour token expiry: the relay mints and refreshes the seat's own GitHub App token itself, so no credential material is ever pasted into a prompt, a file, or a command line.  PR-write authoring (`open_pr`, `edit_pr`, `reply_thread`, `comment_pr`) runs under the owner's scoped PR-only token, so a PR is authored as the owner and the review bots work normally, while `merge_pr`, `workflow_dispatch` and `resolve_thread` run under the seat's own App token.  Per-seat policy (allowed actions, allowed repos, mint ceilings, an hourly call cap) is an allowlist layer that can only narrow the underlying credential, never exceed it, and every call — allowed or denied — lands in an audit log.

The relay is **not deployed yet** (verified Oct 23 2026): there is no daemon, no launchd job, no seat pointed at it and no service host chosen yet, so the interim `gh` and shell path still stands and no seat should expect a minted token today.

## Zulip bot credentials

Each seat bot's key is its own credential and nobody else's.  Every seat, Mac or cloud, resolves its `<BOT>_ZULIP_EMAIL`, `<BOT>_ZULIP_KEY` and `<BOT>_ZULIP_SITE` at runtime through the same approved path: the process environment first (only when it already holds the Infisical-backed value), then `fleet-secrets get` for that name as the approved fallback (or verify with `simple-chat whoami`).  An independent container value is not a credential source.  A seat that cannot resolve the credential (env empty and `fleet-secrets get` unavailable or missing) stops and asks the owner or operator for a qualified route.  It never pastes, mints or invents a key, never opens Infisical by hand as a second path, and never reads another seat's value.  A missing required credential fails with an error naming the variable.  Infisical Fleet-OPS `/zulip` is the one home of a seat's Zulip key; there is no independent credential source for it.  The CLI accepts an operator-set override for a single invocation (`--rc PATH`, or `ZULIP_RC`); that is not a second home for the key, and a listener seat never reads the CLI's generic variable triple, so two seats cannot alias one bot.  `simple-chat whoami` shows which source it used, never the key.

Never print, `cat`, echo, or paste a zuliprc or a key, never put one in a message, a DM, a commit, or a log, and never type one literally on a command line.  Never use another seat's key, and never handle Jay's personal API key.  If a key ever lands in a message: delete the message (do not edit — Zulip keeps edit history), have the owner rotate it in Settings → Bots, update Infisical (the only home of the key), and note the leak on the Linear issue (`simple-board comment <id>`) without the value.

## Secrets-file grep trap (2026-08-14)

A tool result that contains even one `KEY=value` line has leaked.

Forbidden:

```bash
grep '^[A-Z0-9_]+=' ~/.secrets/some-file
grep '^ADMIN' ~/.secrets/some-file
rg TOKEN ~/.secrets/
fleet-secrets run -- env
# Read / open-file / cat / less / bat on any secrets file
```

Allowed:

```bash
fleet-secrets names

TOKEN="$(fleet-secrets get SOME_KEY)" || exit $?
fleet-secrets run --only SOME_KEY -- some-command
```

Never add `2>&1` to a command that touches a secret.  If you must keep a value out of output you will see, redact on stdout only, with the secret on file descriptor 3 so it never reaches the process table (`some-command | python3 -c 'import sys; tok = open(3).read().rstrip("\n"); data = sys.stdin.read(); sys.stdout.write(data.replace(tok, "[REDACTED]") if tok else data)' 3<<<"$TOKEN"`).  The `secret-safety` pack carries the full form; never write `sed "s/$TOKEN/[REDACTED]/g"`.

Your file-read tool is `cat` for this purpose.  Do not "just look" with Read because quoting confused you.

## Loaded-key byte dumps (2026-09-17)

A variable that already holds a key is still a secret.  `od` / `xxd` /
`hexdump` / `hd` / `strings` / `base64` print every byte.  `cut -c` prints a
prefix.  Last-command `printf %s` / `%q` of `$NAME` (NAME looks like KEY /
TOKEN / SECRET / PASSWORD / DSN) prints the live value.  Same class as `cat`
of a secrets file.

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
Claude Code Bash.  Follow the rule on every seat even when that hook is absent.

## Infisical

Never:

```bash
infisical secrets
infisical secrets --output json
infisical secrets get KEY --plain    # unless immediately reduced to length
```

Prefer `bash scripts/infisical-secrets-safe.sh {set|has|names} --projectId … --env prod`.  Verify writes by key presence and **length**, never by printing the value.

## Coolify

| Key | Allowed use |
|-----|-------------|
| `COOLIFY_SERVER_STATS` | Read-only metrics / server-stats UI |
| `COOLIFY_AGENTS` | Agent deploy/admin only |

Never put `COOLIFY_AGENTS` into Infisical as app `COOLIFY_API_TOKEN`.  Prefer env vars over `--token` flags (`ps` and debug loggers).

## Cloudflare "dead token" (2026-08-13)

Do not declare a Cloudflare credential dead without a real resource call.

1. `/user/tokens/verify` only understands **user-owned** tokens.  Account-owned tokens 401 there by design — use `/accounts/{id}/tokens/verify` or `GET /zones`.
2. Global API Key "9103 Unknown X-Auth-Key" can mean the **wrong email**, not a dead key.  This fleet has multiple Cloudflare logins.  Try the pairing in FLEET-PROTOCOL.md / the `CLOUDFLARE_*` names from `fleet-secrets names` before concluding.

Empty `success:true` on a filtered Bearer call means "valid, not scoped to that filter," not dead.  Prefer `CLOUDFLARE_FLEET_API_TOKEN` for ordinary work.

## If it leaks

1. Stop.
2. Name exactly which credential (the owner already has it).
3. Tell the owner to rotate.
4. Delete scratch files / Zulip messages / GitHub comments you can still edit.  In Zulip, delete rather than edit — edit history is kept.  Do **not** force-push git history.  You cannot edit a prior chat turn — say so.
5. Change the technique, not "be more careful."

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` § Secret handoff, Infisical, Coolify, grep trap, loaded-key byte dumps, leak response
- `<YOUR_SKILLS_DIR>/secret-safety/SKILL.md`
- Coordinator `docs/rollouts/2026-08-15-secret-file-grep-ban.md`
- Coordinator `docs/rollouts/2026-09-18-secret-guard-od.md`
