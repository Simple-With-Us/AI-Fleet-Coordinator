"""Tests for scripts/fleet_app_processes.py and its two lines in the doctor (AFC-576).

  python3 scripts/test_fleet_app_processes.py

Fakes only:  `ps` and `launchctl` answer from fixtures, `/proc` is a temporary folder and
pm2's pid files live in a temporary HOME.  Nothing reads this machine's processes.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location(
    "fleet_app_processes", HERE / "fleet_app_processes.py"
)
fap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fap)

import test_fleet_tools_doctor as doctor_tests

doctor = doctor_tests.doctor

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
APPS = [
    {"acronym": "BF", "displayName": "BotFleet.app", "slug": "botfleet"},
    {"acronym": "AFC", "displayName": "AI Fleet Coordinator", "slug": "aifleetcoordinator"},
    {"acronym": "C", "displayName": "Clutch", "slug": "clutch"},
    {"acronym": "HH", "displayName": "Hog Hunter", "slug": "hoghunter"},
    {"acronym": "FL", "displayName": "FleetLink.online", "slug": "fleetlink"},
    {"acronym": "FLL", "displayName": "FleetLink Legacy", "slug": "fleetlinklegacy"},
]

# `ps -A -o pid= -o ppid= -o stat= -o etime= -o ucomm=` on the Mac.
MAC_PS = """\
    1     0 Ss   10-00:00:00 launchd
  501     1 Ss      02:00:00 node
  502   501 S       01:59:00 node
  503     1 S          05:00 BotFleet
  504     1 Ss         30:00 node
  505     1 S          30:00 node
  506     1 S          30:00 node
  507     1 R          00:05 python3
  508   507 R          00:01 ps
  509     1 Z          00:01 Google Chrome He
  510     1 T     1-02:03:04 HogHunter
  511     1 S          00:30 fleetlink-legacy
"""
MAC_LAUNCHCTL = (
    "PID\tStatus\tLabel\n"
    "501\t0\tapp.botfleet.server\n"
    "-\t0\tapp.botfleet.companion\n"
    "507\t0\tcom.jay.fleet-tools-doctor\n"
    "garbage line\n"
)
# The same shape on the server, with `comm=`.
LINUX_PS = """\
    1     0 Ss   10-00:00:00 systemd
  801     1 Ss      05:00:00 python3
  802   801 S       04:00:00 python3
  803     1 Ssl     03:00:00 botfleet-worke
  804     1 Ssl     03:00:00 node
  805     1 Sl      03:00:00 dockerd
  806     1 S          00:10 sshd
  807     1 S          01:00 node
  808     1 D          01:00 python3
"""
# What `ps` would print if anyone asked it for the argv, which this module never does.
ARGV_PS = f"  506     1 S 30:00 node /srv/grok-acp/server.js --token={SECRET}\n"


def fake_runner(answers, calls):
    """A subprocess.run stand-in.  `answers` maps an argv word to (returncode, stdout).  A request
    for the argv (`args`, `command`, `-E`, `e`) gets the secret back, so a leak would show."""

    def runner(argv, **kwargs):
        calls.append(list(argv))
        joined = " ".join(argv)
        if any(word in joined for word in ("args", "command", "cmd")):
            return subprocess.CompletedProcess(argv, 0, ARGV_PS, "")
        for word, (code, out) in answers.items():
            if word in argv:
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 1, "", "no rule")

    return runner


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.proc = root / "proc"
        self.home.mkdir()
        self.proc.mkdir()
        self.calls: list = []

    def tearDown(self):
        self.tmp.cleanup()

    def pm2(self, files):
        folder = self.home / ".pm2" / "pids"
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (folder / name).write_text(text)

    def process(self, pid, exe=None, cgroup=None):
        """A fake /proc/<pid>, with the argv and environment a careless reader would leak."""
        folder = self.proc / str(pid)
        folder.mkdir()
        if exe:
            os.symlink(exe, folder / "exe")
        if cgroup:
            (folder / "cgroup").write_text(cgroup)
        (folder / "cmdline").write_text(f"node\0--token={SECRET}\0")
        (folder / "environ").write_text(f"GITHUB_TOKEN={SECRET}\0")

    def mac_scan(self, answers=None, **kwargs):
        answers = answers or {fap.PS: (0, MAC_PS), fap.LAUNCHCTL: (0, MAC_LAUNCHCTL)}
        return fap.scan_host(
            "mac",
            runner=fake_runner(answers, self.calls),
            home=self.home,
            proc=self.proc,
            apps=APPS,
            clock=lambda: NOW,
            own_pid=507,
            **kwargs,
        )

    def linux_scan(self, answers=None):
        answers = answers or {fap.PS: (0, LINUX_PS)}
        return fap.scan_host(
            "server",
            runner=fake_runner(answers, self.calls),
            home=self.home,
            proc=self.proc,
            apps=APPS,
            clock=lambda: NOW,
            own_pid=4242,
        )

    def linux_fixture(self):
        self.process(1, cgroup="0::/init.scope\n")
        self.process(
            801, exe="/usr/bin/python3.12", cgroup="0::/system.slice/fleet-coding-memory.service\n"
        )
        self.process(802, cgroup="0::/system.slice/fleet-coding-memory.service\n")
        self.process(
            803,
            exe="/opt/botfleet/bin/botfleet-worker (deleted)",
            cgroup="0::/system.slice/cron.service\n",
        )
        self.process(804, cgroup="0::/system.slice/docker-" + "a" * 64 + ".scope\n")
        self.process(805, exe="/usr/bin/dockerd", cgroup="0::/system.slice/docker.service\n")
        self.process(
            807,
            exe="/usr/bin/node",
            cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/fleet-server-status.service\n",
        )
        self.process(808, cgroup="0::/system.slice/fleet-health-recover@disk.service\n")


def by_pid(section):
    return {row["pid"]: row for row in section["processes"]}


class MacScanTests(Fixture):
    def setUp(self):
        super().setUp()
        self.pm2(
            {
                "botfleet-mcp-0.pid": "504\n",
                "clutch-web-3.pid": "505",
                "grok-acp-5.pid": "506",
                "gone-app-1.pid": "99999",  # a stale pid file names no listed process
                "not-a-pid-file.txt": "505",
            }
        )

    def test_each_identity_names_its_app(self):
        section = self.mac_scan()
        self.assertTrue(section["reachable"])
        self.assertTrue(section["complete"])
        self.assertEqual(
            (section["tool"], section["schemaVersion"], section["host"]), (fap.TOOL, 1, "mac")
        )
        rows = by_pid(section)
        self.assertEqual(rows[501]["app"], "BF")
        self.assertEqual(rows[501]["launchdLabel"], "app.botfleet.server")
        self.assertEqual(rows[501]["matchedBy"], "launchd label")
        self.assertEqual((rows[502]["app"], rows[502]["matchedBy"]), ("BF", "parent process"))
        self.assertEqual((rows[503]["app"], rows[503]["matchedBy"]), ("BF", "executable"))
        self.assertEqual((rows[504]["app"], rows[504]["pm2Name"]), ("BF", "botfleet-mcp"))
        self.assertEqual((rows[505]["app"], rows[505]["pm2Name"]), ("C", "clutch-web"))
        self.assertEqual((rows[507]["app"], rows[507]["matchedBy"]), ("AFC", "launchd label"))
        self.assertEqual((rows[510]["app"], rows[510]["state"]), ("HH", "stopped"))
        # The longer name wins:  fleetlink-legacy is FLL, not FleetLink.
        self.assertEqual(rows[511]["app"], "FLL")

    def test_a_process_that_matches_no_app_is_not_written_down(self):
        rows = by_pid(self.mac_scan())
        for pid in (1, 506, 509):  # launchd, an unattributed pm2 app, Chrome
            self.assertNotIn(pid, rows)
        # Every node process is not BotFleet:  only the ones an identity names are.
        self.assertEqual(
            sorted(p for p, r in rows.items() if r.get("executable") == "node"),
            [501, 502, 504, 505],
        )

    def test_the_scans_own_children_are_left_out(self):
        self.assertNotIn(508, by_pid(self.mac_scan()))

    def test_state_and_start_time(self):
        rows = by_pid(self.mac_scan())
        self.assertEqual(rows[501]["state"], "sleeping")
        self.assertEqual(rows[507]["state"], "running")
        self.assertEqual(rows[501]["startedAt"], fap.utc(NOW - 2 * 3600))
        self.assertEqual(rows[510]["startedAt"], fap.utc(NOW - (26 * 3600 + 3 * 60 + 4)))

    def test_the_mac_reads_no_proc_and_asks_ps_for_the_short_name(self):
        self.mac_scan()
        ps = [call for call in self.calls if call[0] == fap.PS]
        self.assertEqual(len(ps), 1)
        self.assertIn("ucomm=", ps[0])
        self.assertEqual(
            [call for call in self.calls if call[0] == fap.LAUNCHCTL], [[fap.LAUNCHCTL, "list"]]
        )

    def test_launchctl_that_does_not_answer_makes_the_read_partial(self):
        section = self.mac_scan({fap.PS: (0, MAC_PS), fap.LAUNCHCTL: (1, "")})
        self.assertTrue(section["reachable"])
        self.assertFalse(section["complete"])
        self.assertIn("launchctl", section["notes"][0])
        self.assertNotIn(501, by_pid(section))  # its label was the only thing that named it
        self.assertIn(503, by_pid(section))

    def test_ps_that_does_not_answer_is_an_unreachable_host_never_a_raise(self):
        for answer in ((1, ""),):
            section = self.mac_scan({fap.PS: answer})
            self.assertFalse(section["reachable"])
            self.assertIn("ps did not answer", section["error"])
            self.assertEqual(section["processes"], [])

        def broken(argv, **kwargs):
            raise OSError("no ps")

        section = fap.scan_host("mac", runner=broken, home=self.home, apps=APPS)
        self.assertFalse(section["reachable"])

    def test_an_unexpected_failure_is_a_section_that_says_so(self):
        with mock.patch.object(fap, "_scan", side_effect=RuntimeError("boom")):
            section = fap.scan_host(
                "mac", runner=fake_runner({}, self.calls), home=self.home, apps=APPS
            )
        self.assertFalse(section["reachable"])
        self.assertIn("RuntimeError", section["error"])

    def test_too_many_rows_are_cut_and_the_read_says_it_is_partial(self):
        lines = "".join(f"{2000 + n} 1 S 00:10 BotFleet\n" for n in range(fap.MAX_ROWS + 50))
        section = self.mac_scan({fap.PS: (0, lines), fap.LAUNCHCTL: (0, "")})
        self.assertEqual(len(section["processes"]), fap.MAX_ROWS)
        self.assertTrue(section["truncated"])
        self.assertFalse(section["complete"])
        self.assertLessEqual(fap.section_bytes(section), fap.MAX_SECTION_BYTES)


class LinuxScanTests(Fixture):
    def test_units_and_executables_name_their_apps(self):
        self.linux_fixture()
        section = self.linux_scan()
        rows = by_pid(section)
        self.assertEqual(rows[801]["app"], "AFC")
        self.assertEqual(rows[801]["systemdUnit"], "fleet-coding-memory.service")
        self.assertEqual(rows[801]["executable"], "/usr/bin/python3.12")
        self.assertEqual(rows[801]["matchedBy"], "systemd unit")
        self.assertEqual(
            rows[802]["matchedBy"], "systemd unit"
        )  # the unit names it, not its parent
        self.assertEqual(rows[802]["executable"], "python3")  # no readable exe:  the short name
        self.assertEqual(
            (rows[803]["app"], rows[803]["executable"]), ("BF", "/opt/botfleet/bin/botfleet-worker")
        )
        self.assertEqual(rows[807]["systemdUnit"], "fleet-server-status.service")
        self.assertEqual((rows[808]["app"], rows[808]["state"]), ("AFC", "waiting"))
        for pid in (1, 804, 805, 806):  # systemd, a container's node, dockerd, sshd
            self.assertNotIn(pid, rows)

    def test_the_server_asks_ps_for_comm(self):
        self.linux_fixture()
        self.linux_scan()
        ps = [call for call in self.calls if call[0] == fap.PS]
        self.assertIn("comm=", ps[0])
        self.assertFalse([call for call in self.calls if call[0] == fap.LAUNCHCTL])


class PrivacyTests(Fixture):
    """No argv and no environment, ever:  a secret in a process's argv writes none of it."""

    FORBIDDEN = ("args", "command", "cmd", "-E", "e", "-e", "jlist", "env", "environ")

    def test_a_secret_in_argv_or_environment_never_reaches_the_section(self):
        self.linux_fixture()
        self.process(506, exe="/usr/bin/node", cgroup="0::/system.slice/botfleet-api.service\n")
        ps = LINUX_PS + "  506     1 S          30:00 node\n"
        server = self.linux_scan({fap.PS: (0, ps)})
        self.assertEqual(by_pid(server)[506]["app"], "BF")  # it was scanned, by its unit
        self.pm2({"grok-acp-5.pid": "506", "botfleet-mcp-0.pid": "504"})
        mac = self.mac_scan()
        for section in (server, mac):
            self.assertNotIn(SECRET, json.dumps(section))
            self.assertNotIn("token", json.dumps(section).lower())

    def test_no_command_asks_for_argv_or_environment(self):
        self.linux_fixture()
        self.pm2({"botfleet-mcp-0.pid": "504"})
        self.linux_scan()
        self.mac_scan()
        self.assertTrue(self.calls)
        for call in self.calls:
            for word in call[1:]:
                self.assertNotIn(word, self.FORBIDDEN, call)
                self.assertFalse(word.startswith(("args", "command")), call)

    def test_the_mac_rebuilds_the_servers_rows_from_the_allowed_keys(self):
        server_scan = {
            "schemaVersion": 1,
            "tool": fap.TOOL,
            "host": "server",
            "reachable": True,
            "error": None,
            "generatedAt": fap.utc(NOW),
            "complete": True,
            "truncated": False,
            "notes": [],
            "processes": [
                {
                    "app": "BF",
                    "pid": 9,
                    "executable": "/usr/bin/node",
                    "state": "sleeping",
                    "argv": ["node", f"--token={SECRET}"],
                    "environment": {"GITHUB_TOKEN": SECRET},
                    "matchedBy": "systemd unit",
                }
            ],
        }
        section = fap.section_from(server_scan, "server")
        self.assertEqual(
            set(section["processes"][0]), {"app", "pid", "executable", "state", "matchedBy"}
        )
        self.assertNotIn(SECRET, json.dumps(section))
        for row in section["processes"]:
            self.assertLessEqual(set(row), set(fap.ROW_KEYS))


def server_scan_doc(**overrides):
    """The server's section as its doctor prints it."""
    doc = {
        "schemaVersion": 1,
        "tool": fap.TOOL,
        "host": "server",
        "reachable": True,
        "error": None,
        "generatedAt": fap.utc(NOW),
        "complete": True,
        "truncated": False,
        "notes": ["one note"],
        "processes": [
            {"app": "AFC", "pid": 7, "state": "sleeping", "startedAt": fap.utc(NOW - 60)}
        ],
    }
    doc.update(overrides)
    return doc


class ServerSectionTests(unittest.TestCase):
    def scan(self, **overrides):
        return server_scan_doc(**overrides)

    def test_a_valid_scan_is_kept_and_taken_out_of_the_servers_half(self):
        server = {"reachable": True, "error": None, "appProcesses": self.scan()}
        section = fap.server_section(server)
        self.assertNotIn("appProcesses", server)
        self.assertTrue(section["reachable"])
        self.assertTrue(section["complete"])
        self.assertEqual(section["processes"][0]["pid"], 7)
        self.assertEqual(section["notes"], ["one note"])

    def test_every_way_of_not_knowing_is_an_unreachable_section(self):
        cases = (
            (None, "no --remote given"),
            (
                {
                    "reachable": False,
                    "error": "ssh fleet-tools-ro exited 255",
                    "appProcesses": self.scan(),
                },
                "exited 255",
            ),
            ({"reachable": True}, "does not carry a process scan yet"),
            ({"reachable": True, "appProcesses": "text"}, "no process scan this doctor reads"),
            ({"reachable": True, "appProcesses": self.scan(tool="other")}, "no process scan"),
            ({"reachable": True, "appProcesses": self.scan(schemaVersion=2)}, "version 2"),
            ({"reachable": True, "appProcesses": self.scan(host="mac")}, "not the server"),
            (
                {"reachable": True, "appProcesses": self.scan(generatedAt="yesterday")},
                "carries no time",
            ),
            ({"reachable": True, "appProcesses": self.scan(processes="none")}, "no process list"),
        )
        for server, why in cases:
            section = fap.server_section(server)
            self.assertFalse(section["reachable"], why)
            self.assertIn(why, section["error"])
            self.assertEqual(section["processes"], [])
            if isinstance(server, dict):
                self.assertNotIn("appProcesses", server)

    def test_a_server_that_could_not_scan_itself_keeps_its_reason_and_no_rows(self):
        section = fap.section_from(self.scan(reachable=False, error="ps did not answer"), "server")
        self.assertFalse(section["reachable"])
        self.assertEqual(section["error"], "ps did not answer")
        self.assertEqual(section["processes"], [])

    def test_a_partial_or_malformed_row_makes_the_section_partial(self):
        section = fap.section_from(
            self.scan(processes=[{"app": "AFC", "pid": 7}, {"pid": "x"}]), "server"
        )
        self.assertTrue(section["reachable"])
        self.assertFalse(section["complete"])
        self.assertEqual(len(section["processes"]), 1)
        self.assertEqual(section["processes"][0]["state"], "unknown")
        self.assertFalse(fap.section_from(self.scan(truncated=True), "server")["complete"])
        self.assertFalse(fap.section_from(self.scan(complete=False), "server")["complete"])


class KeepTests(Fixture):
    def mac_section(self):
        return self.mac_scan()

    def test_the_report_has_both_hosts_and_the_pinned_contract(self):
        written = {}
        server = {"reachable": True, "appProcesses": server_scan_doc()}
        ok = fap.keep(
            server,
            self.mac_section(),
            Path("/x/app-processes.json"),
            lambda path, doc: written.update(path=path, doc=doc),
            apps=APPS,
            now=NOW,
        )
        self.assertTrue(ok)
        report = written["doc"]
        self.assertEqual((report["schemaVersion"], report["tool"]), (1, "fleet-app-processes"))
        self.assertEqual(report["generatedAt"], fap.utc(NOW))
        self.assertEqual([host["host"] for host in report["hosts"]], ["mac", "server"])
        self.assertEqual(report["apps"][0], {"acronym": "BF", "displayName": "BotFleet.app"})
        self.assertNotIn("appProcesses", server)
        for host in report["hosts"]:
            for key in ("reachable", "error", "generatedAt", "complete", "processes"):
                self.assertIn(key, host)

    def test_a_write_that_fails_never_raises(self):
        server = {"reachable": True, "appProcesses": {}}

        def refuse(path, doc):
            raise OSError("read-only")

        self.assertFalse(fap.keep(server, self.mac_section(), Path("/x"), refuse, apps=APPS))
        self.assertNotIn("appProcesses", server)


class AppsAndRulesTests(unittest.TestCase):
    def test_the_real_app_list_reads(self):
        apps = fap.load_apps()
        acronyms = [app["acronym"] for app in apps]
        self.assertIn("BF", acronyms)
        self.assertIn("AFC", acronyms)
        self.assertEqual(fap.load_apps(Path("/no/such/file.json")), [])

    def test_every_explicit_rule_names_a_fleet_app(self):
        acronyms = {app["acronym"] for app in fap.load_apps()}
        for rule in fap.EXPLICIT_RULES:
            self.assertIn(rule[0], acronyms, rule)
            self.assertIn(rule[1], fap.KINDS, rule)

    def test_every_template_this_repository_ships_is_attributed(self):
        """A new LaunchAgent or unit in this repository needs its row, or it reads as no app."""
        slugs = fap.slug_rules(fap.load_apps())
        launchd = [path.stem for path in (REPO / "scripts/launchd").glob("*.plist")]
        launchd += [path.stem for path in (REPO / "scripts/fleet_lanes/templates").glob("*.plist")]
        launchd += [path.stem for path in (REPO / "deploy").glob("com.jay.*.plist")]
        self.assertIn("com.jay.fleet-tools-doctor", launchd)
        for label in launchd:
            want = "BF" if "botfleet" in label else "AFC"
            self.assertEqual(fap.match_identity("launchd", label, slugs), want, label)
        units = []
        for folder in (
            "scripts/systemd",
            "scripts/host",
            "scripts/hetzner",
            "scripts/agent_sync/server",
        ):
            units += [
                path.name.replace("@.service", "@instance.service")
                for path in (REPO / folder).glob("*.service")
            ]
        self.assertIn("fleet-tools-doctor.service", units)
        for unit in units:
            self.assertEqual(fap.match_identity("systemd", unit, slugs), "AFC", unit)

    def test_etime_and_state_words(self):
        self.assertEqual(fap.etime_seconds("05"), None)
        self.assertEqual(fap.etime_seconds("00:05"), 5)
        self.assertEqual(fap.etime_seconds("01:00:05"), 3605)
        self.assertEqual(fap.etime_seconds("2-01:00:05"), 2 * 86400 + 3605)
        self.assertEqual(fap.state_word("Ss+"), "sleeping")
        self.assertEqual(fap.state_word("?"), "unknown")
        self.assertEqual(fap.state_word(""), "unknown")


class DoctorTests(doctor_tests.HomeCase):
    """The doctor's own part:  the server's --json carries the scan, the Mac writes the file."""

    run_doctor = doctor_tests.ReportAndRunTests.run_doctor
    CONFIGURED = (("ssh", "-G"), (0, "user root\nhostname 192.0.2.10\n"))

    def server_answer(self, scan):
        doc = {"ok": True, "checks": [], "schemaVersion": 1, "host": "server"}
        if scan is not None:
            doc["appProcesses"] = scan
        return (("BatchMode=yes",), (0, json.dumps(doc).encode()))

    def server_scan(self):
        return server_scan_doc(
            processes=[{"app": "BF", "pid": 9, "state": "sleeping", "argv": [f"--token={SECRET}"]}],
        )

    def test_the_server_json_carries_its_scan(self):
        tmp = Path(self.tmp.name) / "proc"
        tmp.mkdir()
        module = doctor.app_processes()
        self.assertIsNotNone(module)
        module.scan_host = functools.partial(module.scan_host, proc=tmp, apps=APPS)
        rules = [((fap.PS,), (0, "  900     1 S 00:10 botfleet-api\n"))]
        with mock.patch.object(doctor, "app_processes", return_value=module):
            text, _ = self.run_doctor(["--json", "--no-network", "--no-registry"], rules)
        doc = json.loads(text)
        scan = doc["appProcesses"]
        self.assertEqual(
            (scan["tool"], scan["host"], scan["reachable"]), (fap.TOOL, "server", True)
        )
        self.assertEqual(scan["processes"][0]["app"], "BF")
        self.assertEqual((doc["schemaVersion"], doc["host"]), (1, "server"))

    def test_the_mac_writes_both_hosts_beside_the_report_and_keeps_them_out_of_it(self):
        report = self.home / ".local/state/fleet-tools/report.json"
        rules = [
            self.CONFIGURED,
            self.server_answer(self.server_scan()),
            ((fap.PS,), (0, MAC_PS)),
            ((fap.LAUNCHCTL, "list"), (0, MAC_LAUNCHCTL)),
        ]
        self.run_doctor(
            ["--no-network", "--no-registry", "--remote", "fleet-tools-ro", "--write", str(report)],
            rules,
            host="mac",
        )
        combined = json.loads(report.read_text())
        self.assertNotIn("appProcesses", combined["hosts"][1])
        path = report.parent / fap.REPORT_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        written = json.loads(path.read_text())
        self.assertEqual([h["host"] for h in written["hosts"]], ["mac", "server"])
        self.assertTrue(all(h["reachable"] for h in written["hosts"]))
        self.assertIn(501, [row["pid"] for row in written["hosts"][0]["processes"]])
        self.assertEqual(
            written["hosts"][1]["processes"][0], {"app": "BF", "pid": 9, "state": "sleeping"}
        )
        self.assertNotIn(SECRET, path.read_text())
        self.assertNotIn(SECRET, report.read_text())

    def test_printing_the_report_writes_no_scan_and_strips_the_servers(self):
        rules = [self.CONFIGURED, self.server_answer(self.server_scan())]
        text, _ = self.run_doctor(
            ["--no-network", "--no-registry", "--remote", "fleet-tools-ro", "--write", "-"],
            rules,
            host="mac",
        )
        self.assertNotIn("appProcesses", json.loads(text)["hosts"][1])
        self.assertFalse((self.home / ".local/state/fleet-tools" / fap.REPORT_NAME).exists())

    def test_a_scan_module_that_cannot_load_never_fails_the_doctor(self):
        report = self.home / ".local/state/fleet-tools/report.json"
        rules = [self.CONFIGURED, self.server_answer(self.server_scan())]
        with mock.patch.object(doctor, "app_processes", return_value=None):
            self.run_doctor(
                [
                    "--no-network",
                    "--no-registry",
                    "--remote",
                    "fleet-tools-ro",
                    "--write",
                    str(report),
                ],
                rules,
                host="mac",
            )
            text, _ = self.run_doctor(["--json", "--no-network", "--no-registry"])
        self.assertNotIn("appProcesses", json.loads(report.read_text())["hosts"][1])
        self.assertNotIn("appProcesses", json.loads(text))
        self.assertFalse((report.parent / fap.REPORT_NAME).exists())


if __name__ == "__main__":
    unittest.main()
