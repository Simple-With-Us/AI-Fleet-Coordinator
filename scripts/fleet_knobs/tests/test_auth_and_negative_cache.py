"""Scoped service-token auth and the fetch-failed negative cache."""

from __future__ import annotations

import json
import os
import stat
import time
from unittest import mock

from fleet_knobs import knobs, lkg, source

from ._fake import KnobTestCase, fixture_service_token

GOOD = {"FLEET_AUTOPUSH_MODE": "snapshot"}
FAKE_TOKEN = fixture_service_token()
GENERIC_TOKEN = fixture_service_token()


class ServiceTokenAuthTests(KnobTestCase):
    def test_scoped_token_is_required_even_when_generic_or_machine_credentials_exist(self):
        os.environ.pop(source.ENV_KNOBS_TOKEN, None)
        os.environ["INFISICAL_TOKEN"] = GENERIC_TOKEN
        os.environ["INFISICAL_AUTOMATION_CLIENT_ID"] = "legacy-test-client"
        os.environ["INFISICAL_AUTOMATION_CLIENT_SECRET"] = "legacy-test-secret"
        self.fake.set(secrets=GOOD)

        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")

        self.assertEqual((knob.source, knob.stale), ("default", True))
        self.assertEqual(knobs.report()["fetch_reason"], "service-token-required")
        self.assertEqual(self.fake.calls(), [], "the CLI must not run without the scoped token")

    def test_malformed_scoped_tokens_fail_before_cli_without_fallback(self):
        for token in ("", "not-a-token", "st.short", "st." + "x" * 8193, "st.bad token"):
            with self.subTest(token_shape=(len(token), token.startswith("st."))):
                self.new_process()
                os.environ[source.ENV_KNOBS_TOKEN] = token
                self.fake.set(secrets=GOOD)

                knob = knobs.resolve("FLEET_AUTOPUSH_MODE")

                self.assertIn(knob.source, ("default", "lkg"))
                self.assertTrue(knob.stale)
                self.assertEqual(
                    knobs.report()["fetch_reason"], "service-token-required"
                )
                self.assertEqual(self.fake.calls(), [])

    def test_scoped_token_is_isolated_to_child_and_overrides_generic_token(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        os.environ["INFISICAL_TOKEN"] = GENERIC_TOKEN
        os.environ["INFISICAL_AUTOMATION_CLIENT_ID"] = "legacy-test-client"
        os.environ["INFISICAL_AUTOMATION_CLIENT_SECRET"] = "legacy-test-secret"
        self.fake.set(secrets=GOOD)

        from unittest import mock
        helper = mock.Mock()
        helper.fetch_project.return_value = GOOD
        with mock.patch.object(source, "HELPER_LOADER", return_value=helper):
            self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")

        helper.fetch_project.assert_called_once()
        args, kwargs = helper.fetch_project.call_args
        self.assertEqual(args[0:2], (source.PROJECT_ID, "/"))
        self.assertEqual(args[2][source.ENV_AI_FLEET_COORDINATOR_TOKEN], FAKE_TOKEN)
        self.assertNotIn(source.ENV_TOKEN, args[2])
        self.assertNotIn("INFISICAL_WRITE_TOKEN", args[2])
        child = source.child_env()
        self.assertEqual(child[source.ENV_AI_FLEET_COORDINATOR_TOKEN], FAKE_TOKEN)
        self.assertNotIn(source.ENV_TOKEN, child)
        for name in (
            "INFISICAL_AUTOMATION_CLIENT_ID",
            "INFISICAL_AUTOMATION_CLIENT_SECRET",
        ):
            self.assertNotIn(name, child)
        self.assertEqual(os.environ["INFISICAL_TOKEN"], GENERIC_TOKEN)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.fake.calls()))

    def test_invalid_or_revoked_token_fails_closed_without_auth_retry(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        self.fake.set(mode="fail", code=1, leak=FAKE_TOKEN)

        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
        report = knobs.report()

        self.assertEqual((knob.source, knob.stale), ("default", True))
        self.assertEqual(report["fetch_reason"], "scoped-read-failed")
        self.assertNotIn(FAKE_TOKEN, json.dumps(report))
        self.assertEqual(len(self.fake.calls()), 1, "one failed scoped request and no auth fallback")

    def test_named_fetch_uses_the_same_required_scoped_token(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        self.fake.set(secrets={"A": "1"})

        self.assertEqual(source.fetch_named(["A"], project_id=source.PROJECT_ID, path="/"), {"A": "1"})
        self.assertEqual(self.fake.tokens(), [FAKE_TOKEN])


class NegativeCacheTests(KnobTestCase):
    @property
    def marker(self):
        return self.state_dir / "fetch-failed.json"

    def test_a_failed_fetch_leaves_a_private_marker_with_no_output(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        self.fake.set(mode="fail", leak="leakedStderrSentinel")
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertTrue(self.marker.exists())
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        doc = json.loads(self.marker.read_text())
        self.assertEqual(set(doc), {"failed_epoch", "reason"})
        self.assertEqual(doc["reason"], "scoped-read-failed")
        self.assertNotIn("leakedStderrSentinel", self.marker.read_text())

    def test_a_sibling_skips_the_fetch_inside_the_window_and_uses_the_copy(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.new_process()
        self.fake.set(mode="fail")
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(len(self.fake.calls()), 2)
        self.new_process(keep_failure_marker=True)
        self.fake.set(secrets=GOOD)
        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
        self.assertEqual(len(self.fake.calls()), 2)
        self.assertEqual((knob.value, knob.source, knob.stale), ("snapshot", "lkg", True))
        self.assertEqual(knobs.report()["fetch_reason"], "recent-scoped-read-failed")

    def test_a_secret_still_fails_closed_inside_the_window(self):
        lkg.record_failure("timeout")
        self.fake.set(secrets={"FLEET_FACTS_SIGNING_KEY": "x" * 40})
        with self.assertRaises(knobs.SecretUnavailable):
            knobs.get("FLEET_FACTS_SIGNING_KEY")
        self.assertEqual(self.fake.calls(), [])

    def test_the_marker_expires(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        lkg.record_failure("scoped-read-failed", now=time.time() - lkg.FAILURE_TTL_SECONDS - 1)
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.assertEqual(len(self.fake.calls()), 1)

    def test_a_good_fetch_clears_the_marker(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        lkg.record_failure("scoped-read-failed", now=time.time() - lkg.FAILURE_TTL_SECONDS - 1)
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertFalse(self.marker.exists())

    def test_refresh_retries_even_inside_the_window(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        lkg.record_failure("scoped-read-failed")
        self.fake.set(secrets=GOOD)
        knobs.refresh()
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.assertEqual(len(self.fake.calls()), 1)

    def test_an_untrusted_marker_is_ignored(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        lkg.record_failure("scoped-read-failed")
        os.chmod(self.marker, 0o644)
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.marker.unlink(missing_ok=True)
        self.marker.symlink_to(self.root / "elsewhere.json")
        (self.root / "elsewhere.json").write_text(
            json.dumps({"failed_epoch": time.time(), "reason": "scoped-read-failed"})
        )
        self.new_process(keep_failure_marker=True)
        self.assertIsNone(lkg.recent_failure())

    def test_a_reason_is_reduced_to_a_token(self):
        lkg.record_failure("scoped-read-failed with " + "spaces/and\nnewlines" * 10)
        reason = lkg.recent_failure()
        self.assertLessEqual(len(reason), 128)
        self.assertTrue(set(reason) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_:"))

    def test_a_timeout_is_paid_once_across_siblings(self):
        os.environ[source.ENV_KNOBS_TOKEN] = FAKE_TOKEN
        self.fake.set(mode="hang")
        original = source.TIMEOUT_SECONDS
        source.TIMEOUT_SECONDS = 0.5
        self.addCleanup(setattr, source, "TIMEOUT_SECONDS", original)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "timeout")
        for _ in range(3):
            self.new_process(keep_failure_marker=True)
            started = time.monotonic()
            knobs.get("FLEET_AUTOPUSH_MODE")
            self.assertLess(time.monotonic() - started, 0.4)
            self.assertEqual(knobs.report()["fetch_reason"], "recent-timeout")
        self.assertEqual(len(self.fake.calls()), 1)
