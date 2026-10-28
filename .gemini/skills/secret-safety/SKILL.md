---
name: secret-safety
description: >-
  Hardcoded, mandatory safeguards for handling secrets, API keys, tokens, and credentials during any agent session.  Load and follow this BEFORE running any command whose arguments or environment touch a credential, before printing or displaying any config file, JSON dump, env listing, header, URL, or command string that could contain one, before reading from a known secret source (a secrets-handoff file, Infisical or any other secrets manager, a database, a vault), and before debugging an API/auth failure.  Trigger this even when the request doesn't say "secret" or "credential" — command args, headers, URLs, and verbose/debug tool output are the most common accidental leak vectors, not just files literally named .env or secrets.json.
---

# Secret safety

## Why this exists

Two real incidents happened back to back in one conversation.  A masking script redacted a config's `env` object correctly but missed a bearer token sitting in that same config's `args` array — printed a GitHub PAT, a Render API key, a Twilio SID+token, and a Langfuse basic-auth header in full.  Then, minutes later, a diagnostic command's own debug logging echoed back a `--header "Authorization: Bearer <token>"` argument on stderr, and that stderr got displayed without a second look — exposing another key.

Neither incident was "hunting for secrets" — both credentials had already been legitimately placed in a config file by the owner, for the explicit purpose of being used.  The actual failure was narrower and more mechanical: redaction was applied by *choosing what to relay* after a command already ran, when the real leak point is earlier — the moment a tool call's output lands in context at all, it's already in the transcript, and no amount of care about what gets said next undoes that.  This is a standing practice, not a fix for one conversation — load it every session, not just the one where something went wrong.

## The one rule underneath everything below

**Redact inline, as part of the command that touches the secret — never as a judgment call about what to display afterward.**  If the raw output is already in front of you and you're deciding whether it's safe to show, you're one step too late; the fix has to happen before the command's output is generated, not after.

## How to do it mechanically

1. Extract the secret into a shell variable first.  Never hand-type or interpolate a literal secret value into a command string yourself.
2. Substitute that variable's value out of ALL output — stdout and stderr both — as part of the exact same command, before you ever see it:
   ```bash
   TOKEN="$(cat some-secret-file)"
   some-command --header "Authorization: Bearer $TOKEN" 2>&1 \
     | python3 -c 'import sys
   tok = open(3).read().rstrip("\n")
   data = sys.stdin.read()
   sys.stdout.write(data.replace(tok, "[REDACTED]") if tok else data)' 3<<<"$TOKEN"
   ```
   Do this even when you don't expect the tool to echo anything back.  Plenty of CLIs — OAuth proxies, verbose installers, debug loggers — print exactly the arguments or headers they were invoked with, and there's usually no way to know in advance which ones will until it's already too late.  The substitution has to be a **literal string replacement, never a regular expression**: a token that contains `/`, `&`, `.`, `*`, `[` or a newline breaks a `sed s///` pattern, so the substitution either corrupts the output or prints the secret in full, which is the leak this skill exists to prevent.  `str.replace` is literal by construction.  The token rides on file descriptor 3, so it never reaches the process table or a file on disk, and the empty-token guard keeps an unset value from shredding the output.  Never write `sed "s/$TOKEN/[REDACTED]/g"`.
3. If a command writes to a *file* rather than stdout/stderr (a downloaded config, a cache dump, a log), apply the same substitution when you read that file back, not just to live command output.
4. When inspecting an existing config/JSON structure that might hold secrets (not one you're actively invoking), write the masking pass to walk the WHOLE structure recursively — every string field, every array element — not just a named `env` or `secrets` object.  The exact bug that caused incident #1 was a masking function that handled `env` correctly and simply never looked at `args`.

## Default to broad redaction, not a list of risky-looking names

A field being named `env`, `secret`, `key`, or `token` is a hint, not a boundary.  Credentials show up in places with innocuous names: a bare `args` array, a `command` string with a flag baked in, a URL's query string, a base64 Basic-auth header that "looks like" opaque encoded data, a `--header` flag, a cached OAuth `client_info.json`.

So invert the default: when dumping or displaying any config object you didn't write yourself, mask every string value past a trivial length, and only let through fields you've explicitly confirmed are safe (a command name, a boolean, a short enum, a URL host with no query string) — allowlist the safe fields, don't blocklist the risky-looking ones.  If you're not sure whether a given field could hold a secret, treat it as if it does.

## Your own file-read tool is exactly as dangerous as `cat` — this is not a Bash-only rule

A fourth incident: an agent needed one value (`CT_ADMIN_TOKEN`) out of a known multi-secret file.  A `grep '^KEY=' file | cut -d= -f2` came back with the value still quote-wrapped, which made a same-value comparison look like a mismatch.  Rather than fix the shell parsing, the agent reached for its own **file-read tool** — reasoning, in effect, "the rule says don't `cat` it, and this isn't `cat`."  That tool has no concept of "this file holds credentials" — it rendered the entire file, every one of ~50 live keys (Stripe secret key, GitHub PATs, a Backblaze master key, Turso/Cloudflare/Sentry/Hetzner-root credentials, the Infisical universal-auth identities that themselves unlock everything else), straight into the visible transcript.  This was strictly worse than the incidents above: not one value, the whole file.

**The rule is about the outcome, not the command name.**  Anything that renders a known secrets file's full contents into your context is `cat` for this purpose, regardless of what it's called: a dedicated Read/open-file tool, an IDE's "open file" action, a pager (`less`, `more`, `bat`), a "show file" MCP tool, a Jupyter `!cat` cell — all identical in effect.  Before pointing ANY tool at a path you know or suspect holds multiple credentials (a secrets-handoff file, a `.env`, a downloaded vault export), ask "does this tool return the whole file's content to me" — if yes, and you don't need to see the *whole* file, it's the wrong tool for this job.  Use a targeted extraction instead: a scoped `grep`, a `jq` filter, or a small script that reads the file *and* reduces it to only the one field you need before that result ever reaches you.  If the shell-based extraction is producing a confusing result (quoting, whitespace, encoding) — that confusion is itself a signal to slow down and fix the parsing (e.g. strip quotes explicitly, or write a two-line Python snippet that parses `KEY=value` properly), never a reason to fall back to a full-file view "just to see what's going on."

If you're not certain whether a file holds secrets, treat it as if it does before choosing a tool to inspect it.

### If your platform is not the one this was written on

The above pattern is written from one platform's own experience of getting this wrong, and the specific tool names above (Read, Bash) are that platform's — a different agent platform will have its own equivalent of "a tool that renders full file contents into context" (a file-open action, a codebase-search preview, a terminal pane), and this note can't enumerate all of them.  Treat this as advisory, written by an agent from a platform-specific incident, not a verified cross-platform procedure: the underlying principle (never let a whole known-secrets file render into your own context, regardless of which specific tool does it) should hold everywhere, but confirm the exact mechanics — which of your own tools are "safe, reduces to a value" vs.  "unsafe, dumps everything" — against your own platform's actual behavior rather than assuming it matches another platform's.

## Reading from known secret sources

The pattern is identical everywhere: extract the one value you actually need, programmatically, and never let the raw value pass through your own visible output on its way there.

**Infisical Fleet-OPS through `fleet-secrets` (the fleet's only home for keys; the old `~/.secrets` flat files are retired, AFC-359).**  Fetch the one value you need into a variable, or hand a command only the names it needs, and never let the value pass through your own visible output:
```bash
KEY="$(fleet-secrets get SOME_KEY)"          # exit 3 means the key is absent
fleet-secrets run --only A,B -- some-command  # A and B appear in that command's environment only
```
A key that was stored with quote-wrapping elsewhere comes back without quotes from `fleet-secrets get`.  A file the owner hands you at some other path (a `chmod 600` file) gets the same treatment: never `cat` it, `grep` it loosely, or open it with any whole-file-view tool (see above).

Listing just the key NAMES is fine — that's metadata, not a value — but **only** if
the command cannot emit a value.  Use `-o` so the match is the name only:

```bash
fleet-secrets names | sort -u
```

**Forbidden (2026-08-14 Grok incident):** `grep '^[A-Z0-9_]+=' file`, `grep '^ADMIN' file`,
or `rg TOKEN ~/.secrets/` — those print the whole matching **line**, which is `KEY=value`.
That dumped the entire handoff store into a chat transcript.  `grep PATTERN file` without
`-o` on a secrets file is a leak, even when the intent was "list which keys exist."

**Forbidden (2026-09-17 loaded-key byte dump):** once the value is in a shell
variable, `od` / `xxd` / `hexdump` / `hd` / `strings` / `base64` / `cut -c` /
last-command `printf %s` of that variable print the live secret.  A
subagent loaded `SILICONFLOW_API_KEY` correctly, then ran `od -c` to look for
stray quotes, and the full key hit the transcript.  Safe shape checks:
`[ -n "$VAR" ]`, `${#VAR}`, `[[ $VAR == *'"'* ]]`.  Need length only:
`printf '%s' "$VAR" | wc -c`.  The Bash PreToolUse hook
`~/.claude/hooks/secret-guard-pretooluse.py` denies these forms in Claude Code; every other platform enforces the same rule itself.

**Infisical, or any secrets-manager API/MCP tool.**  A `get-secret`/`list-secrets` call returns the value embedded in its JSON result (e.g.  `secret.secretValue`) — the tool result will contain the raw value the moment the call succeeds, which is unavoidable if the value is actually needed.  From there, treat it exactly like a locally-loaded variable: use it in the next command with inline redaction, don't restate it in your own prose, don't copy it into a new file "for reference," and don't let a later command echo it back unredacted.

### Infisical CLI hard bans (fleet, 2026-07-30)

The Infisical CLI's default `infisical secrets` **table-prints every secret value**.  That is a
confirmed leak vector (agent transcript).  **Never run:**

- `infisical secrets` with no subcommand (list-all-with-values)
- `infisical secrets --output json|yaml|dotenv` unless you **only** extract key *names* via `jq` and never surface the raw blob in the agent-visible transcript
- `infisical secrets get KEY --plain` unless the value is immediately reduced to a non-secret metric (e.g. piped to `wc -c` only) and never printed

**Prefer** repo helper when present: `bash scripts/infisical-secrets-safe.sh {set|has|names} …`.

### Coolify token split (do not mix)

- `COOLIFY_SERVER_STATS` = **read-only** → website/server-stats only; Infisical app metrics
- `COOLIFY_AGENTS` = **full** deploy/admin → agent ops / GH deploy only — **never** as the app's stats `COOLIFY_API_TOKEN`
- If Infisical still has `COOLIFY_API_TOKEN` for metrics, it must equal `COOLIFY_SERVER_STATS`, not `COOLIFY_AGENTS`

Canonical: `/Users/jay/apps/FLEET-PROTOCOL.md` § Secrets and Credentials.

**Anything else** — a database column, a cloud vault, a `.env` file, a config JSON, a password manager entry, a token field in some other API's response.  Same rule regardless of source: fetching it is sometimes unavoidable, and once it's fetched, the job shifts from "prevent access" to "prevent it from propagating any further than the one place it's actually needed" — don't let it flow into a second place (a log line, a repeated command, a written file, your own explanatory text) without the inline-redaction technique above.

## Prefer env vars over CLI args when a tool supports both

If a tool accepts a credential either as `--flag value` or as an environment variable, use the environment variable.  Not foolproof — a process can still log its own environment — but it closes off the single most common leak path: full command lines show up in `ps aux`, in shell history, and in exactly the kind of "here's what I was invoked with" debug logging that caused incident #2. Environment variables are far less commonly echoed back by that kind of logging.

## Don't perform theater once a value is already exposed

If a raw secret value already landed in a tool result despite the above, it's already in the transcript.  Showing the person you're working with a hand-wavy `sk-or-v1-***` or `[REDACTED]` in your OWN prose at that point accomplishes nothing — the transcript already has the real value, obscuring it from the person who legitimately owns that credential doesn't undo the exposure, and it actively misleads them into thinking the situation is more contained than it is.  That's worse than saying nothing plainly, because it manufactures false confidence at exactly the moment they most need an accurate read on what actually happened.

Masking is a tool for BEFORE a value enters a tool result — not a way to soften how you talk about one that's already there.  Once something is confirmed already exposed in the transcript, name exactly what leaked, in plain terms, and move straight to telling the owner to rotate it.

## Never write a redaction pattern reactively, after something already went wrong

A third incident happened the same night as the first two, and it was worse than either: after a secret fragment had already leaked once, the fix attempt was a hand-written `sed` pattern built on the spot to filter a shell error message — and that pattern was written for bash's error format (`file: line N: TOKEN: command not found`) while the actual shell in use was zsh, which formats the same error differently (`file:N: command not found: TOKEN`).  The filter matched nothing, and the same fragment printed again.  Then it happened a third time while trying to redact the retry.

The lesson isn't "get the regex right" — it's that **writing a new content-filtering pattern under pressure, immediately after a mistake, is itself the failure mode.**  You don't reliably know the exact shape of output you're about to filter (which shell, which error format, whether the message embeds the failing value verbatim), and guessing wrong doesn't fail safe — it fails by displaying the thing you were trying to hide.  If a first attempt at safely inspecting something didn't clearly work, the next step is never a second, hastier filter attempt on the same content.  Switch entirely to a method that cannot display content regardless of its shape:

- Redirect stderr to `/dev/null` or a file, never to something you read.  Check the exit code (`$?`) alone to know success or failure — that tells you what you need without any content ever being at risk.
- If you need to know *why* something failed, diagnose structurally instead of by reading the error text: line counts, regex-shape validation per line (`^[A-Za-z_][A-Za-z0-9_]*=.*$`), boolean checks (does this value contain whitespace? is a quote count even?), lengths.  All of these tell you what's wrong without the code path ever having a chance to print a value.
- If a genuine "read the actual error text" step is unavoidable, write it to a file and inspect that file's *properties* (size, line count) before deciding whether it's safe to read further — don't pipe it through a same-turn filter you just improvised.

## Writing to a secrets file: quote every value

The corruption behind incident #3 traced back to the secrets file itself, not to anything read from it: an existing entry's value contained an unquoted space, and unrelated multi-line reference content (a pasted config-file snippet with lines like `user=...`, `region=...`) had ended up appended to the file as if it were more `KEY=value` entries.  Neither is a hypothetical — sourcing that file (`. file`) executes it as shell code, and any unquoted value containing whitespace gets word-split: the shell treats everything after the space as a separate command, and if that command isn't found, the error message echoes the fragment back — which is exactly how content that was never printed anywhere leaks anyway.

When writing or appending to a `KEY=value`-style secrets file: always wrap the value in double quotes (`KEY="value"`), escaping any literal `"` or `\` inside it first.  This is safe and non-breaking — sourcing strips one layer of surrounding quotes regardless, so the variable ends up holding the identical value either way — and it closes off this entire failure class for values not yet known to contain anything special.  Never paste unrelated multi-line reference content (a config file copied in "for context," documentation, examples) into a file meant to be sourced — if it isn't a credential meant to be loaded as an env var, it doesn't belong in that file, full stop.

## If a secret leaks anyway

Safeguards aren't a guarantee, and the response afterward matters as much as the prevention:

1. Stop.  Don't take another action first, and don't keep going "just to finish this one thing."
2. Name exactly what leaked — the specific credential(s) involved, unmasked (see above) — not a vague "something may have been exposed."
3. Tell the owner to rotate it.  Don't minimize it, don't wait to be asked, don't bury it in a longer message about something else.
4. **Delete it immediately from every location where deletion is actually possible** — this is required in addition to rotation, not instead of it (owner directive, 2026-08-11):
   - A file you wrote (scratch/temp file, generated config, downloaded log) that now holds the plaintext value: delete it the moment it's no longer needed.  Don't leave it sitting around "just in case."
   - A message you sent to a platform that supports edit/delete (a Zulip message, a GitHub PR/issue comment, a draft email): delete or edit it out immediately via that platform's own API, same turn you notice.
   - Git history (an already-pushed commit/PR diff): do NOT unilaterally force-push or rewrite history to scrub it — that's destructive on shared state.  Flag it to the owner as a required follow-up instead.
   - The live chat transcript itself: you have **no capability to delete or edit a prior turn** in this conversation.  Say so plainly if it comes up — don't imply retroactive scrubbing is possible when it isn't.  The correct response to a transcript-level leak is rotation + never re-displaying the value again, not a promise to make the old turn disappear.
5. Identify the specific technique that caused the leak and change it before doing anything similar again — not just a general resolve to "be more careful."

This is a session-authored propagation of the owner's own words — other platforms should apply the same underlying principle (rotate + delete what you genuinely can + be honest about what you can't) via their own mechanics, not assume identical tool/API names.  Canonical fleet-wide copy: `/Users/jay/apps/FLEET-PROTOCOL.md` § "If a secret leaks anyway: delete it immediately everywhere deletion is possible."
