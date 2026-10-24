#!/usr/bin/env python3
"""Mocked-API contract tests for populate-scoped-folders.py.

No network and no real fleet-secrets:  urllib.request.urlopen is stubbed with a
fake Infisical (login, token metadata, the upsert write), and subprocess.run
with a fake `fleet-secrets get` over the same values (every read).  Values are
sentinel strings ("VALUE-<name>"); every test asserts they never appear in any
printed output (names-only reporting, redacted errors)."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location(
    "psf", str(pathlib.Path(__file__).with_name("populate-scoped-folders.py"))
)
psf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(psf)

DEST_PATHS = {(psf.ENV, p) for _, _, _, p, _ in psf.MAPPING}


class FakeInfisical:
    """Routes urlopen calls and `fleet-secrets get` runs over one store.
    values[(ws, path, name)] -> value;  a missing key is fleet-secrets' exit 3."""

    def __init__(self, scopes=None, values=None, fail=None):
        self.scopes = (
            scopes
            if scopes is not None
            else [{"environment": psf.ENV, "secretPath": p} for _, p in sorted(DEST_PATHS)]
        )
        self.values = values or {}
        # ("read", (ws, path, name)) -> fleet-secrets exit code;  ("upsert", name) -> HTTP code
        self.fail = fail or {}
        self.upserts = []
        self.agents = []  # the User-Agent of every API call
        self.reads = []  # every fleet-secrets run:  (argv, child env)

    def _err(self, url, code, body="server message"):
        raise urllib.error.HTTPError(
            url, code, "err", {}, io.BytesIO(json.dumps({"message": body}).encode())
        )

    def run(self, argv, env=None, **kwargs):
        """A fake `fleet-secrets get NAME --path P --project SLUG`:  the project id
        comes from the variable fleet-secrets reads for SLUG, as the real one does."""
        self.reads.append((list(argv), dict(env or {})))
        if argv[1:2] != ["get"] or argv[3] != "--path" or argv[5] != "--project":
            raise AssertionError("unexpected fleet-secrets call: " + " ".join(argv[1:]))
        name, path, slug = argv[2], argv[4], argv[6]
        id_var = {
            "ai-fleet-coordinator": "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID",
            "fleet-ops": "INFISICAL_FLEET_OPS_PROJECT_ID",
        }[slug]
        key = (env[id_var], path, name)
        if ("read", key) in self.fail:
            return subprocess.CompletedProcess(argv, self.fail[("read", key)], b"", None)
        if key not in self.values:
            return subprocess.CompletedProcess(argv, 3, b"", None)
        return subprocess.CompletedProcess(argv, 0, self.values[key].encode("utf-8"), None)

    def open(self, request, timeout=35):
        url, method = request.full_url, request.get_method()
        self.agents.append(request.get_header("User-agent"))
        if url.endswith("/api/v2/service-token"):
            return io.BytesIO(json.dumps({"id": "st1", "scopes": self.scopes}).encode())
        if url.endswith("/auth/universal-auth/login"):
            return io.BytesIO(json.dumps({"accessToken": "automation"}).encode())
        if "/api/v3/secrets/raw/" in url:
            name = urllib.parse.unquote(url.split("/api/v3/secrets/raw/")[1].split("?")[0])
            if method == "POST":
                if ("upsert", name) in self.fail:
                    self._err(url, self.fail[("upsert", name)], "leak VALUE-x")
                body = json.loads(request.data.decode())
                self.values[(body["workspaceId"], body["secretPath"], name)] = body["secretValue"]
                self.upserts.append((body["secretPath"], name))
                return io.BytesIO(b"{}")
        raise AssertionError("unexpected call: " + method + " " + url)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInfisical()
        self._orig = psf.urlopen
        psf.urlopen = self.fake.open
        self._orig_run = subprocess.run
        subprocess.run = self.fake.run
        self._env = dict(os.environ)
        # The automation login comes from the environment (the handoff's first
        # source), so no test ever reaches a real fleet-secrets.
        os.environ["INFISICAL_AUTOMATION_CLIENT_ID"] = "u"
        os.environ["INFISICAL_AUTOMATION_CLIENT_SECRET"] = "p"
        os.environ["READ_TOOLS_SERVICE_TOKEN"] = "read-tools-st"

    def tearDown(self):
        psf.urlopen = self._orig
        subprocess.run = self._orig_run
        os.environ.clear()
        os.environ.update(self._env)

    def run_main(self, *argv):
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["populate-scoped-folders.py", *argv]
        code = None
        with contextlib.redirect_stdout(out):
            try:
                psf.main()
            except SystemExit as e:
                code = e.code
        sys.argv = old
        return code, out.getvalue()


class TestMapping(Base):
    def test_integrity(self):
        self.assertEqual(len(psf.MAPPING), 72)
        self.assertEqual(len({(d, n) for _, _, _, d, n in psf.MAPPING}), 72)
        self.assertEqual(len(DEST_PATHS), 5)  # /wake /fleetlink /zulip /linear /tools


class TestGate(Base):
    def test_full_coverage_passes(self):
        self.assertTrue(psf.check_read_tools_scope())

    def test_missing_scope_aborts(self):
        self.fake.scopes = [s for s in self.fake.scopes if s["secretPath"] != "/tools"]
        self.assertFalse(psf.check_read_tools_scope())

    def test_endpoint_404_aborts(self):
        def boom(request, timeout=35):
            if request.full_url.endswith("/api/v2/service-token"):
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return self.fake.open(request, timeout)

        psf.urlopen = boom
        self.assertFalse(psf.check_read_tools_scope())

    def test_unknown_schema_aborts(self):
        self.fake.scopes = {"prod": ["/wake"]}  # not the expected list of dicts
        self.assertFalse(psf.check_read_tools_scope())

    def test_no_token_aborts(self):
        del os.environ["READ_TOOLS_SERVICE_TOKEN"]
        self.assertFalse(psf.check_read_tools_scope())

    def test_non_object_metadata_aborts_without_attribute_error(self):
        # kody 4365622066:  a top-level list is the parser's fail-safe None.
        self.assertIsNone(psf.parse_scopes([{"environment": psf.ENV, "secretPath": "/wake"}]))
        self.assertIsNone(psf.parse_scopes("scopes"))

    def test_every_call_names_the_project(self):
        # kody 4365622172:  never the runtime's default Python-urllib agent.
        self.assertTrue(psf.check_read_tools_scope())
        self.assertTrue(self.fake.agents)
        self.assertTrue(all(a == psf.USER_AGENT for a in self.fake.agents))

    def test_every_infisical_call_refuses_redirects(self):
        # kody 4366921692:  urllib's default handler would re-send the bearer
        # token to the Location;  the script's one opener refuses every 3xx.
        handlers = [
            h for h in psf._OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
        ]
        self.assertTrue(handlers)
        self.assertTrue(all(isinstance(h, psf.RefuseRedirects) for h in handlers))
        request = urllib.request.Request(
            "https://app.infisical.com/api/v2/service-token",
            headers={"Authorization": "Bearer synthetic"},
        )
        for code in (301, 302, 303, 307, 308):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                handlers[0].redirect_request(
                    request, io.BytesIO(b""), code, "moved", {}, "https://elsewhere.example.test/"
                )
            self.assertEqual(caught.exception.code, code)

    def test_answers_past_the_byte_depth_or_node_cap_are_refused(self):
        # kody 4367021839:  every Infisical answer is capped before and after parsing.
        too_big = b'{"scopes": [], "pad": "' + b"x" * psf.MAX_RESPONSE_BYTES + b'"}'
        # The root is depth 0, so MAX + 2 nested lists put the innermost one at MAX + 1.
        too_deep = b"[" * (psf.MAX_RESPONSE_DEPTH + 2) + b"]" * (psf.MAX_RESPONSE_DEPTH + 2)
        # The list itself is one node more than its children.
        too_many = json.dumps([0] * psf.MAX_RESPONSE_NODES).encode()
        # Under the byte cap, and past Python's recursion limit.
        past_recursion = b"[" * 100_000 + b"]" * 100_000
        self.assertLessEqual(len(past_recursion), psf.MAX_RESPONSE_BYTES)
        self.assertLessEqual(len(too_many), psf.MAX_RESPONSE_BYTES)
        request = urllib.request.Request("https://app.infisical.com/api/v2/service-token")
        for body in (too_big, too_deep, too_many, past_recursion, b"not json"):
            psf.urlopen = lambda request, timeout=35, body=body: io.BytesIO(body)
            with self.assertRaises(psf.ResponseLimitError):
                psf.read_json(request, timeout=35)
            self.assertFalse(psf.check_read_tools_scope(), "the gate aborts fail safe")
        at_depth = b"[" * (psf.MAX_RESPONSE_DEPTH + 1) + b"]" * (psf.MAX_RESPONSE_DEPTH + 1)
        psf.urlopen = lambda request, timeout=35: io.BytesIO(at_depth)
        self.assertIsInstance(
            psf.read_json(request, timeout=35), list, "exactly at the depth cap passes"
        )
        psf.urlopen = self.fake.open
        self.assertEqual(psf.token_data("read-tools-st")["id"], "st1", "a normal answer passes")


class TestDryRun(Base):
    def test_dry_run_writes_nothing(self):
        code, out = self.run_main()
        self.assertIsNone(code)
        self.assertEqual(self.fake.upserts, [])
        self.assertIn("DRY RUN", out)

    def test_dry_run_fails_closed_when_the_gate_fails(self):
        # kody 4366921815:  a gate that cannot pass is a non-zero exit in a dry run too.
        del os.environ["READ_TOOLS_SERVICE_TOKEN"]
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Aborting (fail safe)", out)
        self.assertEqual(self.fake.upserts, [])
        self.assertNotIn("Re-run with --apply", out)


def seeded(fake):
    """Source values present; destinations absent."""
    for sws, spath, sname, dpath, dname in psf.MAPPING:
        fake.values[(sws, spath or "/", sname)] = "VALUE-" + sname


class TestApply(Base):
    def test_happy_path_72_copies_names_only(self):
        seeded(self.fake)
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.upserts), 72)
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-" + sname)
        self.assertNotIn("VALUE-", out)

    def test_idempotent_rerun_skips_equal(self):
        seeded(self.fake)
        self.run_main("--apply")
        self.fake.upserts.clear()
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(self.fake.upserts, [])
        self.assertIn("already in sync", out)

    def test_collision_stops_no_overwrite(self):
        seeded(self.fake)
        _sws, _spath, _sname, dpath, dname = psf.MAPPING[10]
        self.fake.values[(psf.FLEETOPS, dpath, dname)] = "VALUE-different-existing"
        code, out = self.run_main("--apply")
        self.assertEqual(code, 2)
        self.assertNotIn((dpath, dname), self.fake.upserts)
        self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-different-existing")
        self.assertIn("COLLISION", out)
        self.assertNotIn("VALUE-", out)

    def test_read_failure_visible_stop_redacted(self):
        # fleet-secrets exit 4 (fetch or auth failure, a scope denial among them).
        seeded(self.fake)
        sws, spath, victim, _dpath, _dname = psf.MAPPING[5]
        self.fake.fail[("read", (sws, spath, victim))] = 4
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("STOP at 6/72", out)
        self.assertIn("fleet-secrets get exit 4", out)
        self.assertIn(victim, out)  # the NAME is fine
        self.assertNotIn("VALUE-", out)  # values never printed
        self.assertEqual(len(self.fake.upserts), 5)

    def test_unresolved_reference_stops_names_only(self):
        # fleet-secrets exit 5:  a source still an unexpanded ${...} reference is
        # never copied as if it were the value.
        seeded(self.fake)
        sws, spath, victim, _dpath, _dname = psf.MAPPING[70]  # a Fleet-OPS root recall source
        self.assertEqual((sws, spath), (psf.FLEETOPS, "/"))
        self.fake.fail[("read", (sws, spath, victim))] = 5
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("STOP at 71/72", out)
        self.assertIn(f"exit 5, reading the source Fleet-OPS /{victim}.", out)
        self.assertEqual(len(self.fake.upserts), 70)
        self.assertNotIn("VALUE-", out)
        self.assertTrue(all(a == psf.USER_AGENT for a in self.fake.agents))

    def test_destination_read_failure_names_the_destination(self):
        seeded(self.fake)
        _sws, _spath, _sname, dpath, dname = psf.MAPPING[2]
        self.fake.fail[("read", (psf.FLEETOPS, dpath, dname))] = 4
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn(f"reading the destination Fleet-OPS {dpath}/{dname}", out)
        self.assertNotIn((dpath, dname), self.fake.upserts)

    def test_reads_go_through_fleet_secrets_with_project_and_path(self):
        # kody 4365621961:  every value read is `fleet-secrets get NAME --path P
        # --project SLUG`, pinned to the MAPPING's workspace, on the login's token.
        seeded(self.fake)
        code, _out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.reads), 144)  # 72 sources + 72 destinations
        expected = []
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            expected.append((sname, spath, psf.FLEET_SECRETS_PROJECT[sws], sws))
            expected.append((dname, dpath, "fleet-ops", psf.FLEETOPS))
        seen = []
        for argv, env in self.fake.reads:
            slug = argv[6]
            id_var = psf.FLEET_SECRETS_PROJECT_ID_ENV[
                psf.AFC if slug == "ai-fleet-coordinator" else psf.FLEETOPS
            ]
            seen.append((argv[2], argv[4], slug, env[id_var]))
            self.assertEqual(
                env["INFISICAL_TOKEN"], "automation", "the login's own token, one identity"
            )
            for dropped in psf.CHILD_DROPPED_ENV:
                self.assertNotIn(dropped, env, f"{dropped} never reaches the child")
        self.assertEqual(seen, expected)
        # The API carries the login, the scope gate and the 72 writes, and no read.
        self.assertEqual(len(self.fake.upserts), 72)

    def test_values_are_not_taken_from_the_environment(self):
        # A same-named variable in the shell must never be copied in place of the source.
        seeded(self.fake)
        _sws, _spath, sname, dpath, dname = psf.MAPPING[4]
        os.environ[sname] = "VALUE-shell-shadow"
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-" + sname)
        self.assertNotIn("VALUE-", out)

    def test_value_is_copied_byte_for_byte(self):
        seeded(self.fake)
        sws, spath, sname, dpath, dname = psf.MAPPING[7]
        self.fake.values[(sws, spath, sname)] = "VALUE-padded é\n"
        code, _out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-padded é\n")

    def test_missing_fleet_secrets_exits_127(self):
        # kody 4366921905:  a missing executable is the shell's 127, apart from 1.
        seeded(self.fake)

        def missing(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        subprocess.run = missing
        code, out = self.run_main("--apply")
        self.assertEqual(code, psf.MISSING_EXECUTABLE)
        self.assertEqual(code, 127)
        self.assertIn("fleet-secrets get is not installed", out)
        self.assertEqual(self.fake.upserts, [])

    def test_fleet_secrets_read_timeout_exits_124(self):
        # kody 4367021960:  a timed-out read is timeout(1)'s 124, apart from 1 and 127.
        seeded(self.fake)

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        subprocess.run = slow
        code, out = self.run_main("--apply")
        self.assertEqual(code, psf.TIMED_OUT)
        self.assertEqual(code, 124)
        self.assertIn("fleet-secrets get timed out, reading the source", out)
        self.assertEqual(self.fake.upserts, [])
        self.assertNotIn("VALUE-", out)

    def test_login_handoff_timeout_exits_124_names_only(self):
        # kody 4367021960:  the login handoff's fleet-secrets timeout is 124 too.
        del os.environ["INFISICAL_AUTOMATION_CLIENT_ID"]

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        subprocess.run = slow
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            psf.handoff("INFISICAL_AUTOMATION_CLIENT_ID")
        self.assertEqual(caught.exception.code, 124)
        self.assertIn("INFISICAL_AUTOMATION_CLIENT_ID", err.getvalue())
        self.assertIn("timed out", err.getvalue())

    def test_fleet_secrets_own_exit_124_is_a_timeout(self):
        # kody 4368415894:  fleet-secrets' own 124 (its Linux entry point's login
        # timeout) exits 124 too, not as a generic "exit 124" failure.
        seeded(self.fake)
        sws, spath, sname, _dpath, _dname = psf.MAPPING[0]
        self.fake.fail[("read", (sws, spath, sname))] = 124
        code, out = self.run_main("--apply")
        self.assertEqual(code, psf.TIMED_OUT)
        self.assertIn("fleet-secrets get timed out, reading the source", out)
        self.assertNotIn("exit 124", out)
        self.assertEqual(self.fake.upserts, [])
        self.assertNotIn("VALUE-", out)

    def test_login_failure_stops_names_only_before_any_copy(self):
        # kody 4368415797:  a refused, oversized, malformed or unreachable login
        # stops with a names-only STOP line and exit 1, never a traceback.
        seeded(self.fake)

        def refused(url):
            self.fake._err(url, 401, "leak VALUE-x")

        def unreachable(url):
            raise urllib.error.URLError("unreachable")

        answers = [
            ("was refused (HTTP 401)", refused),
            (
                "failed (ResponseLimitError)",
                lambda url: io.BytesIO(b"x" * (psf.MAX_RESPONSE_BYTES + 1)),
            ),
            ("failed (ValueError)", lambda url: io.BytesIO(b"{}")),
            ("failed (URLError)", unreachable),
        ]
        for expected, answer in answers:
            with self.subTest(expected):

                def login_fails(request, timeout=35, answer=answer):
                    if request.full_url.endswith("/auth/universal-auth/login"):
                        return answer(request.full_url)
                    return self.fake.open(request, timeout)

                psf.urlopen = login_fails
                code, out = self.run_main("--apply")
                self.assertEqual(code, 1)
                self.assertIn(
                    f"STOP before the first copy: the Infisical login {expected}.  Nothing was copied.",
                    out,
                )
                self.assertNotIn("Traceback", out)
                self.assertNotIn("VALUE-", out)
                self.assertNotIn("leak", out)
                self.assertEqual(self.fake.upserts, [])
                self.assertEqual(self.fake.reads, [], "no value is read before the login succeeds")

    def test_terminal_messages_use_two_spaces_between_sentences(self):
        # Kody issue comment 6383398733:  plain-text output takes two ASCII spaces.
        seeded(self.fake)
        sws, spath, victim, _dpath, _dname = psf.MAPPING[5]
        self.fake.fail[("read", (sws, spath, victim))] = 4
        _code, out = self.run_main("--apply")
        self.assertIn("5 copied, 0 already in sync.  Re-run reconciles;  equal values", out)
        self.assertNotRegex(out, r"[a-z)]\. [A-Z]")
        self.assertNotIn(" ", out)

    def test_partial_write_stop_then_rerun_completes(self):
        seeded(self.fake)
        victim = psf.MAPPING[40][2]
        self.fake.fail[("upsert", victim)] = 500
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("STOP at 41/72", out)
        first = list(self.fake.upserts)
        self.assertEqual(len(first), 40)
        del self.fake.fail[("upsert", victim)]
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.upserts) - len(first), 32)  # only the remainder re-copied
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-" + sname)


if __name__ == "__main__":
    unittest.main(verbosity=2)
