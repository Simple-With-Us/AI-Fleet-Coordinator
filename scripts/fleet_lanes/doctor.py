"""Lane doctor: find every git checkout on this Mac and say whether removing it could lose work.

    cd scripts && python3 -m fleet_lanes.doctor [--json] [--write PATH] [--sizes] [--deep]
                                               [--no-gh] [--strict] [--only-class CLASS]

REPORT ONLY.  The doctor never deletes, moves, fetches or modifies anything.  Every subprocess goes
through `run_cmd`, which refuses any command that is not on an explicit read-only allowlist (git
status [--ignored], rev-parse, rev-list [--remotes=NAME], log, for-each-ref, branch --show-current,
config --get, remote get-url, worktree list; gh pr list; lsof -d cwd; du -sk -x).  `git diff` is
deliberately absent: it refreshes the index and rewrites `.git/index` in checkouts that belong to
other seats.  Every git call also runs with GIT_OPTIONAL_LOCKS=0 and core.fsmonitor=false so `git
status` cannot touch the index or run a repo-configured monitor command.

Discovery is the union of (a) `git worktree list --porcelain` in every integration tree, every
discovered full clone and every parent repo a `.git` file points to, and (b) a bounded directory
scan (`scan_plan`) of the places checkouts hide.  Results are deduplicated by real path, so the
~/Code/Socratic.Trade symlink and its target are one checkout.  Depth convention: the scan root is
depth 0, its children depth 1, and a checkout is found when its own depth is at most the limit.

Safety classes, in precedence order:
  ACTIVE          a live process has it as cwd, or it is CLAUDE_PROJECT_DIR or the doctor's cwd
  NEEDS-REVIEW    any positive reason removal could lose work (every reason is listed): dirty files,
                  ignored local state (a database, a .env; build output does not count), a
                  .janitor-keep marker, unpushed commits, an open PR, and so on
  UNKNOWN         no known risk, but a needed fact could not be read (a failed git probe, a failed
                  gh lookup, a failed worktree list, git resolving the directory to another repo)
  SAFE-TO-REMOVE  clean, nothing unpushed, no open PR, nothing running (the evidence is listed)
A failed `gh` lookup or `lsof` never hides a dirty tree: known risks win over unknowns.

Fleet repos squash-merge and delete the branch, so ancestry says nothing.  Merged state comes from
one bulk `gh pr list` call per repo, matched by headRefName and headRefOid.  A merged PR whose head
equals local HEAD covers the checkout, and that evidence is applied before any unpushed or
detached-HEAD reason is recorded.  When gh is missing or fails the PR state is UNKNOWN, never NONE.

Tests: fleet_lanes/tests/test_doctor.py (real temp git repos; no network).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as _dt
import fnmatch
import json
import os
import re
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from . import layout as L

__all__ = [
    "SCHEMA", "CommandRefused", "CmdResult", "run_cmd", "check_allowed",
    "redact_url", "redact_text", "parse_owner_repo",
    "WorktreeRecord", "parse_worktree_porcelain", "count_status", "split_ignored", "IGNORED_LIST_CAP",
    "PrMatch", "match_pr", "make_gh_fetcher", "make_lsof_reader", "parse_lsof",
    "ScanSpec", "scan_plan", "find_git_dirs",
    "Checkout", "build_report", "render_text", "main",
]

SCHEMA = 1

# --------------------------------------------------------------------------- safe subprocess runner

# Hard caps in seconds.  git, lsof and du follow the spec (20, 20, 30).  gh gets longer because one
# bulk call lists up to 500 PRs over the network; a timeout there only yields PR state UNKNOWN.
_TIMEOUTS = {"git": 20.0, "gh": 60.0, "lsof": 20.0, "du": 30.0}

# Inherited variables that would point git at some other repository or index.  A hook that exports
# GIT_INDEX_FILE would otherwise make `git status` write a different index.
_STRIP_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_INDEX_VERSION",
)
_FORCED_ENV = {
    "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
    "GIT_NO_LAZY_FETCH": "1", "GH_PROMPT_DISABLED": "1", "GH_PAGER": "cat", "NO_COLOR": "1",
    "GH_NO_UPDATE_NOTIFIER": "1", "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
}

# `--ignored` only adds `!!` lines to the listing.  It reads the working tree the same way the
# untracked scan does and writes nothing; the other modes (`--ignored=matching`) stay refused.
_STATUS_FLAGS = frozenset({
    "--porcelain", "--porcelain=v1", "--untracked-files=normal", "--untracked-files=all",
    "--untracked-files=no", "--no-renames", "--branch", "--ignore-submodules=all",
    "--ignore-submodules=dirty", "--ignored",
})
_REVPARSE_FLAGS = frozenset({
    "--verify", "-q", "--quiet", "--short", "--git-dir", "--git-common-dir", "--show-toplevel",
    "--is-inside-work-tree", "--abbrev-ref", "--is-bare-repository",
})
_REVLIST_FLAGS = frozenset({"--count", "--branches", "--remotes", "--not", "--all", "--tags"})
# `--remotes=NAME` limits the "already pushed" test to one remote's tracking refs.  NAME is a plain
# remote name, never a path or an option.
_REMOTES_ARG = re.compile(r"^--remotes=[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONFIG_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GH_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GH_FIELDS = re.compile(r"^[A-Za-z]+(,[A-Za-z]+)*$")


class CommandRefused(ValueError):
    """The command is not on the read-only allowlist, so it was never started."""


@dataclass(frozen=True)
class CmdResult:
    """What a command did.  `rc` is None when it never ran to completion."""

    rc: int | None
    out: str = ""
    err: str = ""
    timed_out: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out and not self.error

    def failure_text(self) -> str:
        """One short line that says why the command failed, with credentials redacted."""
        if self.timed_out:
            return "timed out"
        if self.error:
            return redact_text(self.error)[:120]
        first = next((ln for ln in self.err.splitlines() if ln.strip()), "")
        return redact_text(first.strip())[:120] or f"exit {self.rc}"


def _positional(tok: str) -> bool:
    """A revision, ref or name: never an option, so it cannot change what a command does."""
    return bool(tok) and not tok.startswith("-") and "\x00" not in tok and "\n" not in tok


def _flags_then_positionals(rest: Sequence[str], flags: frozenset[str], prefixes: Sequence[str] = ()) -> bool:
    for tok in rest:
        if tok in flags or any(tok.startswith(p) for p in prefixes):
            continue
        if not _positional(tok):
            return False
    return True


def _git_allowed(args: Sequence[str]) -> bool:
    if not args or any("\x00" in a for a in args):
        return False
    sub, rest = args[0], list(args[1:])
    if sub == "status":
        return all(a in _STATUS_FLAGS for a in rest)
    if sub == "rev-parse":
        return _flags_then_positionals(rest, _REVPARSE_FLAGS)
    if sub == "rev-list":
        return _flags_then_positionals([a for a in rest if not _REMOTES_ARG.match(a)], _REVLIST_FLAGS,
                                       ("--max-count=",))
    if sub == "log":
        # `git log --output=FILE` writes a file, so only -1 and a format are accepted as options.
        return _flags_then_positionals(rest, frozenset({"-1", "--no-show-signature"}), ("--format=",))
    if sub == "for-each-ref":
        return _flags_then_positionals(rest, frozenset({"--contains"}), ("--format=", "--count=", "--sort="))
    if sub == "branch":
        return rest == ["--show-current"]
    if sub == "config":
        return len(rest) == 2 and rest[0] == "--get" and bool(_CONFIG_KEY.match(rest[1]))
    if sub == "remote":
        return len(rest) == 2 and rest[0] == "get-url" and _positional(rest[1])
    if sub == "worktree":
        return rest in (["list"], ["list", "--porcelain"])
    return False


def _gh_allowed(args: Sequence[str]) -> bool:
    if list(args[:2]) != ["pr", "list"]:
        return False
    checks: dict[str, Callable[[str], bool]] = {
        "--repo": lambda v: bool(_GH_REPO.match(v)),
        "--state": lambda v: v in ("open", "closed", "merged", "all"),
        "--limit": lambda v: v.isdigit() and 1 <= int(v) <= 1000,
        "--json": lambda v: bool(_GH_FIELDS.match(v)),
    }
    seen: set[str] = set()
    rest = list(args[2:])
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--") and "=" in tok:
            flag, value = tok.split("=", 1)
            i += 1
        else:
            flag = tok
            value = rest[i + 1] if i + 1 < len(rest) else None
            i += 2
        if flag not in checks or value is None or flag in seen or not checks[flag](value):
            return False
        seen.add(flag)
    return "--repo" in seen


def check_allowed(argv: Sequence[str]) -> str:
    """Return the tool name (git, gh, lsof, du) when `argv` is an allowlisted read-only command.
    Raises CommandRefused otherwise.  Only bare tool names are accepted as argv[0]."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise CommandRefused("empty or non-string command")
    tool, args = argv[0], list(argv[1:])
    ok = False
    if tool == "git":
        ok = _git_allowed(args)
    elif tool == "gh":
        ok = _gh_allowed(args)
    elif tool == "lsof":
        ok = args == ["-d", "cwd", "-Fpcn"]
    elif tool == "du":
        ok = len(args) == 3 and args[:2] == ["-sk", "-x"] and _positional(args[2])
    if not ok:
        raise CommandRefused("not on the read-only allowlist: " + " ".join(argv[:4]))
    return tool


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update(_FORCED_ENV)
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session makes the pgid equal the pid
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run_cmd(argv: Sequence[str], cwd: str | os.PathLike[str] | None = None,
            timeout: float | None = None) -> CmdResult:
    """The one place a subprocess starts.

    Refuses anything not on the allowlist (CommandRefused, before any process exists), sets
    GIT_OPTIONAL_LOCKS=0 and LC_ALL=C, strips inherited GIT_DIR and friends, and enforces a
    timeout: the smaller of `timeout` and the tool's cap (20 seconds, 30 for du).  git gets
    `--no-optional-locks -c core.fsmonitor=false` added here, so a caller cannot supply its own
    global options.  A timed-out command's whole process group is killed.
    """
    tool = check_allowed(argv)
    cap = _TIMEOUTS[tool]
    limit = cap if timeout is None else min(float(timeout), cap)
    full = list(argv)
    if tool == "git":
        full = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *argv[1:]]
    try:
        proc = subprocess.Popen(
            full, cwd=os.fspath(cwd) if cwd is not None else None, env=_child_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return CmdResult(None, error=f"{type(exc).__name__}: {exc}")
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the process is already dead; reaping is best effort
            pass
        return CmdResult(None, timed_out=True)
    return CmdResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- credentials

# Userinfo runs up to the LAST `@` before the host, because a password pasted into a remote may hold
# `/` or `@` unencoded.  After the user name only `:` or `@` may continue it, so a path such as
# https://registry.npmjs.org/@scope/pkg (a `/` straight after the host) is not mistaken for userinfo.
_URL_USERINFO = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@:\s]*(?:[:@]\S*)?)@(?=[A-Za-z0-9\[])")
# scp-style remotes have no scheme: `user@host:path`, and some people put the token in the user slot.
_SCP_URL = re.compile(r"^(?P<user>[^/@\s]+)@(?P<host>[^/@:\s]+):")
_SCP_TEXT = re.compile(
    r"(?<![\w@/.:%+~-])(?P<user>[A-Za-z0-9._~%+:-]+)@(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*):(?=\S)")


def redact_url(url: str) -> str:
    """Replace userinfo in a URL with REDACTED (user:token@host becomes REDACTED@host), including
    passwords that hold `/` or `@` and a token in the user slot of an scp-style remote.  The
    conventional ssh user `git` carries no secret and is kept."""
    def repl(m: re.Match) -> str:
        return m.group(0) if m.group("userinfo") == "git" else f"{m.group('scheme')}REDACTED@"
    out = _URL_USERINFO.sub(repl, url)
    if "://" not in out:
        out = _SCP_URL.sub(
            lambda m: m.group(0) if m.group("user") == "git" else f"REDACTED@{m.group('host')}:", out)
    return out


def redact_text(text: str) -> str:
    """Redact credentials in free text such as command stderr."""
    out = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}REDACTED@", text)
    return _SCP_TEXT.sub(
        lambda m: m.group(0) if m.group("user") == "git" else f"REDACTED@{m.group('host')}:", out)


_GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)


def parse_owner_repo(url: str | None) -> str | None:
    """OWNER/NAME for a GitHub remote (https, ssh or scp form), else None."""
    if not url:
        return None
    m = _GITHUB_REMOTE.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


# --------------------------------------------------------------------------- parsing helpers

@dataclass(frozen=True)
class WorktreeRecord:
    """One entry of `git worktree list --porcelain`."""

    path: str
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    locked: bool = False
    prunable: str | None = None  # the reason git gave, or "" when it gave none


def parse_worktree_porcelain(text: str) -> list[WorktreeRecord]:
    """Parse `git worktree list --porcelain`.  The first record is the main worktree."""
    records: list[WorktreeRecord] = []
    cur: dict[str, object] = {}

    def flush() -> None:
        if "path" in cur:
            records.append(WorktreeRecord(**cur))  # type: ignore[arg-type]
        cur.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            flush()
            cur["path"] = value
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
        elif key == "prunable":
            cur["prunable"] = value
    flush()
    return records


def count_status(porcelain: str) -> tuple[int, int]:
    """(tracked-modified, untracked) from `git status --porcelain=v1`.  Tracked counts every line
    that is not `??` (staged, unstaged, deleted, renamed, conflicted)."""
    tracked = untracked = 0
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            untracked += 1
        elif not line.startswith("!!"):
            tracked += 1
    return tracked, untracked


# Ignored paths that a build or a tool puts back by itself.  Anything else that git ignores is
# local state (a SQLite file, a dataset, a .env) that exists nowhere but this directory.
_REGENERABLE_NAMES = frozenset({
    "node_modules", ".DS_Store", ".build", "build", "dist", ".next", "__pycache__", "DerivedData", ".venv",
    "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
_REGENERABLE_GLOBS = ("*.tsbuildinfo", "*.pyc", "dist-*")
KEEP_MARKER = ".janitor-keep"  # has its own reason in assess, so it is not "ignored local state" too
IGNORED_LIST_CAP = 50
_QUOTED_ESCAPE = re.compile(r"\\([0-7]{3}|.)", re.S)
_SIMPLE_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, '"': 34, "\\": 92}


def _unquote_porcelain(text: str) -> str:
    """Undo git's C-style quoting of a path that holds spaces, quotes or non-ASCII bytes."""
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return text
    body, out, pos = text[1:-1], bytearray(), 0
    for m in _QUOTED_ESCAPE.finditer(body):
        out += body[pos:m.start()].encode("utf-8")
        tok = m.group(1)
        out.append((int(tok, 8) & 0xFF) if len(tok) == 3 else _SIMPLE_ESCAPES.get(tok, ord(tok[0]) & 0xFF))
        pos = m.end()
    out += body[pos:].encode("utf-8")
    return out.decode("utf-8", "replace")


def split_ignored(porcelain: str) -> tuple[list[str], int]:
    """(local-state paths, count of regenerable ones) from the `!!` lines of `git status --ignored`.
    A path is regenerable when any component of it is a known build or cache name or matches a
    build-artifact glob.  A directory keeps its trailing slash."""
    local: list[str] = []
    regenerable = 0
    for line in porcelain.splitlines():
        if not line.startswith("!! "):
            continue
        path = _unquote_porcelain(line[3:])
        parts = [part for part in path.split("/") if part]
        if not parts or path == KEEP_MARKER:
            continue
        if any(part in _REGENERABLE_NAMES or any(fnmatch.fnmatchcase(part, pat) for pat in _REGENERABLE_GLOBS)
               for part in parts):
            regenerable += 1
        else:
            local.append(path)
    return local, regenerable


def parse_lsof(text: str) -> list[tuple[int, str, str]]:
    """(pid, command, cwd path) rows from `lsof -d cwd -Fpcn`."""
    rows: list[tuple[int, str, str]] = []
    pid = -1
    comm = ""
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                pid = -1
            comm = ""
        elif tag == "c":
            comm = value
        elif tag == "n" and pid >= 0 and value.startswith("/"):
            rows.append((pid, comm, value))
    return rows


# --------------------------------------------------------------------------- PR matching

DEFAULT_BRANCHES = frozenset({"main", "master"})
GH_LIMIT = 500
GH_FIELDS = "number,state,headRefName,headRefOid,mergedAt,closedAt"


@dataclass(frozen=True)
class PrMatch:
    """How a checkout relates to pull requests.

    state: OPEN | MERGED | CLOSED | BEYOND-MERGED | NONE | UNKNOWN | N/A.  MERGED and CLOSED mean
    local HEAD is covered by a PR head that lives on GitHub (so unpushed commits are not at risk).
    BEYOND-MERGED means a merged PR exists for this branch but HEAD has commits past its head.
    """

    state: str
    number: int | None = None
    basis: str = ""
    beyond: int | None = None  # commits past the merged head; None when it could not be counted


def match_pr(branch: str | None, head_sha: str | None, prs: Sequence[Mapping[str, object]] | None, *,
             truncated: bool = False, beyond: Callable[[str], int | None] | None = None,
             limit: int = GH_LIMIT) -> PrMatch:
    """Match a checkout against one repo's bulk PR list by headRefName and headRefOid.

    `prs` None means the lookup failed or was skipped (UNKNOWN).  `truncated` is true when the list
    hit the --limit, so "no match" cannot be trusted.  `beyond(oid)` returns the number of commits
    in HEAD that are not reachable from `oid`, or None when the object is not in the local repo.
    """
    if prs is None:
        return PrMatch("UNKNOWN", basis="gh lookup failed or skipped")
    matched: list[tuple[Mapping[str, object], bool, bool]] = []
    for pr in prs:
        by_name = bool(branch) and pr.get("headRefName") == branch
        by_sha = bool(head_sha) and pr.get("headRefOid") == head_sha
        if by_name or by_sha:
            matched.append((pr, by_name, by_sha))

    def newest(items: list[Mapping[str, object]]) -> Mapping[str, object]:
        return max(items, key=lambda p: int(p.get("number") or 0))

    opens = [p for p, _, _ in matched if p.get("state") == "OPEN"]
    if opens:
        pr = newest(opens)
        return PrMatch("OPEN", int(pr["number"]), "open PR for this branch or HEAD")
    exact_merged = [p for p, _, s in matched if s and p.get("state") == "MERGED"]
    if exact_merged:
        pr = newest(exact_merged)
        return PrMatch("MERGED", int(pr["number"]), "HEAD equals the head of a merged PR")
    exact_closed = [p for p, _, s in matched if s and p.get("state") == "CLOSED"]
    if exact_closed:
        pr = newest(exact_closed)
        return PrMatch("CLOSED", int(pr["number"]), "HEAD equals the head of a closed (unmerged) PR")
    named_merged = [p for p, n, _ in matched if n and p.get("state") == "MERGED"]
    if named_merged:
        pr = newest(named_merged)
        oid = str(pr.get("headRefOid") or "")
        count = beyond(oid) if (beyond is not None and oid) else None
        if count == 0:
            return PrMatch("MERGED", int(pr["number"]), "HEAD is contained in the head of a merged PR", 0)
        return PrMatch("BEYOND-MERGED", int(pr["number"]),
                       "merged PR head is not local; cannot compare" if count is None
                       else "HEAD has commits past the merged PR head", count)
    if truncated:
        return PrMatch("UNKNOWN", basis=f"PR list hit the {limit} limit and had no match")
    return PrMatch("NONE", basis="no PR matches this branch or HEAD")


def make_gh_fetcher(run: Callable[..., CmdResult] = run_cmd,
                    limit: int = GH_LIMIT) -> Callable[[str], list[dict] | None]:
    """A fetcher for ONE bulk `gh pr list` call per repo.  Returns the PR list or None on failure."""
    def fetch(repo: str) -> list[dict] | None:
        res = run(["gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", str(limit),
                   "--json", GH_FIELDS], None, None)
        if not res.ok:
            return None
        try:
            data = json.loads(res.out or "[]")
        except ValueError:
            return None
        return data if isinstance(data, list) else None
    return fetch


def make_lsof_reader(run: Callable[..., CmdResult] = run_cmd) -> Callable[[], list[tuple[int, str, str]] | None]:
    """One read-only `lsof -d cwd -Fpcn` call.  None means lsof failed (the answer is unknown)."""
    def read() -> list[tuple[int, str, str]] | None:
        res = run(["lsof", "-d", "cwd", "-Fpcn"], None, None)
        # lsof exits 1 when it could not read some processes but still prints the rest.
        if res.timed_out or res.error or res.rc not in (0, 1) or not res.out.strip():
            return None
        return parse_lsof(res.out)
    return read


# --------------------------------------------------------------------------- discovery

# Never descend into these anywhere.
SKIP_DIR_NAMES = frozenset({"node_modules", ".Trash", "Library", ".git"})
# Extra names skipped by the ~ scan: macOS-protected folders (opening them prompts for permission
# and iCloud-backed ones time out), and the two roots that have their own scans.
HOME_SKIP = frozenset({"Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Public",
                       "Applications", "apps", "Code"})
# Package-manager and toolchain caches.  They hold git clones that are downloads, not working
# copies, and the ~ scan goes three levels down, deep enough to walk into them.
HOME_CACHE_SKIP = frozenset({".cache", ".npm", ".cargo", ".rustup", ".nvm", ".pyenv", ".rbenv", ".volta",
                             ".pnpm-store", ".yarn", ".bun", ".gradle", ".m2"})
DEEP_DIRS = ("Documents", "Desktop", "Downloads")
STATIC_TMP_SCAN = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")

# (relative path under ~, depth)
_HARNESS_SCANS = (
    (".codex/worktrees", 3), (".cursor/worktrees", 3), (".ag/worktrees", 3), (".grok", 4),
    (".fx", 4), (".botfleet", 5), (".gemini/antigravity", 5), (".buzz", 3),
)


@dataclass(frozen=True)
class ScanSpec:
    label: str
    path: str
    depth: int
    check_root: bool = True
    skip: frozenset[str] = SKIP_DIR_NAMES


def default_tmp_scan_roots(env: Mapping[str, str]) -> list[Path]:
    """/tmp, /private/tmp, /var/tmp, /private/var/tmp and the TMPDIR directory, deduplicated by
    real path."""
    cands = list(STATIC_TMP_SCAN)
    tmpdir = (env.get("TMPDIR") or "").strip()
    if tmpdir and os.path.isabs(tmpdir):
        cands.append(tmpdir)
    seen: set[str] = set()
    out: list[Path] = []
    for raw in cands:
        real = os.path.realpath(raw)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(Path(real))
    return out


def scan_plan(roots: L.Roots, tmp_scan_roots: Iterable[str | os.PathLike[str]], deep: bool = False) -> list[ScanSpec]:
    """Where to look for `.git` entries.  Pure apart from listing ~/Code once."""
    specs: list[ScanSpec] = []
    for t in tmp_scan_roots:
        specs.append(ScanSpec("tmp", os.path.realpath(os.fspath(t)), 4, True))
    specs.append(ScanSpec("apps", os.fspath(roots.apps_root), 2, False))
    specs.append(ScanSpec("lanes", os.fspath(roots.lanes_root), 4, False))
    specs.append(ScanSpec("code", os.fspath(roots.code_root), 1, False))
    try:
        with os.scandir(roots.code_root) as it:
            for entry in sorted(it, key=lambda e: e.name):
                wt = os.path.join(entry.path, ".claude", "worktrees")
                if os.path.isdir(wt):
                    specs.append(ScanSpec("code-claude-worktrees", os.path.realpath(wt), 1, False))
    except OSError:
        pass
    for rel, depth in _HARNESS_SCANS:
        specs.append(ScanSpec(rel, os.path.realpath(os.path.join(roots.home, rel)), depth, True))
    specs.append(ScanSpec("home", os.fspath(roots.home), 3, False, SKIP_DIR_NAMES | HOME_SKIP | HOME_CACHE_SKIP))
    if deep:
        for name in DEEP_DIRS:
            specs.append(ScanSpec(name.lower(), os.path.join(roots.home, name), 4, False))
    seen: set[tuple[str, int]] = set()
    unique: list[ScanSpec] = []
    for spec in specs:
        key = (spec.path.casefold() if roots.case_insensitive else spec.path, spec.depth)
        if key not in seen:
            seen.add(key)
            unique.append(spec)
    return unique


def find_git_dirs(root: str, max_depth: int, *, check_root: bool = True,
                  skip: frozenset[str] = SKIP_DIR_NAMES, found: set[str] | None = None,
                  fold: Callable[[str], str] = str) -> list[str]:
    """Directories at or below `root` that hold a `.git` entry (file or directory).

    Depth 0 is the root itself.  The walk stops at a checkout (nothing under one is reported
    separately), never follows symlinks, and skips `skip` names.  `check_root` False lets the root
    hold a `.git` (a dotfiles repo in ~) without ending the walk.  `found` is a set of folded
    paths already reported by earlier scans; those are skipped, not descended.
    """
    hits: list[str] = []
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if found is not None and depth > 0 and fold(directory) in found:
            continue
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        if any(e.name == ".git" for e in entries) and (depth > 0 or check_root):
            hits.append(directory)
            continue
        if depth >= max_depth:
            continue
        for entry in entries:
            if entry.name in skip:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return sorted(hits)


# --------------------------------------------------------------------------- the checkout record

KINDS = ("LINKED-WORKTREE", "FULL-CLONE", "ORPHAN")
_LANE_CLASSES = (L.LocationClass.LANE_NESTED, L.LocationClass.LANE_FLAT, L.LocationClass.LANE_FLAT_LEGACY)

# Plugin and marketplace caches that tools keep as git clones.  They are not working copies.
_TOOL_CACHE = re.compile(
    r"(/plugins/(cache|marketplaces|managed)(/|$)|/installed-plugins(/|$)|/marketplace-cache(/|$)"
    r"|/\.tmp/marketplaces(/|$)|/\.codex/memories(/|$)|/antigravity-cli/brain(/|$))", re.I)


@dataclass
class Checkout:
    """One checkout.  Fields that start with an underscore are internal and not exported."""

    path: str
    realpath: str
    kind: str = ""
    parent_repo: str | None = None
    owner_repo: str | None = None
    remote_url: str | None = None
    scope: str = "unknown"  # fleet | third-party | unknown
    location_class: str = ""
    lane_root: str | None = None
    creating_tool: str = "unknown"
    creating_tool_basis: str = ""
    tool_cache: bool = False
    branch: str | None = None
    detached_sha: str | None = None
    head_sha: str | None = None
    last_commit: str | None = None
    lane_age_days: float | None = None
    idle_days: float | None = None
    dirty_tracked: int | None = None
    dirty_untracked: int | None = None
    ignored_local_count: int | None = None
    ignored_local: list[str] = field(default_factory=list)
    ignored_regenerable: int | None = None
    unpushed: int | None = None
    unpushed_all_branches: int | None = None
    has_stash: bool | None = None
    upstream: str | None = None
    upstream_state: str = "unknown"
    pr_state: str = "UNKNOWN"
    pr_number: int | None = None
    pr_basis: str = ""
    registered: bool | None = None
    registered_worktrees: int = 0
    locked: bool = False
    cwd_procs: list[str] | None = None
    active: bool | None = None
    janitor_keep: bool = False
    size_mb: float | None = None
    name_verdict: str | None = None
    name_reasons: list[str] = field(default_factory=list)
    branch_verdict: str = ""
    safety: str = "UNKNOWN"
    safety_reasons: list[str] = field(default_factory=list)
    dropped_ball: bool | None = False
    read_errors: list[str] = field(default_factory=list)
    found_by: list[str] = field(default_factory=list)
    _key: str = field(default="", repr=False)
    _admin: str | None = field(default=None, repr=False)
    _detached_referenced: bool | None = field(default=None, repr=False)
    _origin_local: str | None = field(default=None, repr=False)
    _remote_checked: bool = field(default=False, repr=False)
    _remote_notes: list[str] = field(default_factory=list, repr=False)
    _git_ok: bool = field(default=False, repr=False)  # git resolved this very directory

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if not f.name.startswith("_")}


@dataclass(frozen=True)
class _Ctx:
    roots: L.Roots
    registry: L.Registry
    run: Callable[..., CmdResult]
    now: float
    sizes: bool


@dataclass
class _Cand:
    path: str
    key: str
    sources: list[str]


def _call(ctx: _Ctx, argv: Sequence[str], cwd: str | None = None, timeout: float | None = None) -> CmdResult:
    """Run through the injected runner.  A refusal is a bug and propagates; anything else the
    runner raises becomes a failed result so one unreadable repo cannot stop the sweep."""
    try:
        return ctx.run(list(argv), cwd, timeout)
    except CommandRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - an injected or flaky runner must not end the sweep
        return CmdResult(None, error=f"{type(exc).__name__}: {exc}")


def _read_text(path: str, limit: int = 4096) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).isoformat(timespec="seconds")


def _days(now: float, then: float | None) -> float | None:
    return None if then is None else round(max(0.0, now - then) / 86400.0, 1)


# --------------------------------------------------------------------------- per-checkout inspection

def _layout_facts(path: str) -> tuple[str, str | None, str | None]:
    """(kind, admin dir, parent repo) from file reads only: no git process runs here."""
    dot = os.path.join(path, ".git")
    if os.path.isdir(dot):
        return "FULL-CLONE", dot, path
    text = _read_text(dot)
    m = re.match(r"gitdir:\s*(.+?)\s*$", text or "", re.S)
    if not m:
        return "ORPHAN", None, None
    admin = m.group(1)
    if not os.path.isabs(admin):
        admin = os.path.normpath(os.path.join(path, admin))
    if not os.path.isdir(admin):
        guess = re.match(r"(.*)/\.git/worktrees/[^/]+/?$", admin)
        return "ORPHAN", admin, guess.group(1) if guess else None
    common_text = _read_text(os.path.join(admin, "commondir"), 1024)
    if common_text is None:
        # A submodule or separate git dir: a clone whose repository lives elsewhere.
        return "FULL-CLONE", admin, path
    common = os.path.realpath(os.path.join(admin, common_text.strip()))
    parent = os.path.dirname(common) if os.path.basename(common) == ".git" else common
    return "LINKED-WORKTREE", admin, parent


def _upstream_state(track: str, upstream: str) -> str:
    if not upstream:
        return "none"
    t = track.strip()
    if not t:
        return "in-sync"
    if t == "[gone]":
        return "gone"
    ahead, behind = "ahead" in t, "behind" in t
    return "diverged" if ahead and behind else ("ahead" if ahead else "behind")


def _is_unborn(res: CmdResult) -> bool:
    low = res.err.lower()
    return res.rc not in (None, 0) and any(s in low for s in ("unknown revision", "ambiguous argument 'head'", "bad revision"))


def _is_local_url(raw: str) -> bool:
    """True when a remote url names a path on this Mac: absolute, ./ or ../ relative, ~, file: or a
    bare relative path.  `scheme://` and scp-style `host:path` urls are servers."""
    if raw.startswith("file:"):
        return True
    if "://" in raw:
        return False
    return ":" not in raw.split("/", 1)[0]


def _origin_url(ctx: _Ctx, path: str, depth: int = 0) -> tuple[str | None, str | None, str | None, str | None]:
    """(redacted remote url, owner/repo, local origin path, error).  A local-path origin is
    followed up to two hops to find the GitHub repo it was cloned from."""
    res = _call(ctx, ["git", "remote", "get-url", "origin"], path)
    if res.timed_out or res.error:
        return None, None, None, "remote: " + res.failure_text()
    if not res.ok:
        return None, None, None, None
    raw = res.out.strip()
    if not raw:
        return None, None, None, None
    shown = redact_url(raw)
    owner_repo = parse_owner_repo(shown)
    local: str | None = None
    if owner_repo is None and (raw.startswith(("/", "./", "../", "~")) or raw.startswith("file:")):
        local = raw.replace("file://", "", 1)
        if local.startswith("~"):
            local = os.path.join(os.fspath(ctx.roots.home), local[2:])
        if depth < 2 and os.path.isdir(local):
            _, owner_repo, _, _ = _origin_url(ctx, local, depth + 1)
    return shown, owner_repo, local, None


def _inspect(cand: _Cand, ctx: _Ctx) -> Checkout:
    """Phase 1 for one checkout: location, kind and every git fact.  Runs in a worker thread."""
    path = cand.path
    real = L.resolve_path(path, ctx.roots)
    co = Checkout(path=path, realpath=real, found_by=list(cand.sources), _key=cand.key)
    kind, admin, parent = _layout_facts(path)
    co.kind, co._admin, co.parent_repo = kind, admin, parent
    if admin and os.path.exists(os.path.join(admin, "locked")) and kind == "LINKED-WORKTREE":
        co.locked = True
    co.janitor_keep = os.path.exists(os.path.join(path, KEEP_MARKER))
    try:
        st = os.stat(path)
        co.lane_age_days = _days(ctx.now, getattr(st, "st_birthtime", st.st_mtime))
    except OSError:
        pass

    if kind != "ORPHAN":
        _probe_git(co, ctx, path)
        times: list[float] = []
        if admin:
            try:
                times.append(os.stat(os.path.join(admin, "HEAD")).st_mtime)
            except OSError:
                pass
        if co.last_commit:
            times.append(_dt.datetime.fromisoformat(co.last_commit).timestamp())
        co.idle_days = _days(ctx.now, max(times)) if times else None
    if ctx.sizes:
        res = _call(ctx, ["du", "-sk", "-x", path])
        first = res.out.split()[0] if res.ok and res.out.split() else ""
        co.size_mb = round(int(first) / 1024.0, 1) if first.isdigit() else None
    _decorate(co, ctx)
    return co


_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _why(res: CmdResult) -> str:
    """Why a probe gave no usable answer: the failure, or the output it printed instead of a number."""
    return res.failure_text() if not res.ok else "unexpected output"


def _as_count(res: CmdResult) -> int | None:
    text = res.out.strip()
    return int(text) if res.ok and text.isdigit() else None


def _remote_scope(ctx: _Ctx, path: str) -> tuple[list[str] | None, list[str], str | None]:
    """Which remotes may vouch for a commit being pushed: (trusted remote names, notes about the
    others, error).  A remote counts when it is `origin` (a local-path origin has its own risk
    line) or its url is a server.  A tracking ref of any other remote whose url is a local path, or
    unreadable, proves nothing about GitHub.  Trusted None means the remotes could not be listed, so
    the caller falls back to every remote and the error makes the checkout UNKNOWN."""
    res = _call(ctx, ["git", "for-each-ref", "--format=%(refname)", "refs/remotes"], path)
    if not res.ok:
        return None, [], "remotes: " + res.failure_text()
    names: list[str] = []
    for line in res.out.splitlines():
        ref = line.strip()
        name = ref[len("refs/remotes/"):].split("/", 1)[0] if ref.startswith("refs/remotes/") else ""
        if name and name not in names:
            names.append(name)
    trusted: list[str] = []
    notes: list[str] = []
    unreadable = "its tracking refs do not count as pushed"
    for name in names:
        if name == "origin":
            trusted.append(name)
        elif not _REMOTE_NAME.match(name):
            notes.append(f"remote '{name}' has no readable url; {unreadable}")
        else:
            res = _call(ctx, ["git", "config", "--get", f"remote.{name}.url"], path)
            if res.timed_out or res.error or res.rc not in (0, 1):
                return None, [], f"remote-url {name}: " + res.failure_text()
            url = res.out.strip() if res.ok else ""
            if not url:
                notes.append(f"remote '{name}' has no readable url; {unreadable}")
            elif _is_local_url(url):
                notes.append(f"remote '{name}' is a local path ({redact_url(url)}); {unreadable}")
            else:
                trusted.append(name)
    return trusted, notes, None


def _not_remotes(trusted: list[str] | None) -> list[str]:
    """rev-list arguments that leave out everything reachable from a trusted remote."""
    if trusted is None:
        return ["--not", "--remotes"]
    return ["--not", *(f"--remotes={name}" for name in trusted)] if trusted else []


def _probe_git(co: Checkout, ctx: _Ctx, path: str) -> None:
    errors = co.read_errors
    res = _call(ctx, ["git", "rev-parse", "HEAD"], path)
    if res.ok:
        co.head_sha = res.out.strip() or None
    elif not _is_unborn(res):
        errors.append("head: " + res.failure_text())
        return  # not a usable repo; skip the other calls instead of failing each in turn
    # A `.git` that git does not accept (empty, or no HEAD, as after an interrupted rm -rf) makes git
    # walk up and answer for the enclosing repository.  Every fact below would then be that
    # repository's, so insist that git resolves this very directory and this very git dir.
    res = _call(ctx, ["git", "rev-parse", "--show-toplevel", "--git-dir"], path)
    lines = res.out.splitlines() if res.ok else []
    if len(lines) != 2:
        errors.append("toplevel: " + (res.failure_text() if not res.ok else "unexpected output"))
        co.head_sha = None
        return
    toplevel, git_dir = lines[0].strip(), lines[1].strip()
    if L.real_key(toplevel, ctx.roots) != L.real_key(path, ctx.roots):
        errors.append(f"git resolves this directory to {toplevel}")
        co.head_sha = None
        return
    if co._admin and L.real_key(os.path.join(path, git_dir), ctx.roots) != L.real_key(co._admin, ctx.roots):
        errors.append(f"git uses {git_dir} but this directory's .git points to {co._admin}")
        co.head_sha = None
        return
    co._git_ok = True
    res = _call(ctx, ["git", "branch", "--show-current"], path)
    if res.ok:
        co.branch = res.out.strip() or None
    else:
        errors.append("branch: " + res.failure_text())
    if co.branch is None and co.head_sha:
        co.detached_sha = co.head_sha[:9]
    res = _call(ctx, ["git", "status", "--porcelain=v1", "--untracked-files=normal"], path)
    if res.ok:
        co.dirty_tracked, co.dirty_untracked = count_status(res.out)
    else:
        errors.append("status: " + res.failure_text())
    # Ignored files get their own call: walking a large ignored tree is the slow part, and a
    # timeout there must not take the dirty counts down with it.
    res = _call(ctx, ["git", "status", "--porcelain=v1", "--untracked-files=normal", "--ignored"], path)
    if res.ok:
        local, co.ignored_regenerable = split_ignored(res.out)
        co.ignored_local_count, co.ignored_local = len(local), local[:IGNORED_LIST_CAP]
    else:
        errors.append("ignored: " + res.failure_text())
    url, owner_repo, local_origin, err = _origin_url(ctx, path)
    if err:
        errors.append(err)
    else:
        co._remote_checked = True
    co.remote_url, co.owner_repo, co._origin_local = url, owner_repo, local_origin
    not_args: list[str] = []
    if co.head_sha or co.kind == "FULL-CLONE":
        trusted, co._remote_notes, err = _remote_scope(ctx, path)
        if err:
            errors.append(err)
        not_args = _not_remotes(trusted)
    if co.head_sha:
        res = _call(ctx, ["git", "rev-list", "--count", "HEAD", *not_args], path)
        count = _as_count(res)
        if count is not None:
            co.unpushed = count
        else:
            errors.append("unpushed: " + _why(res))
        res = _call(ctx, ["git", "log", "-1", "--format=%ct"], path)
        if res.ok and res.out.strip().isdigit():
            co.last_commit = _iso(float(res.out.strip()))
    else:
        co.unpushed = 0
    if co.branch:
        res = _call(ctx, ["git", "for-each-ref", "--format=%(upstream:short)|%(upstream:track)",
                          "refs/heads/" + co.branch], path)
        if res.ok and res.out.strip():
            up, _, track = res.out.strip().partition("|")
            co.upstream = up or None
            co.upstream_state = _upstream_state(track, up)
        elif res.ok:
            co.upstream_state = "none"
    elif co.head_sha:
        res = _call(ctx, ["git", "for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)",
                          "refs/heads", "refs/remotes", "refs/tags"], path)
        co._detached_referenced = bool(res.out.strip()) if res.ok else None
    if co.kind == "FULL-CLONE":
        res = _call(ctx, ["git", "rev-list", "--count", "--branches", *not_args], path)
        count = _as_count(res)
        if count is not None:
            co.unpushed_all_branches = count
        else:
            errors.append("unpushed-all-branches: " + _why(res))
        res = _call(ctx, ["git", "for-each-ref", "--count=1", "--format=%(refname)", "refs/stash"], path)
        if res.ok:
            co.has_stash = bool(res.out.strip())
        else:
            errors.append("stash: " + res.failure_text())


_CLAUDE_HEX = re.compile(r"-[0-9a-f]{6}$")
_CLAUDE_EPHEMERAL = re.compile(r"^(agent-a[0-9a-f]{7,}|wf[_-].*|bridge-.*|job-.*|bg-.*)$")
# Seat token -> creating tool.  The last five are tools outside the original list (MiniMax Code,
# DeepSeek Harness, Kimi, Muse Assist, Muse Code); they report under their own names instead of
# collapsing into "unknown".
_SEAT_TOOLS = {
    "claude": "claude-cli", "monet": "claude-cli", "renoir": "claude-cli", "codex": "codex",
    "cursor": "cursor", "grok": "grok", "grok-build": "grok", "fx": "fx", "antigravity": "antigravity",
    "minimax": "minimax", "deepseek": "deepseek", "kimi": "kimi", "muse-assist": "muse-assist",
    "muse-code": "muse-code",
}


def infer_tool(real: str, roots: L.Roots, branch: str | None, seat: str | None,
               branch_seat: str | None) -> tuple[str, str]:
    """(creating tool, basis).  A guess from the path first, then the lane's seat token, then the
    branch prefix.  The basis says which evidence was used so a reader can discount it."""
    home = os.fspath(roots.home)
    fold = (lambda s: s.casefold()) if roots.case_insensitive else (lambda s: s)
    r = fold(real)

    def under(rel: str) -> bool:
        base = fold(os.path.join(home, rel))
        return r == base or r.startswith(base + os.sep)

    for rel, tool in ((".codex", "codex"), (".cursor", "cursor"), (".grok", "grok"), (".fx", "fx"),
                      (".gemini", "antigravity"), (".ag", "antigravity"), (".botfleet", "botfleet"),
                      (".buzz", "buzz"), (".claude", "claude-cli")):
        if under(rel):
            return tool, f"path:~/{rel}"
    m = re.search(r"/\.claude/worktrees/([^/]+)", real)
    if m:
        name = m.group(1)
        if (branch or "").startswith("worktree-") or _CLAUDE_EPHEMERAL.match(name):
            return "claude-cli", "path:.claude/worktrees; ephemeral or worktree- name"
        if _CLAUDE_HEX.search(name):
            return "claude-desktop", "path:.claude/worktrees; name ends -<6 hex> like the desktop app"
        return "claude-cli", "path:.claude/worktrees; no desktop suffix (flavor is a guess)"
    for seat_value, label in ((seat, "lane seat token"), (branch_seat, "branch prefix")):
        if seat_value:
            if seat_value.startswith("BF-"):
                return "botfleet", f"{label}:{seat_value}"
            tool = _SEAT_TOOLS.get(seat_value)
            if tool:
                note = " (claude account seat; desktop or cli)" if tool == "claude-cli" else ""
                return tool, f"{label}:{seat_value}{note}"
    if under("Code"):
        return "human", "path:~/Code"
    return "unknown", "no path, seat or branch evidence"


def _decorate(co: Checkout, ctx: _Ctx) -> None:
    """Location class, naming verdicts, scope and creating tool.  Pure."""
    roots, registry = ctx.roots, ctx.registry
    cls = L.classify_location(co.realpath, roots, is_checkout=True)
    co.location_class = str(cls)
    lane = L.lane_root(co.realpath, roots)
    co.lane_root = os.fspath(lane) if lane else None
    res = L.explain_lane_name(co.realpath, registry, roots)
    if cls in _LANE_CLASSES:
        co.name_verdict = str(L.upgrade_with_branch(res.verdict, co.branch, registry))
        co.name_reasons = list(res.reasons)
    co.branch_verdict = str(L.check_branch_name(co.branch, registry))
    seat = res.seat if cls in _LANE_CLASSES else None
    co.creating_tool, co.creating_tool_basis = infer_tool(
        co.realpath, roots, co.branch, seat, L.seat_from_branch(co.branch, registry))
    co.tool_cache = bool(_TOOL_CACHE.search(co.realpath.replace(os.sep, "/")))
    if co.owner_repo:
        co.scope = "fleet" if co.owner_repo.split("/")[0].casefold() == registry.owner.casefold() else "third-party"


# --------------------------------------------------------------------------- safety

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _ignored_reason(co: Checkout) -> str:
    shown = co.ignored_local[:3]
    more = (co.ignored_local_count or 0) - len(shown)
    return "ignored local state: " + ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def assess(co: Checkout, pr: PrMatch, active_reasons: Sequence[str]) -> None:
    """Fill safety, safety_reasons and dropped_ball.  Order matters: the PR coverage test runs
    first, because a squash-merged lane reports every commit as unpushed and its branch as gone
    from the remote, and those reasons must be suppressed when GitHub holds the commits."""
    risk: list[str] = []
    unknown: list[str] = []
    evidence: list[str] = []
    covered = pr.state in ("MERGED", "CLOSED")
    co.pr_state, co.pr_number, co.pr_basis = pr.state, pr.number, pr.basis

    if co.kind == "ORPHAN":
        unknown.append("orphan: .git points to a missing gitdir, so no git state can be read")
    unknown.extend(f"unreadable: {e}" for e in co.read_errors)

    dirty = (co.dirty_tracked or 0) + (co.dirty_untracked or 0)
    if co.dirty_tracked is not None and dirty:
        risk.append(f"dirty: {co.dirty_tracked} tracked-modified, {co.dirty_untracked} untracked")
    elif co.dirty_tracked is not None:
        evidence.append("working tree clean")
    # Ignored files are invisible to a plain status.  Build output is rebuilt on demand, but a
    # database, a dataset or a .env exists only in this directory.
    if co.ignored_local_count:
        risk.append(_ignored_reason(co))
    elif co.ignored_local_count == 0 and co.ignored_regenerable:
        evidence.append(f"ignored files are regenerable build output only ({co.ignored_regenerable})")
    if co.janitor_keep:
        risk.append(f"keep marker {KEEP_MARKER}: ask the owner first")

    if co._remote_checked and co.remote_url is None:
        risk.append("no remote configured (origin)")
    if co._origin_local:
        risk.append(f"origin is a local path ({co._origin_local}); work may not be on GitHub")
    risk.extend(co._remote_notes)

    if pr.state == "OPEN":
        risk.append(f"open PR #{pr.number}")
    elif pr.state == "BEYOND-MERGED":
        extra = "" if pr.beyond is None else f" ({_plural(pr.beyond, 'commit')})"
        risk.append(f"commits beyond merged PR #{pr.number}{extra}" +
                    ("; merged head not in local repo" if pr.beyond is None else ""))
    elif covered:
        evidence.append(f"PR #{pr.number} {pr.state.lower()}: {pr.basis}")
    elif pr.state == "UNKNOWN":
        # An UNKNOWN state is only ever set for a fleet checkout off the default branch, which
        # needed a lookup.  Without it an open PR looks like a clean pushed lane.
        unknown.append("PR state unknown: " + pr.basis)

    if co.unpushed is not None and co.unpushed > 0 and not covered:
        note = " (PR state unknown; may already be merged)" if pr.state == "UNKNOWN" else ""
        risk.append(f"unpushed commits: {co.unpushed}{note}")
    elif co.unpushed == 0 and co.head_sha:
        evidence.append("every commit is reachable from a remote-tracking ref")
    elif co.head_sha is None and not co.read_errors and co.kind != "ORPHAN":
        evidence.append("no commits yet")
    if co.branch is None and co.head_sha and co._detached_referenced is False and not covered:
        risk.append("detached HEAD commits are not on any branch, remote branch or tag (only copy)")
    other_branch_commits = 0
    if co.kind == "FULL-CLONE":
        raw = co.unpushed if (co.branch and co.unpushed) else 0
        other_branch_commits = max(0, (co.unpushed_all_branches or 0) - raw)
        if other_branch_commits:
            risk.append(f"other local branches hold {_plural(other_branch_commits, 'unpushed commit')}")
        if co.has_stash:
            risk.append("stash entries exist")
    if co.registered_worktrees:
        risk.append(f"parent of {_plural(co.registered_worktrees, 'registered linked worktree')}")
    if co.locked:
        risk.append("worktree is locked")
    if co.registered is False:
        risk.append("not in the parent repo's worktree list (moved or stale)")

    if co.active is None:
        unknown.append("process check unavailable (lsof failed)")
    elif not co.active:
        evidence.append("no process has it as cwd")

    if co.active:
        co.safety = "ACTIVE"
        co.safety_reasons = [*active_reasons, *risk, *unknown]
    elif risk:
        co.safety, co.safety_reasons = "NEEDS-REVIEW", [*risk, *unknown]
    elif unknown:
        co.safety, co.safety_reasons = "UNKNOWN", unknown
    else:
        co.safety, co.safety_reasons = "SAFE-TO-REMOVE", evidence

    # dropped_ball: NEEDS-REVIEW with dirty files or uncovered unpushed commits and no open PR.
    # A parent's unpushed branch commits are normally its lanes' work, which the lane rows flag, so
    # they raise dropped_ball only on a clone that hosts no worktrees.
    unpushed_at_risk = ((co.unpushed or 0) > 0 and not covered) or \
        (other_branch_commits > 0 and not co.registered_worktrees)
    if co.safety != "NEEDS-REVIEW" or not (dirty or unpushed_at_risk):
        co.dropped_ball = False
    elif pr.state == "OPEN":
        co.dropped_ball = False
    elif pr.state == "UNKNOWN":
        co.dropped_ball = None  # cannot assert "no open PR"
    else:
        co.dropped_ball = True


# --------------------------------------------------------------------------- build the report

_ANOMALY_ORDER = ("FORBIDDEN_TMP", "FORBIDDEN_CODE_TOPLEVEL", "ORPHAN", "PRUNABLE", "UNSANCTIONED",
                  "FULL-CLONE-IN-LANE", "NAME-DRIFT", "NON-LANE")


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _list_worktrees(ctx: _Ctx, parent: str) -> tuple[str, list[WorktreeRecord] | None, str]:
    res = _call(ctx, ["git", "worktree", "list", "--porcelain"], parent)
    if not res.ok:
        return parent, None, res.failure_text()
    return parent, parse_worktree_porcelain(res.out), ""


def _admin_worktree_count(co: Checkout) -> int:
    """Linked worktrees git has registered for this repository, counted from its admin directory
    without starting git.  Used when `git worktree list` failed."""
    if not co._admin:
        return 0
    try:
        with os.scandir(os.path.join(co._admin, "worktrees")) as it:
            return sum(1 for entry in it if entry.is_dir())
    except OSError:
        return 0


def _is_strict_violation(co: Checkout) -> bool:
    if co.location_class in ("FORBIDDEN_TMP", "FORBIDDEN_CODE_TOPLEVEL"):
        return True
    return co.location_class == "UNSANCTIONED" and not co.tool_cache and co.scope != "third-party"


def _anomalies_for(co: Checkout) -> list[dict]:
    out: list[dict] = []

    def add(kind: str, detail: str) -> None:
        out.append({"type": kind, "path": co.path, "realpath": co.realpath, "detail": detail})

    cls = co.location_class
    if cls == "FORBIDDEN_TMP":
        add("FORBIDDEN_TMP", "checkout inside a temp directory")
    elif cls == "FORBIDDEN_CODE_TOPLEVEL":
        add("FORBIDDEN_CODE_TOPLEVEL", "checkout directly under ~/Code that is not a registered integration tree")
    elif cls == "UNSANCTIONED" and not co.tool_cache and co.scope != "third-party":
        add("UNSANCTIONED", "checkout outside the sanctioned places")
    if co.kind == "ORPHAN":
        add("ORPHAN", ".git points to a missing gitdir")
    if co.kind == "FULL-CLONE" and cls in [str(c) for c in _LANE_CLASSES]:
        add("FULL-CLONE-IN-LANE", "full clone inside a lane root; lanes should be linked worktrees")
    if co.name_verdict in ("NAME-DRIFT", "NON-LANE"):
        add(co.name_verdict, "lane name: " + ", ".join(co.name_reasons))
    return out


def build_report(home: str | os.PathLike[str], env: Mapping[str, str] | None = None, *,
                 cwd: str | os.PathLike[str] | None = None,
                 tmp_scan_roots: Iterable[str | os.PathLike[str]] | None = None,
                 deep: bool = False, sizes: bool = False, use_gh: bool = True,
                 registry: L.Registry | None = None,
                 run: Callable[..., CmdResult] = run_cmd,
                 gh_prs: Callable[[str], list[dict] | None] | None = None,
                 lsof_cwds: Callable[[], list[tuple[int, str, str]] | None] | None = None,
                 clock: Callable[[], _dt.datetime] | None = None, workers: int = 8,
                 case_insensitive: bool | None = None, only_class: str | None = None,
                 gh_limit: int = GH_LIMIT) -> dict:
    """Run the whole sweep and return the JSON-ready report.

    Everything that touches the machine is injectable: `run` (every subprocess), `gh_prs`
    (repo -> PR list or None), `lsof_cwds` (-> rows or None), `clock`, `tmp_scan_roots` (default:
    the real temp dirs) and `cwd` (None skips the current-directory check).  `only_class` filters
    the output but `summary.strict_violations` always counts the whole machine.
    """
    env = os.environ if env is None else env
    clock = clock or _utc_now
    started = clock()
    now = started.timestamp()
    warnings: list[str] = []
    if registry is None:
        try:
            registry = L.load_registry(env=env)
        except (OSError, ValueError) as exc:
            registry = L.parse_registry({})
            warnings.append(f"registry unreadable ({type(exc).__name__}); app and seat names are partial")
    roots = L.make_roots(home, env, registry=registry, case_insensitive=case_insensitive)
    warnings.extend(roots.warnings)
    ctx = _Ctx(roots, registry, run, now, sizes)
    fold = str.casefold if roots.case_insensitive else str

    # ---- discovery (b): bounded directory scan
    tmp_roots = list(default_tmp_scan_roots(env)) if tmp_scan_roots is None else list(tmp_scan_roots)
    plan = scan_plan(roots, tmp_roots, deep)
    cands: dict[str, _Cand] = {}
    found_fold: set[str] = set()
    scan_info: list[dict] = []
    for spec in plan:
        hits = find_git_dirs(spec.path, spec.depth, check_root=spec.check_root, skip=spec.skip,
                             found=found_fold, fold=fold)
        for hit in hits:
            key = L.real_key(hit, roots)
            found_fold.add(fold(hit))
            if key in cands:
                cands[key].sources.append(f"scan:{spec.label}")
            else:
                cands[key] = _Cand(hit, key, [f"scan:{spec.label}"])
        scan_info.append({"label": spec.label, "path": spec.path, "depth": spec.depth, "found": len(hits)})

    # ---- inspect + discovery (a): worktree lists, repeated until no new checkout appears
    checkouts: dict[str, Checkout] = {}
    listed: dict[str, list[WorktreeRecord] | None] = {}
    list_errors: dict[str, str] = {}
    parent_of_list: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending = list(cands.values())
        for _ in range(4):
            if not pending:
                break
            for co in pool.map(lambda c: _inspect(c, ctx), pending):
                checkouts[co._key] = co
            want: dict[str, str] = {}
            for co in checkouts.values():
                if co.kind == "FULL-CLONE":
                    if co._git_ok:  # else git would list the enclosing repository's worktrees
                        want.setdefault(co._key, co.path)
                elif co.kind in ("LINKED-WORKTREE", "ORPHAN") and co.parent_repo and \
                        os.path.lexists(os.path.join(co.parent_repo, ".git")):
                    want.setdefault(L.real_key(co.parent_repo, roots), co.parent_repo)
            for pk in [k for k in want if k in listed]:
                del want[pk]
            keys = list(want)
            results = list(pool.map(lambda k: _list_worktrees(ctx, want[k]), keys))
            pending = []
            for pk, (parent, records, err) in zip(keys, results):
                listed[pk] = records
                parent_of_list[pk] = parent
                if err:
                    list_errors[pk] = err
                for rec in records or []:
                    k = L.real_key(rec.path, roots)
                    if rec.bare or k in checkouts or any(c.key == k for c in pending):
                        continue
                    if os.path.lexists(os.path.join(rec.path, ".git")):
                        pending.append(_Cand(rec.path, k, [f"worktree-list:{parent}"]))
                        found_fold.add(fold(rec.path))
    anomalies: list[dict] = []
    prunable_seen: set[tuple[str, str]] = set()
    for pk, records in listed.items():
        if records is None:
            warnings.append(f"worktree list failed in {parent_of_list[pk]}: {list_errors.get(pk, '')}")
            parent_co = checkouts.get(pk)
            if parent_co is not None:
                # Without the list nobody knows what hangs off this repository, so it can never be
                # SAFE.  The admin directory is a plain listing; it also counts stale entries, so
                # it can overstate the live worktrees but never understate them.
                parent_co.read_errors.append("worktree list: " + list_errors.get(pk, "failed"))
                parent_co.registered_worktrees = _admin_worktree_count(parent_co)
            continue
        registered_keys = {L.real_key(r.path, roots) for r in records[1:]}
        live = 0
        for rec in records[1:]:
            missing = not os.path.lexists(rec.path)
            if rec.prunable is not None or missing:
                dedupe = (pk, L.real_key(rec.path, roots))
                if dedupe not in prunable_seen:
                    prunable_seen.add(dedupe)
                    why = rec.prunable or "registered path no longer exists"
                    anomalies.append({"type": "PRUNABLE", "path": rec.path, "realpath": rec.path,
                                      "detail": f"{why} (parent {parent_of_list[pk]})"})
            else:
                live += 1
        parent_co = checkouts.get(pk)
        if parent_co is not None:
            parent_co.registered_worktrees = live
        for co in checkouts.values():
            if co.kind == "LINKED-WORKTREE" and co.parent_repo and L.real_key(co.parent_repo, roots) == pk:
                co.registered = co._key in registered_keys
    for co in checkouts.values():
        if co.kind == "LINKED-WORKTREE" and co.registered is None and co.parent_repo:
            pk = L.real_key(co.parent_repo, roots)
            if pk in list_errors:
                co.read_errors.append("worktree list: " + list_errors[pk])

    # ---- PR state: ONE bulk gh call per repo
    prs_by_repo: dict[str, list[dict] | None] = {}
    gh_status = "skipped"
    if use_gh:
        fetch = gh_prs or make_gh_fetcher(run, gh_limit)
        need = sorted({co.owner_repo for co in checkouts.values()
                       if co.owner_repo and co.scope == "fleet" and co.head_sha and co.kind != "ORPHAN"
                       and co.branch not in DEFAULT_BRANCHES})

        def safe_fetch(repo: str) -> list[dict] | None:
            try:
                return fetch(repo)
            except Exception:  # noqa: BLE001 - a failed lookup means UNKNOWN, never a crash
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for repo, data in zip(need, pool.map(safe_fetch, need)):
                prs_by_repo[repo] = data
        failed = sorted(repo for repo, data in prs_by_repo.items() if data is None)
        gh_status = "ok" if not failed else ("failed" if len(failed) == len(prs_by_repo) else "partial")
        if failed:
            warnings.append("gh PR lookup failed for " + ", ".join(failed) +
                            "; their checkouts report PR state UNKNOWN")

    # ---- processes: one lsof call
    lsof_rows = (lsof_cwds or make_lsof_reader(run))()
    keymap = {co._key: co for co in checkouts.values()}
    attributed: dict[str, list[str]] = {}

    def deepest(path: str) -> Checkout | None:
        k = L.real_key(path, roots)
        while True:
            if k in keymap:
                return keymap[k]
            up = os.path.dirname(k)
            if up == k:
                return None
            k = up
    if lsof_rows is not None:
        for pid, comm, cwd_path in lsof_rows:
            owner = deepest(cwd_path)
            if owner is not None:
                attributed.setdefault(owner._key, []).append(f"{pid}:{comm}")
    special: dict[str, list[str]] = {}
    project_dir = (env.get("CLAUDE_PROJECT_DIR") or "").strip()
    for label, value in (("CLAUDE_PROJECT_DIR", project_dir), ("current working directory", cwd and os.fspath(cwd))):
        if value:
            owner = deepest(value)
            if owner is not None:
                special.setdefault(owner._key, []).append(f"active: is the {label}")

    # ---- classify
    for co in checkouts.values():
        procs = attributed.get(co._key, [])
        active_reasons = list(special.get(co._key, []))
        if procs:
            active_reasons.append("active: process cwd " + ", ".join(procs[:5]) + (" ..." if len(procs) > 5 else ""))
        co.cwd_procs = procs if lsof_rows is not None else None
        co.active = True if active_reasons else (None if lsof_rows is None else False)
        if co.kind == "ORPHAN":
            pr = PrMatch("N/A", basis="orphan")
        elif not co.head_sha:
            pr = PrMatch("N/A", basis="no commits")
        elif co.branch in DEFAULT_BRANCHES:
            pr = PrMatch("N/A", basis="default branch")
        elif co.scope != "fleet" or not co.owner_repo:
            pr = PrMatch("N/A", basis="not a fleet GitHub repo")
        elif not use_gh:
            pr = PrMatch("UNKNOWN", basis="gh skipped (--no-gh)")
        else:
            prs = prs_by_repo.get(co.owner_repo)
            truncated = prs is not None and len(prs) >= gh_limit

            def beyond(oid: str, _co: Checkout = co) -> int | None:
                res = _call(ctx, ["git", "rev-list", "--count", "HEAD", "--not", oid], _co.path)
                return int(res.out.strip()) if res.ok and res.out.strip().isdigit() else None
            pr = match_pr(co.branch, co.head_sha, prs, truncated=truncated, beyond=beyond, limit=gh_limit)
        assess(co, pr, active_reasons)

    # ---- anomalies and summary
    ordered = sorted(checkouts.values(), key=lambda c: c.path)
    for co in ordered:
        anomalies.extend(_anomalies_for(co))
    anomalies.sort(key=lambda a: (_ANOMALY_ORDER.index(a["type"]), a["path"]))
    strict_violations = sum(1 for co in ordered if _is_strict_violation(co))
    total_found = len(ordered)
    shown = ordered
    if only_class:
        want_cls = only_class.upper()
        shown = [c for c in ordered if c.location_class == want_cls]
        paths = {c.path for c in shown}
        anomalies = [a for a in anomalies if a["path"] in paths]
    summary = _summarize(shown, anomalies)
    summary["total_found"] = total_found
    summary["strict_violations"] = strict_violations
    return {
        "schema": SCHEMA,
        "generated_at": started.isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "layout_mode": roots.layout_mode,
        "home": os.fspath(roots.home),
        "only_class": only_class.upper() if only_class else None,
        "lsof": "ok" if lsof_rows is not None else "unavailable",
        "gh": gh_status,
        "warnings": warnings,
        "scan": scan_info,
        "checkouts": [c.to_dict() for c in shown],
        "anomalies": anomalies,
        "summary": summary,
    }


def _count(values: Iterable[object]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _summarize(shown: Sequence[Checkout], anomalies: Sequence[dict]) -> dict:
    return {
        "total": len(shown),
        "by_location_class": _count(c.location_class for c in shown),
        "by_safety": _count(c.safety for c in shown),
        "by_creating_tool": _count(c.creating_tool for c in shown),
        "by_repo": _count(c.owner_repo or "(no github remote)" for c in shown),
        "by_kind": _count(c.kind for c in shown),
        "by_pr_state": _count(c.pr_state for c in shown),
        "dropped_ball": sum(1 for c in shown if c.dropped_ball is True),
        "dropped_ball_unknown": sum(1 for c in shown if c.dropped_ball is None),
        "forbidden_tmp": sum(1 for c in shown if c.location_class == "FORBIDDEN_TMP"),
        "tool_cache": sum(1 for c in shown if c.tool_cache),
        "anomalies_by_type": _count(a["type"] for a in anomalies),
    }


# --------------------------------------------------------------------------- text output

def _tilde(path: str, home: str) -> str:
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _stamp(iso: str) -> str:
    """Local time, 12-hour, no zone name: Wed, Oct 7, 11:16am."""
    try:
        dt = _dt.datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return iso
    hour = dt.hour % 12 or 12
    return f"{dt.strftime('%a, %b')} {dt.day}, {hour}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def _table(title: str, counts: Mapping[str, int], limit: int | None = None) -> list[str]:
    if not counts:
        return []
    items = list(counts.items())
    rest = items[limit:] if limit else []
    items = items[:limit] if limit else items
    width = max(len(k) for k, _ in items)
    lines = [title] + [f"  {k.ljust(width)}  {v:>4}" for k, v in items]
    if rest:
        lines.append(f"  ... {len(rest)} more ({sum(v for _, v in rest)} checkouts); see the JSON")
    return lines + [""]


def _row(co: dict, home: str) -> list[str]:
    bits = [co["kind"], co["location_class"], co["branch"] or f"(detached {co['detached_sha']})"]
    if co["tool_cache"]:
        bits.append("[tool cache]")
    facts = []
    if co["dirty_tracked"] is not None:
        facts.append(f"dirty {co['dirty_tracked']}t/{co['dirty_untracked']}u")
    if co["unpushed"]:
        facts.append(f"unpushed {co['unpushed']}")
    facts.append(f"PR {co['pr_state']}" + (f" #{co['pr_number']}" if co["pr_number"] else ""))
    facts.append(f"tool {co['creating_tool']}")
    lines = [f"  {_tilde(co['path'], home)}", f"      {'  '.join(bits)}", f"      {'; '.join(facts)}"]
    lines += [f"      - {r}" for r in co["safety_reasons"]]
    return lines


def render_text(report: Mapping) -> str:
    """The summary tables, then NEEDS-REVIEW and anomaly sections, most urgent first."""
    home = report["home"]
    s = report["summary"]
    out = [f"Lane doctor  {_stamp(report['generated_at'])}  {report['host']}  layout {report['layout_mode']}  "
           f"checkouts {s['total']}" + (f" of {s['total_found']}" if s["total"] != s["total_found"] else ""), ""]
    if report["warnings"]:
        out += [f"warning: {w}" for w in report["warnings"]] + [""]
    if report["lsof"] != "ok":
        out += ["lsof unavailable: ACTIVE cannot be determined, so clean checkouts report UNKNOWN", ""]
    out += _table("Location class", s["by_location_class"])
    out += _table("Safety class", s["by_safety"])
    out += _table("Creating tool", s["by_creating_tool"])
    out += _table("Repo", s["by_repo"], limit=15)
    out += [f"Dropped ball {s['dropped_ball']} (PR state unknown for {s['dropped_ball_unknown']})   "
            f"Forbidden tmp {s['forbidden_tmp']}   Tool caches {s['tool_cache']}   "
            f"Strict violations {s['strict_violations']}", ""]
    cos = report["checkouts"]
    dropped = [c for c in cos if c["dropped_ball"] is True]
    tmp = [c for c in cos if c["location_class"] == "FORBIDDEN_TMP" and c not in dropped]
    review = [c for c in cos if c["safety"] == "NEEDS-REVIEW" and c not in dropped and c not in tmp]
    unknown = [c for c in cos if c["safety"] == "UNKNOWN" and c not in tmp]
    for title, items in (("DROPPED BALL: dirty or unpushed, no open PR", dropped),
                         ("FORBIDDEN TMP: checkouts in temp directories", tmp),
                         ("NEEDS-REVIEW", review), ("UNKNOWN", unknown)):
        if items:
            out.append(f"{title} ({len(items)})")
            for c in items:
                out += _row(c, home)
            out.append("")
    if report["anomalies"]:
        out.append(f"ANOMALIES ({len(report['anomalies'])})")
        for a in report["anomalies"]:
            out.append(f"  {a['type']}  {_tilde(a['path'], home)}  {a['detail']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- command line

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # argparse exits 2 on usage errors, which would collide with the --strict exit code.
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def _write_atomic(path: str, text: str) -> None:
    target = Path(path)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         clock: Callable[[], _dt.datetime] | None = None, run: Callable[..., CmdResult] = run_cmd,
         gh_prs: Callable[[str], list[dict] | None] | None = None,
         lsof_cwds: Callable[[], list[tuple[int, str, str]] | None] | None = None,
         cwd: str | None = None, stdout=None) -> int:
    """CLI entry.  Exit 0 normally, 2 with --strict when a FORBIDDEN_TMP, FORBIDDEN_CODE_TOPLEVEL
    or UNSANCTIONED checkout exists, 64 for a usage error."""
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    p = _Parser(prog="fleet_lanes.doctor", description="Find every git checkout on this Mac.  Report only.")
    p.add_argument("--json", action="store_true", help="print the JSON report instead of text")
    p.add_argument("--write", metavar="PATH", help="also write the JSON report here (atomic)")
    p.add_argument("--sizes", action="store_true", help="measure size with du (slow)")
    p.add_argument("--deep", action="store_true", help="also scan ~/Documents, ~/Desktop, ~/Downloads")
    p.add_argument("--no-gh", action="store_true", help="skip PR lookups (PR state becomes UNKNOWN)")
    p.add_argument("--gh-limit", metavar="N", type=int, default=GH_LIMIT,
                   help=f"PRs fetched per repo (default {GH_LIMIT}, max 1000); a list that fills the limit "
                        "cannot prove a branch has no PR, so unmatched checkouts report UNKNOWN")
    p.add_argument("--home", metavar="PATH", help="treat PATH as the home directory (tests)")
    p.add_argument("--tmp-root", metavar="PATH", action="append",
                   help="scan this temp root instead of the real ones (repeatable; tests)")
    p.add_argument("--strict", action="store_true", help="exit 2 if any forbidden or unsanctioned checkout exists")
    p.add_argument("--only-class", metavar="CLASS", type=str.upper,
                   choices=[str(c) for c in L.LocationClass], help="show only this location class")
    args = p.parse_args(argv)
    if not 1 <= args.gh_limit <= 1000:
        p.error("--gh-limit must be between 1 and 1000")
    home = args.home or env.get("HOME") or os.path.expanduser("~")
    report = build_report(
        home, env, cwd=cwd if cwd is not None else os.getcwd(), tmp_scan_roots=args.tmp_root,
        deep=args.deep, sizes=args.sizes, use_gh=not args.no_gh, run=run, gh_prs=gh_prs,
        lsof_cwds=lsof_cwds, clock=clock, only_class=args.only_class, gh_limit=args.gh_limit)
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.write:
        _write_atomic(args.write, text)
    stdout.write(text if args.json else render_text(report))
    return 2 if args.strict and report["summary"]["strict_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
