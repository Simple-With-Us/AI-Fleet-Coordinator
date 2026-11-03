import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import coding_resources as r
import fleet_coding_memory as m
from test_fleet_coding_memory import cg, policy, proc, run_step


class CPUFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "cgroup"
        self.root.mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        (self.proc / "21").mkdir()
        (self.proc / "21" / "cgroup").write_text("0::/docker.scope\n")
        self.online = self.base / "online"
        self.online.write_text("0-3,8-11\n")
        self.paths = {}
        for name in ("user.slice", "docker.scope"):
            path = self.root / name
            path.mkdir()
            (path / "cpuset.cpus").write_text("0-3")
            (path / "cpuset.cpus.effective").write_text("0-3")
            (path / "cpu.weight").write_text("100")
            (path / "memory.max").write_text("2147483648")
            (path / "memory.swap.max").write_text("0")
            (path / "cpu.max").write_text("200000 100000")
            self.paths[name] = path
        self.state = {}
        self.calls = []
        self.kernel_writes = []
        self.persisted = []
        self.partial = False
        self.docker_configured = "0-3"
        self.controller = r.CPUManager(
            self.root, self.state, self.persist, self.runner, self.proc, self.online
        )
        self.policy = {**policy(), "cpu_cores": 5, "cpu_weight": 50}
        self.original_write = os.write
        patcher = mock.patch.object(r.os, "write", side_effect=self.kernel_write)
        patcher.start()
        self.addCleanup(patcher.stop)

    def kernel_write(self, fd, data):
        # A regular fixture file emulates cgroupfs replace-on-write behavior.
        identity = os.fstat(fd).st_ino
        for path in self.paths.values():
            for filename in ("cpuset.cpus", "cpu.weight"):
                control = path / filename
                if control.stat().st_ino != identity:
                    continue
                self.kernel_writes.append([str(control), data.decode().strip()])
                key = "unit:user.slice"
                field = "cpus" if filename == "cpuset.cpus" else "weight"
                journal = self.persisted[-1]["cpu_owned"][key]["fields"][field]
                self.assertEqual(journal["pending"], data.decode().strip())
                self.assertIn("baseline", journal)
                if self.partial and filename == "cpu.weight":
                    raise OSError("simulated kernel write failure")
                os.ftruncate(fd, 0)
                count = self.original_write(fd, data)
                if filename == "cpuset.cpus":
                    (path / "cpuset.cpus.effective").write_bytes(data)
                return count
        self.fail("unexpected non-CPU control write")

    def persist(self):
        self.persisted.append(json.loads(json.dumps(self.state)))

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(args, 0, "/user.slice\n", "")
        if args[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, f"/botfleet-vps-shared|21|{self.docker_configured}\n", ""
            )
        self.assertNotEqual(args[:2], ["systemctl", "set-property"])
        if args[:2] == ["docker", "update"]:
            path = self.paths["docker.scope"]
            (path / "cpuset.cpus").write_text(args[3])
            (path / "cpuset.cpus.effective").write_text(args[3])
        return subprocess.CompletedProcess(args, 0, "", "")

    def discover(self):
        self.controller.discover(self.policy)

    def writes(self):
        return self.kernel_writes + [
            args for args in self.calls if args[:2] == ["docker", "update"]
        ]

    def test_noncontiguous_online_ids_and_two_cpu_reserve(self):
        self.assertEqual(r.selected_cpus("0-3,8-11", 5), "0,1,2,3,8")
        self.assertEqual(r.selected_cpus("0-3,8-11", 6), "0,1,2,3,8,9")
        for count in (0, 7, 8, True):
            with self.assertRaises(ValueError):
                r.selected_cpus("0-3,8-11", count)

    def test_unit_kernel_writes_and_fixed_docker_argv_leave_hard_controls_unchanged(
        self,
    ):
        before = {
            path / key: (path / key).read_bytes()
            for path in self.paths.values()
            for key in ("memory.max", "memory.swap.max", "cpu.max")
        }
        self.discover()
        self.controller.apply(self.policy)
        self.assertEqual(
            self.kernel_writes,
            [
                [str(self.paths["user.slice"] / "cpuset.cpus"), "0,1,2,3,8"],
                [str(self.paths["user.slice"] / "cpu.weight"), "50"],
            ],
        )
        self.assertEqual(
            [args for args in self.calls if args[:2] == ["docker", "update"]],
            [["docker", "update", "--cpuset-cpus", "0,1,2,3,8", "botfleet-vps-shared"]],
        )
        self.assertFalse(any(args[:2] == ["systemctl", "set-property"] for args in self.calls))
        self.assertEqual(
            self.controller.observe(self.policy),
            {
                "cpuOnline": 8,
                "cpuGroups": 2,
                "cpuMismatches": 0,
                "cpuCores": 5,
                "cpuWeight": 50,
            },
        )
        first = self.persisted[0]["cpu_owned"]["unit:user.slice"]["fields"]
        self.assertEqual(first["cpus"]["pending"], "0,1,2,3,8")
        self.assertEqual(first["cpus"]["baseline"], "0,1,2,3")
        # Reproduce the live canary's CPUWeight 49 -> 50 transition, and rollback.
        self.controller.apply({**self.policy, "cpu_weight": 49})
        self.controller.apply(self.policy)
        self.controller.restore()
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertTrue(
            all(path.read_text() == "0" for path in before if path.name == "memory.swap.max")
        )

    def test_unit_write_rechecks_each_field_and_preserves_a_mid_write_external_edit(
        self,
    ):
        self.discover()
        original = self.kernel_write

        def outside_edit(fd, data):
            result = original(fd, data)
            if self.kernel_writes[-1][0].endswith("/cpuset.cpus"):
                (self.paths["user.slice"] / "cpu.weight").write_text("900")
            return result

        with mock.patch.object(r.os, "write", side_effect=outside_edit):
            self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 1)
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "900")
        fields = self.state["cpu_owned"]["unit:user.slice"]["fields"]
        self.assertEqual(fields["cpus"]["last"], "0,1,2,3,8")
        self.assertNotIn("last", fields["weight"])
        self.controller.restore()
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "900")

    def test_unit_write_refuses_a_symlink_control_without_touching_its_target(self):
        self.discover()
        control = self.paths["user.slice"] / "cpu.weight"
        target = self.paths["user.slice"] / "memory.max"
        target.write_text("100")
        control.unlink()
        control.symlink_to(target)
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 1)
        self.assertEqual(target.read_text(), "100")

    def test_inherited_empty_cpuset_is_restored_with_a_nonempty_kernel_write(self):
        path = self.paths["user.slice"]
        (path / "cpuset.cpus").write_text("")
        self.discover()
        self.controller.apply(self.policy)
        self.controller.restore()
        self.assertEqual((path / "cpuset.cpus").read_text().strip(), "")
        self.assertEqual(
            self.kernel_writes[-2:],
            [[str(path / "cpuset.cpus"), ""], [str(path / "cpu.weight"), "100"]],
        )

    def test_docker_inherited_configured_cpu_baseline_refuses_all_mutation(self):
        self.docker_configured = ""
        self.controller.discover(self.policy, excluded_units={"user.slice"})
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.missing, 1)
        self.assertEqual(self.controller.targets, {})
        self.assertEqual(self.writes(), [])
        self.assertNotIn("cpu_owned", self.state)
        self.assertEqual((self.paths["docker.scope"] / "cpuset.cpus").read_text(), "0-3")

    def test_docker_empty_kernel_cpu_baseline_also_refuses_mutation(self):
        (self.paths["docker.scope"] / "cpuset.cpus").write_text("")
        self.controller.discover(self.policy, excluded_units={"user.slice"})
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.missing, 1)
        self.assertEqual(self.writes(), [])
        self.assertNotIn("cpu_owned", self.state)

    def test_legacy_docker_empty_baseline_handback_remains_owned_and_failed(self):
        self.discover()
        self.controller.apply(self.policy)
        record = self.state["cpu_owned"]["docker:botfleet-vps-shared"]
        record["fields"]["cpus"]["baseline"] = ""
        self.calls.clear()
        self.kernel_writes.clear()
        self.controller.restore()
        self.assertEqual(self.controller.failures, 1)
        self.assertIn("docker:botfleet-vps-shared", self.state["cpu_owned"])
        self.assertFalse(any(args[:2] == ["docker", "update"] for args in self.calls))
        self.assertEqual((self.paths["docker.scope"] / "cpuset.cpus").read_text(), "0,1,2,3,8")

    def test_missing_cpuset_controller_is_unavailable_without_enabling_hierarchy(self):
        for path in self.paths.values():
            (path / "cpuset.cpus").unlink()
        self.discover()
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.missing, 2)
        self.assertEqual(self.controller.targets, {})
        self.assertEqual(self.writes(), [])
        self.assertFalse(any((path / "cpuset.cpus").exists() for path in self.paths.values()))
        self.assertNotIn("cpu_owned", self.state)
        self.state["last_status"] = {
            "mode": "normal",
            "policy_stale": False,
            "coding_groups": 2,
            "production_groups": 1,
        }
        self.assertEqual(
            r.public_snapshot(self.policy, self.state, self.controller)["status"], "unavailable"
        )

    def test_transient_discovery_failure_does_not_restore_or_forget_owned_controls(
        self,
    ):
        self.discover()
        self.controller.apply(self.policy)
        with mock.patch.object(self.controller, "_run", side_effect=RuntimeError("unavailable")):
            self.controller.discover(self.policy)
        self.assertEqual(self.controller.missing, 2)
        self.assertEqual(len(self.state["cpu_owned"]), 2)
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "50")

    def test_inode_recreation_never_restores_old_identity_baseline(self):
        self.discover()
        self.controller.apply(self.policy)
        self.state["cpu_owned"]["unit:user.slice"]["inode"] += 1
        self.calls.clear()
        self.kernel_writes.clear()
        self.controller.restore()
        self.assertFalse(self.kernel_writes)
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "50")

    def test_suspended_control_is_left_untouched(self):
        self.discover()
        self.controller.apply(self.policy, {"*"})
        self.assertEqual(self.writes(), [])
        self.controller.apply(self.policy)
        self.calls.clear()
        self.kernel_writes.clear()
        self.controller.restore({"*"})
        self.assertEqual(self.writes(), [])
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "50")

    def test_snapshot_sanitizes_malformed_private_report(self):
        self.discover()
        self.state["last_status"] = {
            "mode": "private-env-value",
            "coding_groups": "private-path",
            "control_suspended": True,
            "policy_stale": "private-secret",
        }
        result = r.public_snapshot(self.policy, self.state, self.controller)
        self.assertEqual(result["observed"]["mode"], "unknown")
        self.assertIs(type(result["observed"]["controlSuspended"]), int)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("private-", json.dumps(result))

    def test_old_policy_does_not_write_any_cpu_property(self):
        self.discover()
        self.controller.apply(policy())
        self.assertEqual(self.writes(), [])
        self.assertNotIn("cpu_owned", self.state)

    def test_unchanged_intent_never_reasserts_external_narrowing(self):
        self.discover()
        self.controller.apply(self.policy)
        self.calls.clear()
        self.kernel_writes.clear()
        path = self.paths["user.slice"]
        (path / "cpuset.cpus").write_text("0-1")
        (path / "cpuset.cpus.effective").write_text("0-1")
        self.controller.apply(self.policy)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.controller.observe(self.policy)["cpuMismatches"], 1)
        self.controller.apply({**self.policy, "cpu_cores": 6})
        self.assertFalse(self.kernel_writes)
        self.controller.restore()
        self.assertEqual((path / "cpuset.cpus").read_text().strip(), "0-1")

    def test_changed_owned_intent_and_graceful_restore(self):
        self.discover()
        self.controller.apply(self.policy)
        self.controller.apply({**self.policy, "cpu_cores": 6, "cpu_weight": 75})
        self.assertEqual(
            self.controller.observe({**self.policy, "cpu_cores": 6, "cpu_weight": 75})[
                "cpuMismatches"
            ],
            0,
        )
        self.controller.restore()
        self.assertEqual(self.state["cpu_owned"], {})
        self.assertEqual((self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0,1,2,3")
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")

    def test_removing_cpu_cores_restores_cpuset_and_preserves_owned_weight(self):
        self.discover()
        self.controller.apply(self.policy)
        self.calls.clear()
        self.kernel_writes.clear()
        changed = dict(self.policy)
        changed.pop("cpu_cores")
        self.controller.apply(changed)
        self.assertEqual((self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0,1,2,3")
        self.assertEqual(
            (self.paths["docker.scope"] / "cpuset.cpus").read_text().strip(), "0,1,2,3"
        )
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "50")
        self.assertEqual(set(self.state["cpu_owned"]), {"unit:user.slice"})
        self.assertEqual(set(self.state["cpu_owned"]["unit:user.slice"]["fields"]), {"weight"})
        self.assertFalse(any(path.endswith("/cpu.weight") for path, _ in self.kernel_writes))

    def test_removing_cpu_weight_restores_weight_and_preserves_owned_cpuset(self):
        self.discover()
        self.controller.apply(self.policy)
        self.calls.clear()
        self.kernel_writes.clear()
        changed = dict(self.policy)
        changed.pop("cpu_weight")
        self.controller.apply(changed)
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")
        self.assertEqual(
            (self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0,1,2,3,8"
        )
        self.assertEqual(set(self.state["cpu_owned"]["unit:user.slice"]["fields"]), {"cpus"})
        self.assertEqual(self.writes(), [[str(self.paths["user.slice"] / "cpu.weight"), "100"]])

    def test_removing_both_cpu_fields_restores_and_relinquishes_every_field(self):
        self.discover()
        self.controller.apply(self.policy)
        self.controller.apply(policy())
        self.assertEqual(self.state["cpu_owned"], {})
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")
        for path in self.paths.values():
            self.assertEqual((path / "cpuset.cpus").read_text().strip(), "0,1,2,3")
        self.calls.clear()
        self.kernel_writes.clear()
        self.controller.apply(policy())
        self.assertEqual(self.writes(), [])

    def test_removing_externally_changed_field_preserves_external_value(self):
        self.discover()
        self.controller.apply(self.policy)
        (self.paths["user.slice"] / "cpu.weight").write_text("900")
        changed = dict(self.policy)
        changed.pop("cpu_weight")
        self.calls.clear()
        self.kernel_writes.clear()
        self.controller.apply(changed)
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "900")
        self.assertEqual(self.writes(), [])
        self.assertEqual(set(self.state["cpu_owned"]["unit:user.slice"]["fields"]), {"cpus"})
        self.controller.restore()
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "900")

    def test_removal_uses_owned_inode_even_when_discovery_temporarily_fails(self):
        self.discover()
        self.controller.apply(self.policy)
        self.controller.targets = {}
        self.controller.apply(policy())
        self.assertEqual(self.state["cpu_owned"], {})
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")

    def test_partial_removal_retries_only_remaining_owned_field(self):
        self.discover()
        self.controller.apply(self.policy)
        self.partial = True
        self.controller.apply(policy())
        self.assertEqual(self.controller.failures, 1)
        self.assertEqual(set(self.state["cpu_owned"]["unit:user.slice"]["fields"]), {"weight"})
        self.assertEqual((self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0,1,2,3")
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "50")
        self.partial = False
        self.calls.clear()
        self.kernel_writes.clear()
        self.controller.apply(policy())
        self.assertEqual(self.state["cpu_owned"], {})
        self.assertEqual(self.writes(), [[str(self.paths["user.slice"] / "cpu.weight"), "100"]])

    def test_failed_first_cpu_write_retries_same_intent_on_next_tick(self):
        self.discover()
        attempts = 0

        def fail_first(fd, data):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("first CPU write failed before taking effect")
            return self.kernel_write(fd, data)

        with mock.patch.object(r.os, "write", side_effect=fail_first):
            self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 1)
        self.assertEqual((self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0-3")
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 0)
        self.assertEqual(self.controller.observe(self.policy)["cpuMismatches"], 0)
        self.assertEqual(
            self.kernel_writes,
            [
                [str(self.paths["user.slice"] / "cpuset.cpus"), "0,1,2,3,8"],
                [str(self.paths["user.slice"] / "cpu.weight"), "50"],
            ],
        )

    def test_partial_second_cpu_write_retries_only_the_remaining_field(self):
        self.discover()
        self.partial = True
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 1)
        self.partial = False
        self.kernel_writes.clear()
        self.calls.clear()
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 0)
        self.assertEqual(self.writes(), [[str(self.paths["user.slice"] / "cpu.weight"), "50"]])
        self.assertEqual(self.controller.observe(self.policy)["cpuMismatches"], 0)

    def test_failed_field_retry_preserves_an_outside_edit(self):
        self.discover()
        self.partial = True
        self.controller.apply(self.policy)
        self.partial = False
        (self.paths["user.slice"] / "cpu.weight").write_text("900")
        self.kernel_writes.clear()
        self.calls.clear()
        self.controller.apply(self.policy)
        self.assertEqual(self.writes(), [])
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "900")
        self.assertEqual(self.controller.observe(self.policy)["cpuMismatches"], 1)

    def test_partial_failure_records_only_actual_writes_and_rollback_is_safe(self):
        self.discover()
        self.partial = True
        self.controller.apply(self.policy)
        self.assertEqual(self.controller.failures, 1)
        fields = self.state["cpu_owned"]["unit:user.slice"]["fields"]
        self.assertEqual(fields["cpus"]["last"], "0,1,2,3,8")
        self.assertNotIn("last", fields["weight"])
        self.assertNotIn("pending", fields["weight"])
        self.partial = False
        self.controller.restore()
        self.assertEqual((self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0,1,2,3")
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")

    def test_restart_recovers_pending_partial_write_from_kernel(self):
        self.discover()
        self.controller.apply(self.policy)
        rec = self.state["cpu_owned"]["unit:user.slice"]["fields"]["cpus"]
        rec.pop("last")
        rec["pending"] = "0,1,2,3,8"
        resumed = r.CPUManager(
            self.root, self.state, self.persist, self.runner, self.proc, self.online
        )
        resumed.restore()
        self.assertEqual((self.paths["user.slice"] / "cpuset.cpus").read_text().strip(), "0,1,2,3")

    def test_excluded_unit_is_not_discovered_or_mutated(self):
        self.controller.discover(self.policy, excluded_units={"user.slice"})
        self.controller.apply(self.policy)
        self.assertFalse(any(args[0] == "systemctl" for args in self.calls))
        self.assertEqual(len(self.writes()), 1)

    def test_removed_target_restores_owned_settings(self):
        self.discover()
        self.controller.apply(self.policy)
        self.controller.discover(self.policy, excluded_units={"user.slice"})
        self.assertNotIn("unit:user.slice", self.state["cpu_owned"])
        self.assertEqual((self.paths["user.slice"] / "cpu.weight").read_text().strip(), "100")

    def test_effective_constraint_detected_even_if_configured_value_matches(self):
        self.discover()
        self.controller.apply(self.policy)
        (self.paths["user.slice"] / "cpuset.cpus.effective").write_text("0-3")
        self.assertEqual(self.controller.observe(self.policy)["cpuMismatches"], 1)
        self.assertNotIn("cpuCores", self.controller.observe(self.policy))

    def test_excessive_cpu_budget_does_not_write(self):
        self.discover()
        self.controller.apply({**self.policy, "cpu_cores": 7})
        self.assertTrue(self.controller.invalid)
        self.assertEqual(self.writes(), [])

    def test_invalid_names_never_reach_subprocess(self):
        self.controller.discover(
            {
                **self.policy,
                "coding_units": ["--bad"],
                "coding_containers": ["a;echo bad"],
            }
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.missing, 2)

    def test_invalid_journal_is_rejected(self):
        for value in (
            [],
            {
                "unit:a": {
                    "kind": "unit",
                    "name": "a",
                    "path": "/outside",
                    "inode": 1,
                    "fields": {},
                }
            },
            {
                "unit:a": {
                    "kind": "unit",
                    "name": 3,
                    "path": "/outside",
                    "inode": 1,
                    "fields": {},
                }
            },
        ):
            with self.assertRaises(ValueError):
                r.CPUManager(self.root, {"cpu_owned": value})

    def test_observation_is_fresh_without_subprocess_discovery(self):
        self.discover()
        self.controller.apply(self.policy)
        self.calls.clear()
        self.kernel_writes.clear()
        (self.paths["user.slice"] / "cpu.weight").write_text("900")
        self.assertEqual(self.controller.observe(self.policy)["cpuWeight"], 900)
        self.assertEqual(self.controller.observe(self.policy)["cpuMismatches"], 1)
        self.assertEqual(self.calls, [])

    def test_snapshot_allowlist_privacy_permissions_and_actual_health(self):
        self.discover()
        self.controller.apply(self.policy)
        self.state["secret"] = "never-public"
        self.state["last_status"] = {
            "mode": "normal",
            "policy_stale": False,
            "coding_groups": 2,
            "production_groups": 1,
            "total_bytes": 32 * 1024**3,
            "actual_high_mismatches": 0,
        }
        snapshot = r.public_snapshot(self.policy, self.state, self.controller)
        self.assertEqual(snapshot["status"], "applied")
        self.assertEqual(snapshot["observed"]["ramTotalBytes"], 32 * 1024**3)
        raw = json.dumps(snapshot)
        for private in (
            "never-public",
            str(self.root),
            "user.slice",
            "botfleet-vps-shared",
            "cpu_owned",
        ):
            self.assertNotIn(private, raw)
        dest = self.base / "public" / "status.json"
        r.publish_snapshot(snapshot, dest)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o644)
        self.assertEqual(dest.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(json.loads(dest.read_text()), snapshot)
        self.state["last_status"]["actual_high_mismatches"] = 1
        self.assertEqual(
            r.public_snapshot(self.policy, self.state, self.controller)["status"],
            "drift",
        )
        self.state["last_status"]["policy_stale"] = True
        self.assertEqual(
            r.public_snapshot(self.policy, self.state, self.controller)["status"],
            "unavailable",
        )

    def test_missing_cpu_policy_or_suspension_is_never_applied(self):
        self.discover()
        self.controller.apply(self.policy)
        self.state["last_status"] = {
            "mode": "normal",
            "policy_stale": False,
            "coding_groups": 2,
            "production_groups": 1,
        }
        self.assertEqual(
            r.public_snapshot(policy(), self.state, self.controller)["status"],
            "unavailable",
        )
        self.state["last_status"]["control_suspended"] = 1
        self.assertEqual(
            r.public_snapshot(self.policy, self.state, self.controller)["status"],
            "unavailable",
        )

    def test_policy_revision_does_not_normalize_optional_defaults(self):
        original = policy()
        self.assertNotEqual(
            r.policy_revision(original),
            r.policy_revision({**original, "allow_borrowing": True}),
        )
        self.assertEqual(
            r.policy_revision(original),
            r.policy_revision(dict(reversed(list(original.items())))),
        )


class PolicyExtensions(unittest.TestCase):
    def test_optional_fields_are_strictly_typed_and_preserve_original_policy(self):
        p = policy()
        self.assertEqual(m.validate_policy(json.dumps(p)), p)
        good = {**p, "cpu_cores": 12, "cpu_weight": 50, "allow_borrowing": False}
        self.assertEqual(m.validate_policy(json.dumps(good)), good)
        for key, value in [
            ("cpu_cores", True),
            ("cpu_cores", 0),
            ("cpu_weight", 10001),
            ("cpu_weight", 0),
            ("allow_borrowing", 1),
            ("allow_borrowing", "false"),
            ("coding_units", ["--bad"]),
            ("coding_containers", ["bad;name"]),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                m.validate_policy(json.dumps({**p, key: value}))

    def test_borrowing_off_is_soft_target_in_normal_mode_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            group = cg(root / "coding")
            process = proc(root / "proc")
            p = {**policy(), "allow_borrowing": False}
            status = run_step(p, root, {}, process, 1, ([group], []))
            self.assertEqual(status["mode"], "normal")
            self.assertEqual(status["coding_target_bytes"], p["budget_bytes"])
            self.assertEqual(status["actual_high_mismatches"], 0)
            pressure = proc(root / "pressure", available=3 * 1024**3)
            self.assertEqual(
                run_step(p, root, {}, pressure, 1, ([group], []))["coding_target_bytes"],
                p["budget_bytes"],
            )
            critical = proc(root / "critical", available=1024**3)
            self.assertEqual(
                run_step(p, root, {}, critical, 1, ([group], []))["coding_target_bytes"],
                p["critical_budget_bytes"],
            )


if __name__ == "__main__":
    unittest.main()
