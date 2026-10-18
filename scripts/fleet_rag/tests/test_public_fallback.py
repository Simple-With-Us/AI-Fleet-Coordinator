"""Unit tests for fleet_rag.public_fallback: the Tailscale-down -> public-service fallback
decision used by the `recall` CLI and the stdio `fleet-recall-mcp.py`.

    cd scripts && python3 -m unittest fleet_rag.tests.test_public_fallback -v

No network: `call_public`'s HTTP call goes through `core.http_json`, which is monkeypatched in
every test that reaches it.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleet_rag import core, public_fallback, recall_api
from fleet_rag.core import FleetRagError

PUBLIC_ENV = {
    "RECALL_API_TOKEN": "tok-123",
    "CF_ACCESS_CLIENT_ID": "id-123",
    "CF_ACCESS_CLIENT_SECRET": "secret-123",
}


# --------------------------------------------------------------------------- tailscale status


class TailscaleStatusTextTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(public_fallback.SKIP_PRIVATE_ENV, None)

    def tearDown(self):
        self.env.stop()

    def test_missing_binary_returns_none(self):
        def boom(*a, **k):
            raise FileNotFoundError("no such file")

        self.assertIsNone(public_fallback.tailscale_status_text(run=boom))

    def test_timeout_returns_none(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="tailscale", timeout=3)

        self.assertIsNone(public_fallback.tailscale_status_text(run=boom))

    def test_other_oserror_returns_none(self):
        def boom(*a, **k):
            raise PermissionError("nope")

        self.assertIsNone(public_fallback.tailscale_status_text(run=boom))

    def test_combines_stdout_and_stderr(self):
        def fake_run(cmd, capture_output, text, timeout, env=None):
            self.assertEqual(cmd, [public_fallback.TAILSCALE_BIN, "status"])
            self.assertTrue(capture_output)
            self.assertTrue(text)
            self.assertEqual((env or {}).get("TAILSCALE_BE_CLI"), "1")
            return SimpleNamespace(stdout="100.1.2.3  mac  online\n", stderr="# Health check:\n")

        out = public_fallback.tailscale_status_text(run=fake_run)
        self.assertIn("online", out)
        self.assertIn("Health check", out)

    def test_none_stdout_or_stderr_do_not_crash(self):
        out = public_fallback.tailscale_status_text(
            run=lambda *a, **k: SimpleNamespace(stdout=None, stderr=None)
        )
        self.assertEqual(out, "")

    def test_skip_private_does_not_start_tailscale(self):
        def boom(*a, **k):
            raise AssertionError("must not invoke Tailscale.app")

        os.environ[public_fallback.SKIP_PRIVATE_ENV] = "1"
        out = public_fallback.tailscale_status_text(run=boom)
        self.assertIn("RECALL_SKIP_PRIVATE", out)


class TailscaleBelievedDownTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(public_fallback.SKIP_PRIVATE_ENV, None)

    def tearDown(self):
        self.env.stop()

    def test_none_is_never_down(self):
        self.assertFalse(public_fallback.tailscale_believed_down(None))

    def test_empty_string_is_never_down(self):
        self.assertFalse(public_fallback.tailscale_believed_down(""))

    def test_online_status_is_not_down(self):
        text = "100.113.106.39  macbook  userid:5254496729001592  macOS  online\n"
        self.assertFalse(public_fallback.tailscale_believed_down(text))

    def test_stopped_is_down(self):
        text = "100.113.106.39  macbook  userid:...  macOS  offline\n\nTailscale is stopped.\n"
        self.assertTrue(public_fallback.tailscale_believed_down(text))

    def test_logged_out_is_down_case_insensitive(self):
        text = "# Health check:\n#     - You are LOGGED OUT. The last login error was: ...\n"
        self.assertTrue(public_fallback.tailscale_believed_down(text))

    def test_clierror_3_is_down(self):
        text = (
            "The Tailscale GUI failed to start: The operation couldn’t be completed. "
            "(Tailscale.CLIError error 3.)\n"
        )
        self.assertTrue(public_fallback.tailscale_believed_down(text))

    def test_gui_failed_to_start_is_down(self):
        self.assertTrue(
            public_fallback.tailscale_believed_down(
                "The Tailscale GUI failed to start: The operation couldn’t be completed.\n"
            )
        )

    def test_skip_private_env_is_down_even_for_unknown(self):
        os.environ[public_fallback.SKIP_PRIVATE_ENV] = "1"
        self.assertTrue(public_fallback.tailscale_believed_down(None))


# --------------------------------------------------------------------------- routing-table check


class PrivateRouteBypassesTailscaleTests(unittest.TestCase):
    """private_route_bypasses_tailscale(): the macOS-only corroborating signal used when
    tailscale_status_text() itself returns None.  Every test injects `run` (or patches
    sys.platform) so no real subprocess is ever spawned."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("QDRANT_URL", None)
        self.darwin = mock.patch.object(public_fallback.sys, "platform", "darwin")
        self.darwin.start()

    def tearDown(self):
        self.darwin.stop()
        self.env.stop()

    @staticmethod
    def _route_run(interface: str):
        def run(cmd, capture_output, text, timeout):
            assert cmd[0] == public_fallback.ROUTE_BIN
            return SimpleNamespace(
                stdout=f"   route to: {cmd[-1]}\ndestination: default\n"
                f"       interface: {interface}\n",
                stderr="",
            )

        return run

    def test_utun_interface_is_not_a_bypass(self):
        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale(
                "http://100.69.77.26:6333", run=self._route_run("utun4")
            )
        )

    def test_en0_interface_is_a_bypass(self):
        self.assertTrue(
            public_fallback.private_route_bypasses_tailscale(
                "http://100.69.77.26:6333", run=self._route_run("en0")
            )
        )

    def test_route_command_failure_is_not_a_bypass(self):
        def boom(cmd, capture_output, text, timeout):
            raise OSError("no such file")

        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale("http://100.69.77.26:6333", run=boom)
        )

    def test_route_timeout_is_not_a_bypass(self):
        def boom(cmd, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd="route", timeout=timeout)

        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale("http://100.69.77.26:6333", run=boom)
        )

    def test_unparseable_output_is_not_a_bypass(self):
        run = lambda cmd, capture_output, text, timeout: SimpleNamespace(
            stdout="garbage\n", stderr=""
        )
        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale("http://100.69.77.26:6333", run=run)
        )

    def test_non_darwin_is_never_a_bypass_even_with_en0(self):
        self.darwin.stop()
        with mock.patch.object(public_fallback.sys, "platform", "linux"):
            self.assertFalse(
                public_fallback.private_route_bypasses_tailscale(
                    "http://100.69.77.26:6333", run=self._route_run("en0")
                )
            )
        self.darwin.start()  # tearDown() stops it again

    def test_non_cgnat_host_is_never_a_bypass(self):
        run = mock.Mock(side_effect=AssertionError("must not shell out for a non-CGNAT host"))
        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale(
                "http://qdrant.example.com:6333", run=run
            )
        )
        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale("http://8.8.8.8:6333", run=run)
        )

    def test_defaults_to_qdrant_url_env_then_documented_default(self):
        os.environ["QDRANT_URL"] = "http://100.69.77.26:6333"
        self.assertTrue(
            public_fallback.private_route_bypasses_tailscale(run=self._route_run("en0"))
        )
        os.environ.pop("QDRANT_URL")
        self.assertTrue(
            public_fallback.private_route_bypasses_tailscale(run=self._route_run("en0"))
        )

    def test_unresolvable_host_is_never_a_bypass(self):
        run = mock.Mock(side_effect=AssertionError("must not shell out for a hostname"))
        self.assertFalse(
            public_fallback.private_route_bypasses_tailscale("http://recall.jays.services", run=run)
        )


# --------------------------------------------------------------------------- combined verdict


class DirectPathBlockedTests(unittest.TestCase):
    def test_tailscale_evidence_blocks_without_consulting_the_route(self):
        with mock.patch.object(
            public_fallback,
            "private_route_bypasses_tailscale",
            side_effect=AssertionError("must not need route evidence"),
        ):
            self.assertTrue(public_fallback.direct_path_blocked("Tailscale is stopped.\n"))

    def test_known_up_status_is_never_blocked(self):
        with mock.patch.object(
            public_fallback,
            "private_route_bypasses_tailscale",
            side_effect=AssertionError("must not need route evidence"),
        ):
            self.assertFalse(public_fallback.direct_path_blocked("100.1.2.3  mac  online\n"))

    def test_unknown_status_defers_to_the_route_check(self):
        with mock.patch.object(
            public_fallback, "private_route_bypasses_tailscale", return_value=True
        ):
            self.assertTrue(public_fallback.direct_path_blocked(None))
        with mock.patch.object(
            public_fallback, "private_route_bypasses_tailscale", return_value=False
        ):
            self.assertFalse(public_fallback.direct_path_blocked(None))


# --------------------------------------------------------------------------- connection errors


class IsConnectionErrorTests(unittest.TestCase):
    def test_reaching_message_is_a_connection_error(self):
        self.assertTrue(
            public_fallback.is_connection_error(
                FleetRagError("RemoteDisconnected reaching 100.69.77.26:8081")
            )
        )

    def test_request_failed_message_is_a_connection_error(self):
        self.assertTrue(
            public_fallback.is_connection_error(FleetRagError("request failed: TimeoutError"))
        )

    def test_http_status_error_is_not_a_connection_error(self):
        self.assertFalse(
            public_fallback.is_connection_error(
                FleetRagError(
                    'HTTP 401 from recall.jays.services/recall/stats: {"error": "unauthorized"}'
                )
            )
        )

    def test_missing_credentials_is_not_a_connection_error(self):
        self.assertFalse(
            public_fallback.is_connection_error(
                FleetRagError(
                    "missing credentials: TEI_URL, QDRANT_URL (set them in the environment...)"
                )
            )
        )

    def test_validation_error_is_not_a_connection_error(self):
        self.assertFalse(
            public_fallback.is_connection_error(
                FleetRagError("category must be one of lesson|preference (got 'bogus')")
            )
        )


# --------------------------------------------------------------------------- fake-backend guard


class UsingFakeBackendTests(unittest.TestCase):
    def setUp(self):
        self._saved_qdrant = recall_api.Qdrant
        self._saved_env = os.environ.get("FLEET_RECALL_FAKE")

    def tearDown(self):
        recall_api.Qdrant = self._saved_qdrant
        if self._saved_env is None:
            os.environ.pop("FLEET_RECALL_FAKE", None)
        else:
            os.environ["FLEET_RECALL_FAKE"] = self._saved_env

    def test_env_flag_is_true_regardless_of_backend(self):
        os.environ["FLEET_RECALL_FAKE"] = "1"
        recall_api.Qdrant = core.Qdrant  # the real client
        self.assertTrue(public_fallback.using_fake_backend())

    def test_fake_qdrant_class_is_detected(self):
        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = recall_api.FakeQdrant
        self.assertTrue(public_fallback.using_fake_backend())

    def test_fake_qdrant_subclass_is_detected(self):
        # A test elsewhere in the suite subclasses FakeQdrant to misbehave one method (e.g.
        # BoomQdrant in test_mcp.py) -- that must still count as "fake", never reach the network.
        class BoomQdrant(recall_api.FakeQdrant):
            pass

        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = BoomQdrant
        self.assertTrue(public_fallback.using_fake_backend())

    def test_real_qdrant_is_not_fake(self):
        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = core.Qdrant
        self.assertFalse(public_fallback.using_fake_backend())


class GuardMayRunTests(unittest.TestCase):
    def setUp(self):
        self._saved_qdrant = recall_api.Qdrant
        self._saved_env = os.environ.get("FLEET_RECALL_FAKE")
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("QDRANT_URL", None)
        os.environ.pop("TEI_URL", None)
        os.environ.pop(public_fallback.SKIP_PRIVATE_ENV, None)

    def tearDown(self):
        self.env.stop()
        recall_api.Qdrant = self._saved_qdrant
        if self._saved_env is None:
            os.environ.pop("FLEET_RECALL_FAKE", None)
        else:
            os.environ["FLEET_RECALL_FAKE"] = self._saved_env

    def test_fake_backend_always_may_run_even_if_tailscale_looks_down(self):
        os.environ["FLEET_RECALL_FAKE"] = "1"
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            side_effect=AssertionError("must not probe Tailscale for a fake backend"),
        ):
            self.assertTrue(public_fallback.guard_may_run())

    def test_real_backend_may_run_when_tailscale_is_up(self):
        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = core.Qdrant
        with mock.patch.object(public_fallback, "tailscale_status_text", return_value=None):
            self.assertTrue(public_fallback.guard_may_run())

    def test_real_backend_may_not_run_when_tailscale_is_down(self):
        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = core.Qdrant
        with mock.patch.object(
            public_fallback, "tailscale_status_text", return_value="Tailscale is stopped.\n"
        ):
            self.assertFalse(public_fallback.guard_may_run())

    def test_local_override_may_run_even_if_tailscale_looks_down(self):
        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = core.Qdrant
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            side_effect=AssertionError("must not probe Tailscale with an override active"),
        ):
            self.assertTrue(public_fallback.guard_may_run())

    def test_skip_private_may_not_run_even_with_override(self):
        os.environ.pop("FLEET_RECALL_FAKE", None)
        recall_api.Qdrant = core.Qdrant
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        os.environ[public_fallback.SKIP_PRIVATE_ENV] = "1"
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            side_effect=AssertionError("must not probe Tailscale when skip-private is set"),
        ):
            self.assertFalse(public_fallback.guard_may_run())


class LocalOverrideActiveTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("QDRANT_URL", None)
        os.environ.pop("TEI_URL", None)

    def tearDown(self):
        self.env.stop()

    def test_both_set_is_active(self):
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        self.assertTrue(public_fallback.local_override_active())

    def test_neither_set_is_inactive(self):
        self.assertFalse(public_fallback.local_override_active())

    def test_only_qdrant_is_inactive(self):
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        self.assertFalse(public_fallback.local_override_active())

    def test_only_tei_is_inactive(self):
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        self.assertFalse(public_fallback.local_override_active())

    def test_blank_values_are_inactive(self):
        os.environ["QDRANT_URL"] = "   "
        os.environ["TEI_URL"] = ""
        self.assertFalse(public_fallback.local_override_active())


class RequireDirectPathTests(unittest.TestCase):
    def setUp(self):
        self._saved_qdrant = recall_api.Qdrant
        self._saved_env = os.environ.get("FLEET_RECALL_FAKE")
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("QDRANT_URL", None)
        os.environ.pop("TEI_URL", None)
        os.environ.pop("FLEET_RECALL_FAKE", None)
        os.environ.pop(public_fallback.SKIP_PRIVATE_ENV, None)
        recall_api.Qdrant = core.Qdrant

    def tearDown(self):
        self.env.stop()
        recall_api.Qdrant = self._saved_qdrant
        if self._saved_env is None:
            os.environ.pop("FLEET_RECALL_FAKE", None)
        else:
            os.environ["FLEET_RECALL_FAKE"] = self._saved_env

    def test_raises_one_actionable_line_when_tailscale_down_and_no_override(self):
        with mock.patch.object(
            public_fallback, "tailscale_status_text", return_value="Logged out.\n"
        ), self.assertRaises(FleetRagError) as ctx:
            public_fallback.require_direct_path("eval")
        msg = str(ctx.exception)
        self.assertNotIn("\n", msg)
        self.assertIn("recall eval", msg)
        self.assertIn("tailscale login", msg)
        self.assertIn("recall-tunnel up", msg)
        self.assertIn('eval "$(recall-tunnel env)"', msg)

    def test_no_op_when_tailscale_status_is_unknown(self):
        with mock.patch.object(public_fallback, "tailscale_status_text", return_value=None):
            public_fallback.require_direct_path("ingest")  # must not raise

    def test_clierror_3_fail_fasts_without_the_private_path(self):
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            return_value="The Tailscale GUI failed to start: (Tailscale.CLIError error 3.)",
        ), self.assertRaises(FleetRagError) as ctx:
            public_fallback.require_direct_path("ingest")
        self.assertIn("recall ingest", str(ctx.exception))

    def test_no_op_when_tailscale_is_up(self):
        with mock.patch.object(public_fallback, "tailscale_status_text", return_value="online\n"):
            public_fallback.require_direct_path("doctor")  # must not raise

    def test_override_active_skips_the_probe_entirely(self):
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            side_effect=AssertionError("must not probe Tailscale with an override active"),
        ):
            public_fallback.require_direct_path("eval")  # must not raise

    def test_fake_backend_skips_the_probe_entirely(self):
        os.environ["FLEET_RECALL_FAKE"] = "1"
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            side_effect=AssertionError("must not probe Tailscale for a fake backend"),
        ):
            public_fallback.require_direct_path("eval")  # must not raise


# --------------------------------------------------------------------------- credential resolution


class PublicCredentialsFileFallbackTests(unittest.TestCase):
    """public_credentials() reads a name from its file (the Access pair) or from `fleet-secrets`
    (RECALL_API_TOKEN) when the environment has none -- the stock `python3 fleet-recall-mcp.py`
    MCP registration never sources these into os.environ, so this is what makes the fallback
    work in practice."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {k: "" for k in public_fallback.PUBLIC_ENV_KEYS})
        self.env.start()
        # Never shell out to a real `fleet-secrets` from a test.
        self.fs = mock.patch.object(core, "fleet_secret", return_value=None)
        self.fleet_secret = self.fs.start()

    def tearDown(self):
        self.fs.stop()
        self.env.stop()

    def test_env_value_wins_over_file(self):
        with mock.patch.object(public_fallback, "_read_named_line", return_value="from-file"):
            with mock.patch.dict(
                os.environ, {"RECALL_API_TOKEN": "from-env", "CF_ACCESS_CLIENT_ID": "id-env"}
            ):
                resolved = public_fallback._resolve_public_env()
        self.assertEqual(resolved["RECALL_API_TOKEN"], "from-env")
        self.assertEqual(resolved["CF_ACCESS_CLIENT_ID"], "id-env")
        self.fleet_secret.assert_not_called()

    def test_falls_back_to_file_and_fleet_secrets_when_env_is_blank(self):
        def fake_read(path, name):
            return {
                "CF_ACCESS_CLIENT_ID": "id-from-file",
                "CF_ACCESS_CLIENT_SECRET": "secret-from-file",
            }[name]

        self.fleet_secret.return_value = "tok-from-fleet-secrets"
        with mock.patch.object(public_fallback, "_read_named_line", side_effect=fake_read):
            creds = public_fallback.public_credentials()
        self.assertEqual(
            creds,
            {
                "RECALL_API_TOKEN": "tok-from-fleet-secrets",
                "CF_ACCESS_CLIENT_ID": "id-from-file",
                "CF_ACCESS_CLIENT_SECRET": "secret-from-file",
            },
        )
        self.fleet_secret.assert_called_once_with("RECALL_API_TOKEN")

    def test_none_when_the_file_and_fleet_secrets_are_also_missing(self):
        with mock.patch.object(public_fallback, "_read_named_line", return_value=None):
            self.assertIsNone(public_fallback.public_credentials())

    def test_read_named_line_parses_a_real_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "handoff.env"
            path.write_text('OTHER=ignored\nRECALL_API_TOKEN="quoted-value"\nTRAILING=x\n')
            self.assertEqual(
                public_fallback._read_named_line(path, "RECALL_API_TOKEN"), "quoted-value"
            )
            self.assertIsNone(public_fallback._read_named_line(path, "NOT_PRESENT"))
            self.assertIsNone(
                public_fallback._read_named_line(path.parent / "missing.env", "RECALL_API_TOKEN")
            )


class CallPublicTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, PUBLIC_ENV, clear=False)
        self.env.start()
        # Isolate from whatever handoff files genuinely exist on the machine running the
        # suite -- these tests control credentials via os.environ only.
        self.no_files = mock.patch.object(public_fallback, "_read_named_line", return_value=None)
        self.no_files.start()
        self.no_fs = mock.patch.object(core, "fleet_secret", return_value=None)
        self.no_fs.start()

    def tearDown(self):
        self.no_fs.stop()
        self.no_files.stop()
        self.env.stop()

    def test_missing_credentials_names_the_missing_keys(self):
        with mock.patch.dict(os.environ, {"CF_ACCESS_CLIENT_SECRET": ""}):
            with self.assertRaisesRegex(FleetRagError, "CF_ACCESS_CLIENT_SECRET"):
                public_fallback.call_public("recall_stats", {})

    def test_strips_ok_true_on_success(self):
        with mock.patch.object(core, "http_json", return_value={"ok": True, "points": 37047}):
            res = public_fallback.call_public("recall_stats", {})
        self.assertEqual(res, {"points": 37047})

    def test_ok_false_becomes_fleetragerror(self):
        with mock.patch.object(
            core, "http_json", return_value={"ok": False, "error": "unauthorized"}
        ), self.assertRaisesRegex(FleetRagError, "unauthorized"):
            public_fallback.call_public("recall_stats", {})

    def test_sends_bearer_and_access_headers_and_the_right_route(self):
        seen = {}

        def fake_http_json(url, body, headers, method=None, timeout=None, retries=None):
            seen.update(url=url, body=body, headers=headers, method=method)
            return {"ok": True, "hits": [], "mode": "dense"}

        with mock.patch.object(core, "http_json", fake_http_json):
            res = public_fallback.call_public("recall_search", {"query": "x", "limit": 3})
        self.assertEqual(seen["url"], "https://recall.jays.services/recall/search")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], {"query": "x", "limit": 3})
        self.assertEqual(seen["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(seen["headers"]["CF-Access-Client-Id"], "id-123")
        self.assertEqual(seen["headers"]["CF-Access-Client-Secret"], "secret-123")
        # A default urllib User-Agent trips Cloudflare's bot management (403, "error code:
        # 1010") in front of recall.jays.services -- this must never regress to the default.
        self.assertNotIn("python-urllib", seen["headers"]["User-Agent"].lower())
        self.assertEqual(res, {"hits": [], "mode": "dense"})

    def test_stats_is_a_get_with_no_body(self):
        seen = {}

        def fake_http_json(url, body, headers, method=None, timeout=None, retries=None):
            seen.update(url=url, body=body, method=method)
            return {"ok": True, "points": 1}

        with mock.patch.object(core, "http_json", fake_http_json):
            public_fallback.call_public("recall_stats", {})
        self.assertEqual(seen["method"], "GET")
        self.assertIsNone(seen["body"])

    def test_unknown_tool_name_rejected(self):
        with self.assertRaisesRegex(FleetRagError, "no public REST route"):
            public_fallback.call_public("recall_ingest", {})


# --------------------------------------------------------------------------- call_with_fallback


class CallWithFallbackTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, PUBLIC_ENV, clear=False)
        self.env.start()
        os.environ.pop("FLEET_RECALL_FAKE", None)
        os.environ.pop("QDRANT_URL", None)
        os.environ.pop("TEI_URL", None)
        os.environ.pop(public_fallback.SKIP_PRIVATE_ENV, None)
        # Isolate from whatever handoff files genuinely exist on the machine running the
        # suite -- these tests control credentials via os.environ only.
        self.no_files = mock.patch.object(public_fallback, "_read_named_line", return_value=None)
        self.no_files.start()
        # Deterministic regardless of the platform/network this suite happens to run on (this
        # Mac's own route to the documented default Qdrant host is a real bypass signal per
        # PrivateRouteBypassesTailscaleTests) -- tests that care about the route-check branch
        # override this explicitly.
        self.no_route_bypass = mock.patch.object(
            public_fallback, "private_route_bypasses_tailscale", return_value=False
        )
        self.no_route_bypass.start()
        self._saved_qdrant = recall_api.Qdrant
        recall_api.Qdrant = core.Qdrant  # ensure using_fake_backend() is False in this class

    def tearDown(self):
        self.no_route_bypass.stop()
        self.no_files.stop()
        self.env.stop()
        recall_api.Qdrant = self._saved_qdrant

    def test_local_success_never_touches_public(self):
        local = mock.Mock(return_value={"points": 5})
        with mock.patch.object(public_fallback, "call_public") as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats", {}, local, status_probe=lambda: None
            )
        local.assert_called_once()
        pub.assert_not_called()
        self.assertEqual(res, {"points": 5})

    def test_tailscale_believed_down_skips_local_entirely(self):
        local = mock.Mock(side_effect=AssertionError("must not run the local path"))
        with mock.patch.object(public_fallback, "call_public", return_value={"points": 1}) as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats", {}, local, status_probe=lambda: "Tailscale is stopped.\n"
            )
        local.assert_not_called()
        pub.assert_called_once_with("recall_stats", {})
        self.assertEqual(res, {"points": 1})

    def test_clierror_3_skips_local_and_does_not_wait_on_qdrant(self):
        local = mock.Mock(side_effect=AssertionError("must not run the 120s private path"))
        with mock.patch.object(public_fallback, "call_public", return_value={"points": 2}) as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats",
                {},
                local,
                status_probe=lambda: (
                    "The Tailscale GUI failed to start: (Tailscale.CLIError error 3.)"
                ),
            )
        local.assert_not_called()
        pub.assert_called_once_with("recall_stats", {})
        self.assertEqual(res, {"points": 2})

    def test_skip_private_skips_local_even_with_override(self):
        os.environ[public_fallback.SKIP_PRIVATE_ENV] = "1"
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        local = mock.Mock(side_effect=AssertionError("must not run the local path"))
        with mock.patch.object(public_fallback, "call_public", return_value={"points": 7}) as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats",
                {},
                local,
                status_probe=lambda: (_ for _ in ()).throw(
                    AssertionError("must not probe Tailscale when skip-private is set")
                ),
            )
        local.assert_not_called()
        pub.assert_called_once_with("recall_stats", {})
        self.assertEqual(res, {"points": 7})

    def test_route_table_bypass_falls_back_when_tailscale_status_is_unknown(self):
        # status_probe -> None (no Tailscale.app to ask) but the macOS routing table shows the
        # private Qdrant host resolves through en0, not a utun interface -- positive evidence
        # the direct path would just hang until core.DEFAULT_TIMEOUT, so this goes straight to
        # the public twin instead of trying the 120s local path first.
        local = mock.Mock(side_effect=AssertionError("must not run the 120s private path"))
        with mock.patch.object(
            public_fallback, "private_route_bypasses_tailscale", return_value=True
        ), mock.patch.object(
            public_fallback, "call_public", return_value={"points": 11}
        ) as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats", {}, local, status_probe=lambda: None
            )
        local.assert_not_called()
        pub.assert_called_once_with("recall_stats", {})
        self.assertEqual(res, {"points": 11})

    def test_connection_error_falls_back(self):
        local = mock.Mock(
            side_effect=FleetRagError("RemoteDisconnected reaching 100.69.77.26:8081")
        )
        with mock.patch.object(public_fallback, "call_public", return_value={"points": 9}) as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats", {}, local, status_probe=lambda: None
            )
        pub.assert_called_once()
        self.assertEqual(res, {"points": 9})

    def test_non_connection_error_is_never_swallowed(self):
        local = mock.Mock(side_effect=FleetRagError("category must be one of lesson|preference"))
        with mock.patch.object(public_fallback, "call_public") as pub:
            with self.assertRaisesRegex(FleetRagError, "category must be one of"):
                public_fallback.call_with_fallback(
                    "recall_search", {"query": "x"}, local, status_probe=lambda: None
                )
        pub.assert_not_called()

    def test_actionable_message_when_public_also_fails(self):
        local = mock.Mock(
            side_effect=FleetRagError("RemoteDisconnected reaching 100.69.77.26:8081")
        )
        pub_err = FleetRagError(
            "public fallback (https://recall.jays.services/recall/stats) "
            "rejected the request: unauthorized"
        )
        with mock.patch.object(public_fallback, "call_public", side_effect=pub_err):
            with self.assertRaises(FleetRagError) as ctx:
                public_fallback.call_with_fallback(
                    "recall_stats", {}, local, status_probe=lambda: None
                )
        msg = str(ctx.exception)
        self.assertIn("Tailscale TEI/Qdrant path is unreachable", msg)
        self.assertIn("unauthorized", msg)
        self.assertIn("tailscale login", msg)

    def test_actionable_message_when_tailscale_down_and_credentials_missing(self):
        local = mock.Mock(side_effect=AssertionError("must not run"))
        with mock.patch.dict(
            os.environ, {"CF_ACCESS_CLIENT_ID": "", "CF_ACCESS_CLIENT_SECRET": ""}
        ), self.assertRaises(FleetRagError) as ctx:
            public_fallback.call_with_fallback(
                "recall_stats", {}, local, status_probe=lambda: "Logged out.\n"
            )
        msg = str(ctx.exception)
        self.assertIn("Tailscale is logged out on this Mac", msg)
        self.assertIn("CF_ACCESS_CLIENT_ID", msg)
        self.assertIn("CF_ACCESS_CLIENT_SECRET", msg)
        local.assert_not_called()

    def test_recall_contribute_drops_force_on_fallback(self):
        local = mock.Mock(
            side_effect=FleetRagError("ConnectionRefusedError reaching 100.69.77.26:8081")
        )
        captured = {}

        def fake_call_public(name, kwargs):
            captured.update(name=name, kwargs=kwargs)
            return {"id": "x", "doc_id": "contrib/X/d/1", "status": "completed"}

        kwargs = {"text": "x" * 60, "category": "lesson", "seat": "CURSOR", "force": False}
        with mock.patch.object(public_fallback, "call_public", fake_call_public):
            public_fallback.call_with_fallback(
                "recall_contribute", kwargs, local, status_probe=lambda: None
            )
        self.assertEqual(captured["name"], "recall_contribute")
        self.assertNotIn("force", captured["kwargs"])

    def test_recall_search_kwargs_pass_through_on_fallback(self):
        """per_doc / rerank / prefer_lessons now have a public route and must reach it; a
        None-valued optional (since_days) and any genuinely unknown key are still dropped."""
        local = mock.Mock(
            side_effect=FleetRagError("ConnectionRefusedError reaching 100.69.77.26:8081")
        )
        captured = {}

        def fake_call_public(name, kwargs):
            captured.update(name=name, kwargs=kwargs)
            return {"hits": [], "mode": "dense"}

        kwargs = {
            "query": "handoff file",
            "limit": 3,
            "per_doc": 2,
            "rerank": False,
            "prefer_lessons": True,
            "since_days": None,
            "bogus_local_only": "x",
        }
        with mock.patch.object(public_fallback, "call_public", fake_call_public):
            public_fallback.call_with_fallback(
                "recall_search", kwargs, local, status_probe=lambda: None
            )
        self.assertEqual(
            captured["kwargs"],
            {
                "query": "handoff file",
                "limit": 3,
                "per_doc": 2,
                "rerank": False,
                "prefer_lessons": True,
            },
        )

    def test_local_override_skips_the_tailscale_probe_and_tries_local_first(self):
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        local = mock.Mock(return_value={"points": 42})
        with mock.patch.object(
            public_fallback,
            "call_public",
            side_effect=AssertionError("must not call the public service"),
        ):
            res = public_fallback.call_with_fallback(
                "recall_stats",
                {},
                local,
                status_probe=lambda: (_ for _ in ()).throw(
                    AssertionError("must not probe Tailscale with an override active")
                ),
            )
        local.assert_called_once()
        self.assertEqual(res, {"points": 42})

    def test_local_override_still_falls_back_on_a_connection_error(self):
        os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
        os.environ["TEI_URL"] = "http://127.0.0.1:18081"
        local = mock.Mock(side_effect=FleetRagError("RemoteDisconnected reaching 127.0.0.1:18081"))
        with mock.patch.object(public_fallback, "call_public", return_value={"points": 9}) as pub:
            res = public_fallback.call_with_fallback(
                "recall_stats",
                {},
                local,
                status_probe=lambda: (_ for _ in ()).throw(
                    AssertionError("must not probe Tailscale with an override active")
                ),
            )
        pub.assert_called_once()
        self.assertEqual(res, {"points": 9})

    def test_fake_backend_bypasses_everything(self):
        recall_api.Qdrant = recall_api.FakeQdrant
        local = mock.Mock(return_value={"points": 3})
        with mock.patch.object(
            public_fallback,
            "tailscale_status_text",
            side_effect=AssertionError("must not probe Tailscale for a fake backend"),
        ), mock.patch.object(
            public_fallback,
            "call_public",
            side_effect=AssertionError("must not call the public service"),
        ):
            res = public_fallback.call_with_fallback("recall_stats", {}, local)
        self.assertEqual(res, {"points": 3})
        local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
