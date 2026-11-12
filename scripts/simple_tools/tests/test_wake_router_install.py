"""Offline checks for the one wake router's installer and canary (OPS-1048, AFC-694).

Everything runs under a temporary test root with synthetic paths:  no real home, no /etc, no
systemctl, no Infisical and no network beyond loopback.  The installer runs with
WAKE_ROUTER_TEST_ROOT set, against a synthetic git origin whose main branch carries the real unit
templates and a stub fleet_knobs, with systemctl, loginctl, tailscale and socat as recording
stubs.  The canary runs against a scripted fake router on a loopback port.  Fixture tokens and
keys are built in parts at run time, so no credential-shaped literal sits in this file.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_wake_router_install -v
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
INSTALL = SERVER / "install-wake-router.sh"
CANARY = SERVER / "wake-router-canary.sh"
ROUTER_TEMPLATE = SERVER / "afc-wake-router.service"
FRONT_TEMPLATE = SERVER / "afc-wake-tls.service"
FAKE_TOKEN = "st" + "." + "fixture" + "-not-a-real-token-" + "0123456789"
FAKE_KEY = "fixture" + "-router-" + "key-" + "0123456789abcdef"
TAILNET = ".".join(("100", "101", "102", "103"))  # a synthetic tailnet address, built in parts
SEAT = "SEATX"

KNOBS_STUB = '''
import json, os


class SecretUnavailable(Exception):
    def __init__(self, name, reason):
        super().__init__(reason)
        self.reason = reason


class Knob:
    def __init__(self, value, source, reason=""):
        self.value, self.source, self.reason = value, source, reason


_STATUS = {}


def _data():
    with open(os.path.join(os.environ["HOME"], "knobs.json")) as handle:
        return json.load(handle)


def resolve(name):
    from .registry import BY_NAME
    data = _data()
    secret = BY_NAME[name].secret
    if os.environ.get("FLEET_KNOBS_INFISICAL_TOKEN") != data["token"]:
        _STATUS["fetch"] = "failed"
        if secret:
            raise SecretUnavailable(name, "unreachable:unauthorized")
        return Knob(None, "default", "unreachable:unauthorized")
    _STATUS["fetch"] = "ok"
    raw = data["values"].get(name)
    if raw is None:
        if secret:
            raise SecretUnavailable(name, "unset")
        return Knob(None, "default", "unset")
    if name == "WAKE_ROUTER_DELIVERY_ENABLED":
        raw = raw == "true"
    return Knob(raw, "infisical")


def folder_status(path):
    return {"fetch": _STATUS.get("fetch", "not-attempted")}
'''
REGISTRY_STUB = '''
class Spec:
    def __init__(self, folder, secret):
        self.folder, self.secret = folder, secret


SECRETS = {"WAKE_ROUTER_KEY", "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"}
NAMES = ["WAKE_ROUTER_DELIVERY_ENABLED", "WAKE_ROUTER_DELIVERY_SEATS", "WAKE_ROUTER_MAC_SSH",
         "WAKE_ROUTER_KEY", "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL", "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"]
BY_NAME = {name: Spec("/wake-router", name in SECRETS) for name in NAMES}
'''
SYSTEMCTL_STUB = """#!/bin/sh
echo "systemctl $*" >> "$(dirname "$0")/../systemctl.log"
if [ "$2" = "is-active" ]; then
  if [ -f "$(dirname "$0")/../active-$3" ]; then echo active; exit 0; fi
  echo inactive; exit 3
fi
if [ "$2" = "show" ]; then echo no; fi
exit 0
"""
LOGINCTL_STUB = """#!/bin/sh
cat "$(dirname "$0")/../linger"
"""
TAILSCALE_STUB = """#!/bin/sh
echo %s
""" % TAILNET
SOCAT_STUB = "#!/bin/sh\nexit 0\n"


def program(script: Path) -> dict:
    """The Python program a script runs, loaded as a module (its __main__ guard stays off)."""
    text = script.read_text()
    body = text.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    namespace = {"__name__": "wake_router_script_under_test"}
    exec(compile(body, str(script), "exec"), namespace)  # noqa: S102 - our own file
    return namespace


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, env=env, check=True, capture_output=True)


class Fixture:
    """A test root:  home, etc, bin stubs, a synthetic origin and a staged token."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wake-router-install-")).resolve()
        case.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "etc" / "systemd" / "system").mkdir(parents=True)
        bindir = self.root / "bin"
        bindir.mkdir()
        for name, body in (("systemctl", SYSTEMCTL_STUB), ("loginctl", LOGINCTL_STUB),
                           ("tailscale", TAILSCALE_STUB), ("socat", SOCAT_STUB)):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        (self.root / "linger").write_text("yes\n")
        self.knobs({"WAKE_ROUTER_DELIVERY_ENABLED": "false", "WAKE_ROUTER_KEY": FAKE_KEY,
                    "WAKE_ROUTER_MAC_SSH": "mac-wake", "WAKE_ROUTER_DELIVERY_SEATS": SEAT})
        self.src = self.root / "src"
        server = self.src / "scripts" / "simple_tools" / "server"
        server.mkdir(parents=True)
        shutil.copy(ROUTER_TEMPLATE, server / ROUTER_TEMPLATE.name)
        shutil.copy(FRONT_TEMPLATE, server / FRONT_TEMPLATE.name)
        (self.src / "scripts" / "simple-chat").write_text("#!/usr/bin/env python3\n")
        stub = self.src / "scripts" / "fleet_knobs"
        stub.mkdir()
        (stub / "__init__.py").write_text("")
        (stub / "knobs.py").write_text(KNOBS_STUB)
        (stub / "registry.py").write_text(REGISTRY_STUB)
        git(self.root, "init", "--bare", "-q", "origin.git")
        git(self.src, "init", "-q")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "fixture")
        git(self.src, "remote", "add", "origin", str(self.root / "origin.git"))
        git(self.src, "push", "-q", "origin", "HEAD:main")
        self.staged = self.home / ".config" / "infisical-staging" / "afc-wake-router.env"
        self.write(self.staged, "%s=%s\n" % ("FLEET_KNOBS_INFISICAL_TOKEN", FAKE_TOKEN))
        self.cert = self.root / "tls-in" / "cert.pem"
        self.key = self.root / "tls-in" / "key.pem"
        self.write(self.cert, "synthetic certificate\n", 0o644)
        self.write(self.key, "synthetic key\n")
        self.units = self.home / ".config" / "systemd" / "user"
        self.env_file = self.home / ".config" / "afc-wake-router" / "env"
        self.base = self.home / ".local" / "share" / "afc-wake-router"
        self.tls = self.home / ".local" / "state" / "afc-wake-router" / "tls"

    @staticmethod
    def write(path: Path, text: str, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)

    def knobs(self, values: dict) -> None:
        (self.home / "knobs.json").write_text(json.dumps({"token": FAKE_TOKEN, "values": values}))

    def install(self, *args: str, tls: bool = True) -> subprocess.CompletedProcess:
        argv = ["bash", str(INSTALL), "--repo", str(self.src), "--tailnet-addr", TAILNET, *args]
        if tls:
            argv += ["--tls-cert", str(self.cert), "--tls-key", str(self.key)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "WAKE_ROUTER_TEST_ROOT": str(self.root)}
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)

    def systemctl_log(self) -> str:
        path = self.root / "systemctl.log"
        return path.read_text() if path.exists() else ""


class InstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)

    def assertNoSecret(self, result) -> None:
        for stream in (result.stdout, result.stderr):
            self.assertNotIn(FAKE_TOKEN, stream)
            self.assertNotIn(FAKE_KEY, stream)

    def test_dry_run_changes_nothing(self) -> None:
        result = self.fx.install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("a real run would install release", result.stdout)
        self.assertFalse(self.fx.units.exists())
        self.assertFalse(self.fx.base.exists())
        self.assertTrue(self.fx.staged.exists())
        self.assertEqual(self.fx.systemctl_log(), "")
        self.assertNoSecret(result)

    def test_dry_run_lists_every_refusal(self) -> None:
        self.fx.staged.unlink()
        (self.fx.root / "linger").write_text("no\n")
        (self.fx.root / "etc/systemd/system/afc-wake-router.service").write_text("[Unit]\n")
        result = self.fx.install("--dry-run", "--port", "08794", "--bind", "0.0.0.0")
        self.assertEqual(result.returncode, 3, result.stdout)
        for words in ("no token", "lingering is off", "root unit", "sudo systemctl disable --now",
                      "plain decimal port", "--bind must be one of"):
            self.assertIn(words, result.stdout)
        self.assertFalse(self.fx.base.exists())

    def test_install_writes_user_units_token_and_release(self) -> None:
        result = self.fx.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNoSecret(result)
        router = (self.fx.units / "afc-wake-router.service").read_text()
        front = (self.fx.units / "afc-wake-tls.service").read_text()
        self.assertTrue(router.startswith("# Installed by install-wake-router.sh"))
        self.assertIn("%h/.local/share/afc-wake-router/current/scripts/simple-chat wake-router serve "
                      "--bind 127.0.0.1 --port 8794", router)
        self.assertNotIn("fleet-cli/current", router)
        self.assertIn("OPENSSL-LISTEN:8795,bind=%s," % TAILNET, front)
        self.assertIn("TCP:127.0.0.1:8794", front)
        self.assertNotIn("@", front.split("[Unit]", 1)[1])
        env = self.fx.env_file
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        body = [line for line in env.read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(body, ["FLEET_KNOBS_INFISICAL_TOKEN=" + FAKE_TOKEN])
        self.assertFalse(self.fx.staged.exists())
        self.assertTrue((self.fx.base / "current" / "scripts" / "simple-chat").is_file())
        self.assertEqual((self.fx.tls / "key.pem").stat().st_mode & 0o777, 0o600)
        log = self.fx.systemctl_log()
        self.assertIn("systemctl --user daemon-reload", log)
        self.assertIn("systemctl --user enable afc-wake-router.service afc-wake-tls.service", log)
        self.assertNotIn(" start ", log)

    def test_install_reads_origin_main_never_the_working_tree(self) -> None:
        knobs = self.fx.src / "scripts" / "fleet_knobs" / "knobs.py"
        knobs.write_text("raise SystemExit(9)\n")  # uncommitted:  must never run
        (self.fx.src / "scripts" / "planted.py").write_text("raise SystemExit(9)\n")  # untracked
        result = self.fx.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = self.fx.base / "current" / "scripts"
        self.assertFalse((installed / "planted.py").exists())
        self.assertIn("SecretUnavailable", (installed / "fleet_knobs" / "knobs.py").read_text())

    def test_missing_key_and_wrong_mac_route_are_refused(self) -> None:
        self.fx.knobs({"WAKE_ROUTER_DELIVERY_ENABLED": "false", "WAKE_ROUTER_MAC_SSH": "elsewhere"})
        result = self.fx.install("--dry-run")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("knob WAKE_ROUTER_KEY is not set in /wake-router (unset)", result.stdout)
        self.assertIn("WAKE_ROUTER_MAC_SSH must be mac-wake", result.stdout)

    def test_wrong_token_fails_the_fetch(self) -> None:
        self.fx.write(self.fx.staged, "FLEET_KNOBS_INFISICAL_TOKEN=%s\n" % ("other" * 5))
        result = self.fx.install("--dry-run")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("could not read /wake-router", result.stdout)

    def test_export_prefix_and_other_names_are_refused(self) -> None:
        for text, words in (
            ("export FLEET_KNOBS_INFISICAL_TOKEN=%s\n" % FAKE_TOKEN, "export"),
            ("FLEET_KNOBS_INFISICAL_TOKEN=%s\nWAKE_ROUTER_BIND=0.0.0.0\n" % FAKE_TOKEN, "may hold only"),
            ('FLEET_KNOBS_INFISICAL_TOKEN="%s"\n' % FAKE_TOKEN, "quoted"),
        ):
            with self.subTest(words=words):
                self.fx.write(self.fx.staged, text)
                result = self.fx.install("--dry-run")
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertIn(words, result.stdout)
                self.assertNoSecret(result)

    def test_group_readable_token_is_refused(self) -> None:
        self.fx.staged.chmod(0o640)
        result = self.fx.install("--dry-run")
        self.assertEqual(result.returncode, 3)
        self.assertIn("no group or other access", result.stdout)

    def test_start_refused_while_delivery_is_armed(self) -> None:
        self.fx.knobs({"WAKE_ROUTER_DELIVERY_ENABLED": "true", "WAKE_ROUTER_KEY": FAKE_KEY,
                       "WAKE_ROUTER_MAC_SSH": "mac-wake"})
        result = self.fx.install("--start")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("--start is refused while delivery is armed", result.stdout)
        self.assertFalse(self.fx.units.exists())
        self.assertTrue(self.fx.staged.exists())

    def test_start_starts_then_restarts_a_stale_unit(self) -> None:
        result = self.fx.install("--start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.fx.systemctl_log()
        self.assertIn("systemctl --user start afc-wake-router.service", log)
        self.assertIn("systemctl --user start afc-wake-tls.service", log)
        for unit in ("afc-wake-router.service", "afc-wake-tls.service"):
            (self.fx.root / ("active-" + unit)).write_text("")
        (self.fx.root / "systemctl.log").unlink()
        # A new origin/main:  the router runs stale code and must be restarted.
        (self.fx.src / "scripts" / "new.py").write_text("")
        git(self.fx.src, "add", "-A")
        git(self.fx.src, "commit", "-q", "-m", "next")
        git(self.fx.src, "push", "-q", "origin", "HEAD:main")
        result = self.fx.install("--start", tls=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.fx.systemctl_log()
        self.assertIn("systemctl --user restart afc-wake-router.service", log)
        self.assertNotIn("restart afc-wake-tls.service", log)
        self.assertEqual(len(list((self.fx.base / "releases").iterdir())), 1)

    def test_foreign_unit_is_never_overwritten(self) -> None:
        self.fx.write(self.fx.units / "afc-wake-router.service", "[Unit]\n", 0o644)
        result = self.fx.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("was not written by this script", result.stdout)
        self.assertEqual((self.fx.units / "afc-wake-router.service").read_text(), "[Unit]\n")

    def test_uninstall_removes_exactly_what_it_installed(self) -> None:
        self.assertEqual(self.fx.install().returncode, 0)
        keep = [self.fx.units / "other.service", self.fx.home / ".local/state/wake-router/receipt.json"]
        for path in keep:
            self.fx.write(path, "kept\n", 0o644)
        result = self.fx.install("--uninstall", tls=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.fx.units / "afc-wake-router.service").exists())
        self.assertFalse((self.fx.units / "afc-wake-tls.service").exists())
        self.assertFalse(self.fx.base.exists())
        self.assertFalse(self.fx.tls.exists())
        self.assertFalse(self.fx.env_file.exists())
        self.assertTrue(self.fx.staged.exists())
        self.assertEqual(self.fx.staged.stat().st_mode & 0o777, 0o600)
        for path in keep:
            self.assertTrue(path.exists(), path)
        self.assertIn("systemctl --user disable --now afc-wake-router.service", self.fx.systemctl_log())

    def test_help_prints_only_the_header(self) -> None:
        for script in (INSTALL, CANARY):
            out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True).stdout
            self.assertIn("Run it on the server" if script == INSTALL else "--seat SEAT", out)
            self.assertNotIn("set -euo", out)
            self.assertNotIn("import ", out)


class InstallerRulesTest(unittest.TestCase):
    """The pure checks, loaded from the script itself."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.m = program(INSTALL)

    def test_ports_are_plain_decimal_and_never_reserved(self) -> None:
        check = self.m["check_port"]
        self.assertEqual(check("8794", "--port"), (8794, ""))
        for bad in ("08794", "0x2262", "+8794", "8794 ", "8793", "80", "65536", "", "８７９４"):
            with self.subTest(bad=bad):
                self.assertIsNone(check(bad, "--port")[0])

    def test_bind_is_an_allowlist(self) -> None:
        check = self.m["check_bind"]
        self.assertEqual(check("127.0.0.1"), "")
        for bad in ("0.0.0.0", "0", "0x0", "127.000.000.001", "127.0.0.2", "::", "::1",
                    TAILNET, "8.8.8.8", "localhost"):
            with self.subTest(bad=bad):
                self.assertTrue(check(bad))

    def test_tailnet_address_is_checked(self) -> None:
        check = self.m["check_tailnet"]
        self.assertEqual(check(TAILNET), "")
        for bad in ("100.064.1.1", "8.8.8.8", "100.128.0.1", "fd7a:115c:a1e0::1", "0"):
            with self.subTest(bad=bad):
                self.assertTrue(check(bad))

    def test_test_root_is_ignored_for_root(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="wake-router-rules-"))
        self.addCleanup(shutil.rmtree, root, True)
        home, etc, test_root, _problem = self.m["paths_for"](0, {"WAKE_ROUTER_TEST_ROOT": str(root)})
        self.assertEqual(test_root, "")
        self.assertNotEqual(home, root / "home")
        self.assertEqual(etc, Path("/etc"))
        home, etc, test_root, _problem = self.m["paths_for"](os.getuid() or 1, {"WAKE_ROUTER_TEST_ROOT": str(root)})
        self.assertEqual((home, etc), (root / "home", root / "etc"))

    def test_root_is_refused_before_anything_runs(self) -> None:
        for argv in (["--dry-run"], [], ["--uninstall"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = self.m["main"](argv, {"WAKE_ROUTER_TEST_ROOT": "/nonexistent"}, 0)
                self.assertIn("never root", out.getvalue())
                self.assertIn(code, (1, 3))

    def test_python_versions(self) -> None:
        for script in (INSTALL, CANARY):
            text = script.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
            ast.parse(text, feature_version=(3, 9))
            self.assertNotIn("from datetime import UTC", text)


class FakeRouter:
    """A scripted router on a loopback port.  `script` maps a path to a list of (status, body)
    answers, the last repeated."""

    def __init__(self, case: unittest.TestCase, script: dict) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.seen: list[tuple[str, str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                pass

            def _go(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                outer.seen.append((self.command, self.path, self.headers.get("Authorization") or ""))
                outer.bodies.append(body)
                answers = outer.script.get(self.path) or [(404, {"error": "not found"})]
                status, payload = answers.pop(0) if len(answers) > 1 else answers[0]
                data = json.dumps(payload).encode()
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("Location", payload.get("location", "/"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _go
            do_POST = _go

        self.bodies: list[str] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]


def receipt(wake_id, **fields):
    record = {"wake_id": wake_id, "receipt_id": "r1", "status": "delivered", "picked_letter": "QQ",
              "delivered": True, "seat": SEAT}
    record.update(fields)
    return record


class CanaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wake-router-canary-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        home = self.root / "home"
        Fixture.write(home / ".config" / "afc-wake-router" / "env",
                      "# synthetic\nFLEET_KNOBS_INFISICAL_TOKEN=%s\n" % FAKE_TOKEN)
        stub = home / ".local" / "share" / "afc-wake-router" / "current" / "scripts" / "fleet_knobs"
        stub.mkdir(parents=True)
        (stub / "__init__.py").write_text("")
        (stub / "knobs.py").write_text(KNOBS_STUB)
        (stub / "registry.py").write_text(REGISTRY_STUB)
        (home / "knobs.json").write_text(json.dumps({"token": FAKE_TOKEN,
                                                     "values": {"WAKE_ROUTER_KEY": FAKE_KEY}}))

    def run_canary(self, router, *args, wake_id="w-1", poll="0.05"):
        argv = ["bash", str(CANARY), "--seat", SEAT, "--letter", "QQ", *args,
                "--poll-seconds", poll]
        if router is not None:
            argv += ["--url", router.url]
        if "--follow" not in args:
            argv += ["--wake-id", wake_id]
            if "--message-id" not in args:
                argv += ["--message-id", "5"]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "WAKE_ROUTER_TEST_ROOT": str(self.root)}
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn(FAKE_KEY, result.stdout + result.stderr)
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
        return result

    def healthy(self, extra):
        script = {"/health/" + SEAT: [(200, {"seat": SEAT, "healthy": True, "delivery": True})]}
        script.update(extra)
        return script

    def test_dry_run_sends_nothing(self) -> None:
        router = FakeRouter(self, self.healthy({}))
        result = self.run_canary(router, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(router.seen, [])

    def test_delivered_receipt_is_proven_with_the_bearer_in_the_header_only(self) -> None:
        router = FakeRouter(self, self.healthy({
            "/wake/" + SEAT: [(202, {"wake_id": "w-1", "receipt_id": "r1", "status": "accepted"})],
            "/receipt/%s/w-1" % SEAT: [(200, receipt("w-1"))],
        }))
        result = self.run_canary(router)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("proven:", result.stdout)
        methods = [(m, p) for m, p, _a in router.seen]
        self.assertEqual(methods[:2], [("GET", "/health/" + SEAT), ("POST", "/wake/" + SEAT)])
        self.assertEqual(router.seen[0][2], "")  # health carries no bearer
        self.assertEqual(router.seen[1][2], "Bearer " + FAKE_KEY)
        sent = json.loads(router.bodies[1])
        self.assertEqual((sent["seat"], sent["letter"], sent["wake_id"]), (SEAT, "QQ", "w-1"))

    def test_failures(self) -> None:
        accept = (202, {"wake_id": "w-1", "receipt_id": "r1", "status": "accepted"})
        cases = {
            "delivered null": ([accept], [(200, receipt("w-1", status="dead", delivered=None))], 4),
            "delivered false": ([accept], [(200, receipt("w-1", delivered=False))], 4),
            "held": ([accept], [(200, receipt("w-1", status="held", delivered=False))], 3),
            "wrong letter": ([accept], [(200, receipt("w-1", picked_letter="ZZ"))], 6),
            "receipt id changed": ([accept], [(200, receipt("w-1", receipt_id="r2"))], 1),
            "missing wake id": ([(202, {"receipt_id": "r1", "status": "accepted"})], [], 1),
            "other wake id": ([(202, {"wake_id": "w-9", "receipt_id": "r1"})], [], 1),
            "refused": ([(401, {"error": "bad bearer"})], [], 1),
        }
        for name, (post, receipts, code) in cases.items():
            with self.subTest(name=name):
                script = {"/wake/" + SEAT: post}
                if receipts:
                    script["/receipt/%s/w-1" % SEAT] = receipts
                result = self.run_canary(FakeRouter(self, self.healthy(script)))
                self.assertEqual(result.returncode, code, name + ": " + result.stdout + result.stderr)

    def test_test_mode_checks_the_letter_and_delivered_false(self) -> None:
        answer = receipt("w-1", status="test", test=True, delivered=False)
        for name, body, code in (
            ("ok", answer, 0),
            ("wrong letter", {**answer, "picked_letter": "ZZ"}, 6),
            ("delivered null", {**answer, "delivered": None}, 1),
            ("no wake id", {k: v for k, v in answer.items() if k != "wake_id"}, 1),
        ):
            with self.subTest(name=name):
                router = FakeRouter(self, self.healthy({"/wake/" + SEAT: [(200, body)]}))
                result = self.run_canary(router, "--test")
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertTrue(json.loads(router.bodies[1]).get("test"))

    def test_redirect_is_never_followed(self) -> None:
        other = FakeRouter(self, {})
        router = FakeRouter(self, self.healthy({
            "/wake/" + SEAT: [(307, {"location": other.url + "/wake/" + SEAT})]}))
        result = self.run_canary(router)
        self.assertEqual(result.returncode, 1)
        self.assertIn("redirect", result.stderr)
        self.assertEqual(other.seen, [])

    def test_unhealthy_seat_stops_before_the_key_or_a_wake(self) -> None:
        router = FakeRouter(self, {"/health/" + SEAT: [(503, {"healthy": False, "reason": "not-installed"})]})
        result = self.run_canary(router)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([m for m, _p, _a in router.seen], ["GET"])

    def test_follow_reads_an_existing_wake(self) -> None:
        router = FakeRouter(self, self.healthy({
            "/receipt/%s/w-7" % SEAT: [(404, {"error": "no such wake"}), (200, receipt("w-7"))]}))
        result = self.run_canary(router, "--follow", "w-7", "--wait", "5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("POST", [m for m, _p, _a in router.seen])

    def test_url_and_settings_are_validated(self) -> None:
        for args in (
            ("--url", "https://127.0.0.1:8794"),
            ("--url", "http://10.0.0.1:8794"),
            ("--url", "http://localhost:8794"),
            ("--url", "http://127.0.0.1:08794"),
            ("--url", "http://127.0.0.1:8794/x"),
            ("--url", "http://user@127.0.0.1:8794"),
            ("--seat", "bad seat"),
            ("--letter", "q"),
            ("--wait", "-1"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.run_canary(None, *args).returncode, 2)
        for poll in ("abc", "0", "nan", "1e3", "61", "-1"):
            with self.subTest(poll=poll):
                self.assertEqual(self.run_canary(None, poll=poll).returncode, 2)


if __name__ == "__main__":
    unittest.main()
