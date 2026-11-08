"""Lane placement policy shared by the doctor, the guard, the git shim and the watcher (AFC-618).

Three things live here, none of which touches a process or the network:

  * the exceptions file (`placement-exceptions.json`, shipped beside this module): checkouts the Lane Map does
    not place but that have a recorded reason to be where they are.  Every row carries `class`, `reason`,
    `decided_by`, `decided_on`, `expires` and `hosts`.  A row past `expires` stops excusing anything, and the
    doctor and the watcher name it, so an exception cannot outlive its decision silently.
  * the mode file (`~/.config/fleet/lane-guard-mode`): what the shim and the widened guard rules do.
  * the event log the shim and the hooks append to, which the watcher reads as its second input.

Patterns never name a server volume.  They start with a root token, `{home}`, `{code}`, `{apps}` or `{lanes}`,
or with `**/`, and the tokens are filled from the host's roots (`FLEET_CODE_ROOT` and friends), because the volume's
path is private deployment inventory (fleet-ops:ATTACK-MAP.md).  A pattern matches its target and anything under it.
`*` is one path component, `**` is any number of them.

The mode file holds one line per setting, and `#` starts a comment:

    detect                      off | detect | block [class,class...]
    isolation off               off | detect | block

`block` with no classes blocks every class, and `block temp,code` blocks those two while the rest stay in detect, so
enforcement turns on one rule class at a time and rolls back by editing a file.  With no file the placement mode is
`detect` and isolation is `off`.  `FLEET_LANE_GUARD=off` in the environment still switches everything off.

The hook rules that deny temp and ~/Code destinations predate this file and are not affected by it: only the shim
reads `temp` and `code` here.  `temp-build` (build output pointed into system temp outside the session scratchpad;  a fleet
clone there is the temp class, and the watcher reports any big idle checkout left in temp) is new and obeys this file everywhere:  detect logs, and only `block` or `block temp-build`
refuses.

What a row excuses depends on its class and on who is asking (`row_excuses`), because a pattern says only where and a
tool filling its own cache and a seat typing a clone can name the same folder:

    service, backup     everything, from the hook and the shim alike.  The shim still records the excused event.
    tool-cache          a clone that reaches the shim, outside temp and ~/Code.  Never a worktree, never the hook.
    package-manager     the same, for the folders a package manager clones a git dependency into.
    package-temp        the same, and only when the destination really is in temp (pip, pnpm).
    third-party-build   a mirror or bare clone, or a clone from a local path (the two SwiftPM makes), anywhere.
    personal, trash     nothing a guard judges, since the source there is a fleet repo.  The doctor reads them.
    temp-build          a build tool's own output in temp (Homebrew building from source, `cargo install`), and nothing else.

A build in temp is judged by row_excuses_build instead:  a package-temp row excuses it, a temp-build row only outside the hook (pip and Homebrew build in
temp folders they make and remove themselves), and so does a service or backup row.  No other class does, because a cache row
says a clone may sit there, not that a build may.

Python 3.9 safe.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import fnmatch
import json
import os
import pwd
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

EXCEPTIONS_NAME = "placement-exceptions.json"
ENV_EXCEPTIONS = "FLEET_PLACEMENT_EXCEPTIONS"
ENV_MODE_FILE = "FLEET_LANE_GUARD_MODE_FILE"
ENV_STATE_DIR = "FLEET_LANE_GUARD_STATE_DIR"
ENV_HOST_KIND = "FLEET_HOST_KIND"
ENV_GUARD = "FLEET_LANE_GUARD"
ENV_TEST_RUN = "FLEET_LANES_TEST_RUN"
MODE_FILE_REL = os.path.join(".config", "fleet", "lane-guard-mode")
STATE_DIR_REL = os.path.join(".local", "state", "fleet")
EVENT_LOG_NAME = "lane-guard-events.jsonl"
EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024

OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

MODE_OFF = "off"
MODE_DETECT = "detect"
MODE_BLOCK = "block"
MODES = (MODE_OFF, MODE_DETECT, MODE_BLOCK)

CLASS_TEMP = "temp"
CLASS_CODE = "code"
CLASS_HOME = "home"
CLASS_APPS = "apps"
CLASS_CLONE_IN_LANES = "clone-in-lanes"
CLASS_ELSEWHERE = "elsewhere"
CLASS_TEMP_BUILD = "temp-build"  # build output pointed into system temp, outside the session scratchpad
RULE_CLASSES = (CLASS_TEMP, CLASS_CODE, CLASS_HOME, CLASS_APPS, CLASS_CLONE_IN_LANES, CLASS_ELSEWHERE, CLASS_TEMP_BUILD)

HOST_MAC = "mac"
HOST_SERVER = "server"
HOSTS = (HOST_MAC, HOST_SERVER)

CLASS_ISOLATION = "isolation"  # a harness worktree in the integration tree;  the `isolation` line decides, not the classes

EXCEPTION_CLASSES = ("service", "tool-cache", "package-manager", "package-temp", "third-party-build", "backup", "personal",
                     "trash", "temp-build")
EXPIRY_WARNING_DAYS = 30
REQUIRED_ROW_KEYS = ("id", "pattern", "class", "creator", "reason", "decided_by", "decided_on", "expires", "hosts")
PATTERN_STARTS = ("{home}", "{code}", "{apps}", "{lanes}", "**/")
# Text that would put private deployment inventory in a tracked file.  Tests and the validator both refuse it.
PRIVATE_PATH_MARKS = ("/mnt/", "/root/", "/home/", "/Users/", "/Volumes/")


def host_kind(env: Mapping[str, str] | None = None) -> str:
    """`mac` or `server`, the two words a row's `hosts` holds."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_HOST_KIND) or "").strip().lower()
    if raw in HOSTS:
        return raw
    return HOST_MAC if sys.platform == "darwin" else HOST_SERVER


@dataclass(frozen=True)
class ExceptionRow:
    id: str
    pattern: str
    cls: str
    creator: str
    reason: str
    decided_by: str
    decided_on: str
    expires: str
    hosts: tuple[str, ...]

    def expired(self, today: _dt.date) -> bool:
        try:
            return _dt.date.fromisoformat(self.expires) < today
        except ValueError:
            return True


@dataclass(frozen=True)
class ExceptionHit:
    row: ExceptionRow
    expired: bool


@dataclass(frozen=True)
class Mode:
    """What the mode file says.  `block_classes` is empty unless `mode` is block."""

    mode: str = MODE_DETECT
    block_classes: tuple[str, ...] = ()
    isolation: str = MODE_OFF
    problems: tuple[str, ...] = field(default=())

    def blocks(self, cls: str) -> bool:
        if cls == CLASS_ISOLATION:
            return self.isolation == MODE_BLOCK
        return self.mode == MODE_BLOCK and cls in self.block_classes


def default_exceptions_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), EXCEPTIONS_NAME)


class ExceptionsUnusable(Exception):
    """The exceptions file is missing, unreadable or holds a bad row.  Only a caller that acts on a row asks for this."""


def _pattern_problem(pattern: str) -> str | None:
    """Why this pattern cannot be a row, or None.  A row must name a place:  a root token or `**/` alone is everything."""
    if not pattern.startswith(PATTERN_STARTS):
        return f"pattern must start with one of {PATTERN_STARTS}"
    rest = pattern
    for start in PATTERN_STARTS:
        if pattern.startswith(start):
            rest = pattern[len(start):]
            break
    if not any(part.strip("*?") for part in rest.split("/")):
        return "pattern must name at least one literal folder after its root"
    return None


def validate_rows(data: Any) -> list[str]:
    """Every problem with a parsed exceptions file, in file order.  An empty list is a valid file."""
    if not isinstance(data, Mapping) or not isinstance(data.get("exceptions"), list):
        return ["top level needs an `exceptions` list"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, row in enumerate(data["exceptions"]):
        where = f"row {i}"
        if not isinstance(row, Mapping):
            problems.append(f"{where}: not an object")
            continue
        where = f"row {i} ({row.get('id', '?')})"
        for key in REQUIRED_ROW_KEYS:
            if not row.get(key):
                problems.append(f"{where}: `{key}` is missing or empty")
        extra = set(row) - set(REQUIRED_ROW_KEYS)
        if extra:
            problems.append(f"{where}: unknown keys {sorted(extra)}")
        rid = str(row.get("id", ""))
        if rid in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(rid)
        pattern = str(row.get("pattern", ""))
        why = _pattern_problem(pattern)
        if why:
            problems.append(f"{where}: {why}")
        if row.get("class") not in EXCEPTION_CLASSES:
            problems.append(f"{where}: class {row.get('class')!r} is not one of {EXCEPTION_CLASSES}")
        hosts = row.get("hosts")
        if not isinstance(hosts, list) or not hosts or any(h not in HOSTS for h in hosts):
            problems.append(f"{where}: hosts must be a non-empty list drawn from {HOSTS}")
        dates: dict[str, _dt.date] = {}
        for key in ("decided_on", "expires"):
            try:
                dates[key] = _dt.date.fromisoformat(str(row.get(key, "")))
            except ValueError:
                problems.append(f"{where}: `{key}` is not an ISO date")
        if len(dates) == 2 and dates["expires"] <= dates["decided_on"]:
            problems.append(f"{where}: `expires` must be after `decided_on`")
        text = " ".join(str(row.get(k, "")) for k in ("pattern", "reason", "creator", "decided_by"))
        for mark in PRIVATE_PATH_MARKS:
            if mark in text:
                problems.append(f"{where}: names a private path ({mark!r}); use a root token")
    return problems


def parse_rows(data: Any) -> tuple[ExceptionRow, ...]:
    """The usable rows of a parsed file.  A malformed row, a class this code does not know or a catch-all pattern is dropped, never
    fatal:  a guard must not fail on it, and a row that excuses everything must not load.  validate_rows names what was dropped."""
    out: list[ExceptionRow] = []
    rows = data.get("exceptions") if isinstance(data, Mapping) else None
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping) or any(not row.get(k) for k in REQUIRED_ROW_KEYS):
            continue
        hosts = row["hosts"]
        if not isinstance(hosts, list) or _pattern_problem(str(row["pattern"])) or row["class"] not in EXCEPTION_CLASSES:
            continue
        out.append(ExceptionRow(str(row["id"]), str(row["pattern"]), str(row["class"]), str(row["creator"]),
                                str(row["reason"]), str(row["decided_by"]), str(row["decided_on"]),
                                str(row["expires"]), tuple(str(h) for h in hosts)))
    return tuple(out)


def load_exceptions_reporting(path: str | os.PathLike[str] | None = None,
                              env: Mapping[str, str] | None = None) -> tuple[tuple[ExceptionRow, ...], str | None]:
    """The usable rows on disk and, when the file is missing, unreadable or holds a bad row, what is wrong with it.  Rows that are gone
    change what the guards deny and what the doctor reports, so everything that acts on the rows says why."""
    env = os.environ if env is None else env
    target = os.fspath(path) if path is not None else (env.get(ENV_EXCEPTIONS) or default_exceptions_path())
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return (), f"exceptions file unusable: {type(exc).__name__}"
    problems = validate_rows(data)
    return parse_rows(data), (f"exceptions file has {len(problems)} bad row(s): {problems[0]}" if problems else None)


def load_exceptions(path: str | os.PathLike[str] | None = None,
                    env: Mapping[str, str] | None = None) -> tuple[ExceptionRow, ...]:
    """The usable rows on disk.  A missing or unreadable file is no exceptions at all, so a caller that shows a person anything uses
    load_exceptions_reporting, and one that moves a checkout because no row names it uses load_exceptions_strict."""
    return load_exceptions_reporting(path, env)[0]


def load_exceptions_strict(path: str | os.PathLike[str] | None = None,
                           env: Mapping[str, str] | None = None) -> tuple[ExceptionRow, ...]:
    """The rows on disk, or ExceptionsUnusable.  For code_main_sync, where no row means free to move a pinned clone."""
    rows, problem = load_exceptions_reporting(path, env)
    if problem:
        raise ExceptionsUnusable(problem)
    return rows


def row_excuses(row: ExceptionRow, *, via: str, clone: bool, in_temp: bool, in_code: bool, mirror_or_local: bool) -> bool:
    """Whether a row excuses this one checkout command.  The module docstring has the table."""
    if row.cls in ("service", "backup"):
        return True
    if row.cls not in ("tool-cache", "package-manager", "package-temp", "third-party-build") or via != "shim" or not clone:
        return False  # personal and trash excuse nothing here, and neither does a class this code has never heard of
    if row.cls == "third-party-build":
        return mirror_or_local
    if in_code:
        return False
    if row.cls == "package-temp":
        return in_temp
    return not in_temp


def row_excuses_build(row: ExceptionRow, *, typed: bool = False) -> bool:
    """Whether a row excuses build output pointed into temp, the temp-build rule class.  A temp-build row matches a folder by
    name alone (Homebrew's `<formula>-<date>-...`, `cargo-install...`), and an agent can choose that name, so it excuses only a
    build the agent did not type:  the build shim's and the watcher's.  Homebrew and `cargo install` never come through the
    hook, because neither is a build the hook reads.  `typed` is true for the hook's own judgement."""
    if row.cls == "temp-build":
        return not typed
    return row.cls in ("package-temp", "service", "backup")


def row_excuses_checkout(row: ExceptionRow, *, linked_worktree: bool, registered_app: bool) -> bool:
    """Whether a row excuses a checkout that already exists, for the doctor.  A cache holds clones, never a linked worktree, and
    a personal row is for repos outside the fleet.  A temp-build row is about build output, so it excuses no checkout."""
    if row.cls in ("service", "backup", "trash"):
        return True
    if linked_worktree or row.cls not in EXCEPTION_CLASSES or row.cls == "temp-build":
        return False
    return not (row.cls == "personal" and registered_app)


def _root_tokens(roots: Any) -> dict[str, str]:
    return {"{home}": os.fspath(roots.home), "{code}": os.fspath(roots.code_root),
            "{apps}": os.fspath(roots.apps_root), "{lanes}": os.fspath(roots.lanes_root)}


def _expand(pattern: str, tokens: Mapping[str, str]) -> str:
    for token, value in tokens.items():
        if pattern.startswith(token):
            return value.rstrip("/") + pattern[len(token):]
    return pattern


def _component_match(path: Sequence[str], pat: Sequence[str]) -> bool:
    """`pat` matches a leading run of `path` (at-or-under), with `*` per component and `**` for any number."""
    if not pat:
        return True
    head, rest = pat[0], pat[1:]
    if head == "**":
        return any(_component_match(path[i:], rest) for i in range(len(path) + 1))
    if not path:
        return False
    return fnmatch.fnmatchcase(path[0], head) and _component_match(path[1:], rest)


def _split(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def pattern_matches(pattern: str, path: str, tokens: Mapping[str, str],
                    case_insensitive: bool = False) -> bool:
    expanded = _expand(pattern, tokens)
    if case_insensitive:
        expanded, path = expanded.casefold(), path.casefold()
    return _component_match(_split(path), _split(expanded))


def find_exception(paths: Iterable[str], roots: Any, rows: Sequence[ExceptionRow], *, host: str,
                   today: _dt.date | None = None) -> ExceptionHit | None:
    """The first row for this host that covers any spelling of a path, preferring one still in date."""
    today = today or _dt.date.today()
    tokens = _root_tokens(roots)
    wanted = [p for p in paths if p]
    stale: ExceptionHit | None = None
    for row in rows:
        if host not in row.hosts:
            continue
        if not any(pattern_matches(row.pattern, p, tokens, roots.case_insensitive) for p in wanted):
            continue
        if not row.expired(today):
            return ExceptionHit(row, False)
        stale = stale or ExceptionHit(row, True)
    return stale


def expired_rows(rows: Sequence[ExceptionRow], *, host: str,
                 today: _dt.date | None = None) -> list[ExceptionRow]:
    today = today or _dt.date.today()
    return [r for r in rows if host in r.hosts and r.expired(today)]


def expiring_rows(rows: Sequence[ExceptionRow], *, host: str, today: _dt.date | None = None,
                  within_days: int = EXPIRY_WARNING_DAYS) -> list[ExceptionRow]:
    """Rows still in date that lapse within the warning window, so someone can decide before a block starts refusing."""
    today = today or _dt.date.today()
    limit = today + _dt.timedelta(days=within_days)
    out: list[ExceptionRow] = []
    for row in rows:
        if host not in row.hosts or row.expired(today):
            continue
        if _dt.date.fromisoformat(row.expires) <= limit:
            out.append(row)
    return out


def mode_file_path(home: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_MODE_FILE) or os.path.join(os.fspath(home), MODE_FILE_REL)


def parse_mode(text: str) -> Mode:
    """Unknown words are reported in `problems` and otherwise ignored:  a typo must never turn a block on, and a line that means
    nothing leaves the lines before it standing.  The doctor and the watcher show `problems`."""
    mode, classes, isolation = MODE_DETECT, (), MODE_OFF
    problems: list[str] = []
    for raw in text.splitlines():
        words = raw.split("#", 1)[0].replace(",", " ").split()
        if not words:
            continue
        head = words[0].lower()
        if head == "isolation":
            if len(words) == 2 and words[1].lower() in MODES:
                isolation = words[1].lower()
            else:
                problems.append(f"bad isolation line: {raw.strip()}")
        elif head in MODES:
            if head != MODE_BLOCK:
                mode, classes = head, ()
                if len(words) > 1:
                    problems.append(f"only block takes classes: {raw.strip()}")
                continue
            named = [w.lower() for w in words[1:]]
            bad = [w for w in named if w not in RULE_CLASSES]
            if bad:
                problems.append(f"unknown rule classes: {', '.join(bad)}")
            good = tuple(w for w in named if w in RULE_CLASSES) if named else RULE_CLASSES
            if good:
                mode, classes = head, good
        else:
            problems.append(f"unknown line: {raw.strip()}")
    return Mode(mode, tuple(classes), isolation, tuple(problems))


def read_mode(home: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> Mode:
    env = os.environ if env is None else env
    if (env.get(ENV_GUARD) or "").strip().lower() in OFF_VALUES:
        return Mode(MODE_OFF, (), MODE_OFF)
    try:
        with open(mode_file_path(home, env), encoding="utf-8") as fh:
            return parse_mode(fh.read(4096))
    except FileNotFoundError:
        return Mode()
    except (OSError, ValueError) as exc:
        return Mode(problems=(f"mode file unreadable: {type(exc).__name__}",))


def state_dir(home: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get(ENV_STATE_DIR) or os.path.join(os.fspath(home), STATE_DIR_REL)


def event_log_path(home: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> str:
    return os.path.join(state_dir(home, env), EVENT_LOG_NAME)


def _account_home() -> str:
    """The account's real home, which no `HOME` a test sets can change."""
    return pwd.getpwuid(os.getuid()).pw_dir


def _test_run(env: Mapping[str, str]) -> bool:
    return "unittest" in sys.modules or "pytest" in sys.modules or bool(env.get(ENV_TEST_RUN))


def writes_real_log_in_a_test(path: str, env: Mapping[str, str] | None = None) -> bool:
    """Whether this is a test run about to write the account's own event log.  That log is the input to the seven-day clean-detect gate,
    and a run of the suite once put 114 fixture paths in it, so a test run never reaches it, whatever its `HOME` says.  A test that
    means to read the log it wrote points `FLEET_LANE_GUARD_STATE_DIR` or `HOME` somewhere else, and that still works."""
    if not _test_run(os.environ if env is None else env):
        return False
    try:
        real = os.path.join(_account_home(), STATE_DIR_REL, EVENT_LOG_NAME)
    except (KeyError, OSError):
        return True  # no way to tell, and a test loses nothing by not logging
    return os.path.realpath(path) == os.path.realpath(real)


def _rotate_if_large(path: str) -> None:
    """Rotate under a lock, checking the size again once it is held.  Two writers that both saw a full log would otherwise
    rename twice, and the second rename puts a one-line file over the generation the watcher has not read."""
    try:
        if os.path.getsize(path) <= EVENT_LOG_MAX_BYTES:
            return
        fd = os.open(path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.path.getsize(path) > EVENT_LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    finally:
        os.close(fd)


def log_event(home: str | os.PathLike[str], event: Mapping[str, Any],
              env: Mapping[str, str] | None = None) -> None:
    """Append one JSON line.  Never raises: a full disk must not change what a guard decides."""
    try:
        path = event_log_path(home, env)
        if writes_real_log_in_a_test(path, env):
            return
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        _rotate_if_large(path)
        record = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), **event}
        line = json.dumps(record, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - logging is best effort by contract
        pass


def read_events(path: str | os.PathLike[str], *, limit_bytes: int = 2 * EVENT_LOG_MAX_BYTES) -> list[dict]:
    """The parseable lines of the event log (and its rotated sibling).  A log rotates only once it is past EVENT_LOG_MAX_BYTES, so
    the limit is over that, or the line that crossed it would never be read.  A file past even this keeps its newest lines, and the
    line the limit cuts through is skipped as torn."""
    out: list[dict] = []
    for candidate in (os.fspath(path) + ".1", os.fspath(path)):
        try:
            with open(candidate, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                if size > limit_bytes:
                    fh.seek(size - limit_bytes)
                data = fh.read(limit_bytes)
        except OSError:
            continue
        for raw in data.splitlines():
            try:
                item = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
    return out
