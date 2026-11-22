"""Offline controlled-upgrade custody regressions. No real units, credentials or delivery."""

import json
import tempfile
import unittest
from pathlib import Path

from simple_tools.router_upgrade import UpgradeRefused, census, compare, stopped


class Custody(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "done").mkdir()
        (self.root / "pending").mkdir()
        self.roots = {"SEATX": str(self.root)}

    def write(self, name="a", **over):
        row = {"wake_id": name, "status": "completed", "delivered": True, "code": ""}
        row.update(over)
        (self.root / "done" / (name + ".json")).write_text(json.dumps(row))
        return row

    def test_historical_terminal_unknown_is_not_new_uncertainty(self):
        self.write(delivered=None, status="unknown", code="interrupted")
        before = census(self.roots)
        compare(before, census(self.roots))
        self.write("b")
        compare(before, census(self.roots))

    def test_new_or_changed_unknown_holds(self):
        before = census(self.roots)
        self.write(delivered=None, status="unknown")
        with self.assertRaises(UpgradeRefused):
            compare(before, census(self.roots))
        before = census(self.roots)
        self.write(delivered=None, status="unknown", code="changed")
        with self.assertRaises(UpgradeRefused):
            compare(before, census(self.roots))

    def test_unreadable_pending_or_log_holds_before_stop(self):
        for path in (self.root / "pending" / "a.json", self.root / "wakes.jsonl"):
            path.write_text("{broken")
            with self.assertRaises(UpgradeRefused):
                census(self.roots)
            path.unlink()

    def test_conflicting_log_and_done_hold(self):
        self.write()
        (self.root / "wakes.jsonl").write_text(
            json.dumps({"wake_id": "a", "delivered": None})
        )
        with self.assertRaises(UpgradeRefused):
            census(self.roots)

    def test_symlink_or_missing_state_holds(self):
        link = self.root / "other"
        link.symlink_to(self.root, target_is_directory=True)
        for path in (link, self.root / "missing"):
            with self.assertRaises(UpgradeRefused):
                census({"S": str(path)})

    def test_stop_success_alone_is_not_process_proof(self):
        class Host:
            calls = 0

            def systemctl(self, *args):
                if args[0] == "show":
                    self.calls += 1
                    return 0, "123" if self.calls == 1 else "0", ""
                return (3, "inactive", "") if args[0] == "is-active" else (0, "", "")

        (self.root / "123").mkdir()
        with self.assertRaises(UpgradeRefused):
            stopped(Host(), "unit", self.root)
        (self.root / "123").rmdir()
        stopped(Host(), "unit", self.root)


class CompleteCensus(Custody):
    def test_historic_unknown_disappearance_refused(self):
        self.write(delivered=None, status="unknown")
        before = census(self.roots)
        (self.root / "done" / "a.json").unlink()
        with self.assertRaises(UpgradeRefused):
            compare(before, census(self.roots))

    def test_pending_disappearance_and_pending_done_conflict_refused(self):
        import re

        from simple_tools import wake_router as WR

        platform = WR.Platform(
            "CLAUDE", "claude", "@claude", re.compile(".*"), lambda *a: None
        )
        router = WR.Router(platform, {}, {}, self.root, lambda: ("x" * 48, ""))
        self.assertEqual(router.accept(WR.Wake("one", seat="CLAUDE"))[0], 202)
        before = census(self.roots)
        pending = self.root / "pending" / "one.json"
        value = json.loads(pending.read_text())
        pending.unlink()
        with self.assertRaises(UpgradeRefused):
            compare(before, census(self.roots))
        value["receipt"] = {"wake_id": "one", "status": "completed", "delivered": True}
        pending.write_text(json.dumps(value))
        self.write("one", delivered=False)
        with self.assertRaises(UpgradeRefused):
            census(self.roots)

    def test_inflight_pending_holds_before_outage(self):
        import re

        from simple_tools import wake_router as WR

        platform = WR.Platform(
            "CLAUDE", "claude", "@claude", re.compile(".*"), lambda *a: None
        )
        router = WR.Router(platform, {}, {}, self.root, lambda: ("x" * 48, ""))
        router.accept(WR.Wake("one", seat="CLAUDE"))
        pending = self.root / "pending" / "one.json"
        value = json.loads(pending.read_text())
        value["in_flight"] = True
        pending.write_text(json.dumps(value))
        with self.assertRaises(UpgradeRefused):
            census(self.roots)


class Orchestration(unittest.TestCase):
    def run_flow(self, failure=None):
        from unittest import mock

        from simple_tools import router_upgrade as U

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "release"
            base.mkdir()
            units = root / "units"
            units.mkdir()
            proc = root / "proc"
            proc.mkdir()
            old = "releases/" + "a" * 40
            new = "releases/" + "b" * 40
            for name in (old, new):
                scripts = base / name / "scripts"
                scripts.mkdir(parents=True)
                (scripts / "simple-chat").write_text("fixture")
            (base / "current").symlink_to(old)
            routerunit = "afc-wake-router.service"
            frontunit = "afc-wake-tls.service"
            (units / routerunit).write_text(
                "ExecStart=python "
                + str(base / old / "scripts/simple-chat")
                + " wake-router serve --port 8794\n"
            )
            (units / frontunit).write_text("fixture front")
            process = proc / "123"
            process.mkdir()
            (process / "cmdline").write_bytes(
                str(base / old / "scripts/simple-chat").encode() + b"\0"
            )
            (process / "environ").write_bytes(b"HOME=/synthetic\0")
            state = root / "state"
            state.mkdir()
            events = []

            class Host:
                running = True
                promotion = False
                failed = False

                def run(self, argv, stdin=None, **kw):
                    if "qualify_candidate" in argv[3]:
                        events.append("qualify_no_effect")
                        return (
                            0,
                            json.dumps(
                                {
                                    "qualified": True,
                                    "health": {"healthy": True, "seats": {}},
                                    "seats": [],
                                }
                            ),
                            "",
                        )
                    return 0, json.dumps({"CLAUDE": str(state)}), ""

                def systemctl(self, *args):
                    events.append(tuple(args))
                    if (
                        self.promotion
                        and not self.failed
                        and (
                            (failure == "reload" and args[0] == "daemon-reload")
                            or (
                                failure == "router-start"
                                and args == ("start", routerunit)
                            )
                            or (
                                failure == "front-start"
                                and args == ("start", frontunit)
                            )
                        )
                    ):
                        self.failed = True
                        return 1, "", "fixture failure"
                    if args[0] == "show":
                        return (
                            0,
                            ("123" if self.running and args[-1] == routerunit else "0"),
                            "",
                        )
                    if args[0] == "is-active":
                        return 3, "inactive", ""
                    if args[0] == "stop" and args[1] == routerunit:
                        self.running = False
                        import shutil

                        shutil.rmtree(process, ignore_errors=True)
                    if args[0] == "start" and args[1] == routerunit:
                        self.running = True
                        process.mkdir()
                        (process / "cmdline").write_bytes(
                            str(base / new / "scripts/simple-chat").encode()
                            + b"\0--start-quiesced\0"
                        )
                    return 0, "", ""

            host = Host()
            with (
                mock.patch.object(
                    U, "health", return_value={"healthy": True, "seats": {}}
                ),
                mock.patch.object(
                    U,
                    "wait_health",
                    side_effect=[
                        {"healthy": True, "seats": {}, "upgrade_quiesced": True},
                        {"healthy": True, "seats": {}, "upgrade_quiesced": False},
                    ],
                ),
            ):
                upgrade = U.ControlledUpgrade(
                    host, base, units, proc, base / new / "scripts", "python", 8794
                )
                self.assertFalse(
                    any(isinstance(x, tuple) and x[0] == "stop" for x in events)
                )
                upgrade.quiesce()
                self.assertTrue((upgrade.snapshot / "custody.json").is_file())
                self.assertIn(
                    "baseline",
                    json.loads((upgrade.snapshot / "custody.json").read_text()),
                )
                (base / "current").unlink()
                (base / "current").symlink_to(new)
                (units / routerunit).write_text(
                    "ExecStart=python "
                    + str(base / new / "scripts/simple-chat")
                    + " wake-router serve --port 8794\n"
                )
                host.promotion = True
                if failure:
                    with self.assertRaises(UpgradeRefused):
                        upgrade.promote()
                    activated = any(
                        isinstance(event, tuple) and event[0] == "kill"
                        for event in events
                    )
                    if failure != "front-start":
                        self.assertFalse(activated)
                        upgrade.recover_stopped()
                    else:
                        self.assertTrue(activated)
                        with self.assertRaisesRegex(
                            UpgradeRefused, "post-activation recovery"
                        ):
                            upgrade.recover_stopped()
                    self.assertTrue(
                        (upgrade.snapshot / "recovery-census.json").is_file()
                    )
                    return
                upgrade.promote()
                self.assertNotIn("--start-quiesced", (units / routerunit).read_text())
                self.assertLess(
                    events.index(("stop", frontunit)),
                    events.index(("stop", routerunit)),
                )
                self.assertLess(
                    events.index(("start", routerunit)),
                    events.index(
                        ("kill", "--kill-whom=main", "--signal=USR1", routerunit)
                    ),
                )
                self.assertLess(
                    events.index(
                        ("kill", "--kill-whom=main", "--signal=USR1", routerunit)
                    ),
                    events.index(("start", frontunit)),
                )
                with self.assertRaisesRegex(UpgradeRefused, "post-activation recovery"):
                    upgrade.recover_stopped()
                self.assertEqual(
                    [
                        event
                        for event in events
                        if isinstance(event, tuple) and event[0] == "stop"
                    ][-2:],
                    [("stop", frontunit), ("stop", routerunit)],
                )

    def test_actual_preflight_quiesce_promote_orders_no_effect_start_then_activation(
        self,
    ):
        self.run_flow()

    def test_injected_reload_failure_stops_without_activation(self):
        self.run_flow("reload")

    def test_injected_router_start_failure_stops_without_activation(self):
        self.run_flow("router-start")

    def test_injected_front_start_failure_preserves_postactivation_census(self):
        self.run_flow("front-start")


class CandidateStartup(unittest.TestCase):
    def run_case(self, mutation=False):
        import subprocess
        import sys

        code = """import os,sys,json,re
from unittest import mock
from types import SimpleNamespace
from simple_tools import router_upgrade as U,wake_router as WR,wake_service as S,wake_settings as WS
platform=WR.Platform('CLAUDE','claude','@claude',re.compile('.*'),lambda *a:(_ for _ in ()).throw(AssertionError('delivery')))
r=WR.Router(platform,{'SIMPLE_TOOLS_OPERATOR_REGISTRY_URL':'https://fixture.invalid','SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY':'fixture'}, {},__import__('pathlib').Path('/must-not-write'),lambda:('r'*48,''),review_key=lambda:('s'*48,''))
seats=WR.Seats([r])
def build(env):
 if os.environ.get('MUTATION')=='1':open('/tmp/forbidden-router-upgrade-fixture','w').write('bad')
 return SimpleNamespace(seats=seats)
with mock.patch.object(WS,'preflight'),mock.patch.object(S,'build',side_effect=build),mock.patch.object(WR.Router,'pick',return_value=WR.Pick(reason='required-session-missing')):
 result=U.qualify_candidate(dict(os.environ));print(json.dumps(result))
"""
        env = dict(__import__("os").environ, MUTATION="1" if mutation else "0")
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_actual_qualification_http_canary_no_workers_no_delivery(self):
        result = self.run_case()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["qualified"])

    def test_actual_build_cannot_write_real_state(self):
        result = self.run_case(True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate attempted write outside isolated state", result.stderr)


class RealActivation(unittest.TestCase):
    def test_actual_serve_stays_quiesced_and_duplicate_signals_start_once(self):
        import signal
        import socket
        import subprocess
        import sys
        import time
        import urllib.request

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        code = """import sys,re
from simple_tools import wake_router as W
p=W.Platform('CLAUDE','claude','@claude',re.compile('.*'),lambda *a:None)
r=W.Router(p,{}, {},__import__('pathlib').Path('/not-used'),lambda:('r'*48,''))
s=W.Seats([r]);count=[]
def start():count.append(1)
s.start=start
s.health=lambda:(200,{'healthy':True,'seats':{},'upgrade_quiesced':getattr(s,'upgrade_quiesced',True),'starts':len(count)})
s.stop=lambda:True
raise SystemExit(W.serve(s,'127.0.0.1',int(sys.argv[1]),start_quiesced=True))
"""
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:

            def read():
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2
                ) as reply:
                    return json.load(reply)

            for _ in range(50):
                try:
                    answer = read()
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertTrue(answer["upgrade_quiesced"])
            self.assertEqual(answer["starts"], 0)
            child.send_signal(signal.SIGUSR1)
            child.send_signal(signal.SIGUSR1)
            for _ in range(50):
                answer = read()
                if not answer["upgrade_quiesced"]:
                    break
                time.sleep(0.02)
            self.assertFalse(answer["upgrade_quiesced"])
            self.assertEqual(answer["starts"], 1)
            child.send_signal(signal.SIGUSR1)
            time.sleep(0.05)
            self.assertEqual(read()["starts"], 1)
        finally:
            child.terminate()
            _out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err)


class SlowActivation(unittest.TestCase):
    def test_term_after_usr1_waits_for_slow_replay_then_stops_no_late_workers(self):
        import signal
        import socket
        import subprocess
        import sys

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        code = """import sys,re,time
from simple_tools import wake_router as W
p=W.Platform('CLAUDE','claude','@claude',re.compile('.*'),lambda *a:None)
r=W.Router(p,{}, {},__import__('pathlib').Path('/not-used'),lambda:('r'*48,''))
s=W.Seats([r])
def start():
 print('activation-start',flush=True);time.sleep(0.6);print('activation-end',flush=True)
def stop():print('stopped',flush=True);return True
s.start=start;s.stop=stop;s.health=lambda:(200,{'healthy':True})
raise SystemExit(W.serve(s,'127.0.0.1',int(sys.argv[1]),start_quiesced=True))
"""
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIn("listening", child.stdout.readline())
            child.send_signal(signal.SIGUSR1)
            self.assertEqual(child.stdout.readline().strip(), "activation-start")
            child.send_signal(signal.SIGTERM)
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(out.splitlines(), ["activation-end", "stopped"])
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


class InstallerRecovery(unittest.TestCase):
    def test_actual_installer_catches_quiesce_and_promotion_failures_once(self):

        for failure in (
            "snapshot",
            "front-stop",
            "router-stop",
            "stopped-census",
            "new-unknown",
            "new-delivery",
        ):
            with self.subTest(failure=failure):
                self.run_installer_failure(failure)

    def run_installer_failure(self, failure):
        import contextlib
        import io
        import os
        from unittest import mock

        from simple_tools import router_upgrade as U
        from simple_tools.tests.test_wake_router_install import (
            INSTALL,
            TAILNET,
            Fixture,
            program,
        )

        f = Fixture(self)
        first = f.install("--start")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = f.root / "custody-state"
        state.mkdir()
        (state / "done").mkdir()
        (state / "pending").mkdir()
        events = []
        real_census = U.census
        real_write = Path.write_bytes
        phase = {"stops": 0, "failed": False}

        class Candidate(U.ControlledUpgrade):
            def __init__(
                self,
                host,
                base,
                unit_dir,
                proc_root,
                scripts,
                interpreter,
                port,
            ):
                self.host, self.base, self.unit_dir, self.proc_root = (
                    host,
                    base,
                    unit_dir,
                    proc_root,
                )
                self.front, self.router = (
                    "afc-wake-tls.service",
                    "afc-wake-router.service",
                )
                self.current = os.readlink(base / "current")
                self.old_manifest = U.tree_manifest(base / self.current / "scripts")
                self.candidate_manifest = U.tree_manifest(scripts)
                self.units = {
                    name: (unit_dir / name).read_bytes()
                    for name in (self.front, self.router)
                }
                self.roots = {"CLAUDE": str(state)}
                self.before = real_census(self.roots)
                self.qualified = {"qualified": True}
                self.snapshot = base / "upgrade-fixture-snapshot"

            def recover_stopped(self):
                events.append("recovery")
                return super().recover_stopped()

            def promote(self):
                self.activation_requested = True
                row = {
                    "wake_id": "one",
                    "status": "unknown" if failure == "new-unknown" else "completed",
                    "delivered": None if failure == "new-unknown" else True,
                }
                (state / "done/one.json").write_text(json.dumps(row))
                raise U.UpgradeRefused("injected original promotion failure")

        def stop(host, unit, proc):
            phase["stops"] += 1
            if not phase["failed"] and (
                (failure == "front-stop" and unit == "afc-wake-tls.service")
                or (failure == "router-stop" and unit == "afc-wake-router.service")
            ):
                phase["failed"] = True
                raise U.UpgradeRefused("injected original stop failure")

        def census_fault(roots):
            if (
                failure == "stopped-census"
                and phase["stops"] == 2
                and not phase["failed"]
            ):
                phase["failed"] = True
                raise U.UpgradeRefused("injected original stopped census failure")
            return real_census(roots)

        def write_fault(path, data):
            if (
                failure == "snapshot"
                and path.parent.name == "upgrade-fixture-snapshot"
                and not phase["failed"]
            ):
                phase["failed"] = True
                raise OSError("injected original snapshot failure")
            return real_write(path, data)

        ns = program(INSTALL)
        output = io.StringIO()
        with (
            mock.patch.object(U, "ControlledUpgrade", Candidate),
            mock.patch.object(U, "stopped", stop),
            mock.patch.object(U, "census", census_fault),
            mock.patch.object(Path, "write_bytes", write_fault),
            contextlib.redirect_stdout(output),
        ):
            result = ns["main"](
                [
                    "--repo",
                    str(f.src),
                    "--tailnet-addr",
                    TAILNET,
                    "--controlled-upgrade",
                    "--start",
                ],
                {
                    "PATH": os.environ["PATH"],
                    "WAKE_ROUTER_TEST_ROOT": str(f.root),
                },
                os.getuid(),
            )
        self.assertEqual(result, 1, output.getvalue())
        self.assertEqual(events, ["recovery"], output.getvalue())
        self.assertIn("recovery outcome:", output.getvalue())
        if failure in ("new-unknown", "new-delivery"):
            recovered = json.loads(
                (f.base / "upgrade-fixture-snapshot/recovery-census.json").read_text()
            )
            self.assertEqual(recovered["phase"], "post-activation")
            self.assertTrue(recovered["census"]["receipts"])
            self.assertIn("original promotion failure", output.getvalue())


class ImportGuard(unittest.TestCase):
    def test_bootstrap_precedes_import_and_blocks_relative_escape(self):
        import subprocess
        import sys

        from simple_tools.router_upgrade import READONLY_BOOTSTRAP

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "simple_tools"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "wake_service.py").write_text(
                "open('../escaped','w').write('bad')"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    READONLY_BOOTSTRAP
                    + "\nimport sys;sys.path.insert(0,sys.argv[1]);from simple_tools import wake_service",
                    str(root),
                ],
                cwd=package,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "escaped").exists())
            self.assertIn("candidate attempted write", result.stderr)
