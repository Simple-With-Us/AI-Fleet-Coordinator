"""Muse's half of the wake router:  how one wake reaches one Muse Code chat (AFC-713).

    (called by `simple-chat wake-deliver` through the `PLATFORMS` table in `claude_wake.py`)

The join.  The Operator registry's `muse` namespace holds one row per interactive Muse
chat:  its letter, generation, native session id and host.  A Muse session id is a UUID, and
the chat's own store on this host is `<data root>/sessions/<YYYY>/<MM>/<DD>/<id>/session.jsonl`,
with a `.session.lock` file beside it that names the holding process while the chat is open
(see `muse_session_rename`, which owns that reading).  A sub-agent's session, stored under
its parent chat, is never a delivery target.

The route, and the rule around it.  When no process holds the chat, one headless run,
`muse exec --session-id <id>`, runs the wake text as one prompt in that same session, under
a per-session lock, re-checking for a holder once the lock is taken.  It names no provider
and no model:  a stored session resumes on the provider and model it was recorded with, and
naming another would move the chat.  The holder check fails closed:  a session store that
cannot be read well enough to say no process holds the chat is `busy`, and no headless run
starts.

While a process does hold the chat, the wake is parked for it and the answer is `busy`
(the router waits on `busy` without using up an attempt):  waking a held chat in place
has no direct route, measured on Muse 1.4.15 (`muse session-message send` from outside a
Muse session answers `sender_unverified`, because the channel verifies its sender as a
Muse session and this delivery runs under the ssh forced command, as no session;  a
headless run against a held chat would be a second writer on one session;  the chat's
own socket speaks the peer-message hello and nothing else).  Instead the parked wake is
handed over by the chat's own UserPromptSubmit hook, as prompt context on its next turn,
and a later retry finds the wake id consumed (or in the transcript) and answers `sent`
without running anything twice.  A held chat's wake is never refused for being live:
the turn has not run, and parking uses up no attempt.

The registry is asked again immediately before delivering, while the per-session lock is
held, as the router did before sending.  A letter that no longer holds this session at this
generation, or is closed, is `refused`.  A registry that cannot be read is `busy`:  nothing
has run, and the outage may outlast the wake's attempts, so the retry spends none of them.
A host with no registry address or key is `failed`, because a retry does not fix a
deployment fault.  A live row that cannot be read as a well-formed Muse Code chat of this
seat is `failed` too -- nothing has run, so a bounded retry is safe, and neither is proof
that the letter was released.  `failed` is used only there and where the delivery command
itself never started;  once a command may have run a turn, any failure is `refused` and
final.

Host facts come from this host only:  the session store under this user's Muse data root
and the `muse` binary on this host's PATH.  Delivery settings are the router's, passed to
`router_platform`;  no per-seat config file is read, and nothing in the request names a
path, a binary or a module.

The wake text is the router's fixed `COMMAND_TEXT`:  only the wake id, the router's own
link note and the message id can vary, so no Zulip-authored field (sender, topic, channel,
link) reaches the prompt, and the message body is never in it.  The chat reads the message
in Zulip itself.

The wire is the router's envelope (see `claude_wake.py`):  a request
`{"platform", "target": {"session_id", "letter", "host", "generation"}, "wake": <Wake>}` and
an answer that repeats `wake_id`, `session_id` and `generation` exactly, plus the delivery.
A request that names no generation to check against is not this module's to deliver.

Turns (wake_router, Turns).  This module takes them (`TURNS`), and only by the Mac
dispatcher's mark:  a request marked `turn_dispatch` puts its valid riders into the same one
headless run, as the router's turn text with this module's prefix, given the resume limit once
per wake it carries, and the answer echoes them:
every rider as carried once the run may have started, none when it never started, and every
one as confirmed when the turn id shows up in the transcript.  `router_platform` declares
`deliver_turn`, by the same routes as `deliver`.  A request without the mark is delivered
exactly as before.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from . import claude_wake as CW
from . import muse_session_rename as MR
from . import platform_letter_muse as PLM
from . import wake_pending as WP
from . import wake_router as WR

SEAT = "MC"
PLATFORM = "muse"
MENTION = "Muse Code"
# A Muse session id is a UUID, the same shape `claude_wake.SESSION_RE` checks.
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
LETTER_RE = re.compile(r"[A-Z]{1,2}")
TARGET_FIELDS = frozenset(WR.Target.__dataclass_fields__)
BAD_REQUEST = "bad-request"
STDIN_LIMIT = 16 * 1024
CONFIRM_POLL_SECONDS = 1.0
#: The transcript is scanned newest-first in chunks of this size:  a live transcript can
#: record its cwd far from the tail, so no tail window can decide a missing cwd.
_SCAN_CHUNK = 1 << 20
#: A line cut by a chunk edge is carried into the next pass whole, so no overlap
#: allowance is needed:  a chunk boundary only ever splits lines, never records.

#: This seat's settings for the one-router service:  every router key, plus this platform's
#: own.  `wake_service` refuses a module whose DEFAULTS miss a router key (`bad-defaults`).
DEFAULTS: dict[str, Any] = {
    **WR.ROUTER_DEFAULTS,
    "local_host": "server",
    "mac_ssh": "",
    "muse_bin": "muse",
    "resume_timeout_seconds": 900,
    "confirm_seconds": 20,
}

# The router's own fixed text, as `command_platform` uses:  only the wake id, the router's
# link note and the message id can vary, so no Zulip-authored field reaches the prompt.
PREFIX = "[MC wake] "
WAKE_TEXT = PREFIX + WR.COMMAND_TEXT
#: This module takes turns (wake_router, Turns):  the Mac dispatcher reads this before it hands
#: a turn's offer to `deliver_request`.
TURNS = WR.TURN_CONTRACT

Runner = Callable[..., Any]
Resolve = Callable[[Mapping[str, str], str], Any]


def state_dir(env: Mapping[str, str]) -> Path:
    base = env.get("MUSE_WAKE_STATE") or os.path.join(
        str(env.get("HOME") or os.path.expanduser("~")), ".local", "state"
    )
    return Path(base) / "muse-wake"


def _chat_record(env: Mapping[str, str], session_id: str) -> Mapping[str, Any]:
    """The session-letter spool record for one chat, or {} when it has none.  It is the only
    persisted record of the HOME and XDG_DATA_HOME that chat itself ran under."""
    if not SESSION_RE.fullmatch(session_id or ""):
        return {}
    try:
        return MR.read_record(PLM.spool_path(env, session_id))
    except (OSError, ValueError):
        return {}


def _chat_env(env: Mapping[str, str], session_id: str) -> dict[str, str]:
    """The child delivery environment:  this host's HOME and PATH (so this host's `muse`
    binary is the one resolved), with XDG_DATA_HOME set to the parent of the store this
    delivery read.  A record with an empty `xdg_data_home` means the chat ran under its
    recorded home's default (`<home>/.local/share`), so that default is named explicitly:
    without it the child would resolve this host's default store instead.  Non-store Muse
    state under HOME (config, credentials) therefore remains the delivery host's, by
    design:  the forced-command identity on this host is what can run its binary."""
    out = dict(env)
    rec = _chat_record(env, session_id)
    if rec.get("xdg_data_home"):
        out["XDG_DATA_HOME"] = str(rec["xdg_data_home"])
    elif rec.get("home"):
        out["XDG_DATA_HOME"] = os.path.join(str(rec["home"]), ".local", "share")
    return out


def data_root(env: Mapping[str, str], session_id: str = "") -> str:
    """This host's Muse data root:  the chat's recorded root when it has one, else the
    delivery environment's.  A record's empty `xdg_data_home` is a recorded default, not a
    missing value, so the delivery environment's XDG does not override it."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    xdg = str(env.get("XDG_DATA_HOME") or "")
    if session_id:
        rec = _chat_record(env, session_id)
        if rec:
            home = str(rec.get("home") or home)
            xdg = str(rec.get("xdg_data_home") or "")
    return MR.data_root(home, xdg)


def wake_text(wake: WR.Wake) -> str:
    return WAKE_TEXT.format(
        wake_id=wake.wake_id,
        message_id=wake.message_id,
        note=wake.link_note + "  " if wake.link_note else "",
    )


def _short(value: object) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[A-Za-z0-9:_-]{0,80}", text) else "unreadable"


def _wake_from(data: Mapping[str, Any]) -> WR.Wake:
    """The Wake a request carries, with every Zulip-authored field cleaned again on this side."""
    if not WR.WAKE_ID_RE.fullmatch(str(data["wake_id"])):
        raise ValueError("wake id")
    return WR.Wake(
        wake_id=str(data["wake_id"]),
        seat=SEAT,
        message_id=data["message_id"] if type(data.get("message_id")) is int else 0,
        channel=WR.clean(data.get("channel")),
        topic=WR.clean(data.get("topic")),
        zulip_link=WR.clean(data.get("zulip_link"), 500),
        sender=WR.clean(data.get("sender")),
        dm=data.get("dm") is True,
        letter=str(data.get("letter") or ""),
        picked=_short(data.get("picked")),
        reason=_short(data.get("reason")),
        # It goes into the prompt outside the data fence, so only the router's own sentences.
        link_note=note if WR.link_note_ok(note := data.get("link_note")) else "",
    )


def _target_from(data: object) -> WR.Target:
    """The Target a request names, or ValueError.  The recheck needs every one of these fields."""
    if not isinstance(data, Mapping) or not set(data) <= TARGET_FIELDS:
        raise ValueError("target")
    session_id, letter = data.get("session_id"), data.get("letter")
    host, generation = data.get("host"), data.get("generation")
    if (
        not isinstance(session_id, str)
        or not SESSION_RE.fullmatch(session_id)
        or not isinstance(letter, str)
        or not LETTER_RE.fullmatch(letter)
        or not isinstance(host, str)
        or not host
        or type(generation) is not int
        or generation < 1
    ):
        raise ValueError("target")
    return WR.Target(session_id=session_id, letter=letter, host=host, generation=generation)


def _correlation(request: object) -> dict[str, Any]:
    """The ids an answer repeats back, read with this platform's own session-id pattern.  One
    that is not validly shaped comes back blank, so a bad request is never mistaken for an
    answer to a good one."""
    body = request if isinstance(request, Mapping) else {}
    target = body.get("target") if isinstance(body.get("target"), Mapping) else {}
    wake = body.get("wake") if isinstance(body.get("wake"), Mapping) else {}
    wake_id, session_id = wake.get("wake_id"), target.get("session_id")
    generation = target.get("generation")
    return {
        "wake_id": wake_id if isinstance(wake_id, str) and WR.WAKE_ID_RE.fullmatch(wake_id) else "",
        "session_id": session_id
        if isinstance(session_id, str) and SESSION_RE.fullmatch(session_id)
        else "",
        "generation": generation if type(generation) is int and generation > 0 else None,
    }


def _answer(request: object, delivery: WR.Delivery) -> dict[str, Any]:
    return {**_correlation(request), "delivery": asdict(delivery)}


def _registry_env(env: Mapping[str, str]) -> Mapping[str, str]:
    """`env` with the registry's address and key:  a forced ssh command has no login profile.

    The loader is read once and the mapping it gives is the one every call and every
    classification uses, so the host is judged on the configuration it really has.  A loader
    that cannot be read leaves the environment's own values in place."""
    from . import platform_letter as PL

    merged = dict(env)
    try:
        merged = PL.with_registry_env(merged)
    except Exception:  # noqa: BLE001 - the host's own configuration still decides
        pass
    return merged


def _resolve(registry_env: Mapping[str, str], letter: str) -> Any:
    """The registry's answer for one letter, made on the registry environment the caller
    built."""
    return WR.registry_api.call(
        registry_env,
        "resolve",
        {"platform": PLATFORM, "letter": letter},
        timeout=WR.REGISTRY_TIMEOUT,
    )


def _recheck(env: Mapping[str, str], target: WR.Target, resolve: Resolve) -> WR.Delivery | None:
    """Why this wake must not go to this chat after all, or None if it may.  Nothing has run
    yet, so a registry that cannot be read is `busy` and is tried again without spending an
    attempt."""
    # The registry environment is the one the call uses and the one the host is judged on:
    # the shared loader file on the Mac, the knobs on the server.  One mapping, built once,
    # so a call and the classification that follows it are made on the same configuration.
    registry_env = _registry_env(env)
    if WR.registry_loader_lost(env, registry_env):
        # The loader holds the registry's address and the mapping lost it:  the loader
        # could not be read, so the registry's identity is unverifiable here and the
        # delivery fails closed with the source named.  A loader that was read and holds
        # no address is a host that is not configured, and `registry_failure` answers that
        # one with the variable that is missing.  A registry that was configured and later
        # became unreachable is the outage, and it is `busy`.
        return WR.Delivery("failed", code=WR.NOT_CONFIGURED, note=WR.REGISTRY_LOADER_SOURCE)
    try:
        answer = resolve(registry_env, target.letter)
    except Exception:  # noqa: BLE001 - no delivery without a readable answer
        # The host's own configuration decides:  a host with no address or key fails
        # closed, and a host that is configured answers the outage and spends no attempt.
        return WR.registry_failure(registry_env)
    if not isinstance(answer, Mapping) or "error" in answer:
        return WR.registry_delivery(answer)
    held = WR.holds(answer, target, SEAT, SESSION_RE)
    if held is None:  # a live row that is not a well-formed MC chat proves no release
        return WR.Delivery("failed", code=WR.UNREADABLE_ROW)
    if not held:
        return WR.Delivery("refused", code=WR.RELEASED)
    return None


def _config(settings: object = None) -> dict[str, Any]:
    """The delivery settings:  this module's defaults, narrowed only by the router settings a
    `router_platform` caller passes.  No per-seat config file is read (one narrowing file on
    the router host replaces those, per the one-router design)."""
    config = dict(DEFAULTS)
    if isinstance(settings, Mapping):
        for name in DEFAULTS:
            if name in settings:
                config[name] = settings[name]
    return config


#: The `refused` codes this platform can return before anything ran.  The one-router service
#: collects these per adapter instead of keeping one central list in the router.
NEVER_RAN = frozenset(
    {"bad-session-id", "no-such-session", "subagent-session", "session-cwd-gone", "unknown-host"}
)


def _holder(session_dir: str | None) -> bool | None:
    """Whether a live process holds this chat:  True or False, or None on any doubt.  The lock
    file is read here rather than through `muse_session_rename.lock_holder`, which turns a
    read error into an empty file:  an unreadable lock, an unparsable one, or a liveness check
    that cannot answer is a doubt (None, so `busy`), never a no.  A missing or empty lock file
    is Muse's own way of saying the chat was let go, and that is a fact (False).  A pid that
    exists but is not ours counts as held:  it may be the chat, and a headless run against it
    would be a second writer.  The lock file is Muse's and carries no process start time to
    compare against, so a live pid is checked for being Muse at all:  a Muse process is
    held (True), and anything else — a reused pid, a wrapper or bundle whose name does not
    read as Muse, an unreadable process — is a doubt (None, so `busy`), never proof the
    chat was let go.  Muse itself also refuses a held session (`already in
    use`, probed on 1.4.15), but that is the last line, not this check."""
    if session_dir is None:
        return False
    path = os.path.join(session_dir, ".session.lock")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if not raw.strip():
        return False
    found = re.search(rb"^pid=(\d+)\s*$", raw, re.MULTILINE)
    if not found:
        return None
    pid = int(found.group(1))
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists and is not ours to signal; that is held enough
    except (OverflowError, OSError):
        return None
    try:
        proc = MR.read_proc(pid)
    except Exception:  # noqa: BLE001 - an unreadable process is a doubt, never a no
        return None
    if proc is None:
        return None  # raced with its exit, or unreadable: busy is the safe answer
    if MR.MUSE_NAME.match(proc.name) or MR.MUSE_NAME.match(os.path.basename(proc.exe)):
        return True
    return None  # identity unproven (reuse, wrapper, bundle): a doubt, never a no


class StoreUnreadable(Exception):
    """The session store exists but cannot be read well enough to say what it holds."""


def _find_session_dir(root: str, sid: str) -> str | None:
    """`muse_session_rename.find_session_dir` with its errors kept:  `glob` hides a permission
    error as an empty result, and an unreadable store must fail closed (`failed`), never read
    as no such session.  A store with no `sessions` folder at all is a fact:  no session."""
    base = os.path.join(root, "sessions")
    try:
        years = os.listdir(base)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreUnreadable(str(exc)) from exc
    found: list[str] = []
    for year in years:
        if not year[:1].isdigit():
            continue
        for month in _list(os.path.join(base, year)):
            for day in _list(os.path.join(base, year, month)):
                candidate = os.path.join(base, year, month, day, sid)
                # `isdir` answers False where `stat` would raise:  a day folder that can be
                # listed but not searched must fail closed like the rest of the store.
                try:
                    info = os.stat(candidate)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise StoreUnreadable(str(exc)) from exc
                if stat.S_ISDIR(info.st_mode):
                    found.append(candidate)
    return sorted(found)[-1] if found else None


def _list(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except (FileNotFoundError, NotADirectoryError):
        return []  # a stray file or a vanished folder holds no sessions; it poisons nothing
    except OSError as exc:
        raise StoreUnreadable(str(exc)) from exc


def _record_cwd(record: object) -> str:
    """The working directory a transcript record asserts, or "" when it asserts none.
    Only the schema-defined fields count:  a `cwd` mentioned inside a string value (tool
    output, quoted chat text, a pasted snippet) is content, never an assertion, so raw
    text matching must not decide this."""
    if not isinstance(record, dict):
        return ""
    cwd = record.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    payload = record.get("payload")
    if isinstance(payload, dict):
        inner = payload.get("record")
        if isinstance(inner, dict):
            cwd = inner.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    return ""


def _session_cwd(session_dir: str) -> str:
    """The chat's working directory, from the newest `cwd` its transcript asserts, or ""
    when it asserts none.  The scan runs newest-first in bounded chunks and stops at the
    first assertion, so a transcript whose cwd sits far from the tail costs one backward
    walk, not a tail window that can miss it;  proving no cwd exists still reads the
    whole file, which no window can avoid.  A line cut by a chunk edge is carried into
    the next pass whole.  A transcript that cannot be read raises StoreUnreadable:  an
    unknown cwd is a doubt that fails closed, never the absence a refusal is built on."""
    path = os.path.join(session_dir, "session.jsonl")
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            carry = b""
            while end > 0:
                start = max(0, end - _SCAN_CHUNK)
                fh.seek(start)
                buf = fh.read(end - start) + carry
                lines = buf.split(b"\n")
                if start > 0:
                    carry = lines[0]
                    candidates = lines[1:]
                else:
                    carry = b""
                    candidates = lines
                for raw in reversed(candidates):
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        continue
                    cwd = _record_cwd(record)
                    if cwd and os.path.isabs(cwd):
                        return cwd
                end = start
    except OSError as exc:
        raise StoreUnreadable(str(exc)) from exc
    return ""


def _child_env(env: Mapping[str, str]) -> dict[str, str]:
    """What a headless run in the chat inherits:  HOME, PATH and this host's data root when one
    is set, and nothing else.  No registry key and no routine key ever reach a woken chat."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    child = {
        "HOME": home,
        "PATH": os.path.join(home, ".local", "bin")
        + os.pathsep
        + str(env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"),
        "AGENT_LAUNCHER": "simple-tools-wake",
        "AGENT_LAUNCH_SEAT": SEAT,
        "SESSION_LETTER": "0",
    }
    if env.get("XDG_DATA_HOME"):
        child["XDG_DATA_HOME"] = str(env["XDG_DATA_HOME"])
    return child


def _binary(env: Mapping[str, str], config: Mapping[str, Any]) -> str | None:
    return shutil.which(str(config.get("muse_bin") or "muse"), path=_child_env(env)["PATH"])


def _seen_appended(
    path: str,
    offset: int,
    wake_id: str,
    seconds: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Whether the wake id shows up in what a transcript gains after `offset`, within `seconds`.
    An exit 0 alone does not prove the turn ran (a `-p` mode can exit 0 on a failed turn), and
    the wake id in the appended prompt proves only hand-over, never a finished turn, so the
    outcome it confirms is `sent`, exactly the Claude socket route's receipt."""
    needle = wake_id.encode("utf-8")
    deadline = clock() + seconds
    while True:
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                keep = b""
                while chunk := fh.read(64 * 1024):
                    if needle in keep + chunk:
                        return True
                    keep = (keep + chunk)[-len(needle) + 1 :] if len(needle) > 1 else b""
        except OSError:
            pass  # a transient read says nothing; the deadline, not the first error, decides
        if clock() >= deadline:
            return False
        sleep(CONFIRM_POLL_SECONDS)


def _via_exec(
    env: Mapping[str, str],
    config: Mapping[str, Any],
    session_dir: str,
    session_id: str,
    wake: WR.Wake,
    runner: Runner,
    sleep: Callable[[float], None] = time.sleep,
    *,
    riders: tuple[WR.Wake, ...] = (),
    turn_id: str = "",
    echo: dict[str, Any] | None = None,
) -> WR.Delivery:
    """One headless prompt in an unheld chat, in that same session.  With riders it is the
    router's turn text, run for the resume limit once per wake it carries, and `echo` records
    what the prompt carried and what the chat recorded:  every rider once the run may have
    started, none when it never did."""
    try:
        resume_timeout = int(config["resume_timeout_seconds"])
        confirm_seconds = int(config["confirm_seconds"])
    except (KeyError, TypeError, ValueError):
        return WR.Delivery("failed", "exec", code="settings-unreadable")
    transcript = os.path.join(session_dir, "session.jsonl")
    if _wake_seen(transcript, wake.wake_id) and not riders:
        # Singles only:  a turn's prompt also carries the head's id, so a turn request
        # must reach the turn-id check below, which is the one that reports the riders.
        return WR.Delivery("sent", "exec", ack="confirmed")
    binary = _binary(env, config)
    if not binary:
        return WR.Delivery("failed", "exec", code="muse-not-found")
    try:
        cwd = _session_cwd(session_dir)
    except StoreUnreadable:
        return WR.Delivery("failed", "exec", code="store-unreadable")
    if not cwd:
        return WR.Delivery("refused", "exec", code="session-cwd-gone")
    try:
        info = os.stat(cwd)
    except (FileNotFoundError, NotADirectoryError):
        return WR.Delivery("refused", "exec", code="session-cwd-gone")
    except OSError:
        return WR.Delivery("failed", "exec", code="store-unreadable")
    if not stat.S_ISDIR(info.st_mode):
        return WR.Delivery("refused", "exec", code="session-cwd-gone")
    text = WR.turn_text(PREFIX, wake, riders, turn_id) if riders else wake_text(wake)
    argv = [binary, "exec", "--session-id", session_id, text]
    transcript = os.path.join(session_dir, "session.jsonl")
    # A retry must not run a turn the transcript already recorded:  the turn id for a batched turn,
    # the wake id for a single wake.  Either way the earlier run is the hand-over.
    needle = turn_id if riders else wake.wake_id
    if needle and _wake_seen(transcript, needle):
        if riders and echo is not None:
            # The transcript already holds this turn's prompt, which names every rider:  say
            # so, or the router reads the echo as carrying nobody and delivers them again.
            echo["carried"] = tuple(rider.wake_id for rider in riders)
            echo["confirmed"] = echo["carried"]
            try:
                state = WP.state_dir(_pending_home(env))
                for rider in riders:
                    WP.mark_delivered(state, session_id, rider.wake_id)
            except OSError:
                pass
        return WR.Delivery("sent", "exec", ack="confirmed")
    try:
        offset = os.path.getsize(transcript)
    except OSError:
        offset = 0
    if echo is not None:
        echo["carried"] = tuple(rider.wake_id for rider in riders)
    try:
        proc = runner(
            argv,
            cwd=cwd,
            env=_child_env(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=resume_timeout * (1 + len(riders)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # It may have done real work:  retrying could run the turn twice.
        return WR.Delivery("refused", "exec", code="exec-timeout")
    except OSError as exc:
        # It never started:  the one failure here that is safe to try again.
        if echo is not None:
            echo["carried"] = ()
        return WR.Delivery("failed", "exec", code="exec-%s" % type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - it may have started, so it is final
        return WR.Delivery("refused", "exec", code="exec-error-%s" % type(exc).__name__)
    if proc.returncode == 0:
        # The turn already ran:  a ledger failure must not turn this handoff into a retryable `failed`
        # (the router would run the turn twice), so the mark is best-effort and the transcript stays
        # the hand-over record.  Every carried wake is marked, so neither the head nor a rider is
        # injected again afterwards.
        try:
            state = WP.state_dir(_pending_home(env))
            WP.mark_delivered(state, session_id, wake.wake_id)
            for rider in riders:
                WP.mark_delivered(state, session_id, rider.wake_id)
        except OSError:
            pass
        # A turn's text carries the turn id, as a single wake's carries its wake id.
        needle = turn_id if riders else wake.wake_id
        if _seen_appended(transcript, offset, needle, confirm_seconds, sleep=sleep):
            # The prompt reaching the transcript proves hand-over, not a finished turn:  the
            # wake text carries its own id, so it is recorded even when the turn then fails.
            # `completed` would claim the turn finished, so this is `sent`, confirmed.
            if riders and echo is not None:
                echo["confirmed"] = echo["carried"]
            return WR.Delivery("sent", "exec", ack="confirmed")
        # It ran and may have done the work, but nothing in the chat proves it:  handed over,
        # never counted as delivered, and never retried.
        return WR.Delivery("sent", "exec", ack="unknown")
    # It started, so it may have run the turn before it exited:  never retried.
    return WR.Delivery("refused", "exec", code="exec-exit-%d" % proc.returncode)


def _pending_home(env: Mapping[str, str]) -> str:
    """The home directory the parked-wake spool resolves under:  this host user's own home only, never an
    environment override, so the hook (which runs with a cleared environment) resolves the identical spool."""
    return str(env.get("HOME") or os.path.expanduser("~"))


def _wake_seen(transcript: str, wake_id: str) -> bool:
    """Whether the wake id is already recorded in the transcript's tail:  one bounded,
    non-waiting read.  The hook hands a parked wake over as prompt context, so a retry
    that finds the id here must not run the turn again."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _SCAN_CHUNK))
            return wake_id.encode("utf-8") in fh.read()
    except (OSError, ValueError):
        return False


def _handed_over(state: str, transcript: str, wake: WR.Wake) -> WR.Delivery | None:
    """`sent` when this wake already reached the chat through the hook (its id consumed,
    and confirmed when the transcript itself carries it), or None when it did not."""
    if not WP.is_consumed(state, wake.wake_id):
        return None
    if _wake_seen(transcript, wake.wake_id):
        return WR.Delivery("sent", "hook", ack="confirmed")
    return WR.Delivery("sent", "hook", ack="unknown")


def deliver_local(
    env: Mapping[str, str],
    config: Mapping[str, Any],
    session_id: str,
    wake: WR.Wake,
    *,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    _locked: bool = False,
    _turn: Mapping[str, Any] | None = None,
) -> WR.Delivery:
    """Deliver one wake to a Muse chat on this host.  Not knowing whether a process
    holds it is `busy`, and a sub-agent's session is never a target.  A held chat gets
    the wake parked for its next prompt and the same `busy`, and an already-handed-over
    wake is `sent` without running anything twice.  `_locked` is for
    `deliver_request`, which already holds this session's lock:  taking it again on a
    second file description would refuse itself.  `_turn` is a turn's, for `_via_exec`."""
    turn = dict(_turn or {})
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        return WR.Delivery("refused", code="bad-session-id")
    chat_env = _chat_env(env, session_id)
    root = data_root(env, session_id)
    try:
        session_dir = _find_session_dir(root, session_id)
        if session_dir is None and MR.is_subagent(root, session_id):
            return WR.Delivery("refused", code="subagent-session")
    except StoreUnreadable:
        return WR.Delivery("failed", code="store-unreadable")
    if session_dir is None:
        return WR.Delivery("refused", code="no-such-session")
    state = WP.state_dir(_pending_home(env))
    transcript = os.path.join(session_dir, "session.jsonl")
    if (handed := _handed_over(state, transcript, wake)) is not None:
        return handed
    held = _holder(session_dir)
    if held is None:
        return WR.Delivery("busy", code="holder-unknown")
    if held:
        if WP.park(state, session_id, wake.wake_id, wake_text(wake)):
            return WR.Delivery("busy", code="chat-live-parked")
        # Park refused:  the wake id is already consumed, so the hook handed it over.
        # The transcript check inside confirms it;  without the record the ledger is
        # still proof the hook emitted it into a prompt.
        handed = _handed_over(state, transcript, wake)
        if handed is not None:
            return handed
        return WR.Delivery("sent", "hook", ack="unknown")
    if _locked:
        return _via_exec(chat_env, config, session_dir, session_id, wake, runner, sleep, **turn)
    try:
        folder = state_dir(env) / "locks"
        WR.private_dir(folder)
        fd = os.open(folder / ("session-%s.lock" % session_id), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        return WR.Delivery("failed", code="lock-%s" % type(exc).__name__)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return WR.Delivery("busy", "exec", code="session-locked")
        # A chat opened while the lock was being taken makes this a message delivery after all.
        held = _holder(session_dir)
        if held is None:
            return WR.Delivery("busy", code="holder-unknown")
        if held:
            return WR.Delivery("busy", code="chat-live")
        return _via_exec(chat_env, config, session_dir, session_id, wake, runner, sleep, **turn)


def deliver_request(
    request: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    settings: object = None,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    resolve: Resolve = _resolve,
) -> dict[str, Any]:
    """One `muse` delivery on this host, for the router on the other one.  The registry is
    rechecked under the per-session lock immediately before anything runs."""
    here = dict(os.environ if env is None else env)
    try:
        target = _target_from(request.get("target"))
        wake = _wake_from(request["wake"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return _answer(request, WR.Delivery("refused", code=BAD_REQUEST))
    # A turn (wake_router, Turns) only when the dispatcher marked it, and only its valid riders.
    riders: tuple[WR.Wake, ...] = ()
    if request.get("turn_dispatch") == WR.TURN_CONTRACT:
        riders = WR.turn_riders_from(request.get("turn"), wake.wake_id)
    turn_id = request["turn"]["id"] if riders else ""
    echo: dict[str, Any] = {"carried": (), "confirmed": ()}
    turn = {"riders": riders, "turn_id": turn_id, "echo": echo}
    config = _config(settings)
    if settings is None:
        # This entry point is the Mac-side handler (`wake-deliver`); a server router passes
        # its own settings when it delivers a chat on its own host.
        config["local_host"] = "mac"
    if target.host != str(config.get("local_host") or ""):
        return _answer(request, WR.Delivery("refused", code="unknown-host"))
    # The lock comes first, so the registry recheck and the delivery cannot race a second
    # delivery of the same wake to the same chat.
    try:
        folder = state_dir(here) / "locks"
        WR.private_dir(folder)
        fd = os.open(
            folder / ("session-%s.lock" % target.session_id), os.O_CREAT | os.O_RDWR, 0o600
        )
    except OSError as exc:
        return _answer(request, WR.Delivery("failed", code="lock-%s" % type(exc).__name__))
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _answer(request, WR.Delivery("busy", code="session-locked"))
        if (stale := _recheck(here, target, resolve)) is not None:
            return _answer(request, stale)
        try:
            delivery = deliver_local(
                here,
                config,
                target.session_id,
                wake,
                runner=runner,
                sleep=sleep,
                _locked=True,
                _turn=turn,
            )
        except OSError as exc:
            delivery = WR.Delivery("failed", code="deliver-%s" % type(exc).__name__)
    answer = _answer(request, delivery)
    if riders:
        answer["turn"] = WR.turn_echo(turn_id, echo["carried"], echo["confirmed"])
    return answer


def router_platform(
    env: Mapping[str, str],
    settings: object = None,
    *,
    runner: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> WR.Platform:
    """This platform for the shared wake router service (`wake_service.BUILDERS` names this
    function for seat MC).  A chat on this router's host goes through `deliver_request`, so
    the registry recheck under the per-session lock and the exact-id answer apply exactly as
    they do under the ssh forced command.  A Mac chat goes through the one fixed ssh route
    in `claude_wake.deliver_remote`; any other host is refused."""
    here = dict(env)
    config = _config(settings)
    local_host = str(config.get("local_host") or "server")

    def deliver(target: WR.Target, wake: WR.Wake) -> WR.Delivery:
        if target.host == "mac" and local_host != "mac":
            return CW.deliver_remote(here, config, target, wake, platform=PLATFORM, runner=runner)
        if target.host != local_host:
            return WR.Delivery("refused", code="unknown-host")
        request = {
            "platform": PLATFORM,
            "target": {
                "session_id": target.session_id,
                "letter": target.letter,
                "host": target.host,
                "generation": target.generation,
            },
            "wake": asdict(wake),
        }
        answer = deliver_request(request, env=here, settings=config, runner=runner, sleep=sleep)
        delivery = answer.get("delivery") if isinstance(answer, Mapping) else None
        if not isinstance(delivery, Mapping) or delivery.get("outcome") not in WR.OUTCOMES:
            return WR.Delivery("refused", code="platform-bad-answer")
        return WR.Delivery(
            str(delivery["outcome"]),
            _short(delivery.get("route")),
            ack=_short(delivery.get("ack")) or "unknown",
            code=_short(delivery.get("code")),
            note=_short(delivery.get("note")),
        )

    def turn(
        target: WR.Target, head: WR.Wake, riders: tuple[WR.Wake, ...], turn_id: str
    ) -> WR.Turn:
        """One idle turn (wake_router, Turns), by the same routes as `deliver`.  Once the local
        delivery may have started, an error in it cannot say what the turn carried."""
        if target.host == "mac" and local_host != "mac":
            return CW.deliver_remote_turn(
                here, config, target, head, riders, turn_id, platform=PLATFORM, runner=runner
            )
        if target.host != local_host:
            return WR.Turn(WR.Delivery("refused", code="unknown-host"))
        request = {
            "platform": PLATFORM,
            "target": {
                "session_id": target.session_id,
                "letter": target.letter,
                "host": target.host,
                "generation": target.generation,
            },
            "wake": asdict(head),
            "turn": WR.turn_offer(turn_id, riders),
            "turn_dispatch": WR.TURN_CONTRACT,
        }
        try:
            answer = deliver_request(request, env=here, settings=config, runner=runner, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 - it may have started, so it is final
            error = WR.Delivery("refused", code="platform-error-%s" % type(exc).__name__)
            return WR.Turn(error, unreadable=True)
        echo = answer.get("turn") if isinstance(answer, Mapping) else None
        return WR.turn_result(_read_answer(answer), echo, turn_id, riders)

    def exclude(targets: list[WR.Target]) -> set[str]:
        # One walk per distinct store root (glob hides its own errors, and delivery
        # re-checks every pick against the store anyway, so a doubt here excludes nothing).
        # A chat may live under its own recorded root, so the root is per target.
        import glob as _glob

        local = str(config.get("local_host") or "")
        by_root: dict[str, set[str]] = {}

        def subagents(root: str) -> set[str]:
            if root not in by_root:
                nested = _glob.glob(
                    os.path.join(
                        _glob.escape(root),
                        "sessions",
                        "[0-9]*",
                        "[0-9]*",
                        "[0-9]*",
                        "*",
                        "subagent",
                        "*",
                    )
                )
                top_level = _glob.glob(
                    os.path.join(_glob.escape(root), "sessions", "[0-9]*", "[0-9]*", "[0-9]*", "*")
                )
                # As in MR.is_subagent, an id with a top-level folder is a chat.
                by_root[root] = {os.path.basename(p) for p in nested} - {
                    os.path.basename(p) for p in top_level
                }
            return by_root[root]

        return {
            t.session_id
            for t in targets
            if t.host == local and t.session_id in subagents(data_root(here, t.session_id))
        }

    return WR.Platform(
        seat=SEAT,
        platform=PLATFORM,
        mention=MENTION,
        target_re=SESSION_RE,
        deliver=deliver,
        exclude=exclude,
        deliver_turn=turn,
    )


def _read_answer(answer: object) -> WR.Delivery:
    """The Delivery a `deliver_request` answer carries, its codes kept only if code-shaped."""
    delivery = answer.get("delivery") if isinstance(answer, Mapping) else None
    if not isinstance(delivery, Mapping) or delivery.get("outcome") not in WR.OUTCOMES:
        return WR.Delivery("refused", code="platform-bad-answer")
    return WR.Delivery(
        str(delivery["outcome"]),
        _short(delivery.get("route")),
        ack=_short(delivery.get("ack")) or "unknown",
        code=_short(delivery.get("code")),
        note=_short(delivery.get("note")),
    )


def main(stdin: TextIO, stdout: TextIO, env: Mapping[str, str]) -> int:
    """One delivery read from stdin, for a caller with no router of its own."""
    try:
        raw = stdin.read(STDIN_LIMIT + 1)
        if len(raw) > STDIN_LIMIT:
            raise ValueError("stdin over the limit")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("not an object")
    except (ValueError, RecursionError):
        answer = _answer(None, WR.Delivery("refused", code=BAD_REQUEST))
    else:
        answer = deliver_request(request, env=env)
    stdout.write(json.dumps(answer) + "\n")
    return 0
