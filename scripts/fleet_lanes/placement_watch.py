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
     fixed and broken again is told again.  One Linear issue per creator and repo, never per path, with a comment for each later
     batch of new findings, and one post in the issue's topic when it is filed.  If that issue has since been closed, the next batch
     gets a new one.  A run with more than MAX_NEW_PER_RUN new findings looks like a renamed mount or a wrong root, not a leak, so it
     sends nothing and says so until a person passes --accept-burst.
  4. The owner hears only about a leak that keeps growing after seven days, or a clone in the Code root holding unpushed work (not a
     harness worktree inside one, which is in-flight work with an issue already).  One message per path:  a send whose outcome is
     unknown counts as sent, because only a proven refusal is safe to send again.  A filing retries every run, since the board dedupes
     it on its uid.

A lost or corrupt state file holds all sending, since everything would otherwise look new, until --reset-state.

Public text uses root tokens ({home}, {code}, {apps}, {lanes}), never a real path, since a server path is private
inventory.  Exit 0 when it ran;  3 when its inputs are stale or unreadable;  4 when filing failed (it retries next run).
The status file beside the state file lets `fleet-tools-doctor` see a watcher that has gone quiet.  It lists no processes
(no `ps`).  The seat is CLAUDE until the owner chooses another.  Python 3.9 safe.
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
REPORT_NAME = "placement-report.json"
VERIFY_TARGETS = ("claude", "codex", "cursor", "grok", "minimax", "opencode", "antigravity")
MAINTAIN_REPORT = "Library/Application Support/HogHunter/Maintain/lane-doctor-report.json"
EXIT_OK, EXIT_STALE, EXIT_FILING = 0, 3, 4
BEARER_LOCATOR = "SIMPLE_BOARD_BEARER_LOCATOR"
# What a send can come to.  Only a refusal the CLI made before sending anything is safe to repeat.
OK, REFUSED, UNKNOWN = "ok", "refused", "unknown"
REFUSED_EXITS = frozenset({2, 6})  # simple-board: bad usage, no verified seat;  simple-chat's argument errors are 2 as well
CLOSED_STATUSES = frozenset({"completed", "deployed", "wontfix", "duplicate", "canceled"})
_REPO_LOCAL_WORKTREES = ("/.claude/worktrees/", "/.muse/worktrees/")
_ISSUE_ID = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


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
        else:
            creator, repo = creator_of(path, co, registry), _repo_of(co)
        real = str(co.get("realpath") or path)
        harness = any(mark in f"{path}/" for mark in _REPO_LOCAL_WORKTREES)
        unpushed = bool(co.get("unpushed")) and not harness and (path.startswith(code) or real.startswith(code))
        out.append(Finding(path, public_path(path, roots), creator, repo, kind, detail, unpushed))
    for item in report.get("placement_warnings", ()):
        path, detail = str(item.get("path", "")), str(item.get("detail") or "")
        out.append(Finding(path, path, "exceptions", "AI-Fleet-Coordinator", str(item.get("type", "")), detail))
    return out


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
            out.append(Finding(f"fail-open:{detail}", f"git shim, {detail}", "hook-coverage", host or P.host_kind(), "FAIL-OPEN",
                               "the shim let a command through without judging it"))
            continue
        dest = str(ev.get("dest") or "")
        if ev.get("action") not in ("detect", "block") or not dest:
            continue
        expanded = os.path.expanduser(dest) if dest.startswith("~") else dest
        creator = _creator_from_seat(str(ev.get("seat") or ""), registry)
        repo = str(ev.get("source") or "unknown-repo").rsplit("/", 1)[-1]
        out.append(Finding(expanded, public_path(expanded, roots), creator, repo, "SHIM-EVENT",
                           f"{ev.get('action')} {ev.get('class')} via {ev.get('via')}"))
    return out, newest


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


def dead_hook_commands(home: str, *, configs: Sequence[str] = HOOK_CONFIGS) -> list[tuple[str, str]]:
    """(config file, command path) for each hook whose absolute command is gone or cannot run.

    A hook that fails silently allows everything, so a moved path turns a guard off without a sound.  Only the first word
    of a command is kept;  nothing else in these files is read out.
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
                try:
                    words = shlex.split(command)
                except ValueError:
                    words = command.split()
                first = words[0] if words else ""
                if first.startswith("/") and not os.access(first, os.X_OK):
                    out.append((path, first))
    return sorted(set(out))


def verify_failures(stable: str, targets: Sequence[str], run: Callable[..., Any] = subprocess.run,
                    env: Mapping[str, str] | None = None) -> list[str] | None:
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
    rows = [line.split(None, 3) for line in (res.stdout or "").splitlines() if line.startswith("FAIL")]
    return [f"{r[1]} {r[2]}" for r in rows if len(r) >= 3]


def hook_findings(home: str, roots: L.Roots, host: str, stable: str | None, *, run=subprocess.run) -> list[Finding]:
    out = [
        Finding(f"hook:{cmd}", public_path(cmd, roots), "hook-coverage", host, "DEAD-HOOK",
                f"the command in {public_path(path, roots)} cannot run, so that guard is off")
        for path, cmd in dead_hook_commands(home)
    ]
    if stable:
        for row in verify_failures(stable, VERIFY_TARGETS, run=run) or ():
            out.append(Finding(f"verify:{row}", row, "hook-coverage", host, "VERIFY-FAIL", "install_tools verify"))
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
    return {"version": 1, "groups": {}, "events_since": "", "dm_paths": []}


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(state, dict) or not isinstance(state.get("groups"), dict):
        return _empty_state()
    return {**_empty_state(), **state}


def state_problem(state_path: str, status_path: str) -> str | None:
    """Why the state file cannot be trusted, or None.  With no memory every finding looks new, so a lost file must not re-send them."""
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return "the state file is gone but the status file says this ran before" if os.path.exists(status_path) else None
    except (OSError, ValueError) as exc:
        return f"the state file is unreadable ({type(exc).__name__})"
    return None if isinstance(state, dict) and isinstance(state.get("groups"), dict) else "the state file has the wrong shape"


def _write_json(path: str, data: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".placement-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Filer:
    """Linear and Zulip through the fleet CLIs, as the seat itself.  Bodies go through 0600 files, as the CLIs ask."""

    def __init__(self, seat: str, session: str, env_label: str, run: Callable[..., Any] = subprocess.run) -> None:
        self.seat, self.session, self.env_label, self.run = seat, session, env_label, run
        self.errors: list[str] = []
        self.outcome = OK  # of the last call

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
            self.outcome = REFUSED if res.returncode in REFUSED_EXITS else UNKNOWN
            self.errors.append(f"{argv[0]} {argv[1]}: exit {res.returncode}: {detail}")
            return None
        return res.stdout or ""

    def file_issue(self, title: str, body: str, uid: str) -> str | None:
        out = self._call(["simple-board", "file", "--title", title, "--app", "AFC", "--severity", "P2",
                          "--by", self.seat, "--env", self.env_label, "--kind", "agent-report",
                          "--session", self.session, "--uid", uid], body, "--desc-file")
        if out is None:
            return None
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
    "main-clone": "Main Clone Off Its Default Branch: {repo} ({host})",
    "exceptions": "Placement Exceptions Expired Or Near Expiry ({host})",
    "hook-coverage": "Lane Guard Hooks Not Running ({host})",
}


def _title(creator: str, repo: str, host: str) -> str:
    template = _TITLES.get(creator, "Misplaced Checkouts: {creator} In {repo} ({host})")
    return template.format(creator=creator.title(), repo=repo, host=host.title())


def _lines(items: Sequence[Finding], limit: int = 40) -> str:
    rows = [f"- `{D.redact_text(f.public)}`: {f.kind.lower()}, {D.redact_text(f.detail)}" for f in items[:limit]]
    if len(items) > limit:
        rows.append(f"- and {len(items) - limit} more")
    return "\n".join(rows)


def _forget_resolved(state: dict, findings: Sequence[Finding]) -> None:
    """Drop what the latest full look no longer sees, so a checkout fixed and broken again is reported again."""
    current = {key for f in findings for key in f.keys}
    for group in state["groups"].values():
        group["paths"] = {k: v for k, v in group.get("paths", {}).items() if k in current}


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
        creator, repo = key.split("|", 1)
        group = state["groups"].setdefault(key, {"first_seen": stamp, "issue": None, "paths": {}})
        title = _title(creator, repo, host)
        if group.get("issue") and filer.issue_is_closed(group["issue"]):
            group.update(issue=None, generation=int(group.get("generation", 0)) + 1, first_seen=stamp)
        if not group.get("issue"):
            body = (f"The placement watcher found these on the {host}.{SG}It never moves or deletes anything.{SG}"
                    f"Rules: docs/protocols/lane-map.md.\n\n{_lines(items)}\n")
            generation = int(group.get("generation", 0))
            uid = f"placement-watch:{host}:{key}" + (f":{generation}" if generation else "")
            issue = filer.file_issue(title, body, uid)
            if issue is None:
                continue
            group["issue"] = issue
            # The issue holds these findings now, whatever becomes of the post.
            filer.post(f"{issue} {title}", f"repo: AI-Fleet-Coordinator{SG}{title}, filed as {issue}.\n\n{_lines(items, 15)}\n")
        elif filer.comment(group["issue"], f"New since the last run:\n{_lines(items)}\n") == REFUSED:
            continue
        for f in items:
            for fingerprint in f.keys:
                group["paths"][fingerprint] = stamp
    _notify_owner(findings, fresh, state, filer, now)
    return fresh


def _notify_owner(findings: Sequence[Finding], fresh: Mapping[str, list[Finding]], state: dict, filer: Filer,
                  now: _dt.datetime) -> None:
    told = set(state.get("dm_paths", ()))
    urgent = [f for f in findings if f.unpushed_in_code and f.path not in told]
    repeat: list[Finding] = []
    for key, items in fresh.items():
        group = state["groups"].get(key) or {}
        try:
            first = _dt.datetime.fromisoformat(group.get("first_seen", ""))
        except ValueError:
            continue
        if (now - first).days >= REPEAT_LEAK_DAYS:
            repeat.extend(f for f in items if f.path not in told)
    if not urgent and not repeat:
        return
    parts = []
    if urgent:
        parts.append(f"{len(urgent)} checkout(s) in the Code root hold unpushed work:\n{_lines(urgent, 10)}")
    if repeat:
        parts.append(f"{len(repeat)} new misplaced checkout(s) from a leak open over {REPEAT_LEAK_DAYS} days:\n{_lines(repeat, 10)}")
    if filer.dm_owner("From the placement watcher.\n\n" + "\n\n".join(parts) + "\n") != REFUSED:
        state["dm_paths"] = sorted(told | {f.path for f in (*urgent, *repeat)})


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
    try:
        registry = L.load_registry(env=env)
        roots = L.make_roots(home, env, registry=registry)
    except (OSError, ValueError, L.LayoutError) as exc:
        if apply or filer is not None:
            _write_json(status_path, {"at": now.isoformat(timespec="seconds"), "ok": False, "input": None, "host": socket.gethostname(),
                                      "findings": 0, "new": 0, "errors": [f"registry unreadable: {type(exc).__name__}"], "applied": False})
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
    acting = apply or filer is not None
    state = load_state(state_path)
    hold = state_problem(state_path, status_path) if acting else None
    if hold and reset_state:
        with contextlib.suppress(OSError):
            os.replace(state_path, f"{state_path}.reset-{now.strftime('%Y%m%dT%H%M%S')}")
        state, hold = _empty_state(), None
    if hold:
        errors.append(f"{hold}, so nothing was sent.  Check it, then run once with --reset-state.")
    active = filer
    if active is None and apply and not hold:
        if env.get(BEARER_LOCATOR):
            active = Filer(seat, session, board_env(host))
        else:
            errors.append(f"{BEARER_LOCATOR} is not set, so nothing was filed.")
    findings: list[Finding] = []
    if report is not None:
        findings += findings_from_report(report, roots, registry)
    events, newest = findings_from_events(P.read_events(P.event_log_path(home, env)), state.get("events_since", ""), roots,
                                          host=host, registry=registry)
    findings += events
    findings += hook_findings(home, roots, host, stable if verify_hooks else None)
    if hold:
        active = None
    fresh = apply_findings(findings, state, active, host=host, now=now, accept_burst=accept_burst)
    if active is not None:
        errors.extend(active.errors)
        # A failed filing keeps its events for the next run.  What did get through is remembered, so nothing is sent twice.
        if not active.errors:
            state["events_since"] = newest
            if report is not None:
                _forget_resolved(state, findings)
        _write_json(state_path, state)
    for key, items in sorted(fresh.items()):
        out.write(f"{key}: {len(items)} new\n")
        for f in items[:10]:
            out.write(f"  {f.kind} {f.public} {f.detail}\n")
    status = {
        "at": now.isoformat(timespec="seconds"),
        "ok": report is not None and not errors,
        "input": source,
        "host": socket.gethostname(),
        "findings": len(findings),
        "new": sum(len(v) for v in fresh.values()),
        "errors": errors,
        "applied": active is not None,
    }
    if acting:
        _write_json(status_path, status)
    if report is None:
        return EXIT_STALE
    return EXIT_FILING if errors else EXIT_OK


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
