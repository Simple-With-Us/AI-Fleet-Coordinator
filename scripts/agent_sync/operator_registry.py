"""Durable Operator session registry and export outbox (AFC-412 PR1).

Identity is (platform, verified_host, profile_scope, native_session_id) - never the seat, never
the label, never the title.  A resume on the same tuple returns the same immutable record_id and
its live label;  a fork or a new native session id is a new registration.

Labels are per-platform letters A-Z with one namespace across all hosts.  RECYCLE is on (owner
2026-10-20, option B):  allocation takes the lowest letter with no active assignment, wrapping
after Z back to A, and a reused letter gets the next generation.  Cross-seat references to a
generation > 1 letter must carry the generation:  "MM A (gen 2)".

Seat is checked separately from identity:  a resolve under an unexpected seat returns the
existing record with outcome "seat_mismatch", writes an events row, and changes nothing.  No
rebind, and no token or wake authority is derived from a match.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from . import secretscan
from . import session_ids as SI
from .operator_registry_schema import DDL, SCHEMA_VERSION

LETTERS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Two letter spaces on one platform (owner 2026-10-28):  a main conversation takes a
# single letter, a subthread takes a double one.  The space is decided when the session
# is registered and never changes afterwards:  a resume finds the row that already
# holds the letter, so a session id always answers with the same letter.
LETTER_SPACES = {
    "single": LETTERS,
    "double": tuple(a + b for a in LETTERS for b in LETTERS),
}
ALL_LETTERS = tuple(sorted(set(LETTERS) | set(LETTER_SPACES["double"])))
MUTABLE_ATTRS = ("title", "project", "thread_link", "apple_note_link", "state", "wake_capability")
IDENTITY_FIELDS = ("platform", "verified_host", "profile_scope", "native_session_id")

# One platform namespace per platform, not per adapter.
#
# The chat catalog discovers sessions through adapters, and two adapters can feed
# one seat:  "cursor-cloud" and "cursor-local" are both Cursor.  A letter is
# issued per platform, so the namespace has to be the platform.  Keyed on the
# adapter name the catalog emits, folding to the namespace the letters live in.
#
# Only adapters that feed the SAME seat are folded.  Two Cursor adapters are one
# platform because they are one seat;  folding an adapter that might later gain
# a sibling for a different seat would merge two seats' letter spaces, and a
# letter is only unique inside one of them.  An adapter that is not listed folds
# to itself, so a new adapter gets its own namespace rather than colliding with
# another platform's.
PLATFORM_NAMESPACES = {
    "cursor": "cursor",
    "cursor-cloud": "cursor",
    "cursor-local": "cursor",
}

SESSIONS_HEADERS = (
    "Record ID",
    "Label",
    "Seat",
    "Platform",
    "Host",
    "Native Session ID",
    "Title",
    "Project",
    "Thread Link",
    "Apple Note Link",
    "State",
    "Wake Capability",
    "Registered At",
    "Last Seen At",
    "Closed At",
    "Source",
)
EVENTS_HEADERS = (
    "Event ID",
    "Recorded At",
    "Session Record ID",
    "Label",
    "Event",
    "Actor",
    "Outcome",
    "Source",
    "Details",
)


class LabelExhausted(RuntimeError):
    """Every letter A-Z on this platform has an active assignment."""


class UnknownRecord(KeyError):
    """No session with this record_id."""


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(now_ms: int | None = None) -> str:
    """A 26-character ULID (48-bit ms time + 80-bit random, Crockford base32).  Minted once per
    record or event and never reused."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    now_ms &= (1 << 48) - 1
    rand = secrets.randbits(80)
    out = []
    for shift in range(45, -1, -5):
        out.append(_CROCKFORD[(now_ms >> shift) & 31])
    for shift in range(75, -1, -5):
        out.append(_CROCKFORD[(rand >> shift) & 31])
    return "".join(out)


def _utcnow() -> str:
    """Canonical UTC instant.  Display formatting (12-hour America/Chicago) is the exporter's job."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def display_title(label: str, title: str) -> str:
    """In-platform display form (owner 2026-10-20):  "H: Title"."""
    return f"{label}: {title}" if label else title


SECRET_DETECTED = "secret_detected"


def _scan_title(title: str | None) -> None:
    """Refuse a title that carries a credential, with a stable code and no value.

    The registry is the system of record for every session name, so the scan lives
    here and not only in the callers:  a title that arrives from anywhere is refused
    before it is stored, and the answer names the problem without repeating the
    secret or any part of it.
    """
    if title is None:
        return
    if secretscan.scan(str(title)):
        raise ValueError(SECRET_DETECTED)


def _scan_field(field: str, value: object) -> None:
    """Refuse a field that carries a credential, with a stable code and no value.

    The title has its own code, so every other field names itself:  the refusal says which
    field was refused, and never repeats the secret or any part of it.
    """
    if not isinstance(value, str) or not value:
        return
    if secretscan.scan(value):
        raise ValueError("%s_secret_detected" % field)


def _scan_fields(fields: Mapping[str, object]) -> None:
    """Scan every user-supplied field that is about to be written.

    The registry is the system of record for every session, so a credential in ANY of the
    fields is refused before it reaches SQLite:  the title keeps its own code and every other
    field names itself.
    """
    for field, value in fields.items():
        if field == "title":
            _scan_title(value if isinstance(value, str) else None)
        else:
            _scan_field(field, value)


def registry_title(label: str, title: str) -> str:
    """The topic stored on the row:  the letter prefix belongs in display_title only."""
    text = (title or "").strip()
    if not label or not text:
        return text
    prefix = f"{label}:"
    if text.startswith(prefix):
        return text[len(prefix) :].lstrip()
    return text


def cross_seat_ref(seat: str, letter: str, generation: int) -> str:
    """Cross-seat reference form.  Generation > 1 (a recycled letter) must be disambiguated."""
    base = f"{seat} {letter}"
    return f"{base} (gen {generation})" if generation > 1 else base


ENV_REGISTRY_PATH = "AGENT_SYNC_OPERATOR_REGISTRY"

#: The seat that may issue a letter.
#:
#: The owner ruled (Oct 21 2026) that no agent issues its own letter:  the
#: Operator does.  A seat that could call the register path would be choosing
#: its own handle, and the handle is the thing that decides who a reply
#: reaches.  The Operator is a service seat, so it is named here rather than
#: inferred, and the register path refuses any other issuer.
ISSUER_OPERATOR = "OPERATOR"


def registry_db_path(root: str, env: Mapping[str, str] | None = None) -> str:
    """Where the one shared registry lives.

    The default is under the state root, which is per host.  That is enough on one host
    and it is not enough for the fleet:  AFC-412 asks for one namespace and one allocator
    across the Mac and the server, and a per-host file would give each host its own
    letters.  The environment override is the seam:  the deployment points it at the
    shared store where the Operator runs, so every host reads and writes one file.
    """
    if env:
        override = env.get(ENV_REGISTRY_PATH)
        if override:
            return os.path.expanduser(override)
    return os.path.join(str(root), "operator", "registry.sqlite3")


def _session_cells(row: sqlite3.Row, label: str) -> list[str]:
    return [
        row["record_id"],
        label,
        row["seat"],
        row["platform"],
        row["verified_host"],
        row["native_session_id"],
        row["title"],
        row["project"],
        row["thread_link"],
        row["apple_note_link"],
        row["state"],
        row["wake_capability"],
        row["registered_at"],
        row["last_seen_at"],
        row["closed_at"] or "",
        row["source"],
    ]


class SessionRegistry:
    """The one writer of the registry database."""

    def __init__(self, db_path: str, *, read_only: bool = False):
        self.db_path = str(db_path)
        self.read_only = bool(read_only)
        parent = os.path.dirname(self.db_path)
        if parent and not self.read_only:
            os.makedirs(parent, exist_ok=True)
        if self.read_only:
            # Read-only by behaviour, not by mode.  A mode=ro handle cannot roll back a
            # hot journal left by a crashed writer, and SQLite then refuses every read
            # with SQLITE_READONLY_ROLLBACK, which would drop every letter for good.
            # A read-write handle can recover that journal, and it takes no write lock
            # for a plain SELECT, so the wake path never contends with the Operator's
            # registration writes.  This handle runs no DDL and inserts no migration row:
            # it never writes.
            #
            # mode=rw and not a plain connect:  the handle is read-write, and it still
            # never creates the file, so a lookup can never bring a registry into being.
            # The cost of the hot-journal recovery is that the wake path needs write
            # permission on the registry file, which the store the Operator owns gives
            # it anyway.
            #
            # The URI comes from as_uri() and not from quoting the path by hand:  a path
            # that begins with two slashes quotes to "file://host/path", which SQLite
            # reads as an authority and opens a different file.  as_uri() keeps the
            # leading slashes as part of the path.
            self._conn = sqlite3.connect(
                "%s?mode=rw" % pathlib.Path(os.path.abspath(self.db_path)).as_uri(),
                uri=True,
            )
            self._conn.row_factory = sqlite3.Row
            return
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(DDL)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _utcnow()),
            )

    def close_db(self) -> None:
        self._conn.close()

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """One write transaction that holds the SQLite write lock for its whole body.

        BEGIN IMMEDIATE takes the lock before the first SELECT, so the identity and status
        checks below cannot run in a window where a second writer has not yet committed:  a
        concurrent registration now resumes the committed record instead of racing to an
        unhandled IntegrityError on the identity UNIQUE constraint.  Callers mint their
        timestamp inside this block, after the lock, so created_at follows commit order and
        the outbox exports in the order the changes were committed.
        """
        with self._conn as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    # ---- reads -------------------------------------------------

    def _get(self, record_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise UnknownRecord(record_id)
        return row

    def _get_by_identity(
        self, platform: str, verified_host: str, profile_scope: str, native_session_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE platform = ? AND verified_host = ?"
            " AND profile_scope = ? AND native_session_id = ?",
            (platform, verified_host, profile_scope, native_session_id),
        ).fetchone()

    def _active_alias(self, conn: sqlite3.Connection, record_id: str) -> tuple[str, int]:
        row = conn.execute(
            "SELECT letter, generation FROM alias_assignments"
            " WHERE record_id = ? AND retired_at IS NULL"
            " ORDER BY assigned_at DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        return (row["letter"], row["generation"]) if row else ("", 0)

    def lookup(
        self,
        platform: str,
        verified_host: str,
        profile_scope: str,
        native_session_id: str,
    ) -> dict[str, Any] | None:
        """Read-only: the live label for one identity, or None when it is not registered.

        This never writes, never mints a letter and never opens a transaction, so it is
        safe to call on the wake path for every catalog row.  A missing registration is
        simply "no letter yet";  it is not an error and it is not a reason to allocate one.

        The platform is folded to its namespace on the way in, because the caller may
        name the adapter it discovered through ("cursor-cloud") while the registry keys
        on the platform ("cursor").  Both sides fold, so the join finds the record.
        """
        row = self._get_by_identity(
            normalize_platform(platform), verified_host, profile_scope, native_session_id
        )
        if row is None:
            return None
        letter, generation = self._active_alias(self._conn, row["record_id"])
        return {
            "record_id": row["record_id"],
            "label": letter,
            "generation": generation,
            "seat": row["seat"],
            "title": row["title"],
            "state": row["state"],
        }

    # ---- writes (each one transaction:  state + events + outbox) ----------------

    def _allocate(
        self,
        conn: sqlite3.Connection,
        platform: str,
        record_id: str,
        now: str,
        space: str = "single",
    ) -> tuple[str, int]:
        """Lowest letter with no active assignment, wrapping after Z to A (RECYCLE on).  A reused
        letter gets the next generation.  Raises LabelExhausted when the space is full.

        The space is chosen by the caller:  "single" for a main conversation, "double"
        for a subthread."""
        for letter in LETTER_SPACES.get(space, LETTERS):
            live = conn.execute(
                "SELECT 1 FROM alias_assignments"
                " WHERE platform = ? AND letter = ? AND retired_at IS NULL",
                (platform, letter),
            ).fetchone()
            if live:
                continue
            row = conn.execute(
                "SELECT MAX(generation) AS g FROM alias_assignments WHERE platform = ? AND letter = ?",
                (platform, letter),
            ).fetchone()
            generation = (row["g"] or 0) + 1
            conn.execute(
                "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (platform, letter, generation, record_id, now),
            )
            return letter, generation
        raise LabelExhausted(
            f"all {len(LETTER_SPACES.get(space, LETTERS))} letters are active on"
            f" platform {platform!r}"
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        label: str,
        event: str,
        actor: str,
        outcome: str,
        source: str,
        details: str,
        now: str,
    ) -> str:
        event_id = _ulid()
        conn.execute(
            "INSERT INTO events(event_id, recorded_at, session_record_id, label, event, actor,"
            " outcome, source, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, now, record_id, label, event, actor, outcome, source, details),
        )
        cells = [event_id, now, record_id, label, event, actor, outcome, source, details]
        conn.execute(
            "INSERT INTO export_outbox(outbox_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (_ulid(), "event_append", json.dumps({"cells": cells}), now),
        )
        return event_id

    def _enqueue_session_upsert(self, conn: sqlite3.Connection, record_id: str, now: str) -> None:
        row = conn.execute("SELECT * FROM sessions WHERE record_id = ?", (record_id,)).fetchone()
        letter, _ = self._active_alias(conn, record_id)
        conn.execute(
            "INSERT INTO export_outbox(outbox_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (_ulid(), "session_upsert", json.dumps({"cells": _session_cells(row, letter)}), now),
        )

    def resolve_or_register(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        """Resume an existing registration or create a new one, atomically with its label,
        events row and outbox rows.  Never rebinds identity on a seat change.

        The platform is folded to its namespace before anything is read or written, so
        the record is keyed on the platform ("cursor") and not on the adapter the caller
        discovered it through ("cursor-cloud").  One Cursor session under each adapter
        would otherwise be two records with the same letter.
        """
        required = IDENTITY_FIELDS + ("seat", "source")
        missing = [k for k in required if not identity.get(k)]
        if missing:
            raise ValueError(f"missing identity fields: {', '.join(missing)}")
        identity = dict(identity)
        identity["platform"] = normalize_platform(str(identity["platform"]))
        with self._write_txn() as conn:
            now = _utcnow()
            row = self._get_by_identity(*(identity[k] for k in IDENTITY_FIELDS))
            if row is not None:
                record_id = row["record_id"]
                letter, generation = self._active_alias(conn, record_id)
                if row["seat"] != identity["seat"]:
                    self._insert_event(
                        conn,
                        record_id,
                        letter,
                        "resolve",
                        identity["seat"],
                        "seat_mismatch",
                        identity["source"],
                        f"stored seat is {row['seat']}",
                        now,
                    )
                    return {
                        "record_id": record_id,
                        "label": letter,
                        "generation": generation,
                        "created": False,
                        "outcome": "seat_mismatch",
                    }
                outcome = "resumed_closed" if row["closed_at"] else "resumed"
                if not row["closed_at"]:
                    # The session is alive, so it was seen:  the sweep that ends a stale
                    # letter's use reads this column, and a session that is being resumed
                    # is by definition not stale.
                    self._conn.execute(
                        "UPDATE sessions SET last_seen_at = ? WHERE record_id = ?",
                        (now, record_id),
                    )
                self._insert_event(
                    conn,
                    record_id,
                    letter,
                    "resume",
                    identity["seat"],
                    outcome,
                    identity["source"],
                    "",
                    now,
                )
                return {
                    "record_id": record_id,
                    "label": letter,
                    "generation": generation,
                    "created": False,
                    "outcome": outcome,
                }
            record_id = _ulid()
            conn.execute(
                "INSERT INTO sessions(record_id, seat, platform, verified_host, profile_scope,"
                " native_session_id, title, project, thread_link, apple_note_link,"
                " wake_capability, registered_at, last_seen_at, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    identity["seat"],
                    identity["platform"],
                    identity["verified_host"],
                    identity["profile_scope"],
                    identity["native_session_id"],
                    identity.get("title", ""),
                    identity.get("project", ""),
                    identity.get("thread_link", ""),
                    identity.get("apple_note_link", ""),
                    identity.get("wake_capability", "unknown"),
                    now,
                    now,
                    identity["source"],
                ),
            )
            letter, generation = self._allocate(
                conn,
                identity["platform"],
                record_id,
                now,
                str(identity.get("letter_space") or "single"),
            )
            self._insert_event(
                conn,
                record_id,
                letter,
                "register",
                identity["seat"],
                "registered",
                identity["source"],
                "",
                now,
            )
            self._enqueue_session_upsert(conn, record_id, now)
            return {
                "record_id": record_id,
                "label": letter,
                "generation": generation,
                "created": True,
                "outcome": "registered",
            }

    def mark_seen(self, record_id: str, at: str | None = None) -> None:
        with self._write_txn() as conn:
            at = at or _utcnow()
            self._get(record_id)
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE record_id = ?", (at, record_id)
            )
            self._enqueue_session_upsert(conn, record_id, at)

    def update_attrs(self, record_id: str, /, **attrs: str) -> None:
        bad = [k for k in attrs if k not in MUTABLE_ATTRS]
        if bad:
            raise ValueError(f"not mutable: {', '.join(bad)}")
        if not attrs:
            return
        with self._write_txn() as conn:
            now = _utcnow()
            row = self._get(record_id)
            if row["closed_at"]:
                raise ValueError(
                    "session %s is closed; open a new registration instead" % record_id
                )
            if "state" in attrs:
                # The lifecycle value is not free-form:  it is derived from closed_at, which only
                # close() sets.  Accepting it here would let a closed row be marked active again,
                # or an open row claim 'closed' with no closed_at and a still-live alias.
                want_closed = attrs["state"] == "closed"
                if want_closed != bool(row["closed_at"]):
                    raise ValueError(
                        f"state 'closed' is set by close(), not update_attrs (record {record_id})"
                    )
            sets = ", ".join(f"{k} = ?" for k in attrs)
            conn.execute(
                f"UPDATE sessions SET {sets} WHERE record_id = ?", (*attrs.values(), record_id)
            )
            self._enqueue_session_upsert(conn, record_id, now)

    def close(
        self,
        record_id: str,
        at: str | None = None,
        actor: str = "registry",
        source: str = "registry",
        reason: str = "",
    ) -> bool:
        """Set closed_at once and retire the live alias.  Closing twice changes nothing.

        The reason says why the letter's use ended ("stale", "replaced"), and it goes
        into the event the close writes, so the letter's history says more than a
        timestamp.
        """
        with self._write_txn() as conn:
            now = at or _utcnow()
            row = self._get(record_id)
            if row["closed_at"]:
                return False
            letter, _ = self._active_alias(conn, record_id)
            conn.execute(
                "UPDATE sessions SET closed_at = ?, state = 'closed' WHERE record_id = ?",
                (now, record_id),
            )
            conn.execute(
                "UPDATE alias_assignments SET retired_at = ?"
                " WHERE record_id = ? AND retired_at IS NULL",
                (now, record_id),
            )
            self._insert_event(
                conn, record_id, letter, "close", actor, "closed", source, reason, now
            )
            self._enqueue_session_upsert(conn, record_id, now)
            return True

    def record_event(
        self, record_id: str, event: str, actor: str, outcome: str, source: str, details: str = ""
    ) -> str:
        with self._write_txn() as conn:
            now = _utcnow()
            self._get(record_id)
            letter, _ = self._active_alias(conn, record_id)
            return self._insert_event(
                conn, record_id, letter, event, actor, outcome, source, details, now
            )


# ---- the register path (AFC-412) ---------------------------------------------------


def normalize_platform(platform: str) -> str:
    """The platform namespace, folded.

    A platform's display form ("MiniMax") and its namespace ("minimax") name the same
    registry namespace, so the namespace is folded on the way in and on the way out.  A
    letter is only ever issued under the folded name, and the index still serves the
    lookup because the comparison stays on the column.

    The fold also maps an adapter name onto its platform:  the catalog discovers
    Cursor through "cursor-cloud" and "cursor-local", and both are the one Cursor
    namespace, so both draw from the same letters.  Without that, one Cursor session
    under each adapter would be issued the same letter twice, and both would read as
    "CURSOR A".
    """
    text = str(platform or "").strip().lower()
    return PLATFORM_NAMESPACES.get(text, text)


def register_session(
    db_path: str,
    *,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
    seat: str,
    source: str,
    title: str = "",
    project: str = "",
    thread_link: str = "",
    apple_note_link: str = "",
    wake_capability: str = "unknown",
    issuer: str = "",
    letter_space: str = "single",
) -> dict[str, Any]:
    """Register one session and hand back its letter.

    The identity is checked the same way the wake path checks it, so a session
    registered here is one the wake can find:  a native session id that would not
    survive the join's own check is refused here rather than stored and never matched.

    A seat never chooses its own letter.  The lowest free letter on the platform goes to
    the session, a recycled letter takes the next generation, and the title comes back in
    the display form the platform expects:  "H: Title".

    Only the Operator issues a letter (owner ruling, Oct 21 2026):  a seat that could
    call this would be choosing its own handle, and the handle is what decides who a
    reply reaches.  The issuer is named rather than defaulted, so a caller cannot
    issue a letter by omitting it, and any issuer other than the Operator is refused
    before anything is written.
    """
    if str(issuer or "").strip().upper() != ISSUER_OPERATOR:
        raise ValueError("only the Operator may issue a letter (issuer %r)" % (issuer or ""))
    _scan_fields(
        {
            "title": title,
            "project": project,
            "thread_link": thread_link,
            "apple_note_link": apple_note_link,
        }
    )
    native = SI.check(native_session_id, "the native session id")
    platform = normalize_platform(platform)
    for name, value in (
        ("platform", platform),
        ("verified_host", verified_host),
        ("profile_scope", profile_scope),
        ("seat", seat),
        ("source", source),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s is required" % name)
    registry = SessionRegistry(db_path)
    try:
        out = registry.resolve_or_register(
            {
                "platform": platform,
                "verified_host": verified_host,
                "profile_scope": profile_scope,
                "native_session_id": native,
                "seat": seat,
                "source": source,
                "title": title,
                "project": project,
                "thread_link": thread_link,
                "apple_note_link": apple_note_link,
                "wake_capability": wake_capability,
                "letter_space": letter_space,
            }
        )
    finally:
        registry.close_db()
    out["title_display"] = display_title(str(out.get("label") or ""), title)
    return out


def _session_row_for_update(
    conn: sqlite3.Connection,
    *,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
) -> sqlite3.Row:
    namespace = normalize_platform(platform)
    native = SI.check(native_session_id, "the native session id")
    row = conn.execute(
        "SELECT * FROM sessions WHERE platform = ? AND verified_host = ?"
        " AND profile_scope = ? AND native_session_id = ?"
        " ORDER BY registered_at DESC LIMIT 1",
        (namespace, verified_host, profile_scope, native),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"session {native} is not registered on {namespace} at {verified_host}/{profile_scope};"
            " register it first"
        )
    return row


def update_session(
    db_path: str,
    *,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
    title: str | None = None,
    project: str | None = None,
    thread_link: str | None = None,
    apple_note_link: str | None = None,
    wake_capability: str | None = None,
) -> dict[str, Any]:
    """Update the mutable fields of one registered session, found by its session id.

    The session id is the identity here:  the same id always finds the same row, so
    running this again with the same values changes nothing and reports "unchanged".
    It never mints a letter — only the Operator issues one, and this cannot.

    The title is the field that moves in practice:  a thread is named once the first
    message lands, and the registry has to carry the name the thread actually carries,
    not the placeholder it was registered under.

    A session id that is not registered raises ValueError:  a caller that wants a
    letter must register first, and a miss here is not a reason to invent a row.
    """
    for name, value in (
        ("verified_host", verified_host),
        ("profile_scope", profile_scope),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s is required" % name)
    fields = {
        "title": title,
        "project": project,
        "thread_link": thread_link,
        "apple_note_link": apple_note_link,
        "wake_capability": wake_capability,
    }
    wanted = {k: v for k, v in fields.items() if v is not None}
    if not wanted:
        raise ValueError("nothing to update: name at least one field")
    bad = [k for k in wanted if k not in MUTABLE_ATTRS]
    if bad:
        raise ValueError(f"not mutable: {', '.join(bad)}")
    _scan_fields(wanted)
    registry = SessionRegistry(db_path)
    namespace = normalize_platform(platform)
    try:
        with registry._write_txn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE platform = ? AND verified_host = ?"
                " AND profile_scope = ? AND native_session_id = ?"
                " ORDER BY registered_at DESC LIMIT 1",
                (
                    namespace,
                    verified_host,
                    profile_scope,
                    SI.check(native_session_id, "the native session id"),
                ),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "session %s is not registered on %s at %s/%s; register it first"
                    % (native_session_id, namespace, verified_host, profile_scope)
                )
            record_id = row["record_id"]
            if row["closed_at"]:
                raise ValueError(
                    "session %s is closed on %s; open a new registration instead"
                    % (native_session_id, namespace)
                )
            letter, generation = registry._active_alias(conn, record_id)
            if title is not None:
                wanted["title"] = registry_title(letter, title)
            changed = {k: v for k, v in wanted.items() if (row[k] or "") != v}
            if changed:
                sets = ", ".join(f"{k} = ?" for k in changed)
                conn.execute(
                    f"UPDATE sessions SET {sets} WHERE record_id = ?",
                    (*changed.values(), record_id),
                )
                now = _utcnow()
                registry._enqueue_session_upsert(conn, record_id, now)
                registry._insert_event(
                    conn,
                    record_id,
                    letter,
                    "update",
                    "registry",
                    "updated",
                    "registry",
                    ", ".join(sorted(changed)),
                    now,
                )
                outcome = "updated"
            else:
                outcome = "unchanged"
            at = _utcnow()
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE record_id = ?", (at, record_id)
            )
            registry._enqueue_session_upsert(conn, record_id, at)
            current = conn.execute(
                "SELECT * FROM sessions WHERE record_id = ?", (record_id,)
            ).fetchone()
    finally:
        registry.close_db()
    out = {
        "record_id": record_id,
        "label": letter,
        "generation": generation,
        "title": current["title"],
        "outcome": outcome,
        "changed": sorted(changed),
    }
    out["title_display"] = display_title(letter, str(current["title"] or ""))
    return out


# ---- the letter's life:  it ends, and the history says when ------------------------


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def touch_session(
    db_path: str,
    *,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
) -> dict[str, Any]:
    """Stamp last_seen_at for a live session without changing its fields."""
    for name, value in (
        ("verified_host", verified_host),
        ("profile_scope", profile_scope),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s is required" % name)
    registry = SessionRegistry(db_path)
    try:
        row = _session_row_for_update(
            registry._conn,
            platform=platform,
            verified_host=verified_host,
            profile_scope=profile_scope,
            native_session_id=native_session_id,
        )
        if row["closed_at"]:
            return {"record_id": row["record_id"], "outcome": "closed"}
        registry.mark_seen(row["record_id"])
        return {"record_id": row["record_id"], "outcome": "seen"}
    finally:
        registry.close_db()


def close_session(
    db_path: str,
    *,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
    reason: str = "",
    actor: str = "registry",
    source: str = "registry",
) -> dict[str, Any]:
    """End one session's letter:  close the row, retire the alias, keep the history.

    The letter's row stays in alias_assignments with its retired_at, so the platform
    keeps the history of every letter it has issued and the window each one was used
    in.  The reason ("stale", "replaced") goes into the close event.
    """
    for name, value in (
        ("verified_host", verified_host),
        ("profile_scope", profile_scope),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s is required" % name)
    registry = SessionRegistry(db_path)
    try:
        row = _session_row_for_update(
            registry._conn,
            platform=platform,
            verified_host=verified_host,
            profile_scope=profile_scope,
            native_session_id=native_session_id,
        )
        record_id = row["record_id"]
        letter, generation = registry._active_alias(registry._conn, record_id)
        closed = registry.close(record_id, actor=actor, source=source, reason=reason)
        current = registry._get(record_id)
    finally:
        registry.close_db()
    return {
        "record_id": record_id,
        "label": letter,
        "generation": generation,
        "closed": bool(closed),
        "outcome": "closed" if closed else "already_closed",
        "reason": reason,
        "closed_at": current["closed_at"],
    }


def sweep_stale(
    db_path: str,
    *,
    platform: str,
    seat: str,
    stale_after_minutes: int = 120,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """End the letters of sessions that are stale AND have been replaced.

    A letter is the seat's handle for replies, so it may only end when the session
    behind it can no longer answer.  Two things have to be true:  the session has not
    been seen for the stale window, and another session for the same seat has taken
    over since.  A stale session nobody replaced keeps its letter, because a reply to
    it is still this seat's business.

    The letters themselves stay in alias_assignments with their start and end times,
    so the history of the platform is complete after a sweep.
    """
    namespace = normalize_platform(platform)
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(minutes=max(1, int(stale_after_minutes)))
    registry = SessionRegistry(db_path)
    ended: list[dict[str, Any]] = []
    try:
        with registry._write_txn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE platform = ? AND seat = ? AND closed_at IS NULL"
                " ORDER BY registered_at ASC",
                (namespace, seat),
            ).fetchall()
            open_rows = list(rows)
            seen_at = {r["record_id"]: _parse_instant(r["last_seen_at"]) for r in open_rows}
            registered_at = {r["record_id"]: _parse_instant(r["registered_at"]) for r in open_rows}
            for row in open_rows:
                last_seen = seen_at.get(row["record_id"])
                if last_seen is None or last_seen >= cutoff:
                    continue
                replaced = False
                for other in open_rows:
                    if other["record_id"] == row["record_id"]:
                        continue
                    other_seen = seen_at.get(other["record_id"])
                    other_registered = registered_at.get(other["record_id"])
                    if (other_seen is None or other_seen > last_seen) and (
                        other_registered is None or other_registered > last_seen
                    ):
                        replaced = True
                        break
                if not replaced:
                    continue
                letter, generation = registry._active_alias(conn, row["record_id"])
                if row["closed_at"]:
                    continue
                now = _utcnow()
                conn.execute(
                    "UPDATE sessions SET closed_at = ?, state = 'closed' WHERE record_id = ?",
                    (now, row["record_id"]),
                )
                conn.execute(
                    "UPDATE alias_assignments SET retired_at = ?"
                    " WHERE record_id = ? AND retired_at IS NULL",
                    (now, row["record_id"]),
                )
                registry._insert_event(
                    conn,
                    row["record_id"],
                    letter,
                    "close",
                    "registry",
                    "closed",
                    "stale_sweep",
                    "stale and replaced by a newer session",
                    now,
                )
                registry._enqueue_session_upsert(conn, row["record_id"], now)
                ended.append(
                    {
                        "record_id": row["record_id"],
                        "label": letter,
                        "generation": generation,
                        "native_session_id": row["native_session_id"],
                        "title": row["title"],
                        "last_seen_at": row["last_seen_at"],
                        "reason": "stale and replaced by a newer session",
                    }
                )
    finally:
        registry.close_db()
    return ended


def letter_history(db_path: str, platform: str, limit: int = 50) -> list[dict[str, Any]]:
    """Every letter the platform has issued, with the window it was used in.

    A live letter has no end time;  a retired one carries the moment its use ended
    and why, so "MM B" can be read as a window rather than only as a handle.
    """
    namespace = normalize_platform(platform)
    if not os.path.isfile(str(db_path)):
        raise RegistryUnavailable("the registry %s is not there" % db_path)
    registry = SessionRegistry(str(db_path), read_only=True)
    try:
        rows = registry._conn.execute(
            "SELECT a.letter AS letter, a.generation AS generation, a.record_id AS record_id,"
            " a.assigned_at AS assigned_at, a.retired_at AS retired_at,"
            " s.native_session_id AS native_session_id, s.seat AS seat, s.title AS title,"
            " s.state AS state, s.verified_host AS verified_host, s.closed_at AS closed_at"
            " FROM alias_assignments a JOIN sessions s ON s.record_id = a.record_id"
            " WHERE a.platform = ? ORDER BY a.assigned_at DESC LIMIT ?",
            (namespace, max(1, int(limit))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        record_ids = [row["record_id"] for row in rows]
        close_by_id: dict[str, sqlite3.Row] = {}
        if record_ids:
            placeholders = ",".join("?" * len(record_ids))
            for event in registry._conn.execute(
                "SELECT session_record_id, recorded_at, details FROM events"
                " WHERE event = 'close' AND session_record_id IN (%s)"
                " ORDER BY recorded_at DESC" % placeholders,
                record_ids,
            ):
                rid = event["session_record_id"]
                if rid not in close_by_id:
                    close_by_id[rid] = event
        for row in rows:
            event = close_by_id.get(row["record_id"])
            item = dict(row)
            item["end_reason"] = (event["details"] if event else "") or ""
            item["ended_at"] = (event["recorded_at"] if event else None) or row["retired_at"]
            item["title_display"] = display_title(row["letter"], str(row["title"] or ""))
            item["live"] = row["retired_at"] is None
            out.append(item)
        return out
    finally:
        registry.close_db()


# ---- the other direction: a letter names a session (AFC-412) ------------------------


class RegistryUnavailable(RuntimeError):
    """The registry could not be read at all:  a hot journal, a locked file, a missing table.

    This is not a miss.  A miss is an identity with no registration;  this is a registry
    that could not be opened, and the caller has to say so rather than report every
    session as having no letter.
    """


def _check_generation(generation: int | None) -> None:
    if generation is None:
        return
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("generation must be a positive integer")


def resolve_label(
    db_path: str,
    platform: str,
    letter: str,
    generation: int | None = None,
) -> dict[str, Any] | None:
    """The session a letter names, or None when the letter is not live.

    This is the other direction of the join.  The catalog answers "what letter is this
    session", and the wake carries that answer on the row.  This answers "which session
    is this letter", which is what an address like "@**MiniMax** Z" needs.

    A letter is unique on its platform while it is live, so the letter alone names one
    session, and the platform is always part of the address.  A recycled letter takes a
    generation, and "MM A (gen 2)" names the second session to hold that letter.

    None means one thing only:  the registry was read and that letter has no live
    holder.  A registry that is missing, unreadable or broken raises
    RegistryUnavailable, and malformed input raises ValueError, so a caller can tell a
    miss from a failure to know.

    The lookup is on the indexed (platform, letter) pair, so it answers for a session
    the catalog no longer lists, which is the point:  the catalog shows a bounded window
    of recent sessions and this does not.
    """
    if not isinstance(platform, str) or not platform.strip():
        raise ValueError("platform is required")
    if not isinstance(letter, str) or not letter.strip():
        raise ValueError("letter is required")
    want = letter.strip().upper()
    if want not in ALL_LETTERS:
        raise ValueError("letter must be A-Z, or two letters for a subthread")
    _check_generation(generation)
    if not db_path:
        raise RegistryUnavailable("no registry path was given")
    if not os.path.isfile(str(db_path)):
        raise RegistryUnavailable("the registry %s is not there" % db_path)
    namespace = normalize_platform(platform)
    columns = (
        "a.letter AS label, a.generation AS generation, a.record_id AS record_id,"
        " s.seat AS seat, s.native_session_id AS native_session_id, s.title AS title,"
        " s.verified_host AS verified_host, s.profile_scope AS profile_scope,"
        " s.state AS state"
    )
    try:
        registry = SessionRegistry(str(db_path), read_only=True)
    except Exception as exc:
        raise RegistryUnavailable(type(exc).__name__) from exc
    try:
        if generation is None:
            row = registry._conn.execute(
                "SELECT %s FROM alias_assignments a JOIN sessions s ON s.record_id = a.record_id"
                " WHERE a.platform = ? AND a.letter = ? AND a.retired_at IS NULL" % columns,
                (namespace, want),
            ).fetchone()
        else:
            row = registry._conn.execute(
                "SELECT %s FROM alias_assignments a JOIN sessions s ON s.record_id = a.record_id"
                " WHERE a.platform = ? AND a.letter = ? AND a.generation = ?" % columns,
                (namespace, want, int(generation)),
            ).fetchone()
    except Exception as exc:
        raise RegistryUnavailable(type(exc).__name__) from exc
    finally:
        try:
            registry.close_db()
        except Exception:
            pass
    if row is None:
        return None
    found = dict(row)
    label = str(found.get("label") or "")
    found["ref"] = cross_seat_ref(
        str(found.get("seat") or ""),
        label,
        int(found.get("generation") or 0),
    )
    found["title_display"] = display_title(label, str(found.get("title") or ""))
    found["found"] = True
    return found


def registrations_for_native_id(
    db_path: str, platform: str, native_session_id: str
) -> list[dict[str, Any]]:
    """Every live registration of one native session id on a platform, across locations.

    The ask-before-letter flow (operator_registration) needs this direction:  a session id
    arrives without a location, and the answer is the set of locations that id is currently
    lettered at.  Only live letters count:  a session whose letter was retired reads as
    unlettered, so the flow asks again and the register path resumes its record with a fresh
    letter.  A closed session is not a live registration either - the same filter every
    other registry read (resolve_ref, the letter reads) applies.  An empty list means unlettered, one row is the short-circuit, and several rows
    are the contradiction the owner settles - the same id lettered on both the Mac and the
    server is never merged or picked between here.

    Read-only by behaviour, on the same handle discipline as resolve_label:  no DDL, no
    migration row, a hot journal recovered rather than reported as an empty registry.  A
    missing or broken registry raises RegistryUnavailable;  an empty list is a real answer.
    """
    if not isinstance(platform, str) or not platform.strip():
        raise ValueError("platform is required")
    native = SI.check(native_session_id, "the native session id")
    if not db_path:
        raise RegistryUnavailable("no registry path was given")
    if not os.path.isfile(str(db_path)):
        raise RegistryUnavailable("the registry %s is not there" % db_path)
    namespace = normalize_platform(platform)
    try:
        registry = SessionRegistry(str(db_path), read_only=True)
    except Exception as exc:
        raise RegistryUnavailable(
            "the registry %s is unavailable (%s)" % (db_path, type(exc).__name__)
        ) from exc
    try:
        rows = registry._conn.execute(
            "SELECT s.verified_host AS verified_host, s.seat AS seat, s.state AS state,"
            " a.letter AS label, a.generation AS generation"
            " FROM sessions s"
            " JOIN alias_assignments a"
            "   ON a.record_id = s.record_id AND a.platform = ? AND a.retired_at IS NULL"
            " WHERE s.platform = ? AND s.native_session_id = ? AND s.closed_at IS NULL"
            " ORDER BY s.registered_at",
            (namespace, namespace, native),
        ).fetchall()
    except Exception as exc:
        raise RegistryUnavailable(
            "the registry %s is unavailable (%s)" % (db_path, type(exc).__name__)
        ) from exc
    finally:
        try:
            registry.close_db()
        except Exception:
            pass
    return [dict(row) for row in rows]


def resolve_ref(db_path: str, platform: str, ref: str) -> dict[str, Any] | None:
    """The session a reference form names, or None when it names none.

    The reference form is what a reply carries:  "MM A", or "MM A (gen 2)" for a letter
    that has been recycled.  This is the inverse of the form the registry hands out, and
    the inverse is exact:  a bare form names generation 1 of the letter, and a suffix
    names that generation.  So "MM A" always names the same session, whether or not the
    letter has been recycled since.

    A bare form that resolved to the live holder would not be an inverse.  After a
    recycle it would name a different session than the one it was handed out for, so a
    reply to an old message would reach the wrong session.  The live holder is what
    `resolve_label` answers, and the two questions are different on purpose.

    The seat in the form is checked against the session it names.  A ref is
    "SEAT LETTER", so "CODEX A" is a claim about Codex holding A;  without the check it
    resolved to whichever session held A on the platform, and the seat in the form was
    decoration.  A ref whose seat does not hold the letter names no session, and a miss
    is None rather than an error.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("the reference form is required")
    text = ref.strip()
    generation = 1
    match = re.search(r"\s*\(gen\s+(\d+)\)\s*$", text)
    if match:
        generation = int(match.group(1))
        text = text[: match.start()].strip()
    parts = text.split()
    if len(parts) < 2:
        raise ValueError("the reference form is SEAT LETTER or SEAT LETTER (gen N)")
    seat = parts[0].strip().upper()
    letter = parts[-1].strip().upper()
    found = resolve_label(db_path, platform, letter, generation)
    if found is None:
        return None
    if str(found.get("seat") or "").strip().upper() != seat:
        # The letter is held, but by another seat:  this ref names no session.
        return None
    return found


def candidates_for(
    db_path: str,
    platform: str,
    letter: str,
) -> list[dict[str, Any]]:
    """The live holders of one letter on one platform.

    This is the query a candidate list needs:  the sessions that could hold the letter
    the sender used.  It is read-only and it never allocates, so a letter nobody holds
    simply has no candidates rather than a new one.

    `retired_at IS NULL` is cached lease state, not proof of life, so a session that has
    actually been closed (closed_at set) is not a candidate even if its alias was never
    retired.  A closed session can never hold a letter again.
    """
    want = str(letter or "").strip().upper()
    if not want or want not in ALL_LETTERS:
        return []
    if not db_path or not os.path.isfile(str(db_path)):
        return []
    try:
        registry = SessionRegistry(str(db_path), read_only=True)
    except Exception:
        return []
    try:
        rows = registry._conn.execute(
            "SELECT a.letter AS label, a.generation AS generation, a.record_id AS record_id,"
            " s.seat AS seat, s.native_session_id AS native_session_id, s.title AS title,"
            " s.verified_host AS verified_host, s.profile_scope AS profile_scope,"
            " s.state AS state"
            " FROM alias_assignments a JOIN sessions s ON s.record_id = a.record_id"
            " WHERE a.platform = ? AND a.letter = ? AND a.retired_at IS NULL"
            " AND s.closed_at IS NULL"
            " ORDER BY s.registered_at DESC, a.record_id ASC",
            (normalize_platform(platform), want),
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            registry.close_db()
        except Exception:
            pass
    out = []
    for row in rows:
        found = dict(row)
        label = str(found.get("label") or "")
        generation = int(found.get("generation") or 0)
        seat = str(found.get("seat") or "")
        out.append(
            {
                "label": label,
                "generation": generation,
                "ref": cross_seat_ref(seat, label, generation),
                "record_id": found.get("record_id"),
                "seat": seat,
                "native_session_id": str(found.get("native_session_id") or ""),
                "title": str(found.get("title") or ""),
                "platform": normalize_platform(platform),
                "location": str(found.get("verified_host") or ""),
                "scope": str(found.get("profile_scope") or ""),
            }
        )
    return out


def candidates_for_letter(db_path: str, letter: str) -> list[dict[str, Any]]:
    """The live holders of one letter on every platform.

    A letter used without a platform is the ambiguous case:  the same letter can be
    live on more than one platform, because the namespace is per platform.  This is the
    query that finds them.
    """
    want = str(letter or "").strip().upper()
    if not want or want not in ALL_LETTERS or not db_path or not os.path.isfile(str(db_path)):
        return []
    try:
        registry = SessionRegistry(str(db_path), read_only=True)
    except Exception:
        return []
    try:
        rows = registry._conn.execute(
            "SELECT a.platform AS platform, a.letter AS label, a.generation AS generation,"
            " a.record_id AS record_id, s.seat AS seat, s.native_session_id AS native_session_id,"
            " s.title AS title, s.verified_host AS verified_host, s.profile_scope AS profile_scope"
            " FROM alias_assignments a JOIN sessions s ON s.record_id = a.record_id"
            " WHERE a.letter = ? AND a.retired_at IS NULL AND s.closed_at IS NULL"
            " ORDER BY a.platform ASC, s.registered_at DESC, a.record_id ASC",
            (want,),
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            registry.close_db()
        except Exception:
            pass
    out = []
    for row in rows:
        found = dict(row)
        label = str(found.get("label") or "")
        generation = int(found.get("generation") or 0)
        seat = str(found.get("seat") or "")
        platform = normalize_platform(str(found.get("platform") or ""))
        out.append(
            {
                "label": label,
                "generation": generation,
                "ref": cross_seat_ref(seat, label, generation),
                "record_id": found.get("record_id"),
                "seat": seat,
                "native_session_id": str(found.get("native_session_id") or ""),
                "title": str(found.get("title") or ""),
                "platform": platform,
                "location": str(found.get("verified_host") or ""),
                "scope": str(found.get("profile_scope") or ""),
            }
        )
    return out


# ---- read-only lookup for the wake path (AFC-412 wake wiring) ------------------------


def lookup_many(
    db_path: str,
    identities: list[tuple[str, str, str, str]],
    failures: list[tuple[str, str, str, str]] | None = None,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Read-only lookups for many identities, over one connection.

    The wake path asks about every catalog row it shows, so one connection per row would
    be one connection per row per wake.  One connection, one pass, many answers.

    Side-effect free on purpose:  a missing database is not created and no letter is
    allocated, because a catalog scan must never mint registrations as a side effect of
    reading.  A registry that cannot be read at all raises RegistryUnavailable, so the
    caller can report that instead of silently showing every session as unlettered.

    A partial failure is not a miss:  one unreadable session losing its letter is not the
    same as that session never having one, so the identities that failed are collected
    for the caller to report.

    The reference form carries the generation when a letter has been recycled, so
    "MM A" and "MM A (gen 2)" never read as the same session.
    """
    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if not db_path or not os.path.isfile(str(db_path)) or not identities:
        return result
    try:
        registry = SessionRegistry(str(db_path), read_only=True)
    except Exception as exc:
        raise RegistryUnavailable(type(exc).__name__) from exc
    failed: list[tuple[str, str, str, str]] = []
    try:
        for identity in identities:
            try:
                found = registry.lookup(*identity)
            except Exception:
                failed.append(identity)
                continue
            if not found or not found.get("label"):
                continue
            letter = str(found["label"])
            generation = int(found.get("generation") or 0)
            seat = str(found.get("seat") or "")
            result[identity] = {
                "record_id": found.get("record_id"),
                "label": letter,
                "generation": generation,
                "seat": seat,
                "ref": cross_seat_ref(seat, letter, generation),
            }
    finally:
        try:
            registry.close_db()
        except Exception:
            pass
    if failures is not None:
        failures.extend(failed)
    if failed and len(failed) == len(identities):
        # Every read failed, so the registry is unreadable rather than empty.
        raise RegistryUnavailable("every lookup failed")
    return result


def lookup_entry(
    db_path: str,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
) -> dict[str, Any]:
    """One read-only registry lookup: the letter, its generation and its reference form."""
    identity = (platform, verified_host, profile_scope, native_session_id)
    try:
        return lookup_many(db_path, [identity]).get(identity, {})
    except RegistryUnavailable:
        return {}


def lookup_label(
    db_path: str,
    platform: str,
    verified_host: str,
    profile_scope: str,
    native_session_id: str,
) -> str:
    """The live letter for one identity, or "" when it is not registered."""
    return str(
        lookup_entry(db_path, platform, verified_host, profile_scope, native_session_id).get(
            "label"
        )
        or ""
    )


# ---- outbox bridge for the async exporter (AFC-412 PR2) -------------------------


def pending_outbox(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # rowid is the commit order (one writer, appended under the write lock), and it is the only
        # correct key here:  created_at can be caller-supplied (close(at=...), mark_seen(at=...)),
        # so a transaction with a historical timestamp would export before the state it supersedes
        # and leave the sheet stale.  outbox_id cannot either:  it is a ULID whose random suffix
        # does not sort with the event it accompanies.
        rows = conn.execute(
            "SELECT outbox_id, kind, payload, created_at FROM export_outbox"
            " WHERE exported_at IS NULL ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_exported(db_path: str, outbox_ids: list[str], exported_at: str | None = None) -> int:
    """Mark outbox rows exported only after the Sheets write is confirmed.  Re-running after a
    crash simply re-exports unmarked rows;  the exporter reconciles by id so that is safe."""
    if not outbox_ids:
        return 0
    at = exported_at or _utcnow()
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            cur = conn.executemany(
                "UPDATE export_outbox SET exported_at = ? WHERE outbox_id = ?",
                [(at, i) for i in outbox_ids],
            )
        return cur.rowcount
    finally:
        conn.close()


def _default_db() -> str:
    """The shared registry location, from the same environment the listener reads."""
    root = os.environ.get("AGENT_SYNC_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".agent-sync"
    )
    return registry_db_path(root, os.environ)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator-registry")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pending-outbox")
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=50)
    m = sub.add_parser("mark-exported")
    m.add_argument("--db", required=True)
    m.add_argument("outbox_ids", nargs="+")
    r = sub.add_parser(
        "register",
        help="register one session and print its letter, generation and title form",
    )
    r.add_argument("--db", default="", help="the registry file (default: the shared path)")
    r.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    r.add_argument(
        "--location",
        required=True,
        help="the fleet location (mac, server, cloud), never an OS hostname",
    )
    r.add_argument("--scope", required=True, help="the profile scope")
    r.add_argument("--session-id", required=True, help="the native session id")
    r.add_argument("--seat", required=True)
    r.add_argument("--source", required=True, help="what asked for the registration")
    r.add_argument("--title", default="")
    r.add_argument("--project", default="")
    r.add_argument("--thread-link", default="")
    r.add_argument("--apple-note-link", default="")
    r.add_argument("--wake-capability", default="unknown")
    r.add_argument(
        "--letter-space",
        default="single",
        choices=sorted(LETTER_SPACES),
        help="single for a main conversation, double for a subthread",
    )
    rs = sub.add_parser("resolve", help="name the session a letter points at")
    rs.add_argument("--db", default="", help="the registry file (default: the shared path)")
    rs.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    rs.add_argument("--letter", required=True, help="the letter, such as A")
    rs.add_argument(
        "--generation", type=int, default=None, help="the generation of a recycled letter"
    )
    u = sub.add_parser(
        "update",
        help="update the mutable fields of one registered session, found by its session id",
    )
    u.add_argument("--db", default="", help="the registry file (default: the shared path)")
    u.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    u.add_argument(
        "--location",
        required=True,
        help="the fleet location (mac, server, cloud), never an OS hostname",
    )
    u.add_argument("--scope", required=True, help="the profile scope")
    u.add_argument("--session-id", required=True, help="the native session id")
    u.add_argument("--title", default=None)
    u.add_argument("--project", default=None)
    u.add_argument("--thread-link", default=None)
    u.add_argument("--apple-note-link", default=None)
    u.add_argument("--wake-capability", default=None)
    c = sub.add_parser("close", help="end one session's letter and keep its history")
    c.add_argument("--db", default="", help="the registry file (default: the shared path)")
    c.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    c.add_argument(
        "--location",
        required=True,
        help="the fleet location (mac, server, cloud), never an OS hostname",
    )
    c.add_argument("--scope", required=True, help="the profile scope")
    c.add_argument("--session-id", required=True, help="the native session id")
    c.add_argument("--reason", default="", help="why the letter's use ended")
    c.add_argument("--actor", default="registry")
    c.add_argument("--source", default="registry")
    w = sub.add_parser(
        "sweep", help="end the letters of sessions that are stale and have been replaced"
    )
    w.add_argument("--db", default="", help="the registry file (default: the shared path)")
    w.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    w.add_argument("--seat", required=True)
    w.add_argument("--stale-after-minutes", type=int, default=120)
    h = sub.add_parser("history", help="every letter the platform has issued, with its window")
    h.add_argument("--db", default="", help="the registry file (default: the shared path)")
    h.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    h.add_argument("--limit", type=int, default=50)
    t = sub.add_parser("touch", help="stamp last_seen_at for one live session")
    t.add_argument("--db", default="", help="the registry file (default: the shared path)")
    t.add_argument("--platform", required=True, help="the platform namespace, such as minimax")
    t.add_argument(
        "--location",
        required=True,
        help="the fleet location (mac, server, cloud), never an OS hostname",
    )
    t.add_argument("--scope", required=True, help="the profile scope")
    t.add_argument("--session-id", required=True, help="the native session id")
    args = parser.parse_args(argv)
    if args.command == "pending-outbox":
        print(json.dumps(pending_outbox(args.db, args.limit)))
        return 0
    if args.command == "resolve":
        try:
            found = resolve_label(
                args.db or _default_db(),
                args.platform,
                args.letter,
                args.generation,
            )
        except (ValueError, RegistryUnavailable) as exc:
            print(json.dumps({"error": str(exc), "found": False}))
            return 2
        print(json.dumps(found if found else {"label": args.letter, "found": False}))
        return 0
    if args.command == "register":
        try:
            out = register_session(
                args.db or _default_db(),
                platform=args.platform,
                verified_host=args.location,
                profile_scope=args.scope,
                native_session_id=args.session_id,
                seat=args.seat,
                source=args.source,
                title=args.title,
                project=args.project,
                thread_link=args.thread_link,
                apple_note_link=args.apple_note_link,
                wake_capability=args.wake_capability,
                issuer=ISSUER_OPERATOR,
                letter_space=args.letter_space,
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(out))
        return 0
    if args.command == "update":
        try:
            out = update_session(
                args.db or _default_db(),
                platform=args.platform,
                verified_host=args.location,
                profile_scope=args.scope,
                native_session_id=args.session_id,
                title=args.title,
                project=args.project,
                thread_link=args.thread_link,
                apple_note_link=args.apple_note_link,
                wake_capability=args.wake_capability,
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(out))
        return 0
    if args.command == "close":
        try:
            out = close_session(
                args.db or _default_db(),
                platform=args.platform,
                verified_host=args.location,
                profile_scope=args.scope,
                native_session_id=args.session_id,
                reason=args.reason,
                actor=args.actor,
                source=args.source,
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(out))
        return 0
    if args.command == "touch":
        try:
            out = touch_session(
                args.db or _default_db(),
                platform=args.platform,
                verified_host=args.location,
                profile_scope=args.scope,
                native_session_id=args.session_id,
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(out))
        return 0
    if args.command == "sweep":
        ended = sweep_stale(
            args.db or _default_db(),
            platform=args.platform,
            seat=args.seat,
            stale_after_minutes=args.stale_after_minutes,
        )
        print(json.dumps({"ended": ended, "count": len(ended)}))
        return 0
    if args.command == "history":
        try:
            rows = letter_history(args.db or _default_db(), args.platform, args.limit)
        except (ValueError, RegistryUnavailable) as exc:
            print(json.dumps({"error": str(exc)}))
            return 2
        print(json.dumps(rows))
        return 0
    print(json.dumps({"marked": mark_exported(args.db, args.outbox_ids)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
