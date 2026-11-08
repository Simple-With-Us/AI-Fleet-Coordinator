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
import shutil
import stat
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

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


def extended_line(
    restart_count="0", exit_code="0", oom_killed="false", restart_policy="no", **fields
):
    """One line of `docker inspect --format` with the fields added for container signals."""
    return SEP.join([inspect_line(**fields), restart_count, exit_code, oom_killed, restart_policy])


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
        # The container name holds a per-deploy stamp, so it is never part of the name.
        self.assertEqual(rows[0]["name"], "Example")
        self.assertEqual(rows[0]["container"], "example-abc")
        self.assertEqual(rows[0]["type"], "application")

    def test_a_container_without_a_coolify_label_is_standalone(self):
        docker = FakeDocker(containers=[inspect_line(container="example-shared-box")])
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["type"], "standalone")
        self.assertEqual(rows[0]["uuid"], "example-shared-box")

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

    def test_a_uuid_from_the_coolify_label_says_so(self):
        docker = FakeDocker(
            containers=[
                inspect_line(
                    container="example-abc",
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="Example",
                )
            ]
        )
        rows = collector.collect_coolify(run_command=docker)
        self.assertEqual(rows[0]["uuid"], "uuid1")
        self.assertEqual(rows[0]["uuidSource"], "label")

    def test_a_uuid_that_stands_in_for_a_missing_label_is_a_name(self):
        docker = FakeDocker(
            containers=[
                # A Coolify resource name but no uuid label:  the name stands in.
                inspect_line(
                    container="example-abc",
                    label_type="service",
                    resource_name="Example",
                ),
                # No Coolify label at all:  the container name stands in.
                inspect_line(identifier="def456", container="example-shared-box"),
            ]
        )
        rows = collector.collect_coolify(run_command=docker)
        by_uuid = {row["uuid"]: row for row in rows}
        self.assertEqual(by_uuid["Example"]["uuidSource"], "name")
        self.assertEqual(by_uuid["example-shared-box"]["uuidSource"], "name")

    def test_resource_identity_names_the_source_of_the_uuid(self):
        fields = {
            "application_uuid": " uuid1 ",
            "resource_name": "Example",
            "label_name": "example",
            "container": "example-abc",
        }
        self.assertEqual(collector.resource_identity(fields), ("uuid1", "Example", "label"))
        fields["application_uuid"] = "   "
        self.assertEqual(collector.resource_identity(fields), ("Example", "Example", "name"))
        fields["resource_name"] = ""
        fields["label_name"] = ""
        self.assertEqual(
            collector.resource_identity(fields), ("example-abc", "example-abc", "name")
        )

    def test_an_absent_resource_keeps_the_source_the_previous_report_gave(self):
        before = {
            ("uuid1", "Example"): {
                "uuid": "uuid1",
                "uuidSource": "label",
                "name": "Example",
                "type": "application",
                "status": "running",
            },
            ("Other", "Other"): {
                "uuid": "Other",
                "uuidSource": "name",
                "name": "Other",
                "type": "service",
                "status": "running",
            },
            # A report written before `uuidSource` existed,  or one that says
            # something this collector does not write,  gives no source.
            ("old", "Old"): {"uuid": "old", "name": "Old", "type": "application"},
            ("odd", "Odd"): {"uuid": "odd", "name": "Odd", "uuidSource": "guess"},
        }
        document = collector.build_report([], [], [], before)
        by_uuid = {row["uuid"]: row for row in document["coolify"]}
        self.assertEqual(by_uuid["uuid1"]["uuidSource"], "label")
        self.assertEqual(by_uuid["Other"]["uuidSource"], "name")
        self.assertNotIn("uuidSource", by_uuid["old"])
        self.assertNotIn("uuidSource", by_uuid["odd"])

    def test_the_ops_control_reader_believes_only_a_label_source(self):
        spec = importlib.util.spec_from_file_location(
            "fleet_ops_control", HERE / "fleet-ops-control.py"
        )
        ops = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ops)
        docker = FakeDocker(
            containers=[
                inspect_line(
                    container="labelled",
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="Example",
                ),
                inspect_line(
                    identifier="def456",
                    container="unlabelled",
                    label_type="application",
                    resource_name="Other",
                ),
            ]
        )
        rows = {row["uuid"]: row for row in collector.collect_coolify(run_command=docker)}
        self.assertTrue(ops.uuid_from_label(rows["uuid1"]))
        self.assertFalse(ops.uuid_from_label(rows["Other"]))

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
            [
                "coolify",
                "files",
                "generatedAt",
                "host",
                "hostRole",
                "processes",
                "schemaVersion",
                "tool",
            ],
        )

    def test_the_host_role_is_computed_and_never_a_literal(self):
        self.assertEqual(collector.host_role("darwin"), "mac")
        self.assertEqual(collector.host_role("linux"), "server")
        self.assertEqual(collector.host_role("freebsd14"), "server")
        document = collector.build_report([], [], [], {}, platform="darwin")
        self.assertEqual(document["hostRole"], "mac")
        document = collector.build_report([], [], [], {}, platform="linux")
        self.assertEqual(document["hostRole"], "server")
        # The role comes from the platform this collector runs on.
        with mock.patch.object(collector.sys, "platform", "darwin"):
            self.assertEqual(collector.build_report([], [], [], {})["hostRole"], "mac")
        with mock.patch.object(collector.sys, "platform", "linux"):
            self.assertEqual(collector.build_report([], [], [], {})["hostRole"], "server")

    def test_the_schema_version_did_not_move(self):
        # Muster checks exact equality, so every addition stays optional and the version stays.
        self.assertEqual(collector.SCHEMA_VERSION, 1)

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


class FakeStatvfs:
    """`os.statvfs` with the sizes a test says each mount has."""

    def __init__(self, sizes):
        self.sizes = sizes

    def __call__(self, mount):
        if mount not in self.sizes:
            raise OSError("not mounted")
        blocks, free, available = self.sizes[mount]
        return os.statvfs_result((4096, 4096, blocks, free, available, 0, 0, 0, 0, 255))


def make_proc_files(root, loadavg=None, meminfo=None, uptime=None, mounts=None):
    """A fake `/proc` with the files a test gives it,  and none of the others."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("loadavg", loadavg),
        ("meminfo", meminfo),
        ("uptime", uptime),
        ("mounts", mounts),
    ):
        if text is not None:
            (root / name).write_text(text)
    return root


MEMINFO = (
    "MemTotal:       16000000 kB\n"
    "MemFree:         2000000 kB\n"
    "MemAvailable:    8000000 kB\n"
    "SwapTotal:       4000000 kB\n"
    "SwapFree:        3000000 kB\n"
)
MOUNTS = (
    "/dev/sda1 / ext4 rw,relatime 0 0\n"
    "proc /proc proc rw 0 0\n"
    "tmpfs /mnt/ram tmpfs rw 0 0\n"
    "/dev/sdb /mnt/data\\040one xfs rw 0 0\n"
    "/dev/sdc /srv/other ext4 rw 0 0\n"
    "/dev/sda1 / ext4 rw,relatime 0 0\n"
)


class HostMetricsTests(unittest.TestCase):
    """The host's load,  memory,  swap,  uptime and disks."""

    def test_every_measurement_is_read_from_proc_and_statvfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = make_proc_files(
                Path(tmp) / "proc",
                loadavg="0.52 0.75 1.10 2/345 6789\n",
                meminfo=MEMINFO,
                uptime="12345.67 99999.00\n",
                mounts=MOUNTS,
            )
            statvfs = FakeStatvfs({"/": (1000, 400, 300), "/mnt/data one": (2000, 2000, 1900)})
            metrics = collector.collect_host_metrics(
                proc=proc, statvfs=statvfs, cpu_count=lambda: 8
            )
        self.assertEqual(metrics["loadAverage"], [0.52, 0.75, 1.1])
        self.assertEqual(metrics["cpuCount"], 8)
        self.assertEqual(
            metrics["memory"], {"totalBytes": 16000000 * 1024, "availableBytes": 8000000 * 1024}
        )
        self.assertEqual(
            metrics["swap"], {"totalBytes": 4000000 * 1024, "freeBytes": 3000000 * 1024}
        )
        self.assertEqual(metrics["uptimeSeconds"], 12345)
        # `/` once,  the real file system under /mnt with its escaped space decoded,  and
        # neither the tmpfs under /mnt nor a disk mounted outside it.
        self.assertEqual(
            metrics["disks"],
            [
                {
                    "mount": "/",
                    "totalBytes": 1000 * 4096,
                    "usedBytes": 600 * 4096,
                    "freeBytes": 300 * 4096,
                },
                {
                    "mount": "/mnt/data one",
                    "totalBytes": 2000 * 4096,
                    "usedBytes": 0,
                    "freeBytes": 1900 * 4096,
                },
            ],
        )

    def test_a_measurement_that_cannot_be_taken_is_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = make_proc_files(Path(tmp) / "proc")
            metrics = collector.collect_host_metrics(
                proc=proc, statvfs=FakeStatvfs({}), cpu_count=lambda: None
            )
        self.assertEqual(metrics, {"disks": []})

    def test_malformed_proc_files_are_left_out_and_never_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = make_proc_files(
                Path(tmp) / "proc",
                loadavg="high\n",
                meminfo="MemTotal: lots\nGarbage\nMemAvailable:\nSwapTotal: -5 kB\n",
                uptime="soon\n",
                mounts="nonsense\n/dev/sda1 / ext4 rw 0 0\n",
            )
            # A mount that statvfs cannot read,  and one that reports no blocks.
            metrics = collector.collect_host_metrics(
                proc=proc, statvfs=FakeStatvfs({}), cpu_count=lambda: 0
            )
            self.assertEqual(metrics, {"disks": []})
            empty = collector.disk_usage("/", FakeStatvfs({"/": (0, 0, 0)}))
            self.assertIsNone(empty)

    def test_the_real_host_is_readable(self):
        metrics = collector.collect_host_metrics()
        self.assertIsInstance(metrics["disks"], list)


class FakeSystemctl:
    """A systemctl that answers `show` with the blocks a test gives it,  per scope."""

    def __init__(self, system=None, user=None):
        self.answers = {"system": system, "user": user}
        self.calls = []

    def __call__(self, command, timeout=30):
        self.calls.append(command)
        if command[0] != "systemctl":
            return None
        scope = "user" if "--user" in command else "system"
        return self.answers[scope]


def show_block(unit, active="active", sub="running", file_state="enabled", since="@1761660000"):
    return "\n".join(
        [
            "Id=" + unit,
            "Description=The %s unit" % unit,
            "LoadState=loaded",
            "ActiveState=" + active,
            "SubState=" + sub,
            "UnitFileState=" + file_state,
            "StateChangeTimestamp=" + since,
        ]
    )


class ServiceTests(unittest.TestCase):
    """The fleet's units come from the allowlist,  and from `systemctl show`."""

    def control(self, tmp, document):
        path = Path(tmp) / "fleet-ops-control.json"
        path.write_text(document if isinstance(document, str) else json.dumps(document))
        return path

    def allowlist(self):
        return {
            "schemaVersion": 1,
            "server": {
                "units": [
                    {
                        "unit": "fleet-server-status.timer",
                        "scope": "user",
                        "actions": ["start", "stop", "restart"],
                    },
                    {"unit": "cron.service", "scope": "system"},
                    {"unit": "docker.service", "scope": "system", "actions": []},
                ]
            },
        }

    def test_each_unit_carries_its_state_and_whether_it_can_be_controlled(self):
        systemctl = FakeSystemctl(
            system=show_block("cron.service")
            + "\n\n"
            + show_block("docker.service", file_state="", since="")
            + "\n",
            user=show_block("fleet-server-status.timer", sub="waiting") + "\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(
                self.control(tmp, self.allowlist()), run_command=systemctl
            )
        by_unit = {row["unit"]: row for row in rows}
        self.assertEqual(
            by_unit["cron.service"],
            {
                "unit": "cron.service",
                "scope": "system",
                "description": "The cron.service unit",
                "activeState": "active",
                "subState": "running",
                "since": collector.iso_from_timestamp(1761660000),
                "enabled": "enabled",
                "controllable": False,
                "actions": [],
            },
        )
        # An empty action list controls nothing,  and an empty field is left out.
        self.assertFalse(by_unit["docker.service"]["controllable"])
        self.assertNotIn("since", by_unit["docker.service"])
        self.assertNotIn("enabled", by_unit["docker.service"])
        timer = by_unit["fleet-server-status.timer"]
        self.assertEqual((timer["scope"], timer["subState"]), ("user", "waiting"))
        # The helper refuses a file with an entry that lists no actions,  so even the
        # timer,  whose own entry is good,  is not offered.
        self.assertFalse(timer["controllable"])

    def test_controllable_is_the_control_helpers_own_verdict(self):
        good = {
            "schemaVersion": 1,
            "server": {
                "units": [
                    {"unit": "fleet-server-status.timer", "scope": "user", "actions": ["start"]},
                    {"unit": "cron.service", "scope": "system", "actions": ["restart"]},
                ]
            },
        }
        # A templated unit the report can show but the helper never accepts:  the helper
        # then refuses the whole file,  so nothing in it is controllable.
        templated = json.loads(json.dumps(good))
        templated["server"]["units"].append(
            {"unit": "docker@backup.service", "scope": "system", "actions": ["restart"]}
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                collector.control_units(self.control(tmp, good)),
                [("fleet-server-status.timer", "user", True), ("cron.service", "system", True)],
            )
            self.assertEqual(
                collector.control_units(self.control(tmp, templated)),
                [
                    ("fleet-server-status.timer", "user", False),
                    ("cron.service", "system", False),
                    ("docker@backup.service", "system", False),
                ],
            )
            # The report reads a larger file than the helper will,  and the helper's own
            # size limit wins.
            padded = dict(good, pad="x" * (70 * 1024))
            self.assertEqual(
                [row[2] for row in collector.control_units(self.control(tmp, padded))],
                [False, False],
            )
            # A helper that cannot be loaded acts on nothing.
            absent = Path(tmp) / "absent.py"
            self.assertEqual(
                [row[2] for row in collector.control_units(self.control(tmp, good), absent)],
                [False, False],
            )

    def test_one_show_call_per_scope_with_an_explicit_property_list(self):
        systemctl = FakeSystemctl(
            system=show_block("cron.service") + "\n\n" + show_block("docker.service"),
            user=show_block("fleet-server-status.timer"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            collector.collect_services(self.control(tmp, self.allowlist()), run_command=systemctl)
        self.assertEqual(len(systemctl.calls), 2)
        for command in systemctl.calls:
            self.assertEqual(command[0], "systemctl")
            self.assertIn("show", command)
            self.assertNotIn("status", command)
            properties = [part for part in command if part.startswith("--property=")]
            self.assertEqual(len(properties), 1)
        self.assertEqual(systemctl.calls[0][-2:], ["cron.service", "docker.service"])
        self.assertIn("--user", systemctl.calls[1])
        self.assertEqual(systemctl.calls[1][-1], "fleet-server-status.timer")

    def test_a_missing_or_malformed_allowlist_means_no_units(self):
        systemctl = FakeSystemctl(system="", user="")
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            self.assertEqual(collector.collect_services(missing, run_command=systemctl), [])
            for document in (
                "not json",
                "[]",
                json.dumps({"server": []}),
                json.dumps({"server": {"units": "cron.service"}}),
                json.dumps({"server": {"units": [None, 3, "cron.service"]}}),
            ):
                path = self.control(tmp, document)
                self.assertEqual(
                    collector.collect_services(path, run_command=systemctl), [], document
                )
        self.assertEqual(systemctl.calls, [])

    def test_an_entry_with_a_bad_name_or_scope_is_skipped(self):
        document = {
            "server": {
                "units": [
                    {"unit": "--now", "scope": "system", "actions": ["start"]},
                    {"unit": "-H.service", "scope": "system"},
                    {"unit": "cron.service", "scope": "root"},
                    {"unit": "cron.service", "scope": "system", "actions": "start"},
                    {"unit": "cron.service", "scope": "system", "actions": ["start"]},
                    {"unit": "space here.service", "scope": "system"},
                    {"unit": 7, "scope": "system"},
                ]
            }
        }
        systemctl = FakeSystemctl(system=show_block("cron.service"))
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(self.control(tmp, document), run_command=systemctl)
        self.assertEqual([row["unit"] for row in rows], ["cron.service"])
        # The first entry for a unit wins,  and an action list that is not a list
        # controls nothing.
        self.assertFalse(rows[0]["controllable"])
        self.assertEqual(systemctl.calls[0][-1], "cron.service")

    def test_a_scope_systemctl_cannot_answer_leaves_its_units_unknown(self):
        systemctl = FakeSystemctl(system=None, user=show_block("fleet-server-status.timer"))
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(
                self.control(tmp, self.allowlist()), run_command=systemctl
            )
        by_unit = {row["unit"]: row for row in rows}
        self.assertEqual(
            by_unit["cron.service"],
            {
                "unit": "cron.service",
                "scope": "system",
                "activeState": "unknown",
                "controllable": False,
                "actions": [],
            },
        )
        self.assertEqual(by_unit["fleet-server-status.timer"]["activeState"], "active")

    def test_an_answer_with_the_wrong_number_of_blocks_is_not_trusted(self):
        # One block for two units cannot be matched to either of them.
        systemctl = FakeSystemctl(system=show_block("cron.service"), user="")
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(
                self.control(tmp, self.allowlist()), run_command=systemctl
            )
        states = {row["unit"]: row["activeState"] for row in rows}
        self.assertEqual(states["cron.service"], "unknown")
        self.assertEqual(states["docker.service"], "unknown")
        self.assertEqual(states["fleet-server-status.timer"], "unknown")

    def test_a_timestamp_that_is_not_a_unix_one_is_left_out(self):
        self.assertIsNone(collector.iso_from_systemd(""))
        self.assertIsNone(collector.iso_from_systemd("n/a"))
        self.assertIsNone(collector.iso_from_systemd("@0"))
        self.assertIsNone(collector.iso_from_systemd("Tue 2026-10-28 10:00:00 UTC"))
        self.assertEqual(collector.iso_from_systemd("@1761660000"), "2025-10-28T14:00:00Z")


class SummaryAndInfoDriftTests(unittest.TestCase):
    """`filesSummary` tells a partial list from a whole one,  and a README is `info`."""

    def test_the_summary_says_when_the_list_was_cut(self):
        self.assertEqual(
            collector.summarize_files(30, 6), {"found": 30, "limit": 6, "truncated": True}
        )
        self.assertEqual(
            collector.summarize_files(6, 6), {"found": 6, "limit": 6, "truncated": False}
        )

    def test_a_changed_readme_or_package_manifest_is_info(self):
        for name in ("README.md", "package.json"):
            before = {"/a/" + name: {"sha256": "aaa"}}
            self.assertEqual(collector.drift_of("/a/" + name, "bbb", before), "info", name)
            self.assertEqual(collector.drift_of("/a/" + name, "aaa", before), "unchanged", name)
            self.assertEqual(collector.drift_of("/a/" + name, "aaa", {}), "new", name)
        before = {"/a/AGENTS.md": {"sha256": "aaa"}}
        self.assertEqual(collector.drift_of("/a/AGENTS.md", "bbb", before), "changed")

    def test_the_new_keys_are_optional(self):
        document = collector.build_report(
            [],
            [],
            [],
            {},
            host_metrics={"disks": []},
            services=[],
            files_summary=collector.summarize_files(0, 200),
        )
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(document["hostMetrics"], {"disks": []})
        self.assertEqual(document["services"], [])
        self.assertEqual(document["filesSummary"], {"found": 0, "limit": 200, "truncated": False})

    def test_a_run_carries_every_new_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "Code" / "Example"
            repo.mkdir(parents=True)
            for name in ("AGENTS.md", "README.md", "CLAUDE.md"):
                (repo / name).write_text(name)
            control = root / "control.json"
            control.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "server": {
                            "units": [
                                {"unit": "cron.service", "scope": "system", "actions": ["restart"]}
                            ]
                        },
                    }
                )
            )
            target = root / "state" / "server-status.json"

            def fake(command, timeout=30):
                if command[0] == "systemctl":
                    return show_block("cron.service")
                if command[:3] == ["docker", "ps", "-a"]:
                    return ""
                return None

            argv = [
                "--output",
                str(target),
                "--code-root",
                str(root / "Code"),
                "--control",
                str(control),
                "--file-limit",
                "2",
                "--process-limit",
                "1",
                "--cpu-interval",
                "0",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(collector.main(argv, run_command=fake), 0)
            document = json.loads(target.read_text())
            self.assertEqual(document["schemaVersion"], 1)
            self.assertEqual(
                document["filesSummary"],
                {"found": 3, "limit": 2, "truncated": True, "cutCheckouts": ["Example"]},
            )
            self.assertEqual(len(document["files"]), 2)
            self.assertEqual(document["services"][0]["unit"], "cron.service")
            self.assertTrue(document["services"][0]["controllable"])
            self.assertIn("disks", document["hostMetrics"])
            # A whole run,  then a change:  a changed README is `info`.
            whole = argv[:6] + ["--file-limit", "5"] + argv[8:]
            with contextlib.redirect_stdout(io.StringIO()):
                collector.main(whole, run_command=fake)
            (repo / "README.md").write_text("changed")
            (repo / "AGENTS.md").write_text("changed")
            with contextlib.redirect_stdout(io.StringIO()):
                collector.main(whole, run_command=fake)
            drift = {
                Path(row["path"]).name: row["drift"]
                for row in json.loads(target.read_text())["files"]
            }
            self.assertEqual(drift["README.md"], "info")
            self.assertEqual(drift["AGENTS.md"], "changed")
            self.assertEqual(drift["CLAUDE.md"], "unchanged")

    def test_a_missing_allowlist_never_crashes_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = collector.main(
                    [
                        "--dry-run",
                        "--code-root",
                        tmp,
                        "--control",
                        str(Path(tmp) / "absent.json"),
                        "--process-limit",
                        "1",
                        "--cpu-interval",
                        "0",
                    ],
                    run_command=lambda command, timeout=30: None,
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["services"], [])


class ResourceReceiptTests(unittest.TestCase):
    def test_missing_or_nonprivate_receipts_are_not_a_measurement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "resources.json"
            self.assertIsNone(collector.collect_resource_policy(path, owner_uid=os.getuid()))
            path.write_text("{}")
            path.chmod(0o666)
            self.assertIsNone(collector.collect_resource_policy(path, owner_uid=os.getuid()))
            path.chmod(0o600)
            link = Path(folder) / "link.json"
            link.symlink_to(path)
            self.assertIsNone(collector.collect_resource_policy(link, owner_uid=os.getuid()))

    def test_receipt_is_projected_without_private_inventory(self):
        import datetime

        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = {
            "schemaVersion": 1,
            "generatedAt": stamp,
            "policyRevision": "a" * 64,
            "desired": {
                "cpuCores": 12,
                "cpuWeight": 50,
                "ramPressureGiB": 8,
                "ramCriticalGiB": 6,
                "reserveGiB": 6,
                "allowBorrowing": True,
            },
            "observed": {
                "cpuOnline": 16,
                "cpuCores": 12,
                "cpuWeight": 50,
                "cpuGroups": 7,
                "cpuMismatches": 0,
                "ramTotalBytes": 32 * 1024**3,
                "ramCurrentBytes": 10 * 1024**3,
                "ramTargetBytes": 18 * 1024**3,
                "mode": "normal",
                "codingGroups": 7,
                "productionGroups": 4,
                "controlWriteFailures": 0,
                "controlSuspended": 0,
                "policyStale": False,
                "actualHighMismatches": 0,
                "updatedAt": stamp,
            },
            "status": "applied",
            "token": "private-value",
            "coding_units": ["private-unit"],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "resources.json"
            path.write_text(json.dumps(raw))
            path.chmod(0o644)
            result = collector.collect_resource_policy(path, owner_uid=os.getuid())
            self.assertEqual(result["status"], "applied")
            self.assertNotIn("private-", json.dumps(result))
            report = collector.build_report([], [], [], {}, resource_policy=result)
            self.assertEqual(report["resourcePolicy"], result)
            raw["generatedAt"] = "2000-01-01T00:00:00Z"
            path.write_text(json.dumps(raw))
            self.assertEqual(
                collector.collect_resource_policy(path, owner_uid=os.getuid())["status"],
                "unavailable",
            )


NOW = 1793800000.0
DAY = 24 * 60 * 60


def stamp(seconds):
    return collector.iso_from_timestamp(seconds)


def previous_map(rows):
    """What `previous_resources` gives for a report holding these rows."""
    return {(row["uuid"], row.get("name")): row for row in rows}


class LeftoverResourceTests(unittest.TestCase):
    """Nothing stays in the report forever because it once existed."""

    def deploy(self, stamp_text):
        """One deploy of a one-container application, the way Coolify labels it for real."""
        container = "uuid1-%s" % stamp_text
        return FakeDocker(
            containers=[
                inspect_line(
                    container=container,
                    label_type="application",
                    application_uuid="uuid1",
                    resource_name="listener",
                    compose_service=container,
                )
            ]
        )

    def test_a_redeploy_leaves_nothing_behind(self):
        first = collector.collect_coolify(run_command=self.deploy("20261103T000000"))
        report = collector.build_report(first, [], [], {}, now=NOW)
        second = collector.collect_coolify(run_command=self.deploy("20261104T000000"))
        report = collector.build_report(second, [], [], previous_map(report["coolify"]), now=NOW)
        self.assertEqual([row["name"] for row in report["coolify"]], ["listener"])
        self.assertEqual(report["coolify"][0]["status"], "running")
        self.assertEqual(report["coolify"][0]["container"], "uuid1-20261104T000000")
        self.assertNotIn("service", report["coolify"][0])

    def test_a_compose_label_coolify_generated_is_not_a_service(self):
        self.assertEqual(collector.real_service("uuid1", "uuid1", "uuid1"), "")
        self.assertEqual(collector.real_service("uuid1-2026", "uuid1", "uuid1-2026"), "")
        self.assertEqual(collector.real_service("x-022619", "name", "x-022619"), "")
        self.assertEqual(collector.real_service("uuid1-2026", "uuid1", "other"), "")
        self.assertEqual(collector.real_service("", "uuid1", "other"), "")
        self.assertEqual(collector.real_service("web", "uuid1", "web-uuid1-2026"), "web")

    def gone_row(self, **extra):
        row = {
            "uuid": "uuid1",
            "uuidSource": "label",
            "name": "Example",
            "type": "application",
            "status": "absent",
            "container": "example-1",
        }
        row.update(extra)
        return row

    def test_a_row_says_since_when_it_is_absent_and_keeps_that_time(self):
        running = dict(self.gone_row(), status="running")
        report = collector.build_report([], [], [], previous_map([running]), now=NOW)
        row = report["coolify"][0]
        self.assertEqual(row["status"], "absent")
        self.assertEqual(row["absentSince"], stamp(NOW))
        # The next run keeps the first time, so the row can age out.
        later = collector.build_report([], [], [], previous_map([row]), now=NOW + DAY)
        self.assertEqual(later["coolify"][0]["absentSince"], stamp(NOW))

    def test_an_absent_row_is_dropped_after_seven_days(self):
        row = self.gone_row(absentSince=stamp(NOW))
        almost = NOW + 7 * DAY - 1
        kept = collector.build_report([], [], [], previous_map([row]), now=almost)
        self.assertEqual(len(kept["coolify"]), 1)
        gone = collector.build_report([], [], [], previous_map([row]), now=NOW + 7 * DAY)
        self.assertEqual(gone["coolify"], [])

    def test_a_row_with_a_bad_or_future_time_starts_its_seven_days_now(self):
        for bad in ("soon", 12, stamp(NOW + DAY)):
            row = self.gone_row(absentSince=bad)
            report = collector.build_report([], [], [], previous_map([row]), now=NOW)
            self.assertEqual(report["coolify"][0]["absentSince"], stamp(NOW), bad)

    def test_the_leftovers_of_an_old_report_are_cleaned_up_once(self):
        # An old report's absent row has no age, so it cannot be aged: it goes.
        old_absent = {"uuid": "uuid2", "name": "Gone", "type": "application", "status": "absent"}
        # A row with a stamped container name matches nothing by name, but its uuid is live.
        old_named = {
            "uuid": "uuid1",
            "name": "listener (uuid1-20261103T000000)",
            "type": "application",
            "status": "running",
        }
        # A running row in an old report whose uuid is really gone is a real disappearance.
        old_running = {"uuid": "uuid3", "name": "Other", "type": "application", "status": "running"}
        live = collector.collect_coolify(run_command=self.deploy("20261104T000000"))
        report = collector.build_report(
            live, [], [], previous_map([old_absent, old_named, old_running]), now=NOW
        )
        rows = {row["name"]: row for row in report["coolify"]}
        self.assertEqual(sorted(rows), ["Other", "listener"])
        self.assertEqual(rows["Other"]["status"], "absent")
        self.assertEqual(rows["Other"]["absentSince"], stamp(NOW))

    def test_the_real_report_shape_shrinks_to_what_is_running(self):
        # The shape on the server the day this was written: stamped rows of one application,
        # helper containers that nothing labelled, and the one live row.
        old = [
            {
                "uuid": "uuid1",
                "name": "listener (uuid1-2026110%dT000000)" % day,
                "type": "application",
                "uuidSource": "label",
                "status": "absent",
            }
            for day in range(1, 4)
        ]
        old += [
            {
                "uuid": "abcdefghijklmnopqrstuvwx",
                "name": "abcdefghijklmnopqrstuvwx",
                "type": "standalone",
                "uuidSource": "name",
                "status": "absent",
            },
            {
                "uuid": "happy_example",
                "name": "happy_example",
                "type": "standalone",
                "uuidSource": "name",
                "status": "absent",
            },
        ]
        live = collector.collect_coolify(run_command=self.deploy("20261104T000000"))
        report = collector.build_report(live, [], [], previous_map(old), now=NOW)
        self.assertEqual([row["name"] for row in report["coolify"]], ["listener"])

    def test_a_throwaway_container_leaves_no_row(self):
        for name in ("happy_example", "abcdefghijklmnopqrstuvwx"):
            row = {
                "uuid": name,
                "name": name,
                "type": "standalone",
                "uuidSource": "name",
                "status": "running",
                "container": name,
            }
            report = collector.build_report([], [], [], previous_map([row]), now=NOW)
            self.assertEqual(report["coolify"], [], name)

    def test_a_container_someone_asked_to_keep_is_reported_whatever_its_name_looks_like(self):
        for policy in ("always", "unless-stopped", "on-failure"):
            row = {
                "uuid": "kept_service",
                "name": "kept_service",
                "type": "standalone",
                "uuidSource": "name",
                "status": "running",
                "container": "kept_service",
                "restartPolicy": policy,
            }
            report = collector.build_report([], [], [], previous_map([row]), now=NOW)
            self.assertEqual(report["coolify"][0]["status"], "absent", policy)
            self.assertEqual(report["coolify"][0]["restartPolicy"], policy)
            # It stays reported on the next run, which reads this run's row as its previous row.
            again = collector.build_report(
                [], [], [], previous_map(report["coolify"]), now=NOW + 60
            )
            self.assertEqual(again["coolify"][0]["status"], "absent", policy)
        row["restartPolicy"] = "no"
        report = collector.build_report([], [], [], previous_map([row]), now=NOW)
        self.assertEqual(report["coolify"], [])

    def test_a_named_container_that_vanished_is_still_reported(self):
        row = {
            "uuid": "example-shared-box",
            "name": "example-shared-box",
            "type": "standalone",
            "uuidSource": "name",
            "status": "running",
            "container": "example-shared-box",
        }
        report = collector.build_report([], [], [], previous_map([row]), now=NOW)
        self.assertEqual(report["coolify"][0]["status"], "absent")
        # A labelled application is never a throwaway, whatever its uuid looks like.
        labelled = {
            "uuid": "abcdefghijklmnopqrstuvwx",
            "name": "App",
            "type": "standalone",
            "uuidSource": "label",
            "status": "running",
            "container": "app-1",
        }
        report = collector.build_report([], [], [], previous_map([labelled]), now=NOW)
        self.assertEqual(report["coolify"][0]["status"], "absent")

    def test_a_docker_failure_starts_no_clock_and_keeps_a_running_clock(self):
        failed = collector.collect_coolify(run_command=lambda *a, **k: None)
        running = {
            "uuid": "u1",
            "name": "One",
            "type": "application",
            "status": "running",
            "container": "one",
        }
        absent = {
            "uuid": "u2",
            "name": "Two",
            "type": "application",
            "status": "absent",
            "container": "two",
            "absentSince": stamp(NOW - DAY),
        }
        report = collector.build_report(failed, [], [], previous_map([running, absent]), now=NOW)
        rows = {row["uuid"]: row for row in report["coolify"]}
        self.assertEqual(rows["u1"]["status"], "unknown")
        self.assertNotIn("absentSince", rows["u1"])
        self.assertEqual(rows["u2"]["status"], "unknown")
        self.assertEqual(rows["u2"]["absentSince"], stamp(NOW - DAY))
        # Docker answers again, and an empty answer is now evidence.
        after = collector.build_report([], [], [], previous_map(report["coolify"]), now=NOW + 60)
        rows = {row["uuid"]: row for row in after["coolify"]}
        self.assertEqual(rows["u1"]["absentSince"], stamp(NOW + 60))
        self.assertEqual(rows["u2"]["absentSince"], stamp(NOW - DAY))

    def test_live_rows_come_before_leftovers(self):
        # The helper takes the first row with a uuid, so a leftover must never be first.
        gone = {
            "uuid": "uuid1",
            "name": "Aardvark (web)",
            "service": "web",
            "type": "application",
            "status": "running",
            "container": "web-1",
            "uuidSource": "label",
        }
        live = collector.collect_coolify(
            run_command=FakeDocker(
                containers=[
                    inspect_line(
                        container="worker-1",
                        label_type="application",
                        application_uuid="uuid1",
                        resource_name="Zebra",
                        compose_service="worker",
                    )
                ]
            )
        )
        report = collector.build_report(live, [], [], previous_map([gone]), now=NOW)
        self.assertEqual([row["status"] for row in report["coolify"]], ["running", "absent"])


class RealActionsTests(unittest.TestCase):
    """Each row carries only what the helper would really do for it."""

    def rows(self):
        return collector.collect_coolify(
            run_command=FakeDocker(
                containers=[
                    inspect_line(
                        container="web-1",
                        label_type="application",
                        application_uuid="uuid1",
                        resource_name="Example",
                        compose_service="web",
                    ),
                    inspect_line(
                        identifier="b",
                        container="worker-1",
                        label_type="application",
                        application_uuid="uuid1",
                        resource_name="Example",
                        compose_service="worker",
                    ),
                    inspect_line(
                        identifier="c",
                        container="relay-1",
                        label_type="application",
                        resource_name="relay",
                    ),
                    inspect_line(
                        identifier="d",
                        container="db-1",
                        label_type="service",
                        resource_name="database",
                    ),
                ]
            )
        )

    def test_only_a_live_labelled_application_can_be_redeployed(self):
        gone = {
            "uuid": "uuid9",
            "name": "Gone",
            "type": "application",
            "uuidSource": "label",
            "status": "absent",
            "absentSince": stamp(NOW),
        }
        report = collector.build_report(
            self.rows(), [], [], previous_map([gone]), now=NOW, can_redeploy=True
        )
        rows = {row["name"]: row for row in report["coolify"]}
        self.assertEqual(rows["Example (web)"]["actions"], ["redeploy"])
        self.assertEqual(rows["Example (worker)"]["actions"], ["redeploy"])
        self.assertNotIn("redeployBlockedReason", rows["Example (web)"])
        self.assertEqual(rows["relay"]["actions"], [])
        self.assertEqual(rows["relay"]["redeployBlockedReason"], "no_uuid_label")
        self.assertEqual(rows["database"]["actions"], [])
        self.assertNotIn("redeployBlockedReason", rows["database"])
        self.assertEqual(rows["Gone"]["actions"], [])
        self.assertEqual(rows["Gone"]["redeployBlockedReason"], "not_seen")

    def test_when_the_helper_would_not_redeploy_no_row_offers_it(self):
        report = collector.build_report(self.rows(), [], [], {}, now=NOW, can_redeploy=False)
        rows = {row["name"]: row for row in report["coolify"]}
        self.assertEqual(rows["Example (web)"]["actions"], [])
        self.assertEqual(rows["Example (web)"]["redeployBlockedReason"], "not_allowed")

    def test_the_first_row_with_a_uuid_decides_like_the_helper(self):
        # The helper reads the first row with a uuid and refuses when that row is not a labelled
        # application, so a labelled application behind it must not claim a redeploy.
        first = {"uuid": "uuid1", "uuidSource": "label", "name": "Shared (db)", "type": "service"}
        second = {
            "uuid": "uuid1",
            "uuidSource": "label",
            "name": "Shared (web)",
            "type": "application",
        }
        report = collector.build_report([first, second], [], [], {}, now=NOW, can_redeploy=True)
        rows = {row["name"]: row for row in report["coolify"]}
        self.assertEqual(rows["Shared (db)"]["actions"], [])
        self.assertNotIn("redeployBlockedReason", rows["Shared (db)"])
        self.assertEqual(rows["Shared (web)"]["actions"], [])
        self.assertEqual(rows["Shared (web)"]["redeployBlockedReason"], "first_row_refused")

    def test_a_row_whose_state_could_not_be_read_claims_no_redeploy(self):
        unknown = {
            "uuid": "uuid1",
            "uuidSource": "label",
            "name": "Shared (web)",
            "type": "application",
            "status": "unknown",
        }
        running = dict(unknown, name="Shared (worker)", status="running")
        report = collector.build_report([unknown, running], [], [], {}, now=NOW, can_redeploy=True)
        rows = {row["name"]: row for row in report["coolify"]}
        self.assertEqual(rows["Shared (web)"]["actions"], [])
        self.assertEqual(rows["Shared (web)"]["redeployBlockedReason"], "state_unknown")
        # The helper reads the first row with a uuid, so the running row behind it cannot claim one.
        self.assertEqual(rows["Shared (worker)"]["actions"], [])
        self.assertEqual(rows["Shared (worker)"]["redeployBlockedReason"], "first_row_refused")

    def test_a_refused_first_row_agrees_with_the_real_helper(self):
        # Every row offers a redeploy exactly when the helper's own check takes the first row
        # with its uuid, so changing that check turns this test red.
        unknown = {
            "uuid": "uuid-unknown",
            "uuidSource": "label",
            "name": "Unread (web)",
            "type": "application",
            "status": "unknown",
        }
        behind = dict(unknown, name="Unread (worker)", status="running")
        gone = {
            "uuid": "uuid-gone",
            "uuidSource": "label",
            "name": "Gone",
            "type": "application",
            "status": "absent",
            "absentSince": stamp(NOW),
        }
        report = collector.build_report(
            self.rows() + [unknown, behind],
            [],
            [],
            previous_map([gone]),
            now=NOW,
            can_redeploy=True,
        )
        spec = importlib.util.spec_from_file_location(
            "fleet_ops_control_for_test", HERE / "fleet-ops-control.py"
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        names = set()
        for row in report["coolify"]:
            names.add(row["name"])
            first = next(r for r in report["coolify"] if r["uuid"] == row["uuid"])
            accepted = helper.redeploy_refusal(first) is None
            self.assertEqual("redeploy" in row["actions"], accepted, row["name"])
        # The rows the rule is about really were in the loop.
        self.assertLessEqual({"Unread (web)", "Unread (worker)", "Gone"}, names)
        self.assertIn("not running", helper.redeploy_refusal(unknown))
        self.assertIn("not running", helper.redeploy_refusal(gone))

    def test_one_application_says_how_many_containers_a_redeploy_restarts(self):
        report = collector.build_report(self.rows(), [], [], {}, now=NOW, can_redeploy=True)
        self.assertEqual(
            report["applications"],
            [{"uuid": "uuid1", "name": "Example", "containers": 2, "services": ["web", "worker"]}],
        )

    def test_a_report_built_without_the_helpers_answer_says_nothing_about_actions(self):
        report = collector.build_report(self.rows(), [], [], {}, now=NOW)
        self.assertNotIn("actions", report["coolify"][0])
        self.assertNotIn("applications", report)

    def test_the_repo_allowlist_grants_redeploy_and_is_accepted_by_the_helper(self):
        control = HERE / "fleet-ops-control.json"
        self.assertTrue(collector.redeploy_allowed(control))
        self.assertIsNone(collector.services_problem(control))


class ContainerSignalTests(unittest.TestCase):
    """A crash loop, a clean finish and a missing health check each read as what they are."""

    def row(self, **fields):
        return collector.collect_coolify(
            run_command=FakeDocker(containers=[extended_line(**fields)])
        )[0]

    def test_a_crash_loop_is_restarting(self):
        row = self.row(
            state="restarting",
            health="",
            restart_count="12",
            exit_code="1",
            restart_policy="always",
        )
        self.assertEqual(row["status"], "restarting")
        self.assertEqual(row["restartCount"], 12)

    def test_a_clean_exit_nothing_was_asked_to_restart_is_finished(self):
        row = self.row(state="exited", health="", exit_code="0", restart_policy="no")
        self.assertEqual(row["status"], "finished")
        self.assertEqual(row["exitCode"], 0)

    def test_every_other_exit_is_exited(self):
        # `on-failure` asks docker to keep it alive through a failure, so a clean exit leaves a
        # service down and nothing in the report says it was a one-shot job.
        for code, policy in (
            ("1", "no"),
            ("0", "always"),
            ("137", "no"),
            ("0", ""),
            ("0", "on-failure"),
        ):
            row = self.row(state="exited", health="", exit_code=code, restart_policy=policy)
            self.assertEqual(row["status"], "exited", (code, policy))
            self.assertEqual(row["exitCode"], int(code))

    def test_an_out_of_memory_kill_is_said(self):
        row = self.row(state="exited", health="", exit_code="137", oom_killed="true")
        self.assertTrue(row["oomKilled"])
        self.assertEqual(row["exitCode"], 137)

    def test_a_running_row_says_whether_it_has_a_health_check(self):
        self.assertEqual(self.row(health="healthy")["health"], "healthy")
        self.assertEqual(self.row(health="starting")["health"], "starting")
        self.assertEqual(self.row(health="")["health"], "none")
        self.assertNotIn("exitCode", self.row(health="healthy"))

    def test_a_row_says_its_restart_policy(self):
        self.assertEqual(self.row(restart_policy="always")["restartPolicy"], "always")

    def test_a_docker_answer_without_the_new_fields_reads_as_not_measured(self):
        row = collector.collect_coolify(
            run_command=FakeDocker(containers=[inspect_line(state="exited", health="")])
        )[0]
        self.assertEqual(row["status"], "exited")
        self.assertNotIn("restartCount", row)
        self.assertNotIn("exitCode", row)
        self.assertNotIn("oomKilled", row)

    def test_the_inspect_format_asks_for_the_new_fields(self):
        for field in ("RestartCount", "ExitCode", "OOMKilled", "RestartPolicy.Name"):
            self.assertIn(field, collector.INSPECT_FORMAT)

    def test_every_time_in_a_row_is_whole_seconds(self):
        row = self.row(started="2026-11-04T19:26:46.54450348Z")
        self.assertEqual(row["updatedAt"], "2026-11-04T19:26:46Z")

    def test_a_time_is_respelled_or_left_out(self):
        self.assertEqual(
            collector.whole_second_utc("2026-11-04T19:26:46.510195485Z"), "2026-11-04T19:26:46Z"
        )
        self.assertEqual(collector.whole_second_utc("2026-11-04T19:26:46Z"), "2026-11-04T19:26:46Z")
        self.assertEqual(
            collector.whole_second_utc("2026-11-04T14:26:46.123456-05:00"), "2026-11-04T19:26:46Z"
        )
        for bad in ("", None, "soon", "2026-13-40T00:00:00Z"):
            self.assertIsNone(collector.whole_second_utc(bad), bad)

    def test_the_resource_receipt_is_respelled_too(self):
        spelled = collector.respell_policy_times(
            {
                "generatedAt": "2026-11-04T19:26:46.123456Z",
                "observed": {"updatedAt": "2026-11-04T19:26:47.123456Z", "mode": "normal"},
                "status": "applied",
            }
        )
        self.assertEqual(spelled["generatedAt"], "2026-11-04T19:26:46Z")
        self.assertEqual(spelled["observed"]["updatedAt"], "2026-11-04T19:26:47Z")
        self.assertEqual(spelled["observed"]["mode"], "normal")
        self.assertEqual(collector.respell_policy_times(None), None)

    def test_the_dead_field_is_gone(self):
        self.assertNotIn("service_name", (HERE / "fleet-server-status.py").read_text())


class RemovedCheckoutTests(unittest.TestCase):
    """Files of a removed checkout never hold the report red."""

    def layout(self, tmp):
        root = Path(tmp) / "Code"
        kept = root / "Kept"
        (kept / "config").mkdir(parents=True)
        (kept / "AGENTS.md").write_text("x\n")
        return root, kept

    def previous_for(self, *paths):
        return {str(path): {"path": str(path), "sha256": "aaaa"} for path in paths}

    def test_a_removed_checkout_is_one_line_and_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, kept = self.layout(tmp)
            before = self.previous_for(
                kept / "AGENTS.md",
                root / "Gone" / "AGENTS.md",
                root / "Gone" / ".github" / "workflows" / "ci.yml",
                root / "Other" / "CLAUDE.md",
            )
            rows, _total = collector.collect_files(root, before, now=NOW)
            self.assertEqual([row["path"] for row in rows], [str(kept / "AGENTS.md")])
            self.assertEqual(rows.removed, {"Gone": 2, "Other": 1})
            notes = collector.merge_removed_checkouts(rows.removed, [], root, now=NOW)
            self.assertEqual(
                notes,
                [
                    {"repo": "Gone", "files": 2, "noticedAt": stamp(NOW)},
                    {"repo": "Other", "files": 1, "noticedAt": stamp(NOW)},
                ],
            )

    def test_the_old_rows_of_a_removed_checkout_are_cleared_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, kept = self.layout(tmp)
            gone = str(root / "Gone" / "AGENTS.md")
            rows, _total = collector.collect_files(
                root, {gone: {"path": gone, "drift": "missing"}}, now=NOW
            )
            self.assertNotIn(gone, [row["path"] for row in rows])
            self.assertEqual(rows.removed, {"Gone": 1})

    def test_the_line_ages_out_and_keeps_its_first_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _kept = self.layout(tmp)
            first = collector.merge_removed_checkouts({"Gone": 2}, [], root, now=NOW)
            kept = collector.merge_removed_checkouts({}, first, root, now=NOW + 6 * DAY)
            self.assertEqual(kept, first)
            self.assertEqual(
                collector.merge_removed_checkouts({}, first, root, now=NOW + 7 * DAY), []
            )

    def test_a_checkout_that_comes_back_loses_its_line_and_its_files_are_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _kept = self.layout(tmp)
            first = collector.merge_removed_checkouts({"Gone": 1}, [], root, now=NOW)
            (root / "Gone").mkdir()
            (root / "Gone" / "AGENTS.md").write_text("x\n")
            self.assertEqual(collector.merge_removed_checkouts({}, first, root, now=NOW + 60), [])
            rows, _total = collector.collect_files(root, {}, now=NOW + 60)
            drift = {Path(row["path"]).parent.name: row["drift"] for row in rows}
            self.assertEqual(drift["Gone"], "new")

    def test_malformed_lines_are_not_carried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _kept = self.layout(tmp)
            for previous in (
                None,
                "Gone",
                [7],
                [{"repo": "../up", "files": 1, "noticedAt": stamp(NOW)}],
                [{"repo": "Gone", "files": "two", "noticedAt": stamp(NOW)}],
                [{"repo": "", "files": 1, "noticedAt": stamp(NOW)}],
            ):
                self.assertEqual(
                    collector.merge_removed_checkouts({}, previous, root, now=NOW), [], previous
                )

    def test_an_empty_or_gone_code_folder_is_a_failure_and_not_a_removal(self):
        # An unmounted volume leaves an empty folder, which says nothing about any checkout.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            root.mkdir()
            path = root / "Gone" / "AGENTS.md"
            before = self.previous_for(path)
            rows, _total = collector.collect_files(root, before, now=NOW)
            self.assertFalse(rows.root_readable)
            self.assertEqual(rows.removed, {})
            self.assertEqual(rows[0]["drift"], "missing")
            self.assertEqual(rows[0]["missingSince"], stamp(NOW))
            root.rmdir()
            rows, _total = collector.collect_files(root, before, now=NOW)
            self.assertFalse(rows.root_readable)
            self.assertEqual(rows[0]["drift"], "missing")

    def test_a_file_missing_from_a_live_checkout_says_since_when_and_ages_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, kept = self.layout(tmp)
            path = str(kept / "CLAUDE.md")
            rows, _total = collector.collect_files(
                root, {path: {"path": path, "sha256": "aaaa"}}, now=NOW
            )
            row = next(row for row in rows if row["path"] == path)
            self.assertEqual(row["drift"], "missing")
            self.assertEqual(row["missingSince"], stamp(NOW))
            # The next run keeps the first time.
            rows, _total = collector.collect_files(root, {path: row}, now=NOW + DAY)
            again = next(row for row in rows if row["path"] == path)
            self.assertEqual(again["missingSince"], stamp(NOW))
            rows, _total = collector.collect_files(root, {path: row}, now=NOW + 7 * DAY)
            self.assertNotIn(path, [row["path"] for row in rows])

    def test_an_old_missing_row_starts_its_seven_days_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, kept = self.layout(tmp)
            path = str(kept / "CLAUDE.md")
            rows, _total = collector.collect_files(
                root, {path: {"path": path, "drift": "missing"}}, now=NOW
            )
            row = next(row for row in rows if row["path"] == path)
            self.assertEqual(row["missingSince"], stamp(NOW))


class FileListTests(unittest.TestCase):
    """The list is read from where config can be, and config goes first."""

    def test_the_walk_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            (repo / ".github" / "workflows").mkdir(parents=True)
            (repo / "config").mkdir()
            (repo / "AGENTS.md").write_text("x\n")
            (repo / ".github" / "workflows" / "ci.yml").write_text("x\n")
            (repo / "config" / "one.json").write_text("{}\n")
            with mock.patch.object(collector.os, "walk", side_effect=AssertionError("walked")):
                paths, total = collector.watched_paths(root)
            self.assertEqual(total, 3)
            self.assertEqual(
                [str(path.relative_to(repo)) for path in paths],
                ["AGENTS.md", ".github/workflows/ci.yml", "config/one.json"],
            )

    def test_a_linked_folder_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            outside = Path(tmp) / "outside"
            (outside / "workflows").mkdir(parents=True)
            (outside / "workflows" / "ci.yml").write_text("x\n")
            (outside / "one.json").write_text("{}\n")
            (repo / "AGENTS.md").write_text("x\n")
            (repo / "config").symlink_to(outside)
            (repo / ".github").symlink_to(outside)
            paths, total = collector.watched_paths(root)
            self.assertEqual([path.name for path in paths], ["AGENTS.md"])
            self.assertEqual(total, 1)

    def test_the_bound_spends_itself_on_config_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            for name in ("README.md", "package.json", "project.yml"):
                (repo / name).write_text("x\n")
            paths, total = collector.watched_paths(root, limit=1)
            self.assertEqual([path.name for path in paths], ["project.yml"])
            self.assertEqual(total, 3)

    def test_the_summary_names_the_checkouts_the_bound_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            for name, count in (("Busy", 3), ("Quiet", 1)):
                (root / name / "config").mkdir(parents=True)
                for index in range(count):
                    (root / name / "config" / ("c%d.json" % index)).write_text("{}\n")
            rows, total = collector.collect_files(root, {}, limit=3)
            self.assertEqual(total, 4)
            self.assertEqual(rows.cut, ["Busy"])
            self.assertEqual(
                collector.summarize_files(total, 3, rows.cut),
                {"found": 4, "limit": 3, "truncated": True, "cutCheckouts": ["Busy"]},
            )

    def test_the_default_bound_covers_the_real_list(self):
        # The server had 291 watched files, against a default of 200 that cut 91 of them.
        self.assertGreaterEqual(collector.parse_args([]).file_limit, 291)


class ScriptedSystemctl:
    """A systemctl that answers each `show` for the units it is asked about, in order."""

    def __init__(self, units=None, failed=None, refuse=()):
        self.units = units or {}
        self.failed = failed or {}
        self.refuse = refuse
        self.calls = []

    def __call__(self, command, timeout=30):
        self.calls.append(command)
        if command[0] != "systemctl":
            return None
        scope = "user" if "--user" in command else "system"
        if scope in self.refuse:
            return None
        if "list-units" in command:
            return "".join(
                "%s loaded failed failed The %s unit\n" % (unit, unit)
                for unit in self.failed.get(scope, [])
            )
        names = [part for part in command[1:] if not part.startswith("-") and part != "show"]
        return "\n\n".join(self.units[(scope, name)] for name in names) + "\n"


def properties(**values):
    return "\n".join("%s=%s" % pair for pair in values.items())


FROZEN = types.SimpleNamespace(time=lambda: NOW, monotonic=lambda: 1000.0)


class TimerTests(unittest.TestCase):
    """A timer says whether its job ran, and whether it worked."""

    def control(self, tmp, units=None, watch=None):
        path = Path(tmp) / "fleet-ops-control.json"
        document = {"schemaVersion": 1, "server": {"units": units or []}}
        if watch is not None:
            document["server"]["watch"] = watch
        path.write_text(json.dumps(document))
        return path

    def timer(self, **extra):
        values = dict(
            Id="example-sync.timer",
            LoadState="loaded",
            ActiveState="active",
            SubState="waiting",
            StateChangeTimestamp="@1793826224",
            ActiveEnterTimestamp="@1793820403",
            Triggers="example-sync.service",
            LastTriggerUSec="@1793826219",
            NextElapseUSecRealtime="",
            NextElapseUSecMonotonic="1h 47min 34.737538s",
        )
        values.update(extra)
        return properties(**values)

    def job(self, **extra):
        values = dict(
            Id="example-sync.service",
            ActiveState="failed",
            SubState="failed",
            Result="exit-code",
            ExecMainCode="1",
            ExecMainStatus="1",
            ExecMainStartTimestamp="@1793826219",
            ExecMainExitTimestamp="@1793826224",
        )
        values.update(extra)
        return properties(**values)

    def collect(self, timer=None, job=None, unit="example-sync.timer", scope="system"):
        with tempfile.TemporaryDirectory() as tmp:
            control = self.control(tmp, [{"unit": unit, "scope": scope, "actions": ["restart"]}])
            systemctl = ScriptedSystemctl(
                {
                    (scope, unit): timer or self.timer(Id=unit),
                    (scope, "example-sync.service"): job or self.job(),
                }
            )
            rows = collector.collect_services(control, run_command=systemctl, clock=FROZEN)
        return rows[0], systemctl

    def test_a_failing_job_shows_on_its_active_timer(self):
        row, _ = self.collect()
        self.assertEqual(row["activeState"], "active")
        self.assertEqual(row["subState"], "waiting")
        self.assertEqual(row["job"], "example-sync.service")
        self.assertEqual(row["lastRunAt"], stamp(1793826219))
        self.assertEqual(row["lastResult"], "exit-code")
        self.assertEqual(row["lastExitCode"], 1)
        self.assertEqual(row["lastFinishedAt"], stamp(1793826224))

    def test_a_job_killed_by_a_signal_has_no_exit_code(self):
        # systemd puts the signal number in ExecMainStatus, so 15 here is SIGTERM.
        for code in ("2", "3", ""):
            row, _ = self.collect(
                job=self.job(Result="timeout", ExecMainCode=code, ExecMainStatus="15")
            )
            self.assertEqual(row["lastResult"], "timeout", code)
            self.assertNotIn("lastExitCode", row, code)

    def test_a_job_started_after_its_timer_last_fired_ran_then(self):
        # Something other than the timer can start the job.
        row, _ = self.collect(job=self.job(ExecMainStartTimestamp="@1793826300"))
        self.assertEqual(row["lastRunAt"], stamp(1793826300))

    def test_a_job_start_older_than_the_trigger_does_not_move_the_last_run_back(self):
        row, _ = self.collect(job=self.job(ExecMainStartTimestamp="@1793826000"))
        self.assertEqual(row["lastRunAt"], stamp(1793826219))

    def test_a_job_start_alone_gives_the_last_run(self):
        row, _ = self.collect(timer=self.timer(LastTriggerUSec=""))
        self.assertEqual(row["lastRunAt"], stamp(1793826219))

    def test_a_job_that_worked_says_so(self):
        row, _ = self.collect(
            job=self.job(
                ActiveState="inactive", SubState="dead", Result="success", ExecMainStatus="0"
            )
        )
        self.assertEqual(row["lastResult"], "success")
        self.assertNotIn("lastExitCode", row)

    def test_a_job_that_has_not_run_since_boot_has_no_result(self):
        # systemd still says `success` here, which is a default and not an outcome.
        row, _ = self.collect(
            job=self.job(
                ActiveState="inactive",
                SubState="dead",
                Result="success",
                ExecMainStatus="0",
                ExecMainStartTimestamp="",
                ExecMainExitTimestamp="",
            )
        )
        self.assertNotIn("lastResult", row)
        self.assertEqual(row["lastRunAt"], stamp(1793826219))

    def test_a_job_running_now_has_no_result(self):
        for state, sub in (
            ("activating", "start"),
            ("active", "running"),
            ("deactivating", "stop"),
        ):
            row, _ = self.collect(job=self.job(ActiveState=state, SubState=sub, Result="success"))
            self.assertNotIn("lastResult", row, state)
            self.assertTrue(row["jobRunning"], state)

    def test_a_oneshot_that_stays_active_has_finished(self):
        row, _ = self.collect(
            job=self.job(
                ActiveState="active", SubState="exited", Result="success", ExecMainStatus="0"
            )
        )
        self.assertEqual(row["lastResult"], "success")
        self.assertNotIn("jobRunning", row)

    def test_the_next_run_comes_from_the_monotonic_clock(self):
        row, _ = self.collect()
        expected = int(NOW + (3600 + 47 * 60 + 34.737538) - 1000.0)
        self.assertEqual(row["nextRunAt"], stamp(expected))

    def test_the_next_run_comes_from_the_calendar(self):
        row, _ = self.collect(
            timer=self.timer(NextElapseUSecRealtime="@1793826900", NextElapseUSecMonotonic="0")
        )
        self.assertEqual(row["nextRunAt"], stamp(1793826900))

    def test_a_timer_with_both_runs_at_whichever_comes_first(self):
        row, _ = self.collect(
            timer=self.timer(
                NextElapseUSecRealtime="@%d" % (NOW + 60),
                NextElapseUSecMonotonic="1h",
            )
        )
        self.assertEqual(row["nextRunAt"], stamp(NOW + 60))

    def test_nothing_scheduled_has_no_next_run(self):
        row, _ = self.collect(
            timer=self.timer(NextElapseUSecRealtime="", NextElapseUSecMonotonic="infinity")
        )
        self.assertNotIn("nextRunAt", row)

    def test_a_time_span_is_read_or_refused(self):
        self.assertAlmostEqual(collector.parse_timespan("1h 47min 34.5s"), 6454.5)
        self.assertAlmostEqual(collector.parse_timespan("2w 1d"), 15 * 86400)
        self.assertAlmostEqual(collector.parse_timespan("500ms"), 0.5)
        self.assertAlmostEqual(collector.parse_timespan("1month"), 2629800)
        for value in ("", None, "0", "infinity", "soon", "5 parsecs", "1h soon"):
            self.assertIsNone(collector.parse_timespan(value), value)

    def test_an_active_timer_is_active_since_it_was_activated(self):
        # Its own state change is only the moment it last fired.
        row, _ = self.collect()
        self.assertEqual(row["since"], stamp(1793820403))

    def test_a_stopped_timer_reads_since_it_stopped(self):
        row, _ = self.collect(timer=self.timer(ActiveState="inactive", SubState="dead"))
        self.assertEqual(row["since"], stamp(1793826224))

    def test_the_job_is_read_with_its_own_property_list(self):
        _row, systemctl = self.collect()
        self.assertEqual(len(systemctl.calls), 2)
        job_call = systemctl.calls[1]
        self.assertEqual(job_call[-1], "example-sync.service")
        self.assertIn("--property=" + ",".join(collector.JOB_PROPERTIES), job_call)

    def test_a_job_that_cannot_be_read_leaves_no_result(self):
        class Refuses(ScriptedSystemctl):
            def __call__(self, command, timeout=30):
                if command[-1].endswith(".service"):
                    return None
                return super().__call__(command, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            control = self.control(
                tmp, [{"unit": "example-sync.timer", "scope": "system", "actions": ["start"]}]
            )
            rows = collector.collect_services(
                control,
                run_command=Refuses({("system", "example-sync.timer"): self.timer()}),
                clock=FROZEN,
            )
        self.assertNotIn("lastResult", rows[0])
        self.assertEqual(rows[0]["job"], "example-sync.service")

    def test_the_collectors_own_timer_does_not_report_this_run(self):
        running = self.timer(
            Id=collector.SELF_TIMER, SubState="running", Triggers="example-sync.service"
        )
        row, _ = self.collect(
            timer=running,
            job=self.job(ActiveState="activating", SubState="start", Result="success"),
            unit=collector.SELF_TIMER,
            scope="user",
        )
        self.assertEqual(row["subState"], "waiting")
        self.assertTrue(row["selfObserved"])
        self.assertNotIn("jobRunning", row)
        self.assertNotIn("lastResult", row)
        self.assertEqual(row["since"], stamp(1793820403))
        self.assertEqual(row["lastRunAt"], stamp(1793826219))

    def test_another_timer_running_now_still_reads_running(self):
        row, _ = self.collect(timer=self.timer(SubState="running"))
        self.assertEqual(row["subState"], "running")
        self.assertNotIn("selfObserved", row)

    def test_a_service_row_gets_no_timer_fields(self):
        block = properties(
            Id="cron.service",
            LoadState="loaded",
            ActiveState="active",
            SubState="running",
            Triggers="",
        )
        row, _ = self.collect(timer=block, unit="cron.service")
        for field in ("lastRunAt", "nextRunAt", "job", "lastResult"):
            self.assertNotIn(field, row)


class WatchTests(unittest.TestCase):
    """A watched unit is read and granted nothing."""

    def control(self, tmp, units, watch):
        path = Path(tmp) / "fleet-ops-control.json"
        path.write_text(
            json.dumps({"schemaVersion": 1, "server": {"units": units, "watch": watch}})
        )
        return path

    def test_a_watched_row_has_no_actions(self):
        units = [
            {"unit": "fleet-server-status.timer", "scope": "user", "actions": ["start", "restart"]}
        ]
        watch = [{"unit": "docker.service", "scope": "system"}]
        systemctl = ScriptedSystemctl(
            {
                ("user", "fleet-server-status.timer"): show_block("fleet-server-status.timer"),
                ("system", "docker.service"): show_block("docker.service"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(
                self.control(tmp, units, watch), run_command=systemctl
            )
        by_unit = {row["unit"]: row for row in rows}
        self.assertEqual(by_unit["docker.service"]["actions"], [])
        self.assertFalse(by_unit["docker.service"]["controllable"])
        self.assertTrue(by_unit["docker.service"]["watched"])
        self.assertEqual(
            by_unit["fleet-server-status.timer"]["actions"], ["start", "restart", "logs"]
        )
        self.assertTrue(by_unit["fleet-server-status.timer"]["controllable"])
        self.assertNotIn("watched", by_unit["fleet-server-status.timer"])

    def test_a_watch_entry_can_never_widen_what_the_helper_accepts(self):
        watch = [{"unit": "docker.service", "scope": "system", "actions": ["stop"]}]
        with tempfile.TemporaryDirectory() as tmp:
            control = self.control(tmp, [], watch)
            self.assertEqual(collector.accepted_units(control), {})
            self.assertEqual(collector.unit_actions(control), {})

    def test_a_unit_in_both_lists_is_one_row_and_keeps_its_actions(self):
        units = [{"unit": "cron.service", "scope": "system", "actions": ["restart"]}]
        watch = [{"unit": "cron.service", "scope": "system"}]
        systemctl = ScriptedSystemctl({("system", "cron.service"): show_block("cron.service")})
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(
                self.control(tmp, units, watch), run_command=systemctl
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actions"], ["restart", "logs"])
        self.assertNotIn("watched", rows[0])

    def test_a_watched_unit_this_host_does_not_have_is_left_out(self):
        watch = [{"unit": "nothing-here.service", "scope": "system"}]
        block = properties(
            Id="nothing-here.service",
            LoadState="not-found",
            ActiveState="inactive",
            SubState="dead",
        )
        systemctl = ScriptedSystemctl({("system", "nothing-here.service"): block})
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(self.control(tmp, [], watch), run_command=systemctl)
        self.assertEqual(rows, [])

    def test_a_watched_unit_in_a_scope_that_cannot_answer_reads_unknown(self):
        watch = [{"unit": "docker.service", "scope": "system"}]
        systemctl = ScriptedSystemctl(refuse=("system",))
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(self.control(tmp, [], watch), run_command=systemctl)
        self.assertEqual(rows[0]["activeState"], "unknown")
        self.assertEqual(rows[0]["actions"], [])

    def test_a_bad_watch_entry_is_skipped(self):
        watch = [
            {"unit": "--now", "scope": "system"},
            {"unit": "docker.service", "scope": "root"},
            {"unit": 7, "scope": "system"},
            "docker.service",
            {"unit": "docker.service", "scope": "system"},
            {"unit": "docker.service", "scope": "system"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                collector.watched_units(self.control(tmp, [], watch)),
                [("docker.service", "system")],
            )
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"schemaVersion": 1, "server": {"watch": "docker.service"}}))
            self.assertEqual(collector.watched_units(path), [])

    def test_a_row_carries_the_role_and_what_stopping_it_costs(self):
        units = [
            {
                "unit": "a.timer",
                "scope": "user",
                "actions": ["stop"],
                "role": "reporting",
                "stopConsequence": "Stopping this stops the report.",
            },
            {
                "unit": "b.timer",
                "scope": "user",
                "actions": ["stop"],
                "role": "boss",
                "stopConsequence": "x" * 500,
            },
        ]
        systemctl = ScriptedSystemctl(
            {("user", "a.timer"): show_block("a.timer"), ("user", "b.timer"): show_block("b.timer")}
        )
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.collect_services(self.control(tmp, units, []), run_command=systemctl)
        by_unit = {row["unit"]: row for row in rows}
        self.assertEqual(by_unit["a.timer"]["role"], "reporting")
        self.assertEqual(by_unit["a.timer"]["stopConsequence"], "Stopping this stops the report.")
        # A value of the wrong shape is left out rather than carried into the report.
        self.assertNotIn("role", by_unit["b.timer"])
        self.assertNotIn("stopConsequence", by_unit["b.timer"])

    def test_the_repo_allowlist_keeps_the_report_timer_and_grants_a_watch_nothing(self):
        control = HERE / "fleet-ops-control.json"
        accepted = collector.accepted_units(control)
        self.assertEqual(accepted["fleet-server-status.timer"], "user")
        watched = [unit for unit, _scope in collector.watched_units(control)]
        self.assertTrue(any(unit.endswith(".timer") for unit in watched))
        self.assertTrue(any(unit.endswith(".service") for unit in watched))
        for unit in watched:
            self.assertNotIn(unit, accepted, unit)
        notes = collector.unit_notes(control)
        self.assertEqual(notes[("fleet-server-status.timer", "user")]["role"], "reporting")
        # One sentence, in plain owner copy, naming the place and never a provider.
        for note in notes.values():
            text = note["stopConsequence"]
            self.assertLessEqual(len(text), collector.STOP_CONSEQUENCE_LIMIT)
            self.assertEqual(text.count("."), 1, text)
            self.assertNotIn("Hetzner", text)


class FailedUnitTests(unittest.TestCase):
    """A failed unit shows, whether or not anything names it."""

    def block(self, unit, result="exit-code", status="1", code="1"):
        return properties(
            Id=unit,
            Description="The %s unit" % unit,
            Result=result,
            ExecMainCode=code,
            ExecMainStatus=status,
            StateChangeTimestamp="@1793826224",
        )

    def test_a_failed_unit_in_either_scope_appears(self):
        systemctl = ScriptedSystemctl(
            {
                ("system", "example-sync.service"): self.block("example-sync.service"),
                # A timed out stop is killed by SIGTERM, which is a signal and not an exit 15.
                ("user", "other.service"): self.block("other.service", "timeout", "15", "2"),
            },
            failed={"system": ["example-sync.service"], "user": ["other.service"]},
        )
        rows, answered = collector.collect_failed_units(systemctl)
        self.assertEqual(answered, ["system", "user"])
        self.assertEqual(
            rows,
            [
                {
                    "unit": "example-sync.service",
                    "scope": "system",
                    "description": "The example-sync.service unit",
                    "since": stamp(1793826224),
                    "result": "exit-code",
                    "exitCode": 1,
                },
                {
                    "unit": "other.service",
                    "scope": "user",
                    "description": "The other.service unit",
                    "since": stamp(1793826224),
                    "result": "timeout",
                },
            ],
        )

    def test_a_status_with_no_exit_behind_it_is_not_an_exit_code(self):
        for code in ("2", "3", "", "0"):
            systemctl = ScriptedSystemctl(
                {("system", "killed.service"): self.block("killed.service", "signal", "9", code)},
                failed={"system": ["killed.service"]},
            )
            rows, _ = collector.collect_failed_units(systemctl)
            self.assertEqual(rows[0]["result"], "signal", code)
            self.assertNotIn("exitCode", rows[0], code)

    def test_nothing_failed_is_an_empty_list(self):
        self.assertEqual(
            collector.collect_failed_units(ScriptedSystemctl()), ([], ["system", "user"])
        )

    def test_no_scope_answering_is_not_the_same_as_nothing_failed(self):
        rows, answered = collector.collect_failed_units(
            ScriptedSystemctl(refuse=("system", "user"))
        )
        self.assertIsNone(rows)
        self.assertEqual(answered, [])
        self.assertNotIn("failedUnits", collector.build_report([], [], [], {}, failed_units=rows))

    def test_one_scope_answering_is_reported_as_partial(self):
        rows, answered = collector.collect_failed_units(ScriptedSystemctl(refuse=("user",)))
        self.assertEqual((rows, answered), ([], ["system"]))
        entry = collector.collection_report([], [], None, answered, [{"pid": 1}], [])["failedUnits"]
        self.assertEqual(
            entry, {"ok": True, "partial": True, "errorCode": "systemctl_scope_unreadable"}
        )

    def test_a_failed_unit_whose_details_cannot_be_read_still_appears(self):
        class Refuses(ScriptedSystemctl):
            def __call__(self, command, timeout=30):
                if "show" in command:
                    return None
                return super().__call__(command, timeout)

        rows, _ = collector.collect_failed_units(Refuses(failed={"system": ["nmbd.service"]}))
        self.assertEqual(rows, [{"unit": "nmbd.service", "scope": "system"}])

    def test_a_line_that_is_not_a_unit_is_ignored(self):
        def answer(command, timeout=30):
            if "list-units" in command:
                return "0 loaded units listed.\n--now failed\n"
            return ""

        self.assertEqual(collector.collect_failed_units(answer)[0], [])

    def test_an_instance_name_with_systemds_escape_is_read(self):
        name = "systemd-fsck@dev-disk-by\\x2dlabel.service"
        self.assertTrue(collector.FAILED_UNIT_RE.fullmatch(name))
        self.assertTrue(collector.FAILED_UNIT_RE.fullmatch("session-3.scope"))
        self.assertFalse(collector.FAILED_UNIT_RE.fullmatch("-H.service"))

    def test_the_report_carries_the_list(self):
        rows = [{"unit": "nmbd.service", "scope": "system"}]
        document = collector.build_report([], [], [], {}, failed_units=rows)
        self.assertEqual(document["failedUnits"], rows)


class CollectionTests(unittest.TestCase):
    """A half that could not be read says so, in a fixed code."""

    def report(
        self,
        coolify=None,
        services=None,
        problem=None,
        scopes=("system", "user"),
        processes=({"pid": 1},),
        files=None,
    ):
        return collector.collection_report(
            [] if coolify is None else coolify,
            services or [],
            problem,
            list(scopes),
            list(processes),
            [] if files is None else files,
        )

    def test_everything_read_says_so(self):
        for half, entry in self.report().items():
            self.assertEqual(entry, {"ok": True, "partial": False}, half)

    def test_docker_down_is_not_a_quiet_host(self):
        rows = collector.collect_coolify(run_command=lambda *a, **k: None)
        self.assertEqual(
            self.report(coolify=rows)["coolify"],
            {"ok": False, "partial": False, "errorCode": "docker_unreadable"},
        )
        quiet = collector.collect_coolify(run_command=FakeDocker())
        self.assertEqual(self.report(coolify=quiet)["coolify"], {"ok": True, "partial": False})

    def test_stats_that_cannot_be_read_are_partial(self):
        class NoStats(FakeDocker):
            def __call__(self, command, timeout=30):
                if command[:2] == ["docker", "stats"]:
                    return None
                return super().__call__(command, timeout)

        rows = collector.collect_coolify(run_command=NoStats(containers=[inspect_line()]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            self.report(coolify=rows)["coolify"],
            {"ok": True, "partial": True, "errorCode": "docker_stats_unreadable"},
        )

    def test_the_allowlist_says_why_it_gave_no_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            self.assertEqual(collector.services_problem(missing), "allowlist_unreadable")
            refused = Path(tmp) / "refused.json"
            refused.write_text(
                json.dumps({"schemaVersion": 1, "server": {"units": [{"unit": "a"}]}})
            )
            self.assertEqual(collector.services_problem(refused), "allowlist_refused")
        self.assertEqual(
            self.report(problem="allowlist_refused")["services"],
            {"ok": False, "partial": False, "errorCode": "allowlist_refused"},
        )

    def test_a_unit_left_unknown_is_partial(self):
        entry = self.report(services=[{"unit": "a.timer", "activeState": "unknown"}])["services"]
        self.assertEqual(
            entry, {"ok": True, "partial": True, "errorCode": "systemctl_scope_unreadable"}
        )

    def test_no_scope_answering_is_said(self):
        self.assertEqual(
            self.report(scopes=())["failedUnits"],
            {"ok": False, "partial": False, "errorCode": "systemctl_unreadable"},
        )

    def test_no_processes_is_said(self):
        self.assertEqual(
            self.report(processes=())["processes"],
            {"ok": False, "partial": False, "errorCode": "proc_unreadable"},
        )

    def test_the_files_half_says_what_went_wrong(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            rows, _total = collector.collect_files(root, {})
            self.assertEqual(
                self.report(files=rows)["files"],
                {"ok": False, "partial": False, "errorCode": "code_root_unreadable"},
            )
            for name in ("AGENTS.md", "CLAUDE.md"):
                (root / "Repo").mkdir(parents=True, exist_ok=True)
                (root / "Repo" / name).write_text("x\n")
            rows, _total = collector.collect_files(root, {}, limit=1)
            self.assertEqual(
                self.report(files=rows)["files"],
                {"ok": True, "partial": True, "errorCode": "files_truncated"},
            )


class ProcessHomeTests(unittest.TestCase):
    """A process says which container or unit it belongs to, from its cgroup alone."""

    def home(self, text, containers=None):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "42").mkdir()
            if text is not None:
                (proc / "42" / "cgroup").write_text(text)
            return collector.process_home(42, proc, containers)

    def test_a_container_is_named_by_its_row(self):
        identifier = "a" * 64
        text = "0::/system.slice/docker-%s.scope\n" % identifier
        self.assertEqual(self.home(text, {identifier: "example-app (web)"}), "example-app (web)")
        # A container this run did not name is still a container, and not a name it made up.
        self.assertEqual(self.home(text), "a container")

    def test_a_systemd_unit_is_named(self):
        self.assertEqual(self.home("0::/system.slice/docker.service\n"), "docker.service")
        self.assertEqual(
            self.home("0::/user.slice/user-1.slice/user@1.service/app.slice/mm.service\n"),
            "mm.service",
        )

    def test_a_process_in_nothing_named_has_no_home(self):
        self.assertIsNone(self.home("0::/user.slice/user-1.slice/session-3.scope\n"))
        self.assertIsNone(self.home(""))
        self.assertIsNone(self.home(None))

    def test_the_busiest_rows_carry_it_and_the_row_says_nothing_else_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            ProcessTests().make_proc(proc, 4242, "worker", 100, 50, 16)
            (proc / "4242" / "cgroup").write_text("0::/system.slice/worker.service\n")
            rows = collector.collect_processes(limit=5, interval=0.0, proc=proc)
        self.assertEqual(rows[0]["belongsTo"], "worker.service")

    def test_the_sample_length_is_in_the_report_and_finer_than_before(self):
        self.assertEqual(collector.parse_args([]).cpu_interval, 2.0)
        document = collector.build_report([], [], [], {}, process_sample_seconds=2.0)
        self.assertEqual(document["processSampleSeconds"], 2.0)


class HostSignalTests(unittest.TestCase):
    """Inodes, the boot time and a pending reboot."""

    def test_a_disk_says_its_inodes_when_it_counts_them(self):
        class WithInodes:
            f_frsize = 4096
            f_bsize = 4096
            f_blocks = 100
            f_bfree = 50
            f_bavail = 40
            f_files = 1000
            f_favail = 900

        usage = collector.disk_usage("/", lambda _mount: WithInodes)
        self.assertEqual(usage["inodesTotal"], 1000)
        self.assertEqual(usage["inodesFree"], 900)
        # A file system that counts no inodes has no reading, never a zero.
        WithInodes.f_files = 0
        usage = collector.disk_usage("/", lambda _mount: WithInodes)
        self.assertNotIn("inodesTotal", usage)
        self.assertNotIn("inodesFree", usage)

    def test_the_boot_time_comes_from_the_kernel_and_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = make_proc_files(Path(tmp) / "proc")
            (proc / "stat").write_text("cpu 1 2 3\nbtime 1793820391\nprocesses 7\n")
            self.assertEqual(
                collector.collect_host_metrics(
                    proc=proc, statvfs=FakeStatvfs({}), cpu_count=lambda: None
                )["bootedAt"],
                stamp(1793820391),
            )
            (proc / "stat").write_text("cpu 1 2 3\n")
            self.assertIsNone(collector.booted_at(proc))
            (proc / "stat").write_text("btime 0\n")
            self.assertIsNone(collector.booted_at(proc))

    def test_a_pending_reboot_is_said_and_its_absence_claims_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = make_proc_files(Path(tmp) / "proc")
            flag = Path(tmp) / "reboot-required"
            metrics = collector.collect_host_metrics(
                proc=proc, statvfs=FakeStatvfs({}), cpu_count=lambda: None, reboot_flag=flag
            )
            self.assertNotIn("rebootRequired", metrics)
            flag.write_text("*** System restart required ***\n")
            metrics = collector.collect_host_metrics(
                proc=proc, statvfs=FakeStatvfs({}), cpu_count=lambda: None, reboot_flag=flag
            )
            self.assertTrue(metrics["rebootRequired"])


class StrongerWriteTests(unittest.TestCase):
    """The report the whole panel rests on gets the strongest write in the fleet."""

    def test_a_linked_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real)
            with self.assertRaises(OSError):
                collector.write_report(link / "report.json", {})
            self.assertEqual(list(real.iterdir()), [])

    def test_a_folder_swapped_for_a_link_after_the_check_is_refused(self):
        # Kody, PR 1477:  the folder is opened with O_NOFOLLOW and used through that descriptor,
        # so passing the early check is not enough to write through a link.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real)
            with mock.patch.object(collector.Path, "is_symlink", return_value=False):
                with self.assertRaises(OSError):
                    collector.write_report(link / "report.json", {})
            self.assertEqual(list(real.iterdir()), [])


class WholeRunTests(unittest.TestCase):
    """Whole runs of `main`, each reading the report the one before it wrote."""

    def run_once(self, target, code, control, docker):
        argv = [
            "--output",
            str(target),
            "--code-root",
            str(code),
            "--control",
            str(control),
            "--process-limit",
            "1",
            "--cpu-interval",
            "0",
        ]

        def answer(command, timeout=30):
            if command[0] == "docker":
                return docker(command, timeout)
            if "list-units" in command:
                return "example-sync.service loaded failed failed sync\n"
            if "show" in command:
                names = [
                    part for part in command[1:] if not part.startswith("-") and part != "show"
                ]
                return "\n\n".join(show_block(name) for name in names) + "\n"
            return None

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collector.main(argv, run_command=answer), 0)
        return json.loads(target.read_text())

    def test_the_old_report_shape_becomes_the_truth_in_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = root / "Code"
            (code / "Kept").mkdir(parents=True)
            (code / "Kept" / "AGENTS.md").write_text("x\n")
            control = root / "control.json"
            control.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "server": {
                            "units": [{"unit": "a.timer", "scope": "user", "actions": ["restart"]}],
                            "watch": [{"unit": "docker.service", "scope": "system"}],
                            "coolify": {"redeploy": True},
                        },
                    }
                )
            )
            target = root / "state" / "server-status.json"
            target.parent.mkdir()
            # What the collector wrote before this change.
            gone = str(code / "Removed" / "AGENTS.md")
            target.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "tool": "fleet-server-status",
                        "coolify": [
                            {
                                "uuid": "uuid1",
                                "name": "App (uuid1-1)",
                                "type": "application",
                                "uuidSource": "label",
                                "status": "running",
                            },
                            {
                                "uuid": "uuid1",
                                "name": "App (uuid1-0)",
                                "type": "application",
                                "uuidSource": "label",
                                "status": "absent",
                            },
                        ],
                        "files": [{"path": gone, "drift": "missing"}],
                    }
                )
            )
            docker = FakeDocker(
                containers=[
                    inspect_line(
                        container="uuid1-2",
                        label_type="application",
                        application_uuid="uuid1",
                        resource_name="App",
                        compose_service="uuid1-2",
                    )
                ]
            )
            document = self.run_once(target, code, control, docker)
            self.assertEqual(document["schemaVersion"], 1)
            self.assertIn(document["hostRole"], ("mac", "server"))
            self.assertEqual([row["name"] for row in document["coolify"]], ["App"])
            self.assertEqual(document["coolify"][0]["actions"], ["redeploy"])
            self.assertEqual(
                [(note["repo"], note["files"]) for note in document["removedCheckouts"]],
                [("Removed", 1)],
            )
            self.assertEqual([row["drift"] for row in document["files"]], ["new"])
            self.assertEqual(document["failedUnits"][0]["unit"], "example-sync.service")
            self.assertEqual(
                [row["unit"] for row in document["services"]], ["docker.service", "a.timer"]
            )
            self.assertEqual(document["applications"][0]["containers"], 1)
            self.assertEqual(document["collection"]["services"], {"ok": True, "partial": False})
            first_noticed = document["removedCheckouts"][0]["noticedAt"]

            # The next run reads that report, and nothing moves or grows.
            again = self.run_once(target, code, control, docker)
            self.assertEqual([row["name"] for row in again["coolify"]], ["App"])
            self.assertEqual(again["removedCheckouts"][0]["noticedAt"], first_noticed)
            self.assertEqual(again["removedCheckouts"][0]["files"], 1)
            self.assertEqual([row["drift"] for row in again["files"]], ["unchanged"])

            # The container goes away for real, and the row says since when.
            quiet = FakeDocker()
            third = self.run_once(target, code, control, quiet)
            self.assertEqual(third["coolify"][0]["status"], "absent")
            since = third["coolify"][0]["absentSince"]
            self.assertLessEqual(abs(collector.seconds_from_report_time(since) - time.time()), 60)
            self.assertEqual(third["coolify"][0]["redeployBlockedReason"], "not_seen")
            fourth = self.run_once(target, code, control, quiet)
            self.assertEqual(fourth["coolify"][0]["absentSince"], since)


class FixtureScriptTests(unittest.TestCase):
    """The committed live-shape fixture keeps the shape and none of the names."""

    def module(self):
        spec = importlib.util.spec_from_file_location(
            "make_server_status_fixture", HERE / "make-server-status-fixture.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def report(self):
        return {
            "schemaVersion": 1,
            "tool": "fleet-server-status",
            "generatedAt": "2026-11-04T21:00:28Z",
            "host": "secret-host-name",
            "hostRole": "server",
            "coolify": [
                {
                    "uuid": "secretuuid",
                    "uuidSource": "label",
                    "name": "secret-app (secret-web)",
                    "service": "secret-web",
                    "container": "secret-web-secretuuid-2026",
                    "image": "secret-image:latest",
                    "type": "application",
                    "status": "running",
                    "updatedAt": "2026-11-04T19:26:46Z",
                    "actions": ["redeploy"],
                },
                {
                    "uuid": "secretuuid",
                    "uuidSource": "label",
                    "name": "secret-app (secret-db)",
                    "service": "secret-db",
                    "container": "secret-db-secretuuid-2026",
                    "type": "application",
                    "status": "running",
                    "actions": ["redeploy"],
                },
                {
                    "uuid": "secret-box",
                    "uuidSource": "name",
                    "name": "secret-box",
                    "container": "secret-box",
                    "type": "standalone",
                    "status": "running",
                    "actions": [],
                },
            ],
            "applications": [
                {
                    "uuid": "secretuuid",
                    "name": "secret-app",
                    "containers": 2,
                    "services": ["secret-db", "secret-web"],
                }
            ],
            "processes": [
                {
                    "pid": 4242,
                    "owner": "secret-owner",
                    "name": "secret-process",
                    "belongsTo": "secret-app (secret-web)",
                    "cpuPercent": 2.5,
                }
            ],
            "files": [
                {
                    "path": "/secret/Code/SecretRepo/AGENTS.md",
                    "bytes": 1,
                    "sha256": "secretdigest",
                    "drift": "unchanged",
                    "modifiedAt": "2026-11-04T19:26:46Z",
                },
                {
                    "path": "/secret/Code/SecretRepo/.github/workflows/secret-flow.yml",
                    "bytes": 1,
                    "sha256": "secretdigest",
                    "drift": "unchanged",
                },
                {
                    "path": "/secret/Code/SecretRepo/config/secret-config.json",
                    "bytes": 1,
                    "sha256": "secretdigest",
                    "drift": "unchanged",
                },
            ],
            "filesSummary": {
                "found": 3,
                "limit": 2,
                "truncated": True,
                "cutCheckouts": ["SecretRepo"],
            },
            "removedCheckouts": [
                {"repo": "SecretGone", "files": 2, "noticedAt": "2026-11-04T21:00:00Z"}
            ],
            "services": [
                {
                    "unit": "secret-timer.timer",
                    "scope": "user",
                    "description": "secret words",
                    "activeState": "active",
                    "job": "secret-timer.service",
                    "lastResult": "success",
                    "controllable": True,
                    "actions": ["restart", "logs"],
                }
            ],
            "failedUnits": [
                {
                    "unit": "secret-sync.service",
                    "scope": "system",
                    "description": "secret words",
                    "result": "exit-code",
                    "exitCode": 1,
                }
            ],
            "hostMetrics": {
                "disks": [
                    {"mount": "/", "totalBytes": 1},
                    {"mount": "/mnt/secret-volume", "totalBytes": 1},
                ]
            },
            "resourcePolicy": {"policyRevision": "secretrevision", "status": "applied"},
            "collection": {"coolify": {"ok": True, "partial": False}},
        }

    def test_no_real_name_survives(self):
        text = json.dumps(self.module().sanitize_report(self.report()))
        self.assertNotIn("secret", text.lower())

    def test_the_shape_survives(self):
        result = self.module().sanitize_report(self.report())
        rows = result["coolify"]
        self.assertEqual(len(rows), 3)
        # Two rows of one application still share a uuid and a base name.
        self.assertEqual(rows[0]["uuid"], rows[1]["uuid"])
        self.assertEqual(rows[0]["name"].split(" ")[0], rows[1]["name"].split(" ")[0])
        self.assertNotEqual(rows[0]["service"], rows[1]["service"])
        self.assertTrue(rows[0]["name"].endswith("(%s)" % rows[0]["service"]))
        # A stand-in uuid is still the row's own name, which the redeploy rule reads.
        self.assertEqual(rows[2]["uuid"], rows[2]["name"])
        self.assertEqual(rows[0]["actions"], ["redeploy"])
        self.assertEqual(rows[0]["updatedAt"], "2026-11-04T19:26:46Z")
        self.assertEqual(result["applications"][0]["uuid"], rows[0]["uuid"])
        paths = [row["path"] for row in result["files"]]
        self.assertTrue(paths[0].endswith("/AGENTS.md"))
        self.assertIn("/.github/workflows/", paths[1])
        self.assertTrue(paths[1].endswith(".yml"))
        self.assertIn("/config/", paths[2])
        self.assertTrue(paths[2].endswith(".json"))
        self.assertEqual(result["services"][0]["unit"].rpartition(".")[2], "timer")
        self.assertEqual(result["services"][0]["job"].rpartition(".")[2], "service")
        self.assertEqual(result["hostMetrics"]["disks"][0]["mount"], "/")

    def test_the_committed_fixture_is_sanitized(self):
        path = HERE.parent / "muster" / "MusterTests" / "Fixtures" / "server_status_live_shape.json"
        document = json.loads(path.read_text())
        self.assertEqual(document["host"], "example-server")
        self.assertEqual(document["schemaVersion"], 1)
        self.module().validate_report(document)
        # The rule is what a name must look like, so the test itself names nothing real.
        self.assertNotRegex(path.read_text(), r"/(?:Users|home|root)/|\.ts\.net\b")
        for row in document["coolify"]:
            self.assertRegex(row["uuid"], r"\A(?:app|uuid)-\d+\Z")
            self.assertRegex(row["name"], r"\Aapp-\d+(?: \(service-\d+\))?\Z")
            self.assertRegex(row.get("container", "container-1"), r"\Acontainer-\d+\Z")
            self.assertRegex(row.get("image", "image-1"), r"\Aimage-\d+\Z")
        for row in document["files"]:
            self.assertTrue(row["path"].startswith("/srv/example/checkout-"), row["path"])
        for row in document["services"] + document["failedUnits"]:
            self.assertRegex(row["unit"], r"\Aunit-\d+\.[a-z]+\Z")
            self.assertNotIn("description", row)
        for row in document["processes"]:
            self.assertRegex(row.get("name", "process-1"), r"\Aprocess-\d+\Z")
            self.assertRegex(row.get("owner", "owner-1"), r"\Aowner-\d+\Z")

    def test_a_section_it_does_not_know_is_refused_before_anything_is_written(self):
        # A section the sanitizer does not know would be copied through with its names in it.
        for change, named in (
            ({"newSection": []}, "newSection"),
            ({"coolify": {}}, "coolify"),
            ({"coolify": ["row"]}, "coolify[0]"),
            ({"coolify": [{"uuid": "u", "name": "n", "newField": 1}]}, "coolify[0].newField"),
            ({"coolify": [{"uuid": 7, "name": "n"}]}, "coolify[0].uuid"),
            ({"files": [{"path": 1}]}, "files[0].path"),
            ({"applications": [{"uuid": "u", "name": "n", "services": "web"}]}, "services"),
            ({"filesSummary": {"cutCheckouts": "all"}}, "filesSummary.cutCheckouts"),
            ({"hostMetrics": {"disks": [{"mount": 1}]}}, "hostMetrics.disks[0].mount"),
            ({"resourcePolicy": {"policyRevision": 1}}, "resourcePolicy.policyRevision"),
            ({"schemaVersion": "1"}, "schemaVersion"),
            ({"host": 1}, "host"),
        ):
            with self.assertRaises(ValueError) as caught:
                self.module().validate_report(dict(self.report(), **change))
            self.assertIn(named, str(caught.exception))
        for missing in ("schemaVersion", "tool", "host", "coolify", "processes", "files"):
            report = self.report()
            del report[missing]
            with self.assertRaises(ValueError) as caught:
                self.module().validate_report(report)
            self.assertIn(missing, str(caught.exception))
        with self.assertRaises(ValueError):
            self.module().validate_report(["not", "an", "object"])

    def test_a_good_report_passes_and_a_bad_one_writes_nothing(self):
        self.module().validate_report(self.report())
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.json"
            target = Path(tmp) / "fixture.json"
            source.write_text(json.dumps(self.report()))
            self.assertEqual(self.module().main([str(source), str(target)]), 0)
            self.assertTrue(target.exists())
            target.unlink()
            source.write_text(json.dumps(dict(self.report(), newSection=[])))
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(self.module().main([str(source), str(target)]), 1)
            self.assertIn("newSection", errors.getvalue())
            self.assertFalse(target.exists())


def git(repo, *arguments):
    """Run git in a test checkout,  away from this machine's own git settings."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *arguments,
        ],
        check=True,
        capture_output=True,
        env=environment,
    )


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class IntentTests(unittest.TestCase):
    """Each file is compared with its checkout's own `origin/main`,  and never guessed clean."""

    def checkout(self, root):
        repo = root / "Example"
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / "AGENTS.md").write_text("one\n")
        (repo / "CLAUDE.md").write_text("same\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "one")
        git(repo, "update-ref", collector.INTENT_REF, "HEAD")
        return repo

    def intents(self, root, git_command=None):
        rows, _total = collector.collect_files(root, {}, git=git_command)
        return {Path(row["path"]).name: (row["intent"], row.get("intentReason")) for row in rows}

    def test_a_file_that_matches_origin_main_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            self.checkout(root)
            self.assertEqual(
                self.intents(root),
                {"AGENTS.md": ("clean", None), "CLAUDE.md": ("clean", None)},
            )

    def test_an_uncommitted_edit_and_an_untracked_file_are_local_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = self.checkout(root)
            (repo / "AGENTS.md").write_text("edited here\n")
            (repo / "INFISICAL.md").write_text("only here\n")
            intents = self.intents(root)
            self.assertEqual(intents["AGENTS.md"], ("localEdit", None))
            self.assertEqual(intents["INFISICAL.md"], ("localEdit", None))
            self.assertEqual(intents["CLAUDE.md"], ("clean", None))

    def test_a_local_commit_that_is_not_on_main_is_a_local_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = self.checkout(root)
            (repo / "AGENTS.md").write_text("committed here only\n")
            git(repo, "commit", "-q", "-am", "local")
            self.assertEqual(self.intents(root)["AGENTS.md"], ("localEdit", None))

    def test_a_checkout_that_has_not_caught_up_is_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = self.checkout(root)
            (repo / "AGENTS.md").write_text("two\n")
            git(repo, "commit", "-q", "-am", "two")
            git(repo, "update-ref", collector.INTENT_REF, "HEAD")
            git(repo, "reset", "-q", "--hard", "HEAD~1")
            intents = self.intents(root)
            self.assertEqual(intents["AGENTS.md"], ("behind", None))
            self.assertEqual(intents["CLAUDE.md"], ("clean", None))

    def test_no_origin_main_is_unknown_and_never_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = self.checkout(root)
            git(repo, "update-ref", "-d", collector.INTENT_REF)
            self.assertEqual(
                self.intents(root),
                {
                    "AGENTS.md": ("unknown", "no_origin_main"),
                    "CLAUDE.md": ("unknown", "no_origin_main"),
                },
            )

    def test_a_folder_that_is_not_a_checkout_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            (root / "Plain").mkdir(parents=True)
            (root / "Plain" / "AGENTS.md").write_text("x\n")
            self.assertEqual(self.intents(root), {"AGENTS.md": ("unknown", "not_a_checkout")})

    def test_git_that_cannot_answer_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            self.checkout(root)
            intents = self.intents(root, git_command=lambda repo, arguments, timeout=10: None)
            self.assertEqual(set(intents.values()), {("unknown", "git_failed")})

    def test_a_spent_budget_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = self.checkout(root)
            contents = {str(repo / "AGENTS.md"): collector.blob_ids(b"one\n")}
            intents = collector.file_intents(root, contents, budget=-1)
            self.assertEqual(list(intents.values()), [("unknown", "git_budget")])

    def test_git_is_only_asked_read_only_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = self.checkout(root)
            (repo / "AGENTS.md").write_text("two\n")
            git(repo, "commit", "-q", "-am", "two")
            git(repo, "update-ref", collector.INTENT_REF, "HEAD")
            git(repo, "reset", "-q", "--hard", "HEAD~1")
            asked = []

            def recording(repo, arguments, timeout=10):
                asked.append(arguments[0])
                return collector.git_output(repo, arguments, timeout)

            self.intents(root, git_command=recording)
            self.assertLessEqual(set(asked), {"rev-parse", "ls-tree", "merge-base"})


class ChangedAtTests(unittest.TestCase):
    """`changedAt` is when the content last changed,  carried across reports."""

    def rows(self, root, previous, now):
        rows, _total = collector.collect_files(root, previous, now=now)
        return {row["path"]: row for row in rows}

    def test_it_is_kept_across_runs_and_moves_only_when_the_content_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            path = repo / "AGENTS.md"
            path.write_text("one\n")
            os.utime(path, (NOW - DAY, NOW - DAY))
            first = self.rows(root, {}, NOW)
            self.assertEqual(first[str(path)]["changedAt"], collector.iso_from_timestamp(NOW - DAY))

            # Touched,  and the content is the same:  it does not move.
            os.utime(path, (NOW - 60, NOW - 60))
            second = self.rows(root, first, NOW + 900)
            self.assertEqual(second[str(path)]["changedAt"], first[str(path)]["changedAt"])
            self.assertEqual(
                second[str(path)]["modifiedAt"], collector.iso_from_timestamp(NOW - 60)
            )

            # The content changes:  it moves to when the new content was written.
            path.write_text("two\n")
            os.utime(path, (NOW + 1000, NOW + 1000))
            third = self.rows(root, second, NOW + 1800)
            self.assertEqual(
                third[str(path)]["changedAt"], collector.iso_from_timestamp(NOW + 1000)
            )
            fourth = self.rows(root, third, NOW + 2700)
            self.assertEqual(fourth[str(path)]["changedAt"], third[str(path)]["changedAt"])

    def test_a_time_from_the_future_is_never_later_than_now(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Code"
            repo = root / "Example"
            repo.mkdir(parents=True)
            path = repo / "AGENTS.md"
            path.write_text("one\n")
            os.utime(path, (NOW + DAY, NOW + DAY))
            rows = self.rows(root, {}, NOW)
            self.assertEqual(rows[str(path)]["changedAt"], collector.iso_from_timestamp(NOW))

    def test_an_older_report_without_it_starts_from_the_modification_time(self):
        before = {"sha256": "aaaa", "drift": "unchanged"}
        self.assertEqual(
            collector.changed_at(before, "aaaa", NOW - 5, NOW),
            collector.iso_from_timestamp(NOW - 5),
        )


def unit_file_block(unit, load="loaded", fragment="", drop_ins="", triggers=""):
    return "\n".join(
        [
            "Id=" + unit,
            "LoadState=" + load,
            "FragmentPath=" + fragment,
            "DropInPaths=" + drop_ins,
            "Triggers=" + triggers,
        ]
    )


class HostConfigTests(unittest.TestCase):
    """The host's own config surface,  by digest and never by content."""

    SECRET = "CONTENT-THAT-MUST-NOT-LEAK"

    def layout(self, tmp):
        tmp = Path(tmp)
        source = tmp / "source"
        (source / "scripts" / "systemd").mkdir(parents=True)
        timer_text = "[Timer]\nOnCalendar=daily\n"
        (source / "scripts" / "systemd" / "example.timer").write_text(timer_text)
        (source / "scripts" / "fleet_resource_control.py").write_text("print('%s')\n" % self.SECRET)
        (source / "scripts" / "fleet-ops-control.json").write_text('{"schemaVersion": 1}\n')
        etc = tmp / "etc"
        (etc / "example.timer.d").mkdir(parents=True)
        (etc / "example.timer").write_text(timer_text)
        (etc / "example.timer.d" / "override.conf").write_text("Environment=X=%s\n" % self.SECRET)
        broker = tmp / "broker"
        (broker / "fleet_secrets").mkdir(parents=True)
        (broker / "fleet_secrets" / "core.py").write_text(self.SECRET)
        (broker / "fleet_resource_control.py").write_text("print('%s')\n" % self.SECRET)
        (broker / "fleet-ops-control.json").write_text('{"word": "%s"}\n' % self.SECRET)
        control = source / "scripts" / "fleet-ops-control.json"
        control.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "server": {"watch": [{"unit": "gone.service", "scope": "system"}]},
                }
            )
        )
        return source, etc, broker, control

    def systemd(self, etc):
        asked = []

        def answer(command, timeout=30):
            asked.append(command)
            if "show" not in command:
                return None
            names = [part for part in command[1:] if not part.startswith("-") and part != "show"]
            blocks = []
            for name in names:
                if "--user" in command or name == "gone.service":
                    blocks.append(unit_file_block(name, load="not-found"))
                elif name == "example.timer":
                    blocks.append(
                        unit_file_block(
                            name,
                            fragment=str(etc / "example.timer"),
                            drop_ins=str(etc / "example.timer.d" / "override.conf"),
                            triggers="example.service",
                        )
                    )
                elif name == "example.service":
                    blocks.append(unit_file_block(name, fragment=str(etc / "example.service")))
                else:
                    blocks.append(unit_file_block(name, load="not-found"))
            return "\n\n".join(blocks) + "\n"

        return answer, asked

    def collect(self, tmp, previous=None, now=NOW, platform="linux"):
        source, etc, broker, control = self.layout(tmp) if previous is None else self.paths(tmp)
        answer, _asked = self.systemd(etc)
        return collector.collect_host_config(
            control,
            previous or {},
            answer,
            source_root=source,
            broker_dir=broker,
            now=now,
            platform=platform,
        )

    def paths(self, tmp):
        tmp = Path(tmp)
        source = tmp / "source"
        return source, tmp / "etc", tmp / "broker", source / "scripts" / "fleet-ops-control.json"

    def test_digests_are_reported_and_contents_never_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.collect(tmp)
            text = json.dumps(rows)
            self.assertNotIn(self.SECRET, text)
            self.assertNotIn("OnCalendar", text)
            self.assertNotIn("fleet_secrets", text)
            etc = Path(tmp) / "etc"
            by_path = {row["path"]: row for row in rows}

            timer = by_path[str(etc / "example.timer")]
            self.assertEqual(
                (timer["kind"], timer["unit"], timer["scope"], timer["state"]),
                ("unit", "example.timer", "system", "present"),
            )
            self.assertEqual(timer["sha256"], collector.digest_of(etc / "example.timer"))
            self.assertEqual(timer["source"], "scripts/systemd/example.timer")
            self.assertIs(timer["matchesSource"], True)

            drop_in = by_path[str(etc / "example.timer.d" / "override.conf")]
            self.assertEqual((drop_in["kind"], drop_in["state"]), ("dropIn", "present"))
            self.assertRegex(drop_in["sha256"], r"\A[0-9a-f]{16}\Z")
            self.assertNotIn("source", drop_in)

            # The service the timer starts is looked up too,  and its file is not there.
            self.assertEqual(by_path[str(etc / "example.service")]["state"], "missing")
            self.assertFalse(any(row.get("unit") == "gone.service" for row in rows))

            broker = Path(tmp) / "broker"
            allowlist = by_path[str(broker / "fleet-ops-control.json")]
            self.assertEqual(allowlist["kind"], "allowlist")
            self.assertIs(allowlist["matchesSource"], False)
            code = by_path[str(broker / "fleet_resource_control.py")]
            self.assertEqual((code["kind"], code["state"]), ("broker", "present"))
            self.assertIs(code["matchesSource"], True)
            self.assertEqual(by_path[str(broker / "fleet-ops-control.py")]["state"], "missing")

    def test_changed_at_is_sticky_for_host_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.collect(tmp)
            installed = Path(tmp) / "etc" / "example.timer"
            previous = {row["path"]: row for row in first}
            os.utime(installed, (NOW + 60, NOW + 60))
            second = self.collect(tmp, previous=previous, now=NOW + 900)
            again = {row["path"]: row for row in second}
            self.assertEqual(
                again[str(installed)]["changedAt"], previous[str(installed)]["changedAt"]
            )
            installed.write_text("[Timer]\nOnCalendar=weekly\n")
            os.utime(installed, (NOW + 1000, NOW + 1000))
            third = {row["path"]: row for row in self.collect(tmp, previous=again, now=NOW + 1800)}
            self.assertEqual(
                third[str(installed)]["changedAt"], collector.iso_from_timestamp(NOW + 1000)
            )
            self.assertIs(third[str(installed)]["matchesSource"], False)

    def test_a_broker_that_is_not_installed_is_one_absent_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = collector.broker_rows(Path(tmp) / "nothing", {}, NOW)
            self.assertEqual(
                rows, [{"kind": "broker", "path": str(Path(tmp) / "nothing"), "state": "absent"}]
            )

    def test_a_mac_has_no_broker_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.collect(tmp, platform="darwin")
            self.assertFalse(any(row["kind"] in ("broker", "allowlist") for row in rows))

    def test_a_separate_allowlist_is_compared_with_the_repo_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, etc, broker, _control = self.layout(tmp)
            other = Path(tmp) / "installed-control.json"
            other.write_text('{"schemaVersion": 1}\n')
            answer, _asked = self.systemd(etc)
            rows = collector.collect_host_config(
                other, {}, answer, source_root=source, broker_dir=broker, now=NOW, platform="linux"
            )
            row = [row for row in rows if row["path"] == str(other)][0]
            self.assertEqual(row["kind"], "allowlist")
            self.assertEqual(row["source"], "scripts/fleet-ops-control.json")
            self.assertIn("matchesSource", row)

    def test_the_whole_run_carries_both_times_from_report_to_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = root / "Code"
            (code / "Kept").mkdir(parents=True)
            (code / "Kept" / "AGENTS.md").write_text("x\n")
            os.utime(code / "Kept" / "AGENTS.md", (NOW, NOW))
            control = root / "control.json"
            control.write_text(json.dumps({"schemaVersion": 1, "server": {}}))
            broker = root / "broker"
            broker.mkdir()
            (broker / "fleet_resource_control.py").write_text("x = 1\n")
            target = root / "state" / "server-status.json"
            argv = [
                "--output",
                str(target),
                "--code-root",
                str(code),
                "--control",
                str(control),
                "--broker-dir",
                str(broker),
                "--process-limit",
                "1",
                "--cpu-interval",
                "0",
            ]

            def run_once():
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(collector.main(argv, run_command=FakeDocker()), 0)
                return json.loads(target.read_text())

            with mock.patch.object(collector.sys, "platform", "linux"):
                first = run_once()
                os.utime(code / "Kept" / "AGENTS.md", (NOW + 60, NOW + 60))
                os.utime(broker / "fleet_resource_control.py", (NOW + 60, NOW + 60))
                second = run_once()
            self.assertEqual(first["files"][0]["intent"], "unknown")
            self.assertEqual(first["files"][0]["changedAt"], second["files"][0]["changedAt"])
            self.assertEqual(first["files"][0]["drift"], "new")
            self.assertEqual(second["files"][0]["drift"], "unchanged")
            path = str(broker / "fleet_resource_control.py")
            before = [row for row in first["hostConfig"] if row["path"] == path][0]
            after = [row for row in second["hostConfig"] if row["path"] == path][0]
            self.assertEqual(before["changedAt"], after["changedAt"])
            self.assertNotEqual(before["modifiedAt"], after["modifiedAt"])


class FixtureNewFieldTests(unittest.TestCase):
    """The fixture script knows the new fields,  and scrubs the names in `hostConfig`."""

    def test_new_fields_pass_and_host_names_are_replaced(self):
        spec = importlib.util.spec_from_file_location(
            "make_server_status_fixture", HERE / "make-server-status-fixture.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        report = {
            "schemaVersion": 1,
            "tool": "fleet-server-status",
            "host": "secret-host",
            "coolify": [],
            "processes": [],
            "files": [
                {
                    "path": "/secret/Code/SecretRepo/AGENTS.md",
                    "sha256": "secretdigest",
                    "drift": "unchanged",
                    "changedAt": "2026-11-04T19:26:46Z",
                    "intent": "unknown",
                    "intentReason": "no_origin_main",
                }
            ],
            "hostConfig": [
                {
                    "kind": "unit",
                    "path": "/home/secret/.config/systemd/user/secret.timer",
                    "unit": "secret.timer",
                    "scope": "user",
                    "state": "present",
                    "sha256": "secretdigest",
                    "changedAt": "2026-11-04T19:26:46Z",
                    "source": "scripts/systemd/secret.timer",
                    "matchesSource": True,
                }
            ],
        }
        module.validate_report(report)
        result = module.sanitize_report(report)
        self.assertNotIn("secret", json.dumps(result).lower())
        row = result["hostConfig"][0]
        self.assertTrue(row["path"].endswith(".timer"))
        self.assertTrue(row["unit"].endswith(".timer"))
        self.assertIs(row["matchesSource"], True)
        self.assertEqual(result["files"][0]["intentReason"], "no_origin_main")


if __name__ == "__main__":
    unittest.main()
