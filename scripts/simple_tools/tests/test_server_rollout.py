"""The server rollout helpers:  the Infisical loader the entrypoint runs at start (server/infisical_env.py)
and the scoped live-config update (server/apply_live_config.py).  No network:  Infisical is a loopback fake."""
from __future__ import annotations

import http.server
import importlib.util
import io
import ipaddress
import json
import os
import re
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

from simple_tools import config as C

REPO = Path(__file__).resolve().parents[3]
SERVER_DIR = REPO / "scripts" / "simple_tools" / "server"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "server-listener-live-oct9.toml"
TARGETS = ["JET"] + ["GB-" + r for r in ("DIRECTOR", "FIXER", "DESIGNER", "COMPILER", "HOUSEKEEPER", "PUBLISHER",
                                          "DEPLOYER", "MONITOR", "PLUMBER", "ORACLE", "TRADER", "ENGINEER")]
UNTOUCHED = ["MA", "GROK-WEB", "WISE", "ECHO"]
INFISICAL_PROJECT_ID = "00000000-0000-4000-8000-000000000042"


def _load(name: str):
    spec = importlib.util.spec_from_file_location("simple_tools_server_" + name, SERVER_DIR / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IE = _load("infisical_env")
AL = _load("apply_live_config")


def folder(path: str) -> str:
    """The secretPath a fake Infisical read asked for."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("secretPath", [""])[0]


# --------------------------------------------------------------------------------------------
# A loopback Infisical:  direct service-token bearer, then one folder of raw secrets
# --------------------------------------------------------------------------------------------

class FakeInfisical:
    def __init__(self, client_id: str, client_secret: str, secrets_map: dict[str, str]) -> None:
        self.client_id, self.client_secret, self.secrets = client_id, client_secret, secrets_map
        self.token = "st." + "service-token-fixture-value"
        # The second, narrower token:  exactly prod /wake-router, the router's bearer and the registry key.
        self.wake_token = "st." + "wake-router-token-fixture-value"
        self.operator_secrets = {
            "OPERATOR_ZULIP_USER_ID": "4242",
            "OPERATOR_ZULIP_EMAIL": "operator-bot@zulip.test",
            "OPERATOR_NOTES_WORKER_KEY": "nw-" + "n" * 40,
        }
        self.wake_secrets = {
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "rk-" + "r" * 40,
            "WAKE_ROUTER_KEY": "wk-" + "w" * 40,
            "WAKE_ROUTER_DELIVERY_ENABLED": "false",
        }
        self.login_status = 200
        self.read_failures = 0          # 5xx answers before a good read
        self.requests: list[tuple[str, str]] = []
        self.metadata_override: dict[str, object] = {}
        self.fail_folder = ""
        self.provenance_override: dict[str, str] = {}
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
                return self._send(404, {"message": "login route is not available"})

            def do_GET(self) -> None:
                fake.requests.append(("GET", self.path))
                presented = self.headers.get("Authorization")
                if presented not in ("Bearer " + fake.token, "Bearer " + fake.wake_token):
                    return self._send(401, {"message": "no token"})
                wake = presented == "Bearer " + fake.wake_token
                if self.path == "/api/v2/service-token" and wake:
                    return self._send(200, {
                        "id": "synthetic-wake-token-id",
                        "name": "synthetic-wake-router-reader",
                        "projectId": INFISICAL_PROJECT_ID,
                        "workspace": INFISICAL_PROJECT_ID,
                        "scopes": [{"environment": "prod", "secretPath": "/wake-router"}],
                        "permissions": ["read"],
                        "expiresAt": "2099-01-01T00:00:00Z",
                    })
                if self.path == "/api/v2/service-token":
                    metadata = {
                        "id": "synthetic-token-id",
                        "name": "synthetic-listener-reader",
                        "projectId": INFISICAL_PROJECT_ID,
                        "workspace": INFISICAL_PROJECT_ID,
                        "scopes": [
                            {"environment": "prod", "secretPath": "/zulip"},
                            {"environment": "prod", "secretPath": "/operator"},
                        ],
                        "permissions": ["read"],
                        "expiresAt": "2099-01-01T00:00:00Z",
                    }
                    metadata.update(fake.metadata_override)
                    return self._send(200, metadata)
                if fake.read_failures and self.path.startswith("/api/v3/secrets/raw?"):
                    fake.read_failures -= 1
                    return self._send(503, {"message": "busy"})
                requested_path = folder(self.path)
                if requested_path == fake.fail_folder:
                    return self._send(404, {"message": "no folder"})
                granted = ("/wake-router",) if wake else ("/zulip", "/operator")
                if requested_path not in granted or "environment=prod" not in self.path:
                    return self._send(404, {"message": "no folder"})
                listing = {"/zulip": fake.secrets, "/operator": fake.operator_secrets,
                           "/wake-router": fake.wake_secrets}[requested_path]
                self._send(200, {"secrets": [
                    {
                        "secretKey": k,
                        "secretValue": v,
                        "workspace": fake.provenance_override.get("workspace", INFISICAL_PROJECT_ID),
                        "environment": "prod",
                        "secretPath": requested_path,
                        "type": "shared",
                    }
                    for k, v in listing.items()
                ]})

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
        self.tmp = Path(tempfile.mkdtemp(prefix="simple-tools-infisical-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cid, self.csecret = "cid-" + secrets.token_hex(8), "cs-" + secrets.token_hex(16)
        self.values = {
            "ZULIP_GB_FIXER_EMAIL": "fixer-grok-bot@zulip.test",
            "ZULIP_GB_FIXER_API_KEY": "zk-" + secrets.token_hex(12),
            "ZULIP_ALERT_GB_FIXER_ENDPOINT": "https://routine.test/hook/fixer",
            "ZULIP_ALERT_GB_FIXER_KEY": "crsr_" + secrets.token_hex(16),
            "ZULIP_ALERT_GB_FIXER_HEADER": "Authorization: Bearer crsr_never",
            "ZULIP_JET_EMAIL": "openai-dot-bot@zulip.test",
            "JET_ROUTINE_URL": "https://jet.test/internal/wake/JET",
            "JET_ROUTINE_KEY": "jk-" + secrets.token_hex(20),
            "ZULIP_BF_BUILDER_API_KEY": "bf-" + secrets.token_hex(8),      # the retired BotFleet seat:  not a server seat
            "ZULIP_OPERATOR_API_KEY": "mac-" + secrets.token_hex(8),       # the one seat the partition gives to "mac"
            "ZULIP_SITE": "https://evil.test",                             # never from Infisical
            "PATH": "/nowhere",
        }

    def env(self, fake: FakeInfisical | None = None, **extra: str) -> dict[str, str]:
        env = {"INFISICAL_TOKEN": "st.service-token-fixture-value",
               "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN": "st.wake-router-token-fixture-value",
               "INFISICAL_PROJECT_ID": INFISICAL_PROJECT_ID, "PATH": "%s:/usr/bin:/bin" % os.path.dirname(sys.executable),
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
        for name in ("ZULIP_ALERT_GB_FIXER_HEADER", "ZULIP_BF_BUILDER_API_KEY", "ZULIP_OPERATOR_API_KEY"):
            self.assertNotIn(name, out)
        self.assertEqual(out["ZULIP_SITE"], "https://simplewithus.zulipchat.com")
        self.assertNotEqual(out["PATH"], "/nowhere")
        self.assertNotIn("INFISICAL_CLIENT_ID", out)
        self.assertNotIn("INFISICAL_CLIENT_SECRET", out)
        self.assertIn("loaded 12 variables from Infisical (prod /zulip; 1 replaced a container value, 5 other", line)
        for value in list(self.values.values()) + [self.csecret, fake.token]:
            self.assertNotIn(value, line)

    def test_the_sentry_dsn_is_loaded_and_other_sentry_names_are_not(self) -> None:
        values = dict(self.values, SENTRY_FLEET_DSN="https://k@o1.ingest.us.sentry.io/1",
                      SENTRY_API_TOKEN="sntrys_never", SIMPLE_TOOLS_SENTRY_DSN_OTHER="never")
        with FakeInfisical(self.cid, self.csecret, values) as fake:
            out, line = IE.build_env(self.env(fake))
        self.assertEqual(out["SENTRY_FLEET_DSN"], "https://k@o1.ingest.us.sentry.io/1")
        self.assertNotIn("SENTRY_API_TOKEN", out)
        self.assertNotIn("SIMPLE_TOOLS_SENTRY_DSN_OTHER", out)
        self.assertIn("loaded 13 variables", line)
        self.assertNotIn("k@o1", line)

    def test_missing_service_token_fails_closed_before_any_request(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            env = self.env(fake, ZULIP_GB_FIXER_API_KEY="coolify-copy")
            env.pop("INFISICAL_TOKEN")
            with self.assertRaisesRegex(IE.LoadError, "missing INFISICAL_TOKEN"):
                IE.build_env(env)

    def test_a_busy_read_is_retried_once(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.read_failures = 1
            out, line = IE.build_env(self.env(fake), sleep=lambda s: None)
        self.assertIn("loaded 12 variables", line)
        # The /zulip read is retried once;  then /operator and /wake-router are read once each.
        self.assertEqual([m for m, _ in fake.requests], ["GET"] * 7)
        raw_paths = [folder(p) for m, p in fake.requests if "/api/v3/secrets/raw?" in p]
        self.assertEqual(raw_paths, ["/zulip", "/zulip", "/operator", "/wake-router"])

    def test_token_metadata_project_mismatch_stops_before_secret_values(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.metadata_override = {"projectId": "wrong-project"}
            with self.assertRaisesRegex(IE.LoadError, "project identity did not match"):
                IE.build_env(self.env(fake))
        self.assertEqual([path for _, path in fake.requests], ["/api/v2/service-token"])

    def test_secret_record_provenance_mismatch_fails_closed(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.provenance_override = {"workspace": "wrong-project"}
            with self.assertRaisesRegex(IE.LoadError, "invalid or unverified listing"):
                IE.build_env(self.env(fake))
        raw_paths = [folder(path) for _, path in fake.requests if "/api/v3/secrets/raw?" in path]
        self.assertEqual(raw_paths, ["/zulip"])

    def test_a_plain_http_api_that_is_not_loopback_is_refused(self) -> None:
        with self.assertRaisesRegex(IE.LoadError, "must be an https URL"):
            IE.build_env(self.env(INFISICAL_API_URL="http://infisical.example.com/api"))

    def test_an_incomplete_identity_is_not_used(self) -> None:
        env = self.env()
        env.pop("INFISICAL_PROJECT_ID")
        with self.assertRaisesRegex(IE.LoadError, "missing INFISICAL_PROJECT_ID"):
            IE.build_env(env)

    # ---- names whose only source is Infisical (INFISICAL_ONLY), and per-seat names from no container

    COOLIFY_COPIES = {
        "OPERATOR_REGISTRY_KEY": "coolify-registry-" + "c" * 32,
        "OPERATOR_ZULIP_USER_ID": "999999",
        "OPERATOR_ZULIP_EMAIL": "coolify-copy@zulip.test",
        "JET_ROUTINE_KEY": "coolify-jet-" + "c" * 32,
        "JET_ROUTINE_URL": "https://coolify-copy.test/wake/JET",
        "WAKE_ROUTER_KEY": "coolify-router-" + "c" * 32,
        "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "coolify-client-" + "c" * 32,
        "ZULIP_GB_FIXER_API_KEY": "coolify-fixer-" + "c" * 32,
        "ZC_ROUTINE_KEY": "coolify-zc-" + "c" * 32,
        "ZC_ROUTINE_URL": "https://coolify-copy.test/wake/ZC",
        "ZULIP_CLAUDE_EMAIL": "coolify-claude@zulip.test",
        "ZULIP_OPERATOR_API_KEY": "coolify-operator-voice-" + "c" * 32,
        "OPERATOR_NOTES_RUNNER_KEY": "coolify-notes-" + "c" * 32,
    }

    def test_every_infisical_only_name_comes_from_infisical_and_never_from_coolify(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            out, line = IE.build_env(self.env(fake, **self.COOLIFY_COPIES))
        self.assertEqual(out["OPERATOR_ZULIP_USER_ID"], fake.operator_secrets["OPERATOR_ZULIP_USER_ID"])
        self.assertEqual(out["OPERATOR_ZULIP_EMAIL"], fake.operator_secrets["OPERATOR_ZULIP_EMAIL"])
        self.assertEqual(out["OPERATOR_REGISTRY_KEY"], fake.wake_secrets["SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"])
        self.assertEqual(out["JET_ROUTINE_URL"], self.values["JET_ROUTINE_URL"])
        self.assertEqual(out["JET_ROUTINE_KEY"], self.values["JET_ROUTINE_KEY"])
        self.assertEqual(out["ZULIP_GB_FIXER_API_KEY"], self.values["ZULIP_GB_FIXER_API_KEY"])
        # The one router bearer comes from /wake-router, never from the container copy.
        self.assertEqual(out["WAKE_ROUTER_KEY"], fake.wake_secrets["WAKE_ROUTER_KEY"])
        self.assertNotEqual(out["WAKE_ROUTER_KEY"], self.COOLIFY_COPIES["WAKE_ROUTER_KEY"])
        # Names Infisical does not hold are gone, never kept from the container:  a seat's names (any seat),
        # the Operator's voice and notes-queue keys.
        for name in ("SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY", "ZC_ROUTINE_KEY", "ZC_ROUTINE_URL",
                     "ZULIP_CLAUDE_EMAIL", "ZULIP_OPERATOR_API_KEY", "OPERATOR_NOTES_RUNNER_KEY"):
            self.assertNotIn(name, out)
        # The router's other knobs are not the Eavesdropper's to carry.
        self.assertNotIn("WAKE_ROUTER_DELIVERY_ENABLED", out)
        self.assertIn("Infisical-only names: 4 of 4 (", line)
        self.assertIn("OPERATOR_REGISTRY_KEY from /wake-router SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY", line)
        self.assertIn("container copies removed: %d (" % len(self.COOLIFY_COPIES), line)
        for value in (list(self.COOLIFY_COPIES.values()) + list(fake.wake_secrets.values())
                      + list(fake.operator_secrets.values()) + [self.values["JET_ROUTINE_KEY"], fake.wake_token]):
            self.assertNotIn(value, line)

    def test_a_coolify_value_present_but_infisical_missing_stops_startup(self) -> None:
        cases = [
            ("/operator", "OPERATOR_ZULIP_USER_ID", "OPERATOR_ZULIP_USER_ID (/operator)"),
            ("/operator", "OPERATOR_ZULIP_EMAIL", "OPERATOR_ZULIP_EMAIL (/operator)"),
            ("/wake-router", "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY",
             "OPERATOR_REGISTRY_KEY (/wake-router SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY)"),
        ]
        for where, name, named in cases:
            with self.subTest(name=name):
                with FakeInfisical(self.cid, self.csecret, dict(self.values)) as fake:
                    listing = {"/zulip": fake.secrets, "/operator": fake.operator_secrets,
                               "/wake-router": fake.wake_secrets}[where]
                    listing.pop(name)
                    with self.assertRaises(IE.LoadError) as caught:
                        IE.build_env(self.env(fake, **self.COOLIFY_COPIES))
                message = str(caught.exception)
                self.assertIn("Infisical is missing " + named, message)
                self.assertIn("a container copy is never used", message)
                for value in self.COOLIFY_COPIES.values():
                    self.assertNotIn(value, message)

    def test_a_seat_missing_from_infisical_is_not_loaded_and_every_other_seat_runs(self) -> None:
        """No seat's names are a start condition:  JET (or any seat) missing its routine pair is unready, named
        in the line, and the container copy is never used."""
        values = dict(self.values)
        values.pop("JET_ROUTINE_KEY")
        with FakeInfisical(self.cid, self.csecret, values) as fake:
            out, line = IE.build_env(self.env(fake, **self.COOLIFY_COPIES))
        self.assertNotIn("JET_ROUTINE_KEY", out)
        self.assertEqual(out["ZULIP_GB_FIXER_API_KEY"], self.values["ZULIP_GB_FIXER_API_KEY"])
        self.assertRegex(line, r"server seats with a wake pair from Infisical: 1 of \d+ \(not loaded: [^)]*\bJET\b")
        self.assertNotIn(self.COOLIFY_COPIES["JET_ROUTINE_KEY"], line)

    def test_an_unusable_seat_value_is_refused_for_that_seat_alone(self) -> None:
        values = dict(self.values, JET_ROUTINE_KEY="k", JET_ROUTINE_URL="ftp://jet.test/wake")
        with FakeInfisical(self.cid, self.csecret, values) as fake:
            out, line = IE.build_env(self.env(fake))
        self.assertNotIn("JET_ROUTINE_KEY", out)
        self.assertNotIn("JET_ROUTINE_URL", out)
        self.assertIn("not usable, so not loaded: JET_ROUTINE_KEY, JET_ROUTINE_URL", line)
        self.assertNotIn("ftp://", line)

    def test_a_quoted_routine_value_is_judged_as_the_daemon_reads_it(self) -> None:
        # The daemon strips one wrapping pair of quotes from a routine URL and key (adapters.resolve_routine), so the
        # loader does not refuse a seat for quotes the daemon would have removed.
        from simple_tools import adapters, zulip

        key = "k" * 40
        for quote in ('"', "'"):
            url = "%shttps://jet.test/wake%s" % (quote, quote)
            self.assertTrue(IE.seat_value_usable("JET_ROUTINE_URL", url), url)
            self.assertTrue(IE.seat_value_usable("JET_ROUTINE_URL", "  %s  " % url), url)
            self.assertTrue(IE.seat_value_usable("JET_ROUTINE_KEY", quote + key + quote))
            self.assertIsNone(adapters.routine_url_problem(zulip._strip_quotes(url)))
        # Quotes hide nothing:  what is inside is still judged, and a lone or mismatched quote is still refused.
        self.assertFalse(IE.seat_value_usable("JET_ROUTINE_URL", '"ftp://jet.test/wake"'))
        self.assertFalse(IE.seat_value_usable("JET_ROUTINE_URL", '"https://jet.test/wake'))
        self.assertFalse(IE.seat_value_usable("JET_ROUTINE_URL", "\"https://jet.test/wake'"))
        self.assertFalse(IE.seat_value_usable("JET_ROUTINE_KEY", '"short"'))
        # The loader's copy of the daemon's reading is held to it over every shape a stored value can take:  each
        # core, bare or wrapped once or twice in either quote or a mismatched pair, with each padding inside and out.
        cores = ("", "a", "https://jet.test/wake", "k" * 40, "a b", '"', "'", "`x`")
        wraps = ("%s", '"%s"', "'%s'", "\"%s'", "'%s\"", '""%s""', "''%s''", "'\"%s\"'", "\"'%s'\"", '"%s', "%s'")
        pads = ("", " ", "  ", "\t", "\n", "\r\n")
        corpus = {out + (wrap % (inner + core + inner)) + out
                  for core in cores for wrap in wraps for inner in pads for out in pads}
        self.assertGreater(len(corpus), 2000)
        for value in sorted(corpus):
            self.assertEqual(IE._strip_quotes(value), zulip._strip_quotes(value), repr(value))

    def test_a_seat_with_a_quoted_routine_pair_keeps_its_wake_pair_at_start(self) -> None:
        # End to end, through build_env:  the seat's two names are loaded as stored (the daemon strips the quotes
        # itself) and the start line counts the seat instead of naming its values not usable.
        with FakeInfisical(self.cid, self.csecret, dict(self.values)) as fake:
            _, plain_line = IE.build_env(self.env(fake))
        counted = re.search(r"server seats with a wake pair from Infisical: (\d+) of (\d+)", plain_line)
        self.assertIsNotNone(counted, plain_line)
        for quote in ('"', "'"):
            url = quote + self.values["JET_ROUTINE_URL"] + quote
            key = quote + self.values["JET_ROUTINE_KEY"] + quote
            values = dict(self.values, JET_ROUTINE_URL=url, JET_ROUTINE_KEY=key)
            with FakeInfisical(self.cid, self.csecret, values) as fake:
                out, line = IE.build_env(self.env(fake))
            self.assertEqual(out["JET_ROUTINE_URL"], url)
            self.assertEqual(out["JET_ROUTINE_KEY"], key)
            self.assertNotIn("not usable", line)
            self.assertIn(counted.group(0), line)  # the same count as with the pair stored bare
            self.assertNotIn(self.values["JET_ROUTINE_KEY"], line)

    def test_the_routine_url_rule_matches_the_daemons(self) -> None:
        # The ZCode bridge is plain http to a Tailscale address;  the daemon accepts that, so the loader does too.
        tailnet = "http://%s:8791/wake/ZC" % ipaddress.IPv4Address((100 << 24) | (64 << 16) | 7)
        for url in (tailnet, "http://127.0.0.1:8791/wake", "http://localhost/wake", "https://jet.test/wake"):
            self.assertTrue(IE.seat_value_usable("ZC_ROUTINE_URL", url), url)
        for url in ("http://jet.test/wake", "http://10.0.0.1/wake", "ftp://jet.test/wake", "https://u:p@jet.test/",
                    "https://[::1/wake", "https://jet.test:99999/wake", "https://jet.test/a b", "https:///wake"):
            self.assertFalse(IE.seat_value_usable("ZC_ROUTINE_URL", url), url)
        from simple_tools import adapters

        for url in (tailnet, "http://jet.test/wake", "https://[::1/wake", "https://jet.test:99999/wake"):
            self.assertEqual(IE.routine_url_usable(url), adapters.routine_url_problem(url) is None, url)

    def test_a_malformed_routine_url_is_refused_for_that_seat_and_never_raises(self) -> None:
        values = dict(self.values, JET_ROUTINE_URL="https://[jet.test/wake")
        with FakeInfisical(self.cid, self.csecret, values) as fake:
            out, line = IE.build_env(self.env(fake))
        self.assertNotIn("JET_ROUTINE_URL", out)
        self.assertIn("not usable, so not loaded: JET_ROUTINE_URL", line)
        self.assertIn("ZULIP_GB_FIXER_API_KEY", out, "every other seat still runs")

    def test_a_reference_in_a_name_the_loader_never_uses_does_not_stop_startup(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.wake_secrets["WAKE_ROUTER_MAC_SSH"] = "${fleet.some.reference}"
            fake.operator_secrets["OPERATOR_SOMETHING_ELSE"] = "${fleet.other.reference}"
            out, _ = IE.build_env(self.env(fake))
        self.assertEqual(out["OPERATOR_REGISTRY_KEY"], fake.wake_secrets["SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"])
        self.assertNotIn("WAKE_ROUTER_MAC_SSH", out)
        self.assertNotIn("OPERATOR_SOMETHING_ELSE", out)

    def test_a_reference_in_a_name_the_loader_uses_still_stops_startup(self) -> None:
        for folder, name in (("wake", "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"), ("operator", "OPERATOR_ZULIP_EMAIL")):
            with self.subTest(name=name), FakeInfisical(self.cid, self.csecret, self.values) as fake:
                getattr(fake, folder + "_secrets")[name] = "${fleet.some.reference}"
                with self.assertRaisesRegex(IE.LoadError, "unresolved reference"):
                    IE.build_env(self.env(fake))

    def test_an_unusable_infisical_only_value_stops_startup_without_the_value(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.wake_secrets["SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"] = "too-short-registry-key"
            with self.assertRaises(IE.LoadError) as caught:
                IE.build_env(self.env(fake))
        message = str(caught.exception)
        self.assertIn(
            "the Infisical value for OPERATOR_REGISTRY_KEY (/wake-router SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY) "
            "is not usable", message)
        self.assertNotIn("too-short-registry-key", message)

    def test_a_missing_infisical_only_name_stops_the_process_before_the_program_runs(self) -> None:
        probe = "import os; print(os.environ.get('OPERATOR_ZULIP_EMAIL'))"
        with FakeInfisical(self.cid, self.csecret, dict(self.values)) as fake:
            fake.operator_secrets.pop("OPERATOR_ZULIP_EMAIL")
            run = subprocess.run([sys.executable, str(SERVER_DIR / "infisical_env.py"), sys.executable, "-c", probe],
                                 env=self.env(fake, **self.COOLIFY_COPIES), capture_output=True, text=True,
                                 timeout=60)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stdout, "", "the program never ran on the Coolify copy")
        self.assertIn("ERROR: Infisical is missing OPERATOR_ZULIP_EMAIL (/operator)", run.stderr)
        self.assertNotIn(self.COOLIFY_COPIES["OPERATOR_ZULIP_EMAIL"], run.stderr)

    def test_the_registry_key_has_one_source_and_an_operator_copy_is_never_used(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.wake_secrets["SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"] = "   "
            fake.operator_secrets["OPERATOR_REGISTRY_KEY"] = "op-" + "o" * 40
            with self.assertRaises(IE.LoadError) as caught:
                IE.build_env(self.env(fake, OPERATOR_REGISTRY_KEY="coolify-registry-" + "c" * 32))
        message = str(caught.exception)
        self.assertIn("Infisical is missing OPERATOR_REGISTRY_KEY (/wake-router SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY);",
                      message)
        self.assertNotIn("/operator", message)
        self.assertNotIn("o" * 40, message)

    def test_a_failed_operator_read_stops_startup(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.fail_folder = "/operator"
            with self.assertRaisesRegex(IE.LoadError, r"^read operator: HTTP 404$"):
                IE.build_env(self.env(fake), sleep=lambda s: None)

    def test_the_retired_folder_and_environment_knobs_stop_startup_before_any_request(self) -> None:
        for name, value in (("INFISICAL_SECRET_PATH", "/zulip_operator"), ("INFISICAL_SECRET_PATH", "/zulip/"),
                            ("INFISICAL_SECRET_PATH", "/zulip"), ("INFISICAL_ENVIRONMENT", "prod")):
            with self.subTest(name=name, value=value):
                with FakeInfisical(self.cid, self.csecret, self.values) as fake:
                    with self.assertRaisesRegex(IE.LoadError, "configuration: %s is retired" % name):
                        IE.build_env(self.env(fake, **{name: value}))
                self.assertEqual(fake.requests, [])

    def test_an_empty_infisical_value_counts_as_missing(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.operator_secrets["OPERATOR_ZULIP_EMAIL"] = "   "
            with self.assertRaisesRegex(IE.LoadError, r"Infisical is missing OPERATOR_ZULIP_EMAIL \(/operator\)"):
                IE.build_env(self.env(fake, OPERATOR_ZULIP_EMAIL="coolify-copy@zulip.test"))

    def test_without_the_wake_router_token_startup_fails_before_any_request(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            env = self.env(fake)
            env.pop("EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN")
            with self.assertRaisesRegex(IE.LoadError, "missing EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN"):
                IE.build_env(env)
        self.assertEqual(fake.requests, [])

    def test_an_invalid_wake_router_token_is_named(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            with self.assertRaisesRegex(IE.LoadError, "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN is invalid"):
                IE.build_env(self.env(fake, EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN="not-a-service-token"))
        self.assertEqual(fake.requests, [])

    def test_the_primary_token_is_never_used_for_wake_router(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            # Bound to the primary token's value:  its grant is /zulip and /operator, so the exact-scope
            # check refuses it before any /wake-router value is requested.
            with self.assertRaisesRegex(IE.LoadError, "folder scope did not match"):
                IE.build_env(self.env(fake, EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN=fake.token))
        self.assertNotIn("/wake-router", [folder(p) for _, p in fake.requests])

    def test_a_failed_operator_or_wake_router_read_stops_startup(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            fake.wake_token = "st." + "rotated-away-token-value"
            # The error names the token that was refused, so the responder rotates the right one.
            with self.assertRaisesRegex(
                IE.LoadError, r"^service-token metadata \(EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN\): HTTP 401$"
            ):
                IE.build_env(self.env(fake))

    def test_the_wake_router_token_is_stripped_from_the_program_environment(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            out, _ = IE.build_env(self.env(fake))
        self.assertNotIn("EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN", out)
        self.assertNotIn("INFISICAL_TOKEN", out)

    def test_the_allowlist_follows_the_partition(self) -> None:
        names = IE.allowed_names(IE.server_codes(IE.PARTITION_DEFAULT))
        self.assertIn("ZULIP_ALERT_GB_TRADER_KEY", names)
        self.assertIn("JET_ROUTINE_KEY", names)
        self.assertIn("ZULIP_GROK_WEB_API_KEY", names)
        # Every live BotFleet seat is a server seat now (owner, Fri, Nov 6), so its pair loads too.
        self.assertIn("ZULIP_BF_COMPILER_EMAIL", names)
        self.assertIn("ZULIP_BF_TRADER_API_KEY", names)
        # The Operator's own Zulip identity comes only from /operator (INFISICAL_ONLY), never from the
        # seat folder's allowlist.
        self.assertNotIn("OPERATOR_ZULIP_USER_ID", names)
        self.assertNotIn("OPERATOR_ZULIP_EMAIL", names)
        # The Mac CLI seats are server seats since the one-listener cutover (owner, Tue, Nov 10).
        for name in ("ZULIP_CLAUDE_API_KEY", "ZULIP_CODEX_EMAIL", "ZULIP_ZC_API_KEY", "ZULIP_KIMI_API_KEY",
                     "ZULIP_AMP_API_KEY", "ZULIP_GROK_API_KEY", "ZULIP_OPENCODE_EMAIL"):
            self.assertIn(name, names)
        # GROK-BUILD is "none", so the GROK seat's older ZULIP_GROK_BUILD_* stem never loads, and
        # OPERATOR is "mac".
        for name in ("ZULIP_BF_BUILDER_EMAIL", "ZULIP_GROK_BUILD_API_KEY", "ZULIP_OPERATOR_API_KEY",
                     "ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE", "ZULIP_ALERT_GB_TRADER_HEADER"):
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
        self.assertIn("simple-chat: loaded 12 variables from Infisical", run.stderr)
        self.assertNotIn(self.csecret, run.stderr)

    def entrypoint(self, env: dict[str, str]) -> subprocess.CompletedProcess:
        env = {**env, "SIMPLE_TOOLS_STATE_DIR": str(self.tmp / "data"), "SIMPLE_TOOLS_INSTANCE": "server"}
        return subprocess.run(["sh", str(SERVER_DIR / "entrypoint.sh"), "status", "--json"], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_the_entrypoint_loads_from_infisical_and_passes_the_arguments_through(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            run = self.entrypoint(self.env(fake))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(json.loads(run.stdout)["running"], "simple-chat daemon status --json ran with the given arguments")
        self.assertIn("simple-chat: loaded 12 variables from Infisical", run.stderr)
        self.assertEqual([p.split("?")[0] for _, p in fake.requests if "/api/v3/secrets/raw?" in p],
                         ["/api/v3/secrets/raw"] * 3)
        # /zulip, /operator (AFC-486 and the Operator's identity) and the router's /wake-router
        self.assertEqual(
            [folder(p) for m, p in fake.requests if "/api/v3/secrets/raw?" in p],
            ["/zulip", "/operator", "/wake-router"],
        )
        raw_queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(p).query)
                       for _, p in fake.requests if "/api/v3/secrets/raw?" in p]
        for query in raw_queries:
            self.assertEqual(query["expandSecretReferences"], ["false"])
            self.assertEqual(query["recursive"], ["false"])
            self.assertEqual(query["include_imports"], ["false"])

    def test_the_entrypoint_without_a_service_token_fails_closed(self) -> None:
        env = self.env()
        for name in ("INFISICAL_TOKEN", "INFISICAL_PROJECT_ID"):
            env.pop(name)
        run = self.entrypoint(env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("missing INFISICAL_TOKEN", run.stderr)

    def test_the_entrypoint_with_a_service_token_but_no_project_fails_closed(self) -> None:
        env = self.env()
        env.pop("INFISICAL_PROJECT_ID")
        run = self.entrypoint(env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("missing INFISICAL_PROJECT_ID", run.stderr)

    def test_a_project_id_without_service_token_fails_loudly(self) -> None:
        env = self.env()
        env.pop("INFISICAL_TOKEN")
        run = self.entrypoint(env)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("missing INFISICAL_TOKEN", run.stderr)

    def test_the_load_line_counts_every_server_seat_the_same_way_without_values(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            out, line = IE.build_env(self.env(fake))
        codes = IE.server_codes(IE.PARTITION_DEFAULT)
        self.assertIn("; server seats with a Zulip pair from Infisical: 1 of %d" % len(codes), line)
        # GB-FIXER has an alert pair and JET a routine pair:  either kind counts, with no per-platform rule.
        self.assertIn("; server seats with a wake pair from Infisical: 2 of %d" % len(codes), line)
        self.assertNotIn("GB seats", line)
        self.assertIn("(not loaded: ", line)
        for value in self.values.values():
            self.assertNotIn(value, line)

    def test_the_entrypoint_starts_even_when_infisical_is_unreachable(self) -> None:
        with FakeInfisical(self.cid, self.csecret, self.values) as fake:
            url = fake.url
        run = self.entrypoint(self.env(INFISICAL_API_URL=url))        # the fake is stopped:  connection refused
        self.assertNotEqual(run.returncode, 0, run.stderr)
        self.assertIn("ERROR: service-token metadata (INFISICAL_TOKEN): URLError", run.stderr)


# --------------------------------------------------------------------------------------------
# apply_live_config.py
# --------------------------------------------------------------------------------------------

class ApplyLiveConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="simple-tools-apply-"))
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
        self.assertIn("[seat.GB-FIXER]\n    budget.owner_per_day: 20 -> '<unset>'\n    budget.owner_per_topic_per_hour: 6 -> '<unset>'\n    budget.per_topic_per_hour: 2 -> '<unset>'\n    budget.wakes_per_day: 40 -> '<unset>'\n    budget.wakes_per_hour: 6 -> '<unset>'\n    enabled: False -> True", out)
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
        self.assertEqual(sorted(cfg.disabled), ["AMP", "GROK"], "only the two keyless Mac CLI seats are off")
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
        # A seat the partition gives to no listener ("none") is a partition problem the merge cannot fix.
        self.live.write_text(self.live.read_text() + '\n[seat.GROK-BUILD]\nwake = "inbox"\n')
        before = self.live.read_text()
        code, out = self.run_main("--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("refused:  the result would not load", out)
        self.assertIn("seat GROK-BUILD", out)
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
