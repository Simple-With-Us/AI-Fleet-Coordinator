"""Signed Mac facts to the private fleet-loops-data repository, every 3 hours (open-loops F7).

    fleet-facts publish-mac [--dry-run] [--out DIR]       one cycle (what the LaunchAgent runs)
    fleet-facts publish --path REL --file LOCAL           sign (JSON) and write one file
    fleet-facts verify FILE [--path REL]                  check a signed file (cli.py)

One cycle:

  1. take the lock (a second run exits), start a 20-minute wall-clock cap, and skip the whole run when load1 is
     over 250 (the last published files stay where they are, the status file says they are stale, and nothing
     is re-dated:  their `generated_at` keeps ageing, which is how a consumer knows);
  2. fetch the signing key through K1.  No key means nothing is published;
  3. gather the Mac parts (lanes, autopush, backup, needs_owner) with the F6 gatherers, then the full list
     (`open-loops gather` and `open-loops render --markdown`, skipped and reported when open-loops is not installed);
  4. build the documents:  per-field redaction (the key itself is a `known` value), a whole-document secret scan that
     fails closed, the minimized Grok Bot copy (minimize.py), and an HMAC signature on each JSON file (signing.py);
  5. repo.guard() (private, no webhook, no deploy key, no app that can read it), then sha-aware PUTs:
     gb/full-list.md, gb/mac-facts.json and, only when the Infisical knob FLEET_FACTS_PUBLISH_FULL_COPY is on, mac-facts.json
     (it keeps lane paths, and whatever can read the repository can read it).  The Markdown is covered by its SHA-256 in the signed JSON.
     When the list could not be built, `gb/full-list.md` is replaced by a short "not available" note (reason words only) and
     `full_list.status` says why, so an old list is never read as today's.

Files, state and logs hold ids, counts and reason words, never fact text and never the key:

  ~/.local/state/fleet-facts/mac-facts-publish.json   the last result (0600, in a 0700 folder)
  ~/.local/state/fleet-facts/mac-facts-publish.lock   the lock
  ~/Library/Logs/mac-facts-publish.log                 the LaunchAgent's stdout and stderr
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from . import gather as ff_gather
from . import localtime, repo as repomod, safety, signing
from .minimize import PathLeak, mac_path_in, minimize, scrub
from .runner import Result, Runner, home_path, kill_live, which
from .runner import run as default_run

__all__ = ["CAP_SECONDS", "LOAD_LIMIT", "PARTS", "FULL_LIST", "GB_FACTS", "FULL_FACTS", "Outcome", "Withheld", "UnreadableFile", "CapExceeded",
           "build_documents", "build_full_list", "run_cycle", "publish_file", "verify_file", "state_dir"]

CAP_SECONDS = 20 * 60
LOAD_LIMIT = 250.0
PARTS = ("lanes", "autopush", "backup", "needs_owner")
FULL_LIST = "gb/full-list.md"
GB_FACTS = "gb/mac-facts.json"
FULL_FACTS = "mac-facts.json"
WRITE_ORDER = (FULL_LIST, GB_FACTS, FULL_FACTS)     # the Markdown first:  the signed JSON that carries its hash lands after it
GATHER_RESERVE_S = 300       # kept back from the gatherers for the full list and the writes
LIST_RESERVE_S = 120         # kept back from the full list for the writes
MIN_WRITE_S = 60             # no write phase starts with less than this left
MAX_FILE_BYTES = 8 * 1024 * 1024
EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE, EXIT_REFUSED, EXIT_FAILED = 0, 1, 2, 3, 4


class Withheld(Exception):
    """A document is not safe to publish.  Nothing is written for the cycle."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnreadableFile(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CapExceeded(BaseException):
    """The wall-clock cap.  A BaseException, so the gatherers' `except Exception` cannot swallow it."""


@dataclass
class Outcome:
    result: str                      # published, skipped, refused, no-key, failed, over-cap, dry-run
    reason: str | None = None
    exit_code: int = EXIT_OK
    files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# ---- paths, lock, status ---------------------------------------------------------------------------------

def state_dir() -> Path:
    return home_path(".local", "state", "fleet-facts")


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.stat()
        if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) != 0o700:
            path.chmod(0o700)
    except OSError:
        pass
    return path


@contextlib.contextmanager
def _single_instance(folder: Path) -> Iterator[bool]:
    fd = os.open(str(folder / "mac-facts-publish.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        yield True
    finally:
        os.close(fd)        # closing the descriptor releases the lock


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write `data` at mode 0600 (beside the final name, then renamed)."""
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_status(folder: Path) -> dict:
    try:
        data = json.loads((folder / "mac-facts-publish.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(folder: Path, outcome: Outcome, now: datetime, seconds: float, knob_report: dict | None) -> None:
    """Write the status file:  the result, the reason words and the per-file results, no fact text."""
    previous = _read_status(folder)
    published = outcome.result == "published"
    status = {
        "schema": 1, "tool": "mac-facts-publish",
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "at_local": localtime.stamp(now),
        "result": outcome.result, "reason": outcome.reason, "files": outcome.files, "notes": outcome.notes,
        "seconds": int(seconds),
        "stale": not published,
        "last_published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ") if published else previous.get("last_published_at"),
        "last_published_generated_at": outcome.extra.get("generated_at") if published else previous.get("last_published_generated_at"),
    }
    status.update({k: v for k, v in outcome.extra.items() if k != "generated_at"})
    if knob_report:
        status["knobs"] = knob_report
    with contextlib.suppress(OSError):
        _write_private(folder / "mac-facts-publish.json", (json.dumps(status, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _knob_report() -> dict:
    module = sys.modules.get("fleet_knobs.knobs")
    if module is None:
        return {}
    try:
        return module.report()      # names, sources and the stale flag, never a value
    except Exception:  # noqa: BLE001
        return {}


def _default_apps() -> list[str]:
    try:
        from fleet_knobs import knobs  # noqa: PLC0415
        return list(knobs.get("FLEET_LOOPS_DATA_ALLOWED_APPS"))
    except Exception:  # noqa: BLE001 - an unreadable knob is the empty list, which is the strict answer
        return []


def _default_full_copy() -> bool:
    try:
        from fleet_knobs import knobs  # noqa: PLC0415
        return bool(knobs.get("FLEET_FACTS_PUBLISH_FULL_COPY"))
    except Exception:  # noqa: BLE001 - an unreadable knob is off, which is the strict answer
        return False


FULL_COPY_OFF = "skipped:  FLEET_FACTS_PUBLISH_FULL_COPY is off"


# ---- time budget -----------------------------------------------------------------------------------------

class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.end = clock() + seconds
        self.start = clock()

    def left(self) -> float:
        return self.end - self.clock()

    def used(self) -> float:
        return self.clock() - self.start


def bounded(run: Runner, deadline: Deadline, reserve: float) -> Runner:
    """`run` with every child's timeout clamped to what is left of the cap minus `reserve`;  no room is a result, not a start."""
    def inner(argv, *, timeout, env=None, cwd=None):
        room = deadline.left() - reserve
        if room < 5:
            return Result(None, reason="out of time")
        return run(argv, timeout=min(timeout, room), env=env, cwd=cwd)
    return inner


@contextlib.contextmanager
def hard_cap(seconds: int) -> Iterator[None]:
    def on_alarm(signum, frame):  # noqa: ARG001
        raise CapExceeded()
    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(max(1, int(seconds)))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# ---- the full list ---------------------------------------------------------------------------------------

def build_full_list(run: Runner, deadline: Deadline, key: str) -> tuple[dict, str | None]:
    """(the `full_list` info block, the Markdown or None).  `open-loops gather` then `open-loops render --markdown`,
    both as child processes with their output in a private folder.  Not installed is "absent";  anything else that
    goes wrong is "failed" with a reason;  neither stops the cycle."""
    info = {"file": FULL_LIST}      # not "path":  the Grok Bot copy drops every `path` key

    def miss(status: str, reason: str) -> tuple[dict, None]:
        return dict(info, status=status, reason=reason), None

    exe = which("open-loops")
    if exe is None:
        return miss("absent", "open-loops is not installed on this host")
    scanner = safety.secretscan()
    folder = tempfile.mkdtemp(prefix="fleet-facts-list-")
    try:
        os.chmod(folder, 0o700)
        facts = os.path.join(folder, "facts.json")
        markdown = os.path.join(folder, "full-list.md")
        room = deadline.left() - LIST_RESERVE_S
        if room < 30:
            return miss("failed", "out of time before the full list")
        got = run([exe, "gather", "--json", "--out", facts], timeout=min(600, room))
        if got.reason or got.rc != 0:
            return miss("failed", "open-loops gather %s" % (got.reason or "exited %s" % got.rc))
        room = deadline.left() - LIST_RESERVE_S
        if room < 10:
            return miss("failed", "out of time before the full list was rendered")
        got = run([exe, "render", "--facts", facts, "--markdown", "--out", markdown], timeout=min(120, room))
        if got.reason or got.rc != 0:
            return miss("failed", "open-loops render %s" % (got.reason or "exited %s" % got.rc))
        try:
            data = Path(markdown).read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return miss("failed", "the rendered list could not be read")
        if not text.strip() or len(data) > MAX_FILE_BYTES:
            return miss("failed", "the rendered list is empty or too large")
        kind = scanner.scan(text, known=[key])
        if kind:
            return miss("failed", "secret-shaped text (%s) withheld from the list" % kind)
        text = scrub(text)          # gb/ holds no Mac path, in a list any more than in the JSON
        if mac_path_in(text):
            return miss("failed", "a Mac path is still in the list")
        data = text.encode("utf-8")
        return dict(info, status="ok", reason=None, sha256=signing.sha256_hex(data), bytes=len(data)), text
    finally:
        shutil.rmtree(folder, ignore_errors=True)


GAP = "\u00a0 "      # the Markdown is rendered on GitHub, where two typed spaces collapse


def unavailable_text(now: datetime, reason: str | None) -> str:
    """What `gb/full-list.md` says when this run could not build the list:  the previous list is replaced on purpose, so a daily
    digest that always links this file never shows an old list as today's.  Reason words only."""
    why = (reason or "no reason given").strip()
    return ("# Full List Not Available\n\n"
            "The Mac could not build the full open-loops list for the run at %s.%sReason:  %s.\n\n"
            "The list that was here before was removed on purpose, so an old list is not read as today's.%sThe next run tries again.\n"
            % (localtime.stamp(now), GAP, why, GAP))


# ---- the documents ---------------------------------------------------------------------------------------

def _serialize(document: dict) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _check_whole(document: dict, rel: str, key: str) -> None:
    kind = safety.secretscan().scan(json.dumps(document, sort_keys=True, ensure_ascii=True), known=[key])
    if kind:
        raise Withheld("secret-shaped text (%s) is still in %s after redaction" % (kind, rel))


def build_documents(facts: Mapping, full_list: Mapping, key: str, *, include_full: bool = True) -> dict[str, bytes]:
    """{path: bytes} for gb/mac-facts.json and, with `include_full`, mac-facts.json, signed.  The full copy keeps lane paths, so
    the cycle builds it only when the owner turned FLEET_FACTS_PUBLISH_FULL_COPY on.  Raises Withheld (or PathLeak) when
    a document is unsafe."""
    document = json.loads(json.dumps(facts))
    document["kind"] = "mac-facts"
    document["signed_for"] = FULL_FACTS
    document["full_list"] = dict(full_list)
    cleaned, paths = safety.secretscan().redact_fields(document, known=[key])
    redacted = list(dict.fromkeys(list(facts.get("redacted") or []) + paths))
    cleaned["redacted"] = redacted
    sources = dict(cleaned.get("sources") or {})
    sources["publish"] = {"status": "ok", "reason": None, "redacted": redacted}
    cleaned["sources"] = sources
    _check_whole(cleaned, FULL_FACTS, key)

    gb = minimize(cleaned)
    gb["kind"] = "mac-facts-gb"
    gb["signed_for"] = GB_FACTS
    _check_whole(gb, GB_FACTS, key)
    out = {GB_FACTS: _serialize(signing.sign(gb, key))}
    if include_full:
        out[FULL_FACTS] = _serialize(signing.sign(cleaned, key))
    return out


# ---- the cycle -------------------------------------------------------------------------------------------

def _log(now: datetime, message: str) -> None:
    sys.stdout.write("[%s] mac-facts-publish: %s\n" % (localtime.stamp(now), message))
    sys.stdout.flush()


def _write_dry_run(folder: Path, files: Mapping[str, bytes]) -> None:
    _private_dir(folder)
    for rel, data in files.items():
        target = folder / rel
        _private_dir(target.parent)
        _write_private(target, data)


def _cycle(*, run: Runner, now: datetime, key_provider: Callable[[], str], load1: Callable[[], float], sleep: Callable[[float], None],
           repo: str, gh: str | None, apps_provider: Callable[[], list[str]], full_copy_provider: Callable[[], bool],
           dry_run_dir: Path | None, deadline: Deadline) -> Outcome:
    try:
        load = float(load1())
    except OSError:
        load = 0.0
    if load > LOAD_LIMIT:
        return Outcome("skipped", "load1 %.0f is over %d;  the last published files stay and are stale" % (load, LOAD_LIMIT), extra={"load1": round(load, 1)})
    try:
        key = key_provider()
    except signing.KeyUnavailable as exc:
        return Outcome("no-key", "nothing was published:  %s" % exc.reason, EXIT_REFUSED)
    gh_path = gh or which("gh")
    if dry_run_dir is None:
        # A cheap look first, so a repository that is not ready does not cost a lane doctor run every 3 hours.  It is looked at
        # again, and that look is the one that counts, right before the writes.
        if gh_path is None:
            return Outcome("refused", "nothing was published:  gh is not installed", EXIT_REFUSED)
        try:
            repomod.guard(run, gh_path, repo, allowed_apps=apps_provider())
        except repomod.Refused as exc:
            return Outcome("refused", "nothing was published:  %s" % exc.reason, EXIT_REFUSED)
    try:
        facts = ff_gather.build(list(PARTS), no_mac=False, run=bounded(run, deadline, GATHER_RESERVE_S), now=now)
        full_info, full_text = build_full_list(bounded(run, deadline, 90), deadline, key)
        if full_text is None:
            # Never leave the previous list standing next to fresh facts:  replace it with a note that says why.
            full_text = unavailable_text(now, full_info.get("reason"))
            data = full_text.encode("utf-8")
            full_info = dict(full_info, placeholder=True, sha256=signing.sha256_hex(data), bytes=len(data))
        include_full = bool(full_copy_provider())
        files = build_documents(facts, full_info, key, include_full=include_full)
    except safety.HelpersMissing as exc:
        return Outcome("refused", "nothing was published:  %s" % exc, EXIT_REFUSED)
    except (Withheld, PathLeak) as exc:
        return Outcome("refused", "nothing was published:  %s" % (exc.reason if isinstance(exc, Withheld) else str(exc)), EXIT_REFUSED)
    extra = {"generated_at": facts.get("generated_at"), "full_list": full_info["status"],
             "sources": {name: entry.get("status") for name, entry in (facts.get("sources") or {}).items()},
             "redacted_count": len(json.loads(files[GB_FACTS])["redacted"]), "full_copy": include_full}
    files[FULL_LIST] = full_text.encode("utf-8")
    if dry_run_dir is not None:
        _write_dry_run(dry_run_dir, files)
        written = {rel: "written" for rel in files}
        if FULL_FACTS not in files:
            written[FULL_FACTS] = FULL_COPY_OFF
        return Outcome("dry-run", "wrote %d files to %s;  nothing was sent" % (len(files), dry_run_dir.name), files=written, extra=extra)
    if deadline.left() < MIN_WRITE_S:
        return Outcome("over-cap", "less than %d seconds were left for the writes;  nothing was written" % MIN_WRITE_S, EXIT_FAILED, extra=extra)
    try:
        notes = repomod.guard(run, gh_path, repo, allowed_apps=apps_provider())
    except repomod.Refused as exc:
        return Outcome("refused", "nothing was published:  %s" % exc.reason, EXIT_REFUSED, extra=extra)
    writer = repomod.Writer(run, gh_path, repo, sleep=sleep)
    stamp = facts.get("generated_at") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    results: dict[str, str] = {}
    failure: str | None = None
    if FULL_FACTS not in files:
        # The path-bearing copy is off:  if a run published it while the knob was on, it must not stay readable at the tip.
        try:
            results[FULL_FACTS] = FULL_COPY_OFF if writer.remove(FULL_FACTS, "mac-facts: remove %s (the full copy is off)" % FULL_FACTS) == "absent" \
                else "removed:  FLEET_FACTS_PUBLISH_FULL_COPY is off"
        except (repomod.WriteFailed, repomod.Refused) as exc:
            return Outcome("failed", exc.reason, EXIT_FAILED, {FULL_FACTS: "failed"}, notes, extra)
    for rel in WRITE_ORDER:
        if rel not in files:
            continue
        try:
            results[rel] = writer.put(rel, files[rel], "mac-facts: %s %s" % (rel, stamp))
        except (repomod.WriteFailed, repomod.Refused) as exc:
            results[rel] = "failed"
            failure = exc.reason
            break       # a later file may depend on this one (the JSON carries the list's hash)
    if failure:
        if results.get(FULL_LIST) in ("created", "updated"):
            # The new list landed and the facts that describe it did not:  do not leave the new list beside the old facts.
            note = unavailable_text(now, "the facts file could not be written, so this list was withdrawn").encode("utf-8")
            try:
                writer.put(FULL_LIST, note, "mac-facts: %s withdrawn" % FULL_LIST)
                results[FULL_LIST] += ";  then withdrawn (a not-available note)"
            except (repomod.WriteFailed, repomod.Refused):
                results[FULL_LIST] += ";  could not be withdrawn"
        return Outcome("failed", failure, EXIT_FAILED, results, notes, extra)
    return Outcome("published", None, EXIT_OK, results, notes, extra)


def run_cycle(*, run: Runner = default_run, now: datetime | None = None, key_provider: Callable[[], str] = signing.key_from_knobs,
              load1: Callable[[], float] = lambda: os.getloadavg()[0], sleep: Callable[[float], None] = time.sleep,
              state: Path | None = None, repo: str = repomod.REPO, gh: str | None = None,
              apps_provider: Callable[[], list[str]] = _default_apps, full_copy_provider: Callable[[], bool] = _default_full_copy,
              dry_run_dir: Path | None = None, cap_seconds: int = CAP_SECONDS, clock: Callable[[], float] = time.monotonic) -> Outcome:
    """One publish cycle.  Never raises for a condition it knows;  the Outcome says what happened and the exit status to use."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    folder = _private_dir(state or state_dir())
    with _single_instance(folder) as got:
        if not got:
            outcome = Outcome("skipped", "another publish is running")
            _log(current, "skipped:  another publish is running")
            return outcome
        deadline = Deadline(cap_seconds, clock)
        _log(current, "start")
        try:
            with hard_cap(cap_seconds):
                outcome = _cycle(run=run, now=current, key_provider=key_provider, load1=load1, sleep=sleep, repo=repo, gh=gh,
                                 apps_provider=apps_provider, full_copy_provider=full_copy_provider, dry_run_dir=dry_run_dir,
                                 deadline=deadline)
        except CapExceeded:
            kill_live()
            outcome = Outcome("over-cap", "the %d-minute cap was reached;  running children were ended" % (cap_seconds // 60), EXIT_FAILED)
        if dry_run_dir is None:     # a dry run must not overwrite what the real runs reported
            _record(folder, outcome, current, deadline.used(), _knob_report())
        _log(current, "%s%s" % (outcome.result, ("  (%s)" % outcome.reason) if outcome.reason else ""))
        for rel, what in outcome.files.items():
            _log(current, "  %s: %s" % (rel, what))
        if outcome.notes:
            _log(current, "  apps allowed by name: %s" % ", ".join(outcome.notes))
        return outcome


# ---- one file, and verify --------------------------------------------------------------------------------

def _read_regular(path: str) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.path.expanduser(path), flags)
    except OSError as exc:
        raise UnreadableFile("cannot open the file (%s)" % (exc.strerror or type(exc).__name__)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnreadableFile("the file is not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise UnreadableFile("the file is larger than %d MB" % (MAX_FILE_BYTES // (1024 * 1024)))
        with os.fdopen(os.dup(fd), "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)


def publish_file(rel: str, local: str, *, run: Runner = default_run, key_provider: Callable[[], str] = signing.key_from_knobs,
                 repo: str = repomod.REPO, gh: str | None = None, apps_provider: Callable[[], list[str]] = _default_apps,
                 full_copy_provider: Callable[[], bool] = _default_full_copy,
                 sleep: Callable[[float], None] = time.sleep, dry_run: bool = False) -> Outcome:
    """Sign (a .json file) or scan (anything else) one local file and write it to `rel`.  The same gate as the cycle."""
    try:
        repomod.check_path(rel)
        if rel == FULL_FACTS and not full_copy_provider():
            raise repomod.Refused("mac-facts.json keeps lane paths and FLEET_FACTS_PUBLISH_FULL_COPY is off")
        raw = _read_regular(local)
    except repomod.Refused as exc:
        return Outcome("refused", exc.reason, EXIT_REFUSED)
    except UnreadableFile as exc:
        return Outcome("refused", exc.reason, EXIT_USAGE)
    try:
        key = key_provider()
    except signing.KeyUnavailable as exc:
        return Outcome("no-key", "nothing was published:  %s" % exc.reason, EXIT_REFUSED)
    try:
        scanner = safety.secretscan()
        if rel.endswith(".json"):
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return Outcome("refused", "the file is not JSON", EXIT_USAGE)
            if not isinstance(document, dict):
                return Outcome("refused", "the file is not a JSON object", EXIT_USAGE)
            document["signed_for"] = rel
            cleaned, paths = scanner.redact_fields(document, known=[key])
            if paths:
                cleaned["redacted"] = list(dict.fromkeys(list(cleaned.get("redacted") or []) + paths))
            if rel.startswith("gb/"):
                try:
                    cleaned = minimize(cleaned)         # everything Grok Bot can read is path-free and has its quotes cut
                except PathLeak as exc:
                    raise Withheld(str(exc)) from None
            _check_whole(cleaned, rel, key)
            data = _serialize(signing.sign(cleaned, key))
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return Outcome("refused", "the file is not UTF-8 text", EXIT_USAGE)
            kind = scanner.scan(text, known=[key])
            if kind:
                raise Withheld("secret-shaped text (%s) is in %s" % (kind, rel))
            if rel.startswith("gb/") and mac_path_in(text):
                raise Withheld("a Mac path is in %s, and everything under gb/ is path-free" % rel)
            data = raw
    except safety.HelpersMissing as exc:
        return Outcome("refused", "nothing was published:  %s" % exc, EXIT_REFUSED)
    except Withheld as exc:
        return Outcome("refused", "nothing was published:  %s" % exc.reason, EXIT_REFUSED)
    if dry_run:
        return Outcome("dry-run", "checked and signed %s;  nothing was sent" % rel, files={rel: "checked"})
    gh_path = gh or which("gh")
    if gh_path is None:
        return Outcome("refused", "nothing was published:  gh is not installed", EXIT_REFUSED)
    try:
        notes = repomod.guard(run, gh_path, repo, allowed_apps=apps_provider())
        done = repomod.Writer(run, gh_path, repo, sleep=sleep).put(rel, data, "mac-facts: %s" % rel)
    except repomod.Refused as exc:
        return Outcome("refused", "nothing was published:  %s" % exc.reason, EXIT_REFUSED)
    except repomod.WriteFailed as exc:
        return Outcome("failed", exc.reason, EXIT_FAILED, {rel: "failed"})
    return Outcome("published", None, EXIT_OK, {rel: done}, notes)


def verify_file(path: str, environ: Mapping[str, str], *, signed_for: str | None = None) -> tuple[int, str]:
    """(exit status, one line).  0 verified;  1 not verified (unsigned, tampered, wrong key, not JSON);  3 cannot check (no
    usable key).  2 is the usage error and is never returned here:  the open-loops consumer reads 2 as "no verify command"."""
    try:
        key = signing.key_from_environ(environ)
    except signing.KeyUnavailable as exc:
        return 3, "cannot verify:  %s" % exc.reason
    try:
        raw = _read_regular(path)
        document = json.loads(raw.decode("utf-8"))
    except UnreadableFile as exc:
        return 1, "not verified:  %s" % exc.reason
    except (UnicodeDecodeError, ValueError):
        return 1, "not verified:  the file is not JSON"
    verdict = signing.verify(document, key, signed_for=signed_for)
    if not verdict:
        return 1, "not verified:  %s" % verdict.reason
    return 0, "verified:  %s, generated %s" % (document.get("signed_for", "unknown path"), document.get("generated_at", "at an unknown time"))
