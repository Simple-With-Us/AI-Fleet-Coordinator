"""Running `board` and `simple-chat`.

Both are run as argv lists (never through a shell), with identity variables removed from the child's
environment.  Free text never rides on argv:  the board reads a description or a comment from a private
temp file (mode 0600, removed afterwards) and `simple-chat` reads a message from standard input, so
nothing a filer wrote shows up in `ps` or in a shell history.  Each tool is looked up on PATH, then in
~/.local/bin (a routine's PATH can be short).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from . import seat as seatmod
from .errors import BoardFailed, NeedsOwnerError

BOARD_TIMEOUT = 240  # seconds:  the board waits out a Linear rate limit itself
AGENT_SYNC_TIMEOUT = 90


@dataclass
class Result:
    code: int
    out: str = ""
    err: str = ""
    timed_out: bool = False
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out and not self.missing


def find_tool(name: str, environ: Mapping[str, str]) -> str | None:
    if (
        name == "simple-board"
    ):  # the board is the managed install of this account, never a PATH or HOME lookup
        return seatmod.managed_board()
    found = shutil.which(name, path=environ.get("PATH"))
    if found:
        return found
    home = environ.get("HOME") or os.path.expanduser(
        "~"
    )  # the caller's HOME:  a test points it at a temp directory
    candidate = os.path.join(home, ".local", "bin", name)
    return candidate if os.access(candidate, os.X_OK) else None


def first_line(text: str, cap: int = 200) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:cap]
    return ""


class Runner:
    def __init__(self, seat: str | None, environ: Mapping[str, str] | None = None) -> None:
        """`seat` is the verified seat for a writer (every child loses AGENT_SEAT and the bearer locator picks the identity);  a reader passes
        None and the environment goes through as it is."""
        self.seat = seat or ""
        base = os.environ if environ is None else environ
        self.env = seatmod.child_environment(base) if seat else dict(base)

    # ---- private files ---------------------------------------------------------------------
    @contextlib.contextmanager
    def private_file(self, text: str) -> Iterator[str]:
        """A 0600 file holding `text`, deleted when the block ends."""
        fd, path = tempfile.mkstemp(prefix=".needs-owner-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(path, 0o600)
            yield path
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    # ---- children --------------------------------------------------------------------------
    def _run(
        self,
        tool: str,
        args: Sequence[str],
        stdin: str | None,
        timeout: int,
        env: Mapping[str, str] | None = None,
    ) -> Result:
        exe = find_tool(tool, self.env)
        if exe is None:
            return Result(127, err="%s is not installed or not on PATH" % tool, missing=True)
        try:
            done = subprocess.run(
                [exe, *args],
                input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(env) if env is not None else self.env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Result(
                124, err="%s timed out after %d seconds" % (tool, timeout), timed_out=True
            )
        except OSError as exc:
            return Result(
                126,
                err="%s could not be run (%s)" % (tool, exc.strerror or type(exc).__name__),
                missing=True,
            )
        return Result(done.returncode, done.stdout or "", done.stderr or "")

    def board(self, *args: str) -> Result:
        # The bearer-verified seat is the write gate, so a write needs no opt-in here.
        return self._run("simple-board", args, None, BOARD_TIMEOUT, dict(self.env))

    def board_json(self, *args: str):
        """Run a `board ... --json` read and return the parsed JSON, or raise BoardFailed."""
        result = self.board(*args)
        if not result.ok:
            raise BoardFailed(
                "board %s failed:  %s"
                % (args[0], first_line(result.err) or "exit %d" % result.code),
                result.code,
            )
        try:
            return json.loads(result.out)
        except ValueError:
            raise BoardFailed("board %s did not return JSON" % args[0]) from None

    def agent_sync(self, *args: str, stdin: str | None = None) -> Result:
        return self._run("simple-chat", args, stdin, AGENT_SYNC_TIMEOUT)

    def require_board(self) -> None:
        if find_tool("simple-board", self.env) is None:
            raise NeedsOwnerError("simple-board is not installed or not on PATH")
