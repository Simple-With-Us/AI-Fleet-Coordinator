"""On-disk job records, atomic writes, ring-buffered tail, process-group kill.

Directory:  ~/.seat-mcp/jobs/<jobId>.json
Do not put records under ~/.dsh/mcp-jobs.  This is multi-seat.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import JOBS_DIR, TAIL_BYTES

JsonDict = dict[str, Any]
_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_job_id() -> str:
    return uuid.uuid4().hex


def job_path(job_id: str) -> Path:
    if not job_id or any(ch in job_id for ch in "/\\"):
        raise ValueError("invalid jobId")
    return JOBS_DIR / ("%s.json" % job_id)


def ensure_jobs_dir() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(JOBS_DIR.parent, 0o700)
        os.chmod(JOBS_DIR, 0o700)
    except OSError:
        pass


def atomic_write(path: Path, data: JsonDict) -> None:
    """Write JSON via a sibling tmp file, then os.replace."""
    ensure_jobs_dir()
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_job(job_id: str) -> JsonDict:
    path = job_path(job_id)
    if not path.is_file():
        raise FileNotFoundError("unknown jobId")
    with _LOCK:
        return json.loads(path.read_text(encoding="utf-8"))


def save_job(rec: JsonDict) -> JsonDict:
    job_id = str(rec.get("jobId") or "")
    with _LOCK:
        atomic_write(job_path(job_id), rec)
    return rec


def update_job(job_id: str, **fields: Any) -> JsonDict:
    with _LOCK:
        rec = load_job(job_id)
        rec.update(fields)
        atomic_write(job_path(job_id), rec)
        return rec


def ring_tail(data: bytes | str, limit: int = TAIL_BYTES) -> str:
    """Keep the last `limit` bytes, decoded as text."""
    if isinstance(data, str):
        raw = data.encode("utf-8", errors="replace")
    else:
        raw = data
    if len(raw) > limit:
        raw = raw[-limit:]
    return raw.decode("utf-8", errors="replace")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def kill_process_group(pgid: int | None, grace_sec: float = 1.0) -> None:
    """SIGTERM the process GROUP, then SIGKILL.  Not just the parent pid."""
    if not pgid:
        return
    pgid = int(pgid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def new_record(
    *,
    seat: str,
    prompt: str,
    cwd: str,
    opts: JsonDict,
    prior_job_id: str | None = None,
) -> JsonDict:
    job_id = new_job_id()
    rec: JsonDict = {
        "jobId": job_id,
        "seat": seat,
        "state": "queued",
        "prompt": prompt,
        "cwd": cwd,
        "opts": opts or {},
        "createdAt": _now_iso(),
        "startedAt": None,
        "finishedAt": None,
        "heartbeatAt": _now_iso(),
        "pid": None,
        "pgid": None,
        "exitCode": None,
        "sessionId": None,
        "lastTool": None,
        "gitHeadStart": git_head(cwd),
        "text": "",
        "partialTail": "",
        "stats": {"elapsedMs": 0, "bytesOut": 0},
        "artifacts": [],
        "error": None,
        "priorJobId": prior_job_id,
    }
    save_job(rec)
    return rec


def elapsed_ms(rec: JsonDict) -> int:
    start = rec.get("startedAt") or rec.get("createdAt")
    end = rec.get("finishedAt")
    try:
        t0 = datetime.fromisoformat(str(start))
    except (TypeError, ValueError):
        return 0
    if end:
        try:
            t1 = datetime.fromisoformat(str(end))
        except (TypeError, ValueError):
            t1 = datetime.now(timezone.utc)
    else:
        t1 = datetime.now(timezone.utc)
    return max(0, int((t1 - t0).total_seconds() * 1000))


def heartbeat_view(rec: JsonDict, wedge_sec: float) -> JsonDict:
    """working vs wedged vs dead, from heartbeat + pid."""
    state = rec.get("state")
    at = rec.get("heartbeatAt")
    alive = pid_alive(rec.get("pid"))
    note = str(state or "unknown")
    if state in {"succeeded", "failed", "timeout"}:
        note = str(state)
        alive = False
    elif not alive and state in {"queued", "running"}:
        note = "process gone"
    else:
        age = 0.0
        if at:
            try:
                t = datetime.fromisoformat(str(at))
                age = (datetime.now(timezone.utc) - t).total_seconds()
            except (TypeError, ValueError):
                age = 0.0
        if alive and age > wedge_sec:
            note = "wedged"
        elif alive:
            note = "working"
    return {"at": at, "alive": alive, "note": note}


def git_head(cwd: str | None) -> str | None:
    """HEAD of cwd, or None if not a git repo.  2s cap.  Never prints secrets."""
    if not cwd:
        return None
    try:
        out = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return None
    head = out.decode("utf-8", errors="replace").strip()
    return head or None


def iter_jobs() -> Iterator[JsonDict]:
    ensure_jobs_dir()
    for path in sorted(JOBS_DIR.glob("*.json")):
        if path.name.endswith(".tmp"):
            continue
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue


def running_grok_job(exclude_id: str | None = None) -> JsonDict | None:
    """A grok ACP job that is queued or whose pid is still alive."""
    for rec in iter_jobs():
        if rec.get("seat") != "grok":
            continue
        job_id = rec.get("jobId")
        if exclude_id and job_id == exclude_id:
            continue
        state = rec.get("state")
        if state == "queued":
            return rec
        if state == "running" and pid_alive(rec.get("pid")):
            return rec
    return None


def status_view(rec: JsonDict, wedge_sec: float) -> JsonDict:
    start = rec.get("gitHeadStart")
    now_head = git_head(rec.get("cwd"))
    git_moved = bool(start and now_head and start != now_head)
    stats = rec.get("stats") or {}
    return {
        "jobId": rec.get("jobId"),
        "seat": rec.get("seat"),
        "state": rec.get("state"),
        "elapsedMs": elapsed_ms(rec),
        "sessionId": rec.get("sessionId"),
        "bytesOut": stats.get("bytesOut") or 0,
        "lastTool": rec.get("lastTool"),
        "gitMoved": git_moved,
        "gitHead": now_head,
        "heartbeat": heartbeat_view(rec, wedge_sec),
        "partialTail": rec.get("partialTail") or "",
        "error": rec.get("error"),
    }


def result_view(rec: JsonDict) -> JsonDict:
    return {
        "jobId": rec.get("jobId"),
        "seat": rec.get("seat"),
        "state": rec.get("state"),
        "text": rec.get("text") or "",
        "exitCode": rec.get("exitCode"),
        "sessionId": rec.get("sessionId"),
        "lastTool": rec.get("lastTool"),
        "gitMoved": bool(
            rec.get("gitHeadStart")
            and git_head(rec.get("cwd"))
            and rec.get("gitHeadStart") != git_head(rec.get("cwd"))
        ),
        "stats": rec.get("stats") or {},
        "artifacts": rec.get("artifacts") or [],
        "error": rec.get("error"),
    }
