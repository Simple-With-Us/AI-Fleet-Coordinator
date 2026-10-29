"""The server rollout helpers:  the Infisical loader the entrypoint runs at start (server/infisical_env.py)
and the scoped live-config update (server/apply_live_config.py).  No network:  Infisical is a loopback fake."""
from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import urllib.parse
from pathlib import Path

from agent_sync import config as C

REPO = Path(__file__).resolve().parents[3]
SERVER_DIR = REPO / "scripts" / "agent_sync" / "server"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "server-listener-live-oct9.toml"
TARGETS = ["JET"] + ["GB-" + r for r in ("DIRECTOR", "FIXER", "DESIGNER", "COMPILER", "HOUSEKEEPER", "PUBLISHER",
                                          "DEPLOYER", "MONITOR", "PLUMBER", "ORACLE", "TRADER", "ENGINEER")]
UNTOUCHED = ["MA", "GROK-WEB", "WISE", "ECHO"]


def _load(name: str):
    spec = importlib.util.spec_from_file_location("agent_sync_server_" + name, SERVER_DIR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IE = _load("infisical_env")
AL = _load("apply_live_config")


def folder(path: str) -> str:
    """The secretPath a fake Infisical read asked for."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("secretPath", [""])[0]


# --------------------------------------------------------------------------------------------
# A loopback Infisical:  universal-auth login, then one folder of raw secrets
# --------------------------------------------------------------------------------------------

class FakeInfisical:
    def __init__(self, client_id: str, client_secret: str, secrets_map: dict[str, str]) -> None:
        self.client_id, self.client_secret, self.secrets = client_id, client_secret, secrets_map
        self.token = "tok-" + secrets.token_hex(16)
        self.login_status = 200
        self.read_failures = 0          # 5xx answers before a good read
        self.requests: list[tuple[str, str]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, code: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                fake.requests.append(("POST", self.path))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/v1/auth/universal-auth/login" or fake.login_status != 200:
                    return self._send(fake.login_status if fake.login_status != 200 else 404,
                                      {"message": "refused " + body.get("clientSecret", "")})
                if (body.get("clientId"), body.get("clientSecret")) != (fake.client_id, fake.client_secret):
                    return self._send(401, {"message": "bad identity"})
                self._send(200, {"accessToken": fake.token, "expiresIn": 7200})

            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                if self.headers.get("Authorization") != "Bearer " + fake.token:
                    return self._send(401, {"message": "no token"})
                if fake.read_failures:
                    fake.read_failures -= 1
                    return self._send(503, {"message": "busy"})
                if "secretPath=%2Fzulip" not in self.path or "environment=prod" not in self.path:
                    return self._send(404, {"message": "no folder"})
                self._send(200, {"secrets": [{"secretKey": k, "secretValue": v} for k, v in fake.secrets.items()]})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/api" % self.server.server_address[1]

    def __enter__(self) -> "FakeInfisical":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


class InfisicalEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sync-infisical-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cid, self.csecret = "cid-" + secrets.token_hex(8), "cs-" + secrets.token_hex(16)
        self.values = {
            "ZULIP_GB_FIXER_EMAIL": "fixer-grok-bot@zulip.test",
            "ZULIP_GB_FIXER_API_KEY": "zk-" + secrets.token_hex(12),
            "ZULIP_ALERT_GB_FIXER_ENDPOINT": "https://routine.test/hook/fixer",
            "ZULIP_ALERT_GB_FIXER_KEY": "crsr_" + secrets.token_hex(16),
            "ZULIP_ALERT_GB_FIXER_HEADER": "Authorization: Bearer crsr_never",
            "ZULIP_JET_EMAIL": "openai-dot-bot@zulip.test",
            "ZULIP_BF_COMPILER_API_KEY": "bf-" + secrets.token_hex(8),     # a BotFleet bot:  not a server seat
            "ZULIP_CLAUDE_API_KEY": "mac-" + secrets.token_hex(8),         # a Mac seat
            "ZULIP_SITE": "https://evil.test",                             # never from Infisical
            "PATH": "/nowhere",
        }

    def env(self, fake: FakeInfisical | None = None, **extra: str) -> dict[str, str]:
        env = {"INFISICAL_CLIENT_ID": self.cid, "INFISICAL_CLIENT_SECRET": self.csecret,
               "INFISICAL_PROJECT_ID": "proj-1", "PATH": "%s:/usr/bin:/bin" % os.path.dirname(sys.executable),
               "HOME": str(self.tmp), "ZULIP_SITE": "https://simplewithus.zulipchat.com"}
        if fake:
            env["INFISICAL_API_URL"] = fake.url
        env.update(extra)
        return env

    def test_it_loads_only_the_allowlisted_names_and_drops_the_identity(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            out, line = IE.build_env(self.env(fake, ZULIP_GB_FIXER_API_KEY="stale-coolify-copy"))
        for name in ("ZULIP_GB_FIXER_EMAIL", "ZULIP_GB_FIXER_API_KEY", "ZULIP_ALERT_GB_FIXER_ENDPOINT",
                     "ZULIP_ALERT_GB_FIXER_KEY", "ZULIP_JET_EMAIL"):
            self.assertEqual(out[name], self.values[name], "%s comes from Infisical, which wins" % name)
        for name in ("ZULIP_ALERT_GB_FIXER_HEADER", "ZULIP_BF_COMPILER_API_KEY", "ZULIP_CLAUDE_API_KEY"):
            self.assertNotIn(name, out)
        self.assertEqual(out["ZULIP_SITE"], "https://simplewithus.zulipchat.com")
        self.assertNotEqual(out["PATH"], "/nowhere")
        self.assertNotIn("INFISICAL_CLIENT_ID", out)
        self.assertNotIn("INFISICAL_CLIENT_SECRET", out)
        self.assertIn("loaded 5 variables from Infisical (prod /zulip; 1 replaced a container value, 5 other", line)
        for value in list(self.values.values()) + [self.csecret, fake.token]:
            self.assertNotIn(value, line)

    def test_the_sentry_dsn_is_loaded_and_other_sentry_names_are_not(self) -> None:
        values = dict(self.values, SENTRY_FLEET_DSN="https://k@o1.ingest.us.sentry.io/1",
                      SENTRY_API_TOKEN="sntrys_never", AGENT_SYNC_SENTRY_DSN_OTHER="never")
        with FakeInfisical(self.cid, self.csecret, values) as fake:
            out, line = IE.build_env(self.env(fake))
        self.assertEqual(out["SENTRY_FLEET_DSN"], "https://k@o1.ingest.us.sentry.io/1")
        self.assertNotIn("SENTRY_API_TOKEN", out)
        self.assertNotIn("AGENT_SYNC_SENTRY_DSN_OTHER", out)
        self.assertIn("loaded 6 variables", line)
        self.assertNotIn("k@o1", line)

    def test_a_refused_login_falls_back_to_the_container_environment_without_a_body(self) -> None:
        with FakeInfisical(self.cid, "other-secret", self.values) as fake:
            out, line = IE.build_env(self.env(fake, ZULIP_GB_FIXER_API_KEY="coolify-copy"))
        self.assertEqual(line, "Infisical load failed (login: HTTP 401); starting with the container environment")
        self.assertEqual(out["ZULIP_GB_FIXER_API_KEY"], "coolify-copy")
        self.assertNotIn("INFISICAL_CLIENT_SECRET", out)

    def test_a_busy_read_is_retried_once(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.read_failures = 1
            out, line = IE.build_env(self.env(fake), sleep=lambda s: None)
        self.assertIn("loaded 5 variables", line)
        # The /zulip read is retried once;  then the notes-queue keys' own folder is read once (AFC-486).
        self.assertEqual([m for m, _ in fake.requests], ["POST", "GET", "GET", "GET"])
        self.assertEqual([folder(p) for m, p in fake.requests if m == "GET"], ["/zulip", "/zulip", "/operator"])

    def test_a_plain_http_api_that_is_not_loopback_is_refused(self) -> None:
        out, line = IE.build_env(self.env(INFISICAL_API_URL="http://infisical.example.com/api"))
        self.assertEqual(line, "Infisical load failed (INFISICAL_API_URL must be an https URL); starting with the "
                               "container environment")
        self.assertNotIn("INFISICAL_CLIENT_SECRET", out)

    def test_an_incomplete_identity_is_not_used(self) -> None:
        env = self.env()
        env.pop("INFISICAL_PROJECT_ID")
        out, line = IE.build_env(env)
        self.assertIn("Infisical not used (INFISICAL_PROJECT_ID not set)", line)
        self.assertNotIn("INFISICAL_CLIENT_SECRET", out)

    def test_the_allowlist_follows_the_partition(self) -> None:
        names = IE.allowed_names(IE.server_codes(IE.PARTITION_DEFAULT))
        self.assertIn("ZULIP_ALERT_GB_TRADER_KEY", names)
        self.assertIn("JET_ROUTINE_KEY", names)
        self.assertIn("ZULIP_GROK_WEB_API_KEY", names)
        # The Operator's own Zulip identity is the host's to provide:  a user id and an address,
        # never a credential, so they are shared names the loader copies from the store.
        self.assertIn("OPERATOR_ZULIP_USER_ID", names)
        self.assertIn("OPERATOR_ZULIP_EMAIL", names)
        for name in ("ZULIP_CLAUDE_API_KEY", "ZULIP_BF_COMPILER_EMAIL", "ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE",
                     "ZULIP_ALERT_GB_TRADER_HEADER"):
            self.assertNotIn(name, names)

    # ---- the launcher and the entrypoint as processes

    def test_the_launcher_execs_the_program_with_the_loaded_environment(self) -> None:
        probe = "import json, os; print(json.dumps({k: os.environ.get(k) for k in %r}))" % (
            ["ZULIP_ALERT_GB_FIXER_KEY", "INFISICAL_CLIENT_SECRET", "INFISICAL_CLIENT_ID", "ZULIP_ALERT_GB_FIXER_HEADER"],)
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            run = subprocess.run([sys.executable, str(SERVER_DIR / "infisical_env.py"), sys.executable, "-c", probe],
                                 env=self.env(fake), capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        got = json.loads(run.stdout)
        self.assertEqual(got, {"ZULIP_ALERT_GB_FIXER_KEY": self.values["ZULIP_ALERT_GB_FIXER_KEY"],
                               "INFISICAL_CLIENT_SECRET": None, "INFISICAL_CLIENT_ID": None,
                               "ZULIP_ALERT_GB_FIXER_HEADER": None})
        self.assertIn("simple-chat: loaded 5 variables from Infisical", run.stderr)
        self.assertNotIn(self.csecret, run.stderr)

    def entrypoint(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        env = {**env, "AGENT_SYNC_STATE_DIR": str(self.tmp / "data"), "AGENT_SYNC_INSTANCE": "server"}
        return subprocess.run(["sh", str(SERVER_DIR / "entrypoint.sh"), "status", "--json"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_the_entrypoint_loads_from_infisical_and_passes_the_arguments_through(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            run = self.entrypoint(self.env(fake))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(json.loads(run.stdout)["running"], "agent-sync status --json ran with the given arguments")
        self.assertIn("simple-chat: loaded 5 variables from Infisical", run.stderr)
        self.assertEqual([p.split("?")[0] for _, p in fake.requests],
                         ["/api/v1/auth/universal-auth/login", "/api/v3/secrets/raw", "/api/v3/secrets/raw"])
        # one login, then /zulip and the notes-queue keys' /operator folder (AFC-486)
        self.assertEqual([folder(p) for m, p in fake.requests if m == "GET"], ["/zulip", "/operator"])

    def test_the_entrypoint_without_an_identity_runs_as_before(self) -> None:
        env = self.env()
        for name in ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_PROJECT_ID"):
            env.pop(name)
        run = self.entrypoint(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("Infisical", run.stderr)

    def test_the_entrypoint_with_a_partial_identity_says_so_and_still_runs(self) -> None:
        env = self.env()
        env.pop("INFISICAL_PROJECT_ID")
        run = self.entrypoint(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("ERROR: Infisical not used (missing: INFISICAL_PROJECT_ID;", run.stderr)
        self.assertNotIn(self.csecret, run.stderr)

    def test_a_project_id_without_client_credentials_fails_loudly_naming_them(self) -> None:
        env = self.env()
        env.pop("INFISICAL_CLIENT_ID")
        env.pop("INFISICAL_CLIENT_SECRET")
        run = self.entrypoint(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("ERROR: Infisical not used (missing: INFISICAL_CLIENT_ID INFISICAL_CLIENT_SECRET;", run.stderr)
        self.assertIn("NOT in use", run.stderr)

    def test_the_load_line_counts_gb_seats_with_both_zulip_names_without_values(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            out, line = IE.build_env(self.env(fake))
        self.assertIn("; GB seats with both Zulip names from Infisical: ", line)
        self.assertIn("; GB seats with alert endpoint and key from Infisical: ", line)
        self.assertIn("(not loaded: ", line)
        for value in self.values.values():
            self.assertNotIn(value, line)

    def test_the_entrypoint_starts_even_when_infisical_is_unreachable(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            url = fake.url
        run = self.entrypoint(self.env(INFISICAL_API_URL=url))        # the fake is stopped:  connection refused
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Infisical load failed (login: ", run.stderr)
        self.assertIn("starting with the container environment", run.stderr)


# --------------------------------------------------------------------------------------------
# apply_live_config.py
# --------------------------------------------------------------------------------------------

class ApplyLiveConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sync-apply-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = self.tmp / "listener.toml"
        self.live.write_text(FIXTURE.read_text())
        os.chmod(self.live, 0o600)

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        code = AL.main(["--live", str(self.live), "--sample", str(SERVER_DIR / "listener.toml"), *args], out=out)
        return code, out.getvalue()

    def sections(self, text: str) -> dict[str, str]:
        return {name: "".join(body).strip() for name, body in AL.blocks(text)[1]}

    def test_the_fixture_is_the_pre_change_live_shape(self) -> None:
        raw = tomllib.loads(FIXTURE.read_text())
        self.assertNotEqual(raw["daemon"]["owner_user_id"], 0, "pinned by daemon init")
        self.assertEqual(raw["seat"]["JET"]["wake"], "inbox", "seeded before JET's http wake")
        self.assertEqual(raw["seat"]["GB-FIXER"]["enabled"], False)

    def test_a_dry_run_shows_the_seat_changes_and_writes_nothing(self) -> None:
        before = self.live.read_text()
        code, out = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.live.read_text(), before)
        self.assertIn("[seat.JET]\n    budget.board_per_day:", out)
        self.assertIn("wake: 'inbox' -> 'http'", out)
        self.assertIn("[seat.GB-FIXER]\n    enabled: False -> True", out)
        self.assertIn("routine.url_env: 'GB_FIXER_ROUTINE_URL' -> 'ZULIP_ALERT_GB_FIXER_ENDPOINT'", out)
        self.assertIn("dry run:  nothing written", out)
        self.assertEqual(list(self.tmp.glob("*.bak-*")), [])

    def test_apply_is_scoped_backed_up_valid_and_idempotent(self) -> None:
        before = self.live.read_text()
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        after = self.live.read_text()
        backups = list(self.tmp.glob("listener.toml.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o600, "the live file keeps its mode")
        self.assertEqual(AL.daemon_block(after), AL.daemon_block(before), "[daemon] stays byte for byte")
        old, new, sample = self.sections(before), self.sections(after), self.sections((SERVER_DIR / "listener.toml").read_text())
        for seat in UNTOUCHED:
            self.assertEqual(new["seat." + seat], old["seat." + seat], "%s is not a target" % seat)
        for seat in TARGETS:
            self.assertEqual(new["seat." + seat], sample["seat." + seat], seat)
        cfg = C.from_dict(tomllib.loads(after))
        self.assertEqual(cfg.errors, [])
        self.assertEqual(sorted(cfg.disabled), [], "every GB persona is enabled")
        self.assertEqual(AL.problems(after), [])
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("no changes", out)
        self.assertEqual(self.live.read_text(), after)
        self.assertEqual(len(list(self.tmp.glob("listener.toml.bak-*"))), 1, "a second run writes no backup")

    def test_write_is_durable_via_temp_replace_and_fsync(self) -> None:
        from unittest import mock

        live = self.tmp / "durable.toml"
        live.write_text("old\n")
        os.chmod(live, 0o600)
        replaces: list[tuple[str, str]] = []
        real_replace = os.replace

        def counting_replace(src, dst):  # type: ignore[no-untyped-def]
            replaces.append((str(src), str(dst)))
            return real_replace(src, dst)

        fsyncs: list[int] = []
        real_fsync = os.fsync

        def counting_fsync(fd):  # type: ignore[no-untyped-def]
            fsyncs.append(fd)
            return real_fsync(fd)

        with mock.patch.object(os, "replace", side_effect=counting_replace):
            with mock.patch.object(os, "fsync", side_effect=counting_fsync):
                backup = AL.write(live, "new\n", "20250101T000000Z")
        self.assertEqual(backup.read_text(), "old\n")
        self.assertEqual(live.read_text(), "new\n")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o600)
        self.assertEqual(len(replaces), 2, "backup and replacement each rename once")
        for src, _ in replaces:
            self.assertEqual(Path(src).parent, self.tmp)
            self.assertTrue(Path(src).name.startswith("."))
        self.assertEqual(Path(replaces[0][1]).name, backup.name)
        self.assertEqual(Path(replaces[1][1]), live)
        self.assertGreaterEqual(len(fsyncs), 4, "both files and both directory entries")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".")], [])

    def test_a_missing_target_section_is_appended(self) -> None:
        text = self.live.read_text()
        _, blocks = AL.blocks(text)
        without = "".join("".join(body) for name, body in [(None, AL.blocks(text)[0])] + blocks
                          if name != "seat.GB-TRADER")
        self.live.write_text(without)
        self.assertNotIn("[seat.GB-TRADER]", without)
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("[seat.GB-TRADER]\n    added", out)
        after = self.live.read_text()
        self.assertEqual(after.count("[seat.GB-TRADER]"), 1)
        self.assertEqual(C.from_dict(tomllib.loads(after)).errors, [])
        self.assertIn("no changes", self.run_main()[1])

    def test_listener_api_and_notes_queue_are_added_when_missing_and_never_changed(self) -> None:
        """AFC-486:  a live volume gets the sample's [listener_api] and [notes_queue] (both off) when it has none, and
        one it already has keeps its own values, because enabling either is the operator's step on that volume."""
        headers = lambda text: [line for line in text.splitlines() if line in ("[listener_api]", "[notes_queue]")]  # noqa: E731
        self.assertEqual(headers(self.live.read_text()), [])
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("[listener_api]\n    added", out)
        self.assertIn("[notes_queue]\n    added", out)
        after = self.live.read_text()
        self.assertEqual(headers(after), ["[listener_api]", "[notes_queue]"])
        sample = self.sections((SERVER_DIR / "listener.toml").read_text())
        for name in ("listener_api", "notes_queue"):
            self.assertEqual(self.sections(after)[name], sample[name])
        cfg = C.from_dict(tomllib.loads(after))
        self.assertEqual(cfg.errors, [])
        self.assertEqual((cfg.listener_api_enabled, cfg.notes_queue_enabled), (False, False))
        self.assertEqual(cfg.notes_queue_server_seats, frozenset({"CLAUDE", "CODEX"}))
        self.assertIn("no changes", self.run_main()[1])
        # A volume where the operator turned both on (and narrowed the seats) keeps them byte for byte.
        live = self.live.read_text()
        for name in ("listener_api", "notes_queue"):
            live = live.replace(self.sections(live)[name] + "\n", "")
        enabled = (
            '\n[listener_api]\nenabled = true\nbind = "0.0.0.0"\nport = 8788\n'
            '\n[notes_queue]\nenabled = true\nworker_seats = ["JET"]\nserver_seats = []\n'
        )
        self.live.write_text(live + enabled)
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("no changes", out)
        self.assertTrue(self.live.read_text().endswith(enabled))
        cfg = C.from_dict(tomllib.loads(self.live.read_text()))
        self.assertEqual(cfg.errors, [])
        self.assertEqual((cfg.listener_api_enabled, cfg.notes_queue_enabled), (True, True))
        self.assertEqual((cfg.notes_queue_worker_seats, cfg.notes_queue_server_seats), (frozenset({"JET"}), frozenset()))
        # and a seat change elsewhere in the file still leaves them alone.
        self.live.write_text(self.live.read_text().replace('wake = "http"', 'wake = "inbox"', 1))
        code, out = self.run_main("--apply")
        self.assertEqual(code, 0, out)
        self.assertNotIn("[notes_queue]", out)
        self.assertNotIn("[listener_api]", out)
        self.assertTrue(self.live.read_text().endswith(enabled))

    def test_a_result_that_would_not_load_is_refused_and_nothing_is_written(self) -> None:
        # A Mac seat in the server's live file is a partition problem the merge cannot fix.
        self.live.write_text(self.live.read_text() + '\n[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n')
        before = self.live.read_text()
        code, out = self.run_main("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("refused:  the result would not load", out)
        self.assertIn("seat CLAUDE", out)
        self.assertEqual(self.live.read_text(), before)
        self.assertEqual(list(self.tmp.glob("*.bak-*")), [])

    def test_a_missing_live_file_exits_1(self) -> None:
        self.live.unlink()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("cannot read", out)

    def test_blocks_round_trip_the_text(self) -> None:
        for text in (FIXTURE.read_text(), (SERVER_DIR / "listener.toml").read_text()):
            preamble, blocks = AL.blocks(text)
            self.assertEqual("".join(preamble) + "".join("".join(b) for _, b in blocks), text)


if __name__ == "__main__":
    unittest.main()
