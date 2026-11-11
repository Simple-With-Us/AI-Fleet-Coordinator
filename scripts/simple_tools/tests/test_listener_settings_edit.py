"""The server listener settings editor (AFC-710):  `server/listener_settings_edit.py`, its allowlist
`scripts/fleet-ops-listener-settings.json`, and the `listener-settings` verb of `fleet-ops-control.py`.

What these hold:  a key that is not allowlisted is refused, a key that reads as a secret or is a pin
is refused whatever the allowlist says, a plan writes nothing and equals what an apply then does, a
candidate the listener's check-config would not take leaves the file byte for byte, and a refused
reload puts the old bytes back.  Fakes only:  no docker, no listener, no network.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from simple_tools.server import listener_settings_edit as M

SCRIPTS = Path(__file__).resolve().parent.parent.parent
ALLOWLIST_PATH = SCRIPTS / "fleet-ops-listener-settings.json"
SAMPLE = SCRIPTS / "simple_tools" / "server" / "listener.toml"

FIXTURE = """# a comment at the top that must stay
[daemon]
instance = "server"
owner_user_id = 42                # pinned by daemon init
eligible_user_ids = []
coalesce_seconds = 20   # how long to wait
presence_topics = [
    ["work-in-progress", "roll call"],
    ["alerts", "*"],
]
note = "a # that is not a comment"   # the real comment

[linear_comments]
enabled = false

# The JET seat.
[seat.JET]
instance = "server"
creds = "env"                     # ZULIP_JET_EMAIL, ZULIP_JET_API_KEY
wake = "http"
routine = { url_env = "JET_ROUTINE_URL", key_env = "JET_ROUTINE_KEY", method = "POST", auth = "hmac-sha256", header = "X-Simple-Tools-Signature", timeout_seconds = 15 }
budget = { usd_per_day = 2.0, board_per_day = 0 }

[seat.ZC]
instance = "server"
creds = "env"
wake = "inbox"

[seat.GB-FIXER]
instance = "server"
enabled = true
creds = "env"
wake = "inbox"
"""


def shipped_allowlist(apply_enabled: bool = True) -> dict:
    doc = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    doc["applyEnabled"] = apply_enabled
    return doc


def ok_check(candidate, live):
    return {"ok": True, "problems": [], "warnings": 0}


def bad_check(candidate, live):
    return {"ok": False, "problems": ["seat.JET: something the listener would not take"], "warnings": 0}


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "data"
        self.dir.mkdir(mode=0o700)
        self.live = self.dir / "listener.toml"
        self.live.write_text(FIXTURE, encoding="utf-8")
        os.chmod(self.live, 0o640)
        self.before = self.live.read_bytes()
        self.allow = shipped_allowlist()

    def request(self, action, changes=None, allow=None, **more):
        request = {"action": action, "allowlist": self.allow if allow is None else allow}
        if changes is not None:
            request["set"] = changes
        request.update(more)
        return request

    def run_request(self, action, changes=None, allow=None, checker=ok_check, **kw):
        more = {k: kw.pop(k) for k in list(kw) if k == "expectPlanSHA256"}
        return M.run_request(
            self.request(action, changes, allow, **more), path=self.live, checker=checker, **kw
        )

    def names(self) -> list[str]:
        return sorted(os.listdir(self.dir))

    def assert_untouched(self) -> None:
        self.assertEqual(self.live.read_bytes(), self.before)
        self.assertEqual(self.names(), ["listener.toml"])


class AllowlistTests(Case):
    def test_the_shipped_allowlist_is_valid_and_ships_with_apply_off(self) -> None:
        doc = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        self.assertIs(doc["applyEnabled"], False)
        checked = M.validate_allowlist(doc)
        self.assertEqual(sorted(checked["seat"]), ["budget", "enabled", "live", "routine", "wake"])
        self.assertEqual(
            sorted(checked["tables"]),
            ["daemon", "linear_comments", "linear_projects", "listener_api", "notes_queue"],
        )
        for table in ("linear_comments", "linear_projects", "listener_api", "notes_queue"):
            self.assertEqual(list(checked["tables"][table]), ["enabled"])

    def test_the_shipped_allowlist_names_no_pin_and_no_credential_key(self) -> None:
        doc = M.validate_allowlist(shipped_allowlist())
        for pin in ("instance", "owner_user_id", "eligible_user_ids", "owner_clients"):
            self.assertNotIn(pin, doc["tables"]["daemon"])
        for key in ("instance", "creds", "bot", "bearer_file", "email_env", "key_env", "site_env"):
            self.assertNotIn(key, doc["seat"])

    def test_an_allowlist_that_lists_a_pin_or_a_secret_is_refused_whole(self) -> None:
        for table, key in (
            ("daemon", "owner_user_id"),
            ("daemon", "instance"),
            ("daemon", "eligible_user_ids"),
            ("daemon", "zulip_token"),
            ("daemon", "bearer_file"),
            ("notes_queue", "api_key"),
        ):
            with self.subTest(key=key):
                doc = shipped_allowlist()
                doc["tables"].setdefault(table, {})[key] = {"type": "bool"}
                with self.assertRaises(M.Refusal) as caught:
                    M.validate_allowlist(doc)
                self.assertEqual(caught.exception.code, "allowlist_invalid")
        for key in ("creds", "instance", "bearer_file", "key_env", "email_env", "secret"):
            with self.subTest(seat_key=key):
                doc = shipped_allowlist()
                doc["seat"][key] = {"type": "bool"}
                with self.assertRaises(M.Refusal):
                    M.validate_allowlist(doc)

    def test_a_secret_named_sub_key_cannot_be_listed_inside_a_table(self) -> None:
        doc = shipped_allowlist()
        doc["seat"]["routine"]["keys"]["token_file"] = {"type": "bool"}
        with self.assertRaises(M.Refusal):
            M.validate_allowlist(doc)
        doc = shipped_allowlist()
        doc["seat"]["routine"]["keys"]["url_env"] = {"type": "string", "pattern": ".*", "maxLength": 64}
        with self.assertRaises(M.Refusal):
            M.validate_allowlist(doc)

    def test_a_malformed_allowlist_is_refused(self) -> None:
        for change in (
            lambda d: d.pop("applyEnabled"),
            lambda d: d.update(applyEnabled="yes"),
            lambda d: d.update(schemaVersion=2),
            lambda d: d.update(extra=1),
            lambda d: d["tables"].update(seat={}),
            lambda d: d["seat"].update(enabled={"type": "anything"}),
            lambda d: d["tables"]["daemon"].update(coalesce_seconds={"type": "number", "min": 5, "max": 1}),
            lambda d: d["seat"].update(enabled={"type": "bool", "extra": 1}),
        ):
            doc = shipped_allowlist()
            change(doc)
            with self.assertRaises(M.Refusal):
                M.validate_allowlist(doc)
        answer, code = M.run_request({"action": "get", "allowlist": None}, path=self.live)
        self.assertEqual((code, answer["errorCode"]), (2, "allowlist_invalid"))


class RefusalTests(Case):
    def refused(self, changes, code, action="plan"):
        answer, status = self.run_request(action, changes)
        self.assertEqual(status, 2, answer)
        self.assertEqual(answer["result"], "refused")
        self.assertEqual(answer["errorCode"], code, answer)
        self.assert_untouched()
        return answer

    def test_a_key_that_is_not_allowlisted_is_refused(self) -> None:
        for key, value in (
            ("daemon.notify_banners", True),
            ("daemon.held_alert_seat", "MA"),
            ("seat.JET.model", "sonnet"),
            ("seat.JET.fallback", "mcode"),
            ("outgoing_webhook.enabled", True),
            ("outgoing_webhook.port", 9999),
            ("decider.enabled", True),
        ):
            with self.subTest(key=key):
                self.refused({key: value}, "key_not_allowed")
                self.refused({key: value}, "key_not_allowed", action="apply")

    def test_a_pin_or_a_credential_key_is_refused_even_if_an_allowlist_lists_it(self) -> None:
        # The allowlist is bypassed on purpose here (validate_allowlist would refuse it):  the
        # second gate, spec_for, refuses on its own.
        allow = M.validate_allowlist(shipped_allowlist())
        allow["tables"]["daemon"]["owner_user_id"] = {"type": "int", "min": 0, "max": 10**12}
        allow["tables"]["daemon"]["instance"] = {"type": "enum", "values": ["mac", "server"]}
        allow["seat"]["creds"] = {"type": "enum", "values": ["env"]}
        allow["seat"]["instance"] = {"type": "enum", "values": ["mac", "server"]}
        for key, value in (
            ("daemon.owner_user_id", 7),
            ("daemon.instance", "mac"),
            ("daemon.eligible_user_ids", [7]),
            ("daemon.owner_clients", ["website"]),
            ("seat.JET.creds", "env"),
            ("seat.JET.instance", "mac"),
            ("seat.JET.bearer_file", "x"),
            ("seat.JET.email_env", "ZULIP_JET_EMAIL"),
            ("seat.JET.key_env", "ZULIP_CODEX_API_KEY"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(M.Refusal) as caught:
                    M.build_plan(FIXTURE, allow, {key: value})
                self.assertEqual(caught.exception.code, "key_never_allowed")
                self.refused({key: value}, "key_never_allowed")

    def test_a_secret_looking_key_is_refused(self) -> None:
        for key in (
            "seat.JET.api_key",
            "seat.JET.routine_key",
            "seat.JET.token",
            "seat.JET.password",
            "seat.JET.secret_name",
            "seat.JET.bearer_locator",
            "seat.JET.routine_url",
            "daemon.zulip_token",
            "daemon.token_path",
            "daemon.credentials_file",
            "linear_comments.developer_token_env",
            "listener_api.bearer",
        ):
            with self.subTest(key=key):
                self.refused({key: "x"}, "key_never_allowed")

    def test_a_secret_looking_value_is_refused(self) -> None:
        routine = {"url_env": "JET_ROUTINE_URL", "key_env": "JET_ROUTINE_KEY"}
        for header in (
            "sk-abcdefghijklmnopqrstuvwxyz0123456789",
            "crsr_0123456789abcdefABCDEF0123456789",
            "lin_api_abcdefghij0123456789ABCDEFGHIJ",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH",
            "https://example.com/hook",
            "/data/secret.key",
            "~/token",
        ):
            with self.subTest(header=header):
                self.refused({"seat.JET.routine": dict(routine, header=header)}, "secret_like_value")
        self.refused(
            {"daemon.presence_topics": [["alerts", "https://example.com/x"]]}, "secret_like_value"
        )
        self.refused(
            {"daemon.presence_topics": [["alerts", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH"]]},
            "secret_like_value",
        )

    def test_a_name_made_of_words_is_not_taken_for_a_secret(self) -> None:
        routine = tomllib.loads(FIXTURE)["seat"]["JET"]["routine"]
        for header in ("X-Simple-Tools-Signature-256-V2", "X-Hub-Signature-256", "X-Routine-Key-2026-11-11-Rev-2"):
            with self.subTest(header=header):
                answer, status = self.run_request("plan", {"seat.JET.routine": dict(routine, header=header)})
                self.assertEqual((status, answer["result"]), (0, "planned"), answer)
        for topic in ("daily-standup-2026-11-11", "release_2026.11.11_candidate_3", "AFC-710 listener settings editor"):
            with self.subTest(topic=topic):
                answer, status = self.run_request(
                    "plan", {"daemon.presence_topics": [["work-in-progress", topic]]}
                )
                self.assertEqual((status, answer["result"]), (0, "planned"), answer)
        for secret in (
            "0123456789abcdef0123456789abcdef01234567",
            "prefix-0123456789abcdefghij-suffix",
            "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A",
        ):
            with self.subTest(secret=secret):
                self.assertTrue(M._secret_like(secret))
        for name in ("work-in-progress", "roll call", "X-Simple-Tools-Signature", "ZULIP_ALERT_GB_HOUSEKEEPER_ENDPOINT", "*"):
            with self.subTest(name=name):
                self.assertFalse(M._secret_like(name))

    def test_a_routine_env_name_is_bound_to_its_own_seat(self) -> None:
        # Pointing ZC's routine at JET's URL or key variable would send one seat's key to another
        # seat's endpoint.
        for routine in (
            {"url_env": "JET_ROUTINE_URL", "key_env": "ZC_ROUTINE_KEY"},
            {"url_env": "ZC_ROUTINE_URL", "key_env": "JET_ROUTINE_KEY"},
            {"url_env": "ZC_ROUTINE_URL", "key_env": "ZULIP_CODEX_API_KEY"},
            {"url_env": "ZC_ROUTINE_URL", "key_env": "COOLIFY_API_TOKEN"},
            {"url_env": "zc_routine_url", "key_env": "ZC_ROUTINE_KEY"},
        ):
            with self.subTest(routine=routine):
                answer, status = self.run_request("plan", {"seat.ZC.routine": routine})
                self.assertEqual(status, 2)
                self.assertIn(answer["errorCode"], ("env_name_not_allowed", "value_not_allowed"))
        answer, status = self.run_request(
            "plan", {"seat.ZC.routine": {"url_env": "ZC_ROUTINE_URL", "key_env": "ZC_ROUTINE_KEY"}}
        )
        self.assertEqual((status, answer["result"]), (0, "planned"))
        answer, status = self.run_request(
            "plan",
            {
                "seat.GB-FIXER.routine": {
                    "url_env": "ZULIP_ALERT_GB_FIXER_ENDPOINT",
                    "key_env": "ZULIP_ALERT_GB_FIXER_KEY",
                }
            },
        )
        self.assertEqual((status, answer["result"]), (0, "planned"))

    def test_a_routine_may_keep_the_names_it_has(self) -> None:
        text = FIXTURE.replace("JET_ROUTINE_URL", "LEGACY_JET_HOOK")
        self.live.write_text(text, encoding="utf-8")
        routine = tomllib.loads(text)["seat"]["JET"]["routine"]
        answer, status = self.run_request("plan", {"seat.JET.routine": dict(routine, timeout_seconds=30)})
        self.assertEqual((status, answer["result"]), (0, "planned"))
        answer, status = self.run_request(
            "plan", {"seat.JET.routine": dict(routine, url_env="OTHER_HOOK")}
        )
        self.assertEqual((status, answer["errorCode"]), (2, "env_name_not_allowed"))

    def test_a_value_of_the_wrong_type_or_range_is_refused(self) -> None:
        for key, value in (
            ("seat.ZC.enabled", "false"),
            ("seat.ZC.enabled", 0),
            ("seat.ZC.live", False),
            ("seat.ZC.live", {"per_hour": -1}),
            ("seat.ZC.live", {"per_hour": True}),
            ("seat.ZC.wake", "claude"),
            ("seat.ZC.wake", "openrouter"),
            ("seat.JET.budget", {"usd_per_day": 1000000}),
            ("seat.JET.budget", {"wakes_per_hour": 1.5}),
            ("seat.JET.budget", 5),
            ("daemon.coalesce_seconds", -1),
            ("daemon.coalesce_seconds", float("inf")),
            ("daemon.coalesce_seconds", True),
            ("daemon.host_wake_load_per_core", 101),
            ("daemon.presence_topics", [["only-one"]]),
            ("daemon.presence_topics", "alerts"),
            ("daemon.presence_topics", [["a", "b\nc"]]),
            ("linear_comments.enabled", "true"),
        ):
            with self.subTest(key=key, value=value):
                self.refused({key: value}, "value_not_allowed")
        self.refused({"seat.JET.budget": {"made_up_cap": 1}}, "key_not_allowed")
        self.refused({"seat.JET.routine": {"url_file": "x"}}, "key_not_allowed")

    def test_a_key_of_the_wrong_shape_is_refused(self) -> None:
        for key in (
            "enabled",
            "seat.ZC",
            "seat.ZC.live.per_hour",
            "seat.zc.enabled",
            "seat.Z C.enabled",
            'seat."ZC".enabled',
            "daemon.sub.key",
            "seat..enabled",
            "DAEMON.coalesce_seconds",
            "",
        ):
            with self.subTest(key=key):
                self.refused({key: True}, "key_not_allowed")

    def test_a_seat_the_file_does_not_have_is_refused(self) -> None:
        self.refused({"seat.NOBODY.enabled": False}, "unknown_seat")

    def test_an_empty_or_oversized_request_is_refused(self) -> None:
        self.refused({}, "usage")
        self.refused(None, "usage")
        many = {"seat.ZC.enabled": False}
        answer, status = M.run_request(
            {"action": "plan", "allowlist": self.allow, "set": many, "file": "/etc/passwd"},
            path=self.live,
        )
        self.assertEqual((status, answer["errorCode"]), (2, "usage"))
        answer, status = M.run_request({"action": "delete", "allowlist": self.allow}, path=self.live)
        self.assertEqual((status, answer["errorCode"]), (2, "usage"))


class EditTests(Case):
    def plan_text(self, changes, text=FIXTURE):
        return M.build_plan(text, M.validate_allowlist(self.allow), changes)

    def test_only_the_changed_line_differs_and_its_comment_stays(self) -> None:
        new, changes = self.plan_text({"daemon.coalesce_seconds": 45})
        old_lines, new_lines = FIXTURE.splitlines(), new.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        differing = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        self.assertEqual(
            differing, [("coalesce_seconds = 20   # how long to wait", "coalesce_seconds = 45   # how long to wait")]
        )
        self.assertEqual(changes, [{"key": "daemon.coalesce_seconds", "set": True, "from": 20, "to": 45, "removes": []}])

    def test_a_missing_key_is_added_inside_its_own_section(self) -> None:
        new, changes = self.plan_text({"seat.ZC.enabled": False})
        self.assertEqual(changes, [{"key": "seat.ZC.enabled", "set": False, "from": None, "to": False, "removes": []}])
        self.assertIn('[seat.ZC]\ninstance = "server"\ncreds = "env"\nwake = "inbox"\nenabled = false\n', new)
        doc = tomllib.loads(new)
        self.assertIs(doc["seat"]["ZC"]["enabled"], False)
        self.assertNotIn("enabled", doc["seat"]["JET"])
        self.assertIs(doc["seat"]["GB-FIXER"]["enabled"], True)

    def test_a_value_over_several_lines_is_replaced_whole(self) -> None:
        new, _ = self.plan_text({"daemon.presence_topics": [["alerts", "*"], ["builds", "gates"]]})
        self.assertIn('presence_topics = [["alerts", "*"], ["builds", "gates"]]\n', new)
        self.assertNotIn("roll call", new)
        self.assertEqual(
            tomllib.loads(new)["daemon"]["presence_topics"], [["alerts", "*"], ["builds", "gates"]]
        )
        # The line after the old array still follows, comment and all.
        self.assertIn('note = "a # that is not a comment"   # the real comment\n', new)

    def test_a_hash_inside_a_string_is_not_taken_for_a_comment(self) -> None:
        self.assertEqual(
            M._trailing_comment('note = "a # that is not a comment"   # the real comment\n'),
            "   # the real comment",
        )
        self.assertEqual(M._trailing_comment('note = "a # b"\n'), "")
        self.assertEqual(M._trailing_comment("x = 1\n"), "")

    def test_a_line_inside_an_array_is_never_taken_for_a_header(self) -> None:
        text = '[daemon]\ninstance = "server"\npresence_topics = [\n["seat.ZC"],\n[ "x" ],\n]\ncoalesce_seconds = 5\n\n[seat.ZC]\nwake = "inbox"\n'
        lines, found = M.scan(text)
        self.assertEqual(
            [(s["kind"], s["table"], s.get("key")) for s in found],
            [
                ("header", ("daemon",), None),
                ("pair", ("daemon",), "instance"),
                ("pair", ("daemon",), "presence_topics"),
                ("pair", ("daemon",), "coalesce_seconds"),
                ("header", ("seat", "ZC"), None),
                ("pair", ("seat", "ZC"), "wake"),
            ],
        )

    def test_a_settings_table_the_file_lacks_is_appended(self) -> None:
        new, changes = self.plan_text({"notes_queue.enabled": True})
        self.assertTrue(new.startswith(FIXTURE))
        self.assertTrue(new.endswith("\n[notes_queue]\nenabled = true\n"))
        self.assertEqual(changes[0]["set"], False)

    def test_several_changes_at_once_change_exactly_those(self) -> None:
        wanted = {
            "seat.JET.budget": {"usd_per_day": 3.5, "board_per_day": 0},
            "seat.GB-FIXER.enabled": False,
            "linear_comments.enabled": True,
            "daemon.coalesce_seconds": 30,
            "seat.ZC.live": {"per_hour": 2, "loop_turns": 1},
        }
        new, changes = self.plan_text(wanted)
        self.assertEqual([c["key"] for c in changes], sorted(wanted))
        before, after = M.settings_of(tomllib.loads(FIXTURE)), M.settings_of(tomllib.loads(new))
        differing = {
            M._key_text(*k) for k in set(before) | set(after) if before.get(k) != after.get(k)
        }
        self.assertEqual(differing, set(wanted))
        for key, value in wanted.items():
            table, name = M.split_key(key)
            self.assertEqual(after[(table, name)], value)
        # Pins and credential names are where they were, byte for byte.
        for line in ("owner_user_id = 42                # pinned by daemon init", 'instance = "server"', 'creds = "env"                     # ZULIP_JET_EMAIL, ZULIP_JET_API_KEY'):
            self.assertIn(line, new)

    def test_a_whole_table_value_never_drops_a_sub_key_silently(self) -> None:
        # JET's budget has usd_per_day and board_per_day.  Sending only one would drop the other:
        # on a live seat that could be a cap or a pause (wakes_per_day = 0).
        with self.assertRaises(M.Refusal) as caught:
            self.plan_text({"seat.JET.budget": {"usd_per_day": 3.5}})
        self.assertEqual(caught.exception.code, "would_remove_keys")
        self.assertIn("seat.JET.budget.board_per_day", caught.exception.detail)
        paused = FIXTURE.replace(
            "budget = { usd_per_day = 2.0, board_per_day = 0 }",
            "budget = { usd_per_day = 2.0, wakes_per_day = 0 }",
        )
        with self.assertRaises(M.Refusal) as caught:
            self.plan_text({"seat.JET.budget": {"usd_per_day": 2.0}}, paused)
        self.assertEqual(caught.exception.code, "would_remove_keys")
        self.assertIn("seat.JET.budget.wakes_per_day", caught.exception.detail)
        with self.assertRaises(M.Refusal) as caught:
            self.plan_text({"seat.JET.routine": {"url_env": "JET_ROUTINE_URL", "key_env": "JET_ROUTINE_KEY"}})
        self.assertEqual(caught.exception.code, "would_remove_keys")

    def test_a_named_removal_drops_exactly_what_it_names(self) -> None:
        new, changes = M.build_plan(
            FIXTURE,
            M.validate_allowlist(self.allow),
            {"seat.JET.budget": {"usd_per_day": 3.5}},
            ["seat.JET.budget.board_per_day"],
        )
        self.assertEqual(changes[0]["removes"], ["seat.JET.budget.board_per_day"])
        self.assertEqual(tomllib.loads(new)["seat"]["JET"]["budget"], {"usd_per_day": 3.5})
        # A value that keeps every sub-key removes nothing.
        _, changes = self.plan_text({"seat.JET.budget": {"usd_per_day": 3.5, "board_per_day": 0}})
        self.assertEqual(changes[0]["removes"], [])
        # A first value for a seat that had none removes nothing either.
        _, changes = self.plan_text({"seat.ZC.budget": {"usd_per_day": 1.0}})
        self.assertEqual(changes[0]["removes"], [])

    def test_a_removal_the_request_does_not_make_is_refused(self) -> None:
        allow = M.validate_allowlist(self.allow)
        for remove in (
            ["seat.JET.budget.usd_per_day"],
            ["seat.ZC.budget.board_per_day"],
            ["seat.JET.budget.board_per_day", "seat.JET.budget.nothing"],
            ["daemon.coalesce_seconds.x"],
            ["seat.JET.budget.board_per_day", "seat.JET.budget.board_per_day"],
            ["seat.JET.budget"],
            ["not a name"],
            "seat.JET.budget.board_per_day",
            [5],
        ):
            with self.subTest(remove=remove):
                with self.assertRaises(M.Refusal) as caught:
                    # This value keeps both of the budget's sub-keys, so it drops nothing.
                    M.build_plan(
                        FIXTURE,
                        allow,
                        {"seat.JET.budget": {"usd_per_day": 3.5, "board_per_day": 0}},
                        remove,
                    )
                self.assertEqual(caught.exception.code, "usage")

    def test_the_removal_is_part_of_the_plan_and_of_its_hash(self) -> None:
        wanted = {"seat.JET.budget": {"usd_per_day": 3.5}}
        refused, status = self.run_request("plan", wanted)
        self.assertEqual((status, refused["errorCode"]), (2, "would_remove_keys"))
        self.assert_untouched()
        planned, status = M.run_request(
            self.request("plan", wanted, remove=["seat.JET.budget.board_per_day"]),
            path=self.live,
            checker=ok_check,
        )
        self.assertEqual((status, planned["changes"][0]["removes"]), (0, ["seat.JET.budget.board_per_day"]))
        applied, status = M.run_request(
            self.request(
                "apply",
                wanted,
                remove=["seat.JET.budget.board_per_day"],
                expectPlanSHA256=planned["planSHA256"],
            ),
            path=self.live,
            checker=ok_check,
        )
        self.assertEqual((status, applied["result"], applied["changes"]), (0, "applied", planned["changes"]))
        self.assertEqual(
            tomllib.loads(self.live.read_text())["seat"]["JET"]["budget"], {"usd_per_day": 3.5}
        )

    def test_a_value_already_as_asked_is_no_change(self) -> None:
        new, changes = self.plan_text({"daemon.coalesce_seconds": 20, "seat.JET.wake": "http"})
        self.assertEqual((new, changes), (FIXTURE, []))

    def test_one_and_one_point_zero_are_different_values(self) -> None:
        new, changes = self.plan_text({"daemon.coalesce_seconds": 20.0})
        self.assertEqual(changes[0]["from"], 20)
        self.assertIn("coalesce_seconds = 20.0 ", new)
        self.assertFalse(M.same(1, True))
        self.assertFalse(M.same(1, 1.0))
        self.assertTrue(M.same({"a": [1, "x"]}, {"a": [1, "x"]}))

    def test_a_key_written_in_a_form_the_editor_does_not_rewrite_is_refused_not_duplicated(self) -> None:
        dotted = FIXTURE.replace(
            'budget = { usd_per_day = 2.0, board_per_day = 0 }', "budget.usd_per_day = 2.0"
        )
        with self.assertRaises(M.Refusal) as caught:
            self.plan_text({"seat.JET.budget": {"usd_per_day": 5.0}}, dotted)
        self.assertIn(caught.exception.code, ("edit_unsafe", "file_not_toml"))
        sub = FIXTURE + "\n[seat.ZC.live]\nper_hour = 3\n"
        with self.assertRaises(M.Refusal):
            self.plan_text({"seat.ZC.live": {"per_hour": 9}}, sub)

    def test_a_file_that_is_not_toml_is_refused(self) -> None:
        with self.assertRaises(M.Refusal) as caught:
            self.plan_text({"seat.ZC.enabled": False}, FIXTURE + "\n[seat.ZC\n")
        self.assertEqual(caught.exception.code, "file_not_toml")

    def test_crlf_line_endings_are_kept(self) -> None:
        text = FIXTURE.replace("\n", "\r\n")
        new, _ = self.plan_text({"seat.ZC.enabled": False, "daemon.coalesce_seconds": 9}, text)
        self.assertNotIn("\n", new.replace("\r\n", ""))
        self.assertIs(tomllib.loads(new)["seat"]["ZC"]["enabled"], False)

    def test_rendered_values_read_back_as_themselves(self) -> None:
        for value in (True, False, 0, -3, 2.5, 1e-07, "plain", 'q"uote\\', "ünï", [], [["a", "b"]], {"a": 1, "b": {"c": [1.0]}}, {}):
            with self.subTest(value=value):
                self.assertTrue(M.same(tomllib.loads("v = " + M.render(value))["v"], value))

    def test_parse_assignment_reads_toml_values(self) -> None:
        self.assertEqual(M.parse_assignment("seat.ZC.enabled=false"), ("seat.ZC.enabled", False))
        self.assertEqual(
            M.parse_assignment("seat.JET.budget={ usd_per_day = 2.0 }"),
            ("seat.JET.budget", {"usd_per_day": 2.0}),
        )
        self.assertEqual(M.parse_assignment('seat.ZC.wake="inbox"'), ("seat.ZC.wake", "inbox"))
        for bad in ("seat.ZC.enabled", "seat.ZC.wake=inbox", "a b=1", "x=1\ny=2", "=1"):
            with self.subTest(bad=bad):
                with self.assertRaises(M.Refusal):
                    M.parse_assignment(bad)


class GetAndPlanTests(Case):
    def test_get_answers_allowlisted_settings_only(self) -> None:
        answer, status = self.run_request("get")
        self.assertEqual((status, answer["result"]), (0, "read"))
        settings = answer["settings"]
        self.assertEqual(settings["daemon.coalesce_seconds"], 20)
        self.assertEqual(settings["seat.JET.wake"], "http")
        self.assertIs(settings["linear_comments.enabled"], False)
        text = json.dumps(answer)
        for hidden in ("owner_user_id", "eligible_user_ids", "creds", "instance", "note", "42"):
            self.assertNotIn(hidden, text.replace("listener.toml", ""))
        self.assertEqual(answer["file"], "listener.toml")
        self.assertNotIn(str(self.dir), text)
        self.assert_untouched()

    def test_plan_writes_nothing_and_shows_only_the_changed_lines(self) -> None:
        seen = {}

        def checker(candidate, live):
            # The candidate is somewhere else, never beside the live file.
            seen["dir"] = os.path.dirname(candidate)
            seen["text"] = Path(candidate).read_text(encoding="utf-8")
            seen["beside"] = sorted(os.listdir(self.dir))
            return {"ok": True, "problems": [], "warnings": 3}

        answer, status = self.run_request("plan", {"seat.ZC.enabled": False}, checker=checker)
        self.assertEqual((status, answer["result"]), (0, "planned"))
        self.assertNotEqual(os.path.realpath(seen["dir"]), os.path.realpath(self.dir))
        self.assertEqual(seen["beside"], ["listener.toml"])
        self.assertFalse(os.path.exists(seen["dir"]))
        self.assertEqual(hashlib.sha256(seen["text"].encode()).hexdigest(), answer["newSHA256"])
        self.assertEqual(answer["baseSHA256"], hashlib.sha256(self.before).hexdigest())
        self.assertEqual(answer["check"], {"ok": True, "problems": [], "warnings": 3})
        body = [l for l in answer["diff"] if not l.startswith(("---", "+++", "@@"))]
        self.assertEqual(body, ["+enabled = false"])
        self.assertNotIn("owner_user_id", "\n".join(answer["diff"]))
        self.assert_untouched()

    def test_plan_reports_a_result_the_listener_would_not_take(self) -> None:
        answer, status = self.run_request("plan", {"seat.ZC.enabled": False}, checker=bad_check)
        self.assertEqual(status, 0)
        self.assertIs(answer["check"]["ok"], False)
        self.assert_untouched()

    def test_plan_with_nothing_to_change_says_unchanged(self) -> None:
        answer, status = self.run_request("plan", {"seat.JET.wake": "http"})
        self.assertEqual((status, answer["result"], answer["changes"], answer["diff"]), (0, "unchanged", [], []))


class ApplyTests(Case):
    def test_apply_is_refused_while_the_allowlist_has_it_off(self) -> None:
        answer, status = self.run_request(
            "apply", {"seat.ZC.enabled": False}, allow=shipped_allowlist(apply_enabled=False)
        )
        self.assertEqual((status, answer["errorCode"]), (2, "apply_disabled"))
        self.assert_untouched()
        # The shipped file itself, unmodified:
        shipped = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        answer, status = self.run_request("apply", {"seat.ZC.enabled": False}, allow=shipped)
        self.assertEqual((status, answer["errorCode"]), (2, "apply_disabled"))
        self.assert_untouched()

    def test_plan_output_equals_the_applied_diff(self) -> None:
        wanted = {
            "seat.ZC.enabled": False,
            "daemon.coalesce_seconds": 31,
            "seat.JET.budget": {"usd_per_day": 1.5, "board_per_day": 0},
            "listener_api.enabled": True,
        }
        planned, status = self.run_request("plan", wanted)
        self.assertEqual(status, 0)
        self.assert_untouched()
        applied, status = self.run_request("apply", wanted, expectPlanSHA256=planned["planSHA256"])
        self.assertEqual((status, applied["result"]), (0, "applied"))
        for field in ("changes", "diff", "baseSHA256", "newSHA256", "planSHA256"):
            self.assertEqual(applied[field], planned[field], field)
        after = self.live.read_bytes()
        self.assertEqual(hashlib.sha256(after).hexdigest(), planned["newSHA256"])
        # The diff between the bytes before and the bytes now is the diff the plan printed.
        self.assertEqual(M.changed_lines(self.before.decode(), after.decode()), planned["diff"])
        doc = tomllib.loads(after.decode())
        self.assertIs(doc["seat"]["ZC"]["enabled"], False)
        self.assertEqual(doc["daemon"]["owner_user_id"], 42)
        self.assertEqual(applied["reload"]["code"], "reload_skipped")

    def test_apply_keeps_a_backup_of_the_old_bytes_and_the_files_mode(self) -> None:
        answer, status = self.run_request("apply", {"seat.ZC.enabled": False})
        self.assertEqual(status, 0)
        backup = self.dir / answer["backup"]
        self.assertTrue(answer["backup"].startswith("listener.toml.bak-settings-"))
        self.assertEqual(backup.read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o640)
        self.assertEqual(self.names(), sorted(["listener.toml", answer["backup"]]))

    def test_a_failed_check_config_leaves_the_file_byte_for_byte(self) -> None:
        seen = {}

        def checker(candidate, live):
            seen["candidate"] = candidate
            seen["live_during"] = Path(live).read_bytes()
            return bad_check(candidate, live)

        answer, status = self.run_request("apply", {"seat.ZC.enabled": False}, checker=checker)
        self.assertEqual((status, answer["result"], answer["errorCode"]), (2, "refused", "check_failed"))
        self.assertEqual(seen["live_during"], self.before)
        self.assertFalse(os.path.exists(seen["candidate"]))
        self.assertEqual(answer["check"]["problems"], ["seat.JET: something the listener would not take"])
        self.assertNotIn("backup", answer)
        self.assert_untouched()

    def test_a_check_that_cannot_run_writes_nothing(self) -> None:
        def checker(candidate, live):
            return M.daemon_check(candidate, live, scripts_dir=str(self.dir / "nowhere"))

        answer, status = self.run_request("apply", {"seat.ZC.enabled": False}, checker=checker)
        self.assertEqual((status, answer["errorCode"]), (2, "check_failed"))
        self.assertEqual(answer["check"]["problems"], ["check_unavailable"])
        self.assert_untouched()

    def test_a_refused_reload_rolls_back_byte_for_byte(self) -> None:
        calls = []

        def reloader(path):
            calls.append(Path(path).read_bytes())
            return {"ok": False, "confirmed": True, "code": "reload_refused"}

        answer, status = self.run_request(
            "apply", {"seat.ZC.enabled": False, "daemon.coalesce_seconds": 7}, reloader=reloader, reload="always"
        )
        self.assertEqual((status, answer["result"], answer["errorCode"]), (1, "failed", "reload_refused"))
        self.assertIs(answer["rolledBack"], True)
        self.assertEqual(self.live.read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o640)
        # The first reload saw the new file;  the second, after the rollback, the old one.
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], self.before)
        self.assertEqual(calls[1], self.before)
        self.assertEqual([n for n in self.names() if ".new-" in n or ".cand-" in n], [])

    def test_a_reload_that_raises_rolls_back_too(self) -> None:
        def reloader(path):
            raise RuntimeError("no listener")

        answer, status = self.run_request(
            "apply", {"seat.ZC.enabled": False}, reloader=reloader, reload="always"
        )
        self.assertEqual((status, answer["errorCode"]), (1, "reload_failed"))
        self.assertEqual(self.live.read_bytes(), self.before)

    def test_an_accepted_reload_keeps_the_new_file(self) -> None:
        answer, status = self.run_request(
            "apply",
            {"seat.ZC.enabled": False},
            reloader=lambda path: {"ok": True, "confirmed": True, "code": "reloaded"},
            reload="always",
        )
        self.assertEqual((status, answer["result"]), (0, "applied"))
        self.assertEqual(answer["reload"], {"ok": True, "confirmed": True, "code": "reloaded"})
        self.assertNotEqual(self.live.read_bytes(), self.before)

    def test_reload_auto_only_reloads_the_listeners_own_file(self) -> None:
        called = []
        old = os.environ.get(M.ENV_CONFIG)
        self.addCleanup(
            lambda: os.environ.pop(M.ENV_CONFIG, None) if old is None else os.environ.__setitem__(M.ENV_CONFIG, old)
        )
        os.environ[M.ENV_CONFIG] = str(self.dir / "another.toml")
        self.run_request(
            "apply", {"seat.ZC.enabled": False}, reloader=lambda p: called.append(p) or {"ok": True}
        )
        self.assertEqual(called, [])
        os.environ[M.ENV_CONFIG] = str(self.live)
        self.run_request(
            "apply", {"seat.ZC.enabled": True}, reloader=lambda p: called.append(p) or {"ok": True}
        )
        self.assertEqual(len(called), 1)

    def test_a_plan_hash_that_does_not_match_is_refused(self) -> None:
        planned, _ = self.run_request("plan", {"seat.ZC.enabled": False})
        answer, status = self.run_request(
            "apply", {"seat.ZC.enabled": True}, expectPlanSHA256=planned["planSHA256"]
        )
        self.assertEqual((status, answer["errorCode"]), (2, "plan_mismatch"))
        self.assert_untouched()
        # The same request against a file that changed since the plan:
        self.live.write_text(FIXTURE + "# edited by hand since the plan\n", encoding="utf-8")
        answer, status = self.run_request(
            "apply", {"seat.ZC.enabled": False}, expectPlanSHA256=planned["planSHA256"]
        )
        self.assertEqual((status, answer["errorCode"]), (2, "plan_mismatch"))
        answer, status = self.run_request("apply", {"seat.ZC.enabled": False}, expectPlanSHA256="abc")
        self.assertEqual((status, answer["errorCode"]), (2, "usage"))

    def test_a_file_that_changes_during_the_check_is_not_overwritten(self) -> None:
        other = FIXTURE + "# someone else wrote this during the check\n"

        def checker(candidate, live):
            Path(live).write_text(other, encoding="utf-8")
            return ok_check(candidate, live)

        answer, status = self.run_request("apply", {"seat.ZC.enabled": False}, checker=checker)
        self.assertEqual((status, answer["errorCode"]), (2, "changed_underneath"))
        self.assertEqual(self.live.read_text(encoding="utf-8"), other)
        self.assertEqual(self.names(), ["listener.toml"])

    def test_apply_with_nothing_to_change_writes_nothing(self) -> None:
        answer, status = self.run_request("apply", {"seat.JET.wake": "http"})
        self.assertEqual((status, answer["result"]), (0, "unchanged"))
        self.assert_untouched()

    def test_one_edit_at_a_time(self) -> None:
        with M._FolderLock(self.dir):
            answer, status = self.run_request("apply", {"seat.ZC.enabled": False})
        self.assertEqual((status, answer["errorCode"]), (2, "busy"))
        self.assert_untouched()
        answer, status = self.run_request("apply", {"seat.ZC.enabled": False})
        self.assertEqual(status, 0)

    def test_a_symlink_in_place_of_the_file_is_refused(self) -> None:
        real = self.dir / "real.toml"
        self.live.rename(real)
        os.symlink(real, self.live)
        answer, status = self.run_request("apply", {"seat.ZC.enabled": False})
        self.assertEqual((status, answer["errorCode"]), (2, "file_unavailable"))
        self.assertEqual(real.read_bytes(), self.before)

    def test_old_backups_are_pruned_and_two_in_one_second_do_not_collide(self) -> None:
        for n in range(M.BACKUPS_KEPT + 3):
            (self.dir / ("listener.toml.bak-settings-20200101T0000%02dZ" % n)).write_text("old")
        first, _ = self.run_request("apply", {"seat.ZC.enabled": False})
        second, _ = self.run_request("apply", {"seat.ZC.enabled": True})
        self.assertNotEqual(first["backup"], second["backup"])
        backups = [n for n in self.names() if ".bak-settings-" in n]
        self.assertEqual(len(backups), M.BACKUPS_KEPT)
        self.assertIn(first["backup"], backups)
        self.assertIn(second["backup"], backups)
        self.assertEqual((self.dir / first["backup"]).read_bytes(), self.before)


class DaemonCheckTests(Case):
    """The real `simple-chat daemon check-config`, on the image's sample, in a child process."""

    def setUp(self) -> None:
        super().setUp()
        self.live.write_bytes(SAMPLE.read_bytes())
        self.before = self.live.read_bytes()

    def test_the_real_check_takes_a_good_change(self) -> None:
        answer, status = M.run_request(
            self.request("plan", {"seat.GROK.enabled": True, "daemon.coalesce_seconds": 25}), path=self.live
        )
        self.assertEqual((status, answer["result"]), (0, "planned"), answer)
        self.assertEqual(answer["check"]["problems"], [])
        self.assertIs(answer["check"]["ok"], True)
        self.assert_untouched()

    def test_the_real_check_refuses_a_change_the_listener_would_not_take(self) -> None:
        # An http wake with no routine:  inside every allowlist range, and still a config the
        # listener reports an error for.  Only its own check knows that.
        answer, status = M.run_request(
            self.request("apply", {"seat.CLAUDE.wake": "http", "seat.CLAUDE.budget": {"usd_per_day": 0.5}}),
            path=self.live,
        )
        self.assertEqual((status, answer["errorCode"]), (2, "check_failed"), answer)
        self.assertTrue(answer["check"]["problems"])
        self.assert_untouched()

    def test_the_remote_mode_switch_does_not_reach_the_child(self) -> None:
        env = M._child_env(self.live, {"SIMPLE_CHAT_REMOTE": "1", "SIMPLE_CHAT_REMOTE_WRITES": "1", "PATH": "/usr/bin"})
        self.assertEqual(env, {"PATH": "/usr/bin", M.ENV_CONFIG: str(self.live)})

    def test_reload_readback(self) -> None:
        state = self.dir / "state"
        (state / "listener").mkdir(parents=True)
        status_file = state / "listener" / "status.json"
        env = {M.ENV_STATE_DIR: str(state)}
        clock = iter(range(1000, 2000))
        real_cli = M._cli
        self.addCleanup(setattr, M, "_cli", real_cli)

        def reload_with(document, exit_status=0):
            M._cli = lambda *a, **k: (exit_status, "")
            if document is None:
                status_file.unlink(missing_ok=True)
            else:
                status_file.write_text(json.dumps(document))
            return M.daemon_reload(self.live, environ=env, clock=lambda: next(clock), sleep=lambda s: None)

        self.assertEqual(reload_with({"reload_at": 5000, "reload_refused": []})["code"], "reloaded")
        self.assertEqual(
            reload_with({"reload_at": 5000, "reload_refused": ["seat MA is configured here"]}),
            {"ok": False, "confirmed": True, "code": "reload_refused"},
        )
        self.assertEqual(
            reload_with({"reload_at": 1, "reload_refused": []}),
            {"ok": True, "confirmed": False, "code": "reload_unconfirmed"},
        )
        self.assertEqual(reload_with(None)["code"], "reload_unconfirmed")
        self.assertEqual(
            reload_with({"reload_at": 5000}, exit_status=1),
            {"ok": False, "confirmed": False, "code": "reload_not_sent"},
        )


class RequestProtocolTests(Case):
    def test_main_reads_one_request_and_writes_one_answer(self) -> None:
        old = os.environ.get(M.ENV_CONFIG)
        self.addCleanup(
            lambda: os.environ.pop(M.ENV_CONFIG, None) if old is None else os.environ.__setitem__(M.ENV_CONFIG, old)
        )
        os.environ[M.ENV_CONFIG] = str(self.live)
        out = io.StringIO()
        code = M.main(["--request-stdin"], io.BytesIO(json.dumps(self.request("get")).encode()), out)
        answer = json.loads(out.getvalue())
        self.assertEqual((code, answer["tool"], answer["result"]), (0, "listener-settings", "read"))
        for bad in (b"", b"not json", b"[]", b"x" * (M.REQUEST_MAX_BYTES + 5)):
            out = io.StringIO()
            self.assertEqual(M.main(["--request-stdin"], io.BytesIO(bad), out), 2)
            self.assertEqual(json.loads(out.getvalue())["result"], "refused")
        out = io.StringIO()
        self.assertEqual(M.main(["plan", "--file", "/etc/passwd"], io.BytesIO(b""), out), 2)
        self.assert_untouched()

    def test_the_module_imports_nothing_from_simple_tools(self) -> None:
        # fleet-ops-control loads it from its bytes and the container runs it with `python3 -I`.
        source = (SCRIPTS / "simple_tools" / "server" / "listener_settings_edit.py").read_text()
        self.assertNotIn("from simple_tools", source.split('"""', 2)[2].split("_CLI_SHIM")[0])
        self.assertNotIn("import simple_tools", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)


def load_control():
    spec = importlib.util.spec_from_file_location(
        "fleet_ops_control_for_listener_settings", SCRIPTS / "fleet-ops-control.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeReader:
    """Stands in for `docker exec`:  records the call and answers what a test sets."""

    def __init__(self, document=None, status=0, err=b"", raw=None, over=False):
        self.calls = []
        self.document, self.status, self.err, self.raw, self.over = document, status, err, raw, over

    def __call__(self, argv, timeout, env, max_bytes, data=None):
        self.calls.append({"argv": list(argv), "env": dict(env), "data": data, "timeout": timeout})
        out = self.raw if self.raw is not None else json.dumps(self.document).encode()
        return self.status, out, self.err, self.over


class ControlVerbTests(Case):
    """`fleet-ops-control.py listener-settings` and `server listener-settings`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.foc = load_control()

    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir(mode=0o700)

    def ctx(self, platform="darwin", reader=None, stdin=b"", environ=None):
        return self.foc.Context(
            platform=platform,
            uid=os.getuid(),
            home=self.home,
            environ=environ or {"PATH": "/usr/bin:/bin"},
            reader=reader or FakeReader({"tool": "listener-settings", "ok": True, "result": "read", "settings": {}}),
            stdin=lambda: stdin,
        )

    def run_main(self, argv, ctx=None):
        out = io.StringIO()
        code = self.foc.main(list(argv), ctx=ctx or self.ctx(), out=out)
        text = out.getvalue()
        return code, (json.loads(text) if text.startswith("{") else text)

    def audit_rows(self):
        path = self.home / ".local" / "state" / "fleet-tools" / "ops-audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def allow_apply(self):
        doc = shipped_allowlist(apply_enabled=True)
        real = self.foc.load_listener_allowlist
        self.addCleanup(setattr, self.foc, "load_listener_allowlist", real)
        self.foc.load_listener_allowlist = lambda ctx: copy.deepcopy(doc)

    # -- the command-line verb, on a file -----------------------------------------------------

    def test_plan_on_a_fixture_prints_a_diff_and_writes_nothing(self) -> None:
        code, text = self.run_main(
            ["listener-settings", "plan", "--set", "seat.ZC.enabled=false", "--file", str(self.live)]
        )
        self.assertEqual(code, 0, text)
        self.assertIn("planned: listener-settings plan", text)
        self.assertIn("+enabled = false", text)
        self.assertIn("@@", text)
        self.assert_untouched()
        self.assertEqual(self.audit_rows(), [])

    def test_plan_as_json_carries_the_editors_answer(self) -> None:
        code, answer = self.run_main(
            ["listener-settings", "plan", "--set", "daemon.coalesce_seconds=33", "--file", str(self.live), "--json"]
        )
        self.assertEqual((code, answer["ok"], answer["result"]), (0, True, "planned"))
        inner = answer["listenerSettings"]
        self.assertEqual(inner["changes"], [{"key": "daemon.coalesce_seconds", "set": True, "from": 20, "to": 33, "removes": []}])
        self.assertEqual(answer["commands"], [])
        self.assert_untouched()

    def test_the_shipped_allowlist_refuses_every_apply(self) -> None:
        code, answer = self.run_main(
            ["listener-settings", "apply", "--set", "seat.ZC.enabled=false", "--file", str(self.live), "--yes", "--json"]
        )
        self.assertEqual((code, answer["result"], answer["errorCode"]), (2, "refused", "apply_disabled"))
        self.assert_untouched()
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["verb"], rows[0]["target"], rows[0]["result"]), ("listener-settings apply", "listener settings", "refused"))

    def test_apply_on_a_file_when_the_owner_has_turned_it_on(self) -> None:
        self.allow_apply()
        _, planned = self.run_main(
            ["listener-settings", "plan", "--set", "seat.ZC.enabled=false", "--file", str(self.live), "--json"]
        )
        code, answer = self.run_main(
            [
                "listener-settings", "apply", "--set", "seat.ZC.enabled=false", "--file", str(self.live),
                "--expect", planned["listenerSettings"]["planSHA256"], "--yes", "--json",
            ]
        )
        self.assertEqual((code, answer["result"]), (0, "applied"), answer)
        self.assertEqual(answer["listenerSettings"]["diff"], planned["listenerSettings"]["diff"])
        self.assertIs(tomllib.loads(self.live.read_text())["seat"]["ZC"]["enabled"], False)
        rows = self.audit_rows()
        self.assertEqual((rows[-1]["result"], rows[-1]["id"]), ("applied", answer["auditID"]))
        # The audit names no setting and no value.
        self.assertNotIn("ZC", json.dumps(rows))
        self.assertNotIn("enabled", json.dumps(rows))

    def test_remove_reaches_the_editor_from_the_command_line(self) -> None:
        argv = ["listener-settings", "plan", "--set", "seat.JET.budget={ usd_per_day = 3.5 }", "--file", str(self.live), "--json"]
        code, answer = self.run_main(argv)
        self.assertEqual((code, answer["errorCode"]), (2, "would_remove_keys"))
        code, answer = self.run_main(argv + ["--remove", "seat.JET.budget.board_per_day"])
        self.assertEqual((code, answer["result"]), (0, "planned"), answer)
        self.assertEqual(
            answer["listenerSettings"]["changes"][0]["removes"], ["seat.JET.budget.board_per_day"]
        )
        self.assertIn("removes: seat.JET.budget.board_per_day", answer["output"])
        self.assert_untouched()

    def test_usage_refusals(self) -> None:
        file = str(self.live)
        for argv in (
            ["listener-settings"],
            ["listener-settings", "delete", "--file", file],
            ["listener-settings", "apply", "--set", "seat.ZC.enabled=false", "--file", file],
            ["listener-settings", "plan", "--file", file],
            ["listener-settings", "plan", "--set", "seat.ZC.enabled=false", "--file", file, "--yes"],
            ["listener-settings", "get", "--set", "seat.ZC.enabled=false", "--file", file],
            ["listener-settings", "plan", "--set", "seat.ZC.enabled", "--file", file],
            ["listener-settings", "plan", "--set", "seat.ZC.enabled=false", "--set", "seat.ZC.enabled=true", "--file", file],
            ["listener-settings", "plan", "--set", "seat.ZC.enabled=false", "--file", file, "--shell"],
            ["listener-settings", "plan", "--set", "seat.ZC.enabled=false"],
            ["listener-settings", "plan", "--set", "daemon.owner_user_id=1", "--file", file],
            ["listener-settings", "plan", "--set", "seat.ZC.live=false", "--file", file],
        ):
            with self.subTest(argv=argv[1:]):
                code, answer = self.run_main(argv + ["--json"])
                self.assertEqual((code, answer["result"]), (2, "refused"), answer)
        self.assert_untouched()

    def test_an_untrusted_or_missing_allowlist_refuses(self) -> None:
        real = self.foc.LISTENER_ALLOWLIST_NAME
        self.addCleanup(setattr, self.foc, "LISTENER_ALLOWLIST_NAME", real)
        self.foc.LISTENER_ALLOWLIST_NAME = "no-such-allowlist.json"
        code, answer = self.run_main(["listener-settings", "get", "--file", str(self.live), "--json"])
        self.assertEqual((code, answer["errorCode"]), (2, "allowlist_unavailable"))

    # -- the ssh gate ---------------------------------------------------------------------------

    def test_the_ssh_gate_takes_the_server_verb_and_nothing_of_a_request(self) -> None:
        words = self.foc.parse_ssh_command("server listener-settings server --plan --json")
        self.assertEqual(words[:3], ["server", "listener-settings", "server"])
        for line in (
            "listener-settings plan --set seat.ZC.enabled=false --file x",
            "listener-settings get --file x",
            "server listener-settings server --plan --file x",
            "server listener-settings server --plan --set seat.ZC.enabled=false",
            "server listener-settings server --yes --expect abc",
            "server listener-settings server",
        ):
            with self.subTest(line=line):
                with self.assertRaises(self.foc.Refused):
                    self.foc.parse_ssh_command(line)

    def test_from_ssh_cannot_reach_the_file_form(self) -> None:
        ctx = self.ctx(
            platform="linux",
            environ={"SSH_ORIGINAL_COMMAND": "listener-settings get --file " + str(self.live)},
        )
        code, answer = self.run_main(["--from-ssh"], ctx)
        self.assertEqual((code, answer["result"]), (2, "refused"))
        code, answer = self.run_main(["listener-settings", "get", "--from-ssh"], self.ctx())
        self.assertEqual((code, answer["result"] if isinstance(answer, dict) else "refused"), (2, "refused"))

    # -- the server route -----------------------------------------------------------------------

    EXPECTED_ARGV_TAIL = [
        "exec", "-i", "--user", "simpletools", "eavesdropper", "/usr/local/bin/python3", "-I",
        "/app/scripts/simple_tools/server/listener_settings_edit.py", "--request-stdin",
    ]

    def server(self, mode, stdin=b"", reader=None, **kw):
        reader = reader or FakeReader({"tool": "listener-settings", "ok": True, "result": "read", "settings": {"daemon.coalesce_seconds": 20}})
        ctx = self.ctx(platform="linux", reader=reader, stdin=stdin)
        answer, code = self.foc.handle(ctx, "server", "listener-settings", "server", mode, **kw)
        return answer, code, reader

    def test_a_get_runs_one_fixed_docker_argv_with_the_request_on_stdin(self) -> None:
        answer, code, reader = self.server("plan")
        self.assertEqual((code, answer["ok"], answer["result"]), (0, True, "read"))
        self.assertEqual(len(reader.calls), 1)
        call = reader.calls[0]
        self.assertEqual(call["argv"][1:], self.EXPECTED_ARGV_TAIL)
        self.assertIn(os.path.basename(call["argv"][0]), ("docker",))
        self.assertEqual(sorted(call["env"]), ["DOCKER_CONFIG", "DOCKER_HOST", "HOME", "PATH"])
        request = json.loads(call["data"])
        self.assertEqual(request["action"], "get")
        self.assertIs(request["allowlist"]["applyEnabled"], False)
        self.assertNotIn("set", request)
        self.assertEqual(self.audit_rows(), [])

    def test_a_plan_sends_the_changes_on_stdin_and_never_in_the_argv(self) -> None:
        document = {"tool": "listener-settings", "ok": True, "result": "planned", "changes": [{"key": "seat.ZC.enabled"}], "diff": ["+enabled = false"], "check": {"ok": True, "problems": []}}
        stdin = json.dumps({"set": {"seat.ZC.enabled": False}}).encode()
        answer, code, reader = self.server("plan", stdin, FakeReader(document))
        self.assertEqual((code, answer["result"]), (0, "planned"))
        call = reader.calls[0]
        self.assertEqual(call["argv"][1:], self.EXPECTED_ARGV_TAIL)
        self.assertNotIn("ZC", " ".join(call["argv"]))
        request = json.loads(call["data"])
        self.assertEqual((request["action"], request["set"]), ("plan", {"seat.ZC.enabled": False}))
        self.assertEqual(answer["listenerSettings"], document)
        self.assertEqual(answer["output"], "+enabled = false\n")
        self.assertEqual(self.audit_rows(), [])

    def test_a_remove_list_on_stdin_is_passed_on(self) -> None:
        document = {"tool": "listener-settings", "ok": True, "result": "planned", "changes": [], "diff": []}
        stdin = json.dumps(
            {"set": {"seat.JET.budget": {"usd_per_day": 3.5}}, "remove": ["seat.JET.budget.board_per_day"]}
        ).encode()
        answer, code, reader = self.server("plan", stdin, FakeReader(document))
        self.assertEqual(code, 0)
        request = json.loads(reader.calls[0]["data"])
        self.assertEqual(request["remove"], ["seat.JET.budget.board_per_day"])
        answer, code, reader = self.server("plan", b'{"set": {"seat.ZC.enabled": false}, "remove": "x"}')
        self.assertEqual((code, answer["result"], reader.calls), (2, "refused", []))

    def test_an_apply_is_audited_without_settings_or_values(self) -> None:
        document = {"tool": "listener-settings", "ok": True, "result": "applied", "changes": [{"key": "seat.ZC.enabled"}], "diff": [], "reload": {"ok": True, "code": "reloaded"}}
        sha = "a" * 64
        stdin = json.dumps({"set": {"seat.ZC.enabled": False}, "expectPlanSHA256": sha}).encode()
        answer, code, reader = self.server("yes", stdin, FakeReader(document))
        self.assertEqual((code, answer["result"]), (0, "applied"))
        request = json.loads(reader.calls[0]["data"])
        self.assertEqual((request["action"], request["expectPlanSHA256"]), ("apply", sha))
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["verb"], rows[0]["target"], rows[0]["result"]), ("server listener-settings", "listener settings", "applied"))
        self.assertNotIn("ZC", json.dumps(rows))
        self.assertEqual(answer["auditID"], rows[0]["id"])

    def test_the_editors_refusal_comes_back_as_a_refusal(self) -> None:
        document = {"tool": "listener-settings", "ok": False, "result": "refused", "errorCode": "apply_disabled", "detail": "apply is off"}
        stdin = json.dumps({"set": {"seat.ZC.enabled": False}}).encode()
        answer, code, _ = self.server("yes", stdin, FakeReader(document, status=2))
        self.assertEqual((code, answer["ok"], answer["result"], answer["errorCode"]), (2, False, "refused", "apply_disabled"))
        self.assertEqual(self.audit_rows()[0]["result"], "refused")

    def test_bad_requests_on_stdin_are_refused_before_docker(self) -> None:
        for mode, stdin in (
            ("yes", b""),
            ("plan", b"not json"),
            ("plan", b"[]"),
            ("plan", b'{"set": {}}'),
            ("plan", b'{"set": {"seat.ZC.enabled": false}, "file": "/etc/passwd"}'),
            ("plan", b'{"set": {"seat.ZC.enabled": false}, "allowlist": {"applyEnabled": true}}'),
            ("plan", b'{"set": {"seat.ZC.enabled": false}, "expectPlanSHA256": "' + b"a" * 64 + b'"}'),
            ("plan", None),
        ):
            with self.subTest(mode=mode, stdin=stdin):
                answer, code, reader = self.server(mode, stdin)
                self.assertEqual((code, answer["result"]), (2, "refused"))
                self.assertEqual(reader.calls, [])

    def test_only_on_the_server_only_its_target_and_not_from_muster(self) -> None:
        ctx = self.ctx(platform="darwin")
        answer, code = self.foc.handle(ctx, "server", "listener-settings", "server", "plan")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        ctx = self.ctx(platform="linux")
        answer, code = self.foc.handle(ctx, "server", "listener-settings", "mac", "plan")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        answer, code = self.foc.handle(
            ctx, "server", "listener-settings", "server", "plan", require_approval=True
        )
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(ctx.reader.calls, [])

    def test_docker_failures_are_fixed_words(self) -> None:
        for reader, word in (
            (FakeReader(raw=b"", status=1, err=b"Error response from daemon: container abc is not running"), "container_not_running"),
            (FakeReader(raw=b"python3: can't open file", status=2), "editor_unavailable"),
            (FakeReader(raw=b'{"tool": "something-else"}', status=0), "editor_unavailable"),
            (FakeReader(raw=b"", status=127), "docker_unavailable"),
            (FakeReader(raw=b"", status=124), "timeout"),
            (FakeReader(raw=b"", status=-9, over=True), "too_large"),
            (FakeReader({"tool": "listener-settings", "ok": True, "result": "exploded"}), "editor_failed"),
        ):
            with self.subTest(word=word):
                answer, code, _ = self.server("plan", reader=reader)
                self.assertEqual((code, answer["result"], answer["errorCode"]), (1, "failed", word))
                self.assertNotIn("Error response", json.dumps(answer))

    def test_the_bounded_reader_gives_the_child_its_input(self) -> None:
        status, out, err, over = self.foc.run_bounded_read(
            ["/bin/cat"], timeout=10, env={"PATH": "/usr/bin:/bin"}, max_bytes=1024, data=b'{"a": 1}'
        )
        self.assertEqual((status, out, over), (0, b'{"a": 1}', False))
        status, out, err, over = self.foc.run_bounded_read(
            ["/bin/cat"], timeout=10, env={"PATH": "/usr/bin:/bin"}, max_bytes=1024
        )
        self.assertEqual((status, out, over), (0, b"", False))

    def test_the_bounded_reader_carries_on_after_a_short_write(self) -> None:
        # Far more than a pipe holds, so no single write can take it, and `cat` fills its output
        # while its input is still coming:  the input and the output have to move together.
        big = bytes(range(256)) * 4096
        status, out, err, over = self.foc.run_bounded_read(
            ["/bin/cat"], timeout=30, env={"PATH": "/usr/bin:/bin"}, max_bytes=2 * len(big), data=big
        )
        self.assertEqual((status, over, len(out)), (0, False, len(big)))
        self.assertEqual(out, big)

    def test_the_bounded_reader_does_not_wait_on_a_child_that_never_reads(self) -> None:
        status, out, err, over = self.foc.run_bounded_read(
            ["/bin/sleep", "30"], timeout=1, env={"PATH": "/usr/bin:/bin"}, max_bytes=1024, data=b"x" * (1 << 20)
        )
        self.assertEqual((status, out, over), (124, b"", False))
        # A child that ends without reading its input is told by its own exit status.
        status, out, err, over = self.foc.run_bounded_read(
            ["/usr/bin/true"], timeout=10, env={"PATH": "/usr/bin:/bin"}, max_bytes=1024, data=b"x" * (1 << 20)
        )
        self.assertEqual((status, out, over), (0, b"", False))
        status, out, err, over = self.foc.run_bounded_read(
            ["/bin/cat"], timeout=10, env={"PATH": "/usr/bin:/bin"}, max_bytes=1024, data=b""
        )
        self.assertEqual((status, out, over), (0, b"", False))

    def test_the_helper_still_parses_on_python_3_9(self) -> None:
        import ast

        source = (SCRIPTS / "fleet-ops-control.py").read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))
        self.assertNotIn("import tomllib", source)


if __name__ == "__main__":
    unittest.main()
