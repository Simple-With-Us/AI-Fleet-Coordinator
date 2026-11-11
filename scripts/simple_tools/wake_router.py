"""The wake router every multithreaded platform shares (OPS-1048;  owner design, Nov 10, 2026).

The chain.  The listener captures a tagged message to the seat inbox and POSTs one signed
simple-tools-wake/1 body to the platform's router.  The router picks the chat deterministically,
asks the one consultant in the Operator only when the rules cannot decide, and then delivers and
wakes the picked chat.  A platform supplies only how to wake one of its chats:  a `Platform` that
names its seat, its mention and its `deliver` function.

Writing a platform's `deliver`.  It is one plain function:

    def deliver(target: Target, wake: Wake) -> Delivery

`target` is the picked chat (`session_id`, `letter`, `host`, `title`, `thread_link`).  `wake` is
metadata only (`wake_id`, `message_id`, `channel`, `topic`, `zulip_link`, `sender`, `dm`, `letter`),
and never holds the message body:  a platform's prompt names the thread and says to read it.  It
runs on a worker thread, may take as long as it needs, and is never called twice at once for the
same `session_id`.  It returns a `Delivery` whose `outcome` is one of:

    sent         handed over, with no proof it was read;  set `ack` to "confirmed" if you have proof
    completed    the chat ran the wake to the end (a headless resume that exited 0)
    busy         the chat cannot take it yet;  the wake waits, and this uses up no attempt
    unreachable  the chat's host did not answer;  the wake waits, and this uses up no attempt
    failed       this try failed;  it is retried up to `max_attempts`
    refused      it can never work, or a retry could run the turn twice;  no retry

Any exception counts as `failed`.  `route`, `code` and `note` are short fixed codes for the receipt,
never message text and never a credential.

The answer and the durability.  The listener waits at most its routine timeout, resends with the
same id, and treats any 2xx as accepted, so it never retries after one.  So the POST only checks
and reserves:  the accepted wake is written to `pending/<wake_id>.json` and fsynced BEFORE the 202
goes out, a worker does the pick and the delivery, a restart replays whatever is still pending, and
the entry is dropped only once the final receipt is written.  An id with a final receipt answers
that record;  an id still pending answers 202 again and starts nothing.  Delivery is at least once:
a crash in the middle of a delivery replays it, and the receipt says `replayed`.

A later wake for a chat being delivered to waits for it and runs after it, in order;  it is not
failed as busy.  A wake that still cannot be delivered ends in a `dead` receipt, visible in the
state directory and at GET /receipt/<id>, and its message is in the seat inbox regardless.

The test flag.  A body with `"test": true` is answered at once with the pick and its reason (the
fields in `test_answer`), writes a receipt marked as a test and delivers nothing.  It never calls
the consultant and never reserves its id.

The answers carry no `ok` field on purpose:  the listener reads a body with a boolean `ok` as the
hosted Worker's settled outcome (adapters.routine_answer), which this is not.

No message body is stored, logged or forwarded.  The excerpt is read for an addressed letter and
then dropped;  Zulip-authored names pass the secret scanner before they are kept.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from re import Pattern
from typing import Any

from . import registry_api, secretscan

CONTRACT = "simple-tools-wake/1"
MAX_BODY = 64 * 1024
WAKE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
STOP_WORDS = frozenset(["the", "and", "for", "with", "from", "this", "that", "ops", "afc"])
NAME_CHARS = 200
QUESTION_CHARS = 500
HISTORY_LIMIT = 200
# Short enough that two reads fit inside the listener's default 15-second wait on a test wake.
TEST_REGISTRY_TIMEOUT = 6
REGISTRY_TIMEOUT = 15
DONE_KEEP_SECONDS = 30 * 86400
OUTCOMES = ("sent", "completed", "busy", "unreachable", "failed", "refused")

ROUTER_DEFAULTS: dict[str, Any] = {
    # Off until someone arms it:  with this false the router picks and receipts, and delivers nothing.
    "delivery_enabled": False,
    "workers": 4,
    "max_attempts": 5,
    "retry_backoff": [30, 120, 300, 900],
    "busy_retry_seconds": 30,
    "unreachable_retry_seconds": 300,
    "max_age_seconds": 86400,
}


@dataclass(frozen=True)
class Target:
    """One chat a wake can go to."""

    session_id: str
    letter: str = ""
    host: str = ""
    title: str = ""
    thread_link: str = ""


@dataclass(frozen=True)
class Wake:
    """A wake's metadata.  It has no field for the message body, by design."""

    wake_id: str
    seat: str = ""
    message_id: int = 0
    channel: str = ""
    topic: str = ""
    zulip_link: str = ""
    sender: str = ""
    dm: bool = False
    letter: str = ""


@dataclass(frozen=True)
class Delivery:
    """What a platform's `deliver` did.  See the module docstring for the outcomes."""

    outcome: str
    route: str = ""
    ack: str = "unknown"
    code: str = ""
    note: str = ""


@dataclass(frozen=True)
class Platform:
    """What one platform supplies to the shared router."""

    seat: str
    platform: str
    mention: str
    target_re: Pattern[str]
    deliver: Callable[[Target, Wake], Delivery]
    # The session ids among the candidates that must never be picked (a sub-agent, a headless run).
    exclude: Callable[[list[Target]], set[str]] | None = None
    stop_words: frozenset[str] = frozenset()


#: The seam for the Operator's one consultant.  It receives the candidates and the wake's metadata
#: and returns {"session_id": id} (a pick), {"question": text} (ask the sender) or None.  It decides;
#: it never answers.  A missing consultant, a None or an exception holds the wake with a receipt.
Consult = Callable[[list[Target], Wake], Mapping[str, Any] | None]


@dataclass(frozen=True)
class Pick:
    target: Target | None = None
    picked: str = ""  # letter | topic-match | most-recent | consultant | ""
    reason: str = ""
    letter_live: bool = False
    retry: bool = False
    undecided: str = ""  # topic-tie | ambiguous, when the rules could not decide
    question: str = ""
    candidates: tuple[str, ...] = ()


def clean(value: object, limit: int = NAME_CHARS) -> str:
    """Zulip-authored text as it may be kept:  one line, bounded, and withheld if secret-shaped."""
    text = " ".join(str(value or "").split())[:limit]
    return secretscan.WITHHELD if secretscan.scan(text) else text


def letter_of(payload: Mapping[str, Any], mention: str) -> str:
    """The letter the sender addressed, or "".  An explicit field wins, then a real mention anywhere
    in the excerpt (@**Name** AA, with or without Zulip's |id).  The plain "Name AA" form counts only
    at the very start of the body:  later in a message it is usually a reference to that chat, not
    an address.  A silent mention (@_**Name**) addresses nobody."""
    explicit = str(payload.get("letter") or "").strip()
    if re.fullmatch(r"[A-Z]{1,2}", explicit):
        return explicit
    excerpt = str(payload.get("excerpt") or "")
    name = re.escape(mention)
    found = re.search(r"@\*\*%s(?:\|\d+)?\*\*[ \t]*([A-Z]{2})\b" % name, excerpt)
    if found:
        return found.group(1)
    head, _, rest = excerpt.partition("\n")
    body = rest if head.startswith("BEGIN_UNTRUSTED") else excerpt
    found = re.match(r"\s*%s[ \t]+([A-Z]{2})\b" % name, body)
    return found.group(1) if found else ""


def wake_from_payload(payload: Mapping[str, Any], mention: str) -> Wake:
    message_id = payload.get("message_id")
    return Wake(
        wake_id=str(payload.get("wake_id") or ""),
        seat=str(payload.get("seat") or ""),
        message_id=message_id if type(message_id) is int else 0,
        channel=clean(payload.get("channel")),
        topic=clean(payload.get("topic")),
        zulip_link=clean(payload.get("zulip_link"), 500),
        sender=clean(payload.get("sender_full_name")),
        dm=payload.get("dm") is True,
        letter=letter_of(payload, mention),
    )


def atomic_write(path: Path, obj: Any) -> None:
    """One private file, fsynced, then renamed into place and its folder fsynced:  it survives a
    crash whole or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_config(
    path: Path, defaults: Mapping[str, Any], *, required: bool = False
) -> dict[str, Any]:
    """The defaults overlaid with a JSON file.  A file named explicitly must exist.  A file that is
    there must parse, name only known keys and match each default's type:  an unreadable or mistyped
    config is a refusal that names the path, never a quiet run on defaults."""
    config = dict(defaults)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise SystemExit("wake-router: the config file %s is not there" % path) from None
        return config
    except OSError as exc:
        raise SystemExit(
            "wake-router: the config file %s is unreadable (%s)" % (path, type(exc).__name__)
        ) from None
    try:
        loaded = json.loads(text)
    except ValueError:
        raise SystemExit("wake-router: the config file %s is not valid JSON" % path) from None
    if not isinstance(loaded, dict):
        raise SystemExit("wake-router: the config file %s is not a JSON object" % path)
    for key, value in loaded.items():
        if key not in defaults:
            raise SystemExit("wake-router: %s: unknown setting %s" % (path, key))
        if not _same_shape(value, defaults[key]):
            raise SystemExit("wake-router: %s: %s has the wrong type" % (path, key))
        config[key] = value
    return config


def _same_shape(value: object, default: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return type(value) is int and value > 0
    if isinstance(default, list):
        return (
            isinstance(value, list) and bool(value) and all(type(v) is int and v > 0 for v in value)
        )
    return isinstance(value, str)


def read_key(path: Path) -> str:
    """The routine key, from a 0600 file, on every request.  "" when it is missing, short or open
    to anyone else."""
    try:
        key = path.read_text(encoding="utf-8").strip()
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return ""
    return "" if mode & 0o077 or len(key) < 16 else key


class Router:
    """The pick, the durable queue, the workers and the receipts for one platform."""

    def __init__(
        self,
        platform: Platform,
        env: Mapping[str, str],
        config: Mapping[str, Any],
        state: Path,
        key_file: Path,
        consult: Consult | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.platform = platform
        self.env = env
        self.config = config
        self.state = state
        self.key_file = key_file
        self.consult = consult
        self.clock = clock
        self._entries: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._cond = threading.Condition()
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ---- the pick ---------------------------------------------------------------------------

    def _valid(self, row: Mapping[str, Any]) -> Target | None:
        sid = str(row.get("native_session_id") or "")
        if not self.platform.target_re.fullmatch(sid):
            return None  # a crafted row can never steer an argv token a child reads as a flag
        if str(row.get("seat") or self.platform.seat).upper() != self.platform.seat:
            return None
        return Target(
            session_id=sid,
            letter=str(row.get("label") or row.get("letter") or ""),
            host=str(row.get("verified_host") or ""),
            title=str(row.get("title") or ""),
            thread_link=str(row.get("thread_link") or ""),
        )

    def _excluded(self, targets: list[Target]) -> set[str]:
        return self.platform.exclude(targets) if self.platform.exclude and targets else set()

    def _words(self, text: str) -> set[str]:
        stop = STOP_WORDS | self.platform.stop_words | {self.platform.platform}
        return {w.lower() for w in WORD_RE.findall(text or "") if w.lower() not in stop}

    def pick(self, wake: Wake, *, consult: bool = True, timeout: int = REGISTRY_TIMEOUT) -> Pick:
        """The rules, in order:  an addressed letter;  the one chat whose title best shares the
        topic's words;  the only live chat.  A tie or several chats with no match go to the
        consultant.  A registry that cannot be read asks for a retry:  it is not "no live chat"."""
        letter_live = False
        if wake.letter:
            answer = registry_api.call(
                self.env,
                "resolve",
                {"platform": self.platform.platform, "letter": wake.letter},
                timeout=timeout,
            )
            if "error" in answer:
                return Pick(reason="registry-unavailable", retry=True)
            target = self._valid(answer) if answer.get("found") else None
            if target is not None:
                letter_live = True
                if target.session_id in self._excluded([target]):
                    return Pick(reason="letter-target-excluded", letter_live=True)
                return Pick(target=target, picked="letter", reason="letter", letter_live=True)
        answer = registry_api.call(
            self.env,
            "history",
            {"platform": self.platform.platform, "limit": HISTORY_LIMIT},
            timeout=timeout,
        )
        if "error" in answer:
            return Pick(reason="registry-unavailable", retry=True)
        live: dict[str, Target] = {}
        for row in answer.get("items") or []:
            if not isinstance(row, dict):
                continue
            if row.get("retired_at") or row.get("closed_at") or row.get("state") == "closed":
                continue
            target = self._valid(row)
            if target is not None and target.session_id not in live:
                live[target.session_id] = target
        excluded = self._excluded(list(live.values()))
        candidates = [t for sid, t in live.items() if sid not in excluded]
        topic_words = self._words(wake.topic)
        scored = [(len(topic_words & self._words(t.title)), t) for t in candidates]
        best = max((score for score, _ in scored), default=0)
        if best > 0:
            top = [t for score, t in scored if score == best]
            if len(top) == 1:
                return Pick(target=top[0], picked="topic-match", reason="topic-match")
            return self._ask(top, wake, "topic-tie", consult)
        if len(candidates) == 1:
            return Pick(target=candidates[0], picked="most-recent", reason="sole-live-chat")
        if not candidates:
            return Pick(reason="no-live-session", letter_live=letter_live)
        return self._ask(candidates, wake, "ambiguous", consult)

    def _ask(self, candidates: list[Target], wake: Wake, why: str, consult: bool) -> Pick:
        letters = tuple(t.letter for t in candidates)

        def hold(reason: str, question: str = "") -> Pick:
            return Pick(reason=reason, undecided=why, question=question, candidates=letters)

        if not consult:
            return hold("needs-consultant")
        if self.consult is None:
            return hold("consultant-missing")
        try:
            answer = self.consult(list(candidates), wake)
        except Exception:  # noqa: BLE001 - an Operator failure holds the wake, never crashes a worker
            return hold("consultant-unavailable")
        if not isinstance(answer, Mapping):
            return hold("consultant-none")
        sid = answer.get("session_id")
        if sid:
            for target in candidates:
                if target.session_id == sid:
                    return Pick(
                        target=target,
                        picked="consultant",
                        reason="consultant-pick",
                        undecided=why,
                        candidates=letters,
                    )
            return hold("consultant-invalid")
        question = clean(answer.get("question"), QUESTION_CHARS)
        return hold("consultant-asked", question) if question else hold("consultant-none")

    # ---- accepting --------------------------------------------------------------------------

    def _pending(self, wake_id: str) -> Path:
        return self.state / "pending" / (wake_id + ".json")

    def _done(self, wake_id: str) -> Path:
        return self.state / "done" / (wake_id + ".json")

    def accept(self, wake: Wake) -> tuple[int, dict[str, Any]]:
        """Reserve one wake durably and answer.  A failure to write is a 503, so the listener
        retries it;  a 202 is only ever sent for a wake that is on disk."""
        with self._cond:
            record = _read_json(self._done(wake.wake_id))
            if isinstance(record, dict):
                return 200, record
            held = self._entries.get(wake.wake_id)
            if held is not None:
                return 202, self._accepted(held)
            entry = {
                "wake": asdict(wake),
                "receipt_id": secrets.token_hex(8),
                "accepted_at": self.clock(),
                "next_at": 0.0,
                "attempts": 0,
                "failures": 0,
                "pick": None,
            }
            try:
                atomic_write(self._pending(wake.wake_id), entry)
            except OSError as exc:
                self._log("accept-write-failed-%s" % type(exc).__name__, wake.wake_id)
                return 503, {"error": "the wake could not be recorded", "wake_id": wake.wake_id}
            self._entries[wake.wake_id] = entry
            self._cond.notify_all()
            return 202, self._accepted(entry)

    @staticmethod
    def _accepted(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "wake_id": entry["wake"]["wake_id"],
            "receipt_id": entry["receipt_id"],
            "status": "accepted",
        }

    def test_answer(self, wake: Wake) -> dict[str, Any]:
        """A test wake's answer:  `test`, `wake_id`, `receipt_id`, `picked`, `picked_letter`,
        `session_id`, `reason`, `delivered`.  It delivers nothing and reserves nothing."""
        pick = self.pick(wake, consult=False, timeout=TEST_REGISTRY_TIMEOUT)
        answer = self._receipt(
            {"wake": asdict(wake), "receipt_id": secrets.token_hex(8), "attempts": 0},
            pick,
            status="test",
            delivered=False,
            result=None,
        )
        answer["test"] = True
        self._append_log(answer)
        return answer

    def receipt(self, wake_id: str) -> dict[str, Any] | None:
        """A wake's record:  the final receipt, or its accepted state while it is pending."""
        with self._cond:
            record = _read_json(self._done(wake_id))
            if isinstance(record, dict):
                return record
            entry = self._entries.get(wake_id)
            return self._accepted(entry) if entry is not None else None

    # ---- the workers ------------------------------------------------------------------------

    def start(self) -> None:
        """Replay what a restart left pending, then start the workers."""
        self.replay()
        for _ in range(int(self.config["workers"])):
            thread = threading.Thread(target=self._loop, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=5)

    def replay(self) -> int:
        """Load every pending file.  One with a final receipt finished just before a crash, so it
        is only removed;  one that cannot be read stays on disk and is named on stderr."""
        self._prune()
        folder = self.state / "pending"
        loaded = 0
        with self._cond:
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                wake_id = path.name[: -len(".json")]
                if self._done(wake_id).exists():
                    path.unlink(missing_ok=True)
                    continue
                entry = _read_json(path)
                try:
                    if entry["wake"]["wake_id"] != wake_id:
                        raise KeyError(wake_id)
                    Wake(**entry["wake"])
                except (KeyError, TypeError):
                    self._log("pending-unreadable", wake_id)
                    continue
                entry["replayed"] = True
                entry["next_at"] = 0.0
                self._entries[wake_id] = entry
                loaded += 1
            self._cond.notify_all()
        return loaded

    def _prune(self) -> None:
        folder = self.state / "done"
        cutoff = self.clock() - DONE_KEEP_SECONDS
        for path in folder.glob("*.json") if folder.is_dir() else []:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _claim(self) -> dict[str, Any] | None:
        """The oldest wake that can run now, under the lock.  A wake behind one that is waiting for
        the same chat stays behind it, so one chat's wakes run in the order they came."""
        now = self.clock()
        blocked: set[str] = set(self._active)
        ordered = sorted(self._entries.values(), key=lambda e: e["accepted_at"])
        for entry in ordered:
            session = _session_of(entry)
            if entry.get("running"):
                continue
            if session and session in blocked:
                continue
            if entry["next_at"] > now:
                if session:
                    blocked.add(session)
                continue
            entry["running"] = True
            return entry
        return None

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
                entry = self._claim()
                if entry is None:
                    self._cond.wait(timeout=1.0)
                    continue
            self._run(entry)

    def run_once(self) -> bool:
        """Run one claimable wake on this thread, or answer False.  The workers' body, for tests."""
        with self._cond:
            entry = self._claim()
        if entry is None:
            return False
        self._run(entry)
        return True

    def _run(self, entry: dict[str, Any]) -> None:
        try:
            self._process(entry)
        except Exception as exc:  # noqa: BLE001 - one bad wake must not take a worker with it
            self._log("worker-error-%s" % type(exc).__name__, entry["wake"]["wake_id"])
            self._retry(entry, counted=True, code="worker-error-%s" % type(exc).__name__)

    def _process(self, entry: dict[str, Any]) -> None:
        wake = Wake(**entry["wake"])
        if entry["pick"] is None:
            pick = self.pick(wake)
            if pick.retry:
                self._retry(entry, counted=True, code=pick.reason)
                return
            entry["pick"] = asdict(pick)
            self._save(entry)
        pick = _pick_from(entry["pick"])
        if pick.target is None:
            self._finish(entry, pick, status="held", delivered=False, result=None)
            return
        if not self.config["delivery_enabled"]:
            held = replace(pick, reason="delivery-disabled")
            self._finish(entry, held, status="held", delivered=False, result=None)
            return
        session = pick.target.session_id
        with self._cond:
            if session in self._active:
                # Another wake is being delivered to this chat:  wait behind it, using no attempt.
                entry["running"] = False
                self._cond.notify_all()
                return
            self._active.add(session)
        try:
            entry["attempts"] += 1
            self._save(entry)
            try:
                result = self.platform.deliver(pick.target, wake)
            except Exception as exc:  # noqa: BLE001 - a platform's failure is a failed try
                result = Delivery("failed", code="deliver-error-%s" % type(exc).__name__)
        finally:
            with self._cond:
                self._active.discard(session)
                self._cond.notify_all()
        self._settle(entry, pick, result)

    def _settle(self, entry: dict[str, Any], pick: Pick, result: Delivery) -> None:
        outcome = result.outcome if result.outcome in OUTCOMES else "failed"
        if outcome in ("sent", "completed"):
            # A hand-over with no proof is recorded as unknown (null), never as delivered.
            delivered = True if outcome == "completed" or result.ack == "confirmed" else None
            self._finish(entry, pick, status="delivered", delivered=delivered, result=result)
        elif outcome == "refused":
            self._finish(entry, pick, status="dead", delivered=False, result=result)
        elif outcome == "failed":
            self._retry(entry, counted=True, code=result.code or "failed", pick=pick, result=result)
        else:
            self._retry(entry, counted=False, code=result.code or outcome, pick=pick, result=result)

    def _retry(
        self,
        entry: dict[str, Any],
        *,
        counted: bool,
        code: str,
        pick: Pick | None = None,
        result: Delivery | None = None,
    ) -> None:
        now = self.clock()
        if counted:
            entry["failures"] += 1
        too_many = entry["failures"] >= int(self.config["max_attempts"])
        too_old = now - entry["accepted_at"] >= int(self.config["max_age_seconds"])
        if too_many or too_old:
            final = result or Delivery("failed", code=code)
            self._finish(
                entry, pick or Pick(reason=code), status="dead", delivered=False, result=final
            )
            return
        if counted:
            backoff = self.config["retry_backoff"]
            wait = backoff[min(entry["failures"] - 1, len(backoff) - 1)]
        elif result is not None and result.outcome == "busy":
            wait = self.config["busy_retry_seconds"]
        else:
            wait = self.config["unreachable_retry_seconds"]
        entry["next_at"] = now + wait
        # Saved while this worker still owns the entry:  once it is released another worker may
        # finish it, and a late save would bring its pending file back.
        self._save(entry)
        with self._cond:
            entry["running"] = False
            self._cond.notify_all()

    def _save(self, entry: Mapping[str, Any]) -> None:
        """Keep the pending file current, so a replay keeps the pick and the attempt counts.  A
        failure here costs only those counts:  the wake is already on disk."""
        record = {k: v for k, v in entry.items() if k != "running"}
        try:
            atomic_write(self._pending(entry["wake"]["wake_id"]), record)
        except OSError as exc:
            self._log("pending-write-failed-%s" % type(exc).__name__, entry["wake"]["wake_id"])

    # ---- the receipt ------------------------------------------------------------------------

    def _receipt(
        self,
        entry: Mapping[str, Any],
        pick: Pick,
        *,
        status: str,
        delivered: bool | None,
        result: Delivery | None,
    ) -> dict[str, Any]:
        wake = entry["wake"]
        target = pick.target
        return {
            "receipt_id": entry["receipt_id"],
            "wake_id": wake["wake_id"],
            "seat": self.platform.seat,
            "at": self.clock(),
            "test": False,
            "status": status,
            "letter": wake["letter"],
            "letter_live": pick.letter_live,
            "picked": pick.picked,
            "picked_letter": target.letter if target else "",
            "session_id": target.session_id if target else "",
            "host": target.host if target else "",
            "thread_link": target.thread_link if target else "",
            "reason": pick.reason,
            "undecided": pick.undecided,
            "candidates": list(pick.candidates),
            "question": pick.question,
            "delivered": delivered,
            "route": result.route if result else "",
            "ack": result.ack if result else "",
            "code": result.code if result else "",
            "note": result.note if result else "",
            "attempts": entry["attempts"],
            "replayed": bool(entry.get("replayed")),
            "channel": wake["channel"],
            "topic": wake["topic"],
            "zulip_link": wake["zulip_link"],
        }

    def _finish(
        self,
        entry: dict[str, Any],
        pick: Pick,
        *,
        status: str,
        delivered: bool | None,
        result: Delivery | None,
    ) -> None:
        """Write the final receipt, then drop the pending entry, in that order:  a crash between
        the two leaves a pending file that replay removes, never a wake with no record."""
        wake_id = entry["wake"]["wake_id"]
        receipt = self._receipt(entry, pick, status=status, delivered=delivered, result=result)
        try:
            atomic_write(self._done(wake_id), receipt)
        except OSError as exc:
            # The wake stays pending and tries to finish again;  it is not lost.
            self._log("receipt-write-failed-%s" % type(exc).__name__, wake_id)
            with self._cond:
                entry["next_at"] = self.clock() + float(self.config["busy_retry_seconds"])
                entry["running"] = False
                self._cond.notify_all()
            return
        self._append_log(receipt)
        with self._cond:
            self._pending(wake_id).unlink(missing_ok=True)
            self._entries.pop(wake_id, None)
            self._cond.notify_all()
        self._log("%s %s" % (status, receipt["reason"] or receipt["code"]), wake_id)

    def _append_log(self, receipt: Mapping[str, Any]) -> None:
        path = self.state / "wakes.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(receipt, sort_keys=True) + "\n")
        except OSError as exc:
            self._log("log-write-failed-%s" % type(exc).__name__, str(receipt.get("wake_id")))

    @staticmethod
    def _log(code: str, wake_id: str) -> None:
        """Codes and wake ids only:  no payload, no names, no key."""
        sys.stderr.write("wake-router: %s %s\n" % (wake_id, code))


def _session_of(entry: Mapping[str, Any]) -> str:
    pick = entry.get("pick") or {}
    target = pick.get("target") or {}
    return str(target.get("session_id") or "")


def _pick_from(record: Mapping[str, Any]) -> Pick:
    fields = dict(record)
    target = fields.get("target")
    fields["target"] = Target(**target) if target else None
    fields["candidates"] = tuple(fields.get("candidates") or ())
    return Pick(**fields)


def make_handler(router: Router) -> type[BaseHTTPRequestHandler]:
    seat = router.platform.seat
    wake_path = "/wake/%s" % seat

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # the default prints paths
            pass

        def _answer(self, code: int, payload: Mapping[str, Any]) -> None:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            key = read_key(router.key_file)
            if not key:
                self._answer(503, {"error": "the routine key is missing or unreadable"})
                return False
            given = (self.headers.get("Authorization") or "").encode("utf-8")
            # Constant time:  the bearer is the only thing between the network and a wake.
            if not hmac.compare_digest(given, ("Bearer %s" % key).encode("utf-8")):
                self._answer(401, {"error": "bad bearer"})
                return False
            return True

        def do_GET(self) -> None:
            if self.path == "/health":
                self._answer(200, {"seat": seat, "healthy": True})
                return
            found = re.fullmatch(r"/receipt/([A-Za-z0-9][A-Za-z0-9._-]{0,127})", self.path or "")
            if not found:
                self._answer(404, {"error": "not found"})
                return
            if not self._authorized():
                return
            record = router.receipt(found.group(1))
            if record is None:
                self._answer(404, {"error": "no such wake"})
            else:
                self._answer(200, record)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.path != wake_path:
                self._answer(404, {"error": "not found"})
                return
            raw_length = (self.headers.get("Content-Length") or "").strip()
            if not raw_length.isdigit() or len(raw_length) > 9:
                self._answer(400, {"error": "Content-Length missing or malformed"})
                return
            length = int(raw_length)
            if length <= 0 or length > MAX_BODY:
                self._answer(413, {"error": "body size refused"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._answer(400, {"error": "body is not JSON"})
                return
            if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
                self._answer(400, {"error": "unknown contract"})
                return
            if str(payload.get("seat") or "") != seat:
                self._answer(403, {"error": "seat mismatch"})
                return
            if not WAKE_ID_RE.fullmatch(str(payload.get("wake_id") or "")):
                self._answer(400, {"error": "wake_id missing or malformed"})
                return
            wake = wake_from_payload(payload, router.platform.mention)
            if payload.get("test") is True:
                self._answer(200, router.test_answer(wake))
                return
            status, answer = router.accept(wake)
            self._answer(status, answer)

    return Handler


def serve(router: Router, bind: str, port: int) -> int:
    """Run the router.  It refuses to start without its registry configuration or its key:  a
    router missing either would answer every wake and look healthy doing it."""
    if not registry_api.base_url(router.env):
        raise SystemExit("wake-router: the registry API URL is not set")
    if not read_key(router.key_file):
        raise SystemExit("wake-router: the routine key file is missing, short or group-readable")
    router.start()
    server = ThreadingHTTPServer((bind, port), make_handler(router))
    server.daemon_threads = True
    print(
        "wake-router listening on %s:%d for seat %s" % (bind, port, router.platform.seat),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        router.stop()
    return 0
