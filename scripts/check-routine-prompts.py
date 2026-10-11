#!/usr/bin/env python3
"""Lint the fleet's routine prompt texts (open-loops Phase 1a, unit RT0).

The routine texts are private (Simple-With-Us/fleet-loops-data, docs/routines/) and are pasted into Grok Bot
or POSTed to BotFleet.  This checker keeps a text safe to run unattended.  Rules:

  too-long          more than 20,000 characters (BotFleet `sanitizeInput` caps a routine prompt there;  counted in
                    UTF-16 code units, which is what its JavaScript `.length` counts)
  typed-mention     a hand-typed Zulip mention (at sign plus two asterisks, silent form included):  a typed display
                    name wakes nobody or the wrong one, so mentions are built at run time from the live user list
  code-path         a path under ~/Code:  that tree is the human's integration tree and is reset by a daemon
  slack-helper      the retired Slack helper or a Slack endpoint (Zulip replaced Slack on 2026-10-07)
  single-space-gap  a sentence boundary with one ASCII space (the fleet writes two spaces, or U+00A0 plus a space)
  reminder-missing  the owner's standing reminder is not quoted
  secret-shaped     a secret-shaped string (the agent-sync secret scanner, plus the F3 prefixes held locally)
  gb-label          a Grok Bot text (`gb-<role>.md`) without its own `[GB-<ROLE>]` label or `<role>-grok-bot@` email,
                    carrying another role's label or email, or a text that is not a GB text carrying any GB label
  placeholder       (only with --no-placeholders) a leftover `<C2 ID>`, `<C5 ID>`, TODO or FIXME
  private-dir       (only with --private) the folder or a file is readable by others, or sits inside a git worktree
  unreadable        a file that is not UTF-8

    python3 scripts/check-routine-prompts.py DIR_OR_FILE ...
    python3 scripts/check-routine-prompts.py --private --no-placeholders /Users/jay/apps/state/routines-private
    python3 scripts/check-routine-prompts.py --join gb-deployer.md open-loops-digest.md    # one pasted routine

A directory is checked file by file (`*.md`, `*.txt`).  Where it holds a Grok Bot block and its core (the pairs
below), the pasted routine, block then core, is checked for length too.  Exit 0 clean, 1 on any finding, 2 on a
usage error.  Findings print as `path:line: rule (message)` and never echo the text, so a secret never prints.

The module is importable:  `check_text(name, text, gb_role=None)` returns a list of `Finding`.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent

MAX_CHARS = 20_000
# Jay's words, verbatim (owner 2026-10-10).  Matched after collapsing whitespace, with or without the final period.
OWNER_REMINDER = ("I often distract away from things but never intend to undo what I said prior "
                  "unless explicitly say so")
# Which core each Grok Bot block is pasted with (the block comes first, then the core).
PAIRS = {
    "gb-deployer": "open-loops-digest",
    "gb-plumber": "backup-health",
    "gb-housekeeper": "weekly-landing-sweep",
}
JOIN_SEPARATOR = "\n\n---\n\n"
TEXT_SUFFIXES = (".md", ".txt")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int          # 0 = the whole file
    rule: str
    message: str

    def __str__(self) -> str:
        where = "%s:%d" % (self.path, self.line) if self.line else self.path
        return "%s: %s (%s)" % (where, self.rule, self.message)


# --- secret scanning ---------------------------------------------------------------------------------------------

def _load_secretscan() -> Callable[[str], str | None] | None:
    """`agent_sync.secretscan.scan`, loaded by path so the package's own imports never run.  None if absent."""
    path = HERE / "agent_sync" / "secretscan.py"
    try:
        spec = importlib.util.spec_from_file_location("_routine_check_secretscan", str(path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "scan", None)
    except Exception:  # noqa: BLE001 - a missing or broken scanner falls back to the local copy
        return None


# Local copy of the F3 prefixes (Linear, Sentry, xAI, Infisical, Cloudflare, Coolify).  Always applied:  once F3
# lands the same kinds are in agent_sync.secretscan and this list is a harmless duplicate.
_LOCAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Linear API key", re.compile(r"\blin_(api|oauth)_[A-Za-z0-9]{20,}")),
    ("Linear client secret", re.compile(r"(?i)\blinear[\w -]{0,24}secret\s*[=:]\s*['\"]?[A-Za-z0-9]{24,}")),
    ("Sentry token", re.compile(r"\bsntry[su]_[A-Za-z0-9+/=_-]{20,}")),
    ("xAI key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("Infisical service token", re.compile(r"\bst\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}")),
    ("Cloudflare token", re.compile(r"\bcf(ut|at|k)_[A-Za-z0-9]{30,}")),
    ("Cloudflare token", re.compile(r"(?i)\bcloudflare[\w ]{0,24}(token|key)\s*[=:]\s*['\"]?[A-Za-z0-9_-]{37,}")),
    ("Coolify token", re.compile(r"(?<![A-Za-z0-9])\d{1,5}\|[A-Za-z0-9]{36,}(?![A-Za-z0-9])")),
    ("assigned credential", re.compile(r"\b[A-Z][A-Z0-9_]*(SECRET|TOKEN|PASSWORD|API_?KEY)\s*=\s*['\"]?[A-Za-z0-9+/=._-]{16,}")),
]
# Only used when agent_sync.secretscan cannot be loaded (this script copied out of the repo).
_FALLBACK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic or OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("bearer token", re.compile(r"(?i)\bauthorization:\s*(basic|bearer)\s+[A-Za-z0-9+/=._-]{16,}")),
    ("assigned secret", re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?[A-Za-z0-9+/=._-]{16,}")),
]
_ZULIP_SHAPED = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")

_SCAN = _load_secretscan()


def _secret_kind(line: str) -> str | None:
    if _SCAN is not None:
        kind = _SCAN(line)
        if kind:
            return kind
    else:
        for label, pattern in _FALLBACK_PATTERNS:
            if pattern.search(line):
                return label
        for match in _ZULIP_SHAPED.finditer(line):
            token = match.group()
            if any(c.isupper() for c in token) and any(c.islower() for c in token) and any(c.isdigit() for c in token):
                return "a Zulip-shaped API key"
    for label, pattern in _LOCAL_PATTERNS:
        if pattern.search(line):
            return label
    return None


# --- prose masking -----------------------------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}[.)]\s+)?(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_URL = re.compile(r"https?://\S+")


def prose_lines(text: str) -> list[str]:
    """The lines of `text` with fenced blocks, inline code spans and URLs blanked out (line numbers preserved)."""
    out: list[str] = []
    fence = ""
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if fence:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = ""
            out.append("")
            continue
        if match:
            fence = match.group(1)
            out.append("")
            continue
        line = _INLINE_CODE.sub(lambda m: "x" * len(m.group(0)), line)
        line = _URL.sub(lambda m: "u" * len(m.group(0)), line)
        out.append(line)
    return out


# One ASCII space after a sentence end, then the next sentence starting with a capital letter.  Two ASCII spaces
# and U+00A0 plus a space both fail the `(?: )` + lookahead shape, so they never match.
_SINGLE_GAP = re.compile(r"""(?P<end>[.!?])(?P<close>["'”’)\]*_]*)[ ](?=["'“‘(\[*_]*[A-Z])""")
_NOT_A_BOUNDARY = {"e.g", "i.e", "vs", "cf", "approx", "mr", "mrs", "ms", "dr", "st", "no", "inc"}
_LIST_MARKER = re.compile(r"^\s*(?:[-*+>]\s+|#{1,6}\s+)*\d{1,3}$")     # "1." and "## 1." open a list item or heading


def single_space_gaps(text: str) -> list[int]:
    """Line numbers holding a sentence boundary written with one ASCII space."""
    found: list[int] = []
    for number, line in enumerate(prose_lines(text), 1):
        for match in _SINGLE_GAP.finditer(line):
            head = line[:match.start()]
            token = re.search(r"(\S+)$", head + match.group("end"))
            word = (token.group(1) if token else "").rstrip(".!?").lstrip("([\"'*_").lower()
            if match.group("end") == "." and (word in _NOT_A_BOUNDARY or (len(word) == 1 and word.isalpha())):
                continue            # e.g. / i.e. / an initial
            if match.group("end") == "." and _LIST_MARKER.match(head):
                continue            # "1. Decisions"
            found.append(number)
            break
    return found


# --- the rules ---------------------------------------------------------------------------------------------------

_MENTION = re.compile(r"@_?\*\*")
_CODE_PATH = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s`'\"]+)/Code\b")
_SLACK = re.compile(r"agent-sync-websocket|agent-sync-push|slack[-_]post|slack[-_]helper|hooks\.slack\.com"
                    r"|slack\.com/api", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"<C\d+ ID>|\bTODO\b|\bFIXME\b")
# `[GB-PLUMBER]` and the directed form `[GB-PLUMBER→BF-PLUMBER]`.
_GB_LABEL = re.compile(r"\[GB-([A-Za-z][A-Za-z0-9]*)(?:[→·][^\]\n]*)?\]")
_GB_EMAIL = re.compile(r"\b([a-z][a-z0-9]*)-grok-bot@")
_GB_NAME = re.compile(r"^gb-([a-z][a-z0-9]*)\.(?:md|txt)$")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def gb_role_for(name: str) -> str | None:
    match = _GB_NAME.match(os.path.basename(name))
    return match.group(1).upper() if match else None


def _lines_matching(text: str, pattern: re.Pattern[str]) -> list[int]:
    return [number for number, line in enumerate(text.split("\n"), 1) if pattern.search(line)]


def check_text(name: str, text: str, gb_role: str | None = None, *, max_chars: int = MAX_CHARS,
               placeholders: bool = False, length_only: bool = False) -> list[Finding]:
    """Every finding for one routine text.  `gb_role` (e.g. "DEPLOYER") is read from a `gb-<role>.md` name when
    not given.  `length_only` checks just the length, for a joined pair whose parts were already checked."""
    found: list[Finding] = []
    length = utf16_length(text)
    if length > max_chars:
        found.append(Finding(name, 0, "too-long", "%d characters, limit %d" % (length, max_chars)))
    if length_only:
        return found

    for number in _lines_matching(text, _MENTION):
        found.append(Finding(name, number, "typed-mention",
                             "build a mention from the live user list, never type one"))
    for number in _lines_matching(text, _CODE_PATH):
        found.append(Finding(name, number, "code-path", "a path under ~/Code, the human's integration tree"))
    for number in _lines_matching(text, _SLACK):
        found.append(Finding(name, number, "slack-helper", "the Slack helper is retired;  post through Zulip"))
    for number in single_space_gaps(text):
        found.append(Finding(name, number, "single-space-gap",
                             "two spaces, or U+00A0 and a space, between sentences"))

    collapsed = re.sub(r"\s+", " ", text.replace(" ", " "))
    if OWNER_REMINDER not in collapsed:
        found.append(Finding(name, 0, "reminder-missing", "quote the owner's standing reminder verbatim"))

    for number, line in enumerate(text.split("\n"), 1):
        kind = _secret_kind(line)
        if kind:
            found.append(Finding(name, number, "secret-shaped", kind))

    role = (gb_role or gb_role_for(name) or "").upper() or None
    labels = [(number, m.group(1).upper()) for number, line in enumerate(text.split("\n"), 1)
              for m in _GB_LABEL.finditer(line)]
    emails = [(number, m.group(1).lower()) for number, line in enumerate(text.split("\n"), 1)
              for m in _GB_EMAIL.finditer(line)]
    if role is None:
        for number, label in labels:
            found.append(Finding(name, number, "gb-label",
                                 "[GB-%s] in a text that is not a GB block (a core is shared with BotFleet)" % label))
    else:
        if role not in {label for _, label in labels}:
            found.append(Finding(name, 0, "gb-label", "no [GB-%s] label for its posts" % role))
        for number, label in labels:
            if label != role:
                found.append(Finding(name, number, "gb-label", "[GB-%s] label in the GB-%s text" % (label, role)))
        if role.lower() not in {e for _, e in emails}:
            found.append(Finding(name, 0, "gb-label", "no %s-grok-bot@ email for the GET /users/me check"
                                 % role.lower()))
        for number, email in emails:
            if email != role.lower():
                found.append(Finding(name, number, "gb-label", "%s-grok-bot@ in the GB-%s text" % (email, role)))

    if placeholders:
        for number in _lines_matching(text, _PLACEHOLDER):
            found.append(Finding(name, number, "placeholder", "a leftover placeholder"))
    return found


def check_private_dir(path: Path) -> list[Finding]:
    """The private folder must be 0700, hold files no one else can read, and sit outside every git worktree."""
    found: list[Finding] = []
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return [Finding(str(path), 0, "private-dir", "cannot stat the folder (%s)" % exc.__class__.__name__)]
    if mode & 0o077:
        found.append(Finding(str(path), 0, "private-dir", "mode %04o, expected 0700" % mode))
    for parent in [path.resolve(), *path.resolve().parents]:
        if (parent / ".git").exists():
            found.append(Finding(str(path), 0, "private-dir", "inside a git worktree (%s)" % parent.name))
            break
    for item in sorted(path.iterdir()):
        if item.is_file() and stat.S_IMODE(item.stat().st_mode) & 0o077:
            found.append(Finding(str(item), 0, "private-dir",
                                 "mode %04o, expected 0600" % stat.S_IMODE(item.stat().st_mode)))
    return found


# --- files and pairs ---------------------------------------------------------------------------------------------

def _read(path: Path) -> tuple[str | None, list[Finding]]:
    try:
        return path.read_text(encoding="utf-8"), []
    except UnicodeDecodeError:
        return None, [Finding(str(path), 0, "unreadable", "not UTF-8")]
    except OSError as exc:
        return None, [Finding(str(path), 0, "unreadable", exc.__class__.__name__)]


def text_files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)
    return [path]


def joined(texts: Iterable[str]) -> str:
    return JOIN_SEPARATOR.join(t.strip("\n") for t in texts)


def check_directory(directory: Path, *, max_chars: int, placeholders: bool) -> tuple[list[Finding], int, int]:
    """(findings, files checked, pairs checked)."""
    found: list[Finding] = []
    contents: dict[str, str] = {}
    files = text_files(directory)
    for path in files:
        text, problems = _read(path)
        found += problems
        if text is None:
            continue
        contents[path.stem] = text
        found += check_text(str(path), text, max_chars=max_chars, placeholders=placeholders)
    pairs = 0
    for block, core in PAIRS.items():
        if block in contents and core in contents:
            pairs += 1
            found += check_text("%s.md+%s.md" % (block, core), joined([contents[block], contents[core]]),
                                gb_role=block.split("-", 1)[1].upper(), max_chars=max_chars, length_only=True)
    return found, len(files), pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", help="directories or files of routine texts")
    parser.add_argument("--max-chars", type=int, default=MAX_CHARS, help="length limit (default %(default)s)")
    parser.add_argument("--no-placeholders", action="store_true",
                        help="also fail a leftover <C2 ID>, <C5 ID>, TODO or FIXME (the gate before copying texts out)")
    parser.add_argument("--private", action="store_true",
                        help="the paths are the private folder:  it must be 0700 and outside every git worktree")
    parser.add_argument("--join", action="store_true",
                        help="check the given files, in order, as one pasted routine (length only, plus each file)")
    args = parser.parse_args(argv)

    found: list[Finding] = []
    checked = 0
    pairs = 0
    paths = [Path(raw) for raw in args.paths]
    for path in paths:
        if not path.exists():
            print("%s: no such file or folder" % path, file=sys.stderr)
            return 2
    if args.join:
        files = [f for path in paths for f in text_files(path)]
        texts: list[str] = []
        for path in files:
            text, problems = _read(path)
            found += problems
            if text is None:
                continue
            texts.append(text)
            found += check_text(str(path), text, max_chars=args.max_chars, placeholders=args.no_placeholders)
        checked = len(files)
        if texts:
            role = next((gb_role_for(str(f)) for f in files if gb_role_for(str(f))), None)
            found += check_text(" + ".join(os.path.basename(str(f)) for f in files), joined(texts),
                                gb_role=role, max_chars=args.max_chars, length_only=True)
            pairs = 1
    else:
        for path in paths:
            if path.is_dir():
                more, count, pair_count = check_directory(path, max_chars=args.max_chars,
                                                          placeholders=args.no_placeholders)
                found += more
                checked += count
                pairs += pair_count
                if args.private:
                    found += check_private_dir(path)
            else:
                text, problems = _read(path)
                found += problems
                checked += 1
                if text is not None:
                    found += check_text(str(path), text, max_chars=args.max_chars,
                                        placeholders=args.no_placeholders)
    if checked == 0:
        print("no routine texts found", file=sys.stderr)
        return 2
    for finding in found:
        print(finding)
    if found:
        files_hit = len({f.path for f in found})
        print("%d finding(s) in %d file(s)" % (len(found), files_hit), file=sys.stderr)
        return 1
    print("routine texts ok: %d file(s), %d pasted pair(s), limit %d characters%s"
          % (checked, pairs, args.max_chars, ", private folder ok" if args.private else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
