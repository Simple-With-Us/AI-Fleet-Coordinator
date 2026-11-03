"""Tests for scripts/fleet-vm-status.py:  python3 scripts/test_fleet_vm_status.py

The rules under test mirror the server collector:  a measurement that cannot be
taken is omitted and never reported as a zero,  only allowlisted executable
names appear,  no session content and no argv or environment is read,  and the
report is written atomically and privately.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fleet_vm_status", HERE / "fleet-vm-status.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def make_proc(root, pid, comm, start_ticks=1000):
    folder = root / str(pid)
    folder.mkdir(parents=True)
    rest = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 5
    (folder / "stat").write_text("%d (%s) %s\n" % (pid, comm, " ".join(rest)))
    (folder / "comm").write_text(comm + "\n")
    executable = root / (comm + ".bin")
    if not executable.exists():
        executable.write_text("#!/bin/sh\n")
    (folder / "exe").symlink_to(executable)


def make_host_proc(root):
    (root / "loadavg").write_text("1.50 0.50 0.25 1/100 999\n")
    (root / "uptime").write_text("3600.5 7200.0\n")
    (root / "meminfo").write_text(
        "MemTotal:       1024000 kB\nMemAvailable:    512000 kB\n"
        "SwapTotal:       204800 kB\nSwapFree:        102400 kB\n"
    )
    (root / "stat").write_text("btime 1700000000\n")


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


class OmissionTests(unittest.TestCase):
    """A measurement that cannot be taken is omitted, never a zero."""

    def test_missing_proc_files_omit_host_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            metrics = collector.collect_host_metrics(
                proc=proc,
                cpu_count=lambda: None,
                getloadavg=lambda: (_ for _ in ()).throw(OSError()),
            )
            for key in ("uptimeSec", "load1", "load5", "load15", "cpuCount", "memory", "swap"):
                self.assertNotIn(key, metrics, key)

    def test_measured_host_numbers_are_carried(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            make_host_proc(proc)
            metrics = collector.collect_host_metrics(proc=proc, cpu_count=lambda: 4)
            self.assertEqual(metrics["uptimeSec"], 3600)
            self.assertEqual(
                (metrics["load1"], metrics["load5"], metrics["load15"]), (1.5, 0.5, 0.25)
            )
            self.assertEqual(metrics["cpuCount"], 4)
            self.assertEqual(metrics["memory"]["totalBytes"], 1024000 * 1024)
            self.assertEqual(metrics["swap"]["freeBytes"], 102400 * 1024)

    def test_partial_meminfo_omits_only_the_missing_half(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            (proc / "meminfo").write_text("MemTotal: 1024 kB\n")
            metrics = collector.collect_host_metrics(proc=proc, cpu_count=lambda: 2)
            self.assertNotIn("memory", metrics)
            self.assertNotIn("swap", metrics)

    def test_unmeasurable_disk_is_omitted_not_zero(self):
        def bad_statvfs(path):
            raise OSError("nope")

        self.assertIsNone(collector.disk_usage("/nope", statvfs=bad_statvfs))
        disks = collector.collect_disks("/", None, statvfs=bad_statvfs)
        self.assertEqual(disks, [])


class ProcessTests(unittest.TestCase):
    """Only allowlisted exe basenames appear, grouped with oldest start."""

    def test_non_allowlisted_exe_never_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            make_host_proc(proc)
            make_proc(proc, 101, "evil-daemon", start_ticks=500)
            rows = collector.collect_processes(proc=proc, clock_ticks=100)
            self.assertEqual(rows, [])

    def test_allowlisted_processes_group_with_count_and_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            make_host_proc(proc)
            # exe symlink basename drives the name: rename targets to allowlisted names.
            for pid, name, start in (
                (101, "muse", 2000),
                (102, "muse", 1000),
                (103, "node", 3000),
                (104, "evil", 1),
            ):
                folder = proc / str(pid)
                folder.mkdir()
                rest = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 5
                (folder / "stat").write_text("%d (%s) %s\n" % (pid, name, " ".join(rest)))
                (folder / "comm").write_text(name + "\n")
                target = Path(tmp) / name
                if not target.exists():
                    target.write_text("x")
                (folder / "exe").symlink_to(target)
            rows = collector.collect_processes(proc=proc, clock_ticks=100)
            by_name = {row["name"]: row for row in rows}
            self.assertEqual(set(by_name), {"muse", "node"})
            self.assertEqual(by_name["muse"]["count"], 2)
            # boot 1700000000 + 1000 ticks/100 = +10s
            self.assertEqual(by_name["muse"]["oldestStartedAt"], "2023-11-14T22:13:30Z")
            self.assertEqual(by_name["node"]["count"], 1)

    def test_comm_fallback_when_exe_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            proc.mkdir()
            folder = proc / "201"
            folder.mkdir()
            rest = ["S"] + ["0"] * 18 + ["100"] + ["0"] * 5
            (folder / "stat").write_text("201 (grok) %s\n" % " ".join(rest))
            (folder / "comm").write_text("grok\n")
            rows = collector.collect_processes(proc=proc, clock_ticks=100)
            self.assertEqual([r["name"] for r in rows], ["grok"])
            self.assertNotIn("oldestStartedAt", rows[0])  # no btime: omitted

    def test_missing_proc_omits_processes_section(self):
        self.assertIsNone(
            collector.collect_processes(proc=Path("/no-such-proc-xyz"), clock_ticks=100)
        )


class HostLabelTests(unittest.TestCase):
    def test_env_wins_then_alias_file_and_ip_is_never_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            alias = home / ".config" / "fleet-tools" / "vm-host-label"
            alias.parent.mkdir(parents=True)
            alias.write_text("vm-seat-a\n")
            self.assertEqual(collector.host_label(home, {"FLEET_VM_HOST_LABEL": "vm-x"}), "vm-x")
            self.assertEqual(collector.host_label(home, {}), "vm-seat-a")
            self.assertIsNone(collector.host_label(home, {"FLEET_VM_HOST_LABEL": "203.0.113.7"}))
            alias.write_text("2001:db8::1\n")
            self.assertIsNone(collector.host_label(home, {}))

    def test_seats_parse_and_omit(self):
        self.assertEqual(collector.seat_list({"FLEET_VM_SEATS": " MC, GROK ,,MC "}), ["MC", "GROK"])
        self.assertIsNone(collector.seat_list({}))
        self.assertIsNone(collector.seat_list({"FLEET_VM_SEATS": " , "}))


class HarnessSessionTests(unittest.TestCase):
    def test_counts_session_files_and_newest_mtime_without_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            muse_dir = home / ".local" / "share" / "muse" / "sessions" / "2026" / "s1"
            muse_dir.mkdir(parents=True)
            older = muse_dir / "session.jsonl"
            older.write_text("SECRET-CONTENT-THAT-MUST-NEVER-APPEAR\n")
            os.utime(older, (1700000000, 1700000000))
            (muse_dir / "notes.txt").write_text("not a session file\n")
            result = collector.collect_harness_sessions(home)
            self.assertEqual(set(result), {"muse"})
            self.assertEqual(result["muse"]["sessionFileCount"], 1)
            self.assertEqual(result["muse"]["newestMtime"], "2023-11-14T22:13:20Z")
            self.assertNotIn("SECRET", json.dumps(result))

    def test_absent_dirs_are_omitted_and_empty_dir_counts_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            result = collector.collect_harness_sessions(home)
            self.assertEqual(result, {"codex": {"sessionFileCount": 0}})


class CheckoutTests(unittest.TestCase):
    def test_dirty_counts_branch_head_and_unpushed_on_temp_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            git(repo, "init", "-b", "main")
            (repo / "a.txt").write_text("one\n")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "first")
            (repo / "a.txt").write_text("two\n")
            (repo / "b.txt").write_text("new\n")
            (root / "NotARepo").mkdir()
            rows = collector.collect_checkouts(root)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["name"], "Example")
            self.assertEqual(row["branch"], "main")
            self.assertTrue(row["headShort"])
            self.assertEqual(row["dirtyTracked"], 1)
            self.assertEqual(row["dirtyUntracked"], 1)
            self.assertIs(row["unpushed"], True)

    def test_missing_code_root_omits_checkouts(self):
        self.assertIsNone(collector.collect_checkouts(Path("/no-such-code-root-xyz")))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(collector.collect_checkouts(Path(tmp)), [])


class ReportShapeTests(unittest.TestCase):
    def test_json_flag_prints_shape_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            code = Path(tmp) / "Code"
            code.mkdir()
            proc = Path(tmp) / "proc"
            proc.mkdir()
            make_host_proc(proc)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code_rc = collector.main(
                    ["--json", "--home", str(home), "--code-root", str(code)],
                    proc=proc,
                    env={"FLEET_VM_HOST_LABEL": "vm-a", "FLEET_VM_SEATS": "MC"},
                )
            self.assertEqual(code_rc, 0)
            doc = json.loads(out.getvalue())
            self.assertEqual(doc["schemaVersion"], collector.SCHEMA_VERSION)
            self.assertTrue(doc["generatedAt"].endswith("Z"))
            self.assertEqual(doc["hostLabel"], "vm-a")
            self.assertEqual(doc["seats"], ["MC"])
            self.assertEqual(doc["uptimeSec"], 3600)
            self.assertIn("disks", doc)
            self.assertEqual(doc["checkouts"], [])
            self.assertFalse((home / ".local").exists(), "--json must write nothing")

    def test_build_report_omits_optional_sections_when_unmeasurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            proc = Path(tmp) / "no-proc"
            doc = collector.build_report(home=home, code_root=None, proc=proc, env={})
            self.assertNotIn("hostLabel", doc)
            self.assertNotIn("seats", doc)
            self.assertNotIn("processes", doc)
            self.assertNotIn("harnessSessions", doc)
            self.assertNotIn("checkouts", doc)

    def test_write_report_is_atomic_and_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "vm-status.json"
            collector.write_report(path, {"schemaVersion": 1})
            self.assertEqual(json.loads(path.read_text()), {"schemaVersion": 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            leftovers = [p.name for p in path.parent.iterdir() if p.name != "vm-status.json"]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
