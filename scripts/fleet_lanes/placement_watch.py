#!/usr/bin/env python3
"""Scheduled placement watcher (AFC-618).  It reports misplaced checkouts and never moves or deletes anything.

    python3 -m fleet_lanes.placement_watch [--apply] [--seat CLAUDE] [--doctor-report PATH] [--verify-hooks]

Without --apply it prints what it would file and changes no state.  One run does this:

  1. Inputs.  A doctor report (`lane ls --write`; the Mac's Maintain step writes one) when it is under six hours old and
     carries `placement_anomalies`;  otherwise its own sweep through doctor.build_report without gh.  The second input is
     the event log the git shim and the hooks append to (placement.py), read from where the last run stopped.
  2. Findings.  Everything in placement_anomalies, each detect-mode event, and any hook whose command no longer exists.
     With --verify-hooks, also the failures of `install_tools verify` for the platforms that are safe to probe.
  3. Dedupe.  The state file remembers each path and problem it has reported, until a clean sweep no longer sees it, so a checkout
     fixed and broken again is told again.  A path on a disk that is not mounted or cannot be read was not seen either way, so it stays
     remembered.  One rolling Linear issue per rule class per host, never per path or per creator, with every row of the batch in its
     body, a comment for each later batch of new findings, and one post in the issue's topic when it is filed.  If that issue has since
     been closed, the next batch gets a new one.  Every finding remembered has reached Linear in a body or a comment.  A run with more
     than MAX_NEW_PER_RUN new findings looks like a renamed mount or a wrong root, not a leak, so it sends nothing and says so until a
     person passes --accept-burst.
  4. The owner hears only about one creator's leak in one repo that keeps growing after seven days, or a clone in the Code root holding
     unpushed work on any branch or in a stash (not a harness worktree inside one, which is in-flight work with an issue already, and not
     a clone whose only such work is local branches past 30 days, which looks just like old squash-merged branches and stays in the
     issue).  One message for each new set of rows, whatever its size, and at most one a day:  rows that arrive after it wait for the
     next.  A send whose outcome is unknown counts as sent, because only a proven refusal is safe to send again.  A filing retries every
     run, since the board dedupes it on its uid, and a filing the board deduped goes in as a comment, because the board keeps the old
     text.

A lost or corrupt state file holds all sending, since everything would otherwise look new, until --reset-state.  Who has been told
lives in its own small file as well, so a reset does not message the owner twice.  An unusable exceptions file holds every placement
finding, since each excused checkout would look misplaced, and files only the problem with that file.

Public text uses root tokens ({home}, {code}, {apps}, {lanes}), never a real path, since a server path is private
inventory.  Exit 0 when it ran;  3 when its inputs are stale or unreadable, the exceptions file among them, or `install_tools verify`
could not run;  4 when filing failed (it retries next run);  5 when it ran but an external root refused to be read, as launchd meets a
disk it has no permission for, so the result is partial and not clean (`coverage` in the status file).
The status file beside the state file lets `fleet-tools-doctor` and Muster see a watcher that has gone quiet.  A run without --apply
writes it too, with `held` true, so a watcher held in dry run shows up instead of leaving no row at all.  It leaves the recent
status of a run meant to act alone, so a dry run by hand neither calls that watcher held nor hides its errors.  It lists no
processes (no `ps`).  The seat is CLAUDE until the owner chooses another.  Python 3.9 safe.
Tests: fleet_lanes/tests/test_placement_watch.py.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import glob
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from fleet_lanes import doctor as D
    from fleet_lanes import layout as L
    from fleet_lanes import placement as P
except ImportError:  # run as a file, by launchd or systemd
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from fleet_lanes import doctor as D
    from fleet_lanes import layout as L
    from fleet_lanes import placement as P

SG = "  "  # the fleet's sentence gap, in everything a person reads
REPORT_MAX_AGE_HOURS = 2.0
REPORT_MAX_FUTURE_S = 300
MAX_NEW_PER_RUN = 25
REPEAT_LEAK_DAYS = 7
DEFAULT_SEAT = "CLAUDE"
STATE_NAME = "placement-watch.json"
STATUS_NAME = "placement-watch-status.json"
TOLD_NAME = "placement-watch-told.json"
REPORT_NAME = "placement-report.json"
VERIFY_TARGETS = ("claude", "codex", "cursor", "grok", "minimax", "opencode", "antigravity")
MAINTAIN_REPORT = "Library/Application Support/HogHunter/Maintain/lane-doctor-report.json"
EXIT_OK, EXIT_STALE, EXIT_FILING, EXIT_UNREADABLE = 0, 3, 4, 5
COVERAGE_FULL, COVERAGE_UNREADABLE = "full", "external-unreadable"
STATUS_FRESH_HOURS = 3  # as long as fleet-tools-doctor gives a watcher before it calls it stale
BEARER_FILE = "SIMPLE_BOARD_BEARER_FILE"
# What a send can come to.  Only a refusal the CLI made before sending anything is safe to repeat.
OK, REFUSED, UNKNOWN = "ok", "refused", "unknown"
# The two CLIs number their exits differently (linear-board/board EXIT_*, simple_tools/zulip.py exit_code).
REFUSED_EXITS = {
    # Bad usage, an untrusted seat helper, no credentials, a rate limit that outlasted the wait, no verified seat.
    "simple-board": frozenset({2, 3, 4, 5, 6}),
    # Bad usage, a credential or DM-guard refusal.  Its 6 is a network error, which is a refusal only as described below.
    "simple-chat": frozenset({2, 3}),
}
# simple-chat exits 6 for any network error, and adds this to its message when the request may have reached Zulip.
CHAT_NETWORK_EXIT, CHAT_MAYBE_SENT = 6, "not retried"
LINES_PER_BODY = 40
DM_MAX_CHARS = 9000  # under Zulip's 10,000
OWNER_DM_HOURS = 24
MAX_GENERATIONS = 16
# Judged against the exception rows, so none of them means anything while those rows cannot be read.
_JUDGED_BY_ROWS = frozenset({"MISPLACED", "SHIM-EVENT", "MAIN-CLONE", "TEMP-BUILD", "TEMP-BUILD-EVENT"})
CLOSED_STATUSES = frozenset({"completed", "deployed", "wontfix", "duplicate", "canceled"})
_REPO_LOCAL_WORKTREES = ("/.claude/worktrees/", "/.muse/worktrees/")
_ISSUE_ID = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


# One rolling issue per rule class:  a reader acts on the rule, and the rows say who and where.
RULE_CLASSES = {
    "MISPLACED": "misplaced", "SHIM-EVENT": "misplaced", "MAIN-CLONE": "main-clone", "CODE-WORK": "code-work",
    "EXPIRED-EXCEPTION": "exceptions", "EXPIRING-EXCEPTION": "exceptions", "EXCEPTIONS-UNUSABLE": "exceptions-file",
    "MODE-FILE": "mode-file", "TEMP-BUILD": "temp-build", "TEMP-BUILD-EVENT": "temp-build",
}
# Temp build trees (the temp-build class):  a tree this big and this idle is reported, never removed.
TEMP_TREE_MIN_BYTES = 1 << 30
TEMP_TREE_MIN_AGE_HOURS = 24
ENV_TEMP_SCAN_ROOTS = "FLEET_TEMP_BUILD_SCAN_ROOTS"  # colon-separated folders to scan instead of the host's temp roots
# A name at the top of a tree, or one level down, that says a build or a checkout made it.
_BUILD_MARKS = frozenset({"Build", "Logs", "ModuleCache.noindex", "SourcePackages", "info.plist", ".build", "target",
                          "node_modules", "Package.swift", "Cargo.toml", "package.json", ".git", "DerivedData", "build",
                          "dist", "Intermediates.noindex", "Products", "CACHEDIR.TAG"})


@dataclass(frozen=True)
class Finding:
    path: str  # a real path, kept only in the private state file
    public: str  # the same path with root tokens, safe to post
    creator: str
    repo: str
    kind: str
    detail: str
    unpushed_in_code: bool = False

    @property
    def group(self) -> str:
        return RULE_CLASSES.get(self.kind, "hook-coverage")

    @property
    def leak(self) -> str:
        """Who is leaking where, which is what a leak's seven days count from, now that one issue holds them all."""
        return f"{self.creator}|{self.repo}"

    @property
    def keys(self) -> tuple[str, ...]:
        """What a report remembers.  A misplaced checkout is one fact whichever input saw it and whoever seems to have made it, and a
        main clone's problems are separate facts, so a new one on the same clone is news."""
        if self.kind in ("MISPLACED", "SHIM-EVENT"):
            return (f"{self.path}|misplaced",)
        if self.kind == "MAIN-CLONE":
            return tuple(f"{self.path}|main-clone|{item.strip()}" for item in self.detail.split(",") if item.strip()) or (self.path,)
        return (f"{self.path}|{self.kind}",)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def public_path(path: str, roots: L.Roots) -> str:
    """`path` with its root replaced by a token, most specific root first."""
    pairs = [(roots.lanes_root, "{lanes}"), (roots.code_root, "{code}"), (roots.apps_root, "{apps}")]
    pairs += [(root, "{lanes}") for root in roots.external_lanes_roots]
    pairs.append((roots.home, "{home}"))
    for root, token in pairs:
        text = os.fspath(root)
        if path == text or path.startswith(text + "/"):
            return token + path[len(text):]
    # Anywhere else may be private inventory, and the name alone is enough to find it from the state file.
    return "{outside}/" + os.path.basename(path.rstrip("/"))


def creator_of(path: str, co: Mapping[str, Any], registry: L.Registry) -> str:
    """Who made this checkout, from the place first, then the branch, then the doctor's guess."""
    low = path.replace(os.sep, "/").casefold()
    for mark, name in (("/.muse/worktrees/", "muse"), ("/.claude/worktrees/", "claude"), ("/.codex/", "codex"),
                       ("/.grok/", "grok"), ("/.cursor/", "cursor"), ("/.gemini/", "antigravity"),
                       ("/.botfleet/", "botfleet"), ("/.zcode/", "zcode"), ("/conductor/", "conductor")):
        if mark in low:
            return name
    branch = co.get("branch")
    if isinstance(branch, str) and branch:
        seat = L.seat_from_branch(branch, registry)
        if seat:
            return seat.lower()
        if branch.split("/", 1)[0].casefold().startswith("bf"):
            return "botfleet"
    tool = co.get("creating_tool")
    return str(tool).lower() if tool and tool != "unknown" else "unknown"


def _repo_of(co: Mapping[str, Any]) -> str:
    owner_repo = co.get("owner_repo")
    if isinstance(owner_repo, str) and "/" in owner_repo:
        return owner_repo.split("/", 1)[1]
    return "unknown-repo"


def findings_from_report(report: Mapping[str, Any], roots: L.Roots, registry: L.Registry) -> list[Finding]:
    by_path = {c.get("path"): c for c in report.get("checkouts", ()) if isinstance(c, Mapping)}
    code = os.fspath(roots.code_root) + "/"
    out: list[Finding] = []
    for item in report.get("placement_anomalies", ()):
        path, kind, detail = item.get("path", ""), item.get("type", ""), str(item.get("detail") or "")
        co = by_path.get(path, {})
        if kind == "MAIN-CLONE":
            creator, repo = "main-clone", os.path.basename(path.rstrip("/"))
        elif kind == "EXPIRED-EXCEPTION":
            creator, repo = "exceptions", "AI-Fleet-Coordinator"
        elif kind == "EXCEPTIONS-UNUSABLE":
            creator, repo = "exceptions-file", "AI-Fleet-Coordinator"
        else:
            creator, repo = creator_of(path, co, registry), _repo_of(co)
        real = str(co.get("realpath") or path)
        harness = any(mark in f"{path}/" for mark in _REPO_LOCAL_WORKTREES)
        unpushed = bool(co.get("unpushed")) and not harness and (path.startswith(code) or real.startswith(code))
        out.append(Finding(path, public_path(path, roots), creator, repo, kind, detail, unpushed))
    for item in report.get("placement_warnings", ()):
        path, detail = str(item.get("path", "")), str(item.get("detail") or "")
        if item.get("type") == "CODE-WORK":
            # One issue for all of them, because a Code clone with old branches is the usual case and not a leak per repo.
            out.append(Finding(path, public_path(path, roots), "code-work", "AI-Fleet-Coordinator", "CODE-WORK", detail,
                               item.get("old_branches_only") is not True))
            continue
        creator = "mode-file" if item.get("type") == "MODE-FILE" else "exceptions"
        out.append(Finding(path, path, creator, "AI-Fleet-Coordinator", str(item.get("type", "")), detail))
    return out


def mode_findings(home: str, env: Mapping[str, str]) -> list[Finding]:
    """What the mode file says that nothing can act on, read live:  a stale report must not hide that a block is not on."""
    return [Finding(f"mode-file:{problem}", f"mode-file:{problem}", "mode-file", "AI-Fleet-Coordinator", "MODE-FILE",
                    "that line of the lane-guard mode file does nothing") for problem in P.read_mode(home, env).problems]


def _creator_from_seat(seat: str, registry: L.Registry | None) -> str:
    """The same word creator_of gets from a branch, so a seat's tag and its branch prefix are one creator."""
    known = registry.seat_by_name(seat) if registry is not None and seat else None
    return (known.suffix if known is not None else seat or "unknown").lower()


def findings_from_events(events: Iterable[Mapping[str, Any]], since: str, roots: L.Roots, *, host: str = "",
                         registry: L.Registry | None = None) -> tuple[list[Finding], str]:
    """Findings from the event log at or after `since`, and the new high-water mark.  Temp and Code hits count too.  Stamps are whole
    seconds, so the mark's own second is read again, and what was already reported is dropped later by its key."""
    out: list[Finding] = []
    newest = since
    for ev in events:
        at = str(ev.get("at") or "")
        if not at or at < since:
            continue
        newest = max(newest, at)
        if ev.get("action") == "fail-open":
            detail = str(ev.get("detail") or "unknown")
            shim = "build shim" if ev.get("via") == "build-shim" else "git shim"
            out.append(Finding(f"fail-open:{detail}", f"{shim}, {detail}", "hook-coverage", host or P.host_kind(), "FAIL-OPEN",
                               "the shim let a command through without judging it"))
            continue
        dest = str(ev.get("dest") or "")
        if ev.get("action") not in ("detect", "block") or not dest or ev.get("exceptions") == "unusable":
            continue
        expanded = os.path.expanduser(dest) if dest.startswith("~") else dest
        creator = _creator_from_seat(str(ev.get("seat") or ""), registry)
        repo = str(ev.get("source") or "unknown-repo").rsplit("/", 1)[-1]
        kind = "TEMP-BUILD-EVENT" if ev.get("class") == P.CLASS_TEMP_BUILD else "SHIM-EVENT"
        public = temp_public_path(expanded) if kind == "TEMP-BUILD-EVENT" else public_path(expanded, roots)
        out.append(Finding(expanded, public, creator, repo, kind, f"{ev.get('action')} {ev.get('class')} via {ev.get('via')}"))
    return out, newest


def temp_public_path(path: str) -> str:
    """A temp path as it may be posted:  `{temp}/` and the top folder's name, which is enough to find it from the state file."""
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part in ("tmp", "T") and i + 1 < len(parts):
            return "{temp}/" + "/".join(parts[i + 1:i + 2])
    return "{temp}/" + os.path.basename(path.rstrip("/"))


def temp_scan_roots(env: Mapping[str, str]) -> list[str]:
    """The temp folders whose top-level trees the watcher measures:  the override when set, none in a test run without one (the
    suite must not walk the real temp folders), and otherwise /private/tmp (or /tmp), /var/tmp and TMPDIR, each once."""
    raw = env.get(ENV_TEMP_SCAN_ROOTS)
    if raw is not None:
        candidates = [p for p in raw.split(":") if p]
    elif P._test_run(env):
        return []
    else:
        candidates = ["/tmp", "/var/tmp", (env.get("TMPDIR") or "").strip()]
    out: list[str] = []
    for cand in candidates:
        if not cand.startswith("/") or not os.path.isdir(cand):
            continue
        real = os.path.realpath(cand)
        if real not in out:
            out.append(real)
    return out


def _tree_facts(top: str, *, stop_bytes: int, max_entries: int = 200_000) -> tuple[int, float, bool]:
    """(bytes on disk, counted only up to stop_bytes;  the newest mtime of the top and the two levels under it;  whether a build mark
    is at the top or one level down).  It never follows a symlink or leaves the device."""
    try:
        dev = os.lstat(top).st_dev
    except OSError:
        return 0, 0.0, False
    total, newest, marked, seen = 0, 0.0, False, 0
    stack = [(top, 0)]
    while stack and seen < max_entries:
        here, depth = stack.pop()
        try:
            it = os.scandir(here)
        except OSError:
            continue
        with it:
            for entry in _entries(it):
                if seen >= max_entries:
                    break
                seen += 1
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if depth <= 1:
                    marked = marked or entry.name in _BUILD_MARKS
                if depth <= 2:
                    newest = max(newest, st.st_mtime)
                if entry.is_dir(follow_symlinks=False):
                    if st.st_dev == dev and (total < stop_bytes or depth < 2):
                        stack.append((entry.path, depth + 1))
                elif total < stop_bytes:
                    total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total, newest, marked


def _entries(it):
    """Stream a scandir iterator, ending quietly when a folder vanishes or turns unreadable mid-listing."""
    while True:
        try:
            entry = next(it)
        except StopIteration:
            return
        except OSError:
            return
        yield entry


def temp_build_findings(roots: Sequence[str], now: _dt.datetime, *, min_bytes: int = TEMP_TREE_MIN_BYTES,
                        min_age_hours: float = TEMP_TREE_MIN_AGE_HOURS, exceptions=(), host: str = "",
                        layout_roots: L.Roots | None = None) -> list[Finding]:
    """Build trees and checkouts at the top of a temp folder that hold at least `min_bytes` and that nothing has touched for
    `min_age_hours`.  The session scratchpad (claude-<uid>) and anything an in-date temp-build or package-temp row covers are
    passed over.  It reports;  it never removes."""
    out: list[Finding] = []
    limit = now.timestamp() - min_age_hours * 3600
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if re.fullmatch(r"claude-\d+", name) or os.path.islink(path) or not os.path.isdir(path):
                continue
            if exceptions and layout_roots is not None:
                hit = P.find_exception([path], layout_roots, exceptions, host=host or P.host_kind(), today=now.date())
                if hit is not None and not hit.expired and P.row_excuses_build(hit.row):
                    continue
            size, newest, marked = _tree_facts(path, stop_bytes=min_bytes)
            if not marked or size < min_bytes or not newest or newest > limit:
                continue
            idle_days = int((now.timestamp() - newest) // 86400)
            out.append(Finding(path, "{temp}/" + name, "temp-build", "AI-Fleet-Coordinator", "TEMP-BUILD",
                               f"at least {size / (1 << 30):.1f} GB, untouched for {idle_days} day(s)"))
    return out


def _hook_nodes(rel: str, data: Any) -> Any:
    """The part of a config that holds hooks.  A settings file holds other `command` keys (an MCP server, a status line), which are
    not guards, and only a file that is all hooks is walked whole."""
    if isinstance(data, Mapping) and "hooks" in data:
        return data["hooks"]
    return data if "/hooks/" in rel or rel.endswith("hooks.json") else None


def _commands_in(node: Any) -> Iterable[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                yield value
            else:
                yield from _commands_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _commands_in(value)


HOOK_CONFIGS = (
    ".claude/settings.json",
    ".codex/hooks.json",
    ".cursor/hooks.json",
    ".grok/hooks/*.json",
    ".minimax/plugins/*/hooks/hooks.json",
    ".gemini/settings.json",
    ".local/share/muse/plugins/cache/local/*/*/package/.muse-plugin/plugin.json",
    ".local/share/muse/plugins/cache/local/*/*/package/hooks/*.json",
)


_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "dash", "node", "ruby", "perl", "deno", "bun"})
_INLINE_FLAGS = frozenset({"--eval", "-"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _home_expanded(word: str, home: str) -> str:
    if word == "~" or word.startswith("~/"):
        return home + word[1:]
    for var in ("$HOME", "${HOME}"):
        if word == var or word.startswith(var + "/"):
            return home + word[len(var):]
    return word


def _dead_part(command: str, home: str) -> str | None:
    """The path this hook command needs that is not there, or None.  That is the program, and the script when the program is an
    interpreter, because `python3 /gone/guard.py` fails as silently as `/gone/guard`.  A word with any other variable is not judged."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    while words and (_ASSIGNMENT.match(words[0]) or os.path.basename(words[0]) == "env"):
        words = words[1:]
    if not words:
        return None
    first = _home_expanded(words[0], home)
    if first.startswith("/") and "$" not in first and not os.access(first, os.X_OK):
        return first
    if os.path.basename(first) not in _INTERPRETERS:
        return None
    for word in words[1:]:
        # Code on the command line, or a module, including in a cluster such as `bash -lc`.
        if word in _INLINE_FLAGS or (word[:1] == "-" and word[1:2] != "-" and set(word[1:]) & {"c", "e", "m"}):
            return None
        if not word.startswith("-"):
            script = _home_expanded(word, home)
            return script if script.startswith("/") and "$" not in script and not os.path.exists(script) else None
    return None


def dead_hook_commands(home: str, *, configs: Sequence[str] = HOOK_CONFIGS) -> list[tuple[str, str]]:
    """(config file, missing path) for each hook whose program or script is gone or cannot run.

    A hook that fails silently allows everything, so a moved path turns a guard off without a sound.  Only that one path is kept;
    nothing else in these files is read out.
    """
    out: list[tuple[str, str]] = []
    for rel in configs:
        for path in glob.glob(os.path.join(home, rel)):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            for command in _commands_in(_hook_nodes(rel, data)):
                dead = _dead_part(command, home)
                if dead:
                    out.append((path, dead))
    return sorted(set(out))


@dataclass(frozen=True)
class VerifyRow:
    """One FAIL row.  `detail` can name a real path, so it stays here and only `problem` leaves."""

    target: str
    name: str
    detail: str

    @property
    def row(self) -> str:
        return f"{self.target} {self.name}"

    @property
    def timed_out(self) -> bool:
        return "timed out" in self.detail

    @property
    def problem(self) -> str:
        if self.timed_out:
            return "probe timed out"
        return "hook missing" if _HOOK_MISSING.search(self.detail) else "hook failed its probe"


_FAIL_ROW = re.compile(r"^FAIL\s+(\S+)\s+(.*)$")
_NAME_THEN_DETAIL = re.compile(r"^(.{1,24}?)\s{2,}(.*)$")
_HOOK_MISSING = re.compile(r"\bno \S*\s?hook\b|does not exist|is missing|no usable hook", re.IGNORECASE)


def _verify_row(line: str) -> VerifyRow | None:
    """Check.line() pads a name to 24 columns, and a name can hold a space, so a whitespace split would merge rows."""
    match = _FAIL_ROW.match(line)
    if match is None:
        return None
    target, rest = match.groups()
    named = _NAME_THEN_DETAIL.match(rest)
    name, detail = named.groups() if named else (rest[:24].rstrip(), rest[24:].strip())
    return VerifyRow(target, name, detail)


def _verify_once(stable: str, targets: Sequence[str], run: Callable[..., Any], env: Mapping[str, str] | None) -> list[VerifyRow] | None:
    """`install_tools verify` FAIL rows for the platforms safe to probe, or None if it could not run.  Muse, Clutch and Kimi
    are left out:  their verify runs the tool's own validator, which is not proven read-only."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); targets = sys.argv[2:]; "
            "from fleet_lanes import install_tools as t; sys.exit(t.main(['verify', *targets]))")
    argv = [sys.executable, "-I", "-B", "-c", code, stable, *targets]
    try:
        res = run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=180, check=False,
                  env=dict(os.environ if env is None else env))
    except (OSError, subprocess.SubprocessError):
        return None
    rows = [row for row in map(_verify_row, (res.stdout or "").splitlines()) if row is not None]
    if res.returncode != 0 and not rows:
        return None  # it crashed, or could not import:  no FAIL rows here says nothing about the hooks
    return rows


def verify_rows(stable: str, targets: Sequence[str], run: Callable[..., Any] = subprocess.run,
                env: Mapping[str, str] | None = None) -> list[VerifyRow] | None:
    """What fails `install_tools verify` twice, or None if it could not run.  Under load, such as a doctor sweep alongside, a good hook
    misses its five seconds, so each platform with a failure is probed again, one at a time, and only a row that fails both times is
    reported.  A retry that cannot run proves nothing, so the first answer stands."""
    first = _verify_once(stable, targets, run, env)
    if not first:
        return first
    out: list[VerifyRow] = []
    for target in dict.fromkeys(row.target for row in first):
        wanted = [row for row in first if row.target == target]
        again = _verify_once(stable, (target,), run, env)
        if again is None:
            out += wanted
            continue
        still = {row.name: row for row in again if row.target == target}
        out += [still[row.name] for row in wanted if row.name in still]
    return out


def verify_failures(stable: str, targets: Sequence[str], run: Callable[..., Any] = subprocess.run,
                    env: Mapping[str, str] | None = None) -> list[str] | None:
    rows = verify_rows(stable, targets, run, env)
    return None if rows is None else [row.row for row in rows]


VERIFY_UNAVAILABLE = "VERIFY-UNAVAILABLE"
VERIFY_TIMEOUT = "VERIFY-TIMEOUT"


def hook_findings(home: str, roots: L.Roots, host: str, stable: str | None, *, run=subprocess.run) -> list[Finding]:
    out = [
        Finding(f"hook:{cmd}", public_path(cmd, roots), "hook-coverage", host, "DEAD-HOOK",
                f"the command in {public_path(path, roots)} cannot run, so that guard is off")
        for path, cmd in dead_hook_commands(home)
    ]
    if stable:
        failures = verify_rows(stable, VERIFY_TARGETS, run=run)
        if failures is None:
            out.append(Finding("verify:unavailable", "install_tools verify", "hook-coverage", host, VERIFY_UNAVAILABLE,
                               "it could not run, so the hooks are unverified"))
        for row in failures or ():
            kind = VERIFY_TIMEOUT if row.timed_out else "VERIFY-FAIL"
            out.append(Finding(f"verify:{row.row}", row.row, "hook-coverage", host, kind, f"install_tools verify: {row.problem}, twice"))
    return out


def load_report(path: str, now: _dt.datetime, max_age_hours: float) -> dict | None:
    """A report young enough to trust, that this version of the doctor wrote, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
        generated = _dt.datetime.fromisoformat(report["generated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=_dt.timezone.utc)
    age = (now - generated).total_seconds()
    if age > max_age_hours * 3600 or age < -REPORT_MAX_FUTURE_S or "placement_anomalies" not in report:
        return None
    return report


def _empty_state() -> dict:
    return {"version": 1, "groups": {}, "events_since": "", "dm_paths": [], "repeat_pending": {}, "last_owner_dm": ""}


def _shape_problem(state: Any) -> bool:
    """Whether a parsed state file is not what this code writes, all the way down, so a bad inner value holds the sends and
    --reset-state can clear it, where it would otherwise crash every run."""
    if not isinstance(state, dict) or not isinstance(state.get("groups"), dict):
        return True
    for group in state["groups"].values():
        if not isinstance(group, dict) or not isinstance(group.get("paths", {}), dict):
            return True
        if not isinstance(group.get("issue"), (str, type(None))) or not isinstance(group.get("first_seen", ""), str):
            return True
        if not isinstance(group.get("leaks", {}), dict):
            return True
        if type(group.get("generation", 0)) is not int:
            return True
    dm_paths = state.get("dm_paths", [])
    return not (isinstance(state.get("events_since", ""), str) and isinstance(dm_paths, list)
                and all(isinstance(p, str) for p in dm_paths) and isinstance(state.get("repeat_pending", {}), dict)
                and isinstance(state.get("last_repeat_dm", ""), str) and isinstance(state.get("last_owner_dm", ""), str))


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_state(path: str) -> dict:
    state = _read_json(path)
    return _empty_state() if _shape_problem(state) else {**_empty_state(), **state}


def _state_was_written(state_path: str, status_path: str) -> bool:
    """Whether this host has ever had a state file.  A run that sent nothing writes a status file and no state, and that is no lost state."""
    status = _read_json(status_path)
    return os.path.exists(state_path) or (isinstance(status, dict) and status.get("state_written") is True)


def state_problem(state_path: str, status_path: str) -> str | None:
    """Why the state file cannot be trusted, or None.  With no memory every finding looks new, so a lost file must not re-send them."""
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return "the state file is gone but the status file says it was written before" if _state_was_written(state_path, status_path) else None
    except (OSError, ValueError) as exc:
        return f"the state file is unreadable ({type(exc).__name__})"
    return "the state file has the wrong shape" if _shape_problem(state) else None


def _write_json(path: str, data: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".placement-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_status(path: str, status: Mapping[str, Any], acting: bool, now: _dt.datetime) -> None:
    """A dry run does not replace the recent status of a run that was meant to act, whether it worked or failed:  it would call that
    watcher held and hide its errors.  Once that status is stale the watcher is gone or has been held, so the dry run takes over and
    keeps when the last acting run was, so a watcher that stopped still reads as stopped."""
    if acting:
        _write_json(path, {**status, "acting": True})
        return
    prior = _read_json(path)
    prior = prior if isinstance(prior, dict) else {}
    last = prior.get("at") if prior.get("acting") is True else prior.get("last_acting_run")
    if prior.get("acting") is True:
        try:
            if (now - _dt.datetime.fromisoformat(str(last))).total_seconds() < STATUS_FRESH_HOURS * 3600:
                return
        except (TypeError, ValueError):
            pass
    _write_json(path, {**status, "acting": False, "last_acting_run": last if isinstance(last, str) else ""})


def _nothing_sent(tool: str, code: int, stderr: str) -> bool:
    """Whether this exit proves the CLI sent nothing."""
    if code in REFUSED_EXITS.get(tool, ()):
        return True
    return tool == "simple-chat" and code == CHAT_NETWORK_EXIT and CHAT_MAYBE_SENT not in stderr


class Filer:
    """Linear and Zulip through the fleet CLIs, as the seat itself.  Bodies go through 0600 files, as the CLIs ask."""

    def __init__(self, seat: str, session: str, env_label: str, run: Callable[..., Any] = subprocess.run) -> None:
        self.seat, self.session, self.env_label, self.run = seat, session, env_label, run
        self.errors: list[str] = []
        self.outcome = OK  # of the last call
        self.deduped = False  # whether the last filing found the uid already filed, which leaves that issue's text as it was

    def _call(self, argv: list[str], body: str | None = None, body_flag: str = "--body-file") -> str | None:
        path = None
        self.outcome = OK
        try:
            if body is not None:
                fd, path = tempfile.mkstemp(prefix="placement-body-")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body)
                argv = [*argv, body_flag, path]
            res = self.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            # A CLI that never started sent nothing.  One that timed out may have.
            self.outcome = REFUSED if isinstance(exc, OSError) else UNKNOWN
            self.errors.append(f"{argv[0]} {argv[1]}: {type(exc).__name__}")
            return None
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if res.returncode != 0:
            detail = D.redact_text((res.stderr or "").strip())[:200]
            self.outcome = REFUSED if _nothing_sent(argv[0], res.returncode, res.stderr or "") else UNKNOWN
            self.errors.append(f"{argv[0]} {argv[1]}: exit {res.returncode}: {detail}")
            return None
        return res.stdout or ""

    def file_issue(self, title: str, body: str, uid: str) -> str | None:
        out = self._call(["simple-board", "file", "--title", title, "--app", "AFC", "--severity", "P2",
                          "--by", self.seat, "--env", self.env_label, "--kind", "agent-report",
                          "--session", self.session, "--uid", uid], body, "--desc-file")
        self.deduped = False
        if out is None:
            return None
        self.deduped = "already filed" in out
        match = _ISSUE_ID.search(out)
        if match is None:
            self.errors.append("simple-board file: no issue id in its output")
        return match.group(0) if match else None

    def issue_is_closed(self, issue: str) -> bool:
        """Whether the issue has been closed.  A read that fails says nothing, so the issue counts as open and that is no error."""
        before = len(self.errors)
        out = self._call(["simple-board", "show", issue, "--json"])
        del self.errors[before:]
        try:
            return json.loads(out or "{}").get("status") in CLOSED_STATUSES
        except (ValueError, AttributeError):
            return False

    def comment(self, issue: str, text: str) -> str:
        self._call(["simple-board", "comment", issue, "--by", self.seat, "--session", self.session, "--text", text])
        return self.outcome

    def post(self, topic: str, body: str) -> str:
        self._call(["simple-chat", "post", "--as", self.seat, "--session", self.session, "--topic", topic[:60]], body)
        return self.outcome

    def dm_owner(self, body: str) -> str:
        self._call(["simple-chat", "dm", "--owner", "--as", self.seat, "--session", self.session], body)
        return self.outcome


def board_env(host: str) -> str:
    """The word `simple-board file --env` takes.  The board has Mac and Cloud, and a server host is its Cloud."""
    return "Mac" if host == P.HOST_MAC else "Cloud"


_TITLES = {
    "misplaced": "Misplaced Checkouts ({host})",
    "main-clone": "Main Clones Off Their Default Branch ({host})",
    "exceptions": "Placement Exceptions Expired Or Near Expiry ({host})",
    "exceptions-file": "Placement Exceptions File Unusable ({host})",
    "code-work": "Code Clones Holding Work No Remote Has ({host})",
    "mode-file": "Lane Guard Mode File Has Lines It Cannot Use ({host})",
    "hook-coverage": "Lane Guard Hooks Not Running ({host})",
    "temp-build": "Build Output Left In System Temp ({host})",
}


def _title(group: str, host: str) -> str:
    return _TITLES[group].format(host=host.title())


def _who(f: Finding) -> str:
    """An issue is per rule, so a misplaced row says who and where, which the rule alone does not."""
    return f"{f.creator} in {f.repo}, " if f.group == "misplaced" else ""


def _lines(items: Sequence[Finding], limit: int = LINES_PER_BODY) -> str:
    rows = [f"- `{D.redact_text(f.public)}`: {f.kind.lower()}, {_who(f)}{D.redact_text(f.detail)}" for f in items[:limit]]
    if len(items) > limit:
        rows.append(f"- and {len(items) - limit} more")
    return "\n".join(rows)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _out_of_sight(path: str, roots: L.Roots, unreadable: Sequence[str] = ()) -> bool:
    """Whether `path` lies on something not there right now:  a lanes symlink with no target, a disk that is not mounted, or a root the
    sweep could not read.  A sweep says nothing about such a path, and AGENTS.md says these do stop resolving on a remount.  A symlink
    into an unreadable root still resolves, so that is judged by where it leads."""
    if not path.startswith("/"):
        return False
    real = os.path.realpath(path)
    for root in unreadable:
        if any(_under(spelling, candidate) for spelling in (path, real) for candidate in (root, os.path.realpath(root))):
            return True
    for root in roots.external_lanes_roots:
        text = os.fspath(root)
        if (path == text or path.startswith(text + "/")) and not os.path.isdir(text):
            return True
    parts = path.split("/")
    if len(parts) > 2 and parts[1] == "Volumes" and not os.path.isdir(os.path.join("/Volumes", parts[2])):
        return True
    here = path
    while here not in ("/", ""):
        if os.path.islink(here) and not os.path.exists(here):
            return True
        here = os.path.dirname(here)
    return False


def _forget_resolved(state: dict, findings: Sequence[Finding], roots: L.Roots, unreadable: Sequence[str] = ()) -> int:
    """Drop what the latest full look no longer sees, so a checkout fixed and broken again is reported again.  Returns how many it kept
    only because it could not look."""
    current = {key for f in findings for key in f.keys}
    held = 0
    for group in state["groups"].values():
        kept = {}
        for key, stamp in group.get("paths", {}).items():
            if key in current:
                kept[key] = stamp
            elif _out_of_sight(key.split("|", 1)[0], roots, unreadable):
                kept[key] = stamp
                held += 1
        group["paths"] = kept
    return held


def _file_group(filer: Filer, group: dict, key: str, title: str, body: str, host: str, stamp: str) -> tuple[str | None, bool]:
    """(issue, whether that issue now holds `body`).  A uid the board has seen answers with the old issue and its old text, closed or
    not.  A closed one means moving to the next generation, and an open one means the findings still have to go in as a comment."""
    for _ in range(MAX_GENERATIONS):
        generation = int(group.get("generation", 0))
        uid = f"placement-watch:{host}:{key}" + (f":{generation}" if generation else "")
        issue = filer.file_issue(title, body, uid)
        if issue is None or not filer.deduped:
            return issue, issue is not None
        if not filer.issue_is_closed(issue):
            return issue, False
        group.update(generation=generation + 1, first_seen=stamp, leaks={})
    filer.errors.append(f"simple-board file: {MAX_GENERATIONS} closed issues in a row for {key}")
    return None, False


def apply_findings(findings: Sequence[Finding], state: dict, filer: Filer | None, *, host: str,
                   now: _dt.datetime, accept_burst: bool = False) -> dict[str, list[Finding]]:
    """Update `state` and act.  With no filer it only reports:  the new findings per group, nothing remembered."""
    stamp = now.isoformat(timespec="seconds")
    fresh: dict[str, list[Finding]] = {}
    known = {key for group in state["groups"].values() for key in group.get("paths", {})}
    # The same checkout can arrive twice with different guesses at who made it.  A named creator wins over `unknown`.
    for f in sorted(findings, key=lambda item: item.creator == "unknown"):
        new = [key for key in f.keys if key not in known]
        if new:
            known.update(new)
            fresh.setdefault(f.group, []).append(f)
    if filer is None:
        return fresh
    total = sum(len(items) for items in fresh.values())
    if total > MAX_NEW_PER_RUN and not accept_burst:
        filer.errors.append(f"burst: {total} new findings in one run, over {MAX_NEW_PER_RUN}, so nothing was filed.  "
                            "Check the mount and the roots, then run once with --accept-burst.")
        return fresh
    for key, items in sorted(fresh.items()):
        group = state["groups"].setdefault(key, {"first_seen": stamp, "issue": None, "paths": {}})
        title = _title(key, host)
        if group.get("issue") and filer.issue_is_closed(group["issue"]):
            group.update(issue=None, generation=int(group.get("generation", 0)) + 1, first_seen=stamp, leaks={})
        waiting = list(items)
        sent: list[Finding] = []
        if not group.get("issue"):
            body = (f"The placement watcher found these on the {host}.{SG}It never moves or deletes anything.{SG}"
                    f"Rules: docs/protocols/lane-map.md.\n\n{_lines(items, len(items))}\n")
            issue, holds = _file_group(filer, group, key, title, body, host, stamp)
            if issue is None:
                continue
            group["issue"] = issue
            if holds:
                # The issue holds these findings now, whatever becomes of the post.
                sent, waiting = waiting, []
                filer.post(f"{issue} {title}", f"repo: AI-Fleet-Coordinator{SG}{title}, filed as {issue}.\n\n{_lines(items, 15)}\n")
        for chunk in (waiting[i:i + LINES_PER_BODY] for i in range(0, len(waiting), LINES_PER_BODY)):
            if filer.comment(group["issue"], f"New since the last run:\n{_lines(chunk)}\n") == REFUSED:
                break
            sent += chunk
        leaks = group.setdefault("leaks", {})
        for f in items:
            leaks.setdefault(f.leak, stamp)
        for f in sent:
            for fingerprint in f.keys:
                group["paths"][fingerprint] = stamp
    _notify_owner(findings, fresh, state, filer, now)
    return fresh


def _fit_message(body: str) -> str:
    """Every row, unless Zulip would refuse the message for its length.  What is cut is counted, and those rows are all in the issues."""
    if len(body) <= DM_MAX_CHARS:
        return body
    kept: list[str] = []
    used, dropped = 0, 0
    for line in body.splitlines():
        if dropped or used + len(line) + 1 > DM_MAX_CHARS - 80:
            dropped += line.startswith("- ")
            continue
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept) + f"\n- and {dropped} more, all in the issues\n"


def _notify_owner(findings: Sequence[Finding], fresh: Mapping[str, list[Finding]], state: dict, filer: Filer,
                  now: _dt.datetime) -> None:
    """One message for everything new, and at most one a day (OWNER_DM_HOURS).  Unpushed work goes as soon as today's message is free, and
    a growing leak waits in `repeat_pending` for the same one, since each new path in an old leak would otherwise be a message an hour for
    as long as the issue stays open.  A path counts as told only once a message named it."""
    told = set(state.get("dm_paths", ()))
    urgent = list({f.path: f for f in findings if f.unpushed_in_code and f.path not in told}.values())
    pending: dict = state.setdefault("repeat_pending", {})
    # Only a misplaced checkout is a leak.  The other rules, a Code clone's old branches among them, stay in their issues.
    for key, items in fresh.items():
        if key != "misplaced":
            continue
        leaks = (state["groups"].get(key) or {}).get("leaks", {})
        for f in items:
            # A state file from before one issue per rule class kept this date on the leak's own group.
            before = (state["groups"].get(f.leak) or {}).get("first_seen", "")
            try:
                first = _dt.datetime.fromisoformat(before or leaks.get(f.leak, ""))
            except (TypeError, ValueError):
                continue
            if (now - first).days >= REPEAT_LEAK_DAYS and f.path not in told:
                pending.setdefault(f.path, _lines([f]))
    try:
        last = _dt.datetime.fromisoformat(state.get("last_owner_dm") or state.get("last_repeat_dm") or "")
    except ValueError:
        last = None
    if not urgent and not pending:
        return
    if last is not None and (now - last).total_seconds() < OWNER_DM_HOURS * 3600:
        return
    parts = []
    if urgent:
        parts.append(f"{len(urgent)} checkout(s) in the Code root hold unpushed work:\n{_lines(urgent, len(urgent))}")
    if pending:
        rows = "\n".join(pending[path] for path in sorted(pending))
        parts.append(f"{len(pending)} new misplaced checkout(s) from a leak open over {REPEAT_LEAK_DAYS} days:\n{rows}")
    if filer.dm_owner(_fit_message("From the placement watcher.\n\n" + "\n\n".join(parts) + "\n")) == REFUSED:
        return
    state["dm_paths"] = sorted(told | {f.path for f in urgent} | set(pending))
    pending.clear()
    state["last_owner_dm"] = now.isoformat(timespec="seconds")


def run_once(*, home: str, env: Mapping[str, str], apply: bool, seat: str, session: str, report_path: str | None,
             verify_hooks: bool, stable: str | None, now: _dt.datetime | None = None, build_report=None,
             filer: Filer | None = None, out=None, accept_burst: bool = False, reset_state: bool = False) -> int:
    now = now or _now()
    out = sys.stdout if out is None else out
    host = P.host_kind(env)
    state_root = P.state_dir(home, env)
    state_path = os.path.join(state_root, STATE_NAME)
    status_path = os.path.join(state_root, STATUS_NAME)
    errors: list[str] = []
    acting = apply or filer is not None
    try:
        registry = L.load_registry(env=env)
        roots = L.make_roots(home, env, registry=registry)
    except (OSError, ValueError, L.LayoutError) as exc:
        _write_status(status_path, {"at": now.isoformat(timespec="seconds"), "ok": False, "input": None, "host": socket.gethostname(),
                                    "findings": 0, "new": 0, "errors": [f"registry unreadable: {type(exc).__name__}"], "applied": False,
                                    "held": not acting, "state_written": _state_was_written(state_path, status_path)}, acting, now)
        return EXIT_STALE
    # Never this watcher's own last sweep:  an hourly job that read it back would look at the disk once every few hours.
    candidates = [report_path] if report_path else [os.path.join(home, MAINTAIN_REPORT)]
    report = source = None
    for cand in candidates:
        report = load_report(cand, now, REPORT_MAX_AGE_HOURS)
        if report is not None:
            source = cand
            break
    if report is None:
        try:
            report = (build_report or D.build_report)(home, env, cwd=None, use_gh=False)
            source = "sweep"
            if apply:
                _write_json(os.path.join(state_root, REPORT_NAME), report)
        except Exception as exc:  # noqa: BLE001 - reported through the status file and the exit code
            errors.append(f"sweep failed: {type(exc).__name__}")
    state = load_state(state_path)
    hold = state_problem(state_path, status_path) if acting else None
    if hold and reset_state:
        with contextlib.suppress(OSError):
            os.replace(state_path, f"{state_path}.reset-{now.strftime('%Y%m%dT%H%M%S')}")
        state, hold = _empty_state(), None
        # Who has been told outlives the reset, or the owner gets every message again.
        told = _read_json(os.path.join(state_root, TOLD_NAME))
        if isinstance(told, dict) and isinstance(told.get("dm_paths"), list):
            state["dm_paths"] = [p for p in told["dm_paths"] if isinstance(p, str)]
        if isinstance(told, dict) and isinstance(told.get("last_owner_dm"), str):
            state["last_owner_dm"] = told["last_owner_dm"]
    if hold:
        errors.append(f"{hold}, so nothing was sent.  Check it, then run once with --reset-state.")
    active = filer
    if active is None and apply and not hold:
        if env.get(BEARER_FILE):
            active = Filer(seat, session, board_env(host))
        else:
            errors.append(f"{BEARER_FILE} is not set, so nothing was filed.")
    findings: list[Finding] = []
    if report is not None:
        findings += findings_from_report(report, roots, registry)
    events, newest = findings_from_events(P.read_events(P.event_log_path(home, env)), state.get("events_since", ""), roots,
                                          host=host, registry=registry)
    findings += events
    findings += hook_findings(home, roots, host, stable if verify_hooks else None)
    findings += mode_findings(home, env)
    try:
        rows = P.load_exceptions(env=env)
        findings += temp_build_findings(temp_scan_roots(env), now, exceptions=rows, host=host, layout_roots=roots)
    except Exception as exc:  # noqa: BLE001 - a temp folder that cannot be read never stops the rest of the run
        errors.append(f"temp scan failed: {type(exc).__name__}")
    unverified = any(f.kind == VERIFY_UNAVAILABLE for f in findings)
    if unverified:
        errors.append("install_tools verify could not run, so the hooks are unverified.")
    # With no rows every excused checkout looks misplaced.  Report the file, and judge nothing against it until it is repaired.
    unusable = P.load_exceptions_reporting(env=env)[1]
    if unusable is None:
        unusable = next((f.detail for f in findings if f.kind == "EXCEPTIONS-UNUSABLE"), None)
    if unusable:
        findings = [f for f in findings if f.kind not in _JUDGED_BY_ROWS and f.kind != "EXCEPTIONS-UNUSABLE"]
        findings.append(Finding(f"exceptions:{P.EXCEPTIONS_NAME}", P.EXCEPTIONS_NAME, "exceptions-file", "AI-Fleet-Coordinator",
                                "EXCEPTIONS-UNUSABLE", unusable))
        errors.append(f"{unusable}, so no placement finding was judged.  Repair the file.")
    if hold:
        active = None
    fresh = apply_findings(findings, state, active, host=host, now=now, accept_burst=accept_burst)
    no_read = [row for row in (report or {}).get("unreadable_roots", ()) if isinstance(row, Mapping) and isinstance(row.get("path"), str)]
    out_of_sight = 0
    if active is not None:
        errors.extend(active.errors)
        # A failed filing keeps its events for the next run.  What did get through is remembered, so nothing is sent twice.
        # So does a run that could not judge anything, and it forgets nothing either.
        if not active.errors and not unusable:
            state["events_since"] = newest
            if report is not None:
                out_of_sight = _forget_resolved(state, findings, roots, [row["path"] for row in no_read])
        _write_json(state_path, state)
        _write_json(os.path.join(state_root, TOLD_NAME), {"dm_paths": state.get("dm_paths", []), "last_owner_dm": state.get("last_owner_dm", "")})
    for key, items in sorted(fresh.items()):
        out.write(f"{key}: {len(items)} new\n")
        for f in items[:10]:
            out.write(f"  {f.kind} {f.public} {f.detail}\n")
    for row in no_read:
        out.write(f"external root unreadable: {public_path(row['path'], roots)} ({row.get('error')}).  This result is partial, not clean.\n")
    status = {
        "at": now.isoformat(timespec="seconds"),
        "ok": report is not None and not errors and not no_read,
        "held": not acting,
        "coverage": COVERAGE_UNREADABLE if no_read else COVERAGE_FULL,
        "unreadable_roots": [{"path": public_path(row["path"], roots), "error": row.get("error")} for row in no_read],
        "input": source,
        "host": socket.gethostname(),
        "findings": len(findings),
        "new": sum(len(v) for v in fresh.values()),
        "groups": {key: len(items) for key, items in sorted(fresh.items())},
        "errors": errors,
        "applied": active is not None,
        "out_of_sight": out_of_sight,
        # A hold stays a hold, and a reset that sent nothing leaves none.  A dry run looks at nothing here, so it passes on what it found.
        "state_written": (bool(hold) or os.path.exists(state_path)) if acting else _state_was_written(state_path, status_path),
    }
    _write_status(status_path, status, acting, now)
    if report is None or unusable or unverified:
        return EXIT_STALE
    if errors:
        return EXIT_FILING
    return EXIT_UNREADABLE if no_read else EXIT_OK


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog="fleet_lanes.placement_watch",
                                description="Report misplaced checkouts.  Never moves or deletes.")
    p.add_argument("--apply", action="store_true", help="file issues, post, and remember what was reported")
    p.add_argument("--seat", default=DEFAULT_SEAT, help=f"the seat that files (default {DEFAULT_SEAT})")
    p.add_argument("--session", help="session id to record (default placement-watch-<host>)")
    p.add_argument("--doctor-report", metavar="PATH", help="use this report rather than looking in the usual places")
    p.add_argument("--verify-hooks", action="store_true", help="also run install_tools verify for the probe-safe platforms")
    p.add_argument("--stable-dir", metavar="PATH", help="the installed lane-tools folder (default: the parent of this package)")
    p.add_argument("--home", metavar="PATH", help="treat PATH as the home folder (tests)")
    p.add_argument("--accept-burst", action="store_true", help=f"file this run even with more than {MAX_NEW_PER_RUN} new findings")
    p.add_argument("--reset-state", action="store_true", help="set aside a lost or corrupt state file and start again")
    args = p.parse_args(argv)
    home = args.home or env.get("HOME") or os.path.expanduser("~")
    stable = args.stable_dir or os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    session = args.session or f"placement-watch-{P.host_kind(env)}"
    return run_once(home=home, env=env, apply=args.apply, seat=args.seat, session=session,
                    report_path=args.doctor_report, verify_hooks=args.verify_hooks, stable=stable,
                    accept_burst=args.accept_burst, reset_state=args.reset_state)


if __name__ == "__main__":
    sys.exit(main())
