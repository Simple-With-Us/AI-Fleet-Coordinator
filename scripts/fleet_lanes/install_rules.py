"""install_rules: put the Lane Map rule into each platform's own rules file.

Every platform reads its own user-level rules file at the start of a session.  This tool adds one
marker block to each of them, so every platform sees the Lane Map rule every session:

    <!-- fleet-lane-map:begin v1 -->
    ...rule text...
    <!-- fleet-lane-map:end -->

The owner approves live installs file by file, so the tool is read-only until you say `apply` and
name the platform.

    python3 -m fleet_lanes.install_rules plan [PLATFORM ...]      # read-only; default: all platforms
    python3 -m fleet_lanes.install_rules apply PLATFORM ...       # writes; names are required
    python3 -m fleet_lanes.install_rules verify PLATFORM ...      # exit 1 unless the block is current

Options: `--home PATH` (default: $HOME; an empty value is a usage error, never the real home, and PATH
must be an existing directory), `--create` (plan and apply: allow creating a file that does not exist
yet), `--i-own-this-file` (plan and apply: required for the owner's own ~/.claude/CLAUDE.md),
`--dry-run` (apply: same as plan).

Exit codes: 0 ok, 1 verify failed or a write failed, 2 refused (a guard said no, which includes a
current block that the platform would never read), 64 usage error.

What `apply` does, per platform:
  - refuses a symlink that does not resolve to a regular file inside --home, an unreadable or
    non-UTF-8 file, malformed markers, and a result over the platform's size cap
  - copies the original to `<file>.bak-lane-map-<UTC stamp>` (exclusive create, read back and
    compared) before touching it; a second backup in the same second gets a `-2` suffix
  - writes through a temp file in the same directory and renames it over the target, so a failure
    leaves the original intact
  - appends the block when absent and replaces it in place when present; bytes outside the block
    are never changed, and a second apply changes nothing and writes no backup

Lines elsewhere in a file that state the old flat-lane rule or advise checkouts in /tmp are only
REPORTED (the CONTRADICTIONS section of `plan`).  They are never edited.  `plan` never prints a line of
the owner's file: the diff shows the hunk headers, our own added lines and, for a refresh, the old
block's lines, and collapses every other line into a count; a contradiction shows its line number, its
kind and the matched text only (the path, the temp directory and checkout words, the legacy prefix).
Whatever is printed still goes through a credential redactor, as a second layer.

The Codex cap.  Codex reads the first 32 KiB of AGENTS.md.  The block counts only if it ENDS inside those
bytes: `verify` fails, and `apply` is refused (exit 2) instead of reporting `unchanged`, for a block that
ends beyond the cap.  A refresh of a block near the top of a longer file is allowed.

Python standard library only.  Tests: `cd scripts && python3 -m unittest fleet_lanes.tests.test_install_rules -v`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence, TextIO

from . import layout as L
from .doctor import redact_text as _redact_urls

__all__ = [
    "BLOCK_VERSION", "BEGIN_PREFIX", "END_LINE", "PLATFORMS", "Platform", "PlanEntry", "ApplyResult",
    "VerifyResult", "MarkerError", "Contradiction", "block_lines", "block_text", "compose",
    "find_block", "find_contradictions", "redact", "build_entry", "apply_platform",
    "verify_platform", "atomic_write", "main", "EXIT_OK", "EXIT_FAILED", "EXIT_REFUSED", "EXIT_USAGE",
]

EXIT_OK = 0
EXIT_FAILED = 1      # verify found no current block, or a write failed
EXIT_REFUSED = 2     # a guard refused (flag missing, symlink, size cap, markers, ...)
EXIT_USAGE = 64      # same as doctor: a bad command line can never read as a refusal

BLOCK_VERSION = 1
BEGIN_PREFIX = "<!-- fleet-lane-map:begin v"
END_LINE = "<!-- fleet-lane-map:end -->"
_BEGIN_RE = re.compile(r"^[ \t]*<!-- fleet-lane-map:begin v(?P<v>\d+) -->[ \t]*$")
_END_RE = re.compile(r"^[ \t]*<!-- fleet-lane-map:end -->[ \t]*$")

RULES_DIR = Path(__file__).resolve().parent / "rules"
TEMPLATE_FILES = {
    "full": "lane-map.full.md",
    "minimal": "lane-map.minimal.md",
    "cursor": "lane-map.cursor.mdc",
}
BODY_PLACEHOLDER = "{{LANE_MAP_BODY}}"

CODEX_CAP = 32 * 1024     # Codex reads at most 32 KiB of AGENTS.md
CODEX_WARN = 30 * 1024
BACKUP_TAG = ".bak-lane-map-"
MAX_BACKUP_SUFFIX = 99


# --------------------------------------------------------------------------- platforms

@dataclass(frozen=True)
class Platform:
    """One platform's rules file.  `rel_path` is relative to the home; None means no known file."""

    name: str
    rel_path: str | None
    variant: str | None            # key of TEMPLATE_FILES
    label: str = ""
    owner_file: bool = False       # the owner's own file: plan and apply need --i-own-this-file
    own_file: bool = False         # a file of this tool's own (the whole file is ours)
    size_cap: int | None = None    # apply refuses a result over this many bytes
    size_warn: int | None = None   # plan warns above this many bytes
    note: str = ""

    @property
    def root_dir(self) -> str:
        """First path component below the home (".cursor"); it must exist before --create works."""
        return (self.rel_path or "").split("/", 1)[0]


PLATFORMS: tuple[Platform, ...] = (
    Platform("claude", ".claude/CLAUDE.md", "full", "Claude Code (CLI and desktop)", owner_file=True),
    Platform("codex", ".codex/AGENTS.md", "full", "Codex", size_cap=CODEX_CAP, size_warn=CODEX_WARN),
    Platform("fx", ".fx/AGENTS.md", "full", "Fx"),
    Platform("grok", ".grok/GROK.md", "full", "Grok and Grok Build"),
    Platform("antigravity", ".gemini/config/AGENTS.md", "full", "Antigravity"),
    Platform("minimax", ".minimax/memory/user.md", "minimal", "MiniMax (user.md goes into every prompt)"),
    Platform("cursor", ".cursor/rules/fleet-lane-map.mdc", "cursor", "Cursor", own_file=True),
    Platform("muse-code", None, None, "Muse Code",
             note="no known user-level rules file (UNVERIFIED); carry the rule in each project's "
                  "AGENTS.md instead (docs/protocols/lane-map.md, platform table)"),
)
PLATFORM_BY_NAME = {p.name: p for p in PLATFORMS}
PLATFORM_ALIASES = {"ag": "antigravity", "gemini": "antigravity", "mm": "minimax", "grok-build": "grok"}


def resolve_platform_names(names: Iterable[str]) -> list[Platform]:
    """Names to platforms, in the order given, duplicates dropped.  Raises KeyError(name) when unknown."""
    out: list[Platform] = []
    for raw in names:
        key = PLATFORM_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if key not in PLATFORM_BY_NAME:
            raise KeyError(raw)
        plat = PLATFORM_BY_NAME[key]
        if plat not in out:
            out.append(plat)
    return out


# --------------------------------------------------------------------------- templates

def _read_template(variant: str) -> str:
    path = RULES_DIR / TEMPLATE_FILES[variant]
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    """(frontmatter lines including both `---` fences, rest).  No frontmatter gives ([], text)."""
    if not text.startswith("---\n"):
        return [], text
    close = text.find("\n---\n", 3)
    if close < 0:
        return [], text
    return text[:close + 4].split("\n"), text[close + 5:]


def frontmatter_lines(variant: str) -> list[str]:
    """Lines that must sit at the top of the file, outside the marker block (Cursor .mdc only)."""
    fm, _ = _split_frontmatter(_read_template(variant))
    return fm


def body_text(variant: str) -> str:
    """The rule text of a variant, without markers and without surrounding blank lines."""
    text = _read_template(variant)
    _, rest = _split_frontmatter(text)
    if variant == "cursor":
        full = _split_frontmatter(_read_template("full"))[1]
        rest = rest.replace(BODY_PLACEHOLDER, full.strip("\n"))
    return rest.strip("\n")


def block_lines(variant: str) -> list[str]:
    """The marker block as lines (no line endings)."""
    return [f"{BEGIN_PREFIX}{BLOCK_VERSION} -->"] + body_text(variant).split("\n") + [END_LINE]


def block_text(variant: str) -> str:
    return "\n".join(block_lines(variant)) + "\n"


# --------------------------------------------------------------------------- the marker block

class MarkerError(ValueError):
    """The markers in a file cannot be trusted: the tool refuses rather than guess."""


@dataclass(frozen=True)
class BlockLoc:
    begin: int      # index into text.split("\n") of the begin marker line
    end: int        # index of the end marker line
    version: int


def find_block(text: str) -> BlockLoc | None:
    """Locate the one marker block.  None when there is none.  Raises MarkerError when there is a
    begin without an end (or the reverse), an end before its begin, or more than one block."""
    begins: list[tuple[int, int]] = []
    ends: list[int] = []
    for i, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\r")
        m = _BEGIN_RE.match(line)
        if m:
            begins.append((i, int(m.group("v"))))
        elif _END_RE.match(line):
            ends.append(i)
    if not begins and not ends:
        return None
    if len(begins) > 1 or len(ends) > 1:
        raise MarkerError(f"found {len(begins)} begin and {len(ends)} end markers; expected one block")
    if not begins:
        raise MarkerError(f"end marker on line {ends[0] + 1} has no begin marker")
    if not ends:
        raise MarkerError(f"begin marker on line {begins[0][0] + 1} has no end marker")
    (bi, ver), ei = begins[0], ends[0]
    if ei < bi:
        raise MarkerError(f"end marker (line {ei + 1}) comes before the begin marker (line {bi + 1})")
    return BlockLoc(bi, ei, ver)


@dataclass(frozen=True)
class Composition:
    new_text: str
    action: str                  # create | append | replace | none
    loc: BlockLoc | None         # the block in the CURRENT text, when there was one


def compose(current: str | None, variant: str) -> Composition:
    """Return the text a platform's file should hold.  `current` None means the file does not exist.
    Raises MarkerError for bad markers and for a block written by a newer version of this tool."""
    block = block_lines(variant)
    fm = frontmatter_lines(variant)
    if current is None or not current.strip():
        lines = (fm + [""] if fm else []) + block
        return Composition("\n".join(lines) + "\n", "create" if current is None else "append", None)

    crlf = "\r\n" in current
    eol = "\r\n" if crlf else "\n"
    loc = find_block(current)
    if loc is None:
        base = current
        if not base.endswith("\n"):
            base += eol
        if not base.endswith("\n\n") and not base.endswith("\n\r\n"):
            base += eol
        return Composition(base + eol.join(block) + eol, "append", None)

    if loc.version > BLOCK_VERSION:
        raise MarkerError(f"the block is version {loc.version}, newer than this tool's version {BLOCK_VERSION}")
    lines = current.split("\n")
    old = [ln.rstrip("\r") for ln in lines[loc.begin:loc.end + 1]]
    if old == block:
        return Composition(current, "none", loc)
    cr = "\r" if crlf else ""
    new_block = [ln + cr for ln in block]
    if loc.end == len(lines) - 1 and cr:
        new_block[-1] = block[-1]       # the file had no newline after the end marker; keep it that way
    return Composition("\n".join(lines[:loc.begin] + new_block + lines[loc.end + 1:]), "replace", loc)


def frontmatter_problem(text: str) -> str | None:
    """Why a Cursor rule file would not load every session, or None when its frontmatter is right."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return "the file does not start with a --- frontmatter fence"
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r") == "---"), None)
    if end is None:
        return "the frontmatter has no closing --- fence"
    head = [ln.rstrip("\r") for ln in lines[1:end]]
    if "alwaysApply: true" not in [ln.strip() for ln in head]:
        return "frontmatter lacks `alwaysApply: true`, so Cursor would not load the rule every session"
    if not any(ln.startswith("description:") and ln[len("description:"):].strip() for ln in head):
        return "frontmatter lacks a description"
    loc = find_block(text)
    if loc is not None and loc.begin <= end:
        return "the marker block sits inside the frontmatter"
    return None


# --------------------------------------------------------------------------- credentials

_SECRET_KV = re.compile(
    r"(?i)(\b[\w-]*(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key|credential)s?[\w-]*)"
    r"([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_TOKEN_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{8,}|pk_(?:live|test)_[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|A[CK][0-9a-f]{32}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}|Bearer\s+[A-Za-z0-9._~+/=-]{16,})")
_LONG_TOKEN = re.compile(r"(?<![\w/.~-])[A-Za-z0-9_-]{40,}(?![\w/.~-])")
_AWS_SECRET = re.compile(r"(?<![\w/.~+%-])[A-Za-z0-9/+]{40}(?![\w/.~+%-])")


def _mask_aws(m: "re.Match[str]") -> str:
    """40 characters of the base64 alphabet that mix digits and both cases: an AWS secret access key (it
    can contain `/` and `+`, which the plain long-token rule cannot see).  Ordinary paths fail a test."""
    t = m.group(0)
    secret = re.search(r"\d", t) and re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and t.count("/") <= 3
    return "[REDACTED]" if secret else t


def redact(text: str) -> str:
    """Replace anything shaped like a credential.  A second layer: `plan` prints almost nothing from a
    rules file in the first place.  URL userinfo goes through the doctor's redactor."""
    out = _redact_urls(text)
    out = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    out = _TOKEN_SHAPES.sub("[REDACTED]", out)
    out = _AWS_SECRET.sub(_mask_aws, out)
    return _LONG_TOKEN.sub(
        lambda m: "[REDACTED]" if re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0))
        else m.group(0), out)


def _snippet(tokens: Iterable[str], width: int = 110) -> str:
    """The matched text of a contradiction, never the line around it: a rules file holds the owner's own
    notes, and `plan` must not echo them.  Redacted and shortened as a second layer."""
    seen: list[str] = []
    for t in tokens:
        t = t.strip().rstrip(".,;:)`'\"")
        if t and t not in seen:
            seen.append(t)
    s = redact(", ".join(seen))
    return s if len(s) <= width else s[:width - 3] + "..."


# --------------------------------------------------------------------------- contradictions

@dataclass(frozen=True)
class Contradiction:
    line: int          # 1-based line number in the file
    kind: str          # flat-lane | tmp-checkout | legacy-agent-prefix-mention
    snippet: str       # the matched text only (not the line), redacted and shortened


_BUILTIN_PREFIXES = (
    "trading", "congress", "usage", "cts", "dealdex", "fleet", "personal", "autorotate", "contactlogo",
    "botfleet", "hoghunter", "fleet-ops", "clutch", "codecaps", "fleetlink", "simple-with-us",
    "kodus-config",
)
_APPS_PATH = re.compile(
    r"(?<![\w.-])(?:~|\$\{?HOME\}?|/Users/[\w.-]+|/home/[\w.-]+)?/apps/(?P<name>[A-Za-z0-9][\w.<>-]*)")
_FLAT_PLACEHOLDER = re.compile(r"<(?:prefix|app)>-<(?:seat|agent)>")
_TMP_TOKEN = re.compile(r"(?:/private)?/tmp\b|/var/tmp\b|\$\{?TMPDIR\}?|/var/folders\b|\bmktemp\b")
_CHECKOUT_WORD = re.compile(
    r"\b(?:git\s+clone|git\s+worktree|git\s+init|clone[sd]?|cloning|worktrees?|checkouts?)\b", re.I)
_NEGATION = re.compile(
    r"\b(?:never|don't|dont|do\s+not|not|no|forbidden|forbid|ban|banned|avoid|must\s+not|cannot|can't)\b", re.I)
_AGENT_PREFIX = re.compile(r"(?<![\w/.~$@-])agent/[A-Za-z][\w.-]*")


def default_registry() -> L.Registry | None:
    """The repo's fleet-apps.json, or None when it cannot be read (the built-in names still apply)."""
    try:
        return L.load_registry()
    except (OSError, ValueError):
        return None


def _vocab(registry: L.Registry | None) -> tuple[list[str], list[str]]:
    prefixes = set(_BUILTIN_PREFIXES)
    if registry is not None:
        for app in registry.apps:
            prefixes.add(app.prefix.lower())
            prefixes.update(a.lower() for a in app.prefix_aliases())
    seats = set(L.canonical_seat_set(registry)) | set(L.seat_alias_map(registry))
    return sorted(prefixes, key=lambda s: (-len(s), s)), sorted(seats, key=lambda s: (-len(s), s))


def _is_flat_lane_name(name: str, prefixes: Sequence[str], seats: Sequence[str]) -> bool:
    lower = name.lower().rstrip(".,;:)`'\"")
    for p in prefixes:
        if lower.startswith(p + "-"):
            rest = lower[len(p) + 1:]
            if any(rest == s or rest.startswith(s + "-") for s in seats):
                return True
    return False


def find_contradictions(text: str, *, registry: L.Registry | None = None,
                        skip: BlockLoc | None = None) -> list[Contradiction]:
    """Lines that state the old flat-lane rule, advise a checkout in a temp directory, or mention a
    legacy `agent/` branch prefix.  Lines inside our own marker block (`skip`) are never reported.
    Report only: nothing here is ever edited."""
    prefixes, seats = _vocab(registry)
    found: list[Contradiction] = []
    for i, line in enumerate(text.split("\n")):
        if skip is not None and skip.begin <= i <= skip.end:
            continue
        line = line.rstrip("\r")
        kinds: list[tuple[str, list[str]]] = []
        flat = [m.group(0) for m in _FLAT_PLACEHOLDER.finditer(line)] + [
            m.group(0) for m in _APPS_PATH.finditer(line) if _is_flat_lane_name(m.group("name"), prefixes, seats)]
        if flat:
            kinds.append(("flat-lane", flat))
        tmp, word = _TMP_TOKEN.search(line), _CHECKOUT_WORD.search(line)
        if tmp and word and not _NEGATION.search(line):
            kinds.append(("tmp-checkout", [f"{tmp.group(0)} + {word.group(0)}"]))
        legacy = [m.group(0) for m in _AGENT_PREFIX.finditer(line)]
        if legacy:
            kinds.append(("legacy-agent-prefix-mention", legacy))
        for kind, tokens in kinds:
            found.append(Contradiction(i + 1, kind, _snippet(tokens)))
    return found


# --------------------------------------------------------------------------- inspecting a file

@dataclass
class FileState:
    platform: Platform
    path: Path                       # home/rel_path as named
    real: str                        # path after resolving symlinks
    exists: bool
    via_link: bool = False           # the path, or a directory above it, is a symlink
    size: int = 0
    mode: int = 0o644
    raw: bytes | None = None
    text: str | None = None
    refusals: list[str] = field(default_factory=list)    # guard reasons: nothing may be read or written


def _within(child: str, root: str) -> bool:
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def inspect_file(platform: Platform, home: str | os.PathLike[str]) -> FileState:
    """Look at a platform's file without changing anything.  Never raises: a problem becomes a
    refusal.  A file that fails a guard is not read, so a symlink cannot make the tool print
    somebody else's file."""
    assert platform.rel_path is not None
    home_real = os.path.realpath(home)
    path = Path(os.path.abspath(home)) / platform.rel_path
    real = os.path.realpath(path)
    expected = os.path.join(home_real, platform.rel_path)
    is_link = os.path.islink(path)
    via_link = is_link or real != expected
    st = FileState(platform, path, real, exists=os.path.lexists(path), via_link=via_link)
    if via_link and not _within(real, home_real):
        st.refusals.append(f"{path} resolves to {real}, outside {home_real}; not following it")
        return st
    if st.exists:
        try:
            info = os.stat(path)
        except OSError as exc:
            st.refusals.append(f"{path} is a symlink that cannot be followed ({exc.strerror or exc})")
            return st
        if not stat.S_ISREG(info.st_mode):
            st.refusals.append(f"{real} is not a regular file")
            return st
        st.size = info.st_size
        st.mode = stat.S_IMODE(info.st_mode)
        try:
            raw = Path(real).read_bytes()
        except OSError as exc:
            st.refusals.append(f"{path} is unreadable ({exc.strerror or exc})")
            return st
        if b"\x00" in raw:
            st.refusals.append(f"{path} contains NUL bytes; not a text file")
            return st
        try:
            st.text = raw.decode("utf-8")
        except UnicodeDecodeError:
            st.refusals.append(f"{path} is not valid UTF-8")
            return st
        st.raw = raw
    return st


# --------------------------------------------------------------------------- planning

@dataclass
class PlanEntry:
    platform: Platform
    status: str                          # ok | refused | needs-flag | unsupported
    state: FileState | None = None
    action: str = "none"                 # create | append | replace | none
    block: str = "n/a"                   # absent | current | stale | malformed | file missing
    new_text: str | None = None
    refusals: list[str] = field(default_factory=list)    # why `apply` would refuse, given the flags
    warnings: list[str] = field(default_factory=list)
    diff: list[str] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)

    @property
    def will_change(self) -> bool:
        return self.status == "ok" and self.action != "none"


def _display(path: Path) -> str:
    return str(path)


def _diff(old: str | None, new: str, name: str) -> list[str]:
    """Unified diff of the whole file; `name` is the home-relative path (a/.codex/AGENTS.md).  Only the
    added and removed lines are shown (our block, or the old block being replaced).  Every unchanged
    line is the owner's own text, so a run of them becomes one `(N unchanged lines not shown)` line;
    the lines that stay are redacted anyway, as a second layer."""
    old_lines = [] if old is None else old.splitlines()
    raw = list(difflib.unified_diff(
        old_lines, new.splitlines(), fromfile="/dev/null" if old is None else f"a/{name}",
        tofile=f"b/{name}", lineterm="", n=3))
    out = raw[:2]
    hidden = 0

    def flush() -> None:
        nonlocal hidden
        if hidden:
            out.append(f"   ({hidden} unchanged line{'' if hidden == 1 else 's'} not shown)")
            hidden = 0

    for ln in raw[2:]:
        if ln.startswith("@@"):
            flush()
            out.append(ln)
        elif ln.startswith(("+", "-")):
            flush()
            out.append(redact(ln))
        else:
            hidden += 1
    flush()
    return out


def block_end_offset(text: str, loc: BlockLoc) -> int:
    """How many bytes of `text` (UTF-8) a reader that stops at a byte cap must read to see the whole block:
    everything through the last character of the end marker.  Bytes, not characters, and a CRLF counts 2."""
    lines = text.split("\n")
    head = "\n".join(lines[:loc.end]) + ("\n" if loc.end else "") + lines[loc.end].rstrip("\r")
    return len(head.encode("utf-8"))


def _beyond_cap(text: str, platform: Platform) -> int | None:
    """The byte offset at which the block ends when that is past the platform's cap, else None."""
    if platform.size_cap is None:
        return None
    loc = find_block(text)
    if loc is None:
        return None
    end = block_end_offset(text, loc)
    return end if end > platform.size_cap else None


def build_entry(platform: Platform, home: str | os.PathLike[str], *, create: bool = False,
                own_flag: bool = False, registry: L.Registry | None = None,
                with_contradictions: bool = True) -> PlanEntry:
    """Work out, without touching the disk, what `apply` would do to one platform."""
    if platform.rel_path is None or platform.variant is None:
        return PlanEntry(platform, "unsupported", refusals=[f"{platform.name} is unsupported: {platform.note}"])
    if platform.owner_file and not own_flag:
        return PlanEntry(platform, "needs-flag", refusals=[
            f"{platform.rel_path} is the owner's own file; plan and apply need --i-own-this-file"])
    state = inspect_file(platform, home)
    entry = PlanEntry(platform, "ok", state=state)
    if state.refusals:
        entry.status = "refused"
        entry.refusals = list(state.refusals)
        return entry

    if not state.exists:
        entry.block = "file missing"
        if not create:
            entry.refusals.append(f"{state.path} does not exist; pass --create to create it")
        if not os.path.isdir(os.path.join(os.path.realpath(home), platform.root_dir)):
            entry.refusals.append(
                f"~/{platform.root_dir} does not exist, so {platform.name} does not look installed; "
                "refusing to create it")
    try:
        comp = compose(state.text, platform.variant)
    except MarkerError as exc:
        entry.status = "refused"
        entry.block = "malformed"
        entry.refusals.append(f"{state.path}: {exc}")
        return entry

    entry.action, entry.new_text = comp.action, comp.new_text
    if state.exists:
        entry.block = {"none": "current", "replace": "stale"}.get(comp.action, "absent")
    new_size = len(comp.new_text.encode("utf-8"))
    cap, warn = platform.size_cap, platform.size_warn
    past = _beyond_cap(comp.new_text, platform)
    if past is not None and comp.action == "none":
        entry.refusals.append(
            f"the Lane Map block is current but ends at byte {past}, beyond the {cap}-byte cap {platform.name} "
            f"reads, so {platform.name} never sees it; move the block above that point by hand")
    elif past is not None:
        entry.refusals.append(
            f"the Lane Map block would end at byte {past}, beyond the {cap}-byte cap {platform.name} reads")
    if warn is not None and new_size > warn:
        entry.warnings.append(
            f"{new_size} bytes is above {warn} bytes ({warn // 1024} KiB); {platform.name} reads at most "
            f"{(cap or warn) // 1024} KiB, so text near the end may be cut off")
    if platform.variant == "cursor":
        problem = frontmatter_problem(comp.new_text)
        if problem:
            entry.warnings.append(problem)
    if comp.action != "none" and state.exists:
        if not state.mode & 0o200:
            entry.refusals.append(
                f"{state.path} is read-only (mode {state.mode:04o}); not overriding the owner's choice")
        if not os.access(os.path.dirname(state.real), os.W_OK | os.X_OK):
            entry.refusals.append(f"the directory {os.path.dirname(state.real)} is not writable")
    if comp.action != "none":
        entry.diff = _diff(state.text, comp.new_text, platform.rel_path)
    if with_contradictions and state.text:
        entry.contradictions = find_contradictions(state.text, registry=registry, skip=comp.loc)
    if entry.refusals:
        entry.status = "refused"
    return entry


# --------------------------------------------------------------------------- writing

def atomic_write(target: str | os.PathLike[str], data: bytes, mode: int) -> None:
    """Write `data` to a temp file in the target's directory, then rename it over the target.  A
    failure at any point leaves the target as it was and removes the temp file."""
    target = os.fspath(target)
    directory = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(target) + ".",
                               suffix=".lane-map.tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    try:                                # make the rename durable; best effort
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _make_backup(real: str, raw: bytes, mode: int, stamp: str) -> str:
    """Exclusive-create `<file>.bak-lane-map-<stamp>` holding `raw`, verified by reading it back."""
    base = f"{real}{BACKUP_TAG}{stamp}"
    for n in range(1, MAX_BACKUP_SUFFIX + 1):
        candidate = base if n == 1 else f"{base}-{n}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(candidate, mode)
            if Path(candidate).read_bytes() != raw:
                raise OSError("backup did not read back identical")
        except BaseException:
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
            raise
        return candidate
    raise OSError(f"{MAX_BACKUP_SUFFIX} backups with stamp {stamp} already exist")


@dataclass
class ApplyResult:
    platform: Platform
    status: str                    # changed | created | unchanged | refused | failed
    path: str = ""
    backup: str | None = None
    messages: list[str] = field(default_factory=list)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def apply_platform(platform: Platform, home: str | os.PathLike[str], *, create: bool = False,
                   own_flag: bool = False, clock: Callable[[], _dt.datetime] = _utc_now,
                   registry: L.Registry | None = None) -> ApplyResult:
    """Install or refresh the block in one platform's file.  Idempotent."""
    entry = build_entry(platform, home, create=create, own_flag=own_flag, registry=registry,
                        with_contradictions=False)
    state = entry.state
    shown = _display(state.path) if state else (platform.rel_path or platform.name)
    res = ApplyResult(platform, "refused", shown)
    if entry.status != "ok":
        res.messages = list(entry.refusals)
        return res
    assert state is not None and entry.new_text is not None
    res.messages.extend(f"warning: {w}" for w in entry.warnings)
    if entry.action == "none":
        res.status = "unchanged"
        res.messages.append("block already current; nothing written, no backup")
        return res

    real = state.real
    try:
        if state.exists:
            assert state.raw is not None
            if Path(real).read_bytes() != state.raw:
                res.status = "failed"
                res.messages.append("the file changed while the tool was working; nothing written")
                return res
            mode = state.mode
            stamp = clock().strftime("%Y%m%d-%H%M%S")
            res.backup = _make_backup(real, state.raw, mode, stamp)
        else:
            mode = 0o644
            os.makedirs(os.path.dirname(real), exist_ok=True)
        try:
            atomic_write(real, entry.new_text.encode("utf-8"), mode)
        except BaseException:
            if res.backup:               # the original is untouched; do not leave a stray copy
                try:
                    os.unlink(res.backup)
                    res.backup = None
                except OSError:
                    pass
            raise
        after = Path(real).read_text(encoding="utf-8")
        if compose(after, platform.variant or "full").action != "none":
            res.status = "failed"
            res.messages.append("read-back after the write does not hold a current block")
            return res
    except (OSError, MarkerError) as exc:
        res.status = "failed"
        res.messages.append(f"write failed, original left as it was: {exc}")
        return res
    res.status = "created" if not state.exists else "changed"
    res.messages.append(f"{entry.action} done" + (f"; backup {res.backup}" if res.backup
                                                  else "; no backup (the file did not exist)"))
    return res


# --------------------------------------------------------------------------- verifying

@dataclass
class VerifyResult:
    platform: Platform
    ok: bool
    path: str
    reasons: list[str] = field(default_factory=list)


def verify_platform(platform: Platform, home: str | os.PathLike[str]) -> VerifyResult:
    """Re-read the file and confirm the block is present and current."""
    if platform.rel_path is None or platform.variant is None:
        return VerifyResult(platform, False, "-", [f"unsupported: {platform.note}"])
    path = Path(os.path.abspath(home)) / platform.rel_path
    res = VerifyResult(platform, False, _display(path))
    state = inspect_file(platform, home)
    if state.refusals:
        res.reasons = list(state.refusals)
        return res
    if not state.exists or state.text is None:
        res.reasons.append("file does not exist")
        return res
    try:
        loc = find_block(state.text)
        comp = compose(state.text, platform.variant)
    except MarkerError as exc:
        res.reasons.append(f"malformed markers: {exc}")
        return res
    if loc is None:
        res.reasons.append("no Lane Map block")
        return res
    if comp.action != "none":
        res.reasons.append(f"block is stale (v{loc.version}); run apply to refresh it")
        return res
    past = _beyond_cap(state.text, platform)
    if past is not None:
        res.reasons.append(
            f"block ends at byte {past}, beyond the {platform.size_cap}-byte cap {platform.name} reads, so "
            f"{platform.name} never sees it; move the block above that point by hand")
        return res
    if platform.variant == "cursor":
        problem = frontmatter_problem(state.text)
        if problem:
            res.reasons.append(problem)
            return res
    res.ok = True
    return res


# --------------------------------------------------------------------------- output

def _rel_note(state: FileState) -> str:
    if not state.via_link:
        return "no"
    return f"yes -> {state.real}"


def render_entry(entry: PlanEntry, out: TextIO, *, explicit: bool, home: str = "") -> None:
    p = entry.platform
    path = entry.state.path if entry.state else os.path.join(home, p.rel_path or "-")
    print(f"== {p.name}  ({p.label})", file=out)
    if entry.status == "unsupported":
        print(f"   UNSUPPORTED: {p.note}", file=out)
        print(file=out)
        return
    print(f"   file: {path}", file=out)
    if entry.status == "needs-flag":
        verb = "REFUSED" if explicit else "SKIPPED"
        print(f"   {verb}: {entry.refusals[0]}", file=out)
        print(file=out)
        return
    st = entry.state
    assert st is not None
    if st.refusals:
        print(f"   exists: {'yes' if st.exists else 'no'}   symlink: {_rel_note(st)}", file=out)
        for r in entry.refusals:
            print(f"   REFUSED: {r}", file=out)
        print(file=out)
        return
    print(f"   exists: {'yes' if st.exists else 'no'}   size: {st.size} bytes   symlink: {_rel_note(st)}", file=out)
    print(f"   marker block: {entry.block}", file=out)
    if entry.action == "none":
        print("   apply would change: nothing", file=out)
    else:
        new_size = len((entry.new_text or "").encode("utf-8"))
        print(f"   apply would: {entry.action}  ({st.size} -> {new_size} bytes)", file=out)
    for w in entry.warnings:
        print(f"   WARNING: {w}", file=out)
    for r in entry.refusals:
        print(f"   apply would be REFUSED: {r}", file=out)
    if entry.diff:
        print("   diff:", file=out)
        for ln in entry.diff:
            print(f"     {ln}", file=out)
    print("   CONTRADICTIONS (report only; never edited automatically):", file=out)
    if not entry.contradictions:
        print("     none found", file=out)
    for c in entry.contradictions:
        print(f"     line {c.line}  [{c.kind}]  {c.snippet}", file=out)
    print(file=out)


# --------------------------------------------------------------------------- command line

class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):                      # never sys.exit(2): 2 means "refused"
        raise _UsageError(message)


def _add_common(sp: argparse.ArgumentParser, *, write_flags: bool) -> None:
    sp.add_argument("platforms", nargs="*", metavar="PLATFORM",
                    help="platform names: " + ", ".join(p.name for p in PLATFORMS))
    sp.add_argument("--home", default=None, help="home directory to work in (default: $HOME; must exist and not be empty)")
    if write_flags:
        sp.add_argument("--create", action="store_true",
                        help="allow creating a rules file that does not exist yet")
        sp.add_argument("--i-own-this-file", dest="own_flag", action="store_true",
                        help="required for the owner's own ~/.claude/CLAUDE.md")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python3 -m fleet_lanes.install_rules", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    _add_common(sub.add_parser("plan", help="show what apply would change (read-only; default: all platforms)"),
                write_flags=True)
    ap = sub.add_parser("apply", help="write the block into the named platforms' files")
    _add_common(ap, write_flags=True)
    ap.add_argument("--dry-run", action="store_true", help="same as plan")
    _add_common(sub.add_parser("verify", help="exit 1 unless each named file holds the current block"),
                write_flags=False)
    return parser


def _resolve_home(raw: str | None) -> str:
    """The home to work in.  Only an ABSENT --home means $HOME: an empty one (`--home "$UNSET"`) is a usage
    error, so a script bug can never fall through to the real home."""
    if raw is None:
        env = os.environ.get("HOME")
        if env is not None and not env.strip():
            raise _UsageError("HOME is set but empty; pass --home DIR")
        home, what = os.path.expanduser("~"), "HOME"
    else:
        if not raw.strip():
            raise _UsageError("--home must not be empty")
        home, what = os.path.expanduser(raw), "--home"
    if not os.path.isdir(home):
        raise _UsageError(f"{what} {home} is not a directory")
    if os.path.realpath(home) == os.sep:
        raise _UsageError(f"{what} resolves to /; refusing to use the filesystem root as a home")
    return home


def main(argv: Sequence[str] | None = None, *, clock: Callable[[], _dt.datetime] | None = None,
         out: TextIO | None = None, err: TextIO | None = None,
         registry: L.Registry | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    clock = clock or _utc_now
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or (args[0].startswith("-") and args[0] not in ("-h", "--help")):
        args.insert(0, "plan")                                  # the default command is the read-only one
    parser = build_parser()
    try:
        ns = parser.parse_args(args)
        if ns.command is None:
            raise _UsageError("a command is required: plan, apply or verify")
        home = _resolve_home(ns.home)
        explicit = bool(ns.platforms)
        try:
            platforms = resolve_platform_names(ns.platforms) if explicit else list(PLATFORMS)
        except KeyError as exc:
            raise _UsageError(f"unknown platform {exc.args[0]!r}; choose from "
                              + ", ".join(p.name for p in PLATFORMS)) from None
        if ns.command in ("apply", "verify") and not explicit:
            raise _UsageError(f"{ns.command} needs at least one platform name; it never defaults to all")
    except _UsageError as exc:
        print(f"install_rules: {exc}", file=err)
        return EXIT_USAGE

    reg = registry if registry is not None else default_registry()
    create, own_flag = getattr(ns, "create", False), getattr(ns, "own_flag", False)

    if ns.command == "plan" or getattr(ns, "dry_run", False):
        return _run_plan(platforms, home, create, own_flag, reg, explicit, out)
    if ns.command == "apply":
        return _run_apply(platforms, home, create, own_flag, clock, reg, out)
    return _run_verify(platforms, home, out)


def _run_plan(platforms: list[Platform], home: str, create: bool, own_flag: bool,
              registry: L.Registry | None, explicit: bool, out: TextIO) -> int:
    print(f"Lane Map rules plan  (home {home}; read-only, nothing is written)", file=out)
    print(file=out)
    entries = [build_entry(p, home, create=create, own_flag=own_flag, registry=registry) for p in platforms]
    for e in entries:
        render_entry(e, out, explicit=explicit, home=home)
    changing = sum(1 for e in entries if e.will_change)
    current = sum(1 for e in entries if e.status == "ok" and e.action == "none")
    other = len(entries) - changing - current
    print(f"Summary: {changing} would change, {current} already current, "
          f"{other} skipped, unsupported or refused.", file=out)
    hard = [e for e in entries if e.status == "needs-flag" and explicit]
    return EXIT_REFUSED if hard else EXIT_OK


def _run_apply(platforms: list[Platform], home: str, create: bool, own_flag: bool,
               clock: Callable[[], _dt.datetime], registry: L.Registry | None, out: TextIO) -> int:
    results = [apply_platform(p, home, create=create, own_flag=own_flag, clock=clock, registry=registry)
               for p in platforms]
    for r in results:
        print(f"{r.platform.name}: {r.status.upper()}  {r.path}", file=out)
        for m in r.messages:
            print(f"   {m}", file=out)
    if any(r.status == "refused" for r in results):
        return EXIT_REFUSED
    if any(r.status == "failed" for r in results):
        return EXIT_FAILED
    return EXIT_OK


def _run_verify(platforms: list[Platform], home: str, out: TextIO) -> int:
    results = [verify_platform(p, home) for p in platforms]
    for r in results:
        print(f"{r.platform.name}: {'OK' if r.ok else 'FAIL'}  {r.path}", file=out)
        for reason in r.reasons:
            print(f"   {reason}", file=out)
    return EXIT_OK if all(r.ok for r in results) else EXIT_FAILED


if __name__ == "__main__":         # pragma: no cover
    sys.exit(main())
