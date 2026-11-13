#!/usr/bin/env python3
"""Mocked-API contract tests for populate-scoped-folders.py.

No network or child process: the gate/write HTTP transport and shared named-read
function are stubbed over the same fake store.  Values are
sentinel strings ("VALUE-<name>"); every test asserts they never appear in any
printed output (names-only reporting, redacted errors)."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import fleet_infisical as FI

spec = importlib.util.spec_from_file_location(
    "psf", str(pathlib.Path(__file__).with_name("populate-scoped-folders.py"))
)
psf = importlib.util.module_from_spec(spec)
with (
    tempfile.TemporaryDirectory() as fixture_home,
    mock.patch.object(pathlib.Path, "home", return_value=pathlib.Path(fixture_home)),
    mock.patch.dict(
        os.environ, {"INFISICAL_FLEET_OPS_PROJECT_ID": "00000000-0000-4000-8000-000000000099"}
    ),
):
    spec.loader.exec_module(psf)

DEST_PATHS = {(psf.ENV, p) for _, _, _, p, _ in psf.MAPPING}


def fixture_service_token():
    return "st." + secrets.token_urlsafe(32)


class FakeInfisical:
    """Routes gate/write HTTP and shared named reads over one store.
    values[(ws, path, name)] -> value; a missing key returns no named value."""

    def __init__(self, scopes=None, values=None, fail=None, read_tokens=None, write_token=None):
        self.scopes = scopes
        self.values = values or {}
        self.read_tokens = read_tokens or {
            psf.AFC: fixture_service_token(),
            psf.FLEETOPS: fixture_service_token(),
        }
        self.write_token = write_token or fixture_service_token()
        # ("read", (ws, path, name)) -> fleet-secrets exit code;  ("upsert", name) -> HTTP code
        self.fail = fail or {}
        self.upserts = []
        self.agents = []  # the User-Agent of every API call
        self.authorizations = []
        self.reads = []  # project, path, isolated bindings, own token, names, deadline

    def default_metadata(self, token):
        if token == self.read_tokens[psf.AFC]:
            project = psf.AFC
            paths = {(psf.ENV, path or "/") for ws, path, *_ in psf.MAPPING if ws == project}
            permissions = ["read"]
        elif token == self.read_tokens[psf.FLEETOPS]:
            project = psf.FLEETOPS
            paths = {
                (psf.ENV, path or "/") for ws, path, *_ in psf.MAPPING if ws == project
            } | DEST_PATHS
            permissions = ["read"]
        else:
            project = psf.FLEETOPS
            paths = DEST_PATHS
            permissions = ["read", "write"]
        scopes = (
            self.scopes
            if self.scopes is not None
            else [
                {"environment": environment, "secretPath": path}
                for environment, path in sorted(paths)
            ]
        )
        return {
            "id": "fixture-service-token",
            "workspace": project,
            "projectId": project,
            "permissions": permissions,
            "scopes": scopes,
            "expiresAt": "2099-01-01T00:00:00Z",
        }

    def _err(self, url, code, body="server message"):
        raise urllib.error.HTTPError(
            url, code, "err", {}, io.BytesIO(json.dumps({"message": body}).encode())
        )

    def fetch_project(self, project, path, env, *, token, names, timeout):
        self.reads.append((project, path, dict(env), token, tuple(names), timeout))
        if len(names) != 1:
            raise AssertionError("expected one named read")
        name = names[0]
        key = (project, path, name)
        if ("read", key) in self.fail:
            raise FI.SecretsError("untrusted provider VALUE-message", self.fail[("read", key)])
        if key not in self.values:
            return {}
        return {name: self.values[key]}

    def open(self, request, timeout=35):
        url, method = request.full_url, request.get_method()
        self.agents.append(request.get_header("User-agent"))
        self.authorizations.append((method, request.get_header("Authorization")))
        if url.endswith("/api/v2/service-token"):
            token = (request.get_header("Authorization") or "").removeprefix("Bearer ")
            return io.BytesIO(json.dumps(self.default_metadata(token)).encode())
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
        self.read_tokens = {psf.AFC: fixture_service_token(), psf.FLEETOPS: fixture_service_token()}
        self.write_token = fixture_service_token()
        self.fake = FakeInfisical(read_tokens=self.read_tokens, write_token=self.write_token)
        self._orig = psf.urlopen
        psf.urlopen = self.fake.open
        self._orig_fetch = psf.FI.fetch_project
        psf.FI.fetch_project = self.fake.fetch_project
        self._env = dict(os.environ)
        os.environ[psf.READ_TOKEN_ENVS[psf.AFC]] = self.read_tokens[psf.AFC]
        os.environ[psf.READ_TOKEN_ENVS[psf.FLEETOPS]] = self.read_tokens[psf.FLEETOPS]
        os.environ[psf.WRITE_TOKEN_ENV] = self.write_token

    def tearDown(self):
        psf.urlopen = self._orig
        psf.FI.fetch_project = self._orig_fetch
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
        reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
        self.assertTrue(psf.check_scoped_tokens(reads, self.write_token))

    def test_missing_scope_aborts(self):
        self.fake.scopes = self.fake.default_metadata(self.read_tokens[psf.AFC])["scopes"]
        self.fake.scopes = [s for s in self.fake.scopes if s["secretPath"] != "/tools"]
        reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
        self.assertFalse(psf.check_scoped_tokens(reads, self.write_token))

    def test_endpoint_404_aborts(self):
        def boom(request, timeout=35):
            if request.full_url.endswith("/api/v2/service-token"):
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return self.fake.open(request, timeout)

        psf.urlopen = boom
        reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
        self.assertFalse(psf.check_scoped_tokens(reads, self.write_token))

    def test_unknown_schema_aborts(self):
        self.fake.scopes = {"prod": ["/wake"]}  # not the expected list of dicts
        reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
        self.assertFalse(psf.check_scoped_tokens(reads, self.write_token))

    def test_no_token_aborts(self):
        reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
        self.assertFalse(psf.check_scoped_tokens(reads, ""))

    def test_non_object_metadata_aborts_without_attribute_error(self):
        # kody 4365622066:  a top-level list is the parser's fail-safe None.
        self.assertIsNone(psf.parse_scopes([{"environment": psf.ENV, "secretPath": "/wake"}]))
        self.assertIsNone(psf.parse_scopes("scopes"))

    def test_every_call_names_the_project(self):
        # kody 4365622172:  never the runtime's default Python-urllib agent.
        reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
        self.assertTrue(psf.check_scoped_tokens(reads, self.write_token))
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
            headers={"Authorization": "Bearer " + self.write_token},
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
            reads = {ws: os.environ[name] for ws, name in psf.READ_TOKEN_ENVS.items()}
            self.assertFalse(
                psf.check_scoped_tokens(reads, self.write_token),
                "the gate aborts fail safe",
            )
        at_depth = b"[" * (psf.MAX_RESPONSE_DEPTH + 1) + b"]" * (psf.MAX_RESPONSE_DEPTH + 1)
        psf.urlopen = lambda request, timeout=35: io.BytesIO(at_depth)
        self.assertIsInstance(
            psf.read_json(request, timeout=35), list, "exactly at the depth cap passes"
        )
        psf.urlopen = self.fake.open
        self.assertEqual(
            psf.token_data(os.environ[psf.READ_TOKEN_ENVS[psf.AFC]])["id"],
            "fixture-service-token",
            "a normal answer passes",
        )


class TestSharedReader(Base):
    def api(self, method, path, token, body, env, **limits):
        self.calls.append((method, path, token, dict(env)))
        if path == "/v2/service-token":
            return 200, self.metadata
        self.assertTrue(path.startswith("/v3/secrets/raw/WANTED?"))
        return self.status, {"secret": self.row}

    def setUp(self):
        super().setUp()
        self.calls = []
        self.metadata = self.fake.default_metadata(self.read_tokens[psf.AFC])
        self.status = 200
        self.row = {
            "secretKey": "WANTED",
            "secretValue": "VALUE-padded é\n",
            "secretValueHidden": False,
            "workspace": psf.AFC,
            "environment": "prod",
            "secretPath": "/",
            "type": "shared",
        }
        self.fetch = mock.patch.object(psf.FI, "fetch_project", self._orig_fetch)
        self.api_patch = mock.patch.object(psf.FI, "_api_json", side_effect=self.api)
        self.fetch.start()
        self.api_patch.start()

    def tearDown(self):
        self.api_patch.stop()
        self.fetch.stop()
        super().tearDown()

    def test_actual_shared_reader_verifies_grant_and_provenance_before_value(self):
        value = psf.scoped_get(self.read_tokens[psf.AFC], psf.AFC, "/", "WANTED")
        self.assertEqual(value, "VALUE-padded é\n")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(call[2] == self.read_tokens[psf.AFC] for call in self.calls))
        self.metadata["permissions"] = ["read", "write"]
        self.calls.clear()
        with self.assertRaises(psf.ReadError):
            psf.scoped_get(self.read_tokens[psf.AFC], psf.AFC, "/", "WANTED")
        self.assertEqual(len(self.calls), 1)

    def test_actual_shared_reader_rejects_wrong_folder_and_unresolved_reference(self):
        for field, value in (("secretPath", "/unrelated"), ("secretValue", "${OTHER_KEY}")):
            with self.subTest(field=field):
                original = self.row[field]
                self.row[field] = value
                with self.assertRaises(psf.ReadError) as caught:
                    psf.scoped_get(self.read_tokens[psf.AFC], psf.AFC, "/", "WANTED")
                self.assertNotIn(value, str(caught.exception))
                self.row[field] = original

    def test_actual_shared_reader_absence_does_not_fetch_another_name(self):
        self.status = 404
        self.assertIsNone(psf.scoped_get(self.read_tokens[psf.AFC], psf.AFC, "/", "WANTED"))
        self.assertEqual(len(self.calls), 2)


class TestDryRun(Base):
    def test_dry_run_writes_nothing(self):
        code, out = self.run_main()
        self.assertIsNone(code)
        self.assertEqual(self.fake.upserts, [])
        self.assertIn("DRY RUN", out)

    def test_dry_run_fails_closed_when_the_gate_fails(self):
        # kody 4366921815:  a gate that cannot pass is a non-zero exit in a dry run too.
        del os.environ[psf.WRITE_TOKEN_ENV]
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
        self.assertIn("Infisical named read exit 4", out)
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

    def test_reads_use_only_own_token_exact_project_path_and_name(self):
        seeded(self.fake)
        with mock.patch.dict(
            os.environ,
            {
                "INFISICAL_TOKEN": "ambient-other-reader",
                "INFISICAL_WRITE_TOKEN": "ambient-writer",
                "FLEET_SECRETS_API": "https://untrusted.example.test/api",
                "INFISICAL_READ_PATHS": "/unrelated",
                "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN": "letter-only-reader",
            },
        ):
            code, _out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.reads), 144)
        expected = []
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            expected.extend(((sws, spath, sname), (psf.FLEETOPS, dpath, dname)))
        seen = []
        for project, path, env, token, names, timeout in self.fake.reads:
            self.assertEqual(len(names), 1)
            seen.append((project, path, names[0]))
            self.assertEqual(token, self.read_tokens[project])
            self.assertEqual(
                env,
                {
                    FI.ENV_API: psf.BASE + "/api",
                    FI.ENV_READ_PATHS: ",".join(psf.READ_PATHS_BY_PROJECT[project]),
                },
            )
            self.assertEqual(FI.declared_paths(env, path), psf.READ_PATHS_BY_PROJECT[project])
            self.assertEqual(timeout, 90)
        self.assertEqual(seen, expected)
        self.assertEqual(len(self.fake.upserts), 72)
        self.assertEqual(
            [token for method, token in self.fake.authorizations if method == "POST"],
            ["Bearer " + self.write_token] * 72,
        )

    def test_partial_name_result_is_absent_without_another_route(self):
        with mock.patch.object(
            psf.FI, "fetch_project", return_value={"OTHER_NAME": "VALUE-other"}
        ) as read:
            self.assertIsNone(psf.read_secret_opt(self.read_tokens[psf.AFC], psf.AFC, "/", "WANTED"))
            with self.assertRaises(psf.ReadError) as caught:
                psf.read_secret(self.read_tokens[psf.AFC], psf.AFC, "/", "WANTED")
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.kwargs["names"], ("WANTED",))

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

    def test_copy_needs_no_installed_application_or_child_environment(self):
        seeded(self.fake)
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no child allowed")):
            code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.upserts), 72)
        self.assertNotIn("VALUE-", out)

    def test_direct_read_timeout_exits_124(self):
        seeded(self.fake)
        with mock.patch.object(psf.FI, "fetch_project", side_effect=TimeoutError("VALUE-provider")):
            code, out = self.run_main("--apply")
        self.assertEqual(code, psf.TIMED_OUT)
        self.assertEqual(code, 124)
        self.assertIn("Infisical named read timed out, reading the source", out)
        self.assertEqual(self.fake.upserts, [])
        self.assertNotIn("VALUE-", out)

    def test_shared_reader_exit_124_is_a_timeout(self):
        seeded(self.fake)
        sws, spath, sname, _dpath, _dname = psf.MAPPING[0]
        self.fake.fail[("read", (sws, spath, sname))] = 124
        code, out = self.run_main("--apply")
        self.assertEqual(code, psf.TIMED_OUT)
        self.assertIn("Infisical named read timed out, reading the source", out)
        self.assertNotIn("exit 124", out)
        self.assertEqual(self.fake.upserts, [])
        self.assertNotIn("VALUE-", out)

    def test_missing_or_malformed_scoped_tokens_fail_closed_without_fallback(self):
        seeded(self.fake)
        for name, value in ((psf.WRITE_TOKEN_ENV, ""), (psf.READ_TOKEN_ENVS[psf.AFC], "ua.client")):
            with self.subTest(name=name):
                old = os.environ[name]
                os.environ[name] = value
                try:
                    code, out = self.run_main("--apply")
                finally:
                    os.environ[name] = old
                self.assertEqual(code, 1)
                self.assertIn(name, out)
                self.assertEqual(self.fake.reads, [])
                self.assertEqual(self.fake.upserts, [])
                self.assertEqual(self.fake.authorizations, [])

    def test_write_timeout_is_an_unknown_outcome_like_a_5xx(self):
        # kody 4368532647:  a stalled write may have landed, as a write answered
        # with a 5xx may have, so both exit 1 and a re-run reconciles.  Here the
        # write lands and its answer never comes back.
        seeded(self.fake)
        victim = psf.MAPPING[40][4]

        def write_lands_then_stalls(request, timeout=35):
            answer = self.fake.open(request, timeout)
            if request.get_method() == "POST" and request.full_url.endswith("/" + victim):
                raise TimeoutError("timed out")
            return answer

        psf.urlopen = write_lands_then_stalls
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn(
            f"STOP at 41/72 ({psf.MAPPING[40][3]}/{victim}): the Infisical write timed out, "
            "so its outcome is unknown.\u00a0 40 copied, 0 already in sync.\u00a0 Re-run reconciles;",
            out,
        )
        self.assertNotIn("VALUE-", out)
        psf.urlopen = self.fake.open
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertIn(f"already in sync: {psf.MAPPING[40][3]}/{victim}", out)
        self.assertIn("Done: 31 copies applied, 41 already in sync.", out)
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-" + sname)

    def test_terminal_messages_use_current_owner_sentence_gap(self):
        # The current owner rule supersedes the old plain-ASCII exception.
        seeded(self.fake)
        sws, spath, victim, _dpath, _dname = psf.MAPPING[5]
        self.fake.fail[("read", (sws, spath, victim))] = 4
        _code, out = self.run_main("--apply")
        self.assertIn("5 copied, 0 already in sync.\u00a0 Re-run reconciles; equal values", out)
        self.assertNotRegex(out, r"[a-z)]\. [A-Z]")
        self.assertIn("\u00a0 ", out)

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
