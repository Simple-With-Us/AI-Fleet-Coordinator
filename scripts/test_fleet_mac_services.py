"""Tests for fleet-mac-services.py.  launchctl is never run:  a fake runner answers with fixtures."""

import importlib.util
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("fms", os.path.join(HERE, "fleet-mac-services.py"))
fms = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fms)

# Shaped like `launchctl print gui/501/<label>` on macOS 14 and 15, trimmed.
RUNNING = """gui/501/com.jay.fleet-tools-doctor = {
\tactive count = 1
\tpath = /Users/example/Library/LaunchAgents/com.jay.fleet-tools-doctor.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /usr/bin/python3
\targuments = {
\t\t/usr/bin/python3
\t\t-I
\t}

\tdefault environment = {
\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin
\t}

\tenvironment = {
\t\tpid = 999
\t\tstate = waiting
\t}

\tdomain = gui/501 [100005]
\tminimum runtime = 10
\texit timeout = 5
\truns = 42
\tpid = 4242
\timmediate reason = interval
\tforks = 0
\texecs = 1
\tinitialized = 1
\ttrampolined = 1
\tstarted suspended = 0
\tproxy started suspended = 0
\tlast exit code = 0
}
"""

WAITING_FAILED = """gui/501/com.jay.mac-cleanup = {
\tactive count = 0
\tpath = /Users/example/Library/LaunchAgents/com.jay.mac-cleanup.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /bin/zsh
\truns = 3
\tlast exit code = 78: EX_CONFIG
}
"""

NEVER_EXITED = """gui/501/com.jay.mac-process-watch = {
\tstate = waiting
\truns = 0
\tlast exit code = (never exited)
}
"""

NOT_FOUND_STDERR = (
    'Bad request.\nCould not find service "com.jay.grok-idle-unload" in domain for user gui: 501\n'
)


class FakeRunner:
    """Answers `launchctl print gui/<uid>/<label>` from a {label: stdout} map;  others exit 113."""

    def __init__(self, answers, raise_error=None, slow=()):
        self.answers = answers
        self.raise_error = raise_error
        self.slow = set(slow)
        self.calls = []
        self.timeouts = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.timeouts.append(kwargs.get("timeout"))
        if self.raise_error is not None:
            raise self.raise_error
        label = argv[2].rsplit("/", 1)[-1]
        if label in self.slow:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if label in self.answers:
            return subprocess.CompletedProcess(argv, 0, self.answers[label], "")
        return subprocess.CompletedProcess(argv, 113, "", NOT_FOUND_STDERR)


def write_plist(directory, name, label):
    with open(os.path.join(directory, name), "wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": ["/usr/bin/true"]}, handle)


class ParsePrint(unittest.TestCase):
    def test_running_reads_top_level_pid_and_exit_not_nested(self):
        self.assertEqual(
            fms.parse_print(RUNNING), {"state": "running", "pid": 4242, "lastExitStatus": 0}
        )

    def test_exit_code_with_a_reason_keeps_the_number_and_no_pid(self):
        self.assertEqual(
            fms.parse_print(WAITING_FAILED),
            {"state": "not running", "pid": None, "lastExitStatus": 78},
        )

    def test_never_exited_is_null(self):
        self.assertIsNone(fms.parse_print(NEVER_EXITED)["lastExitStatus"])

    def test_missing_lines_and_empty_output_read_as_null(self):
        for text in ("", None, "garbage\nno = tabs here\n", "x = {\n\t\tstate = running\n}\n"):
            self.assertEqual(
                fms.parse_print(text), {"state": None, "pid": None, "lastExitStatus": None}
            )

    def test_negative_exit_code(self):
        self.assertEqual(fms.parse_print("\tlast exit code = -9\n")["lastExitStatus"], -9)

    def test_pid_without_state_is_kept_but_not_zero(self):
        self.assertEqual(fms.parse_print("\tpid = 77\n")["pid"], 77)
        self.assertIsNone(fms.parse_print("\tstate = running\n\tpid = 0\n")["pid"])


class Document(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.launchd = os.path.join(root, "launchd")
        self.agents = os.path.join(root, "LaunchAgents")
        os.makedirs(self.launchd)
        os.makedirs(self.agents)
        write_plist(self.launchd, "com.jay.fleet-tools-doctor.plist", "com.jay.fleet-tools-doctor")
        write_plist(self.launchd, "com.jay.mac-cleanup.plist", "com.jay.mac-cleanup")
        with open(os.path.join(self.launchd, "com.jay.broken.plist"), "w") as handle:
            handle.write("not a plist")
        with open(os.path.join(self.launchd, "com.jay.dashes.plist"), "w") as handle:
            handle.write(
                '<?xml version="1.0"?>\n<!-- a -- b -->\n<plist version="1.0"><dict>'
                "<key>Label</key><string>com.jay.dashes</string></dict></plist>\n"
            )
        with open(os.path.join(self.launchd, "README.md"), "w") as handle:
            handle.write("ignored")
        write_plist(self.agents, "com.jay.fleet-tools-doctor.plist", "com.jay.fleet-tools-doctor")
        self.control = os.path.join(root, "fleet-ops-control.json")
        with open(self.control, "w") as handle:
            json.dump(
                {
                    "schemaVersion": 1,
                    "mac": {
                        "agents": [
                            {
                                "label": "com.jay.fleet-tools-doctor",
                                "actions": ["start", "stop", "restart"],
                            },
                            {"label": "com.jay.extra-agent", "actions": ["restart"]},
                            {"label": "com.jay.no-actions", "actions": []},
                            {"label": "../escape", "actions": ["start"]},
                            {"label": "com.jay.bad-actions", "actions": ["rm -rf"]},
                            "not a dict",
                        ]
                    },
                },
                handle,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, runner, control=None):
        return fms.document(
            self.launchd,
            self.control if control is None else control,
            self.agents,
            uid=501,
            runner=runner,
            generated_at=0,
        )

    def test_full_document_matches_the_contract(self):
        runner = FakeRunner(
            {"com.jay.fleet-tools-doctor": RUNNING, "com.jay.mac-cleanup": WAITING_FAILED}
        )
        doc = self.build(runner)
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["tool"], "fleet-mac-services")
        self.assertEqual(doc["host"], "mac")
        self.assertEqual(doc["generatedAt"], "1970-01-01T00:00:00Z")
        self.assertNotIn("error", doc)
        by_label = {agent["label"]: agent for agent in doc["agents"]}
        self.assertEqual(
            sorted(by_label),
            [
                "com.jay.bad-actions",
                "com.jay.broken",
                "com.jay.dashes",
                "com.jay.extra-agent",
                "com.jay.fleet-tools-doctor",
                "com.jay.mac-cleanup",
                "com.jay.no-actions",
            ],
        )
        self.assertEqual(
            by_label["com.jay.fleet-tools-doctor"],
            {
                "label": "com.jay.fleet-tools-doctor",
                "plistInRepo": True,
                "installed": True,
                "loaded": True,
                "pid": 4242,
                "lastExitStatus": 0,
                "controllable": True,
            },
        )
        cleanup = by_label["com.jay.mac-cleanup"]
        self.assertEqual(
            (cleanup["installed"], cleanup["loaded"], cleanup["pid"], cleanup["lastExitStatus"]),
            (False, True, None, 78),
        )
        self.assertFalse(cleanup["controllable"])
        extra = by_label["com.jay.extra-agent"]
        self.assertEqual(
            (extra["plistInRepo"], extra["loaded"], extra["controllable"]), (False, False, True)
        )
        self.assertFalse(by_label["com.jay.no-actions"]["controllable"])
        self.assertFalse(by_label["com.jay.bad-actions"]["controllable"])
        self.assertTrue(by_label["com.jay.broken"]["plistInRepo"])
        self.assertTrue(by_label["com.jay.dashes"]["plistInRepo"])
        for agent in doc["agents"]:
            self.assertEqual(
                set(agent),
                {
                    "label",
                    "plistInRepo",
                    "installed",
                    "loaded",
                    "pid",
                    "lastExitStatus",
                    "controllable",
                },
            )
        self.assertIn(["launchctl", "print", "gui/501/com.jay.fleet-tools-doctor"], runner.calls)

    def test_missing_or_malformed_control_file_is_tolerated(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as handle:
            handle.write("{not json")
        for control in (os.path.join(self.tmp.name, "absent.json"), bad):
            doc = self.build(FakeRunner({}), control)
            self.assertEqual(
                [a["label"] for a in doc["agents"]],
                [
                    "com.jay.broken",
                    "com.jay.dashes",
                    "com.jay.fleet-tools-doctor",
                    "com.jay.mac-cleanup",
                ],
            )
            self.assertFalse(any(a["controllable"] for a in doc["agents"]))
        with open(bad, "w") as handle:
            json.dump({"mac": {"agents": "nope"}}, handle)
        self.assertEqual(len(self.build(FakeRunner({}), bad)["agents"]), 4)

    def test_launchctl_that_cannot_run_marks_the_document_and_stops(self):
        runner = FakeRunner({}, raise_error=FileNotFoundError("launchctl"))
        doc = self.build(runner)
        self.assertEqual(doc["error"], "launchctl_unavailable")
        self.assertFalse(any(a["loaded"] for a in doc["agents"]))
        self.assertEqual(len(doc["agents"]), 7)
        # One failed run is enough;  the remaining labels are not tried.
        self.assertEqual(len(runner.calls), 1)

    def test_one_slow_label_marks_only_that_agent(self):
        runner = FakeRunner({"com.jay.fleet-tools-doctor": RUNNING}, slow={"com.jay.mac-cleanup"})
        doc = self.build(runner)
        self.assertNotIn("error", doc)
        by_label = {a["label"]: a for a in doc["agents"]}
        slow = by_label["com.jay.mac-cleanup"]
        self.assertEqual(slow["error"], "timeout")
        self.assertFalse(slow["loaded"])
        self.assertIsNone(slow["pid"])
        others = [a for label, a in by_label.items() if label != "com.jay.mac-cleanup"]
        self.assertTrue(all("error" not in a for a in others))
        self.assertTrue(by_label["com.jay.fleet-tools-doctor"]["loaded"])
        self.assertEqual(len(runner.calls), len(doc["agents"]))

    def test_every_call_uses_the_short_timeout(self):
        runner = FakeRunner({})
        self.build(runner)
        self.assertTrue(runner.timeouts)
        self.assertTrue(all(t == fms.LAUNCHCTL_TIMEOUT for t in runner.timeouts))
        self.assertLessEqual(fms.LAUNCHCTL_TIMEOUT, 5)

    def test_missing_launchd_dir_gives_only_allowlisted_agents(self):
        doc = fms.document(
            os.path.join(self.tmp.name, "nope"),
            self.control,
            self.agents,
            uid=501,
            runner=FakeRunner({}),
        )
        self.assertTrue(all(not a["plistInRepo"] for a in doc["agents"]))
        self.assertEqual(len(doc["agents"]), 4)


class Writing(unittest.TestCase):
    def test_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "state", "mac-services.json")
            fms.write_atomic(path, {"tool": "fleet-mac-services"})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as handle:
                self.assertEqual(json.load(handle), {"tool": "fleet-mac-services"})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["mac-services.json"])

    def test_refuses_a_symlinked_state_directory(self):
        with tempfile.TemporaryDirectory() as root:
            real = os.path.join(root, "real")
            os.makedirs(real)
            link = os.path.join(root, "link")
            os.symlink(real, link)
            with self.assertRaises(OSError):
                fms.write_atomic(os.path.join(link, "mac-services.json"), {})

    def test_dry_run_prints_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "out.json")
            launchd = os.path.join(root, "launchd")
            os.makedirs(launchd)
            write_plist(launchd, "com.jay.a.plist", "com.jay.a")
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = fms.main(
                    [
                        "--dry-run",
                        "--write",
                        path,
                        "--launchd-dir",
                        launchd,
                        "--control",
                        os.path.join(root, "absent.json"),
                        "--agents-dir",
                        root,
                    ],
                    runner=FakeRunner({"com.jay.a": RUNNING}),
                )
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(path))
            self.assertEqual(json.loads(buf.getvalue())["agents"][0]["pid"], 4242)

    def test_main_writes_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "state", "out.json")
            code = fms.main(
                [
                    "--write",
                    path,
                    "--launchd-dir",
                    root,
                    "--control",
                    os.path.join(root, "x.json"),
                    "--agents-dir",
                    root,
                ],
                runner=FakeRunner({}),
            )
            self.assertEqual(code, 0)
            with open(path) as handle:
                self.assertEqual(json.load(handle)["agents"], [])


class RepoTemplates(unittest.TestCase):
    """Every tracked plist parses, its Label matches its file name, and the two new agents exist."""

    # Its header comment holds "--", which XML forbids inside a comment;  the collector still
    # reads its label from the file name.  Drop it from this set once the comment is fixed.
    KNOWN_MALFORMED = {"com.jays.weekly-fleet-mode-digest.plist"}

    def test_every_launchd_template_is_well_formed(self):
        launchd = os.path.join(HERE, "launchd")
        names = [n for n in os.listdir(launchd) if n.endswith(".plist")]
        self.assertTrue(names)
        self.assertEqual(
            fms.repo_labels(launchd),
            {n[: -len(".plist")] for n in names if fms.valid_label(n[: -len(".plist")])},
        )
        for name in names:
            if name in self.KNOWN_MALFORMED:
                continue
            with open(os.path.join(launchd, name), "rb") as handle:
                data = plistlib.load(handle)
            self.assertEqual(data.get("Label"), name[: -len(".plist")], name)
            self.assertTrue(fms.valid_label(data["Label"]), name)

    def test_new_agents_run_the_managed_runtime_copy(self):
        launchd = os.path.join(HERE, "launchd")
        for label, script, interval in (
            ("com.jay.fleet-worker-health", "fleet-worker-health.py", 900),
            ("com.jay.fleet-mac-services", "fleet-mac-services.py", 300),
        ):
            with open(os.path.join(launchd, label + ".plist"), "rb") as handle:
                data = plistlib.load(handle)
            args = data["ProgramArguments"]
            self.assertEqual(args[:2], ["/usr/bin/python3", "-I"])
            self.assertTrue(args[2].endswith("/agent-sync-runtime/scripts/" + script), args[2])
            self.assertTrue(os.path.isfile(os.path.join(HERE, script)))
            self.assertEqual(data["StartInterval"], interval)
            self.assertTrue(data["RunAtLoad"])
            self.assertIn("--write", args)
            self.assertTrue("/.local/state/fleet-tools/" in args[args.index("--write") + 1])


if __name__ == "__main__":
    unittest.main()
