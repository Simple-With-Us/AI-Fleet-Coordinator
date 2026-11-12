"""Fx (Vercel Labs' `fx` CLI; seat FX, Mac only) on `platform-letter`: an Operator session letter on every
interactive fx session.  Unlike every other adapter, this one has no hook to install, and it says so rather
than inventing one — read "No hooks" below.  What it has is the seat's session store and the same registry
side every other adapter uses, so a sync run registers the store's live sessions with the Operator through
`platform_letter.registry_main`: with the registry API configured in the caller's environment (the host's
shared registry env file, `registry_api`) those registrations land in the server registry, the same store
MC's and GROK's letters land in, and with no API configured they land in the local registry file, exactly as
a hook's would on the Mac.

No hooks.  The fx CLI (0.0.x, checked Nov 8, 2026 against fx's own documentation) has no hook configuration:
repository settings live in `.fx.json`, which cannot define custom endpoints or credentials and names no
lifecycle command, and the only `hooks` surface fx ships is the libfx JavaScript API's callbacks, for agents
a host program embeds — not for the interactive CLI the seat runs.  There is therefore no fx hook file for
an installer to write, no event a registration could ride on, and no title a hook could set (fx keeps a
session's title in its own store and generates it itself).  `platform-letter install --platform fx` refuses
with that reason (exit 2), so no host is ever left with a hooks file fx would ignore.

The store.  fx saves each session under `~/.fx/sessions/<id>/session.json`, with `index.json` beside the
folders naming the session ids — the same store the chat catalog reads (`chat_catalog._scan_fx`).  A
session's `session.json` carries its `title` and its workspace (`workspace_root`), and its timestamps
(`updated_at`, `last_active_at`, `created_at`).  The catalog reads that store as metadata-only (`wake=False`):
fx sessions cannot be woken, and a letter does not change that — it makes a live fx session addressable
("@**Fx** XX") in the registry the Operator resolves letters from, which is all a letter ever does.

The sync.  `python3 -m simple_tools.platform_letter_fx sync [--apply] [--location mac]` reads the index and
registers every session the store holds that was active within the library's own idle window
(`platform_letter.STALE_SECONDS`, 36 hours), under the seat FX and this adapter's source, with the title the
store carries.  Without `--apply` it only reports what it would register.  A session the store no longer
holds, or one idle past the window, is closed in the registry if this adapter holds a live registration for
it, so a letter is freed when its session is gone rather than at the next sweep; a registration the Operator
made another way is never closed here (the registry side's own rule, kept by asking it to close only ids this
sync just found registered under this adapter's source).  Registering is idempotent: a session that already
holds a letter resumes it, so a sync can run as often as the seat's host wants — at session start from the
seat's own shell setup, or on a timer — and costs one registry call per live session.  That scheduling is
the host's: nothing in this adapter installs a timer, and a sync that never runs letters nothing, which the
report says plainly.

Who takes a letter.  Every session in the store inside the idle window.  fx names no parent for a forked
session in the fields the catalog reads, so every session is registered as a main chat; a `fx ask` one-shot
writes a session too and cannot be told from an interactive one in the store, so it takes a letter while it
is inside the window and frees it when it leaves — the same trade the sweep-based platforms make for
sessions whose end no event reports.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, TextIO

from . import platform_letter as PL
from . import session_ids as SI

PLATFORM = "fx"
SEAT = "FX"
SOURCE = "fx-letter-sync"
DISPLAY = "Fx"

ENV_OFF = "FX_LETTER"
SESSIONS_SUBPATH = os.path.join(".fx", "sessions")


def sessions_dir(env: Mapping[str, str]) -> str:
    home = str(env.get("HOME") or os.path.expanduser("~"))
    return os.path.join(home, SESSIONS_SUBPATH)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _timestamp(meta: Mapping[str, Any]) -> float | None:
    """The session's last activity as a POSIX time, from the first of its timestamp fields that parses
    (a number is taken as seconds, or milliseconds when it is plainly one; a string as ISO 8601), or None
    when none parses.  An unknown time is never read as the epoch:  that would call a live session stale."""
    for name in ("updated_at", "last_active_at", "created_at"):
        value = meta.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value) / 1000.0 if value > 1e11 else float(value)
        if isinstance(value, str) and value.strip():
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def store_sessions(env: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every session the fx store's index names, with the title and last activity its own record carries.
    A session whose record cannot be read is skipped, never invented."""
    base = sessions_dir(env)
    index = _read_json(os.path.join(base, "index.json")) or {}
    named = index.get("sessions")
    ids: list[str] = []
    if isinstance(named, list):
        for item in named:
            sid = _text(item.get("id")) if isinstance(item, dict) else _text(item)
            if sid:
                ids.append(sid)
    if not ids:
        try:
            ids = sorted(
                name for name in os.listdir(base) if os.path.isdir(os.path.join(base, name))
            )
        except OSError:
            return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sid in ids:
        if sid in seen:
            continue
        seen.add(sid)
        try:
            SI.check(sid, "the session id")
        except ValueError:
            continue  # not an id fx would make, and never a path to build
        if os.path.basename(sid) != sid:
            continue
        meta = _read_json(os.path.join(base, sid, "session.json"))
        if meta is None:
            continue
        out.append(
            {
                "session_id": sid,
                "title": _text(meta.get("title")),
                "updated": _timestamp(meta),
            }
        )
    return out


def _has_session(env: Mapping[str, str], sid: str) -> bool:
    """True when the store holds a readable record for this one id, read directly rather than by loading
    the whole store."""
    try:
        SI.check(sid, "the session id")
    except ValueError:
        return False
    if os.path.basename(sid) != sid:
        return False
    return _read_json(os.path.join(sessions_dir(env), sid, "session.json")) is not None


def fx_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """A payload this adapter is handed (there are no fx hooks, so this is the sync's own shape) is a chat
    when it names a session of the store, and headless otherwise."""
    sid = _text(payload.get("session_id"))
    return "chat" if sid and _has_session(env, sid) else "headless"


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("session_id"),
    kind=fx_kind,
    current_title=PL.from_payload("title"),
    stored_title=PL.from_payload("title"),
    locations=("mac",),
    env_off=ENV_OFF,
)


def install(opts: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """There is nothing to install: fx has no hook file (see the module docstring).  A check or a remove is
    answered the same way, so no caller can mistake a written file for a working hook."""
    return PL._emit(
        stdout,
        {
            "error": "fx has no hook configuration to install into: its CLI names no lifecycle hook, "
            "so this adapter registers from the session store instead — run "
            "`python3 -m simple_tools.platform_letter_fx sync --apply` on the seat's host"
        },
        2,
    )


# --------------------------------------------------------------------------------------------
# The sync (`python3 -m simple_tools.platform_letter_fx sync [--apply] [--location mac]`)
# --------------------------------------------------------------------------------------------


def _registry(
    env: Mapping[str, str], op: str, sid: str, *, location: str, title: str = ""
) -> tuple[int, dict[str, Any]]:
    argv = [
        op,
        "--platform",
        PLATFORM,
        "--location",
        location,
        "--session-id",
        sid,
        "--seat",
        SEAT,
        "--source",
        SOURCE,
    ]
    if title:
        argv.append("--title=" + title)
    out = io.StringIO()
    code = PL.registry_main(argv, env=env, stdout=out)
    answer = PL._last_json(out.getvalue()) or {}
    return code, answer


#: The ids this sync has registered, kept beside the seat's other platform-letter state, so a session that
#: has left the store entirely (deleted, or its record unreadable) is still closed:  the store alone cannot
#: name an id it no longer holds.
LEDGER_NAME = "platform-letter-fx.json"


def _ledger_path(env: Mapping[str, str]) -> str:
    return os.path.join(PL.L.state_root(env), LEDGER_NAME)


def _read_ledger(env: Mapping[str, str]) -> list[str]:
    doc = _read_json(_ledger_path(env)) or {}
    ids = doc.get("registered")
    return [i for i in ids if isinstance(i, str) and i] if isinstance(ids, list) else []


def _write_ledger(env: Mapping[str, str], ids: list[str]) -> None:
    path = _ledger_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"registered": sorted(set(ids))}, fh)
    os.replace(tmp, path)


def _owned(answer: Mapping[str, Any]) -> bool:
    """A `show` answer this sync may close:  found, and not held under another seat.  The API's answer
    names no seat;  there the identity itself (platform fx, this session id) is the ownership check."""
    seat = _text(answer.get("seat"))
    return bool(answer.get("found")) and (not seat or seat.upper() == SEAT)


def sync_main(
    argv: list[str], env: Mapping[str, str], stdout: TextIO, *, now: float | None = None
) -> int:
    """Register the store's live sessions (and, with --apply, close the ones that are gone).

    One JSON answer: the sessions found, the ones inside the idle window with the letter each holds (or
    would hold, without --apply), and the registrations closed.  An answer that carries an `error` for a
    session is counted under `failed`, never hidden."""
    import argparse

    parser = argparse.ArgumentParser(prog="platform-letter-fx sync")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--location", default="mac", choices=PL.LOCATIONS)
    args = parser.parse_args(argv)
    moment = time.time() if now is None else now
    sessions = store_sessions(env)
    # A session with no readable time is neither registered nor closed:  nothing says whether it is live.
    timed = [s for s in sessions if s["updated"] is not None]
    live = [s for s in timed if moment - s["updated"] <= PL.STALE_SECONDS]
    registered: list[dict[str, Any]] = []
    failed: list[str] = []
    closed: list[str] = []
    for session in live:
        sid = session["session_id"]
        if not args.apply:
            registered.append({"session_id": sid, "would_register": True})
            continue
        code, answer = _registry(
            env, "register", sid, location=args.location, title=session["title"]
        )
        if code != 0 or "error" in answer:
            failed.append(sid)
            continue
        registered.append(
            {
                "session_id": sid,
                "label": answer.get("label", ""),
                "outcome": answer.get("outcome", ""),
            }
        )
    if args.apply:
        # Close the registrations this sync no longer has a live session for:  ids the store still names
        # but that have fallen outside the window, and ids this sync registered earlier that the store no
        # longer holds at all.  Each is read back first (`show`), and only a registration found and not
        # held under another seat is closed.  A show or close that fails is counted under `failed`.
        live_ids = {s["session_id"] for s in live}
        store_ids = {s["session_id"] for s in sessions}
        untimed = {s["session_id"] for s in sessions if s["updated"] is None}
        ledger = _read_ledger(env)
        candidates = [s["session_id"] for s in timed if s["session_id"] not in live_ids]
        candidates += [sid for sid in ledger if sid not in store_ids]
        kept = [sid for sid in ledger if sid in untimed]
        for sid in dict.fromkeys(candidates):
            code, answer = _registry(env, "show", sid, location=args.location)
            if answer.get("error") == PL.NO_REGISTRATION:
                continue
            if code != 0 or "error" in answer:
                failed.append(sid)
                kept.append(sid)
                continue
            if not _owned(answer):
                continue
            close_code, close_answer = _registry(env, "close", sid, location=args.location)
            if close_code == 0 and close_answer.get("closed"):
                closed.append(sid)
            elif close_code == 0 and close_answer.get("retired"):
                continue  # already closed:  nothing left to do
            else:
                failed.append(sid)
                kept.append(sid)
        _write_ledger(env, [r["session_id"] for r in registered] + kept)
    answer_doc: dict[str, Any] = {
        "platform": PLATFORM,
        "found": len(sessions),
        "live": len(live),
        "registered": registered,
        "closed": closed,
        "failed": failed,
        "applied": bool(args.apply),
    }
    stdout.write(json.dumps(answer_doc, sort_keys=True) + "\n")
    return 0 if not failed else 1


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = stdout or sys.stdout
    if argv[:1] == ["sync"]:
        return sync_main(argv[1:], env, out)
    out.write("usage: python3 -m simple_tools.platform_letter_fx sync [--apply] [--location mac]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
