"""The single activity definition:  floors, boundaries, sweepers, unknown, and the updated_at ban."""
import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet_facts import activity as A

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def signals(**sources) -> dict:
    base = A.empty_signals()
    base.update(sources)
    return base


def ok(*events) -> dict:
    return {"ok": True, "events": list(events)}


def comment(hours: float, **extra) -> dict:
    return dict({"at": ago(hours), "author": "CLAUDE", "sweeper": False}, **extra)


class FloorAndBoundaries(unittest.TestCase):
    def test_launch_at_is_a_floor_for_an_ancient_claim(self):
        # Claim and every signal are 300 hours old, but expiry launched 10 hours ago:  the clock starts at launch.
        item = {"signals": signals(linear_comments=ok(comment(300))), "launch_at": ago(10), "claimed_at": ago(300)}
        self.assertEqual(A.activity_at(item["signals"], item["launch_at"], item["claimed_at"]),
                         NOW - timedelta(hours=10))
        self.assertEqual(A.idle_band(item, NOW), A.BAND_NONE)
        # ... and 36 hours after launch it pings, not before.
        self.assertEqual(A.idle_band(item, NOW + timedelta(hours=25)), A.BAND_NONE)
        self.assertEqual(A.idle_band(item, NOW + timedelta(hours=26)), A.BAND_36H)

    def test_a_signal_newer_than_the_floor_wins(self):
        sig = signals(branch_push={"ok": True, "times": [ago(2)]})
        self.assertEqual(A.activity_at(sig, ago(10)), NOW - timedelta(hours=2))

    def test_band_boundaries(self):
        def band(hours):
            return A.idle_band({"signals": signals(linear_comments=ok(comment(hours)))}, NOW)
        self.assertEqual(band(35), A.BAND_NONE)
        self.assertEqual(band(35.99), A.BAND_NONE)
        self.assertEqual(band(36), A.BAND_36H)
        self.assertEqual(band(47), A.BAND_36H)
        self.assertEqual(band(47.99), A.BAND_36H)
        self.assertEqual(band(48), A.BAND_48H)
        self.assertEqual(band(300), A.BAND_48H)

    def test_a_future_stamp_is_zero_hours_not_negative(self):
        item = {"signals": signals(linear_comments=ok(comment(-5)))}
        self.assertEqual(A.idle_hours(item, NOW), 0.0)
        self.assertEqual(A.idle_band(item, NOW), A.BAND_NONE)

    def test_the_claim_itself_is_a_baseline(self):
        item = {"signals": A.empty_signals(), "claimed_at": ago(20)}
        self.assertEqual(A.explain(item["signals"], None, ago(20))["basis"], "claimed_at")
        self.assertEqual(A.idle_band(item, NOW), A.BAND_NONE)


class UpdatedAtIsNeverAnInput(unittest.TestCase):
    def test_fresh_updated_at_with_old_signals_still_idles(self):
        # Linear's updated_at was touched by the import and by the relabel, so it says nothing.
        old = signals(linear_comments=ok(comment(60)), branch_push={"ok": True, "times": [ago(70)]})
        for name, expected in (("48h", A.BAND_48H),):
            item = {"signals": old, "updated_at": ago(0.1), "updatedAt": ago(0.1), "linear": {"updated_at": ago(0.1)}}
            self.assertEqual(A.idle_band(item, NOW), expected)
        item_36 = {"signals": signals(linear_comments=ok(comment(40))), "updated_at": ago(0.1)}
        self.assertEqual(A.idle_band(item_36, NOW), A.BAND_36H)

    def test_changing_updated_at_changes_nothing(self):
        sig = signals(linear_comments=ok(comment(50)))
        a = A.idle_hours({"signals": sig, "updated_at": ago(1)}, NOW)
        b = A.idle_hours({"signals": sig, "updated_at": ago(900)}, NOW)
        c = A.idle_hours({"signals": sig}, NOW)
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_the_module_never_reads_it(self):
        text = Path(A.__file__).read_text(encoding="utf-8")
        tree = ast.parse(text)
        used = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse({s for s in used if s.replace("_", "").lower() == "updatedat"})

    def test_a_comment_newer_than_updated_at_wins(self):
        item = {"signals": signals(linear_comments=ok(comment(3))), "updated_at": ago(100)}
        self.assertEqual(A.activity_at(item["signals"], None), NOW - timedelta(hours=3))
        self.assertEqual(A.idle_band(item, NOW), A.BAND_NONE)


class Comments(unittest.TestCase):
    def test_sweeper_comments_are_ignored(self):
        real = comment(50)
        sweeps = [comment(1, sweeper=True), {"at": ago(2), "text": "[claim-expiry] pinged"}, {"at": ago(3), "text": "[sweep] note"},
                  {"at": ago(4), "text": "[CLAUDE·ab12] [Sweep] after a seat tag"}, {"at": ago(5), "text": "  [CLAIM-EXPIRY] spaced"}]
        sig = signals(linear_comments=ok(real, *sweeps))
        self.assertEqual(A.activity_at(sig), NOW - timedelta(hours=50))
        self.assertEqual(A.idle_band({"signals": sig}, NOW), A.BAND_48H)

    def test_any_other_seats_comment_counts(self):
        other = {"at": ago(1), "author": "CODEX", "sweeper": False}
        human = {"at": ago(2), "author": "Example Owner", "sweeper": False}
        self.assertEqual(A.activity_at(signals(linear_comments=ok(comment(50), other)), seat="CLAUDE"), NOW - timedelta(hours=1))
        self.assertEqual(A.activity_at(signals(linear_comments=ok(comment(50), human)), seat="CLAUDE"), NOW - timedelta(hours=2))

    def test_a_prefix_in_the_middle_is_not_a_sweeper(self):
        self.assertFalse(A.is_sweeper_text("see the [sweep] notes"))
        self.assertFalse(A.is_sweeper_text(None))
        self.assertTrue(A.is_sweeper_text("[sweep]"))
        self.assertTrue(A.is_sweeper_text("[claim-expiry] x"))


class OtherSources(unittest.TestCase):
    def test_zulip_counts_only_the_claimants_posts(self):
        z = {"ok": True, "events": [{"at": ago(1), "by": "CODEX"}, {"at": ago(30), "by": "claude"}]}
        self.assertEqual(A.activity_at(signals(zulip_topic=z), seat="CLAUDE"), NOW - timedelta(hours=30))

    def test_a_pr_from_another_repo_does_not_count(self):
        pr = {"ok": True, "events": [{"at": ago(1), "same_repo": False}, {"at": ago(20), "same_repo": True}]}
        self.assertEqual(A.activity_at(signals(pr_update=pr)), NOW - timedelta(hours=20))

    def test_every_source_can_win(self):
        for name in ("branch_push", "c1_snapshot", "pr_update"):
            sig = signals(**{name: {"ok": True, "events": [{"at": ago(1)}]}})
            self.assertEqual(A.explain(sig)["basis"], name)


class Unknown(unittest.TestCase):
    def test_a_failed_source_is_unknown_never_idle(self):
        sig = signals(linear_comments=ok(comment(500)), zulip_topic={"ok": False})
        self.assertEqual(A.activity_at(sig), A.UNKNOWN)
        self.assertEqual(A.explain(sig)["unknown_sources"], ["zulip_topic"])
        self.assertEqual(A.idle_band({"signals": sig, "launch_at": ago(1000)}, NOW), A.UNKNOWN)

    def test_ok_must_be_exactly_true(self):
        for bad in ({"ok": "yes", "events": []}, {"events": []}, {"ok": 1, "events": []}, None, "x"):
            self.assertEqual(A.activity_at(signals(c1_snapshot=bad), launch_at=ago(5)), A.UNKNOWN, bad)

    def test_unknown_is_not_a_band(self):
        band = A.idle_band({"signals": {"linear_comments": {"ok": False}}}, NOW)
        self.assertEqual(band, "unknown")
        self.assertNotIn(band, A.BANDS)
        self.assertFalse(A.is_idle_band(band))
        self.assertTrue(A.is_idle_band(A.BAND_36H) and A.is_idle_band(A.BAND_48H))
        self.assertFalse(A.is_idle_band(A.BAND_NONE))

    def test_a_required_source_that_was_not_passed_is_unknown(self):
        sig = {"linear_comments": ok(comment(100))}
        self.assertEqual(A.idle_band({"signals": sig}, NOW), A.BAND_48H)          # not passed, not required:  not counted
        self.assertEqual(A.idle_band({"signals": sig, "required": A.SOURCES}, NOW), A.UNKNOWN)

    def test_required_as_a_bare_name_means_that_one_source(self):
        sig = {"linear_comments": ok(comment(100))}
        self.assertEqual(A.activity_at(sig, required="zulip_topic"), A.UNKNOWN)
        self.assertEqual(A.explain(sig, required="zulip_topic")["unknown_sources"], ["zulip_topic"])

    def test_bad_times_are_unknown(self):
        for event in ({"at": "not a time"}, {"at": None}, {}, {"at": "2026-10-11T10:00:00"}):   # the last has no zone
            self.assertEqual(A.activity_at(signals(branch_push=ok(event)), launch_at=ago(1)), A.UNKNOWN, event)
        self.assertEqual(A.activity_at(signals(), launch_at="garbage"), A.UNKNOWN)
        self.assertEqual(A.idle_hours({"signals": signals(), "launch_at": ago(1)}, "garbage"), A.UNKNOWN)

    def test_no_evidence_at_all_is_unknown(self):
        self.assertEqual(A.activity_at(A.empty_signals()), A.UNKNOWN)
        self.assertEqual(A.explain(A.empty_signals())["unknown_sources"], ["no_evidence"])
        self.assertEqual(A.activity_at(None), A.UNKNOWN)


class Purity(unittest.TestCase):
    def test_imports_nothing_that_does_io(self):
        tree = ast.parse(Path(A.__file__).read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertLessEqual(modules, {"__future__", "re", "datetime", "typing"})

    def test_there_is_no_second_definition(self):
        root = Path(A.__file__).resolve().parent.parent
        names = {"activity_at", "idle_band", "is_sweeper_text"}
        for path in root.rglob("*.py"):
            if path.name == "activity.py" or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            self.assertFalse(defined & names, path.name)


if __name__ == "__main__":
    unittest.main()
