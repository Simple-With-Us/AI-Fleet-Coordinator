"""The notifier's reading of the K1 knobs through the real `fleet_knobs` against a fake `infisical` (hermetic:  a temp HOME,
the fake CLI first and alone on PATH, and the library's fallback directories patched out, as fleet_knobs's own tests do)."""
from __future__ import annotations

import unittest

import fleet_knobs
from fleet_knobs import KnobError
from fleet_knobs.tests._fake import KnobTestCase
from needs_owner import bar, notifier
from needs_owner.errors import NeedsOwnerError

OWNER = "0b1c2d3e-0000-4000-8000-00000000aaaa"


class RealKnobsTests(KnobTestCase):
    def test_the_settings_come_from_infisical(self) -> None:
        self.fake.set(secrets={"NEEDS_OWNER_NOTIFY_PAUSED": "true", "NEEDS_OWNER_CAPS": "urgent_per_hour=1,per_hour=2,per_day=3",
                               "OWNER_LINEAR_USER_ID": OWNER.upper()})
        got = notifier.read_settings(notifier.FleetKnobs())
        self.assertEqual((got.paused, got.caps, got.owner_linear_id, got.stale),
                         (True, {"urgent_per_hour": 1, "per_hour": 2, "per_day": 3}, OWNER, False))
        self.assertEqual(len(self.fake.calls()), 1, "one fetch per run")

    def test_unset_knobs_take_the_registry_defaults(self) -> None:
        self.fake.set(secrets={})
        got = notifier.read_settings(notifier.FleetKnobs())
        self.assertEqual((got.paused, got.caps, got.owner_linear_id), (False, {"urgent_per_hour": 3, "per_hour": 6, "per_day": 30}, ""))

    def test_an_unreachable_infisical_reads_the_last_known_copy_and_says_stale(self) -> None:
        self.fake.set(secrets={"NEEDS_OWNER_NOTIFY_PAUSED": "true", "OWNER_LINEAR_USER_ID": OWNER})
        notifier.read_settings(notifier.FleetKnobs())
        self.new_process()
        self.fake.set(mode="fail")
        got = notifier.read_settings(notifier.FleetKnobs())
        self.assertTrue(got.stale)
        self.assertTrue(got.paused, "the pause survives an outage")
        self.assertEqual(got.owner_linear_id, OWNER)

    def test_with_no_copy_at_all_an_outage_gives_the_defaults(self) -> None:
        self.fake.set(mode="fail")
        got = notifier.read_settings(notifier.FleetKnobs())
        self.assertEqual((got.paused, got.owner_linear_id), (False, ""))

    def test_a_refused_environment_stops_the_run(self) -> None:
        import os

        os.environ["INFISICAL_ENV"] = "staging"
        self.addCleanup(os.environ.pop, "INFISICAL_ENV", None)
        with self.assertRaises(notifier.NotifierStop) as caught:
            notifier.read_settings(notifier.FleetKnobs())
        self.assertIsInstance(caught.exception, NeedsOwnerError)
        self.assertIn("nothing was sent", str(caught.exception))
        self.assertEqual(self.fake.calls(), [], "no fetch was made")
        self.assertTrue(issubclass(KnobError, Exception))

    def test_the_knobs_this_unit_reads_are_registered(self) -> None:
        for name in ("NEEDS_OWNER_NOTIFY_PAUSED", "NEEDS_OWNER_CAPS", "OWNER_LINEAR_USER_ID", bar.MODE_KNOB, bar.CAP_KNOB):
            self.assertIn(name, fleet_knobs.BY_NAME)


class BarKnobsTests(KnobTestCase):
    """The owner bar's two knobs through the real `fleet_knobs` against a fake `infisical` (`bar.read_policy()` with no reader
    given is what `needs-owner file` runs)."""

    def test_the_bar_comes_from_infisical(self) -> None:
        self.fake.set(secrets={"NEEDS_OWNER_BAR": "ENFORCE", "NEEDS_OWNER_SEAT_DAILY_MAX": "3"})
        self.assertEqual(bar.read_policy(), bar.Policy("enforce", 3))
        self.assertEqual(len(self.fake.calls()), 1, "one fetch per run")

    def test_unset_knobs_are_off_with_no_cap(self) -> None:
        self.fake.set(secrets={})
        self.assertEqual(bar.read_policy(), bar.Policy("off", 0))

    def test_an_invalid_value_is_off_and_an_invalid_cap_is_no_cap(self) -> None:
        for bad in ("bogus", "warning", "on", "enforce please"):
            self.new_process()
            self.fake.set(secrets={"NEEDS_OWNER_BAR": bad, "NEEDS_OWNER_SEAT_DAILY_MAX": "5"})
            self.assertEqual(bar.read_policy(), bar.Policy("off", 0), bad)
        for bad in ("51", "-1", "many", "2.5"):
            self.new_process()
            self.fake.set(secrets={"NEEDS_OWNER_BAR": "warn", "NEEDS_OWNER_SEAT_DAILY_MAX": bad})
            self.assertEqual(bar.read_policy(), bar.Policy("warn", 0), bad)

    def test_an_outage_with_no_copy_is_off(self) -> None:
        self.fake.set(mode="fail")
        self.assertEqual(bar.read_policy(), bar.Policy("off", 0))

    def test_an_outage_keeps_the_last_known_bar(self) -> None:
        self.fake.set(secrets={"NEEDS_OWNER_BAR": "enforce", "NEEDS_OWNER_SEAT_DAILY_MAX": "4"})
        self.assertEqual(bar.read_policy(), bar.Policy("enforce", 4))
        self.new_process()
        self.fake.set(mode="fail")
        self.assertEqual(bar.read_policy(), bar.Policy("enforce", 4), "the last-known-good copy still holds it")

    def test_a_refused_environment_is_off_and_fetches_nothing(self) -> None:
        import os

        os.environ["INFISICAL_ENV"] = "staging"
        self.addCleanup(os.environ.pop, "INFISICAL_ENV", None)
        self.fake.set(secrets={"NEEDS_OWNER_BAR": "enforce"})
        self.assertEqual(bar.read_policy(), bar.Policy("off", 0))
        self.assertEqual(self.fake.calls(), [])

    def test_the_bar_knobs_are_not_the_notifiers(self) -> None:
        self.fake.set(secrets={"NEEDS_OWNER_BAR": "enforce", "NEEDS_OWNER_SEAT_DAILY_MAX": "2"})
        got = notifier.read_settings(notifier.FleetKnobs())
        self.assertEqual((got.paused, got.caps, got.owner_linear_id), (False, {"urgent_per_hour": 3, "per_hour": 6, "per_day": 30}, ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
