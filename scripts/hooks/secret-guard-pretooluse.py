#!/usr/bin/env python3
"""PreToolUse hook for Bash: hard-enforced backstop under the secret-safety skill.

Reads the hook JSON on stdin, checks tool_input.command against five concrete
patterns that caused real secret exposures:
  A) direct dump of a known secret-bearing file (cat/head/tail/less/more/bat
     and byte-dump tools od/xxd/hexdump/hd/strings/base64)
  B) a Bearer/Authorization value embedded in args with no redaction pipe present
  C) any `ps` invocation at all
  C2) `pgrep -l` / `-fl` / `-lf` (prints argv like ps)
  D) a command that directly references a secrets-bearing path AND merges
     stderr into visible output (2>&1)
  E) byte-dump / prefix-cut / last-command printf of a variable whose name
     looks like a key (KEY, TOKEN, SECRET, PASSWORD, PASSWD, DSN)

All five are hard denies. C started as an "auto-filter the dangerous part, then
allow" rewrite (append `| grep -v -- --mcp-config` to exclude this harness's own
argv-embedded credentials), but that was proven wrong THE SAME NIGHT it was
written: the harness's own argv is one exposure source among several running
MCP-server child processes that ALSO carry credentials directly in their argv
(e.g. `mcp-remote <url> --header "Authorization: Bearer <token>"`), and no
single exclusion pattern covers all of them. A filter that misses a case is
worse than no filter, because it creates false confidence that the output was
made safe. `ps` is denied outright; `pgrep`/`pgrep -f`/`pgrep -P` return PIDs
only and cannot leak argv under any flag combination, so they're the correct
tool whenever the actual need is "does this process exist" / "what is its
parent" rather than "show me its full command line."

D exists because of a third incident the same night: after a secret had already
leaked once, the fix attempt was a hand-written `sed` filter for a shell error
message, built reactively under pressure -- and it was written for bash's error
format while the actual shell was zsh, which formats the same error differently.
The filter matched nothing and the same fragment printed again, twice more. The
lesson generalizes past that one regex: capturing stderr (2>&1) from anything
that touches a secrets file is inherently risky, because error messages can
echo back fragments of whatever failed in a shape you can't predict in advance
(a shell's own "command not found: <fragment>" being the exact case here). The
safe pattern is to redirect stderr to /dev/null or a file and check the exit
code alone, never to capture-and-filter it in the same command.

E exists because of 2026-09-17: a subagent loaded SILICONFLOW_API_KEY from the
handoff file (since retired, OPS-818) correctly (quote-stripped, never echoed), then ran `od -c` on the
variable to look for stray quote characters.  `od -c` prints every byte, so the
full key landed in the transcript.  The hook receives the command TEMPLATE
before expansion, so it cannot see the runtime value -- it pattern-matches
structure: a byte-dump tool (od/xxd/hexdump/hd/strings/base64) or `cut -c` in
the same pipeline as `$NAME` / `${NAME}` where NAME looks like a key, or a
last-command `printf %s/%q/%b` of that variable with nothing downstream.  Safe
shape checks stay allowed: `[ -n "$VAR" ]`, `${#VAR}`, `[[ $VAR == *'"'* ]]`.

This is a backstop, not a general secret detector. It cannot see runtime-expanded
shell variable values (the hook receives the command TEMPLATE before execution),
so it can only pattern-match on command STRUCTURE. It does not replace the
secret-safety skill; it enforces the specific failure classes that already
happened.

Tracked copy: AI-Fleet-Coordinator `scripts/hooks/secret-guard-pretooluse.py`.
Live Claude Code copy: `~/.claude/hooks/secret-guard-pretooluse.py` (registered
in `~/.claude/settings.json` PreToolUse matcher Bash).
"""
from __future__ import annotations

import json
import re
import sys

SECRET_FILE_PATTERNS = [
    r"\.secrets/",
    r"credentials\.json",
    r"client_info\.json",
    r"\.env(?!\.example)\b",
    r"id_rsa\b",
    r"\.pem\b",
    r"\.p12\b",
    r"\.key\b",
]

# A command word can also begin inside command substitution, a subshell, a backtick, or
# after a newline in a heredoc/multi-line script.  The old `(?:^|[;&|]\s*)` anchor missed
# all of those -- `$(ps -o comm= -p $p)` did not match, which is exactly how live Stripe /
# GitHub / UptimeRobot keys reached transcripts on 2026-09-01 despite this guard.
_CMD_START = r"(?:^|[;&|(\n`]|\$\()\s*"
# Same tool via /usr/bin/od, ./xxd, command -p cat, exec ps, \od (skip-alias).
# Bare-name-only matching let `/usr/bin/od -c <<< "$KEY"` and `/bin/cat ~/.secrets/`
# through after #261, which is the 2026-09-17 leak with a path prefix.
_INVOKE = (
    r"(?:(?:command|builtin|exec)\s+(?:--\s+|-[pv]+\s+)*)?"
    r"(?:/{0,1}(?:[\w.+-]+/)*)?"
    r"\\?"
)

# Byte-dump tools belong here too: `xxd ~/.secrets/global-api-keys` is cat for this purpose.
DUMP_COMMANDS = _CMD_START + _INVOKE + r"(cat|head|tail|less|more|bat|od|xxd|hexdump|hd|strings|base64)\s"

PS_PATTERN = _CMD_START + _INVOKE + r"ps\s"

# `pgrep -l` / `-fl` / `-lf` print the full command line just like ps, so "don't run bare
# ps" was never sufficient.  pids-only forms (`pgrep -f PATTERN`, `pgrep -x`, `pgrep -c`)
# stay allowed -- only the listing flags are caught.
PGREP_LIST_PATTERN = _CMD_START + _INVOKE + r"pgrep\s+(?:-[a-zA-Z]*l[a-zA-Z]*)(?:\s|$)"

BYTE_DUMP_TOOLS = re.compile(
    _CMD_START + _INVOKE + r"(?:od|xxd|hexdump|hd|strings|base64)\b"
)
CUT_CHARS = re.compile(
    _CMD_START + _INVOKE + r"cut\s+(?:-[^\s]*c[^\s]*|--characters(?:=|\s))"
)
PRINTF_FORMAT = re.compile(
    _CMD_START + _INVOKE + r"printf\s+(?:--\s+)?"
    r"(?:(['\"]).*?%[-+#0-9.]*[sqb].*?\1|%[-+#0-9.]*[sqb])"
)
# $NAME or ${NAME...} but not ${#NAME} (length, safe).
VAR_REF = re.compile(
    r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{(?!#)([A-Za-z_][A-Za-z0-9_]*)[^}]*\})"
)
_SECRET_PARTS = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "DSN",
        "APIKEY",
        "API_KEY",
    }
)
_SECRET_SUFFIXES = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "DSN",
)


def ident_is_secret(name: str) -> bool:
    """True when NAME looks like a credential env var, not KEYBOARD/TOKENIZER."""
    up = name.upper()
    if up.endswith(_SECRET_SUFFIXES):
        return True
    return any(part in _SECRET_PARTS for part in up.split("_"))


def secret_var_names(text: str) -> list[str]:
    names = []
    for m in VAR_REF.finditer(text):
        name = m.group(1) or m.group(2)
        if name and ident_is_secret(name):
            names.append(name)
    return names


def command_units(command: str) -> list[str]:
    """Split on `;` / `&&` / `||` / newlines; keep `|` pipelines together."""
    return [u.strip() for u in re.split(r"(?:&&|\|\||\n|;)+", command) if u.strip()]


def last_pipeline_cmd(unit: str) -> str:
    parts = re.split(r"(?<!\|)\|(?!\|)", unit)
    return parts[-1].strip() if parts else unit


def extract_command(payload: dict) -> str:
    tool_name = payload.get("tool_name") or payload.get("tool") or ""
    if tool_name and tool_name not in ("Bash", "bash", "Shell", "shell"):
        return ""
    tool_input = payload.get("tool_input") or {}
    return tool_input.get("command") or payload.get("command") or ""


def check_command(command: str) -> str | None:
    """Return a deny reason, or None to allow."""
    if not command:
        return None

    # Rule A: direct dump of a known secret-bearing file. Checked per pipeline
    # SEGMENT, not the whole command string -- otherwise a safe command that
    # merely ends in an unrelated `| head -5` (truncating already-safe output)
    # would false-positive just because "head" and ".secrets/" both appear
    # somewhere in the string, even in different segments.
    for segment in re.split(r"[;&|]{1,2}", command):
        segment = segment.strip()
        if re.search(DUMP_COMMANDS, segment) or CUT_CHARS.search(segment):
            for pat in SECRET_FILE_PATTERNS:
                if re.search(pat, segment):
                    return (
                        "This command directly dumps a file that may contain secrets "
                        f"(matched pattern: {pat}). Per the secret-safety skill: extract "
                        "the ONE value you need into a variable (e.g. `grep -m1 "
                        "'^NAME=' file | cut -d= -f2-`), never dump the whole file. "
                        "Listing just the key NAMES (grep -oE '^[A-Z_]+=' file) is fine. "
                        "Byte-dump tools (od/xxd/hexdump/hd/strings/base64) and `cut -c` "
                        "are cat for this purpose."
                    )

    # Rule B: a Bearer/Authorization value embedded directly in args, no redaction
    # pipe present. This can't see the actual runtime value (hook receives the
    # command template pre-expansion) -- it flags the STRUCTURE: a header-style
    # credential argument with nothing downstream that would redact it.
    has_auth_arg = bool(re.search(r"Bearer\s|Authorization\s*:", command))
    has_redaction = "sed" in command or "REDACTED" in command
    if has_auth_arg and not has_redaction:
        return (
            "This command's arguments include an Authorization/Bearer value with "
            "no redaction pipe. Per the secret-safety skill: pipe stdout AND stderr "
            "through an inline sed substitution of the known variable's literal "
            "value before this command's output is returned -- many CLIs echo back "
            "the exact headers/args they were invoked with in their own logging."
        )

    # Rule C: any `ps` invocation. No filter is trusted -- ps output can carry
    # any running process's full argv, including credentials embedded by MCP
    # servers launched with e.g. `mcp-remote <url> --header "Authorization:
    # Bearer <token>"`, and this harness's own --mcp-config argv besides. There
    # is no single exclusion pattern that reliably covers every case.
    if re.search(PS_PATTERN, command):
        return (
            "`ps` can print any running process's full command line, including "
            "credentials some MCP servers embed directly in argv. There is no "
            "reliable filter for this -- use `pgrep -f <pattern>`, `pgrep -x "
            "<name>`, `pgrep -c`, or `pgrep -P <pid>` instead, which return PIDs "
            "only. NOTE: `pgrep -l` / `-fl` / `-lf` DO print full argv and are "
            "blocked separately. If you specifically need one process's command "
            "line and have already confirmed its PID via pgrep, that's a narrower, "
            "deliberate decision to make explicitly -- not something to default "
            "into via a broad ps."
        )

    # Rule C2: pgrep with a listing flag prints argv exactly like ps. The old
    # guidance ("use pgrep instead") was itself unsafe advice for these forms.
    if re.search(PGREP_LIST_PATTERN, command):
        return (
            "`pgrep -l` / `-fl` / `-lf` print each match's full command line, so "
            "they leak argv exactly like ps -- on 2026-09-01 live Stripe, GitHub "
            "and UptimeRobot keys were readable this way. Drop the `l`: use "
            "`pgrep -f <pattern>` for PIDs, or `pgrep -c -f <pattern>` to count."
        )

    # Rule D: directly references a secrets-bearing path AND merges stderr into
    # visible output (2>&1). This is what actually caused the third incident --
    # not the sourcing itself (safe and done constantly), but capturing what a
    # secrets-adjacent command prints on failure in the same breath as running it.
    has_secret_path = any(re.search(pat, command) for pat in SECRET_FILE_PATTERNS)
    has_stderr_merge = bool(re.search(r"2>&1", command))
    if has_secret_path and has_stderr_merge:
        return (
            "This command references a secrets-bearing path and merges stderr "
            "into visible output (2>&1) in the same breath. Error messages can "
            "echo back fragments of whatever failed in a shape you can't predict "
            "-- this is exactly how a shell's own \"command not found: <fragment>\" "
            "leaked a secret earlier. Redirect stderr to /dev/null or a file "
            "instead and check the exit code ($?) alone; if you need to know why "
            "something failed, diagnose structurally (line counts, regex-shape "
            "checks, whitespace/quote-balance booleans) rather than reading the "
            "raw error text."
        )

    # Rule E: byte-dump / prefix-cut of a loaded key variable, or last-command
    # printf %s/%q of one.  2026-09-17: `od -c` on SILICONFLOW_API_KEY printed
    # the live value into a subagent transcript.  Safe: [ -n "$VAR" ], ${#VAR},
    # [[ $VAR == *'"'* ]], printf piped into curl -K - / wc -c / sed redaction.
    for unit in command_units(command):
        names = secret_var_names(unit)
        if not names:
            continue
        if BYTE_DUMP_TOOLS.search(unit):
            shown = ", ".join(sorted(set(names)))
            return (
                "This command byte-dumps a variable whose name looks like a "
                f"credential ({shown}).  `od`/`xxd`/`hexdump`/`hd`/`strings`/"
                "`base64` print every byte, which is how SILICONFLOW_API_KEY "
                "landed in a transcript on 2026-09-17.  Do not inspect a loaded "
                "key's bytes.  Safe shape checks: `[ -n \"$VAR\" ]`, `${#VAR}`, "
                "`[[ $VAR == *'\"'* ]]`.  Need length only: `printf '%s' \"$VAR\" "
                "| wc -c`."
            )
        if CUT_CHARS.search(unit):
            shown = ", ".join(sorted(set(names)))
            return (
                "This command runs `cut -c` on a variable whose name looks like "
                f"a credential ({shown}).  Character cuts print a prefix of the "
                "live value.  Use `${#VAR}` or `[[ $VAR == *'\"'* ]]` instead."
            )
        last = last_pipeline_cmd(unit)
        if PRINTF_FORMAT.search(last) and secret_var_names(last):
            shown = ", ".join(sorted(set(secret_var_names(last))))
            return (
                "This command's last stage is `printf %s/%q` of a variable whose "
                f"name looks like a credential ({shown}), so the tool result is "
                "the live value.  Pipe it into the consumer (`curl -K -`) or a "
                "length check (`wc -c`); never let printf of a key be the output."
            )

    return None


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # malformed input: don't block on a hook bug

    command = extract_command(payload)
    if not command:
        sys.exit(0)

    reason = check_command(command)
    if reason:
        deny(reason)
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
