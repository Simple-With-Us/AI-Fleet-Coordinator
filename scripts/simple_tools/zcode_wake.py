"""ZCode's delivery for the shared wake router (OPS-1048):  one headless resume on this host.

The request is the router's envelope (`{"platform": "zcode", "target": ..., "wake": ...}`) and
the answer carries the same three ids plus the `Delivery` (claude_wake.py, "The wire").  The
command is `zcode --cwd <workspace> --resume <session_id> -p <wake text>`:  the workspace comes
from the settings and is also the binding, because a resume runs in the given directory and a
wake into another project's directory is a wrong-chat delivery.  The settings are required:
with no workspace configured the delivery fails as `settings-missing` and is retried, and no
private path is written into this file.

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
- the CLI store does not mark the session a sub-agent child (`subagent-child` refuses);
- the target's host is the host this delivery is configured for (`host-not-configured`);
- no held per-session lock (a second writer on one session is `session-busy` and uses up
  no attempt).  The lock file is the bridge's own — `<state dir>/session-<sid>.lock` — so
  the bridge and this platform contend on one lock for the cutover.  A chat open in the
  desktop app is still not detected this way;  that needs a zcode liveness signal, which
  does not exist yet.

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
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from . import wake_router as WR

PLATFORM = "zcode"
SEAT = "ZC"
# The bridge's real session-id pattern:  zcode accepts more than bare UUIDs.
ZCODE_SESSION_RE = re.compile(r"^sess_[A-Za-z0-9._-]{1,123}$")
LETTER_RE = re.compile(r"[A-Z]{1,2}\Z")
DEFAULT_HOST = "server"
DEFAULT_ZCODE = "zcode"
DEFAULT_LOCK_DIR = "~/.local/state/zc-wake"
LOCK_NAME = "session-{session_id}.lock"


class _Settings(dict):
    """The delivery's settings:  the env the router hands the platform first, then the process
    environment.  Nothing here is a private path - a deployment names its own directories."""

    def path(self, name: str, default: str) -> Path:
        raw = self.get(name) or os.environ.get(name) or default
        home = self.get("HOME") or os.environ.get("HOME")
        if raw.startswith("~") and home:
            raw = home + raw[1:]
        return Path(raw)


def _settings(env: Mapping[str, str]) -> _Settings:
    return _Settings(env)


def _required_workspace(settings: _Settings) -> str | None:
    """The configured workspace, or None when the deployment has named none."""
    return settings.get("ZCODE_WAKE_WORKSPACE") or os.environ.get("ZCODE_WAKE_WORKSPACE") or None


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


def _cli_subagent_ids(cli_db: Path, sids: list[str]) -> tuple[str, set[str]]:
    """(`ok`|`error`, sub-agent ids) among `sids`, from the CLI store's own marker."""
    if not sids:
        return "ok", set()
    try:
        con = sqlite3.connect(f"file:{cli_db}?mode=ro", uri=True, timeout=5)
    except (OSError, sqlite3.Error):
        return "error", set()
    try:
        placeholders = ",".join("?" * len(sids))
        rows = con.execute(
            "SELECT id FROM session WHERE task_type = 'subagent_child' AND id IN (%s)"
            % placeholders,
            sids,
        ).fetchall()
        return "ok", {str(r[0]) for r in rows if r and r[0]}
    except sqlite3.Error:
        return "error", set()
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


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
    settings = _settings(env)
    workspace = workspace or _required_workspace(settings)
    zcode_bin = zcode_bin or settings.get("ZCODE_WAKE_BIN") or DEFAULT_ZCODE
    index = Path(os.path.expanduser(
        index_path or settings.path("ZCODE_WAKE_TASKS_INDEX", "~/.zcode/v2/tasks-index.sqlite")
    ))
    locks = Path(os.path.expanduser(
        lock_dir or settings.path("ZCODE_WAKE_LOCK_DIR", "~/.local/state/zc-wake")
    ))
    host = host or settings.get("ZCODE_WAKE_HOST") or DEFAULT_HOST
    if not workspace:
        # Hold and retry:  the deployment has not named a workspace, and delivering into an
        # unknown directory is the wrong-chat failure this platform exists to refuse.
        return {
            "wake_id": str((request.get("wake") or {}).get("wake_id") or "") or None,
            "session_id": str((request.get("target") or {}).get("session_id") or "") or None,
            "delivery": asdict(WR.Delivery("failed", code="settings-missing")),
        }
    try:
        target = _target_from(request.get("target"))
        wake = _wake_from(request.get("wake"))
    except (ValueError, KeyError, TypeError, AttributeError):
        # The ids go back as the request stated them (the table has already shape-checked the
        # session id), so this surfaces as refused bad-request and never as a mismatch.
        return {**_raw_ids(request),
                "delivery": asdict(WR.Delivery("refused", code="bad-request"))}

    def answer(delivery: WR.Delivery) -> dict[str, Any]:
        return {
            "wake_id": wake.wake_id,
            "session_id": target.session_id,
            "generation": target.generation,
            "delivery": asdict(delivery),
        }

    if target.host != host:
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


def _subagent_children(cli_db: Path, sids: list[str]) -> tuple[str, set[str]]:
    """(`ok`|`error`, sub-agent ids) among `sids`, from the CLI store's own marker.
    Same read as the bridge's sub-agent check, with the error made visible."""
    return _cli_subagent_ids(cli_db, sids)


def _exclude_best_effort(
    index_path: Path, cli_db: Path, workspace: str | None, candidates: list[WR.Target]
) -> set[str]:
    """The candidate sessions to narrow out of the pick, best effort on purpose:  an unreadable
    store narrows nothing here, because the delivery path re-checks both stores fail-closed
    before any command runs, so a store outage holds the wake instead of hiding it from the
    pick and delivering it anyway."""
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
            try:
                con.close()
            except sqlite3.Error:
                pass
    con = None
    if workspace:
        try:
            con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, timeout=2)
            for sid in sids:
                row = con.execute(
                    "SELECT workspace_path FROM tasks WHERE task_id = ?", (sid,)
                ).fetchone()
                if not row or not row[0] or os.path.realpath(str(row[0])) != os.path.realpath(
                    workspace
                ):
                    out.add(sid)
        except sqlite3.Error:
            pass
        finally:
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
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
    """The server-side `Platform` for the router's table:  the same checks as
    `deliver_request`, re-run fail-closed at delivery time (an unreadable store holds the
    wake as `failed`, a wrong-workspace or sub-agent session refuses with its own typed
    code, and nothing runs), plus the sub-agent and wrong-workspace sessions excluded from
    the pick itself.  Without a configured workspace nothing is built - call this only from
    a deployment that has named one, or the router will refuse every zcode wake as
    `settings-missing`, which is the safe direction."""
    def setting(name: str, default: str | None = None) -> str | None:
        e = env or {}
        return e.get(name) or os.environ.get(name) or default

    workspace = workspace or setting("ZCODE_WAKE_WORKSPACE")
    zcode_bin = zcode_bin or setting("ZCODE_WAKE_BIN") or DEFAULT_ZCODE
    index = Path(os.path.expanduser(
        index_path or setting("ZCODE_WAKE_TASKS_INDEX", "~/.zcode/v2/tasks-index.sqlite")
    ))
    cli_db_path = Path(os.path.expanduser(
        cli_db or setting("ZCODE_WAKE_CLI_DB", "~/.zcode/cli/db/db.sqlite")
    ))
    host = host or setting("ZCODE_WAKE_HOST") or DEFAULT_HOST

    def exclude(candidates: list[WR.Target]) -> set[str]:
        return _exclude_best_effort(index, cli_db_path, workspace, candidates)

    def deliver(target: WR.Target, wake: WR.Wake) -> WR.Delivery:
        if target.host != host:
            return WR.Delivery("refused", code="host-not-configured")
        status, recorded = _tasks_workspace(index, target.session_id)
        if status == "error":
            return WR.Delivery("failed", code="tasks-index-unavailable")
        if status == "missing":
            return WR.Delivery("refused", code="workspace-unknown")
        if os.path.realpath(recorded) != os.path.realpath(workspace):
            return WR.Delivery("refused", code="workspace-mismatch")
        status, subagents = _subagent_children(cli_db_path, [target.session_id])
        if status == "error":
            return WR.Delivery("failed", code="cli-store-unavailable")
        if target.session_id in subagents:
            return WR.Delivery("refused", code="subagent-child")
        return inner.deliver(target, wake)

    inner_platform = WR.command_platform(
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
    inner = inner_platform.deliver
    return WR.Platform(
        seat=SEAT,
        platform=PLATFORM,
        mention="ZCode",
        target_re=ZCODE_SESSION_RE,
        deliver=deliver,
        exclude=exclude,
    )


def settings_path(env: Mapping[str, str] | None, name: str, default: str) -> Path:
    """A settings path with `~` resolved against the delivery env's HOME, then the process's."""
    e = env or {}
    raw = e.get(name) or os.environ.get(name) or default
    home = e.get("HOME") or os.environ.get("HOME")
    if raw.startswith("~") and home:
        raw = home + raw[1:]
    return Path(raw)
