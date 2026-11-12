"""Seat self-test:  every step and refusal cause, with a fake gateway and no live service."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from simple_tools import selftest as S

TOKEN = "sbt_claude_" + "q" * 43
SEAT = "CLAUDE"


def ok(structured=None):
    return {"content": [], "structuredContent": structured or {}}


def refusal(code, reason=None):
    obj = {"code": code, **({"reason": reason} if reason else {})}
    return {"content": [{"type": "text", "text": json.dumps(obj)}], "isError": True,
            "_meta": {"simple-tools/error": obj}}


class Gateway:
    """A fake hosted gateway.  `tools` maps a tool name to a result dict (or an HTTP status int)."""

    def __init__(self, tools=None, status=200, whoami_seat=SEAT, sse=False, version="9.9.9"):
        self.tools = tools or {}
        self.status = status
        self.whoami_seat = whoami_seat
        self.sse = sse
        self.version = version
        self.calls = []
        self.tokens = []

    def __call__(self, body, token, timeout):
        self.tokens.append(token)
        req = json.loads(body)
        self.calls.append(req)
        if self.status != 200:
            return self.status, "", b""
        method, params = req["method"], req.get("params", {})
        if method == "initialize":
            result = {"serverInfo": {"name": "st", "version": self.version}}
        else:
            name = params["name"]
            if name == "whoami":
                result = ok({"seat": self.whoami_seat, "authenticated_seat": self.whoami_seat})
            else:
                result = self.tools.get(name, ok({}))
        reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
        if self.sse:
            return 200, "text/event-stream", ("event: message\ndata: " + reply + "\n\n").encode()
        return 200, "application/json", reply.encode()


class SelfTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()  # /var is a symlink on macOS
        d = self.home / ".secrets" / "fleet-tools"
        d.mkdir(parents=True, mode=0o700)
        self.bearer = d / "claude"
        self.bearer.write_text(TOKEN)
        self.bearer.chmod(0o600)
        self.home.chmod(0o700)
        (self.home / ".secrets").chmod(0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def run_selftest(self, gw, env=None, extra=()):
        out = io.StringIO()
        code = S.main(["--seat", SEAT, *extra], env=env or {}, home=self.home, transport=gw, out=out)
        return code, out.getvalue()


class HappyPathTests(SelfTestBase):
    def test_passes_with_a_leaked_agent_seat_and_prints_no_bearer(self):
        gw = Gateway(tools={"gr_wf_pr_search": refusal("relay_refused", "seat_policy")})
        code, text = self.run_selftest(gw, env={"AGENT_SEAT": "ZC", "AGENT_TAG": "ZC"})
        self.assertEqual(code, 0, text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("qqqq", text)
        self.assertIn("seat: CLAUDE", text)
        self.assertIn("RESULT ok", text)
        self.assertIn("ignored ambient AGENT_SEAT", text)
        self.assertIn("worker version: 9.9.9", text)
        self.assertIn("relay version: not exposed", text)
        # the advisory refusal is reported with its exact cause but is not fatal
        self.assertRegex(text, r"WARN advisory_pr_search\s+cause=seat_policy")

    def test_json_output_and_sse_replies(self):
        code, text = self.run_selftest(Gateway(sse=True), extra=["--json"])
        self.assertEqual(code, 0, text)
        data = json.loads(text)
        self.assertTrue(data["ok"])
        self.assertEqual([s["step"] for s in data["steps"]][:5],
                         ["bearer", "env", "whoami", "gr_read", "policy_read"])
        self.assertNotIn(TOKEN, text)

    def test_the_bearer_is_only_ever_sent_to_the_transport(self):
        gw = Gateway()
        self.run_selftest(gw)
        self.assertEqual(set(gw.tokens), {TOKEN})
        names = [c.get("params", {}).get("name") for c in gw.calls]
        self.assertEqual(names[1:], ["whoami", "gr_wf_repo_get", "gr_wf_pr_list_page",
                                     "gr_wf_pr_search", "gr_wf_repo_list"])

    def test_a_relay_version_is_shown_only_when_exposed(self):
        gw = Gateway(tools={"gr_wf_repo_get": ok({"relay_version": "7c333213"})})
        _, text = self.run_selftest(gw)
        self.assertIn("relay version: 7c333213", text)


class RefusalCauseTests(SelfTestBase):
    def assert_cause(self, gw, cause, step="gr_read", env=None):
        code, text = self.run_selftest(gw, env=env)
        self.assertEqual(code, 1, text)
        self.assertIn("RESULT failed", text)
        self.assertRegex(text, r"FAIL %s\s+cause=%s" % (step, cause))
        self.assertNotIn(TOKEN, text)

    def test_each_relay_cause_is_named_exactly(self):
        for reason in ("seat_not_in_policy", "bearer_rejected", "verifier_unavailable",
                       "no_app_credential", "seat_policy"):
            with self.subTest(reason=reason):
                gw = Gateway(tools={"gr_wf_repo_get": refusal("relay_refused", reason)})
                self.assert_cause(gw, reason)

    def test_policy_read_refusal_is_fatal(self):
        gw = Gateway(tools={"gr_wf_pr_list_page": refusal("relay_refused", "seat_policy")})
        self.assert_cause(gw, "seat_policy", step="policy_read")

    def test_gateway_level_failures(self):
        self.assert_cause_whoami(Gateway(status=401), "bearer_rejected")
        self.assert_cause_whoami(Gateway(status=503), "gateway_unavailable")
        self.assert_cause_whoami(Gateway(whoami_seat="ZC"), "seat_mismatch")

    def assert_cause_whoami(self, gw, cause):
        code, text = self.run_selftest(gw)
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"FAIL whoami\s+cause=%s" % cause)

    def test_unclassified_relay_refusal_and_unknown_code(self):
        self.assert_cause(Gateway(tools={"gr_wf_repo_get": refusal("relay_refused")}),
                          "relay_refused_unclassified")
        self.assert_cause(Gateway(tools={"gr_wf_repo_get": refusal("not_authorized")}),
                          "not_authorized")

    def test_transport_exception_is_gateway_unavailable(self):
        def broken(body, token, timeout):
            raise OSError("boom " + token)

        out = io.StringIO()
        code = S.main(["--seat", SEAT], env={}, home=self.home, transport=broken, out=out)
        self.assertEqual(code, 1)
        self.assertIn("cause=gateway_unavailable", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())

    def test_strict_makes_advisory_refusals_fatal(self):
        gw = Gateway(tools={"gr_wf_pr_search": refusal("relay_refused", "seat_policy")})
        code, text = self.run_selftest(gw, extra=["--strict"])
        self.assertEqual(code, 1, text)
        self.assertRegex(text, r"FAIL advisory_pr_search\s+cause=seat_policy")

    def test_a_bearer_echoed_by_the_gateway_is_refused_and_not_printed(self):
        def echo(body, token, timeout):
            reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"x": token}})
            return 200, "application/json", reply.encode()

        out = io.StringIO()
        code = S.main(["--seat", SEAT], env={}, home=self.home, transport=echo, out=out)
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.getvalue())


class BearerAndEnvTests(SelfTestBase):
    def test_missing_unsafe_or_wrong_seat_bearer(self):
        self.bearer.chmod(0o644)
        code, text = self.run_selftest(Gateway())
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAIL bearer\s+cause=not_configured")
        self.bearer.chmod(0o600)
        self.bearer.write_text("sbt_codex_" + "x" * 43)
        self.bearer.chmod(0o600)
        code, text = self.run_selftest(Gateway())
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAIL bearer\s+cause=seat_mismatch")

    def test_a_launcher_bound_seat_is_kept_and_must_agree(self):
        good = {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "CLAUDE"}
        code, text = self.run_selftest(Gateway(), env=good)
        self.assertEqual(code, 0, text)
        bad = {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "ZC"}
        code, text = self.run_selftest(Gateway(), env=bad)
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAIL env\s+cause=launcher_seat_conflict")
        code, text = self.run_selftest(Gateway(), env={"AGENT_LAUNCHER": "botfleet"})
        self.assertEqual(code, 1)
        self.assertIn("launcher_seat_conflict", text)

    def test_clean_env_strips_stale_hints_only_without_a_launcher(self):
        env = {"AGENT_SEAT": "ZC", "AGENT_TAG": "ZC", "AGENT_LAUNCH_SEAT": "ZC", "PATH": "/bin"}
        self.assertEqual(S.clean_env(env), {"AGENT_LAUNCH_SEAT": "ZC", "PATH": "/bin"})
        env["AGENT_LAUNCHER"] = "x"
        self.assertEqual(S.clean_env(env), env)

    def test_a_lone_launch_seat_for_another_seat_is_refused(self):
        code, text = self.run_selftest(Gateway(), env={"AGENT_LAUNCH_SEAT": "ZC"})
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAIL env\s+cause=launcher_seat_conflict")

    def test_invalid_seat_is_a_bearer_failure_not_a_traceback(self):
        for bad in ("bad seat", ""):
            out = io.StringIO()
            code = S.main(["--seat", bad], env={}, home=self.home, transport=Gateway(), out=out)
            self.assertEqual(code, 1)
            self.assertRegex(out.getvalue(), r"FAIL bearer\s+cause=bad_seat_name")
            self.assertIn("RESULT failed", out.getvalue())

    def test_http_exception_is_gateway_unavailable(self):
        import http.client

        def broken(body, token, timeout):
            raise http.client.IncompleteRead(b"x")

        out = io.StringIO()
        code = S.main(["--seat", SEAT], env={}, home=self.home, transport=broken, out=out)
        self.assertEqual(code, 1)
        self.assertIn("cause=gateway_unavailable", out.getvalue())

    def test_whoami_seat_mismatch_stops_before_the_git_reads(self):
        gw = Gateway(whoami_seat="ZC")
        code, text = self.run_selftest(gw)
        self.assertEqual(code, 1)
        self.assertRegex(text, r"FAIL whoami\s+cause=seat_mismatch")
        names = [c.get("params", {}).get("name") for c in gw.calls]
        self.assertEqual(names, [None, "whoami"])

    def test_only_exact_version_keys_are_read_from_whoami(self):
        self.assertEqual(S._version_fields({"protocol_version": "2025-06-18"}), {})
        self.assertEqual(S._version_fields({"worker_version": "1.2.3"}), {"worker_version": "1.2.3"})


if __name__ == "__main__":
    unittest.main()
