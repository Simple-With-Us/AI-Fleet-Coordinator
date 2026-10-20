"""Machine-identity auth (pm2 and launchd) and the fetch-failed negative cache.

pm2 runs as a system LaunchDaemon, where the login Keychain and the CLI's own session are
unreachable, so a fetch uses fleet-secrets' machine identity when there is one.  A failed
fetch leaves a short marker so sibling processes skip the fetch for a minute.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import types
import unittest
from unittest import mock

from fleet_knobs import knobs, lkg, source

from ._fake import KnobTestCase

GOOD = {"FLEET_AUTOPUSH_MODE": "snapshot"}
FAKE_TOKEN = "fakeMachineIdentityToken0123456789sentinel"


class FakeHelper:
    """Stands in for fleet-secrets' core module.  Records how it was used."""

    def __init__(self, mode="system-keychain", token=FAKE_TOKEN, login_error=None, mode_error=None):
        self.mode = mode
        self.token = token
        self.login_error = login_error
        self.mode_error = mode_error
        self.logins = []

    def auth_mode(self, env):
        if self.mode_error:
            raise self.mode_error
        return "token" if (env.get("INFISICAL_TOKEN") or "").strip() else self.mode

    def machine_token(self, cli, env, mode):
        self.logins.append(mode)
        # Record the credentials the login was handed:  the real helper raises
        # "automation-env-incomplete" without the pair, and source.py turns that into
        # "login-failed", so a child_env() that stripped the pair too early would
        # otherwise degrade every knob on the server while these tests stayed green.
        self.seen_id = env.get("INFISICAL_AUTOMATION_CLIENT_ID")
        self.seen_secret = env.get("INFISICAL_AUTOMATION_CLIENT_SECRET")
        if self.login_error:
            raise self.login_error
        return self.token

    def find_cli(self, env):  # pragma: no cover - present so the loader accepts the shape
        return None


class AuthTests(KnobTestCase):
    def use_helper(self, helper):
        patcher = mock.patch.object(source, "HELPER_LOADER", lambda: helper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return helper

    def test_a_token_already_in_the_environment_is_used_and_no_helper_is_consulted(self):
        os.environ["INFISICAL_TOKEN"] = "presetTokenSentinel"
        loader = mock.Mock(side_effect=AssertionError("helper must not be consulted"))
        with mock.patch.object(source, "HELPER_LOADER", loader):
            self.fake.set(secrets=GOOD)
            self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        loader.assert_not_called()
        self.assertEqual(self.fake.tokens(), ["presetTokenSentinel"])

    def test_the_machine_identity_token_goes_only_to_the_child(self):
        helper = self.use_helper(FakeHelper())
        self.fake.set(secrets=GOOD)
        with self.assertLogs("fleet_knobs", level="DEBUG") as logs:
            knobs.get("FLEET_AUTOPUSH_MODE")
            logging_marker = "fleet_knobs: marker"
            knobs.log.debug(logging_marker)
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.assertEqual(helper.logins, ["system-keychain"])
        self.assertEqual(self.fake.tokens(), [FAKE_TOKEN], "the CLI was handed the token")
        self.assertNotIn("INFISICAL_TOKEN", os.environ, "this process's environment is untouched")
        for argv in self.fake.calls():
            self.assertFalse(any(FAKE_TOKEN in a for a in argv), "never in an argument list")
        text = "\n".join(logs.output) + json.dumps(knobs.report())
        self.assertNotIn(FAKE_TOKEN, text)
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    FAKE_TOKEN, path.read_text(errors="replace"), f"the token reached {path}"
                )

    def test_the_login_keychain_mode_is_passed_through(self):
        helper = self.use_helper(FakeHelper(mode="keychain"))
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(helper.logins, ["keychain"])
        self.assertEqual(self.fake.tokens(), [FAKE_TOKEN])

    def test_the_automation_env_mode_is_used_like_any_other_machine_identity(self):
        # The server has no keychain:  without this the fetch would fall through to mode
        # "session" and run the export unauthenticated, degrading every knob silently.
        helper = self.use_helper(FakeHelper(mode="automation-env"))
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        self.assertEqual(helper.logins, ["automation-env"])
        self.assertEqual(self.fake.tokens(), [FAKE_TOKEN])

    def test_the_automation_credentials_never_reach_the_export_child(self):
        helper = self.use_helper(FakeHelper(mode="automation-env"))
        os.environ["INFISICAL_AUTOMATION_CLIENT_ID"] = "idSentinel0123456789"
        os.environ["INFISICAL_AUTOMATION_CLIENT_SECRET"] = "secretSentinel0123456789"
        env = source.child_env()
        self.assertEqual(env["INFISICAL_TOKEN"], FAKE_TOKEN, "the token it resolved to")
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_ID", env)
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_SECRET", env)
        # Stripped from the child, but the login itself must still have seen the pair.
        self.assertEqual(helper.seen_id, "idSentinel0123456789")
        self.assertEqual(helper.seen_secret, "secretSentinel0123456789")
        # And the real subprocess must see it too:  asserting on child_env() alone would stay
        # green if _run ever built the child's env from os.environ plus a token.
        knobs.reset_for_tests()
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        self.assertEqual(self.fake.tokens(), [FAKE_TOKEN])
        seen = self.fake.automation_seen()
        self.assertTrue(seen, "the fake CLI recorded the call")
        for pair in seen:
            self.assertIsNone(pair["id"], "the export subprocess never sees the client id")
            self.assertIsNone(pair["secret"], "the export subprocess never sees the client secret")

    @unittest.skipUnless(sys.platform.startswith("linux"), "the automation pair is Linux only")
    def test_a_half_set_automation_pair_is_reported_as_such_not_as_a_keychain(self):
        self.use_helper(FakeHelper(mode_error=RuntimeError("automation-env-incomplete")))
        os.environ["INFISICAL_AUTOMATION_CLIENT_ID"] = "idSentinel0123456789"
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "automation-env-incomplete:missing-INFISICAL_AUTOMATION_CLIENT_SECRET")
        self.assertEqual(self.fake.calls(), [])

    def test_linux_incomplete_reason_names_only_the_missing_variable(self):
        with mock.patch.object(source.sys, "platform", "linux"):
            for present, missing in (
                (source.ENV_AUTOMATION_ID, source.ENV_AUTOMATION_SECRET),
                (source.ENV_AUTOMATION_SECRET, source.ENV_AUTOMATION_ID),
            ):
                reason = source._incomplete_reason({present: "synthetic-value"})
                self.assertEqual(reason, "automation-env-incomplete:missing-" + missing)
                self.assertNotIn("synthetic-value", reason)

    def test_no_machine_identity_falls_back_to_the_cli_session(self):
        helper = self.use_helper(FakeHelper(mode="session"))
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        self.assertEqual(helper.logins, [])
        self.assertEqual(self.fake.tokens(), [None])

    def test_no_helper_falls_back_to_the_cli_session(self):
        self.fake.set(secrets=GOOD)  # KnobTestCase's loader answers None
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        self.assertEqual(self.fake.tokens(), [None])

    def test_a_failed_login_fails_fast_without_carrying_its_text(self):
        self.use_helper(FakeHelper(login_error=RuntimeError("login said " + FAKE_TOKEN)))
        self.fake.set(secrets=GOOD)
        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
        self.assertEqual((knob.source, knob.stale), ("default", True))
        rep = knobs.report()
        self.assertEqual(rep["fetch_reason"], "login-failed")
        self.assertNotIn(FAKE_TOKEN, json.dumps(rep))
        self.assertEqual(self.fake.calls(), [], "no export runs without a token")

    def test_a_half_filled_keychain_is_an_error_not_a_downgrade(self):
        self.use_helper(FakeHelper(mode_error=RuntimeError("keychain-incomplete")))
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "keychain-incomplete")
        self.assertEqual(self.fake.calls(), [])

    def test_fetch_named_uses_the_machine_identity_too(self):
        self.use_helper(FakeHelper())
        self.fake.set(secrets={"A": "1"})
        self.assertEqual(source.fetch_named(["A"], project_id="p", path="/zulip"), {"A": "1"})
        self.assertEqual(self.fake.tokens(), [FAKE_TOKEN])

    def test_the_helper_loads_from_fleet_secrets_home(self):
        pkg = self.root / "fs-home" / "fleet_secrets"
        pkg.mkdir(parents=True)
        (pkg / "core.py").write_text(
            "def auth_mode(env):\n    return 'session'\n"
            "def machine_token(cli, env, mode):\n    return ''\n"
            "def find_cli(env):\n    return None\n"
            "MARKER = 'from-fleet-secrets-home'\n"
        )
        os.environ[source.ENV_HELPER_HOME] = str(self.root / "fs-home")
        with mock.patch.dict(source._HELPER_CACHE, clear=True):
            helper = source._load_helper()
        self.assertIsInstance(helper, types.ModuleType)
        self.assertEqual(helper.MARKER, "from-fleet-secrets-home")

    def test_a_broken_helper_file_is_skipped(self):
        pkg = self.root / "fs-home" / "fleet_secrets"
        pkg.mkdir(parents=True)
        (pkg / "core.py").write_text("raise ImportError('nope')\n")
        os.environ[source.ENV_HELPER_HOME] = str(self.root / "fs-home")
        with (
            mock.patch.object(source, "HELPER_HOME", str(self.root / "absent")),
            mock.patch.object(source, "_helper_candidates", lambda: [str(pkg / "core.py")]),
            mock.patch.dict(source._HELPER_CACHE, clear=True),
        ):
            self.assertIsNone(source._load_helper())

    def test_fleet_knobs_auth_session_skips_the_machine_identity(self):
        helper = self.use_helper(FakeHelper())
        os.environ[source.ENV_AUTH] = "session"
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")
        self.assertEqual(helper.logins, [])
        self.assertEqual(self.fake.tokens(), [None])

    STUB_CORE = (
        "import subprocess, sys\n"
        "HANG = [sys.executable, '-c', 'import time; time.sleep(30)']\n"
        "def _hang(timeout):\n"
        "    try:\n"
        "        subprocess.run(HANG, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)\n"
        "    except (subprocess.TimeoutExpired, OSError):\n"
        "        return None\n"
        "    return 'x'\n"
        "def auth_mode(env):\n"
        "    if {hang_lookup}:\n"
        "        _hang(10.0)  # a locked keychain:  the real helper allows 10 s and answers absent\n"
        "        return 'session'\n"
        "    return 'system-keychain'\n"
        "def machine_token(cli, env, mode):\n"
        "    if _hang(30.0) is None:  # a hung login:  the real helper allows 30 s\n"
        "        raise RuntimeError('login-timeout')\n"
        "    return 'never'\n"
        "def find_cli(env):\n"
        "    return None\n"
    )

    def use_stub_core(self, hang_lookup):
        pkg = self.root / "stub-home" / "fleet_secrets"
        pkg.mkdir(parents=True)
        (pkg / "core.py").write_text(self.STUB_CORE.format(hang_lookup=hang_lookup))
        for patcher in (
            mock.patch.object(source, "HELPER_LOADER", source._load_helper),
            mock.patch.object(source, "_helper_candidates", lambda: [str(pkg / "core.py")]),
            mock.patch.dict(source._HELPER_CACHE, clear=True),
            mock.patch.object(source, "TIMEOUT_SECONDS", 0.5),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_timed_out_fast(self):
        self.fake.set(secrets=GOOD)
        started = time.monotonic()
        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0, "the whole fetch is bounded by TIMEOUT_SECONDS")
        self.assertEqual((knob.source, knob.stale), ("default", True))
        self.assertEqual(knobs.report()["fetch_reason"], "timeout")
        self.assertEqual(self.fake.calls(), [], "no export once the budget is spent")
        self.assertEqual(lkg.recent_failure(), "timeout", "the negative cache engages")

    def test_a_hung_keychain_lookup_is_cut_at_the_fetch_deadline(self):
        self.use_stub_core(hang_lookup=True)
        self.assert_timed_out_fast()

    def test_a_hung_machine_identity_login_is_cut_at_the_fetch_deadline(self):
        self.use_stub_core(hang_lookup=False)
        self.assert_timed_out_fast()

    def test_a_helper_that_overruns_without_a_subprocess_is_still_a_timeout(self):
        class Slow(FakeHelper):
            def machine_token(self, cli, env, mode):
                time.sleep(0.7)
                return FAKE_TOKEN

        self.use_helper(Slow())
        with mock.patch.object(source, "TIMEOUT_SECONDS", 0.5):
            self.fake.set(secrets=GOOD)
            knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "timeout")
        self.assertEqual(self.fake.calls(), [])

    def test_the_clamp_touches_only_the_private_helper_copy(self):
        self.assertIs(sys.modules["subprocess"].run, __import__("subprocess").run)
        self.assertIsNone(source._remaining(), "no budget outside a fetch")

    def test_the_per_fetch_timeout_is_short(self):
        self.assertLessEqual(source.TIMEOUT_SECONDS, 10.0)


class NegativeCacheTests(KnobTestCase):
    @property
    def marker(self):
        return self.state_dir / "fetch-failed.json"

    def test_a_failed_fetch_leaves_a_private_marker_with_no_output(self):
        self.fake.set(mode="fail", leak="leakedStderrSentinel")
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertTrue(self.marker.exists())
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        doc = json.loads(self.marker.read_text())
        self.assertEqual(set(doc), {"failed_epoch", "reason"})
        self.assertEqual(doc["reason"], "exit-1")
        self.assertNotIn("leakedStderrSentinel", self.marker.read_text())

    def test_a_sibling_skips_the_fetch_inside_the_window_and_uses_the_copy(self):
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")  # warm the copy
        self.new_process()
        self.fake.set(mode="fail")
        knobs.get("FLEET_AUTOPUSH_MODE")  # fails, writes the marker
        self.assertEqual(len(self.fake.calls()), 2)
        self.new_process(keep_failure_marker=True)
        self.fake.set(secrets=GOOD)
        knob = knobs.resolve("FLEET_AUTOPUSH_MODE")
        self.assertEqual(len(self.fake.calls()), 2, "the sibling did not run the CLI")
        self.assertEqual((knob.value, knob.source, knob.stale), ("snapshot", "lkg", True))
        self.assertEqual(knobs.report()["fetch_reason"], "recent-exit-1")

    def test_a_secret_still_fails_closed_inside_the_window(self):
        lkg.record_failure("timeout")
        self.fake.set(secrets={"FLEET_FACTS_SIGNING_KEY": "x" * 40})
        with self.assertRaises(knobs.SecretUnavailable):
            knobs.get("FLEET_FACTS_SIGNING_KEY")
        self.assertEqual(self.fake.calls(), [])

    def test_the_marker_expires(self):
        lkg.record_failure("exit-1", now=time.time() - lkg.FAILURE_TTL_SECONDS - 1)
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.assertEqual(len(self.fake.calls()), 1)

    def test_a_good_fetch_clears_the_marker(self):
        lkg.record_failure("exit-1", now=time.time() - lkg.FAILURE_TTL_SECONDS - 1)
        self.fake.set(secrets=GOOD)
        knobs.get("FLEET_AUTOPUSH_MODE")
        self.assertFalse(self.marker.exists())

    def test_refresh_retries_even_inside_the_window(self):
        lkg.record_failure("exit-1")
        self.fake.set(secrets=GOOD)
        knobs.refresh()
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.assertEqual(len(self.fake.calls()), 1)

    def test_an_untrusted_marker_is_ignored(self):
        lkg.record_failure("exit-1")
        os.chmod(self.marker, 0o644)
        self.fake.set(secrets=GOOD)
        self.assertEqual(knobs.resolve("FLEET_AUTOPUSH_MODE").source, "infisical")
        self.marker.unlink(missing_ok=True)
        self.marker.symlink_to(self.root / "elsewhere.json")
        (self.root / "elsewhere.json").write_text(
            json.dumps({"failed_epoch": time.time(), "reason": "exit-1"})
        )
        self.new_process(keep_failure_marker=True)
        self.assertIsNone(lkg.recent_failure())

    def test_a_reason_is_reduced_to_a_token(self):
        lkg.record_failure("exit-1 with " + "spaces/and\nnewlines" * 10)
        reason = lkg.recent_failure()
        self.assertLessEqual(len(reason), 64)
        self.assertTrue(set(reason) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_:"))

    def test_a_timeout_is_paid_once_across_siblings(self):
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
