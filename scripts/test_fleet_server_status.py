"""Tests for scripts/fleet-server-status.py:  python3 scripts/test_fleet_server_status.py

The rules under test are the collector's own:  a measurement that cannot be taken is
omitted and never reported as a zero,  a path that looks like a credential is never
hashed and never listed,  the report is written atomically and privately,  and drift
is measured against the previous report.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "fleet_server_status", HERE / "fleet-server-status.py"
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

SEP = "\x1f"


def inspect_line(
    identifier="abc123",
    container="coolify",
    image="coolify:1",
    state="running",
    health="healthy",
    started="2026-10-26T12:00:00.000000000Z",
    finished="0001-01-01T00:00:00Z",
    application_uuid="",
    label_name="",
    label_type="",
    resource_name="",
    service_name="",
    compose_service="",
):
    """One line of `docker inspect --format`,  the way docker writes it."""
    return SEP.join(
        [
            identifier,
            "/" + container,
            image,
            state,
            health,
            started,
            finished,
            application_uuid or "<no value>",
            label_name or "<no value>",
            label_type or "<no value>",
            resource_name or "<no value>",
            service_name or "<no value>",
            compose_service or "<no value>",
        ]
    )


class FakeDocker:
    """A docker that answers with what a test says it should."""

    def __init__(self, containers=None, stats=None, ids=None):
        self.containers = containers or []
        self.stats = stats or []
        self.ids = ids if ids is not None else [line.split(SEP)[0] for line in self.containers]

    def __call__(self, command, timeout=30):
        if command[:3] == ["docker", "ps", "-a"]:
            return "\n".join(self.ids) + "\n"
        if command[:2] == ["docker", "inspect"]:
            return "\n".join(self.containers) + "\n"
        if command[:2] == ["docker", "stats"]:
            return "\n".join(self.stats) + "\n"
        return None


class DriftTests(unittest.TestCase):
    """Drift is measured against the previous report."""

    def previous(self, path, sha):
        return {path: {"path": path, "sha256": sha, "drift": "unchanged"}}

    def test_a_file_that_was_not_in_the_previous_report_is_new(self):
        self.assertEqual(collector.drift_of("/a/AGENTS.md", "abc", {}), "new")

    def test_a_file_whose_digest_changed_is_changed(self):
        before = self.previous("/a/AGENTS.md", "aaa")
        self.assertEqual(collector.drift_of("/a/AGENTS.md", "bbb", before), "changed")

    def test_a_file_that_did_not_change_is_unchanged(self):
        before = self.previous("/a/AGENTS.md", "aaa")
        self.assertEqual(collector.drift_of("/a/AGENTS.md", "aaa", before), "unchanged")

    def test_a_file_that_was_missing_before_and_is_there_now_is_new(self):
        # There is no digest to compare against,  so the file is new and not changed.
        before = {"/a/AGENTS.md": {"path": "/a/AGENTS.md", "drift": "missing"}}
        self.assertEqual(collector.drift_of("/a/AGENTS.md", "aaa", before), "new")

    def test_a_file_that_is_gone_is_missing_and_not_a_zero_byte_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            (repo / "AGENTS.md").write_text("hello\n")
            before = {
                str(repo / "AGENTS.md"): {
                    "path": str(repo / "AGENTS.md"),
                    "sha256": "aaaa",
                }
            }
            rows, _total = collector.collect_files(root, before)
            (repo / "AGENTS.md").unlink()
            rows, _total = collector.collect_files(root, before)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["drift"], "missing")
            # A file that is not there has no bytes and no digest:  it is not a
            # zero byte file.
            self.assertNotIn("bytes", rows[0])
            self.assertNotIn("sha256", rows[0])

    def test_the_first_run_calls_everything_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            (repo / "AGENTS.md").write_text("hello\n")
            rows, _total = collector.collect_files(root, {})
            self.assertEqual([row["drift"] for row in rows], ["new"])


class PathFilterTests(unittest.TestCase):
    """A path that looks like a credential is never hashed and never listed."""

    def test_a_credential_path_is_never_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            (repo / "AGENTS.md").write_text("hello\n")
            (repo / ".env").write_text("TOKEN=secret\n")
            (repo / "credentials.json").write_text("{}\n")
            (repo / "config").mkdir()
            (repo / "config" / "secrets.json").write_text("{}\n")
            (repo / "id_rsa.key").write_text("-----BEGIN-----\n")
            rows, _total = collector.collect_files(root, {})
            listed = [row["path"] for row in rows]
            self.assertEqual(listed, [str(repo / "AGENTS.md")])

    def test_a_secret_marker_anywhere_in_the_path_excludes_it(self):
        for path in (
            "/srv/secrets/AGENTS.md",
            "/srv/credential/AGENTS.md",
            "/srv/.env/AGENTS.md",
            "/srv/keys/AGENTS.md",
            "/srv/token/AGENTS.md",
        ):
            self.assertTrue(collector.is_secret_path(path), path)

    def test_an_ordinary_path_is_not_a_secret(self):
        for path in (
            "/srv/example-code/AI-Fleet-Coordinator/AGENTS.md",
            "/srv/example-code/example-repo/AGENTS.md",
        ):
            self.assertFalse(collector.is_secret_path(path), path)

    def test_the_watched_files_are_the_instruction_and_settings_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            (repo / ".github" / "workflows").mkdir(parents=True)
            (repo / "config").mkdir(parents=True)
            for name in (
                "AGENTS.md",
                "CLAUDE.md",
                "README.md",
                "INFISICAL.md",
                "project.yml",
                "package.json",
            ):
                (repo / name).write_text("x\n")
            (repo / ".github" / "workflows" / "ci.yml").write_text("x\n")
            (repo / "config" / "one.json").write_text("{}\n")
            # Not watched:  a random file,  a nested config file,  a yaml workflow.
            (repo / "notes.txt").write_text("x\n")
            (repo / "config" / "deep").mkdir()
            (repo / "config" / "deep" / "two.json").write_text("{}\n")
            (repo / ".github" / "workflows" / "other.yaml").write_text("x\n")
            rows, _total = collector.collect_files(root, {})
            names = sorted(Path(row["path"]).name for row in rows)
            self.assertEqual(
                names,
                [
                    "AGENTS.md",
                    "CLAUDE.md",
                    "INFISICAL.md",
                    "README.md",
                    "ci.yml",
                    "one.json",
                    "package.json",
                    "project.yml",
                ],
            )

    def test_a_build_folder_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            (repo / "node_modules" / "pkg").mkdir(parents=True)
            (repo / ".git").mkdir(parents=True)
            (repo / "node_modules" / "pkg" / "AGENTS.md").write_text("x\n")
            (repo / ".git" / "AGENTS.md").write_text("x\n")
            (repo / "AGENTS.md").write_text("x\n")
            rows, _total = collector.collect_files(root, {})
            self.assertEqual([row["path"] for row in rows], [str(repo / "AGENTS.md")])

    def test_the_bound_is_shared_out_fairly_across_the_checkouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            for name in ("Alpha", "Beta", "Gamma"):
                repo = root / name
                repo.mkdir(parents=True)
                (repo / "AGENTS.md").write_text("x\n")
                (repo / "CLAUDE.md").write_text("x\n")
                (repo / "README.md").write_text("x\n")
                (repo / "INFISICAL.md").write_text("x\n")
                (repo / "project.yml").write_text("x\n")
                (repo / "package.json").write_text("x\n")
                (repo / ".github" / "workflows").mkdir(parents=True)
                for index in range(4):
                    (repo / ".github" / "workflows" / ("w%d.yml" % index)).write_text("x\n")
            paths, total = collector.watched_paths(root, limit=6)
            self.assertEqual(total, 30)
            self.assertEqual(len(paths), 6)
            owners = {path.parts[len(root.parts)] for path in paths}
            # Every checkout is offered a share,  so no repo is unseen.
            self.assertEqual(owners, {"Alpha", "Beta", "Gamma"})


class OmissionTests(unittest.TestCase):
    """A measurement that cannot be taken is omitted,  never reported as a zero."""

    def test_a_container_without_stats_has_no_cpu_and_no_memory(self):
        docker = FakeDocker(
            containers=[
                inspect_line(
                    container="coolify",
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="Example",
                )
            ],
            stats=[],
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("cpuPercent", rows[0])
        self.assertNotIn("memoryBytes", rows[0])
        self.assertEqual(rows[0]["status"], "running")

    def test_a_container_with_stats_carries_them(self):
        docker = FakeDocker(
            containers=[inspect_line(container="coolify")],
            stats=["coolify\t12.50%\t22.48MiB / 1GiB"],
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["cpuPercent"], 12.5)
        self.assertEqual(rows[0]["memoryBytes"], int(22.48 * 1024 * 1024))

    def test_docker_that_cannot_be_read_leaves_no_resources(self):
        def broken(command, timeout=30):
            return None

        self.assertEqual(collector.collect_coolify(run_command=broken), [])

    def test_a_container_that_vanished_does_not_blank_the_section(self):
        class VanishingDocker:
            """A docker whose batch inspect fails,  because one container went away."""

            def __call__(self, command, timeout=30):
                if command[:3] == ["docker", "ps", "-a"]:
                    return "one\ntwo\n"
                if command[:2] == ["docker", "inspect"]:
                    # The batch carries every id,  so it is longer than one call.
                    if len(command) > 5:
                        return None
                    if command[-1] == "one":
                        return inspect_line(container="coolify") + "\n"
                    return None
                if command[:2] == ["docker", "stats"]:
                    return ""
                return None

        rows = collector.collect_coolify(run_command=VanishingDocker())
        # The container that is still there is reported,  and the one that went away
        # is left out:  one redeploy must not blank the whole section.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "coolify")

    def test_a_health_check_that_fails_is_unhealthy(self):
        docker = FakeDocker(
            containers=[inspect_line(container="app", health="unhealthy")],
            stats=[],
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["status"], "unhealthy")

    def test_a_stopped_container_is_exited(self):
        docker = FakeDocker(
            containers=[inspect_line(container="app", state="exited", health="")],
            stats=[],
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["status"], "exited")

    def test_a_state_this_collector_does_not_know_is_unknown(self):
        docker = FakeDocker(
            containers=[inspect_line(container="app", state="created", health="")],
            stats=[],
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["status"], "unknown")

    def test_a_process_that_cannot_be_measured_is_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            # A pid folder with nothing in it:  no stat,  no statm,  no comm.
            (proc / "4242").mkdir()
            rows = collector.collect_processes(limit=10, interval=0.0, proc=proc)
            self.assertEqual(rows, [])


class ProcessTests(unittest.TestCase):
    """A process is identified by its executable,  and never by its argv."""

    def make_proc(self, root, pid, comm, utime, stime, rss_pages):
        folder = root / str(pid)
        folder.mkdir(parents=True)
        fields = ["S", "1", str(pid), str(pid), "0", "-1", "4194304", "0", "0", "0", "0"]
        fields += [str(utime), str(stime)]
        fields += ["0"] * 20
        (folder / "stat").write_text("%d (%s) %s\n" % (pid, comm, " ".join(fields)))
        (folder / "statm").write_text("100 %d 0 0 0 0 0\n" % rss_pages)
        (folder / "comm").write_text(comm + "\n")
        executable = root / (comm + ".bin")
        executable.write_text("#!/bin/sh\n")
        (folder / "exe").symlink_to(executable)

    def test_a_process_carries_its_pid_owner_name_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            self.make_proc(proc, 4242, "next-server", 100, 50, 2048)
            rows = collector.collect_processes(limit=5, interval=0.0, proc=proc)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["pid"], 4242)
            self.assertEqual(row["name"], "next-server.bin")
            self.assertEqual(row["memoryBytes"], 2048 * os.sysconf("SC_PAGE_SIZE"))
            self.assertIn("owner", row)

    def test_a_process_that_exits_between_the_samples_has_no_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            self.make_proc(proc, 4242, "worker", 100, 50, 16)
            first = collector.process_ticks(4242, proc)
            (proc / "4242" / "stat").unlink()
            self.assertEqual(collector.process_ticks(4242, proc), None)
            self.assertIsNotNone(first)


class CoolifyTests(unittest.TestCase):
    """The Coolify half comes from Docker's own labels."""

    def test_a_coolify_label_gives_the_uuid_the_name_and_the_type(self):
        docker = FakeDocker(
            containers=[
                inspect_line(
                    container="example-abc",
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="Example",
                    label_name="example",
                )
            ]
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["uuid"], "uuid1")
        self.assertEqual(rows[0]["name"], "Example (example-abc)")
        self.assertEqual(rows[0]["type"], "application")

    def test_a_container_without_a_coolify_label_is_standalone(self):
        docker = FakeDocker(containers=[inspect_line(container="botfleet-vps-shared")])
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["type"], "standalone")
        self.assertEqual(rows[0]["uuid"], "botfleet-vps-shared")

    def test_a_type_this_collector_does_not_know_is_standalone(self):
        self.assertEqual(collector.coolify_type("something-new"), "standalone")
        self.assertEqual(collector.coolify_type("database"), "database")
        self.assertEqual(collector.coolify_type(""), "standalone")

    def test_two_services_of_one_application_get_a_name_each(self):
        docker = FakeDocker(
            containers=[
                inspect_line(
                    container="app-one",
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="Example",
                    compose_service="one",
                ),
                inspect_line(
                    container="app-two",
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="Example",
                    compose_service="two",
                ),
            ]
        )
        rows = collector.collect_coolify(run_command=docker)
        names = sorted(row["name"] for row in rows)
        self.assertEqual(names, ["Example (one)", "Example (two)"])

    def test_a_resource_that_is_gone_reads_as_absent(self):
        before = {
            ("uuid1", "Example"): {
                "uuid": "uuid1",
                "name": "Example",
                "type": "application",
                "status": "running",
            }
        }
        document = collector.build_report([], [], [], before)
        self.assertEqual(len(document["coolify"]), 1)
        self.assertEqual(document["coolify"][0]["status"], "absent")
        # An absent resource carries no measurement:  it is not a zero.
        self.assertNotIn("cpuPercent", document["coolify"][0])
        self.assertNotIn("memoryBytes", document["coolify"][0])


class AtomicWriteTests(unittest.TestCase):
    """The report is written atomically and privately."""

    def test_the_report_is_written_0600_and_the_folder_0700(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state" / "fleet-tools" / "server-status.json"
            collector.write_report(target, {"schemaVersion": 1})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_the_write_leaves_no_temporary_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server-status.json"
            collector.write_report(target, {"schemaVersion": 1})
            leftovers = [entry.name for entry in target.parent.iterdir() if entry != target]
            self.assertEqual(leftovers, [])

    def test_the_write_replaces_the_previous_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server-status.json"
            collector.write_report(target, {"schemaVersion": 1, "host": "one"})
            collector.write_report(target, {"schemaVersion": 1, "host": "two"})
            document = json.loads(target.read_text())
            self.assertEqual(document["host"], "two")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_the_report_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server-status.json"
            document = collector.build_report([], [], [], {})
            collector.write_report(target, document)
            self.assertEqual(json.loads(target.read_text()), document)


class ContractTests(unittest.TestCase):
    """The report's own contract,  and what the collector refuses to read."""

    def test_the_report_carries_the_schema_version_and_the_tool(self):
        document = collector.build_report([], [], [], {})
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(document["tool"], "fleet-server-status")
        self.assertEqual(
            sorted(document.keys()),
            ["coolify", "files", "generatedAt", "host", "processes", "schemaVersion", "tool"],
        )

    def test_the_generated_time_is_utc_and_readable(self):
        document = collector.build_report([], [], [], {})
        self.assertTrue(document["generatedAt"].endswith("Z"))
        self.assertEqual(len(document["generatedAt"]), 20)

    def test_the_collector_never_reads_argv_or_the_environment(self):
        source = (HERE / "fleet-server-status.py").read_text()
        for forbidden in ("cmdline", "os.environ", "/environ"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_help_is_offered(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                collector.parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)

    def test_the_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "server-status.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = collector.main(
                    [
                        "--dry-run",
                        "--output",
                        str(target),
                        "--code-root",
                        tmp,
                        "--process-limit",
                        "1",
                        "--cpu-interval",
                        "0",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class RepairRegressionTests(unittest.TestCase):
    def test_truncation_does_not_fabricate_missing_or_repeat_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "example"
            repo.mkdir()
            a = repo / "AGENTS.md"
            b = repo / "README.md"
            a.write_text("a")
            b.write_text("b")
            previous = {str(b): {"sha256": "before"}, str(repo / "secrets.json"): {}}
            rows, total = collector.collect_files(root, previous, limit=1)
            self.assertEqual(total, 2)  # Control: one existing file is out of budget.
            self.assertEqual([r["path"] for r in rows], [str(a)])

    def test_component_filter_keeps_keyboard_tokenizer_and_monkey_names(self):
        for name in ["keyboard", "tokenizer", "monkey", "secretary"]:
            self.assertFalse(collector.is_secret_path("/code/" + name + "/AGENTS.md"))
        self.assertTrue(collector.is_secret_path("/code/app/config/access-token.json"))

    def test_digest_refuses_symlink_fifo_and_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "plain"
            target.write_text("ok")
            self.assertIsNotNone(collector.digest_of(target))
            link = root / "link"
            link.symlink_to(target)
            self.assertIsNone(collector.digest_of(link))
            fifo = root / "fifo"
            os.mkfifo(fifo)
            self.assertIsNone(collector.digest_of(fifo))
            with target.open("wb") as f:
                f.truncate(4 * 1024 * 1024 + 1)
            self.assertIsNone(collector.digest_of(target))

    def test_write_does_not_chmod_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            collector.write_report(root / "report.json", {})
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((root / "report.json").stat().st_mode), 0o600)

    def test_docker_failure_is_unknown_but_successful_empty_is_absent(self):
        old = {("u", "Example"): {"type": "application"}}
        failed = collector.collect_coolify(run_command=lambda *a, **k: None)
        self.assertFalse(failed.complete)
        self.assertEqual(
            collector.build_report(failed, [], [], old)["coolify"][0]["status"], "unknown"
        )
        self.assertEqual(collector.build_report([], [], [], old)["coolify"][0]["status"], "absent")

    def test_surviving_service_keeps_identity_when_peer_disappears(self):
        def row(service):
            return {"uuid": "u", "name": "Example", "status": "running", "_service": service}

        two = collector.disambiguate([row("web"), row("worker")])
        one = collector.disambiguate([row("web")])
        self.assertEqual(one[0]["name"], two[0]["name"])
        before = {(r["uuid"], r["name"]): r for r in two}
        report = collector.build_report(one, [], [], before)["coolify"]
        self.assertEqual(
            [(r["name"], r["status"]) for r in report],
            [("Example (web)", "running"), ("Example (worker)", "absent")],
        )


if __name__ == "__main__":
    unittest.main()
