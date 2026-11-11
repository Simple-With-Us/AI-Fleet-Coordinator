"""ZCode's delivery for the shared wake router (OPS-1048):  one headless resume on this host.

The request is the router's envelope (`{"platform": "zcode", "target": ..., "wake": ...}`) and
the answer carries the same three ids plus the `Delivery` (claude_wake.py, "The wire").  The
command is `zcode --cwd <workspace> --resume <session_id> -p <wake text>`:  the workspace comes
from the settings and is also the binding, because a resume runs in the given directory and a
wake into another project's directory is a wrong-chat delivery.

Checks before anything runs (nothing has run until the spawn, so a pre-spawn failure is
`failed` and retried, unless it is a refusal named below):
- the registry still holds the target's letter on this session at this generation
  (`registry-unavailable` when the registry cannot be read, `released` when it says the
  letter moved on, `unreadable-row` when a live row is not a well-formed zcode chat);
- zcode's own tasks index records the session in the configured workspace
  (`workspace-mismatch` when it records another one, `workspace-unknown` when it has no
  row — the bridge test showed an unknown session resuming into `/`, which is the failure
  this check exists to refuse;  `tasks-index-unavailable` when the index cannot be read);
- no held per-session lock (a second writer on one session is `session-busy` and uses up
  no attempt).

Then the command.  Exit 0 is `completed` — a headless resume that exited 0 ran the turn to
the end.  A non-zero exit or a timeout is `refused`, because the turn may have started and a
retry could run it twice.  An OSError at start is `failed`.  The `-p` text is metadata only
(the shared `WR.COMMAND_TEXT`:  wake id, link note, message id, an instruction to read the
message in Zulip) — the message body never travels.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from . import wake_router as WR

PLATFORM = "zcode"
SEAT = "ZC"
SESSION_RE = re.compile(r"sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
DEFAULT_WORKSPACE = "/mnt/fleet-extra/Code/AI-Fleet-Coordinator"
DEFAULT_ZCODE = "/home/jay/.local/bin/zcode"
DEFAULT_TASKS_INDEX = "~/.zcode/v2/tasks-index.sqlite"
DEFAULT_LOCK_DIR = "~/.local/state/zcode-wake/locks"


def _target_from(value: object) -> WR.Target:
    if not isinstance(value, Mapping):
        raise ValueError("target is not an object")
    session_id = str(value.get("session_id") or "")
    if not SESSION_RE.fullmatch(session_id):
        raise ValueError("bad session id")
    generation = value.get("generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("bad generation")
    return WR.Target(
        session_id=session_id,
        letter=str(value.get("letter") or ""),
        host=str(value.get("host") or ""),
        title=str(value.get("title") or ""),
        thread_link=str(value.get("thread_link") or ""),
        generation=generation,
    )


def _wake_from(value: object) -> WR.Wake:
    if not isinstance(value, Mapping):
        raise ValueError("wake is not an object")
    wake_id = str(value.get("wake_id") or "")
    if not wake_id:
        raise ValueError("bad wake id")
    message_id = value.get("message_id", 0)
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 0:
        raise ValueError("bad message id")
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
        linked_by=str(value.get("linked_by") or ""),
    )


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
    held = WR.holds(answer, target, SEAT, SESSION_RE)
    if held is None:
        return WR.Delivery("failed", code=WR.UNREADABLE_ROW)
    if not held:
        return WR.Delivery("refused", code=WR.RELEASED)
    return None


def _tasks_workspace(
    index_path: Path, session_id: str, runner_ok: bool = True
) -> tuple[str, str]:
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
    workspace: str = DEFAULT_WORKSPACE,
    zcode_bin: str = DEFAULT_ZCODE,
    index_path: str | None = None,
    lock_dir: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """One zcode delivery:  checks, then the headless resume.  See the module docstring."""
    try:
        target = _target_from(request.get("target"))
        wake = _wake_from(request.get("wake"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return {
            "wake_id": "",
            "session_id": "",
            "generation": 0,
            "delivery": asdict(WR.Delivery("refused", code="bad-request")),
        }
    index = Path(index_path or os.path.expanduser(DEFAULT_TASKS_INDEX))
    locks = Path(lock_dir or os.path.expanduser(DEFAULT_LOCK_DIR))

    def answer(delivery: WR.Delivery) -> dict[str, Any]:
        return {
            "wake_id": wake.wake_id,
            "session_id": target.session_id,
            "generation": target.generation,
            "delivery": asdict(delivery),
        }

    if target.letter and (stale := _recheck(env, target)) is not None:
        return answer(stale)
    status, recorded = _tasks_workspace(index, target.session_id)
    if status == "error":
        return answer(WR.Delivery("failed", code="tasks-index-unavailable"))
    if status == "missing" or os.path.realpath(recorded) != os.path.realpath(workspace):
        return answer(WR.Delivery("refused", code="workspace-mismatch"))

    note = wake.link_note if WR.link_note_ok(wake.link_note) else ""
    text = WR.COMMAND_TEXT.format(wake_id=wake.wake_id, note=note + "  " if note else "", message_id=wake.message_id)
    argv = [zcode_bin, "--cwd", workspace, "--resume", target.session_id, "-p", text]
    if any(str(part).startswith("-") for part in (target.session_id,)):
        return answer(WR.Delivery("refused", code="command-option-shaped-value"))
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = locks / (target.session_id + ".lock")
    try:
        handle = open(lock_path, "a", encoding="utf-8")
    except OSError as exc:
        return answer(WR.Delivery("failed", code="lock-%s" % type(exc).__name__))
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return answer(WR.Delivery("busy", route="zcode", code="session-busy"))
        try:
            proc = runner(
                argv,
                cwd=workspace,
                env={name: env[name] for name in ("HOME", "PATH") if name in env},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=900,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return answer(WR.Delivery("refused", route="zcode", code="command-timeout"))
        except OSError as exc:
            return answer(WR.Delivery("failed", route="zcode", code="deliver-%s" % type(exc).__name__))
        if proc.returncode == 0:
            return answer(WR.Delivery("completed", route="zcode", ack="confirmed"))
        return answer(WR.Delivery("refused", route="zcode", code="command-failed-%d" % proc.returncode))
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
