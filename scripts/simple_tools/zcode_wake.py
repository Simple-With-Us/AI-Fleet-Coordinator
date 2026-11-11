"""ZCode's delivery for the shared wake router (OPS-1048):  one headless resume on this host.

The request is the router's envelope (`{"platform": "zcode", "target": ..., "wake": ...}`) and
the answer carries the same three ids plus the `Delivery` (claude_wake.py, "The wire").  The
command is `zcode --cwd <workspace> --resume <session_id> -p <wake text>`:  the workspace comes
from the settings and is also the binding, because a resume runs in the given directory and a
wake into another project's directory is a wrong-chat delivery.

Checks before anything runs (nothing has run until the spawn, so a pre-spawn failure is
`failed` and retried, while the refusals below are final and are in the router's NEVER_RAN
set, because they prove nothing ran):
- the registry still holds the target's letter on this session at this generation
  (`registry-unavailable` when the registry cannot be read, `released` when it says the
  letter moved on, `unreadable-row` when a live row is not a well-formed zcode chat) —
  read a second time under the per-session lock, immediately before the spawn, so a
  release that happens after the first check still stops the delivery;
- zcode's own tasks index records the session in the configured workspace
  (`workspace-mismatch` when it records another one, `workspace-unknown` when it has no row
  for the session — an unknown session would resume into `/`, which the bridge test showed);
  a target whose host is not this delivery's configured one refuses `host-not-configured`
  rather than failing on paths it does not have;
- no held per-session lock (a second writer on one session is `session-busy` and uses up
  no attempt).  The lock file is the bridge's own — `~/.local/state/zc-wake/session-<sid>.lock`
  — so the bridge and this platform contend on one lock for the cutover.  A chat open in the
  desktop app is still not detected this way;  that needs a zcode liveness signal, which does
  not exist yet.

Then the command.  `zcode -p` exits 0 even when the turn fails (the server's own log shows a
`project_token_login_failed` turn exiting 0), so exit 0 is only `sent` — handed over, no proof
the turn ran — and never `completed`.  A non-zero exit or a timeout is `refused`, because the
turn may have started and a retry could run it twice.  An OSError at start is `failed`.  The
`-p` text is metadata only (the shared `WR.COMMAND_TEXT`:  wake id, link note, message id, an
instruction to read the message in Zulip) — the message body never travels.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import wake_router as WR

PLATFORM = "zcode"
SEAT = "ZC"
# The bridge's real session-id pattern:  zcode accepts more than bare UUIDs.
ZCODE_SESSION_RE = re.compile(r"^sess_[A-Za-z0-9._-]{1,123}$")
LETTER_RE = re.compile(r"[A-Z]{1,2}\Z")
DEFAULT_HOST = "server"
DEFAULT_WORKSPACE = "/mnt/fleet-extra/Code/AI-Fleet-Coordinator"
DEFAULT_ZCODE = "/home/jay/.local/bin/zcode"
DEFAULT_TASKS_INDEX = "~/.zcode/v2/tasks-index.sqlite"
# The bridge's own state dir:  the lock file name and directory match the bridge's
# session_lock, so both delivery paths contend on one lock for the cutover.
DEFAULT_LOCK_DIR = "~/.local/state/zc-wake"
LOCK_NAME = "session-{session_id}.lock"


def _target_from(value: object) -> WR.Target:
    if not isinstance(value, Mapping):
        raise ValueError("target is not an object")
    session_id = str(value.get("session_id") or "")
    if not ZCODE_SESSION_RE.fullmatch(session_id):
        raise ValueError("bad session id")
    letter = str(value.get("letter") or "")
    if not LETTER_RE.fullmatch(letter):
        raise ValueError("bad letter")
    generation = value.get("generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("bad generation")
    return WR.Target(
        session_id=session_id,
        letter=letter,
        host=str(value.get("host") or ""),
        title=str(value.get("title") or ""),
        thread_link=str(value.get("thread_link") or ""),
        generation=generation,
    )


def _wake_from(value: object) -> WR.Wake:
    if not isinstance(value, Mapping):
        raise ValueError("wake is not an object")
    wake_id = str(value.get("wake_id") or "")
    if not WR.WAKE_ID_RE.fullmatch(wake_id):
        raise ValueError("bad wake id")
    message_id = value.get("message_id", 0)
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 0:
        raise ValueError("bad message id")

    def tokens(name: str) -> tuple[str, ...]:
        found = value.get(name, ())
        if not isinstance(found, (list, tuple)) or not all(isinstance(t, str) for t in found):
            raise ValueError("bad " + name)
        return tuple(found)

    return WR.Wake(
        wake_id=wake_id,
        seat=str(value.get("seat") or ""),
        message_id=message_id,
        channel=str(value.get("channel") or ""),
        topic=str(value.get("topic") or ""),
        zulip_link=str(value.get("zulip_link") or ""),
        sender=str(value.get("sender") or ""),
        dm=bool(value.get("dm")),
        letter=str(value.get("letter") or ""),
        picked=str(value.get("picked") or ""),
        reason=str(value.get("reason") or ""),
        link_note=str(value.get("link_note") or ""),
        issue_ids=tokens("issue_ids"),
        session_fragments=tokens("session_fragments"),
        sender_fragment=str(value.get("sender_fragment") or ""),
        linked_by=str(value.get("linked_by") or ""),
    )


def _raw_ids(request: Mapping[str, Any]) -> dict[str, Any]:
    """The ids as the request states them, unvalidated:  the answer to a malformed request
    still repeats them, so a refusal surfaces as `bad-request` and never as a mismatch."""
    target = request.get("target") if isinstance(request.get("target"), Mapping) else {}
    wake = request.get("wake") if isinstance(request.get("wake"), Mapping) else {}
    generation = target.get("generation")
    return {
        "wake_id": str(wake.get("wake_id") or "") or None,
        "session_id": str(target.get("session_id") or "") or None,
        "generation": generation if type(generation) is int else 0,
    }


def _recheck(env: Mapping[str, str], target: WR.Target) -> WR.Delivery | None:
    """The registry still holds the letter on this session at this generation, or why not."""
    try:
        answer = WR.registry_api.call(
            env,
            "resolve",
            {"platform": "zcode", "letter": target.letter},
            timeout=WR.REGISTRY_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - no delivery without a readable answer
        return WR.Delivery("failed", code="registry-unavailable")
    if not isinstance(answer, Mapping) or "error" in answer:
        return WR.Delivery("failed", code="registry-unavailable")
    held = WR.holds(answer, target, SEAT, ZCODE_SESSION_RE)
    if held is None:
        return WR.Delivery("failed", code=WR.UNREADABLE_ROW)
    if not held:
        return WR.Delivery("refused", code=WR.RELEASED)
    return None


def _tasks_workspace(index_path: Path, session_id: str) -> tuple[str, str]:
    """(`ok`|`missing`|`error`, workspace) from zcode's own tasks index, read only."""
    try:
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, timeout=2)
    except (OSError, sqlite3.Error):
        return "error", ""
    try:
        row = con.execute(
            "SELECT workspace_path FROM tasks WHERE task_id = ?", (session_id,)
        ).fetchone()
    except sqlite3.Error:
        return "error", ""
    finally:
        con.close()
    if not row or not row[0]:
        return "missing", ""
    return "ok", str(row[0])


def deliver_request(
    request: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    workspace: str | None = None,
    zcode_bin: str | None = None,
    index_path: str | None = None,
    lock_dir: str | None = None,
    host: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: int = 900,
) -> dict[str, Any]:
    """One zcode delivery:  checks, then the headless resume.  See the module docstring."""

    def setting(name: str, default: str) -> str:
        """The delivery env first (the router's registry env carries the deployment's settings),
        then the process environment, then the built-in default."""
        return env.get(name) or os.environ.get(name) or default

    workspace = workspace or setting("ZCODE_WAKE_WORKSPACE", DEFAULT_WORKSPACE)
    zcode_bin = zcode_bin or setting("ZCODE_WAKE_BIN", DEFAULT_ZCODE)
    index = Path(
        os.path.expanduser(index_path or setting("ZCODE_WAKE_TASKS_INDEX", DEFAULT_TASKS_INDEX))
    )
    locks = Path(os.path.expanduser(lock_dir or setting("ZCODE_WAKE_LOCK_DIR", DEFAULT_LOCK_DIR)))
    host = host or setting("ZCODE_WAKE_HOST", DEFAULT_HOST)
    try:
        target = _target_from(request.get("target"))
        wake = _wake_from(request.get("wake"))
    except (ValueError, KeyError, TypeError, AttributeError):
        # The ids go back as the request stated them (the table has already shape-checked the
        # session id), so this surfaces as refused bad-request and never as a mismatch.
        return {**_raw_ids(request), "delivery": asdict(WR.Delivery("refused", code="bad-request"))}

    def answer(delivery: WR.Delivery) -> dict[str, Any]:
        return {
            "wake_id": wake.wake_id,
            "session_id": target.session_id,
            "generation": target.generation,
            "delivery": asdict(delivery),
        }

    if target.host and target.host != host:
        return answer(WR.Delivery("refused", code="host-not-configured"))
    if (stale := _recheck(env, target)) is not None:
        return answer(stale)
    status, recorded = _tasks_workspace(index, target.session_id)
    if status == "error":
        return answer(WR.Delivery("failed", code="tasks-index-unavailable"))
    if status == "missing":
        return answer(WR.Delivery("refused", code="workspace-unknown"))
    if os.path.realpath(recorded) != os.path.realpath(workspace):
        return answer(WR.Delivery("refused", code="workspace-mismatch"))

    note = wake.link_note if WR.link_note_ok(wake.link_note) else ""
    text = WR.COMMAND_TEXT.format(
        wake_id=wake.wake_id, note=note + "  " if note else "", message_id=wake.message_id
    )
    argv = [zcode_bin, "--cwd", workspace, "--resume", target.session_id, "-p", text]
    if target.session_id.startswith("-"):
        return answer(WR.Delivery("refused", code="command-option-shaped-value"))
    try:
        locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = open(locks / LOCK_NAME.format(session_id=target.session_id), "a", encoding="utf-8")
    except OSError as exc:
        return answer(WR.Delivery("failed", code="lock-%s" % type(exc).__name__))
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return answer(WR.Delivery("busy", route="zcode", code="session-busy"))
        # The late fence:  the registry again, under the lock and immediately before the
        # spawn, so a release that happened after the first check stops here — nothing has
        # run yet, so the refusal is final and safe.
        if (late := _recheck(env, target)) is not None:
            return answer(late)
        try:
            proc = runner(
                argv,
                cwd=workspace,
                env={name: env[name] for name in ("HOME", "PATH") if name in env},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return answer(WR.Delivery("refused", route="zcode", code="command-timeout"))
        except OSError as exc:
            return answer(
                WR.Delivery("failed", route="zcode", code="deliver-%s" % type(exc).__name__)
            )
        if proc.returncode == 0:
            # Exit 0 is only `sent`.  The bridge's four resume attempts with failed turns all
            # exited 1 (command-failed-1);  the one run that seemed to exit 0 was a fresh
            # non-resume probe whose exit code was misread from a shell pipeline, so a
            # successful run has not been seen yet and 0 stays handed-over without proof,
            # never completed.
            return answer(WR.Delivery("sent", route="zcode"))
        return answer(
            WR.Delivery("refused", route="zcode", code="command-failed-%d" % proc.returncode)
        )
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _excluded_ids(
    index_path: Path, cli_db: Path, workspace: str, candidates: list[WR.Target]
) -> set[str]:
    """The candidate sessions that must never be picked:  a sub-agent child (the CLI store's
    own marker), a session the tasks index does not know, and one whose recorded workspace is
    not the configured one.  Best effort on purpose:  an unreadable store narrows nothing
    here, because `build_platform`'s delivery re-checks both stores fail-closed before any
    command runs, so a store outage holds the wake instead of widening this set."""
    sids = [t.session_id for t in candidates]
    if not sids:
        return set()
    out: set[str] = set()
    placeholders = ",".join("?" * len(sids))
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{cli_db}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id FROM session WHERE task_type = 'subagent_child' AND id IN (%s)"
            % placeholders,
            sids,
        ).fetchall()
        out |= {str(r[0]) for r in rows if r and r[0]}
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            con.close()
    con = None
    try:
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, timeout=2)
        for sid in sids:
            row = con.execute(
                "SELECT workspace_path FROM tasks WHERE task_id = ?", (sid,)
            ).fetchone()
            if (
                not row
                or not row[0]
                or os.path.realpath(str(row[0])) != os.path.realpath(workspace)
            ):
                out.add(sid)
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            con.close()
    return out


def build_platform(
    env: Mapping[str, str] | None = None,
    *,
    workspace: str | None = None,
    zcode_bin: str | None = None,
    index_path: str | None = None,
    cli_db: str | None = None,
    host: str | None = None,
    timeout: int = 900,
    runner: Callable[..., Any] = subprocess.run,
) -> WR.Platform:
    """The server-side `Platform` for the router's table:  the same delivery as
    `deliver_request`, as a `WR.command_platform` spec, with the sub-agent and
    wrong-workspace sessions excluded from the pick itself and both stores re-checked
    fail-closed at delivery time (an unreadable store holds the wake as `failed`, so a store
    outage can never widen the pick into a delivery)."""

    def setting(name: str, default: str) -> str:
        e = env or {}
        return e.get(name) or os.environ.get(name) or default

    workspace = workspace or setting("ZCODE_WAKE_WORKSPACE", DEFAULT_WORKSPACE)
    zcode_bin = zcode_bin or setting("ZCODE_WAKE_BIN", DEFAULT_ZCODE)
    index = Path(
        os.path.expanduser(index_path or setting("ZCODE_WAKE_TASKS_INDEX", DEFAULT_TASKS_INDEX))
    )
    cli_db = Path(
        os.path.expanduser(cli_db or setting("ZCODE_WAKE_CLI_DB", "~/.zcode/cli/db/db.sqlite"))
    )
    host = host or setting("ZCODE_WAKE_HOST", DEFAULT_HOST)

    def exclude(candidates: list[WR.Target]) -> set[str]:
        return _excluded_ids(index, cli_db, workspace, candidates)

    inner = WR.command_platform(
        seat=SEAT,
        platform=PLATFORM,
        mention="ZCode",
        target_re=ZCODE_SESSION_RE,
        argv=[
            zcode_bin,
            "--cwd",
            workspace,
            "--resume",
            "{session_id}",
            "-p",
            "{wake_text}",
        ],
        env={name: env[name] for name in ("HOME", "PATH") if name in (env or {})} or None,
        cwd=workspace,
        timeout=timeout,
        exclude=exclude,
        runner=runner,
    )

    def deliver(target: WR.Target, wake: WR.Wake) -> WR.Delivery:
        # Fail-closed store checks:  an unreadable index or CLI store is `failed` (the wake is
        # held and retried), never a delivery of a chat the stores could not vet.
        status, _ = _tasks_workspace(index, target.session_id)
        if status == "error":
            return WR.Delivery("failed", code="tasks-index-unavailable")
        subagents = _excluded_ids(index, cli_db, workspace, [target])
        if target.session_id in subagents:
            return WR.Delivery("refused", code="subagent-child")
        return inner.deliver(target, wake)

    platform = WR.Platform(
        seat=SEAT,
        platform=PLATFORM,
        mention="ZCode",
        target_re=ZCODE_SESSION_RE,
        deliver=deliver,
        exclude=exclude,
    )
    return platform
