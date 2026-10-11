"""knobs.get against a fake ``infisical``:  fetch, last-known-good, secrets, narrowing."""
from __future__ import annotations

import json
import os
import stat
import time
from unittest import mock

from fleet_knobs import knobs, lkg, source

from ._fake import SIGNING, UNRELATED, KnobTestCase

REPO_A = "Simple-With-Us/sentinel-repo-aaa"
REPO_B = "Simple-With-Us/sentinel-repo-bbb"
GOOD = {
    "FLEET_AUTOPUSH_MODE": "snapshot",
    "FLEET_AUTOPUSH_REPOS": f"{REPO_A}, {REPO_B}",
    "FLEET_AUTOPUSH_IDLE_HOURS": "6",
    "ASK_CAPTURE_CAPS": "per_session=3,per_seat_day=20",
    "CONSULT_ENABLED": "true",
    "CLAIM_EXPIRY_MODE": "ping-only",
    "QDRANT_API_KEY": UNRELATED,                 # in the project, not a registered knob
    "FLEET_FACTS_SIGNING_KEY": SIGNING,
}


class FetchTests(KnobTestCase):
    def test_fetch_ok_returns_typed_values_with_one_cli_call(self):
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE", "off", "enum"), "snapshot")
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_REPOS"), (REPO_A, REPO_B))
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_IDLE_HOURS", 2, "int"), 6)
        self.assertEqual(knobs.get("ASK_CAPTURE_CAPS"), {"per_session": 3, "per_seat_day": 20})
        self.assertIs(knobs.get("CONSULT_ENABLED", False, "bool"), True)
        calls = self.fake.calls()
        self.assertEqual(len(calls), 1, "one fetch per process")
        argv = calls[0]
        self.assertEqual(argv[0], "export")
        self.assertIn("--env=prod", argv)
        self.assertIn(f"--projectId={source.PROJECT_ID}", argv)
        self.assertIn("--format=json", argv)
        self.assertFalse([a for a in argv if "dev" in a or "staging" in a])
        rep = knobs.report()
        self.assertEqual(rep["fetch"], "ok")
        self.assertFalse(rep["stale"])
        self.assertEqual(rep["knobs"]["FLEET_AUTOPUSH_MODE"]["source"], "infisical")

    def test_other_response_shapes_parse(self):
        for shape in ("list", "api"):
            with self.subTest(shape=shape):
                self.new_process()
                self.fake.set(secrets=GOOD, shape=shape)
                self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")

    def test_unset_knob_gets_the_registry_default_and_is_not_stale(self):
        self.fake.set(secrets={})
        knob = knobs.resolve("CLAIM_EXPIRY_MODE")
        self.assertEqual((knob.value, knob.source, knob.stale, knob.reason),
                         ("dry-run", "default", False, "unset"))

    def test_invalid_live_value_gets_the_default(self):
        self.fake.set(secrets={"CLAIM_EXPIRY_MODE": "full-send", "FLEET_AUTOPUSH_IDLE_HOURS": "-4"})
        self.assertEqual(knobs.get("CLAIM_EXPIRY_MODE"), "dry-run")
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_IDLE_HOURS"), 2)
        self.assertEqual(knobs.resolve("CLAIM_EXPIRY_MODE").reason, "invalid")

    def test_a_callers_default_can_only_narrow_the_registry_default(self):
        self.fake.set(secrets={})
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE", "full", "enum"), "off")
        self.assertIs(knobs.get("ASK_CAPTURE_ENABLED", True, "bool"), False)
        self.assertIs(knobs.get("ASK_CAPTURE_SHADOW", False, "bool"), True)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_IDLE_HOURS", 1, "int"), 2)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_IDLE_HOURS", 9, "int"), 9)
        self.assertEqual(knobs.get("NEEDS_OWNER_CAPS", {"per_hour": 2, "extra": 1})["per_hour"], 2)
        with self.assertRaises(knobs.KnobError):
            knobs.get("LANE_SWEEP_MODE", "bogus", "enum")

    def test_unknown_knob_and_wrong_kind_are_refused_without_a_fetch(self):
        self.fake.set(secrets=GOOD)
        with self.assertRaises(knobs.KnobError):
            knobs.get("QDRANT_API_KEY", "", "str")
        with self.assertRaises(knobs.KnobError):
            knobs.get("FLEET_AUTOPUSH_MODE", "off", "int")
        self.assertEqual(self.fake.calls(), [])

    def test_only_prod_is_ever_asked_for(self):
        self.fake.set(secrets=GOOD)
        for var, slug in (("INFISICAL_ENVIRONMENT", "dev"), ("INFISICAL_ENV", "Staging")):
            with self.subTest(var=var):
                self.new_process()
                os.environ[var] = slug
                with self.assertRaises(knobs.KnobError) as ctx:
                    knobs.get("FLEET_AUTOPUSH_MODE")
                self.assertIn("prod is the only environment", str(ctx.exception))
                del os.environ[var]
        self.assertEqual(self.fake.calls(), [])
        for slug in ("", "prod", " PROD "):
            self.new_process()
            os.environ["INFISICAL_ENVIRONMENT"] = slug
            self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        del os.environ["INFISICAL_ENVIRONMENT"]

    def test_a_failed_fetch_is_not_retried_in_the_same_process(self):
        self.fake.set(mode="fail")
        for _ in range(3):
            knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(len(self.fake.calls()), 1)
        knobs.refresh()
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        self.assertEqual(len(self.fake.calls()), 2)


class UnreachableTests(KnobTestCase):
    def _warm_lkg(self):
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertTrue(self.lkg_file.exists())
        self.new_process()

    def test_unreachable_uses_last_known_good_and_reports_stale(self):
        self._warm_lkg()
        self.fake.set(mode="fail", leak=SIGNING)
        with self.assertLogs("fleet_knobs", level="WARNING") as logs:
            knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
            repos = knobs.get("FLEET_AUTOPUSH_REPOS")
        self.assertEqual((knob.value, knob.source, knob.stale), ("snapshot", "lkg", True))
        self.assertEqual(repos, (REPO_A, REPO_B))
        rep = knobs.report()
        self.assertTrue(rep["stale"])
        self.assertEqual(rep["fetch"], "failed")
        self.assertEqual(rep["fetch_reason"], "exit-1")
        self.assertIsInstance(rep["lkg_age_seconds"], int)
        self.assertEqual(rep["knobs"]["FLEET_AUTOPUSH_MODE"]["source"], "lkg")
        text = "\n".join(logs.output) + json.dumps(rep) + repr(knob)
        for value in (SIGNING, UNRELATED, REPO_A, "snapshot"):
            self.assertNotIn(value, text, "logs and reports carry names, never values")

    def test_unreachable_without_a_copy_gives_defaults_marked_stale(self):
        self.fake.set(mode="fail")
        knob = knobs.resolve("LANE_SWEEP_MODE")
        self.assertEqual((knob.value, knob.source, knob.stale), ("report", "default", True))
        self.assertIn("lkg-absent", knob.reason)
        self.assertTrue(knobs.report()["stale"])

    def test_failure_reasons_are_tokens(self):
        for mode, token in (("garbage", "bad-output"), ("fail", "exit-1")):
            self.new_process()
            self.fake.set(mode=mode)
            knobs.get("FLEET_AUTOPUSH_MODE")
            self.assertEqual(knobs.report()["fetch_reason"], token)
        self.new_process()
        self.fake.set(mode="hang")
        original = source.TIMEOUT_SECONDS
        source.TIMEOUT_SECONDS = 0.5
        self.addCleanup(setattr, source, "TIMEOUT_SECONDS", original)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "timeout")
        self.new_process()
        os.environ["PATH"] = str(self.root)               # no infisical anywhere (fallback dirs are patched out)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "cli-missing")

    def test_an_unexpected_fetch_error_is_a_failed_fetch_not_a_crash(self):
        with mock.patch.object(source, "fetch", side_effect=RuntimeError("secret-in-message")):
            knob = knobs.resolve("CLAIM_EXPIRY_MODE")
        self.assertEqual((knob.value, knob.stale), ("dry-run", True))
        self.assertEqual(knobs.report()["fetch_reason"], "error-RuntimeError")
        self.assertNotIn("secret-in-message", json.dumps(knobs.report()))

    def test_an_old_copy_is_ignored(self):
        lkg.write({"FLEET_AUTOPUSH_MODE": "full"}, now=time.time() - lkg.MAX_AGE_SECONDS - 60)
        self.fake.set(mode="fail")
        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
        self.assertEqual((knob.value, knob.source), ("off", "default"))
        self.assertIn("lkg-expired", knob.reason)

    def test_an_untrusted_copy_is_ignored(self):
        self._warm_lkg()
        self.fake.set(mode="fail")
        good = self.lkg_file.read_text()
        cases = {}

        def loose_file():
            os.chmod(self.lkg_file, 0o644)

        def loose_dir():
            os.chmod(self.state_dir, 0o755)

        def symlink():
            real = self.root / "elsewhere.json"
            real.write_text(good)
            os.chmod(real, 0o600)
            self.lkg_file.unlink()
            os.symlink(real, self.lkg_file)

        def other_project():
            doc = json.loads(good)
            doc["project"] = "00000000-0000-0000-0000-000000000000"
            self.lkg_file.write_text(json.dumps(doc))

        def not_json():
            self.lkg_file.write_text("{not json")

        for name, damage in (("loose_file", loose_file), ("loose_dir", loose_dir),
                             ("symlink", symlink), ("other_project", other_project),
                             ("not_json", not_json)):
            with self.subTest(case=name):
                self.new_process()
                self.fake.set(secrets=GOOD)
                knobs.get("FLEET_AUTOPUSH_MODE")           # rewrite a clean copy
                self.new_process()
                self.fake.set(mode="fail")
                damage()
                knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
                self.assertEqual((knob.value, knob.source), ("off", "default"), name)
                os.chmod(self.state_dir, 0o700)

    def test_a_planted_copy_cannot_supply_a_secret(self):
        lkg.write({"FLEET_FACTS_SIGNING_KEY": SIGNING, "FLEET_AUTOPUSH_MODE": "report"})
        self.fake.set(mode="fail")
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "report")
        with self.assertRaises(knobs.SecretUnavailable):
            knobs.get("FLEET_FACTS_SIGNING_KEY")
        self.new_process()
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertNotIn("FLEET_FACTS_SIGNING_KEY", json.loads(self.lkg_file.read_text())["knobs"])


class EmptyExportTests(KnobTestCase):
    """An export with none of our names is a failed fetch while a copy still has knobs."""

    PAUSED = {"NEEDS_OWNER_NOTIFY_PAUSED": "true", "CLAIM_EXPIRY_MODE": "ping-only"}

    def _warm_paused_copy(self):
        self.fake.set(secrets=self.PAUSED)
        self.assertIs(knobs.get("NEEDS_OWNER_NOTIFY_PAUSED"), True)
        self.assertTrue(self.lkg_file.exists())
        self.new_process()
        return self.lkg_file.read_bytes()

    def _assert_pause_survives(self, before: bytes):
        with self.assertLogs("fleet_knobs", level="WARNING") as logs:
            knob = knobs.resolve("NEEDS_OWNER_NOTIFY_PAUSED")
        self.assertEqual((knob.value, knob.source, knob.stale), (True, "lkg", True))
        self.assertIn("unreachable:empty", knob.reason)
        rep = knobs.report()
        self.assertEqual((rep["fetch"], rep["fetch_reason"], rep["stale"]),
                         ("failed", "empty", True))
        self.assertEqual(knobs.get("CLAIM_EXPIRY_MODE"), "ping-only")
        self.assertEqual(self.lkg_file.read_bytes(), before, "the copy is kept byte for byte")
        self.assertNotIn("true", "\n".join(logs.output), "logs carry names, never values")

    def test_an_empty_export_does_not_erase_the_copy_or_lift_a_pause(self):
        before = self._warm_paused_copy()
        self.fake.set(secrets={})                     # exit 0, valid JSON, nothing in it
        self._assert_pause_survives(before)

    def test_an_export_of_only_unregistered_keys_is_empty_too(self):
        before = self._warm_paused_copy()
        self.fake.set(secrets={"QDRANT_API_KEY": UNRELATED, "SOME_OTHER_TOKEN": UNRELATED})
        self._assert_pause_survives(before)
        self.assertNotIn(UNRELATED, json.dumps(knobs.report()))

    def test_an_empty_export_fails_a_secret_closed_as_unreachable(self):
        self._warm_paused_copy()
        self.fake.set(secrets={})
        with self.assertRaises(knobs.SecretUnavailable) as ctx:
            knobs.get("FLEET_FACTS_SIGNING_KEY")
        self.assertEqual(ctx.exception.reason, "unreachable:empty")

    def test_the_empty_answer_is_not_retried_in_the_same_process(self):
        self._warm_paused_copy()
        self.fake.set(secrets={})
        knobs.get("NEEDS_OWNER_NOTIFY_PAUSED")
        self.fake.set(secrets=self.PAUSED)
        knobs.get("CLAIM_EXPIRY_MODE")
        self.assertEqual(len(self.fake.calls()), 2, "one warm-up fetch and one empty fetch")

    def test_a_deliberate_single_delete_still_applies(self):
        self._warm_paused_copy()
        self.fake.set(secrets={"CLAIM_EXPIRY_MODE": "ping-only"})   # the pause was unset
        knob = knobs.resolve("NEEDS_OWNER_NOTIFY_PAUSED")
        self.assertEqual((knob.value, knob.source, knob.stale), (False, "default", False))
        self.assertEqual(json.loads(self.lkg_file.read_text())["knobs"],
                         {"CLAIM_EXPIRY_MODE": "ping-only"})

    def test_a_fresh_project_with_no_knobs_yet_is_not_an_outage(self):
        self.fake.set(secrets={})
        knob = knobs.resolve("NEEDS_OWNER_NOTIFY_PAUSED")
        self.assertEqual((knob.value, knob.source, knob.stale, knob.reason),
                         (False, "default", False, "unset"))
        rep = knobs.report()
        self.assertEqual((rep["fetch"], rep["stale"]), ("ok", False))
        self.assertEqual(json.loads(self.lkg_file.read_text())["knobs"], {})
        self.new_process()                            # the copy now exists and has no knobs
        knob = knobs.resolve("NEEDS_OWNER_NOTIFY_PAUSED")
        self.assertEqual((knob.stale, knobs.report()["fetch"]), (False, "ok"))

    def test_an_expired_copy_does_not_make_an_empty_export_an_outage(self):
        lkg.write({"NEEDS_OWNER_NOTIFY_PAUSED": "true"},
                  now=time.time() - lkg.MAX_AGE_SECONDS - 60)
        self.fake.set(secrets={})
        knob = knobs.resolve("NEEDS_OWNER_NOTIFY_PAUSED")
        self.assertEqual((knob.value, knob.stale, knobs.report()["fetch"]), (False, False, "ok"))


class LkgFileTests(KnobTestCase):
    def test_good_fetch_writes_a_private_copy_of_registered_non_secrets_only(self):
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.lkg_file.stat().st_mode), 0o600)
        doc = json.loads(self.lkg_file.read_text())
        self.assertEqual(doc["environment"], "prod")
        self.assertEqual(set(doc["knobs"]), {
            "FLEET_AUTOPUSH_MODE", "FLEET_AUTOPUSH_REPOS", "FLEET_AUTOPUSH_IDLE_HOURS",
            "ASK_CAPTURE_CAPS", "CONSULT_ENABLED", "CLAIM_EXPIRY_MODE"})
        self.assertEqual(os.listdir(self.state_dir), ["lkg.json"], "no temp file left behind")

    def test_a_secret_is_never_written_to_disk(self):
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_FACTS_SIGNING_KEY"), SIGNING)
        knobs.get("FLEET_AUTOPUSH_MODE")
        blob = b""
        for dirpath, _dirs, files in os.walk(self.home):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    blob += fh.read()
        self.assertTrue(blob, "the copy was written")
        for forbidden in (SIGNING, UNRELATED, "FLEET_FACTS_SIGNING_KEY", "QDRANT_API_KEY"):
            self.assertNotIn(forbidden.encode(), blob)
        self.assertNotIn(SIGNING, os.environ.values(), "never exported either")

    def test_the_write_refuses_a_secret_value_in_the_bytes(self):
        with self.assertRaises(ValueError) as ctx:
            lkg.write({"FLEET_AUTOPUSH_MODE": "off", "CLAIM_EXPIRY_LAUNCH_AT": SIGNING}, [SIGNING])
        self.assertNotIn(SIGNING, str(ctx.exception))
        self.assertFalse(self.lkg_file.exists())

    def test_a_knob_unset_in_infisical_leaves_the_copy(self):
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.new_process()
        self.fake.set(secrets={"FLEET_AUTOPUSH_MODE": "report"})
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(json.loads(self.lkg_file.read_text())["knobs"],
                         {"FLEET_AUTOPUSH_MODE": "report"})

    def test_a_bad_value_is_dropped_from_the_copy_so_offline_matches_live(self):
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.new_process()
        self.fake.set(secrets=dict(GOOD, FLEET_AUTOPUSH_MODE="nonsense"))
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "off")           # live: invalid -> default
        copy = json.loads(self.lkg_file.read_text())["knobs"]
        self.assertNotIn("FLEET_AUTOPUSH_MODE", copy, "the old good value must not outlive a typo")
        self.assertIn("FLEET_AUTOPUSH_REPOS", copy, "the other knobs are untouched")

    def test_a_typo_then_an_outage_reads_the_default_not_the_older_wider_value(self):
        self.fake.set(secrets=dict(GOOD, CLAIM_EXPIRY_MODE="act"))
        self.assertEqual(knobs.get("CLAIM_EXPIRY_MODE"), "act")
        self.new_process()
        self.fake.set(secrets=dict(GOOD, CLAIM_EXPIRY_MODE="ac t"))
        live = knobs.resolve("CLAIM_EXPIRY_MODE")
        self.assertEqual((live.value, live.source, live.stale, live.reason),
                         ("dry-run", "default", False, "invalid"))
        self.new_process()
        self.fake.set(mode="fail")
        offline = knobs.resolve("CLAIM_EXPIRY_MODE")
        self.assertEqual((offline.value, offline.source, offline.stale),
                         ("dry-run", "default", True))
        self.assertEqual(offline.value, live.value, "an outage must not change the answer")
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot", "other knobs still hold")

    def test_an_unchanged_copy_is_not_rewritten_within_the_hour(self):
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        first = self.lkg_file.stat().st_mtime_ns
        self.new_process()
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(self.lkg_file.stat().st_mtime_ns, first)

    def test_a_copy_that_cannot_be_written_is_not_fatal(self):
        (self.home / ".local").write_text("a file where the directory should be")
        self.fake.set(secrets=GOOD)
        with self.assertLogs("fleet_knobs", level="WARNING"):
            self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")


class SecretTests(KnobTestCase):
    def test_a_secret_is_returned_and_never_shown(self):
        self.fake.set(secrets=GOOD)
        knob = knobs.resolve("FLEET_FACTS_SIGNING_KEY")
        self.assertEqual(knob.value, SIGNING)
        self.assertNotIn(SIGNING, repr(knob))
        self.assertNotIn(SIGNING, json.dumps(knobs.report()))
        self.assertEqual(knobs.report()["knobs"]["FLEET_FACTS_SIGNING_KEY"]["source"], "infisical")

    def test_a_secret_fails_closed(self):
        cases = (("unset", {"mode": "ok", "secrets": {}}),
                 ("unreachable", {"mode": "fail"}),
                 ("malformed", {"mode": "ok", "secrets": {"FLEET_FACTS_SIGNING_KEY": "short"}}))
        for reason, cfg in cases:
            with self.subTest(reason=reason):
                self.new_process()
                self.fake.set(**cfg)
                with self.assertRaises(knobs.SecretUnavailable) as ctx:
                    knobs.get("FLEET_FACTS_SIGNING_KEY")
                self.assertTrue(ctx.exception.reason.startswith(reason))
                self.assertNotIn("short", str(ctx.exception))

    def test_a_secret_takes_no_default_and_no_local_override(self):
        self.fake.set(secrets=GOOD)
        with self.assertRaises(knobs.KnobError):
            knobs.get("FLEET_FACTS_SIGNING_KEY", "fallback" * 8)
        with self.assertRaises(knobs.KnobError):
            knobs.get("FLEET_FACTS_SIGNING_KEY", local="x" * 40)
        with self.assertRaises(knobs.KnobError):
            knobs.get("FLEET_FACTS_SIGNING_KEY", stop=True)


class NarrowTests(KnobTestCase):
    def test_a_local_value_may_narrow_each_kind(self):
        self.fake.set(secrets=dict(GOOD, ASK_CAPTURE_ENABLED="true", ASK_CAPTURE_SHADOW="false",
                                   NEEDS_OWNER_NOTIFY_PAUSED="false"))
        k = knobs.resolve("FLEET_AUTOPUSH_MODE", local="report")
        self.assertEqual((k.value, k.narrowed, k.local_ignored), ("report", True, False))
        k = knobs.resolve("ASK_CAPTURE_ENABLED", local="false")
        self.assertEqual((k.value, k.narrowed), (False, True))
        k = knobs.resolve("ASK_CAPTURE_SHADOW", local=True)
        self.assertEqual((k.value, k.narrowed), (True, True))
        k = knobs.resolve("NEEDS_OWNER_NOTIFY_PAUSED", local=True)
        self.assertEqual((k.value, k.narrowed), (True, True))
        k = knobs.resolve("FLEET_AUTOPUSH_IDLE_HOURS", local=12)
        self.assertEqual((k.value, k.narrowed), (12, True))
        k = knobs.resolve("FLEET_AUTOPUSH_REPOS", local=[REPO_B, "Simple-With-Us/other"])
        self.assertEqual((k.value, k.narrowed, k.local_ignored), ((REPO_B,), True, True))
        k = knobs.resolve("ASK_CAPTURE_CAPS", local={"per_session": 1, "per_seat_day": 99})
        self.assertEqual((k.value, k.narrowed, k.local_ignored),
                         ({"per_session": 1, "per_seat_day": 20}, True, True))
        self.assertTrue(knobs.report()["knobs"]["FLEET_AUTOPUSH_REPOS"]["local_ignored"])

    def test_a_local_value_that_would_widen_is_ignored(self):
        self.fake.set(secrets=dict(GOOD, ASK_CAPTURE_ENABLED="false", ASK_CAPTURE_SHADOW="true",
                                   NEEDS_OWNER_NOTIFY_PAUSED="true",
                                   FLEET_AUTOPUSH_MODE="report"))
        for name, local, expect in (
                ("FLEET_AUTOPUSH_MODE", "full", "report"),
                ("ASK_CAPTURE_ENABLED", True, False),
                ("ASK_CAPTURE_SHADOW", False, True),
                ("NEEDS_OWNER_NOTIFY_PAUSED", False, True),
                ("FLEET_AUTOPUSH_IDLE_HOURS", 1, 6),
                ("FLEET_AUTOPUSH_REPOS", [REPO_A, REPO_B, "Simple-With-Us/more"], (REPO_A, REPO_B)),
                ("ASK_CAPTURE_CAPS", {"per_session": 50}, {"per_session": 3, "per_seat_day": 20})):
            with self.subTest(name=name):
                k = knobs.resolve(name, local=local)
                self.assertEqual((k.value, k.narrowed, k.local_ignored), (expect, False, True))

    def test_the_owner_id_and_launch_time_take_no_local_override(self):
        uid = "11111111-2222-3333-4444-555555555555"
        self.fake.set(secrets={"OWNER_LINEAR_USER_ID": uid,
                               "CLAIM_EXPIRY_LAUNCH_AT": "2026-10-12T05:00:00Z"})
        self.assertEqual(knobs.get("OWNER_LINEAR_USER_ID", local="99999999-2222-3333-4444-555555555555"), uid)
        self.assertEqual(knobs.get("CLAIM_EXPIRY_LAUNCH_AT", local="2000-01-01T00:00:00Z"),
                         "2026-10-12T05:00:00Z")
        self.assertTrue(knobs.resolve("OWNER_LINEAR_USER_ID", local="x").local_ignored)

    def test_a_stop_file_forces_the_narrowest_value(self):
        self.fake.set(secrets=dict(GOOD, ASK_CAPTURE_ENABLED="true", CLAIM_EXPIRY_MODE="act",
                                   LANE_SWEEP_MODE="retire", NEEDS_OWNER_NOTIFY_PAUSED="false"))
        self.assertEqual(knobs.get("CLAIM_EXPIRY_MODE", stop=True), "dry-run")
        self.assertEqual(knobs.get("LANE_SWEEP_MODE", stop=True), "report")
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE", stop=True), "off")
        self.assertIs(knobs.get("ASK_CAPTURE_ENABLED", stop=True), False)
        self.assertIs(knobs.get("NEEDS_OWNER_NOTIFY_PAUSED", stop=True), True)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_REPOS", stop=True), ())
        self.assertEqual(set(knobs.get("ASK_CAPTURE_CAPS", stop=True).values()), {0})
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_IDLE_HOURS", stop=True), 168)
        self.assertTrue(knobs.resolve("LANE_SWEEP_MODE", stop=True).narrowed)

    def test_an_invalid_local_value_is_ignored(self):
        self.fake.set(secrets=GOOD)
        k = knobs.resolve("FLEET_AUTOPUSH_MODE", local="bogus")
        self.assertEqual((k.value, k.narrowed, k.local_ignored), ("snapshot", False, True))

    def test_narrowing_also_applies_to_defaults_and_the_stale_copy(self):
        self.fake.set(mode="fail")
        k = knobs.resolve("CLAIM_EXPIRY_CAPS", local={"releases_per_run": 5})
        self.assertEqual(k.value, {"releases_per_run": 5, "releases_per_day": 40, "pings_per_run": 40})
        self.assertTrue(k.stale)
        self.assertEqual(knobs.get("CLAIM_EXPIRY_MODE", local="act"), "dry-run")
