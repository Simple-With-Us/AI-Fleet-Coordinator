"""A seat's own bracketed title tag, pinned in the reviewed policy.

The relay derives a seat's tag from its App slug, so the MM seat, whose App is
swu-minimax, would open every title it creates with [MINIMAX] and not with the
[MM] its own artifacts carry.  The policy file can pin the tag, and the runtime
already honours it;  these checks hold the pinned tag, the shape the deploy tool
accepts, and the diagnostic the relay gives when it cannot read the policy file
at all.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay.policy import PolicyError, SeatPolicy, effective_title_tag
from git_relay.policy_input import seats_json_from_env

RELAY = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("seat_policy", RELAY / "deploy" / "seat_policy.py")
seat_policy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = seat_policy  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(seat_policy)


class SeatTitleTagTests(unittest.TestCase):
    def setUp(self):
        self.seats = seat_policy.render(seat_policy.load())
        self.policy = SeatPolicy(seat_policy.compact(self.seats))

    def test_the_mm_seat_keeps_its_own_tag(self):
        """The MM seat's App slug is swu-minimax, so the tag has to be pinned."""
        self.assertEqual(self.seats["swu-minimax"]["identity"], "MM")
        self.assertEqual(self.policy.title_tag("swu-minimax"), "MM")

    def test_a_title_that_already_carries_the_tag_is_left_alone(self):
        title = "[MM:  TT.1.M] Add the Mac launchd keeper for the Code mirror job"
        applied, changed = self.policy.apply_title_tag("swu-minimax", title)
        self.assertEqual(applied, title)
        self.assertFalse(changed)

    def test_full_session_prefix_preserves_exact_hair_spaces(self):
        title = "[ZC:" + "\u200a" * 3 + "RR.3.S] Fix"
        applied, changed = self.policy.apply_title_tag("swu-zc", title)
        self.assertEqual(applied.encode(), title.encode())
        self.assertFalse(changed)

    def test_a_title_without_the_tag_gets_it(self):
        applied, changed = self.policy.apply_title_tag("swu-minimax", "Add the keeper")
        self.assertEqual(applied, "[MM] Add the keeper")
        self.assertTrue(changed)

    def test_another_seats_bracket_is_not_mistaken_for_this_one(self):
        applied, changed = self.policy.apply_title_tag("swu-minimax", "[ECHO] Something else")
        self.assertEqual(applied, "[MM] [ECHO] Something else")
        self.assertTrue(changed)

    def test_the_policy_file_may_pin_a_tag_and_a_bad_one_is_refused(self):
        doc = seat_policy.load()
        for row in doc["seats"]:
            if row.get("identity") == "MM":
                row["title_tag"] = "MM"
        self.assertEqual(seat_policy.render(doc)["swu-minimax"]["title_tag"], "MM")
        for bad in ("mm", "MINIMAX!", 7, ""):
            with self.subTest(bad=bad):
                doc = seat_policy.load()
                for row in doc["seats"]:
                    if row.get("identity") == "MM":
                        row["title_tag"] = bad
                with self.assertRaises(seat_policy.PolicyFileError):
                    seat_policy.render(doc)

    def test_a_colliding_title_tag_is_refused_before_it_reaches_the_relay(self):
        """Two seats may not pin the same tag:  the relay refuses it at startup."""
        doc = seat_policy.load()
        rows = doc["seats"]
        rows[0]["title_tag"] = "CLASH"
        rows[1]["title_tag"] = "CLASH"
        with self.assertRaises(seat_policy.PolicyFileError) as error:
            seat_policy.render(doc)
        self.assertIn("share title tag", str(error.exception))

    def test_a_title_tag_that_extends_another_is_refused(self):
        """A tag that extends another seat's tag would make the bracket ambiguous."""
        for tags in (("ZTEST", "ZTEST SS"),):
            with self.subTest(tags=tags):
                doc = seat_policy.load()
                rows = doc["seats"]
                for row, tag in zip(rows[:2], tags):
                    row["title_tag"] = tag
                with self.assertRaises(seat_policy.PolicyFileError) as error:
                    seat_policy.render(doc)
                self.assertIn("extends", str(error.exception))

    def test_a_seat_whose_app_slug_is_not_a_string_is_refused(self):
        """A malformed policy file is refused on the documented error path."""
        doc = seat_policy.load()
        doc["seats"][0]["app_slug"] = 7
        with self.assertRaises(seat_policy.PolicyFileError) as error:
            seat_policy.render(doc)
        self.assertIn("app_slug must be a string", str(error.exception))

    def test_the_derivation_refuses_a_seat_name_that_is_not_a_string(self):
        """The runtime's own derivation raises PolicyError, never an AttributeError."""
        with self.assertRaises(PolicyError) as error:
            effective_title_tag(7)
        self.assertNotIsInstance(error.exception, AttributeError)

    def test_the_rendered_policy_is_one_the_relay_accepts(self):
        """What renders is what the runtime loads:  the same tag rules, one place."""
        doc = seat_policy.load()
        for row in doc["seats"]:
            if row.get("identity") == "MM":
                row["title_tag"] = "MM"
        rendered = seat_policy.render(doc)
        policy = SeatPolicy(seat_policy.compact(rendered))
        self.assertEqual(policy.title_tag("swu-minimax"), "MM")

    def test_the_permission_hint_names_the_ownership_and_mode_it_enforces(self):
        """The hint must not promise a root-owned file the service cannot read."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seats.json"
            path.write_bytes(b"{}")
            path.chmod(0o600)
            env = {"GIT_RELAY_SEATS_FILE": str(path)}
            with (
                patch("git_relay.policy_input.os.open", side_effect=PermissionError("denied")),
                self.assertRaises(PolicyError) as error,
            ):
                seats_json_from_env(env)
            message = str(error.exception)
        self.assertIn("0400 or 0600", message)
        self.assertIn(str(os.geteuid()), message)
        self.assertNotIn("root", message)

    def test_an_unreadable_policy_file_names_the_service_uid(self):
        """The relay serves as its own user, so a root-owned private file is refused."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seats.json"
            path.write_bytes(b"{}")
            path.chmod(0o600)
            env = {"GIT_RELAY_SEATS_FILE": str(path)}
            with (
                patch("git_relay.policy_input.os.open", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(PolicyError, "not readable by this service") as error,
            ):
                seats_json_from_env(env)
            self.assertIn(str(os.geteuid()), str(error.exception))
            self.assertNotIn(tmp, str(error.exception))
