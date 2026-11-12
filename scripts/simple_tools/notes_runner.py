"""The Mac runner for the Operator's notes queue (AFC-486, docs/protocols/operator-notes.md, "The Mac Runner").

`simple-notes runner` runs this resident under the LaunchAgent com.jay.operator-notes-runner.  It acts as no seat and
holds no Zulip key:  its one secret is OPERATOR_NOTES_RUNNER_KEY (notes_client.KeySource), and the RUNNER principal
can only claim requests, report results and send action-log lines.  Each pass:

  1. finish what a crash left:  a request recorded as started with an outcome is reported again;  a write recorded
     as started with no outcome is reported outcome_unknown (it may have written, so it is never run twice);  a read
     or list with no outcome is run again (reading twice changes nothing);
  2. claim one request, waiting up to 25 seconds for one;
  3. check it again with the same rules the Operator applied (notes.check_request and notes.request_problem for the
     seat it is for), record it as started, run apple-notes-coding.sh, record the outcome, then report it;
  4. send the action log's new lines to the Operator.

The state file (~/.local/state/operator/notes-runner.json, mode 600) holds the started requests (ids, the claim and
the outcome;  never a note body), the log cursor, and the last poll, success and error, which the doctor reads.
One runner at a time:  a second one finds the lock taken and exits.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import live as L
from . import notes as N
from . import notes_client as NC
from . import notes_wire as W

STATE_REL = Path(".local") / "state" / "operator" / "notes-runner.json"
LOCK_NAME = "notes-runner.lock"
LOG_BATCH = 200
ERROR_BACKOFF_S = (5.0, 15.0, 30.0, 60.0)


class AlreadyRunning(Exception):
    """Another runner holds the lock."""


#  One read of a log file;  the script rotates it at 5 MiB, so one read is a whole file.
LOG_READ = 8 * 1024 * 1024
#  How far back from the cursor its entry is looked for (the script's lines are a few hundred bytes).
LINE_WINDOW = 64 * 1024
#  The script holds its log lock only to rotate and append, so a few tries a short pause apart always get it.
LOG_LOCK_TRIES = 40
LOG_LOCK_PAUSE_S = 0.05


def _offset(value: Any) -> bool:
    """A file offset as the runner saves one:  a whole number, not a bool, not negative."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _entry(raw: bytes) -> dict[str, Any] | None:
    """One log line as a cleaned entry, or None when it is not one (not JSON, not an entry, or nested too deep)."""
    try:
        return N.clean_log_entry(json.loads(raw.decode("utf-8", "replace")))
    except (ValueError, RecursionError):
        return None


def _error_text(exc: BaseException, where: str = "") -> str:
    """One line for the state file and the log:  a NotesError's code and its own message (which names a missing key
    by its variable), else only the exception's type name, since its text may hold anything.  Text UTF-8 cannot hold
    (a lone surrogate from an answer's JSON) is replaced, or saving the state would fail and end the runner."""
    prefix = where + ":  " if where else ""
    if isinstance(exc, N.NotesError):
        return N.text_safe("%s%s: %s" % (prefix, exc.code, exc.message[:200]))
    return "%sinternal: %s" % (prefix, type(exc).__name__)


def _ids_ok(seat: Any, request_id: Any, claim: Any) -> bool:
    """A claim's seat and request_id in the Operator's own patterns, and a claim that is text:  they name the state
    entry and the report's path, so nothing else is used."""
    return (
        isinstance(seat, str)
        and bool(N.SEAT_RE.fullmatch(seat))
        and isinstance(request_id, str)
        and bool(N.REQUEST_ID_RE.fullmatch(request_id))
        and isinstance(claim, str)
        and bool(claim)
    )


class Runner:
    def __init__(
        self,
        client: NC.OperatorClient,
        *,
        env: Mapping[str, str],
        home: Path,
        state_path: Path | None = None,
        script_finder: Callable[[Mapping[str, str], Path], Path | None] = N.direct_available,
        run: Callable[..., dict[str, Any]] = N.run_direct,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.client = client
        self.env = env
        self.home = home
        self.state_path = state_path or home / STATE_REL
        self.script_finder = script_finder
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self._lock_fd: int | None = None

    # ---- state -----------------------------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        state = L.read_json(str(self.state_path))
        if not isinstance(state.get("inflight"), dict):
            state["inflight"] = {}
        return state

    def _save(self, state: Mapping[str, Any]) -> None:
        L.write_json(str(self.state_path), dict(state), durable=True)

    def _note(self, **fields: Any) -> None:
        state = self._state()
        state.update(fields)
        self._save(state)

    def acquire(self) -> None:
        L.private_dir(str(self.state_path.parent))
        fd = os.open(str(self.state_path.parent / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Only "held by someone else" means another runner.  A flock lock is released by the kernel when its
            # holder dies, so a crash never leaves it stale (a mkdir lock would).
            os.close(fd)
            raise AlreadyRunning("another notes runner is running on this Mac") from None
        except OSError:
            os.close(fd)
            raise
        self._lock_fd = fd

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    # ---- one pass --------------------------------------------------------------------------------
    def recover(self) -> None:
        """Report what a crash left behind (see the module header, step 1).  Each request is reported on its own:  a
        report that fails keeps only its own entry for the next pass, and the others still go out.  If any failed,
        the first failure is raised after the rest have been tried, so the loop backs off before it claims new work."""
        state = self._state()
        failed: Exception | None = None
        for key, entry in list(state["inflight"].items()):
            if not isinstance(entry, dict):
                state["inflight"].pop(key, None)
                self._save(state)  # dropped for good, not only in this pass's copy
                continue
            seat, request_id, claim = entry.get("seat"), entry.get("request_id"), entry.get("claim")
            if not _ids_ok(seat, request_id, claim):
                # Only this runner writes the file, after the same check;  an entry that fails it now cannot be
                # reported (its ids name the report's path), so it is dropped for good.
                self.log("notes-runner: dropped a recovery entry whose ids are malformed")
                state["inflight"].pop(key, None)
                self._save(state)
                continue
            # One boundary per entry, around running a read again as well as reporting.
            try:
                if "result" in entry or "error" in entry:
                    outcome = (
                        {"result": entry["result"]}
                        if "result" in entry
                        else {"error": entry["error"]}
                    )
                elif entry.get("action") in ("read", "list") and isinstance(
                    entry.get("request"), dict
                ):
                    outcome = self._execute(seat, entry["request"])
                else:
                    outcome = {
                        "error": {
                            "code": "outcome_unknown",
                            "message": "the Mac runner stopped while it ran this request, so the note may or may "
                            "not have been written.  Check the Coding folder, then send it again with a new "
                            "request_id.",
                        }
                    }
                self._deliver(seat, request_id, claim, outcome, entry.get("action"))
            except Exception as exc:  # noqa: BLE001 - one entry's failure, of any kind, never stops the others
                self.log(
                    "notes-runner: recovery report for %s failed:  %s"
                    % (request_id, getattr(exc, "code", type(exc).__name__))
                )
                failed = failed or exc
                continue
            state["inflight"].pop(key, None)
            self._save(state)
        if isinstance(failed, N.NotesError):
            raise failed
        if failed is not None:
            # An unexpected failure becomes a NotesError, which the loop backs off on, instead of ending the runner;
            # its entry stays for the next pass.  The type name only:  the error's text may hold anything.
            raise N.NotesError(
                "internal", "a recovery entry failed (%s)" % type(failed).__name__
            ) from failed

    def _deliver(
        self, seat: Any, request_id: Any, claim: Any, outcome: Mapping[str, Any], action: Any
    ) -> None:
        """Report one outcome.  A result the Operator refuses for good (400, 413:  a read too large to send back, say)
        is replaced by an error report, so the caller hears why instead of waiting the lease out;  either way the
        runner then lets the request go.  A network or server failure raises, and the entry stays for the next pass.
        A refused write result is reported outcome_unknown, never a plain refusal:  the script ran, so the note was
        written, and a caller told otherwise could write it again under a new request_id."""
        status = self.client.report(seat, request_id, claim, **outcome)
        if status in (400, 413) and "result" in outcome:
            why = "the Operator refused the runner's answer (HTTP %d)" % status
            if action in ("read", "list"):
                error = {
                    "code": "internal",
                    "message": "%s, for example a note too large to send back;  nothing was changed"
                    % why,
                }
            else:
                error = {
                    "code": "outcome_unknown",
                    "message": "%s after the script ran, so the note was most likely written.  Check it before "
                    "sending it again with a new request_id" % why,
                }
            self.client.report(seat, request_id, claim, error=error)

    def _execute(self, seat: str, request: Mapping[str, Any]) -> dict[str, Any]:
        """{"result": ...} or {"error": {...}} for one request.  Never raises NotesError."""
        try:
            checked = N.check_request(request)
            if checked["action"] in N.WRITE_ACTIONS:
                N.scan_write(
                    checked
                )  # the Operator's own scan, again:  first, so no refusal below can echo a secret
            problem = N.request_problem(checked, seat, env=self.env)
            if problem:
                raise N.NotesError("invalid_argument", problem[:400])
            script = self.script_finder(self.env, self.home)
            if script is None:
                raise N.NotesError(
                    "unavailable", "this Mac cannot run apple-notes-coding.sh;  nothing was written"
                )
            requester = seat if checked["action"] in N.BODY_ACTIONS else None
            return {
                "result": self.run(
                    checked, script, env=self.env, caller="runner", requester=requester
                )
            }
        except N.NotesError as exc:
            return {"error": {"code": exc.code, "message": exc.message[:400]}}
        except Exception as exc:  # noqa: BLE001 - never raises:  every request gets a report
            # Its type name only.  A create or update may have run, so it is never a plain refusal;  any other action
            # changed nothing that a retry could repeat.
            what = type(exc).__name__
            if isinstance(request, Mapping) and request.get("action") in ("create", "update"):
                return {
                    "error": {
                        "code": "outcome_unknown",
                        "message": "the Mac runner failed while it ran this request (%s), so the note may or may not "
                        "have been written.  Check the Coding folder before sending it again."
                        % what,
                    }
                }
            return {
                "error": {
                    "code": "internal",
                    "message": "the Mac runner failed while it ran this request (%s)" % what,
                }
            }

    def once(self, wait: int = W.CLAIM_WAIT_MAX) -> bool:
        """One claim, run and report.  True when a request was handled."""
        self.recover()
        claimed = self.client.claim(wait)
        self._note(last_poll_at=N.iso_now(self.clock))
        if claimed is None:
            return False
        seat, request_id, claim = (
            claimed.get("seat"),
            claimed.get("request_id"),
            claimed.get("claim"),
        )
        request = claimed.get("request") if isinstance(claimed.get("request"), dict) else {}
        if not _ids_ok(seat, request_id, claim) or request.get("request_id") != request_id:
            # Never run, and it cannot be reported (its ids name the report's path), so the queue's lease settles it.
            # Recorded for the doctor, with none of its values.
            what = "the Operator handed out a claim whose seat, request_id or claim is malformed;  it was not run"
            self._note(last_error_at=N.iso_now(self.clock), last_error="internal: " + what)
            self.log("notes-runner: " + what)
            return False
        key = "%s/%s" % (seat, request_id)
        action = request.get("action")
        entry: dict[str, Any] = {
            "seat": seat,
            "request_id": request_id,
            "claim": claim,
            "action": action,
            "started_at": N.iso_now(self.clock),
        }
        if action in ("read", "list"):
            entry["request"] = request  # no body:  a read can be run again after a crash
        state = self._state()
        state["inflight"][key] = entry
        self._save(state)
        outcome = self._execute(seat, request)
        if action not in ("read", "list"):
            # A write's outcome is recorded before it is reported, so a crash in between reports it again rather than
            # calling it unknown.  A read's is not:  it holds the note's text (or the folder's titles), which never
            # goes to disk here, and a read lost in a crash is simply run again.
            state = self._state()
            state["inflight"].setdefault(key, entry).update(outcome)
            self._save(state)
        self._deliver(seat, request_id, claim, outcome, action)
        state = self._state()
        state["inflight"].pop(key, None)
        state["last_ok_at"] = N.iso_now(self.clock)
        state["handled"] = int(state.get("handled") or 0) + 1
        self._save(state)
        self.log(
            "notes-runner: %s %s for %s:  %s"
            % (
                action,
                request_id,
                seat,
                "error " + outcome["error"]["code"] if "error" in outcome else "done",
            )
        )
        return True

    # ---- the action log ----------------------------------------------------------------------------
    def ship_log(self) -> int:
        """Send the script's new log lines (at most LOG_BATCH per call) and move the cursor past what the Operator took.
        The cursor names a file by its inode and two places in it:  `offset`, just past the last whole line read, and
        `at`, just past the last entry read, whose id is `id`.

        The runner resumes at `offset` while the file is still kept (the current log or a rotated copy) and the line
        before `at` is still that entry, then sends each newer file whole, oldest first.  When it cannot trust the
        place (the file rotated out of reach, or was replaced or cut short), it looks the id up in every kept file,
        newest first, and resumes just after it.  When no kept file holds the id, or there is no cursor yet, it sends
        every kept line, oldest first:  none is lost, and the Operator's id check drops those it already holds (it
        remembers more ids than the Mac keeps lines, notes_queue.LOG_IDS_KEEP)."""
        path = N.action_log_path(self.env, self.home)
        if path is None:
            return 0
        try:
            os.stat(path)
        except OSError:
            return 0
        files = self._kept_files(path)
        state = self._state()
        cursor = state.get("log") if isinstance(state.get("log"), dict) else {}
        first, offset, last_id, at = self._resume_point(files, cursor)
        if at is not None and (cursor.get("inode"), cursor.get("offset"), cursor.get("at")) != (
            files[first][1],
            offset,
            at,
        ):
            # Found by its id (the file was replaced, or cut short):  saved now, so the next pass trusts it without a
            # search even when no new line follows, and the doctor counts from the file the cursor names.
            self._note(log={"inode": files[first][1], "offset": offset, "id": last_id, "at": at})
        sent = 0
        for index in range(first, len(files)):
            file, inode = files[index]
            count, last_id, whole = self._ship_from(
                file,
                inode,
                offset if index == first else 0,
                last_id,
                at if index == first else None,
            )
            sent += count
            if not whole:
                break  # the file moved after it was listed (a rotation):  the next pass finds the place again
        return sent

    @staticmethod
    def _kept_files(path: Path) -> list[tuple[Path, int]]:
        """[(file, inode)] for the rotated copies and the current log that exist, oldest first.  They are listed under
        the script's own lock (.<name>.lock, which it holds to rotate and to append), so a rotation never lands between
        two names and leaves a file out of the list.  NotesError unavailable when the lock stays taken.  A lock file
        that can be neither made nor opened (a folder this user cannot write) means no script can take it either, so
        the names are listed without it."""
        lock = str(path.with_name("." + path.name + ".lock"))
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            try:
                fd = os.open(lock, os.O_RDONLY)
            except OSError:
                return Runner._list_kept(path)
        try:
            for attempt in range(LOG_LOCK_TRIES):
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt + 1 == LOG_LOCK_TRIES:
                        raise N.NotesError(
                            "unavailable",
                            "the action log's lock stayed taken, so the log was not read this pass",
                        ) from None
                    time.sleep(LOG_LOCK_PAUSE_S)
            return Runner._list_kept(path)
        finally:
            os.close(fd)  # closing it lets the lock go

    @staticmethod
    def _list_kept(path: Path) -> list[tuple[Path, int]]:
        out: list[tuple[Path, int]] = []
        for n in range(N.ACTION_LOG_ROTATIONS, -1, -1):
            file = path if n == 0 else path.with_name("%s.%d" % (path.name, n))
            try:
                out.append((file, os.stat(file).st_ino))
            except FileNotFoundError:
                continue
        return out

    def _resume_point(
        self, files: list[tuple[Path, int]], cursor: Mapping[str, Any]
    ) -> tuple[int, int, str | None, int | None]:
        """Where this pass starts:  (the index in `files`, the offset in that file, the id of the last entry sent, and
        the offset just past that entry when it is in this file)."""
        inode, offset, at, raw_id = (
            cursor.get("inode"),
            cursor.get("offset"),
            cursor.get("at"),
            cursor.get("id"),
        )
        # Only an id the runner could have written:  text UTF-8 can hold, as clean_log_entry leaves every id.
        last_id = (
            raw_id if isinstance(raw_id, str) and raw_id and N.text_safe(raw_id) == raw_id else None
        )
        if last_id is not None and _offset(offset) and _offset(at) and 0 < at <= offset:
            for index, (file, file_inode) in enumerate(files):
                if file_inode == inode:
                    if self._ends_entry(file, file_inode, at, offset, last_id):
                        return index, offset, last_id, at
                    break
        if last_id is not None:
            for index in range(len(files) - 1, -1, -1):
                after = self._after_id(*files[index], last_id)
                if after is not None:
                    return index, after, last_id, after
        return 0, 0, last_id, None

    @staticmethod
    def _ends_entry(path: Path, inode: int, at: int, offset: int, last_id: str) -> bool:
        """Whether `path` is still `inode`, still reaches `offset`, and the line that ends at `at` is still the entry
        with `last_id`.  Anything else (the file cut short, `at` inside a line, or another line there) means the file
        changed, so the place is found by the id instead."""
        try:
            with open(path, "rb") as fh:
                info = os.fstat(fh.fileno())
                if info.st_ino != inode or offset > info.st_size:
                    return False
                start = max(0, at - LINE_WINDOW)
                fh.seek(start)
                data = fh.read(at - start)
        except OSError:
            return False
        if not data.endswith(b"\n"):
            return False
        entry = _entry(data[data.rfind(b"\n", 0, len(data) - 1) + 1 : -1])
        return entry is not None and entry["id"] == last_id

    @staticmethod
    def _after_id(path: Path, inode: int, last_id: str) -> int | None:
        """The offset just past the last line in `path` (still `inode`) that is the entry with `last_id`, or None."""
        try:
            with open(path, "rb") as fh:
                if os.fstat(fh.fileno()).st_ino != inode:
                    return None
                data = fh.read(LOG_READ)
        except OSError:
            return None
        needle = last_id.encode("utf-8")
        found = len(data)
        while True:
            found = data.rfind(needle, 0, found)
            if found < 0:
                return None
            end = data.find(b"\n", found)
            if end < 0:
                continue  # inside a torn last line
            entry = _entry(data[data.rfind(b"\n", 0, found) + 1 : end])
            if entry is not None and entry["id"] == last_id:
                return end + 1

    def _ship_from(
        self, path: Path, inode: int, offset: int, last_id: str | None, at: int | None
    ) -> tuple[int, str | None, bool]:
        """Send one file's whole lines from `offset`, saving the cursor after each batch (this file's inode, the offset
        just past the batch, and its last entry's id and end), so a send that fails part way resumes at the exact place
        on the next pass;  and once more after the last whole line, when the pass got past one and the file holds an
        entry the cursor can be checked by (`at`, this file's, passed in for the file the cursor names).  (lines sent, the id of
        the last entry passed, whether the file was read to its end):  False when `path` is no longer `inode` (it
        rotated after it was listed)."""
        sent, start = 0, offset
        while True:
            read = self._read_lines(path, offset, inode)
            if read is None:
                return sent, last_id, False
            lines, end, more = read
            for first in range(0, len(lines), LOG_BATCH):
                batch = lines[first : first + LOG_BATCH]
                self.client.send_log([entry for _, entry in batch])
                sent += len(batch)
                at, last_id = batch[-1][0], batch[-1][1]["id"]
                self._note(
                    log={"inode": inode, "offset": at, "id": last_id, "at": at},
                    log_shipped_at=N.iso_now(self.clock),
                )
            if not more or end == offset:
                break
            offset = end
        # Past lines that are not entries too.  A pass that read nothing new writes nothing, and the cursor never moves
        # into a file with no entry of its own yet (it could not be checked there):  it stays where it was.
        if end > start and at is not None:
            self._note(log={"inode": inode, "offset": end, "id": last_id, "at": at})
        return sent, last_id, True

    @staticmethod
    def _read_lines(
        path: Path, offset: int, inode: int
    ) -> tuple[list[tuple[int, dict[str, Any]]], int, bool] | None:
        """[(the offset just past the line, the entry)] for each whole line from `offset` (one read of at most
        LOG_READ bytes), the offset just past the last whole line, and whether there may be more;  or None when `path`
        is gone or is no longer `inode`.  Only a newline ends a line, and a torn last line (a write in progress) is left
        for the next pass.  A file that cannot be read raises, so the loop records it and nothing after it is skipped."""
        try:
            fh = open(path, "rb")
        except FileNotFoundError:
            return None
        with fh:
            if os.fstat(fh.fileno()).st_ino != inode:
                return None
            fh.seek(offset)
            data = fh.read(LOG_READ)
            if len(data) == LOG_READ and b"\n" not in data:
                # One line longer than a whole read:  no log line is, so it is passed over like any line that is not
                # an entry, up to the next newline (or, while it has none, left for the next pass).
                position = offset + len(data)
                while True:
                    chunk = fh.read(LOG_READ)
                    if not chunk:
                        return [], offset, False
                    newline = chunk.find(b"\n")
                    if newline >= 0:
                        return [], position + newline + 1, True
                    position += len(chunk)
        out: list[tuple[int, dict[str, Any]]] = []
        position = offset
        for piece in data.split(b"\n")[:-1]:
            position += len(piece) + 1
            entry = _entry(piece)
            if entry is not None:
                out.append((position, entry))
        return out, position, len(data) == LOG_READ

    # ---- the loop ----------------------------------------------------------------------------------
    def loop(
        self,
        stop: threading.Event,
        wait: int = W.CLAIM_WAIT_MAX,
        *,
        code_stamp: Callable[[], Any] | None = None,
    ) -> None:
        """Run until `stop` is set, or until this package's code changes on disk:  the runtime-sync job moved the
        managed runtime, so the runner returns between requests (never inside one) and launchd's KeepAlive starts it
        again on the new code.  Restarting it from outside could kill a write half-way."""
        stamp = code_stamp or package_stamp
        started = stamp()
        failures = 0
        while not stop.is_set():
            if stamp() != started:
                self.log(
                    "notes-runner: the code changed on disk;  exiting so launchd starts the new one"
                )
                return
            # The log is a record:  shipping it never stops request handling, so it has its own boundary (a line it
            # cannot send would otherwise block every claim behind it).
            try:
                self.ship_log()
            except Exception as exc:  # noqa: BLE001
                self._note(last_error_at=N.iso_now(self.clock), last_error=_error_text(exc, "log"))
                self.log("notes-runner: %s" % _error_text(exc, "log"))
            try:
                self.once(wait)
                failures = 0
            except Exception as exc:  # noqa: BLE001 - nothing unexpected ends the runner:  it backs off
                self._note(last_error_at=N.iso_now(self.clock), last_error=_error_text(exc))
                self.log("notes-runner: %s" % _error_text(exc))
                delay = ERROR_BACKOFF_S[min(failures, len(ERROR_BACKOFF_S) - 1)]
                failures += 1
                stop.wait(delay)


def package_stamp() -> tuple[Any, ...]:
    """The size and modification time of every module in this package (and of `simple-notes`):  what a runtime update
    changes."""
    here = Path(__file__).resolve().parent
    out: list[Any] = []
    for path in sorted([*here.glob("*.py"), here.parent / "simple-notes"]):
        try:
            info = path.stat()
        except OSError:
            out.append((path.name, None))
            continue
        out.append((path.name, info.st_size, info.st_mtime_ns))
    return tuple(out)


def runner_for(env: Mapping[str, str], home: Path, **kwargs: Any) -> Runner:
    """The runner for this Mac:  the RUNNER principal's client, or NotesError not_configured."""
    return Runner(NC.operator_client(env, W.RUNNER), env=env, home=home, **kwargs)
