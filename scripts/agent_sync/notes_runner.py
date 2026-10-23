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
from datetime import UTC, datetime, timedelta
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


FUTURE_ALLOWANCE = timedelta(
    minutes=5
)  # a ts later than the runner's clock by more than this is not believed


def _moment(ts: Any, now: datetime) -> datetime | None:
    """A log ts as a time, or None when it cannot be read or is in the future (a clock that ran ahead must never
    become the floor every later resend is cut at)."""
    moment = N._entry_time({"ts": ts}) if isinstance(ts, str) and ts else None
    return moment if moment is not None and moment <= now + FUTURE_ALLOWANCE else None


def _not_older(entries: list[dict[str, Any]], since: str, now: datetime) -> list[dict[str, Any]]:
    """The log entries whose ts is not older than `since` (all of them when `since` is empty, unreadable or in the
    future).  An entry whose ts cannot be read is kept:  the Operator's id check is the last word on it."""
    floor = _moment(since, now)
    if floor is None:
        return list(entries)
    out = []
    for entry in entries:
        moment = N._entry_time(entry)
        if moment is None or moment >= floor:
            out.append(entry)
    return out


def _newest(entries: list[dict[str, Any]], since: str, now: datetime) -> str:
    """The latest believable ts among `entries` and `since` (a future one is passed over)."""
    best_time = _moment(since, now)
    best = since if best_time is not None else ""
    for entry in entries:
        moment = _moment(entry.get("ts"), now)
        if moment is not None and (best_time is None or moment > best_time):
            best, best_time = str(entry.get("ts")), moment
    return best


def _error_text(exc: BaseException, where: str = "") -> str:
    """One line for the state file and the log:  a NotesError's code and its own message (which names a missing key
    by its variable), else only the exception's type name, since its text may hold anything."""
    prefix = where + ":  " if where else ""
    if isinstance(exc, N.NotesError):
        return "%s%s: %s" % (prefix, exc.code, exc.message[:200])
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
        """Send the script's new log lines (at most LOG_BATCH per call) and move the cursor past what the Operator
        took.  A rotation is followed:  the rest of the rotated file first, then the new one from its start."""
        path = N.action_log_path(self.env, self.home)
        if path is None:
            return 0
        state = self._state()
        cursor = state.get("log") if isinstance(state.get("log"), dict) else {}
        try:
            current = os.stat(path)
        except OSError:
            return 0
        sent = 0
        inode, offset = cursor.get("inode"), int(cursor.get("offset") or 0)
        # The ts of the newest line already sent.  Every resend (a rotated copy, or a file truncated, replaced or
        # rotated out of reach) skips the lines older than it, so a resend never holds more lines than the Operator's
        # id window (LOG_IDS_KEEP) can tell apart;  a line with the same ts is sent again and dropped there by its id.
        since = cursor.get("ts") if isinstance(cursor.get("ts"), str) else ""
        if inode is not None and inode != current.st_ino:
            # The cursor's file has rotated, perhaps more than once while the runner was off.  Found among the copies:
            # the rest of it from the exact offset, then each newer copy whole, by position (lines are not always in
            # ts order, so no ts filter).  Not found (rotated out of reach, or replaced):  every copy still kept from
            # its start, skipping the lines older than the newest one already sent.
            copies = [
                path.with_name("%s.%d" % (path.name, n))
                for n in range(1, N.ACTION_LOG_ROTATIONS + 1)
            ]
            found = None
            for n, copy in enumerate(copies):
                try:
                    if os.stat(copy).st_ino == inode:
                        found = n
                        break
                except OSError:
                    continue
            first = found if found is not None else len(copies) - 1
            for n in range(first, -1, -1):
                try:
                    copy_inode = os.stat(copies[n]).st_ino
                except OSError:
                    continue
                count, since = self._ship_from(
                    copies[n],
                    copy_inode,
                    offset if n == found else 0,
                    since,
                    by_ts=found is None,
                )
                sent += count
            offset = 0
        by_ts = offset > current.st_size  # truncated in place:  no position to resume from
        if by_ts:
            offset = 0
        count, since = self._ship_from(path, current.st_ino, offset, since, by_ts=by_ts)
        return sent + count

    def _ship_from(
        self, path: Path, inode: int, offset: int, since: str, *, by_ts: bool
    ) -> tuple[int, str]:
        """Send one file's lines from `offset` (only those not older than `since` when `by_ts`), saving the cursor
        (this file's inode, the offset past the batch, and `since`) after each batch, so a send that fails part way
        resumes from the exact place on the next pass.  (lines sent, the new `since`)."""
        now = datetime.fromtimestamp(self.clock(), UTC)
        lines, end = self._read_lines(path, offset)
        sent = 0
        for start in range(0, len(lines), LOG_BATCH):
            batch = lines[start : start + LOG_BATCH]
            entries = [entry for _, entry in batch]
            if by_ts:
                entries = _not_older(entries, since, now)
            if entries:
                self.client.send_log(entries)
                sent += len(entries)
                since = _newest(entries, since, now)
            self._note(
                log={"inode": inode, "offset": batch[-1][0], "ts": since},
                log_shipped_at=N.iso_now(self.clock),
            )
        self._note(
            log={"inode": inode, "offset": end, "ts": since}
        )  # past every whole line, the unparsable ones too
        return sent, since

    @staticmethod
    def _read_lines(path: Path, offset: int) -> tuple[list[tuple[int, dict[str, Any]]], int]:
        """[(the offset after the line, the entry)] for every whole line from `offset`, and the offset of the last
        whole line's end.  A torn last line (a write in progress) is left for the next pass."""
        out: list[tuple[int, dict[str, Any]]] = []
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(8 * 1024 * 1024)
        except OSError:
            return out, offset
        position = offset
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                break
            position += len(raw)
            try:
                entry = N.clean_log_entry(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
            if entry is not None:
                out.append((position, entry))
        return out, position

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
