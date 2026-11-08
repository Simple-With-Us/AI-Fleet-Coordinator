"""Tests for reduced_status:  the listener's status.json cut down to names, counts, tokens and times.

The main fixture is built from what `Daemon.status()` emits (daemon.py), one test reduces a real
`Daemon.status()` document written by a real daemon, and the hostile tests put paths, emails, ids,
tokens, huge strings and wrong types into every position of the fixture:  none of them may reach
the output, the output must stay bounded, and a bad value must degrade to null.

  cd scripts && python3 -m unittest agent_sync.tests.test_reduced_status
"""

from __future__ import annotations

import copy
import datetime
import importlib.util
import json
import os
import random
import re
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import daemon as D
from agent_sync import live as L
from agent_sync.tests.test_daemon import DaemonHarness

SCRIPTS = Path(__file__).resolve().parents[2]


def load_reducer():
    """scripts/reduced_status.py, by file path.  It sits beside fleet-ops-control.py and not in the
    agent_sync package, which the fleet CLI release ships under a file budget, so it is not
    importable as `agent_sync.reduced_status`.  The module is not put in sys.modules."""
    spec = importlib.util.spec_from_file_location(
        "reduced_status_under_test", SCRIPTS / "reduced_status.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RS = load_reducer()

NOW = 1_800_000_000.0

# Made up and assembled here so no token-shaped literal sits in this file.
PATH = "/" + "/".join(["home", "owner", ".config", "fleet", "secret-file"])
WINDOWS = "C:" + "\\" + "\\".join(["Users", "owner", "secret.txt"])
TILDE = "~/" + ".ssh/" + "id_key"
EMAIL = "owner" + "@" + "example.invalid"
URL = "https://" + "example.invalid/path?x=1"
WWW = "www." + "example.invalid"
UUID = "-".join(["0123abcd", "4567", "89ab", "cdef", "0123456789ab"])
USER_ID = "918273645"
KEY = "ghp_" + "a1B2c3D4" * 4
KEY2 = "sk-" + "A1b2C3d4E5f6G7"
KEY_CHUNKS = ("aB3dE6g", "H9jK2mN", "5pQ8sT1", "vW4yZ7b", "C0eF")
VALUE = "".join(KEY_CHUNKS)  # 32 characters, shaped like a Zulip API key
HUGE = "x" * 100_000
CONTROL = "ok\x00\x1b[31m‮​\ud800go"

# Strings that must never be in the output, wherever they were put in the input.
PROBES = (
    PATH, WINDOWS, TILDE, EMAIL, URL, WWW, UUID, USER_ID, KEY, KEY2, VALUE, HUGE[:50],
    "Letters Work",
)  # fmt: skip
HOSTILE_STRINGS = (
    PATH,
    WINDOWS,
    TILDE,
    EMAIL,
    URL,
    WWW,
    UUID,
    USER_ID,
    KEY,
    KEY2,
    VALUE,
    HUGE,
    CONTROL,
    "",
    " ",
    "\n",
    "A" * 40,
    "a/b",
    "..",
    "-x",
    "‮evil",
    "ＡＢ",
)
HOSTILE_OTHER = (
    None,
    True,
    False,
    0,
    -1,
    1,
    2**70,
    10**400,
    1e308,
    -1e308,
    float("nan"),
    float("inf"),
    -float("inf"),
    1e12,
    [],
    {},
    [1],
    {"a": 1},
    [[]],
    (),
    b"bytes",
)
HOSTILE = HOSTILE_STRINGS + HOSTILE_OTHER

ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,23}")
SEAT = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")
CODE = re.compile(r"[a-z][a-z0-9_]{0,47}")
UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ud800-\udfff\ue000-\uf8ff]"
)

TOP = {
    "schema",
    "generated_at",
    "instance",
    "status_updated",
    "operator_voice",
    "pause",
    "pause_file_error",
    "seats",
    "skipped",
    "disabled",
}
VOICE = {"available", "code", "since"}
PAUSE = {"paused", "since", "wakes_only", "set_by"}
SEAT_KEYS = {
    "seat",
    "bot",
    "connected",
    "running",
    "wake",
    "paused",
    "wakes_24h",
    "held_back",
    "jobs",
    "last_event",
    "quota",
    "leases",
    "letters",
}
QUOTA = {
    "known",
    "exhausted",
    "remaining",
    "reset_at",
    "used",
    "cap_day",
    "cap_hour",
    "source",
    "reason",
}
LEASE = {
    "letter",
    "platform",
    "alive",
    "pending",
    "verified_host",
    "why",
    "topics",
    "watcher",
}
WAKES = {"inbox", "muse", "http", "claude", "openrouter", "mcode", "fallback", "other"}


def iso(epoch: float) -> str:
    moment = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_shape(case: unittest.TestCase, doc: Any) -> None:
    """The reduced_status v1 contract:  exact key sets, types, character sets and bounds."""

    def optional(value: Any, check: Any) -> None:
        if value is not None:
            check(value)

    def is_iso(value: Any) -> None:
        case.assertIsInstance(value, str)
        case.assertRegex(value, ISO)

    def is_bool(value: Any) -> None:
        case.assertIs(type(value), bool)

    def is_count(value: Any) -> None:
        case.assertIs(type(value), int)
        case.assertTrue(0 <= value <= RS.COUNT_MAX)

    def is_token(value: Any) -> None:
        case.assertIsInstance(value, str)
        case.assertLessEqual(len(value), RS.TOKEN_MAX)
        case.assertTrue(TOKEN.fullmatch(value), repr(value))
        case.assertTrue(any(c.isalpha() for c in value), repr(value))

    case.assertIsInstance(doc, dict)
    case.assertEqual(set(doc), TOP)
    case.assertEqual(doc["schema"], 1)
    is_iso(doc["generated_at"])
    case.assertIn(doc["instance"], ("server", "mac", None))
    optional(doc["status_updated"], is_iso)
    case.assertEqual(set(doc["operator_voice"]), VOICE)
    voice = doc["operator_voice"]
    optional(voice["available"], is_bool)
    optional(voice["code"], lambda v: case.assertTrue(CODE.fullmatch(v), repr(v)))
    optional(voice["since"], is_iso)
    case.assertEqual(set(doc["pause"]), PAUSE)
    optional(doc["pause"]["paused"], is_bool)
    optional(doc["pause"]["since"], is_iso)
    optional(doc["pause"]["wakes_only"], is_bool)
    optional(doc["pause"]["set_by"], is_token)
    case.assertIn(doc["pause_file_error"], ("invalid", None))
    case.assertLessEqual(len(doc["seats"]), RS.MAX_SEATS)
    names = [seat["seat"] for seat in doc["seats"]]
    case.assertEqual(names, sorted(set(names)))
    for seat in doc["seats"]:
        case.assertEqual(set(seat), SEAT_KEYS)
        case.assertTrue(SEAT.fullmatch(seat["seat"]))
        optional(seat["bot"], is_token)
        optional(seat["connected"], is_bool)
        case.assertIn(seat["running"], ("running", "stopped", "unknown"))
        case.assertIn(seat["wake"], WAKES)
        optional(seat["paused"], is_bool)
        for field in ("wakes_24h", "held_back", "jobs"):
            optional(seat[field], is_count)
        optional(seat["last_event"], is_iso)
        quota = seat["quota"]
        case.assertEqual(set(quota), QUOTA)
        is_bool(quota["known"])
        optional(quota["exhausted"], is_bool)
        optional(quota["remaining"], is_count)
        if quota["remaining"] is not None:
            case.assertLessEqual(quota["remaining"], 100)
        optional(quota["reset_at"], is_iso)
        case.assertEqual((quota["used"], quota["cap_day"], quota["cap_hour"]), (None,) * 3)
        optional(quota["source"], is_token)
        optional(quota["reason"], is_token)
        if not quota["known"]:
            case.assertEqual(
                (quota["exhausted"], quota["remaining"], quota["reset_at"], quota["reason"]),
                (None,) * 4,
            )
        case.assertLessEqual(len(seat["leases"]), RS.MAX_LEASES)
        for lease in seat["leases"]:
            case.assertEqual(set(lease), LEASE)
            optional(lease["letter"], lambda v: case.assertRegex(v, r"[A-Z]{1,2}"))
            optional(lease["platform"], is_token)
            optional(lease["alive"], is_bool)
            optional(lease["pending"], is_count)
            case.assertIn(lease["verified_host"], ("mac", "server", "cloud", None))
            optional(lease["why"], is_token)
            optional(lease["topics"], is_count)
            optional(lease["watcher"], is_bool)
        case.assertEqual(set(seat["letters"]), {"known", "why"})
        is_bool(seat["letters"]["known"])
        case.assertIn(seat["letters"]["why"], ("partial", None))
    case.assertLessEqual(len(doc["skipped"]), RS.MAX_SKIPPED)
    for row in doc["skipped"]:
        case.assertEqual(set(row), {"seat", "reason"})
        case.assertTrue(SEAT.fullmatch(row["seat"]))
        is_token(row["reason"])
    case.assertLessEqual(len(doc["disabled"]), RS.MAX_NAMES)
    for name in doc["disabled"]:
        case.assertTrue(SEAT.fullmatch(name))
    text = json.dumps(doc, ensure_ascii=False)
    case.assertLess(len(text), 512 * 1024)
    # No string value in the document holds a control, format, surrogate or private character
    # (every key is a literal of the schema, which the exact key sets above pin down), and no
    # probe string is in it.
    for string in strings(doc):
        if UNSAFE.search(string):
            case.fail("unsafe character in " + repr(string)[:80])
    for probe in PROBES:
        case.assertNotIn(probe, text)


def strings(node: Any):
    """Every string VALUE inside `node` (the output's keys are literals of the schema)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def paths(node: Any, prefix: tuple = ()):
    """Every position inside `node`, containers and leaves alike, but not the root."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield prefix + (key,)
            yield from paths(value, prefix + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield prefix + (index,)
            yield from paths(value, prefix + (index,))


def replace_at(document: Any, path: tuple, value: Any) -> Any:
    copied = copy.deepcopy(document)
    target = copied
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return copied


def rename_key(document: Any, path: tuple, new_key: str) -> Any:
    copied = copy.deepcopy(document)
    target = copied
    for step in path[:-1]:
        target = target[step]
    target[new_key] = target.pop(path[-1])
    return copied


def lease_row(**changes: Any) -> dict[str, Any]:
    """A lease row as `Daemon.status()` writes it."""
    row = {
        "lease_id": UUID,
        "platform": "claude-code",
        "alive": True,
        "why": "ok",
        "topics": 2,
        "watcher": True,
        "pending": 1,
        "session_id": UUID,
        "letter": "AA",
        "title": "Letters Work",
        "verified_host": "server",
    }
    row.update(changes)
    return row


def seat_row(**changes: Any) -> dict[str, Any]:
    """A seat as `Daemon.status()` writes it (every key it emits, with ordinary values)."""
    row = {
        "bot": "MA",
        "creds": "env",
        "connected": True,
        "cursor": 424242,
        "last_event": NOW - 30,
        "wake": "inbox",
        "routine": None,
        "muse_bridge": False,
        "pinned": None,
        "pin": None,
        "claude": None,
        "muse": None,
        "model": None,
        "fallback": None,
        "provider": None,
        "quota": None,
        "wakes_24h": 3,
        "routes_24h": 0,
        "route_enabled": True,
        "budget": {"wakes_per_day": 40, "wakes_per_hour": 6, "usd_per_day": 2.0},
        "live": {"per_day": 30, "per_hour": 6},
        "jobs": 1,
        "running": None,
        "paused": False,
        "wakes_paused": False,
        "held_back": {
            "count": 2,
            "evicted": 0,
            "oldest_age": 60,
            "overflow": 0,
            "reasons": {"wakes_per_hour": 2},
            "totals": {"held": 2},
        },
        "leases": [],
        "red": [],
        "chat_nudges": {"pending": 0},
    }
    row.update(changes)
    return row


def status_document(**changes: Any) -> dict[str, Any]:
    """A listener status.json as the server's eavesdropper writes it, a few seats of each kind."""
    quota = {
        "known": True,
        "exhausted": True,
        "remaining": 0.4,
        "reset_at": "2026-11-09T05:00:00Z",
        "reason": "weekly cap reached until Monday",
        "source": "codecaps",
    }
    seats = {
        "MA": seat_row(),
        "JET": seat_row(
            bot="JET",
            wake="http",
            wakes_24h=7,
            held_back={"count": 85, "reasons": {"wakes_per_day": 85}},
            routine={"ready": True, "why": None, "host": "hosted.invalid", "method": "POST"},
        ),
        "CLAUDE": seat_row(
            bot="Claude",
            wake="claude",
            provider="claude",
            quota=quota,
            claude=PATH,
            pin={"why": "pin_stale", "claude": PATH, "pinned_claude": PATH},
            red=["wake pin is stale: " + PATH],
            leases=[
                lease_row(lease_id="L1", session_id="sess-1", letter="AA"),
                lease_row(
                    lease_id="L2",
                    session_id="sess-2",
                    letter=None,
                    title=None,
                    verified_host=None,
                    platform="cursor",
                    alive=False,
                    why="pid gone",
                    watcher=False,
                ),
            ],
        ),
        "MM": seat_row(
            bot="MiniMax",
            wake="muse",
            provider="muse",
            quota={"known": False, "exhausted": False, "source": "unknown", "reason": None},
            fallback={"runtime": "openrouter", "ready": True, "stands_in": True},
        ),
        "HALTED": seat_row(paused=True, wakes_paused=True),
        "QUIET": seat_row(paused=False, wakes_paused=True),
        "DOWN": seat_row(connected=False, last_event=None, red=["down for 31 minutes"]),
    }
    document = {
        "pid": 7,
        "version": "1.2.3",
        "started": NOW - 5000,
        "updated": NOW - 2,
        "instance": "server",
        "disabled": ["GROK-WEB", "BF-ORACLE"],
        "muse_bridge": {"enabled": False, "remote_seats": [], "pin": "inactive"},
        "skipped_seats": {
            "BAD": ["seat.BAD.creds must be one of file, infisical, env", "seat.BAD.wake x"]
        },
        "config_errors": ["seat.BAD.creds must be one of file, infisical, env " + PATH],
        "reload_refused": [PATH],
        "reload_at": NOW - 100,
        "partition_file": PATH,
        "owner_user_id": int(USER_ID),
        "chat_nudges": {"MA": {"pending": 0}},
        "wake_loop_prevention": True,
        "loop_guard": [{"channel": "ch", "topic": "topic text", "count": 3}],
        "rewake_verified": True,
        "pause": {},
        "operator_voice": {
            "available": False,
            "code": "operator_credential_rejected",
            "since": iso(NOW - 3600),
        },
        "seats": seats,
    }
    document.update(changes)
    return document


def seats_by_name(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {seat["seat"]: seat for seat in doc["seats"]}


class StatusDocumentTests(unittest.TestCase):
    def reduce(self, document: Any = None, now: Any = NOW, instance: Any = "server") -> dict:
        out = RS.reduce_status(status_document() if document is None else document, now, instance)
        check_shape(self, out)
        return out

    def test_the_whole_document_has_the_schema_and_only_the_schema(self) -> None:
        out = self.reduce()
        self.assertEqual(out["generated_at"], iso(NOW))
        self.assertEqual(out["instance"], "server")
        self.assertEqual(out["status_updated"], iso(NOW - 2))
        self.assertEqual(
            out["operator_voice"],
            {
                "available": False,
                "code": "operator_credential_rejected",
                "since": iso(NOW - 3600),
            },
        )
        self.assertEqual(
            [seat["seat"] for seat in out["seats"]],
            ["CLAUDE", "DOWN", "HALTED", "JET", "MA", "MM", "QUIET"],
        )
        self.assertEqual(out["disabled"], ["BF-ORACLE", "GROK-WEB"])
        self.assertEqual(out["skipped"], [{"seat": "BAD", "reason": "invalid_credentials"}])
        self.assertEqual(out["pause"], {"paused": False, "since": None, "wakes_only": None, "set_by": None})
        self.assertIsNone(out["pause_file_error"])

    def test_it_is_json_and_round_trips(self) -> None:
        out = self.reduce()
        self.assertEqual(json.loads(json.dumps(out)), out)

    def test_nothing_the_source_carries_beyond_the_schema_reaches_the_output(self) -> None:
        text = json.dumps(self.reduce())
        for private in (
            PATH,
            "config_errors",
            "reload_refused",
            "partition_file",
            "owner_user_id",
            "loop_guard",
            "topic text",
            "session_id",
            "lease_id",
            "creds",
            "hosted.invalid",
            "sess-1",
            "weekly cap reached",
            "424242",
        ):
            self.assertNotIn(private, text)

    def test_running_is_derived_in_three_states_from_connected_and_freshness(self) -> None:
        seats = seats_by_name(self.reduce())
        self.assertEqual(
            {name: seat["running"] for name, seat in seats.items()},
            {
                "CLAUDE": "running",
                "DOWN": "stopped",
                "HALTED": "running",
                "JET": "running",
                "MA": "running",
                "MM": "running",
                "QUIET": "running",
            },
        )
        self.assertIs(seats["DOWN"]["connected"], False)
        document = status_document()
        document["seats"]["MA"]["connected"] = "yes"
        out = self.reduce(document)
        self.assertEqual(seats_by_name(out)["MA"]["running"], "unknown")
        self.assertIsNone(seats_by_name(out)["MA"]["connected"])

    def test_a_stale_undated_or_future_document_makes_every_seat_unknown(self) -> None:
        for updated in (
            NOW - RS.STALE_AFTER_SECONDS - 1,
            NOW + RS.STALE_AFTER_SECONDS + 1,
            None,
            "yesterday",
            float("nan"),
        ):
            with self.subTest(updated=updated):
                out = self.reduce(status_document(updated=updated))
                self.assertEqual({seat["running"] for seat in out["seats"]}, {"unknown"})
                self.assertEqual(
                    out["status_updated"],
                    iso(updated) if isinstance(updated, float) and updated > 0 else None,
                )
        edge = self.reduce(status_document(updated=NOW - RS.STALE_AFTER_SECONDS))
        self.assertIn("running", {seat["running"] for seat in edge["seats"]})

    def test_the_wake_kind_and_a_standing_in_fallback(self) -> None:
        seats = seats_by_name(self.reduce())
        self.assertEqual(
            {name: seat["wake"] for name, seat in seats.items()},
            {
                "CLAUDE": "claude",
                "DOWN": "inbox",
                "HALTED": "inbox",
                "JET": "http",
                "MA": "inbox",
                "MM": "fallback",
                "QUIET": "inbox",
            },
        )
        document = status_document()
        document["seats"]["MA"]["wake"] = "mystery"
        document["seats"]["JET"]["wake"] = None
        out = seats_by_name(self.reduce(document))
        self.assertEqual((out["MA"]["wake"], out["JET"]["wake"]), ("other", "other"))
        document["seats"]["MM"]["fallback"]["stands_in"] = False
        self.assertEqual(seats_by_name(self.reduce(document))["MM"]["wake"], "muse")

    def test_paused_covers_a_full_pause_and_a_wakes_only_pause(self) -> None:
        seats = seats_by_name(self.reduce())
        self.assertEqual(
            {name: seat["paused"] for name, seat in seats.items()},
            {
                "CLAUDE": False,
                "DOWN": False,
                "HALTED": True,
                "JET": False,
                "MA": False,
                "MM": False,
                "QUIET": True,
            },
        )

    def test_counts_and_times(self) -> None:
        seats = seats_by_name(self.reduce())
        self.assertEqual(
            (seats["JET"]["wakes_24h"], seats["JET"]["held_back"], seats["JET"]["jobs"]),
            (7, 85, 1),
        )
        self.assertEqual(seats["MA"]["last_event"], iso(NOW - 30))
        self.assertIsNone(seats["DOWN"]["last_event"])

    def test_the_quota_is_the_producers_verdict_and_nothing_derived(self) -> None:
        seats = seats_by_name(self.reduce())
        self.assertEqual(
            seats["CLAUDE"]["quota"],
            {
                "known": True,
                "exhausted": True,
                "remaining": 0,
                "reset_at": "2026-11-09T05:00:00Z",
                "used": None,
                "cap_day": None,
                "cap_hour": None,
                "source": "codecaps",
                # The source's reason is free text, so it is dropped rather than echoed.
                "reason": None,
            },
        )
        # A reading the snapshot cannot give is `known: false`, and an absent reading is never
        # called exhausted, whatever the source's own flag says.
        self.assertEqual(seats["MM"]["quota"]["known"], False)
        self.assertIsNone(seats["MM"]["quota"]["exhausted"])
        self.assertEqual(seats["MM"]["quota"]["source"], "unknown")
        # A seat with no provider has no quota at all.
        self.assertEqual(seats["MA"]["quota"]["known"], False)
        self.assertIsNone(seats["MA"]["quota"]["source"])

    def test_a_short_token_reason_is_kept_and_remaining_is_a_clamped_percent(self) -> None:
        document = status_document()
        quota = document["seats"]["CLAUDE"]["quota"]
        quota.update(reason="weekly_cap", remaining=99.6, reset_at="2026-11-09T05:00:00-06:00")
        out = seats_by_name(self.reduce(document))["CLAUDE"]["quota"]
        self.assertEqual(
            (out["reason"], out["remaining"], out["reset_at"]),
            ("weekly_cap", 100, "2026-11-09T11:00:00Z"),
        )
        for value, expected in ((-5, 0), (250, 100), (49.5, 50), (0.2, 0), (True, None)):
            quota["remaining"] = value
            out = seats_by_name(self.reduce(document))["CLAUDE"]["quota"]
            self.assertEqual(out["remaining"], expected, value)

    def test_leases_carry_names_and_no_title_and_no_ids(self) -> None:
        leases = seats_by_name(self.reduce())["CLAUDE"]["leases"]
        self.assertEqual(
            leases,
            [
                {
                    "letter": "AA",
                    "platform": "claude-code",
                    "alive": True,
                    "pending": 1,
                    "verified_host": "server",
                    "why": "ok",
                    "topics": 2,
                    "watcher": True,
                },
                {
                    "letter": None,
                    "platform": "cursor",
                    "alive": False,
                    "pending": 1,
                    "verified_host": None,
                    "why": "pid_gone",
                    "topics": 2,
                    "watcher": False,
                },
            ],
        )

    def test_the_liveness_reason_goes_through_a_fixed_table(self) -> None:
        for source, expected in (
            ("ok", "ok"),
            ("wait grace", "wait_grace"),
            ("pid reused", "pid_reused"),
            ("ended", "ended"),
            ("no lease", "no_lease"),
            ("no heartbeat", "no_heartbeat"),
            ("not an interactive claude (background)", "not_interactive"),
            ("something else entirely", None),
            (PATH, None),
            (None, None),
        ):
            with self.subTest(source=source):
                document = status_document()
                document["seats"]["CLAUDE"]["leases"][0]["why"] = source
                out = seats_by_name(self.reduce(document))["CLAUDE"]
                self.assertEqual(out["leases"][0]["why"], expected)

    def test_letters_are_known_partial_or_unknown_from_the_lease_rows_alone(self) -> None:
        def letters(rows: list) -> dict:
            document = status_document()
            document["seats"]["CLAUDE"]["leases"] = rows
            return seats_by_name(self.reduce(document))["CLAUDE"]["letters"]

        lettered, bare = lease_row(), lease_row(letter=None)
        self.assertEqual(letters([]), {"known": True, "why": None})
        self.assertEqual(letters([lettered, lettered]), {"known": True, "why": None})
        self.assertEqual(letters([lettered, bare]), {"known": False, "why": "partial"})
        self.assertEqual(letters([bare, bare]), {"known": False, "why": None})

    def test_the_global_pause_and_its_fields(self) -> None:
        entry = {"wakes_only": False, "by": "owner-dm", "ts": NOW - 90, "message_id": 12345}
        out = self.reduce(status_document(pause={"*": entry, "MA": dict(entry, by="cli")}))
        self.assertEqual(
            out["pause"],
            {"paused": True, "since": iso(NOW - 90), "wakes_only": False, "set_by": "owner-dm"},
        )
        self.assertIsNone(out["pause_file_error"])
        out = self.reduce(status_document(pause={"MA": entry}))
        self.assertEqual(out["pause"]["paused"], False)
        out = self.reduce(status_document(pause={"*": dict(entry, wakes_only=True, by=EMAIL)}))
        self.assertEqual(
            (out["pause"]["paused"], out["pause"]["wakes_only"], out["pause"]["set_by"]),
            (True, True, None),
        )

    def test_a_pause_of_the_wrong_shape_is_invalid_and_never_unreadable(self) -> None:
        for value in ("paused", ["*"], 7, True):
            with self.subTest(value=value):
                out = self.reduce(status_document(pause=value))
                self.assertEqual(out["pause_file_error"], "invalid")
                self.assertEqual(out["pause"]["paused"], None)
        out = self.reduce(status_document(pause={"*": "yes"}))
        self.assertEqual((out["pause"]["paused"], out["pause_file_error"]), (False, "invalid"))
        out = self.reduce({k: v for k, v in status_document().items() if k != "pause"})
        self.assertEqual((out["pause"]["paused"], out["pause_file_error"]), (None, None))

    def test_skipped_seats_are_classified_and_their_text_is_dropped(self) -> None:
        problems = {
            "A1": ["seat.A1.instance must be one of mac, server"],
            "A2": ["not a valid seat section"],
            "A3": ["seat.A3.enabled must be true or false"],
            "A4": ["seat.A4.bot must be the credential file code"],
            "A5": ["environment variable " + PATH + " is shared with another seat"],
            "A6": ["seat.A6.site_env must be an environment variable name"],
            "A7": ["something unforeseen " + PATH],
            "A8": [],
            "A9": "not a list",
            "B1": [7],
        }
        out = self.reduce(status_document(skipped_seats=problems))
        self.assertEqual(
            {row["seat"]: row["reason"] for row in out["skipped"]},
            {
                "A1": "invalid_instance",
                "A2": "invalid_section",
                "A3": "invalid_enabled",
                "A4": "invalid_bot",
                "A5": "shared_environment",
                "A6": "invalid_environment",
                "A7": "config_problem",
                "A8": "config_problem",
                "A9": "config_problem",
                "B1": "config_problem",
            },
        )
        self.assertNotIn(PATH, json.dumps(out))

    def test_the_instance_is_the_callers_else_the_documents_else_null(self) -> None:
        self.assertEqual(self.reduce(instance="mac")["instance"], "mac")
        self.assertEqual(self.reduce(instance=None)["instance"], "server")
        self.assertEqual(self.reduce(status_document(instance="mac"), instance=None)["instance"], "mac")
        self.assertEqual(self.reduce(status_document(instance="elsewhere"), instance=5)["instance"], None)
        self.assertEqual(self.reduce(status_document(instance=PATH), instance="SERVER")["instance"], None)


class FindingsTests(unittest.TestCase):
    """The review's findings on `disabled`, on how fresh a document must be, and on flags."""

    def reduce(self, document: Any, now: float = NOW) -> dict[str, Any]:
        out = RS.reduce_status(document, now, "server")
        check_shape(self, out)
        return out

    # ---- `disabled` is deduplicated and sorted before it is capped -------------------------

    def test_a_long_unordered_list_keeps_the_smallest_names(self) -> None:
        names = ["S%04d" % n for n in range(400)]
        shuffled = names + names[::3] + ["bad name", "../x", "lower", "", 5, None, ["S0000"]]
        random.Random(7).shuffle(shuffled)
        # The smallest name is last, far past any early slice of the list.
        shuffled.remove("S0000")
        shuffled.extend(["S0000", "S0000"])
        out = self.reduce(status_document(disabled=shuffled))
        self.assertEqual(out["disabled"], names[: RS.MAX_NAMES])
        self.assertEqual(out["disabled"][0], "S0000")
        self.assertEqual(len(out["disabled"]), RS.MAX_NAMES)

    def test_disabled_is_each_valid_name_once_in_order(self) -> None:
        out = self.reduce(status_document(disabled=["Z", "B", "B", "a", "A", "-X", "B", "9", "M"]))
        self.assertEqual(out["disabled"], ["A", "B", "M", "Z"])
        self.assertEqual(self.reduce(status_document(disabled=[]))["disabled"], [])
        self.assertEqual(self.reduce(status_document(disabled="B"))["disabled"], [])

    # ---- freshness: at most 5 seconds ahead, at most 300 seconds old --------------------

    def test_a_document_is_current_from_5_seconds_ahead_to_300_seconds_old(self) -> None:
        self.assertEqual((RS.FUTURE_SKEW_SECONDS, RS.STALE_AFTER_SECONDS), (5.0, 300.0))
        for age, running in (
            (-6, "unknown"),
            (-5.5, "unknown"),
            (-5, "running"),
            (-1, "running"),
            (0, "running"),
            (2, "running"),
            (300, "running"),
            (300.5, "unknown"),
            (301, "unknown"),
            (3600, "unknown"),
            (-3600, "unknown"),
        ):
            with self.subTest(age=age):
                out = self.reduce(status_document(updated=NOW - age))
                states = {seat["seat"]: seat["running"] for seat in out["seats"]}
                if running == "unknown":
                    self.assertEqual(set(states.values()), {"unknown"}, age)
                else:
                    self.assertEqual((states["MA"], states["DOWN"]), ("running", "stopped"), age)
                # The time itself is reported either way, so a reader can see how old it is.
                self.assertEqual(out["status_updated"], iso(NOW - age))

    # ---- a flag is a JSON boolean or it is malformed -------------------------------------------

    NOT_BOOLEANS = (1, 0, 1.0, 0.0, 2, -1, "true", "false", "1", "0", [True], {"a": True})

    def test_integers_floats_and_strings_are_never_flags(self) -> None:
        for value in self.NOT_BOOLEANS:
            with self.subTest(value=repr(value)):
                document = status_document()
                seat = document["seats"]["MA"]
                seat.update(connected=value, paused=value, wakes_paused=value)
                seat["quota"] = {
                    "known": value,
                    "exhausted": value,
                    "remaining": 10,
                    "source": "codecaps",
                }
                seat["fallback"] = {"stands_in": value}
                seat["leases"] = [lease_row(alive=value, watcher=value)]
                document["pause"] = {"*": {"wakes_only": value, "by": "cli", "ts": NOW - 5}}
                document["operator_voice"] = {"available": value, "code": None, "since": None}
                out = self.reduce(document)
                got = seats_by_name(out)["MA"]
                self.assertEqual((got["connected"], got["running"]), (None, "unknown"))
                self.assertIsNone(got["paused"])
                self.assertEqual(got["quota"]["known"], False)
                self.assertIsNone(got["quota"]["exhausted"])
                self.assertEqual(got["wake"], "inbox", "a fallback that is not `true` stands in for nothing")
                self.assertEqual(
                    (got["leases"][0]["alive"], got["leases"][0]["watcher"]), (None, None)
                )
                self.assertEqual(out["pause"]["paused"], True)
                self.assertIsNone(out["pause"]["wakes_only"])
                self.assertIsNone(out["operator_voice"]["available"])

    def test_one_flag_that_is_not_a_boolean_makes_the_seats_pause_null(self) -> None:
        # `wakes_paused` once slipped through `in (False, None)`, which is true of 0 and 0.0.
        for value in self.NOT_BOOLEANS:
            for paused, wakes in ((False, value), (value, False), (value, value), (value, None)):
                with self.subTest(paused=repr(paused), wakes_paused=repr(wakes)):
                    document = status_document()
                    document["seats"]["MA"].update(paused=paused, wakes_paused=wakes)
                    got = seats_by_name(self.reduce(document))["MA"]
                    self.assertIsNone(got["paused"])

    def test_real_booleans_are_read_as_they_are(self) -> None:
        for paused, wakes, expected in (
            (False, False, False),
            (False, None, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
            (None, False, None),
            (None, None, None),
        ):
            with self.subTest(paused=paused, wakes_paused=wakes):
                document = status_document()
                document["seats"]["MA"].update(paused=paused, wakes_paused=wakes)
                self.assertIs(seats_by_name(self.reduce(document))["MA"]["paused"], expected)
        document = status_document()
        document["seats"]["MA"].pop("wakes_paused")
        self.assertIs(seats_by_name(self.reduce(document))["MA"]["paused"], False)
        document = status_document()
        document["seats"]["MM"]["fallback"]["stands_in"] = True
        self.assertEqual(seats_by_name(self.reduce(document))["MM"]["wake"], "fallback")


ABSENT = object()


class OperatorVoiceTests(unittest.TestCase):
    """`operator_voice`: the block main's `Daemon.status()` writes (AFC-602), field by field."""

    SINCE = "2027-01-15T07:00:00Z"

    def voice(self, block: Any) -> dict[str, Any]:
        document = status_document()
        if block is ABSENT:
            document.pop("operator_voice")
        else:
            document["operator_voice"] = block
        out = RS.reduce_status(document, NOW, "server")
        check_shape(self, out)
        self.assertTrue(out["seats"], "the rest of the document is still reduced")
        return out["operator_voice"]

    NONE = {"available": None, "code": None, "since": None}

    def test_a_valid_block_is_read(self) -> None:
        for code in (
            "operator_unavailable",
            "operator_owner_not_pinned",
            "operator_credential_rejected",
            "operator_building",
            "secret_scanner",
            "dm_operator_not_verified",
            "a_" * 24,
        ):
            with self.subTest(code=code):
                block = {"available": False, "code": code, "since": self.SINCE}
                self.assertEqual(self.voice(block), block)
        up = {"available": True, "code": None, "since": self.SINCE}
        self.assertEqual(self.voice(up), up)

    def test_before_the_first_build_the_block_is_null_and_so_is_the_reading(self) -> None:
        self.assertEqual(self.voice(dict(self.NONE)), self.NONE)

    def test_a_since_is_normalised_to_utc(self) -> None:
        for since, expected in (
            ("2027-01-15T01:00:00-06:00", self.SINCE),
            ("2027-01-15T07:00:00.250Z", self.SINCE),
            (NOW - 3600, self.SINCE),
        ):
            with self.subTest(since=since):
                block = {"available": True, "code": None, "since": since}
                self.assertEqual(self.voice(block)["since"], expected)

    def test_a_malformed_field_is_null_and_the_others_stand(self) -> None:
        good = {"available": False, "code": "operator_unavailable", "since": self.SINCE}
        bad = {
            "available": ("yes", "true", 1, 0, 1.0, [], {}, [True], b"x"),
            "code": (
                PATH, EMAIL, URL, WWW, UUID, KEY, KEY2, VALUE, USER_ID, HUGE, CONTROL,
                "Operator_Unavailable", "operator unavailable", "operator-unavailable",
                "9operator", "_operator", "", " ", "\n", "a" * 49, "a_" * 25, "a" * 21,
                "operator_unavailable\n", "\uff41", 5, True, ["operator_unavailable"], {},
            ),
            "since": (
                "yesterday", "2027-01-15 07:00:00Z", "2027-01-15T07:00:00", PATH, HUGE, "",
                1e12, -1, 0, True, [], {}, float("nan"), 10**400,
            ),
        }  # fmt: skip
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)[:30]):
                    got = self.voice(dict(good, **{field: value}))
                    self.assertIsNone(got[field])
                    for other in good:
                        if other != field:
                            self.assertEqual(got[other], good[other])

    def test_a_block_that_is_not_an_object_reads_as_all_null(self) -> None:
        for block in ("available", 5, True, 1.5, [], [{"available": True}], None, (), b"{}"):
            with self.subTest(block=repr(block)[:30]):
                self.assertEqual(self.voice(block), self.NONE)

    def test_an_absent_block_from_an_older_listener_stays_all_null(self) -> None:
        self.assertEqual(self.voice(ABSENT), self.NONE)

    def test_the_fields_the_daemon_does_not_write_are_omitted_even_if_a_block_has_them(self) -> None:
        block = {
            "available": False,
            "code": "operator_unavailable",
            "since": self.SINCE,
            "last_post_at": self.SINCE,
            "last_error_code": "operator_unavailable",
            "last_error_at": self.SINCE,
            "reason": PATH,
            "client": EMAIL,
        }
        got = self.voice(block)
        self.assertEqual(set(got), {"available", "code", "since"})
        self.assertNotIn(PATH, json.dumps(got))
        self.assertNotIn(EMAIL, json.dumps(got))

    def test_a_block_with_a_bad_value_does_not_cost_the_rest_of_the_document(self) -> None:
        document = status_document()
        document["operator_voice"] = {"available": PATH, "code": PATH, "since": PATH}
        out = RS.reduce_status(document, NOW, "server")
        check_shape(self, out)
        self.assertEqual(out["operator_voice"], self.NONE)
        self.assertEqual(len(out["seats"]), 7)


class LeaseTitleTests(unittest.TestCase):
    """Lease titles are never emitted (the design owner's privacy ruling)."""

    TITLES = (
        PATH,
        EMAIL,
        VALUE,
        UUID,
        "Letters Work",
        URL,
        WINDOWS,
        TILDE,
        KEY,
        KEY2,
        USER_ID,
        "claude-eavesdropper-status",
        "Fix " + PATH + " for " + EMAIL,
        "Review PR #1446 for AFC-607",
        HUGE,
        CONTROL,
    )
    NOT_TITLES = ("", " ", None, 5, 1.5, True, ["Letters Work"], {"a": "Letters Work"})

    def reduce_with(self, title: Any) -> tuple[dict[str, Any], str]:
        document = status_document()
        for seat in document["seats"].values():
            for lease in seat["leases"]:
                lease["title"] = title
        # The title-like text is also in keys the daemon does not write, on more leases than one.
        document["seats"]["MA"]["leases"] = [
            lease_row(title=title, letter=letter, name=title, session_title=title, summary=title)
            for letter in ("A", "B", None)
        ]
        out = RS.reduce_status(document, NOW, "server")
        check_shape(self, out)
        return out, json.dumps(out, ensure_ascii=False)

    def test_no_title_like_string_from_the_source_appears_anywhere_in_the_output(self) -> None:
        for title in self.TITLES:
            with self.subTest(title=title[:30]):
                out, text = self.reduce_with(title)
                self.assertNotIn(title[:60], text)
                for words in title.split():
                    if len(words) > 4:
                        self.assertNotIn(words[:40], text)
                self.assertNotIn('"title"', text)

    def test_a_title_of_any_other_kind_changes_nothing_either(self) -> None:
        baseline = self.reduce_with("Letters Work")[0]
        for title in self.NOT_TITLES:
            with self.subTest(title=repr(title)):
                self.assertEqual(self.reduce_with(title)[0], baseline)

    def test_the_lease_objects_have_exactly_the_fields_without_a_title(self) -> None:
        out, _ = self.reduce_with("Letters Work")
        leases = [lease for seat in out["seats"] for lease in seat["leases"]]
        self.assertTrue(leases)
        for lease in leases:
            self.assertEqual(set(lease), LEASE)
            self.assertNotIn("title", lease)
        self.assertEqual(len(LEASE), 8)

    def test_the_module_has_no_title_scrubber_left(self) -> None:
        for gone in ("_title", "_hidden_word", "_kept", "_variants", "_marked", "TITLE_MAX"):
            self.assertFalse(hasattr(RS, gone), gone)


class ScalarTests(unittest.TestCase):
    def test_tokens_and_names_use_the_same_prefix_rule(self) -> None:
        for good in ("Key-Manager", "BF-Compiler", "SK-FOO-BARBAZ", "Secret-Santa"):
            self.assertEqual(RS._token(good), good)
        for bad in ("SK-A1b2C3d4E5f6G7", "Bearer-A1b2C3d4", "ghp_a1B2c3D4a1B2c3"):
            self.assertIsNone(RS._token(bad), bad)

    def test_key_shaped_tokens_are_dropped_in_any_case(self) -> None:
        tail = "A1b2C3d4E5"
        for word in ("sk-" + tail, "SK-" + tail, "Sk_" + tail, "Bearer-" + tail, "tok_" + tail):
            self.assertIsNone(RS._token(word), word)
        for word in ("ghp_a1B2c3", "GHP_a1B2c3", "xoxb-123456", "AKIAABCDEF", "glpat-a1B2c3"):
            self.assertIsNone(RS._token(word), word)
        for word in ("key-rotation", "Key-Manager", "ghost-writer", "npm-install", "asia-pacific"):
            self.assertEqual(RS._token(word), word)

    def test_times(self) -> None:
        self.assertEqual(RS._when(NOW), iso(NOW))
        self.assertEqual(RS._when(int(NOW)), iso(NOW))
        self.assertEqual(RS._when("2026-11-09T05:00:00Z"), "2026-11-09T05:00:00Z")
        self.assertEqual(RS._when("2026-11-09T05:00:00.123456789Z"), "2026-11-09T05:00:00Z")
        self.assertEqual(RS._when("2026-11-09T05:00:00-06:00"), "2026-11-09T11:00:00Z")
        self.assertEqual(RS._when("2026-11-09T05:00:00+05:30"), "2026-11-08T23:30:00Z")
        for bad in (
            "2026-11-09T05:00:00",
            "2026-13-09T05:00:00Z",
            "2026-11-09T25:00:00Z",
            "2026-11-09T05:00:60Z",
            "2026-11-09 05:00:00Z",
            "2026-11-09T05:00:00+99:00",
            "1999-01-01T00:00:00Z",
            "2100-01-02T00:00:00Z",
            "2026-11-09T05:00:00Z\n",
            PATH,
            HUGE,
            "",
            0,
            -1,
            1e12,
            True,
            float("nan"),
            10**400,
            None,
            [],
        ):
            self.assertIsNone(RS._when(bad), repr(bad)[:40])

    def test_counts(self) -> None:
        self.assertEqual(RS._count(0), 0)
        self.assertEqual(RS._count(5), 5)
        self.assertEqual(RS._count(10**30), RS.COUNT_MAX)
        for bad in (-1, 1.0, True, False, None, "3", [], float("nan")):
            self.assertIsNone(RS._count(bad), repr(bad))

    def test_tokens_and_names(self) -> None:
        for good in (
            "Claude",
            "MA",
            "claude-code",
            "owner-dm",
            "codecaps",
            "a_b-c",
            "Ghost-Writer",
            "Key-Manager",
            "BF-Compiler",
        ):
            self.assertEqual(RS._token(good), good)
        for bad in (
            "",
            "-x",
            ".x",
            "a b",
            "a/b",
            "a@b",
            WWW,
            "a.b",
            "reduced_status.py",
            "x" * 25,
            "123456",
            "abc1234567",
            KEY,
            KEY2,
            VALUE,
            UUID,
            "a\n",
            "ok‮",
            "ＡＢ",
            None,
            5,
            ["a"],
        ):
            self.assertIsNone(RS._token(bad), repr(bad))
        for good in ("MA", "BF-HOUSEKEEPER", "GROK-WEB", "A1", "GHOST-WRITER", "SK-FOO-BARBAZ"):
            self.assertEqual(RS._name(good), good)
        for bad in ("ma", "", "-A", "A" * 33, "A B", "A\n", "123", USER_ID, "A" * 21, None, 3):
            self.assertIsNone(RS._name(bad), repr(bad))


class HostileInputTests(unittest.TestCase):
    def test_a_document_that_is_not_a_dict_gives_an_empty_valid_document(self) -> None:
        for document in (None, [], "text", 5, True, 1.5, (), b"{}", [status_document()], PATH):
            with self.subTest(document=repr(document)[:30]):
                out = RS.reduce_status(document, NOW, "server")
                check_shape(self, out)
                self.assertEqual((out["seats"], out["skipped"], out["disabled"]), ([], [], []))
                self.assertIsNone(out["status_updated"])

    def test_a_bad_now_or_instance_never_raises(self) -> None:
        for now in (None, "now", float("nan"), float("inf"), -1, 0, 10**400, [], True):
            for instance in (None, "x", 5, [], PATH):
                with self.subTest(now=repr(now), instance=repr(instance)):
                    out = RS.reduce_status(status_document(), now, instance)
                    check_shape(self, out)

    def test_every_position_of_the_document_given_every_hostile_value(self) -> None:
        document = status_document()
        count = 0
        for path in paths(document):
            for value in HOSTILE:
                mutated = replace_at(document, path, copy.deepcopy(value))
                out = RS.reduce_status(mutated, NOW, "server")
                check_shape(self, out)
                count += 1
        self.assertGreater(count, 3000)

    def test_every_key_of_the_document_renamed_to_every_hostile_string(self) -> None:
        document = status_document()
        for path in paths(document):
            if not isinstance(path[-1], str):
                continue
            parent = document
            for step in path[:-1]:
                parent = parent[step]
            if not isinstance(parent, dict):
                continue
            for hostile in HOSTILE_STRINGS:
                if hostile in parent:
                    continue
                out = RS.reduce_status(rename_key(document, path, hostile), NOW, "server")
                check_shape(self, out)

    def test_canary_keys_and_values_at_every_level_are_never_copied(self) -> None:
        document = status_document()
        document["canary"] = VALUE
        for seat in document["seats"].values():
            seat["canary"] = VALUE
            seat["extra_" + VALUE[:10]] = VALUE
            if isinstance(seat.get("quota"), dict):
                seat["quota"]["canary"] = VALUE
            for lease in seat["leases"]:
                lease["canary"] = VALUE
        out = RS.reduce_status(document, NOW, "server")
        check_shape(self, out)
        self.assertNotIn(VALUE, json.dumps(out))
        self.assertNotIn("canary", json.dumps(out))

    def test_the_output_stays_small_for_a_huge_document(self) -> None:
        leases = [lease_row(title=HUGE, platform=HUGE, letter=HUGE) for _ in range(500)]
        seats = {"S%d" % n: seat_row(leases=leases, bot=HUGE) for n in range(2000)}
        document = status_document(
            seats=seats,
            disabled=["D%d" % n for n in range(5000)],
            skipped_seats={"K%d" % n: [HUGE] for n in range(5000)},
        )
        out = RS.reduce_status(document, NOW, "server")
        check_shape(self, out)
        self.assertEqual(len(out["seats"]), RS.MAX_SEATS)
        self.assertTrue(all(len(seat["leases"]) == RS.MAX_LEASES for seat in out["seats"]))
        self.assertEqual(len(out["disabled"]), RS.MAX_NAMES)
        self.assertEqual(len(out["skipped"]), RS.MAX_SKIPPED)
        self.assertLess(len(json.dumps(out)), 512 * 1024)

    def test_deep_and_wide_values_in_the_fields_it_reads_do_not_raise(self) -> None:
        deep: Any = []
        for _ in range(50_000):
            deep = [deep]
        wide = {str(n): {} for n in range(50_000)}
        for hostile in (deep, wide):
            document = status_document(pause=wide if hostile is wide else deep)
            document["seats"]["MA"]["leases"] = [hostile] if hostile is deep else [wide]
            document["seats"]["MA"]["quota"] = hostile
            out = RS.reduce_status(document, NOW, "server")
            check_shape(self, out)

    def test_an_unforeseen_exception_degrades_to_the_empty_document(self) -> None:
        with mock.patch.object(RS, "_seats", side_effect=RuntimeError(PATH)):
            out = RS.reduce_status(status_document(), NOW, "server")
        check_shape(self, out)
        self.assertEqual((out["seats"], out["instance"]), ([], "server"))
        self.assertNotIn(PATH, json.dumps(out))

    def test_one_seat_that_cannot_be_reduced_costs_only_itself(self) -> None:
        real = RS._seat

        def flaky(name: str, row: dict, fresh: bool) -> dict:
            if name == "MA":
                raise RuntimeError(PATH)
            return real(name, row, fresh)

        with mock.patch.object(RS, "_seat", flaky):
            out = RS.reduce_status(status_document(), NOW, "server")
        check_shape(self, out)
        self.assertEqual(
            [seat["seat"] for seat in out["seats"]],
            ["CLAUDE", "DOWN", "HALTED", "JET", "MM", "QUIET"],
        )

    def test_the_input_is_not_changed(self) -> None:
        document = status_document()
        before = copy.deepcopy(document)
        RS.reduce_status(document, NOW, "server")
        self.assertEqual(document, before)


class FromRealDaemonStatusTests(DaemonHarness):
    """A document written by a real daemon, not one assembled here."""

    def written_status(self) -> dict[str, Any]:
        daemon = self.started()
        daemon.write_status()
        with open(os.path.join(L.listener_dir(self.root), "status.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_real_status_reduces_to_the_schema_without_its_private_fields(self) -> None:
        self.write_lease("L1", session_id="sess-aaaaaaaa")
        self.write_lease("L2", session_id="sess-bbbbbbbb")
        daemon = self.started()
        D.OR.register_session(
            daemon._registration_db(),
            platform="claude",
            verified_host="mac",
            profile_scope="default",
            native_session_id="sess-aaaaaaaa",
            seat="CLAUDE",
            source="test",
            title="Letters Work",
            issuer=D.OR.ISSUER_OPERATOR,
        )
        daemon.write_status()
        path = os.path.join(L.listener_dir(self.root), "status.json")
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
        out = RS.reduce_status(document, document["updated"] + 1, "mac")
        check_shape(self, out)
        text = json.dumps(out)
        for private in (
            self.root,
            "sess-aaaaaaaa",
            "sess-bbbbbbbb",
            "session_id",
            "lease_id",
            "partition_file",
            "owner_user_id",
            "config_errors",
            "L1",
            "L2",
        ):
            self.assertNotIn(private, text)
        seat = seats_by_name(out)["CLAUDE"]
        self.assertEqual((seat["bot"], seat["connected"], seat["running"]), ("Claude", True, "running"))
        self.assertEqual(out["status_updated"], iso(document["updated"]))
        self.assertEqual(out["instance"], "mac")
        self.assertEqual(len(seat["leases"]), 2)
        lettered = [lease for lease in seat["leases"] if lease["letter"]]
        self.assertEqual(len(lettered), 1)
        self.assertEqual(
            (lettered[0]["letter"], lettered[0]["verified_host"]), ("AA", "mac")
        )
        # The registry title ("Letters Work") reached the daemon's own status, and never this.
        self.assertIn("Letters Work", json.dumps(document))
        self.assertNotIn("Letters Work", text)
        for lease in seat["leases"]:
            self.assertNotIn("title", lease)
        self.assertEqual(
            {lease["platform"] for lease in seat["leases"]}, {"claude-code"}
        )
        self.assertEqual(seat["letters"], {"known": False, "why": "partial"})

    def test_the_real_operator_voice_block_is_read_before_and_after_a_build(self) -> None:
        daemon = self.started()
        daemon.write_status()
        path = os.path.join(L.listener_dir(self.root), "status.json")

        def reduced() -> tuple[dict[str, Any], dict[str, Any]]:
            with open(path, encoding="utf-8") as fh:
                document = json.load(fh)
            out = RS.reduce_status(document, document["updated"] + 1, "server")
            check_shape(self, out)
            return document["operator_voice"], out["operator_voice"]

        written, read = reduced()
        self.assertEqual(set(written), {"available", "code", "since"})
        self.assertEqual(read, {"available": None, "code": None, "since": None})
        self.warm_operator(daemon)
        daemon.write_status()
        written, read = reduced()
        self.assertIs(written["available"], True)
        self.assertRegex(written["since"], ISO)
        self.assertEqual(read, {"available": True, "code": None, "since": written["since"]})


    def test_a_real_status_goes_stale_and_the_seat_becomes_unknown(self) -> None:
        document = self.written_status()
        fresh = RS.reduce_status(document, document["updated"] + 5, "server")
        stale = RS.reduce_status(document, document["updated"] + 3600, "server")
        check_shape(self, fresh)
        check_shape(self, stale)
        self.assertEqual(seats_by_name(fresh)["CLAUDE"]["running"], "running")
        self.assertEqual(seats_by_name(stale)["CLAUDE"]["running"], "unknown")
        self.assertEqual(fresh["status_updated"], stale["status_updated"])

    def test_every_key_a_real_status_emits_is_either_read_or_deliberately_not(self) -> None:
        """A new field in `Daemon.status()` shows up here, so someone decides about it."""
        document = self.written_status()
        read = {"updated", "instance", "disabled", "skipped_seats", "pause", "seats", "operator_voice"}
        not_read = {
            "pid", "version", "started", "muse_bridge", "config_errors", "reload_refused",
            "reload_at", "partition_file", "owner_user_id", "chat_nudges", "wake_loop_prevention",
            "loop_guard", "rewake_verified", "rewake_verified_cli", "rewake_verified_desktop",
            # AFC-652's planned-drain checklist:  on-host operator tooling with message ids
            # and effect reasons in its terminal rows, so the reducer deliberately never
            # reads it (see reduced_status.py, "Deliberately never read").
            "drain",
        }  # fmt: skip
        self.assertEqual(set(document), read | not_read)
        seat_keys = {
            "bot", "creds", "connected", "cursor", "webhook_high_water", "last_event", "wake",
            "routine", "muse_bridge",
            "pinned", "pin", "claude", "muse", "model", "fallback", "provider", "quota",
            "wakes_24h", "routes_24h", "route_enabled", "budget", "live", "jobs", "running",
            "paused", "wakes_paused", "held_back", "leases", "red", "chat_nudges",
        }  # fmt: skip
        self.assertEqual(set(document["seats"]["CLAUDE"]), seat_keys)


if __name__ == "__main__":
    unittest.main()
