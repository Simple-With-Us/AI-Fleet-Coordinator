"""The one place fleet_facts starts a subprocess.

Argv lists only (never a shell), stdin closed, a timeout, and on a timeout the whole process group is
killed (the lane doctor starts git, gh and lsof children).  A missing program, a refusal to run it and a
timeout are results, not exceptions, so a gatherer can say "failed" and go on.

Executables are looked up on PATH plus the places a LaunchAgent's short PATH leaves out
(`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`).
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

MAX_OUTPUT_BYTES = 64 * 1024 * 1024


@dataclass
class Result:
    """What a run did.  `reason` is None on a normal exit (any status), else why there is no status."""
    rc: int | None
    out: str = ""
    err: str = ""
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.rc == 0 and self.reason is None


Runner = Callable[..., Result]


def extra_dirs() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin"]


def which(name: str) -> str | None:
    """`name` on PATH, then in the usual extra places.  A path with a slash is checked as given."""
    if os.sep in name:
        return name if os.access(name, os.X_OK) and os.path.isfile(name) else None
    found = shutil.which(name)
    if found:
        return found
    for directory in extra_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def home_path(*parts: str) -> Path:
    return Path(os.path.expanduser("~")).joinpath(*parts)


def run(argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None,
        cwd: str | None = None) -> Result:
    """Run `argv` (a list, never a string) and return its Result."""
    if isinstance(argv, (str, bytes)) or not argv:
        raise TypeError("argv must be a non-empty list")
    full_env = dict(os.environ)
    full_env.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0", "GH_PAGER": "cat"})
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=full_env, cwd=cwd, start_new_session=True)
    except FileNotFoundError:
        return Result(None, reason="not found")
    except PermissionError:
        return Result(None, reason="not executable")
    except OSError as exc:
        return Result(None, reason="could not start (%s)" % type(exc).__name__)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        return Result(None, _text(out), _text(err), reason="timed out after %ds" % int(timeout))
    if len(out) > MAX_OUTPUT_BYTES:
        return Result(proc.returncode, "", _text(err), reason="output too large")
    return Result(proc.returncode, _text(out), _text(err))


def _text(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


# ---- the shape every gatherer answers with --------------------------------------------------------
# {"status": "ok" | "absent" | "failed", "reason": str | None, "data": {...} | None}

def ok(data: dict, reason: str | None = None) -> dict:
    return {"status": "ok", "reason": reason, "data": data}


def absent(reason: str) -> dict:
    return {"status": "absent", "reason": reason, "data": None}


def failed(reason: str, data: dict | None = None) -> dict:
    return {"status": "failed", "reason": reason, "data": data}
