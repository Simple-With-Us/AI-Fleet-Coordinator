import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet_coding_memory as m

M = m.MIB


def policy():
    return {
        "schema_version": 1,
        "budget_bytes": 8 * 1024**3,
        "critical_budget_bytes": 6 * 1024**3,
        "reserve_bytes": 6 * 1024**3,
        "pressure_available_bytes": 4 * 1024**3,
        "critical_available_bytes": 2 * 1024**3,
        "recovery_available_bytes": 8 * 1024**3,
        "pressure_some_avg10": 2.0,
        "critical_full_avg10": 5.0,
        "recovery_some_avg10": 0.5,
        "recovery_seconds": 60,
        "tick_seconds": 5,
        "refresh_seconds": 300,
        "discovery_seconds": 30,
        "coding_units": ["user.slice"],
        "coding_containers": ["botfleet-vps-shared"],
        "production_container_prefixes": ["runtime-private-prefix"],
        "production_name_prefixes": ["congress-app-", "scan-cpu-worker-"],
    }


def cg(path, usage=100 * M, high="max", low="0", maximum=4 * 1024**3):
    path.mkdir(parents=True)
    for name, value in {
        "memory.current": usage,
        "memory.high": high,
        "memory.low": low,
        "memory.max": maximum,
        "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
    }.items():
        (path / name).write_text(str(value))
    return path.resolve()


def proc(path, available=10 * 1024**3, total=32 * 1024**3, some=0.0, full=0.0):
    path.mkdir(parents=True)
    (path / "meminfo").write_text(
        f"MemTotal: {total // 1024} kB\nMemAvailable: {available // 1024} kB\n"
    )
    (path / "pressure").mkdir()
    (path / "pressure" / "memory").write_text(
        f"some avg10={some} avg60=0 avg300=0 total=0\nfull avg10={full} avg60=0 avg300=0 total=0\n"
    )
    return path


def run_step(*args, **kwargs):
    with mock.patch.object(
        m, "write_control", side_effect=lambda path, value: path.write_text(value)
    ):
        return m.step(*args, **kwargs)


class PolicyTests(unittest.TestCase):
    def test_schema_and_missing_cold_cache(self):
        p = policy()
        self.assertEqual(m.validate_policy(json.dumps(p)), p)
        with self.assertRaises(ValueError):
            m.validate_policy(json.dumps({**p, "unexpected": True}))
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(m, "fetch_policy", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "no last-known-good"):
                    m.load_lkg(Path(d))

    def test_failure_uses_marked_lkg(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            m.atomic_json(root / "policy.json", policy())
            with mock.patch.object(m, "fetch_policy", side_effect=RuntimeError("offline")):
                loaded, stale = m.load_lkg(root)
            self.assertEqual(loaded, policy())
            self.assertTrue(stale)
            self.assertTrue(json.loads((root / "policy-meta.json").read_text())["stale"])

    def test_systemd_credential_is_root_private_regular_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            credential = root / "infisical-token"
            credential.write_text(" scoped-token\n")
            credential.chmod(0o600)
            env = {"CREDENTIALS_DIRECTORY": str(root)}
            self.assertEqual(m.read_systemd_token(env, expected_uid=os.geteuid()), "scoped-token")
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                m.read_systemd_token(env, expected_uid=os.geteuid() + 1)
            credential.chmod(0o640)
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                m.read_systemd_token(env, expected_uid=os.geteuid())
            credential.chmod(0o600)
            credential.write_text("x" * 8193)
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                m.read_systemd_token(env, expected_uid=os.geteuid())

    def test_fetch_policy_scopes_auth_and_preserves_project_prod_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            cli = Path(d) / "fleet-secrets"
            cli.write_text("#!/bin/sh\nexit 0\n")
            cli.chmod(0o700)
            ambient = {
                "FLEET_SECRETS_CLI": str(cli),
                "INFISICAL_FLEET_OPS_PROJECT_ID": "project-id",
                "INFISICAL_ENVIRONMENT": "prod",
                "INFISICAL_ENV": "prod",
                "INFISICAL_TOKEN": "ambient-token",
                "INFISICAL_CLIENT_SECRET": "ambient-secret",
                "INFISICAL_AUTOMATION_CLIENT_SECRET": "ambient-automation-secret",
                "INFISICAL_UNRELATED": "discard-me",
            }
            completed = type("Completed", (), {"stdout": json.dumps(policy())})()
            with (
                mock.patch.dict(os.environ, ambient, clear=True),
                mock.patch.object(m, "read_systemd_token", return_value="credential-token"),
                mock.patch.object(m.subprocess, "run", return_value=completed) as run,
            ):
                self.assertEqual(m.fetch_policy(), policy())
            argv = run.call_args.args[0]
            child_env = run.call_args.kwargs["env"]
            self.assertEqual(
                argv, [str(cli), "get-verified", "CODING_RAM_POLICY", "--path", "/coding-memory"]
            )
            self.assertNotIn("credential-token", argv)
            self.assertEqual(child_env["INFISICAL_TOKEN"], "credential-token")
            self.assertEqual(child_env["INFISICAL_FLEET_OPS_PROJECT_ID"], "project-id")
            self.assertEqual(child_env["INFISICAL_ENVIRONMENT"], "prod")
            self.assertEqual(child_env["INFISICAL_ENV"], "prod")
            self.assertNotIn("INFISICAL_CLIENT_SECRET", child_env)
            self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_SECRET", child_env)
            self.assertNotIn("INFISICAL_UNRELATED", child_env)

    def test_fetch_policy_without_systemd_credential_never_invokes_cli(self):
        with (
            mock.patch.dict(
                os.environ,
                {"FLEET_SECRETS_CLI": "/bin/true", "INFISICAL_FLEET_OPS_PROJECT_ID": "project-id"},
                clear=True,
            ),
            mock.patch.object(m.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "credential unavailable"):
                m.fetch_policy()
        run.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def test_docker_inspect_is_batched_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "cgroup"
            proc_root = Path(d) / "proc"
            names = [
                "botfleet-vps-shared",
                "congress-app-one",
                "scan-cpu-worker-one",
                "congress-app-disappeared",
            ]
            live_names = names[:-1]
            output = []
            for index, name in enumerate(live_names, start=101):
                unit = f"docker-{index}.scope"
                cg(root / "system.slice" / unit)
                pid_dir = proc_root / str(index)
                pid_dir.mkdir(parents=True)
                (pid_dir / "cgroup").write_text(f"0::/system.slice/{unit}\n")
                output.append(f"/{name}|{index}|{2 * 1024**3}\n")

            calls = []

            def runner(argv, **kwargs):
                calls.append((argv, kwargs))
                if argv[1] == "ps":
                    return type("Result", (), {"stdout": "\n".join(names)})()
                return type("Result", (), {"stdout": "".join(output), "returncode": 1})()

            coding, production, limits = m.docker_groups(
                policy(), root, runner=runner, proc_root=proc_root
            )
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][0][1], "inspect")
            self.assertEqual(calls[1][1]["timeout"], 8)
            self.assertFalse(calls[1][1]["check"])
            self.assertEqual(len(coding), 1)
            self.assertEqual(len(production), 2)
            self.assertEqual(len(limits), 2)

    def test_unlimited_or_unsupported_production_groups_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            parent = cg(root / "system.slice")
            protected = cg(parent / "protected", maximum=3 * 1024**3)
            unlimited = cg(parent / "unlimited", maximum="max")
            unsupported = cg(root / "other.slice" / "unsupported", maximum=3 * 1024**3)
            protected_count, unprotected_count = m.production_protection_counts(
                [protected, unlimited, unsupported], root
            )
            self.assertEqual(protected_count, 1)
            self.assertEqual(unprotected_count, 2)


class ControllerTests(unittest.TestCase):
    def test_nested_group_dedup_and_soft_budget_sum(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            parent = cg(root / "user.slice", usage=500 * M)
            child = cg(parent / "session.scope", usage=400 * M)
            procroot = proc(root / "proc")
            state = {}
            status = run_step(
                policy(), root, state, procroot, now=100, discovered=([parent, child], [])
            )
            self.assertEqual(status["coding_groups"], 1)
            self.assertEqual(status["mode"], "normal")
            self.assertEqual(
                int((parent / "memory.high").read_text()), status["coding_target_bytes"]
            )
            self.assertEqual(status["coding_target_bytes"], 8 * 1024**3)

    def test_status_counts_control_write_failures_without_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            group = cg(root / "coding")
            with mock.patch.object(m, "write_control", side_effect=OSError("refused")):
                status = m.step(policy(), root, {}, proc(root / "proc"), 1, ([group], []))
            self.assertEqual(status["control_write_failures"], 1)
            self.assertFalse(any(str(group) in str(value) for value in status.values()))

    def test_oom_delta_uses_deduplicated_discovered_cgroups_and_inode(self):
        with tempfile.TemporaryDirectory() as d:
            parent = cg(Path(d) / "service")
            child = cg(parent / "container")
            state = {}
            (parent / "memory.events").write_text("oom_kill 2\n")
            (child / "memory.events").write_text("oom_kill 9\n")
            self.assertEqual(m.oom_kill_delta([parent, child], state), 0)
            (parent / "memory.events").write_text("oom_kill 5\n")
            self.assertEqual(m.oom_kill_delta([parent, child], state), 3)
            old_inode = parent.stat().st_ino
            replacement = parent.with_name("previous")
            parent.rename(replacement)
            parent.mkdir()
            (parent / "memory.events").write_text("oom_kill 80\n")
            self.assertNotEqual(parent.stat().st_ino, old_inode)
            self.assertEqual(m.oom_kill_delta([parent], state), 0)
            (parent / "memory.events").write_text("oom_kill 82\n")
            self.assertEqual(m.oom_kill_delta([parent], state), 2)

    def test_pressure_critical_and_timed_recovery_hysteresis(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            group = cg(root / "coding", usage=500 * M)
            state = {}
            status = run_step(
                policy(), root, state, proc(root / "p1", available=3 * 1024**3), 0, ([group], [])
            )
            self.assertEqual(status["mode"], "pressure")
            self.assertEqual(status["coding_target_bytes"], 8 * 1024**3)
            status = run_step(
                policy(),
                root,
                state,
                proc(root / "p2", available=1024**3, full=6),
                1,
                ([group], []),
            )
            self.assertEqual(status["mode"], "critical")
            self.assertEqual(status["coding_target_bytes"], 6 * 1024**3)
            self.assertEqual(int((group / "memory.high").read_text()), 6 * 1024**3)
            status = run_step(
                policy(), root, state, proc(root / "p3", available=9 * 1024**3), 10, ([group], [])
            )
            self.assertEqual(status["mode"], "critical")
            status = run_step(
                policy(), root, state, proc(root / "p4", available=9 * 1024**3), 71, ([group], [])
            )
            self.assertEqual(status["mode"], "normal")

    def test_high_global_available_memory_prevents_psi_only_throttle(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            group = cg(root / "coding", usage=500 * M)
            state = {}
            status = run_step(
                policy(),
                root,
                state,
                proc(root / "proc", available=13 * 1024**3, some=12.0, full=9.0),
                1,
                ([group], []),
            )
            self.assertEqual(status["mode"], "normal")
            self.assertEqual(status["coding_target_bytes"], 8 * 1024**3)

    def test_invalid_recovery_timer_is_replaced_and_controller_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            group = cg(root / "coding", usage=500 * M)
            state = {"mode": "critical", "recovery_since": "invalid"}
            status = run_step(
                policy(), root, state, proc(root / "p1", available=9 * 1024**3), 10, ([group], [])
            )
            self.assertEqual(status["mode"], "critical")
            self.assertEqual(state["recovery_since"], 10)
            status = run_step(
                policy(), root, state, proc(root / "p2", available=9 * 1024**3), 71, ([group], [])
            )
            self.assertEqual(status["mode"], "normal")

    def test_reset_transient_state_clears_stale_mode_and_timer(self):
        state = {"mode": "critical", "recovery_since": 123.0, "owned": {}}
        m.reset_transient_state(state)
        self.assertEqual(state["mode"], "normal")
        self.assertNotIn("recovery_since", state)
        self.assertIn("owned", state)

    def test_allocation_preserves_preexisting_narrower_high_and_can_later_grow(self):
        with tempfile.TemporaryDirectory() as d:
            a = cg(Path(d) / "a", 100 * M, high=200 * M)
            b = cg(Path(d) / "b", 300 * M)
            state = {}
            first = m.allocate(2 * 1024**3, [a, b], state)
            self.assertEqual(first[a], 200 * M)
            m.write_owned(a, "memory.high", first[a], state)
            second = m.allocate(4 * 1024**3, [a, b], state)
            self.assertEqual(second[a], 200 * M)
            self.assertGreater(second[b], 3 * 1024**3)

    def test_allocation_skips_disappeared_group_and_keeps_healthy_group(self):
        with tempfile.TemporaryDirectory() as d:
            missing = cg(Path(d) / "missing", 100 * M)
            healthy = cg(Path(d) / "healthy", 300 * M)
            (missing / "memory.high").unlink()
            result = m.allocate(2 * 1024**3, [missing, healthy])
            self.assertNotIn(missing, result)
            self.assertIn(healthy, result)
            self.assertGreater(result[healthy], 0)

    def test_allocation_waterfills_from_snapshot_for_uneven_service_usage(self):
        with tempfile.TemporaryDirectory() as d:
            groups = [
                cg(Path(d) / "heavy", usage=10 * 1024**3),
                cg(Path(d) / "medium", usage=1024**3),
                cg(Path(d) / "small", usage=100 * M),
            ]
            current = {p: int((p / "memory.current").read_text()) for p in groups}
            target = 15 * 1024**3
            allocation = m.allocate(target, groups)
            self.assertEqual(sum(allocation.values()), target)
            for p in groups:
                self.assertGreaterEqual(allocation[p], current[p])

    def test_restore_only_owned_unchanged_inode_and_value(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                self.assertTrue(m.write_owned(p, "memory.high", 256 * M, state))
                m.restore_owned(state)
            self.assertEqual((p / "memory.high").read_text(), "max")
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.write_owned(p, "memory.high", 256 * M, state)
            (p / "memory.high").write_text("123")
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.restore_owned(state)
            self.assertEqual((p / "memory.high").read_text(), "123")

    def test_owned_limits_are_page_aligned_and_match_kernel_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            page = os.sysconf("SC_PAGE_SIZE")

            def kernel_write(path, value):
                requested = int(value)
                normalized = ((requested + page - 1) // page) * page
                path.write_text(str(normalized))

            with mock.patch.object(m, "write_control", side_effect=kernel_write):
                self.assertTrue(m.write_owned(p, "memory.high", 3 * page + 1, state))
                self.assertTrue(m.write_owned(p, "memory.low", 7 * page + 1, state))

            high_key = f"{p}:memory.high"
            low_key = f"{p}:memory.low"
            self.assertEqual((p / "memory.high").read_text(), str(3 * page))
            self.assertEqual(state["owned"][high_key]["last"], str(3 * page))
            self.assertEqual((p / "memory.low").read_text(), str(7 * page))
            self.assertEqual(state["owned"][low_key]["last"], str(7 * page))

    def test_recreated_cgroup_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.write_owned(p, "memory.high", 256 * M, state)
            replacement = p.with_name("replacement")
            p.rename(replacement)
            p.mkdir()
            (p / "memory.high").write_text("max")
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.restore_owned(state)
            self.assertEqual((p / "memory.high").read_text(), "max")

    def test_external_value_change_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.write_owned(p, "memory.high", 256 * M, state)
                (p / "memory.high").write_text("128")
                self.assertFalse(m.write_owned(p, "memory.high", 512 * M, state))
                m.restore_owned(state)
            self.assertEqual((p / "memory.high").read_text(), "128")

    def test_write_persists_restore_record_before_control_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            snapshots = []
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.write_owned(
                    p,
                    "memory.high",
                    256 * M,
                    state,
                    persist=lambda: snapshots.append(json.loads(json.dumps(state))),
                )
            self.assertEqual(snapshots[0]["owned"][f"{p}:memory.high"]["baseline"], "max")
            self.assertIsNone(snapshots[0]["owned"][f"{p}:memory.high"]["last"])
            self.assertEqual(snapshots[0]["owned"][f"{p}:memory.high"]["pending"], str(256 * M))

    def test_reconcile_removes_pending_record_when_write_never_happened(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {
                "owned": {
                    f"{p}:memory.high": {
                        "inode": p.stat().st_ino,
                        "baseline": "max",
                        "last": str(256 * M),
                    }
                }
            }
            m.reconcile_owned(state)
            self.assertEqual(state["owned"], {})

    def test_failed_first_and_subsequent_writes_restore_previous_record_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            key = f"{p}:memory.high"
            with mock.patch.object(m, "write_control", side_effect=OSError("write refused")):
                self.assertFalse(m.write_owned(p, "memory.high", 256 * M, state))
            self.assertNotIn(key, state["owned"])
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                self.assertTrue(m.write_owned(p, "memory.high", 256 * M, state))
            first_record = state["owned"][key].copy()
            with mock.patch.object(m, "write_control", side_effect=OSError("write refused")):
                self.assertFalse(m.write_owned(p, "memory.high", 512 * M, state))
            self.assertEqual(state["owned"][key], first_record)
            self.assertEqual((p / "memory.high").read_text(), str(256 * M))
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                self.assertTrue(m.write_owned(p, "memory.high", 512 * M, state))
            self.assertEqual((p / "memory.high").read_text(), str(512 * M))

    def test_target_landed_despite_write_exception_keeps_successful_ownership(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}

            def write_then_raise(path, value):
                path.write_text(value)
                raise OSError("post-write error")

            with mock.patch.object(m, "write_control", side_effect=write_then_raise):
                self.assertTrue(m.write_owned(p, "memory.high", 256 * M, state))
            rec = state["owned"][f"{p}:memory.high"]
            self.assertEqual(rec["last"], str(256 * M))
            self.assertNotIn("pending", rec)

    def test_crash_reconcile_keeps_previous_owned_value(self):
        with tempfile.TemporaryDirectory() as d:
            p = cg(Path(d) / "unit", high="max")
            state = {"owned": {}}
            with mock.patch.object(
                m, "write_control", side_effect=lambda path, value: path.write_text(value)
            ):
                m.write_owned(p, "memory.high", 256 * M, state)
            persisted = {}

            def snapshot():
                nonlocal persisted
                persisted = json.loads(json.dumps(state))

            with mock.patch.object(m, "write_control", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    m.write_owned(p, "memory.high", 512 * M, state, persist=snapshot)
            m.reconcile_owned(persisted)
            rec = persisted["owned"][f"{p}:memory.high"]
            self.assertEqual(rec["last"], str(256 * M))
            self.assertNotIn("pending", rec)
            self.assertEqual((p / "memory.high").read_text(), str(256 * M))

    def test_controller_lock_is_singleton_and_released(self):
        with tempfile.TemporaryDirectory() as d:
            first = m.acquire_lock(Path(d))
            try:
                with self.assertRaises(BlockingIOError):
                    m.acquire_lock(Path(d))
            finally:
                os.close(first)
            second = m.acquire_lock(Path(d))
            os.close(second)

    def test_status_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "absent"
            result = subprocess.run(
                [sys.executable, str(Path(m.__file__)), "--status", "--state-dir", str(missing)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(missing.exists())
            state_dir = Path(d) / "existing"
            state_dir.mkdir(mode=0o755)
            (state_dir / "state.json").write_text('{"last_status":{"mode":"normal"}}')
            (state_dir / "state.json").chmod(0o644)
            result = subprocess.run(
                [sys.executable, str(Path(m.__file__)), "--status", "--state-dir", str(state_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o755)
            self.assertEqual((state_dir / "state.json").stat().st_mode & 0o777, 0o644)

    def test_no_groups_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            status = run_step(policy(), root, {}, proc(root / "proc"), 1, ([], []))
            self.assertEqual(status["coding_groups"], 0)
            self.assertEqual(status["production_groups"], 0)
            self.assertEqual(status["coding_target_bytes"], 0)

    def test_production_low_is_capped_and_sums_at_supported_ancestor(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            parent = cg(root / "system.slice", usage=0, low="0")
            groups = [cg(parent / f"docker-{i}.scope", maximum=4 * 1024**3) for i in range(3)]
            state = {}
            limits = {p: 8 * 1024**3 for p in groups}
            run_step(policy(), root, state, proc(root / "proc"), 1, ([], groups, limits))
            lows = [int((p / "memory.low").read_text()) for p in groups]
            self.assertEqual(sum(lows), 18 * 1024**3)
            self.assertEqual(int((parent / "memory.low").read_text()), sum(lows))
            reduced = {**policy(), "budget_bytes": 16 * 1024**3}
            run_step(reduced, root, state, proc(root / "proc2"), 2, ([], groups, limits))
            reduced_lows = [int((p / "memory.low").read_text()) for p in groups]
            self.assertLessEqual(sum(reduced_lows), 10 * 1024**3)
            page = os.sysconf("SC_PAGE_SIZE")
            self.assertLessEqual(10 * 1024**3 - sum(reduced_lows), page * (len(groups) + 1))
            self.assertEqual(int((parent / "memory.low").read_text()), sum(reduced_lows))


if __name__ == "__main__":
    unittest.main()
