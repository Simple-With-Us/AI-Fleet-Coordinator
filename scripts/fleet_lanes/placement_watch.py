#!/usr/bin/env python3
"""Scheduled placement watcher (AFC-618).  It reports misplaced checkouts and never moves or deletes anything.

    python3 -m fleet_lanes.placement_watch [--apply] [--seat CLAUDE] [--doctor-report PATH] [--verify-hooks]

Without --apply it prints what it would file and changes no state.  One run does this:

  1. Inputs.  A doctor report (`lane ls --write`; the Mac's Maintain step writes one) when it is under six hours old and
     carries `placement_anomalies`;  otherwise its own sweep through doctor.build_report without gh.  The second input is
     the event log the git shim and the hooks append to (placement.py), read from where the last run stopped.
  2. Findings.  Everything in placement_anomalies, each detect-mode event, and any hook whose command no longer exists.
     With --verify-hooks, also the failures of `install_tools verify` for the platforms that are safe to probe.
  3. Dedupe.  The state file remembers every path it has reported.  One Linear issue per creator and repo, never per path,
     with a comment for each later batch of new paths, and one post in the issue's topic when it is filed.
  4. The owner hears only about a leak that keeps growing after seven days, or a checkout in the Code root holding unpushed
     work.  One message per path, ever.

Public text uses root tokens ({home}, {code}, {apps}, {lanes}), never a real path, since a server path is private
inventory.  Exit 0 when it ran;  3 when its inputs are stale or unreadable;  4 when filing failed (it retries next run).
The status file beside the state file lets `fleet-tools-doctor` see a watcher that has gone quiet.  It lists no processes
(no `ps`).  The seat is CLAUDE until the owner chooses another.  Python 3.9 safe.
Tests: fleet_lanes/tests/test_placement_watch.py.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
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
REPORT_MAX_AGE_HOURS = 6.0
REPEAT_LEAK_DAYS = 7
DEFAULT_SEAT = "CLAUDE"
STATE_NAME = "placement-watch.json"
STATUS_NAME = "placement-watch-status.json"
REPORT_NAME = "placement-report.json"
VERIFY_TARGETS = ("claude", "codex", "cursor", "grok", "minimax", "opencode", "antigravity")
MAINTAIN_REPORT = "Library/Application Support/HogHunter/Maintain/lane-doctor-report.json"
EXIT_OK, EXIT_STALE, EXIT_FILING = 0, 3, 4
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
        unpushed = bool(co.get("unpushed")) and (path.startswith(code) or real.startswith(code))
        out.append(Finding(path, public_path(path, roots), creator, repo, kind, detail, unpushed))
    return out


def findings_from_events(events: Iterable[Mapping[str, Any]], since: str, roots: L.Roots) -> tuple[list[Finding], str]:
    """Findings from the event log after `since`, and the new high-water mark.  Temp and Code hits count too."""
    out: list[Finding] = []
    newest = since
    for ev in events:
        at = str(ev.get("at") or "")
        if not at or at <= since:
            continue
        newest = max(newest, at)
        dest = str(ev.get("dest") or "")
        if ev.get("action") not in ("detect", "block") or not dest:
            continue
        expanded = os.path.expanduser(dest) if dest.startswith("~") else dest
        creator = str(ev.get("seat") or "unknown").lower()
        repo = str(ev.get("source") or "unknown-repo").rsplit("/", 1)[-1]
        out.append(Finding(expanded, public_path(expanded, roots), creator, repo, "SHIM-EVENT",
                           f"{ev.get('action')} {ev.get('class')} via {ev.get('via')}"))
    return out, newest


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
            for command in _commands_in(data):
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
    if (now - generated).total_seconds() > max_age_hours * 3600 or "placement_anomalies" not in report:
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


def _write_json(path: str, data: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".placement-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


class Filer:
    """Linear and Zulip through the fleet CLIs, as the seat itself.  Bodies go through 0600 files, as the CLIs ask."""

    def __init__(self, seat: str, session: str, env_label: str, run: Callable[..., Any] = subprocess.run) -> None:
        self.seat, self.session, self.env_label, self.run = seat, session, env_label, run
        self.errors: list[str] = []

    def _call(self, argv: list[str], body: str | None = None, body_flag: str = "--body-file") -> str | None:
        path = None
        try:
            if body is not None:
                fd, path = tempfile.mkstemp(prefix="placement-body-")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body)
                argv = [*argv, body_flag, path]
            res = self.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
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

    def comment(self, issue: str, text: str) -> None:
        self._call(["simple-board", "comment", issue, "--by", self.seat, "--session", self.session, "--text", text])

    def post(self, topic: str, body: str) -> None:
        self._call(["simple-chat", "post", "--as", self.seat, "--session", self.session, "--topic", topic[:60]], body)

    def dm_owner(self, body: str) -> None:
        self._call(["simple-chat", "dm", "--owner", "--as", self.seat, "--session", self.session], body)


_TITLES = {
    "main-clone": "Main Clone Off Its Default Branch: {repo} ({host})",
    "exceptions": "Placement Exceptions Past Their Expiry ({host})",
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


def apply_findings(findings: Sequence[Finding], state: dict, filer: Filer | None, *, host: str,
                   now: _dt.datetime) -> dict[str, list[Finding]]:
    """Update `state` and act.  With no filer it only reports:  the new findings per group, nothing remembered."""
    stamp = now.isoformat(timespec="seconds")
    fresh: dict[str, list[Finding]] = {}
    seen_this_run = set()
    for f in findings:
        if (f.group, f.path) in seen_this_run:
            continue
        seen_this_run.add((f.group, f.path))
        group = state["groups"].get(f.group)
        if group is None or f.path not in group.get("paths", {}):
            fresh.setdefault(f.group, []).append(f)
    if filer is None:
        return fresh
    for key, items in sorted(fresh.items()):
        creator, repo = key.split("|", 1)
        group = state["groups"].setdefault(key, {"first_seen": stamp, "issue": None, "paths": {}})
        title = _title(creator, repo, host)
        before = len(filer.errors)
        if not group.get("issue"):
            body = (f"The placement watcher found these on the {host}.{SG}It never moves or deletes anything.{SG}"
                    f"Rules: docs/protocols/lane-map.md.\n\n{_lines(items)}\n")
            issue = filer.file_issue(title, body, f"placement-watch:{host}:{key}")
            if issue is None:
                continue
            group["issue"] = issue
            filer.post(f"{issue} {title}", f"repo: AI-Fleet-Coordinator{SG}{title}, filed as {issue}.\n\n{_lines(items, 15)}\n")
        else:
            filer.comment(group["issue"], f"New since the last run:\n{_lines(items)}\n")
        if len(filer.errors) == before:
            for f in items:
                group["paths"][f.path] = stamp
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
    before = len(filer.errors)
    filer.dm_owner("From the placement watcher.\n\n" + "\n\n".join(parts) + "\n")
    if len(filer.errors) == before:
        state["dm_paths"] = sorted(told | {f.path for f in (*urgent, *repeat)})


def run_once(*, home: str, env: Mapping[str, str], apply: bool, seat: str, session: str, report_path: str | None,
             verify_hooks: bool, stable: str | None, now: _dt.datetime | None = None, build_report=None,
             filer: Filer | None = None, out=None) -> int:
    now = now or _now()
    out = sys.stdout if out is None else out
    registry = L.load_registry(env=env)
    roots = L.make_roots(home, env, registry=registry)
    host = P.host_kind(env)
    state_root = P.state_dir(home, env)
    state_path = os.path.join(state_root, STATE_NAME)
    status_path = os.path.join(state_root, STATUS_NAME)
    errors: list[str] = []
    candidates = [report_path] if report_path else [os.path.join(state_root, REPORT_NAME), os.path.join(home, MAINTAIN_REPORT)]
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
    findings: list[Finding] = []
    if report is not None:
        findings += findings_from_report(report, roots, registry)
    events, newest = findings_from_events(P.read_events(P.event_log_path(home, env)), state.get("events_since", ""), roots)
    findings += events
    findings += hook_findings(home, roots, host, stable if verify_hooks else None)
    active = filer
    if active is None and apply:
        active = Filer(seat, session, "Mac" if host == P.HOST_MAC else "Server")
    fresh = apply_findings(findings, state, active, host=host, now=now)
    if active is not None:
        errors.extend(active.errors)
        state["events_since"] = newest
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
    if active is not None:
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
    args = p.parse_args(argv)
    home = args.home or env.get("HOME") or os.path.expanduser("~")
    stable = args.stable_dir or os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    session = args.session or f"placement-watch-{P.host_kind(env)}"
    return run_once(home=home, env=env, apply=args.apply, seat=args.seat, session=session,
                    report_path=args.doctor_report, verify_hooks=args.verify_hooks, stable=stable)


if __name__ == "__main__":
    sys.exit(main())
