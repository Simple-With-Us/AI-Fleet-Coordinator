"""Resource broker tests; every network, reload, clock and policy store is fake."""

import copy
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import fleet_resource_control as r

NOW = 1800000000


def policy():
    return dict(
        schema_version=1,
        budget_bytes=8 * r.GIB,
        critical_budget_bytes=6 * r.GIB,
        reserve_bytes=6 * r.GIB,
        pressure_available_bytes=4 * r.GIB,
        critical_available_bytes=2 * r.GIB,
        recovery_available_bytes=8 * r.GIB,
        pressure_some_avg10=2.0,
        critical_full_avg10=5.0,
        recovery_some_avg10=0.5,
        recovery_seconds=60,
        tick_seconds=5,
        refresh_seconds=300,
        discovery_seconds=30,
        coding_units=["private.service"],
        coding_containers=["private-container"],
        production_container_prefixes=["private-prefix"],
        production_name_prefixes=["private-name"],
        cpu_cores=12,
        cpu_weight=50,
        allow_borrowing=True,
    )


def snapshot(p, **changes):
    o = dict(
        cpuOnline=16,
        cpuCores=p.get("cpu_cores", 12),
        cpuWeight=p.get("cpu_weight", 50),
        cpuGroups=3,
        cpuMismatches=0,
        ramTotalBytes=32 * r.GIB,
        ramCurrentBytes=4 * r.GIB,
        ramTargetBytes=8 * r.GIB,
        mode="normal",
        codingGroups=3,
        productionGroups=4,
        controlWriteFailures=0,
        controlSuspended=0,
        policyStale=False,
        actualHighMismatches=0,
        updatedAt=r._stamp(NOW),
    )
    o.update(changes)
    return dict(
        schemaVersion=1,
        generatedAt=r._stamp(NOW),
        policyRevision=r.revision(p),
        desired=r.desired(p),
        observed=o,
        status="applied",
    )


class Store:
    def __init__(self):
        self.p = policy()
        self.saves = []
        self.reads = 0

    def get(self):
        self.reads += 1
        return copy.deepcopy(self.p)

    def save(self, value):
        self.saves.append(copy.deepcopy(value))
        self.p = copy.deepcopy(value)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store()
        self.commands, self.events = [], []
        self.clock = [0]

        def runner(argv, **kwargs):
            self.commands.append(argv)
            return 0, b""

        def sleep(seconds):
            self.clock[0] += seconds

        self.ctx = types.SimpleNamespace(
            platform="linux",
            home=Path(self.tmp.name),
            uid=os.getuid(),
            now=lambda: NOW,
            resource_store=self.store,
            resource_host_bounds=lambda: (16, 32 * r.GIB),
            resource_snapshot=lambda: snapshot(self.store.p),
            resource_audit=self.events.append,
            runner=runner,
            child_env=dict,
            resource_monotonic=lambda: self.clock[0],
            resource_sleep=sleep,
        )

    def target(self, field="cpu-weight", value=100, p=None):
        return f"{field}.{value}.{r.revision(p or self.store.p)}"

    def call(self, mode="yes", field="cpu-weight", value=100):
        return r.handle(self.ctx, "resource-policy", self.target(field, value), mode)

    def test_plan_contains_before_and_full_revision_without_private_inventory(self):
        answer, code = self.call("plan")
        self.assertEqual(code, 0)
        self.assertEqual(
            answer["commands"],
            [["resource-policy", "cpu-weight", "100", "50", r.revision(policy())]],
        )
        self.assertNotIn("private", json.dumps(answer))
        self.assertFalse(self.store.saves)
        self.assertFalse(self.commands)

    def test_apply_merges_only_one_field_signals_hup_and_audits(self):
        before = copy.deepcopy(self.store.p)
        answer, code = self.call()
        self.assertEqual((code, answer["result"]), (0, "done"))
        expected = {**before, "cpu_weight": 100}
        self.assertEqual(self.store.saves, [expected])
        self.assertEqual(self.commands, [r.RELOAD])
        self.assertEqual([e["result"] for e in self.events], ["intent", "done"])
        self.assertTrue(all(e["source"] == "fleet-ops-ctl-owner-key" for e in self.events))

    def test_all_fields_and_bool(self):
        for field, value in (
            ("cpu-cores", 10),
            ("cpu-weight", 500),
            ("ram-budget-gib", 10),
            ("ram-critical-gib", 5),
            ("ram-reserve-gib", 4),
            ("borrow-idle", 0),
            ("borrow-idle", 1),
        ):
            with self.subTest(field=field, value=value):
                answer, code = self.call("plan", field, value)
                self.assertEqual(code, 0, answer)

    def test_noncanonical_and_ssh_injections_refused(self):
        rev = r.revision(self.store.p)
        for value in (
            f"cpu-weight.010.{rev}",
            f"cpu-weight.0.{rev}",
            f"borrow-idle.2.{rev}",
            f"borrow-idle.true.{rev}",
            f"cpu-weight.+10.{rev}",
            f"cpu-weight.10.{rev.upper()}",
            f"cpu-weight.10.{rev};id",
            f"cpu-weight.10.{rev}\n",
            "../../file",
            f"secret.1.{rev}",
            f"cpu-weight.10.{rev} --yes",
        ):
            with self.subTest(value=value):
                self.assertEqual(r.handle(self.ctx, "resource-policy", value, "yes")[1], 2)
        self.assertFalse(self.store.saves)

    def test_bounds_and_crossfield_thresholds(self):
        for field, value in (
            ("cpu-cores", 15),
            ("cpu-weight", 10001),
            ("ram-critical-gib", 9),
            ("ram-budget-gib", 27),
            ("ram-reserve-gib", 1),
            ("ram-budget-gib", 5),
        ):
            with self.subTest(field=field):
                self.assertEqual(self.call("plan", field, value)[1], 2)

    def test_complete_policy_invalid_unrelated_field_refused(self):
        for change in (
            {"tick_seconds": True},
            {"unexpected": 1},
            {"critical_full_avg10": float("nan")},
            {"coding_units": []},
            {"critical_available_bytes": 9 * r.GIB},
        ):
            self.store.p = {**policy(), **change}
            # Invalid JSON numbers may not be hashed by the target constructor.
            answer, code = r.handle(self.ctx, "resource-policy", self.target(p=policy()), "yes")
            self.assertEqual(code, 2, answer)
        self.assertFalse(self.store.saves)

    def test_conflict_before_plan_and_immediate_before_write(self):
        target = self.target()
        self.store.p["tick_seconds"] = 9
        self.assertEqual(r.handle(self.ctx, "resource-policy", target, "plan")[1], 2)
        self.store.p = policy()
        original = self.store.get

        def get():
            value = original()
            if self.store.reads > 1:
                value["tick_seconds"] = 9
            return value

        self.store.reads = 0
        self.store.get = get
        self.assertEqual(r.handle(self.ctx, "resource-policy", target, "yes")[1], 2)
        self.assertFalse(self.store.saves)

    def test_uncertain_save_never_retries_or_reloads(self):
        def save(value):
            self.store.saves.append(value)
            raise r.WriteUncertain("The server could not confirm whether the new value was saved.")

        self.store.save = save
        answer, code = self.call()
        # A write that may have landed is unknown, never failed.
        self.assertEqual((code, answer["result"], answer["ok"]), (1, "unknown", False))
        self.assertEqual(len(self.store.saves), 1)
        self.assertFalse(self.commands)
        self.assertIn("could not confirm", answer["detail"])
        self.assertTrue(answer["detail"].endswith(r.REFRESH_ADVICE))

    def test_a_refused_write_is_still_failed(self):
        def save(value):
            raise r.Failed("The server did not accept the new value.")

        self.store.save = save
        answer, code = self.call()
        self.assertEqual((code, answer["result"]), (1, "failed"))

    def test_one_core_is_not_plural(self):
        self.assertEqual(r.field_display("cpu-cores", 1), "1 core")
        self.assertEqual(r.field_display("cpu-cores", 12), "12 cores")

    def assertSavedNotConfirmed(self, answer, code):
        """A write that landed is never answered as failed (RESOURCES-3)."""
        self.assertEqual((code, answer["result"], answer["ok"]), (0, "submitted", True))
        self.assertEqual(answer["executionState"], "saved")
        self.assertIs(answer["saved"], True)
        self.assertIs(answer["verified"], False)
        self.assertEqual(answer["errorCode"], "saved_apply_unverified")
        self.assertNotIn("SavedButApplyUnverified", answer["detail"])
        self.assertTrue(answer["detail"].endswith(r.REFRESH_ADVICE))

    def test_readback_mismatch_never_reloads(self):
        self.store.save = lambda value: self.store.saves.append(value)
        answer, code = self.call()
        self.assertSavedNotConfirmed(answer, code)
        self.assertIn("reading the saved policy back did not match", answer["detail"])
        self.assertFalse(self.commands)

    def test_reload_failure_retains_desired_separately(self):
        self.ctx.runner = lambda *args, **kwargs: (1, b"private output")
        answer, code = self.call()
        self.assertSavedNotConfirmed(answer, code)
        self.assertIn("did not confirm that it reloaded", answer["detail"])
        self.assertEqual(answer["resourcePolicy"]["desired"]["cpuWeight"], 100)
        self.assertNotIn("private", json.dumps(answer))
        self.assertEqual([e["result"] for e in self.events], ["intent", "submitted"])

    def test_apply_wait_bounded_and_requires_matching_revision(self):
        self.ctx.resource_snapshot = lambda: snapshot(policy())
        answer, code = self.call()
        self.assertSavedNotConfirmed(answer, code)
        self.assertIn("within 15 seconds", answer["detail"])
        self.assertEqual(self.clock[0], 15)
        self.assertEqual(answer["resourcePolicy"]["status"], "drift")
        self.assertIn("policy-not-in-effect", answer["resourcePolicy"]["reasons"])

    def test_stale_missing_mismatch_suspended_snapshots_never_applied(self):
        for change in (
            {"updatedAt": r._stamp(NOW - 61)},
            {"cpuMismatches": 1},
            {"cpuCores": 10},
            {"cpuWeight": 99},
            {"actualHighMismatches": 1},
            {"controlSuspended": 1},
            {"policyStale": True},
            {"cpuGroups": 0},
        ):
            self.assertNotEqual(
                r.public_snapshot(snapshot(policy(), **change), NOW)["status"], "applied"
            )
        self.assertEqual(r.public_snapshot({}, NOW)["status"], "unavailable")
        raw = snapshot(policy())
        raw["generatedAt"] = r._stamp(NOW - 61)
        self.assertEqual(
            r.sanitize_snapshot(raw, NOW, require_fresh=False)["status"], "unavailable"
        )
        self.assertEqual(r.sanitize_snapshot(raw, NOW)["generatedAt"], raw["generatedAt"])

    def test_status_readonly_without_credentials(self):
        del self.ctx.resource_store
        answer, code = r.handle(self.ctx, "resource-status", "coding", "plan")
        self.assertEqual(code, 0)
        self.assertEqual(answer["resourcePolicy"]["status"], "applied")
        self.assertEqual(r.handle(self.ctx, "resource-status", "coding", "yes")[1], 2)
        self.assertFalse(self.commands)

    def test_snapshot_projection_rejects_arbitrary_values(self):
        raw = snapshot(policy())
        raw.update(credentials="private", privateInventory=["private"])
        raw["desired"]["private"] = "private"
        raw["observed"]["mode"] = "private"
        raw["observed"]["cpuWeight"] = "private"
        raw["observed"]["updatedAt"] = "private"
        self.assertNotIn("private", json.dumps(r.public_snapshot(raw, NOW)))

    def test_old_cpu_fields_unknown_borrowing_defaults_true(self):
        p = policy()
        for key in ("cpu_cores", "cpu_weight", "allow_borrowing"):
            del p[key]
        r.validate(p, (16, 32 * r.GIB))
        self.assertNotIn("cpuCores", r.desired(p))
        self.assertTrue(r.desired(p)["allowBorrowing"])

    def test_private_files_permissions_symlink_size_and_owner(self):
        path = self.ctx.home / "token"
        path.write_text("safe-test-token")
        path.chmod(0o600)
        self.assertEqual(r.private_text(path, self.ctx.uid), "safe-test-token")
        for mode in (0o644, 0o400, 0o660):
            path.chmod(mode)
            with self.assertRaises(r.Refused):
                r.private_text(path, self.ctx.uid)
        path.chmod(0o600)
        with self.assertRaises(r.Refused):
            r.private_text(path, self.ctx.uid + 1)
        link = self.ctx.home / "link"
        link.symlink_to(path)
        with self.assertRaises(r.Refused):
            r.private_text(link, self.ctx.uid)
        path.write_bytes(b"x" * 8193)
        with self.assertRaises(r.Refused):
            r.private_text(path, self.ctx.uid)

    def credentials(self):
        token = self.ctx.home / ".config/fleet-tools/resource-policy-token"
        project = self.ctx.home / ".agent-sync/fleet-ops-project-id"
        for path, value in (
            (token, "dedicated-test-token"),
            (project, "12345678-1234-1234-1234-123456789abc"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
            path.chmod(0o600)

    def test_no_ambient_credential_or_endpoint_fallback(self):
        with mock.patch.dict(
            os.environ, {"INFISICAL_TOKEN": "ambient", "INFISICAL_API_URL": "https://evil.invalid"}
        ):
            with self.assertRaises(r.Refused):
                r.InfisicalStore(self.ctx)
            self.credentials()
            store = r.InfisicalStore(self.ctx)
            self.assertEqual(set(store.env), {"HOME", "INFISICAL_FLEET_OPS_PROJECT_ID"})
            self.assertEqual(store.token, "dedicated-test-token")

    def test_backend_verified_get_and_single_fixed_patch_no_create_fallback(self):
        self.credentials()
        store = r.InfisicalStore(self.ctx)
        with mock.patch.object(
            store.core, "fetch_verified", return_value=json.dumps(policy())
        ) as get:
            self.assertEqual(store.get(), policy())
            self.assertEqual(get.call_args.args[:2], (r.KEY, r.FOLDER))
            self.assertEqual(get.call_args.kwargs["token"], "dedicated-test-token")
        for status in (0, 400, 404, 500):
            calls = []

            def api(*args, **kwargs):
                calls.append(copy.deepcopy((args, kwargs)))
                return status, {"secretValue": "private"}

            with mock.patch.object(store.core, "_api_json", side_effect=api):
                with self.assertRaises(r.Failed):
                    store.save(policy())
            self.assertEqual(len(calls), 1)
            args, kwargs = calls[0]
            self.assertEqual(args[:2], ("PATCH", "/v3/secrets/raw/CODING_RAM_POLICY"))
            self.assertEqual(
                set(args[3]), {"workspaceId", "environment", "secretPath", "secretValue", "type"}
            )
            self.assertEqual(args[3]["environment"], "prod")
            self.assertNotIn("If-Match", args[3])

    def test_lock_serializes_processes_and_refuses_symlink(self):
        with r.broker_lock(self.ctx), self.assertRaises(r.Refused), r.broker_lock(self.ctx):
            self.fail("nested writer entered")
        path = self.ctx.home / ".local/state/fleet-tools/resource-policy.lock"
        path.unlink()
        path.symlink_to(self.ctx.home / "not-a-lock")
        answer, code = self.call()
        self.assertEqual(code, 1)
        self.assertFalse(self.store.saves)

    def test_audit_intent_failure_prevents_save(self):
        def audit(record):
            raise OSError("private path must not leak")

        self.ctx.resource_audit = audit
        answer, code = self.call()
        self.assertEqual(code, 1)
        self.assertFalse(self.store.saves)
        self.assertNotIn("private", answer["detail"])

    def test_unexpected_failure_after_intent_never_saves_or_reloads(self):
        original = self.store.get
        reads = []

        def get():
            reads.append(True)
            if len(reads) == 2:
                raise RuntimeError("private backend detail")
            return original()

        self.store.get = get
        answer, code = self.call()
        self.assertEqual((code, answer["result"]), (1, "failed"))
        self.assertNotIn("private", answer["detail"])
        self.assertFalse(self.store.saves)
        self.assertFalse(self.commands)
        self.assertEqual([e["result"] for e in self.events], ["intent", "failed"])

    def test_unexpected_reload_failure_reports_saved_but_unverified_and_audits(self):
        def runner(argv, **kwargs):
            self.commands.append(argv)
            raise RuntimeError("private runner detail")

        self.ctx.runner = runner
        answer, code = self.call()
        self.assertSavedNotConfirmed(answer, code)
        self.assertTrue(answer["detail"].startswith("The new value is saved, but what happened"))
        self.assertNotIn("private", answer["detail"])
        self.assertEqual(len(self.store.saves), 1)
        self.assertEqual(self.commands, [r.RELOAD])
        self.assertEqual([e["result"] for e in self.events], ["intent", "submitted"])

    def test_saved_change_keeps_the_saved_fact_when_the_outcome_audit_fails(self):
        self.ctx.runner = lambda *args, **kwargs: (1, b"")

        def audit(record):
            if record["result"] != "intent":
                raise OSError("private audit path")
            self.events.append(record)

        self.ctx.resource_audit = audit
        answer, code = self.call()
        self.assertEqual((code, answer["result"]), (0, "submitted"))
        self.assertTrue(answer["detail"].startswith("The new value is saved"))
        self.assertTrue(
            answer["detail"].endswith("could not be written to the server's audit log.")
        )
        self.assertNotIn("private", answer["detail"])

    def test_applied_change_stays_done_when_only_the_closing_audit_fails(self):
        def audit(record):
            if record["result"] == "done":
                raise OSError("private audit path")
            self.events.append(record)

        self.ctx.resource_audit = audit
        answer, code = self.call()
        self.assertEqual((code, answer["result"], answer["ok"]), (0, "done", True))
        self.assertIn("reports it in effect", answer["detail"])
        self.assertIn("audit log", answer["detail"])
        self.assertEqual([e["result"] for e in self.events], ["intent"])

    def test_unexpected_audit_failure_is_sanitized_and_prevents_save(self):
        def audit(record):
            raise RuntimeError("private audit detail")

        self.ctx.resource_audit = audit
        answer, code = self.call()
        self.assertEqual((code, answer["result"]), (1, "failed"))
        self.assertEqual(
            answer["detail"], "the resource-policy outcome audit could not be recorded"
        )
        self.assertFalse(self.store.saves)
        self.assertFalse(self.commands)

    def test_short_audit_writes_complete_intent_and_result_records(self):
        del self.ctx.resource_audit
        original = os.write
        chunks = []

        def short_write(fd, data):
            chunks.append(len(data))
            return original(fd, data[:7])

        with mock.patch.object(r.os, "write", side_effect=short_write):
            answer, code = self.call()
        self.assertEqual((code, answer["result"]), (0, "done"))
        path = self.ctx.home / ".local/state/fleet-tools/resource-policy-audit.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([record["result"] for record in records], ["intent", "done"])
        self.assertGreater(len(chunks), 2)
        self.assertEqual(len(self.store.saves), 1)

    def test_zero_or_incomplete_audit_intent_never_saves_or_reloads(self):
        del self.ctx.resource_audit
        original = os.write
        for partial in (False, True):
            with self.subTest(partial=partial):
                wrote = []

                def incomplete(fd, data):
                    if partial and not wrote:
                        wrote.append(True)
                        return original(fd, data[:7])
                    return 0

                with mock.patch.object(r.os, "write", side_effect=incomplete):
                    answer, code = self.call()
                self.assertEqual(code, 1)
                self.assertEqual(answer["result"], "failed")
                self.assertFalse(self.store.saves)
                self.assertFalse(self.commands)

    def test_snapshot_requires_every_mandatory_ram_receipt_field(self):
        for section, fields in (
            ("desired", ("ramPressureGiB", "ramCriticalGiB", "reserveGiB", "allowBorrowing")),
            (
                "observed",
                ("ramTotalBytes", "ramCurrentBytes", "ramTargetBytes", "mode", "productionGroups"),
            ),
        ):
            for field in fields:
                with self.subTest(section=section, field=field):
                    raw = snapshot(policy())
                    del raw[section][field]
                    self.assertEqual(r.public_snapshot(raw, NOW)["status"], "unavailable")
                    # A persisted-policy overlay must not repair an incomplete receipt.
                    self.assertEqual(r.public_snapshot(raw, NOW, policy())["status"], "unavailable")

    def test_snapshot_rejects_invalid_ram_bounds_counters_and_timestamps(self):
        cases = (
            ("desired", "ramPressureGiB", 0),
            ("desired", "ramCriticalGiB", -1),
            ("desired", "ramCriticalGiB", 9),
            ("desired", "ramPressureGiB", 30),
            ("desired", "reserveGiB", 1),
            ("desired", "reserveGiB", float("inf")),
            ("desired", "ramPressureGiB", float("nan")),
            ("desired", "allowBorrowing", 1),
            ("desired", "ramPressureGiB", 10**1000),
            ("observed", "ramTotalBytes", 0),
            ("observed", "ramCurrentBytes", 33 * r.GIB),
            ("observed", "ramTargetBytes", 0),
            ("observed", "ramTargetBytes", 33 * r.GIB),
            ("observed", "productionGroups", -1),
            ("observed", "controlWriteFailures", True),
            ("observed", "controlSuspended", -1),
            ("observed", "ramCurrentBytes", 2**63),
            ("observed", "updatedAt", "invalid"),
            ("observed", "updatedAt", r._stamp(NOW + 6)),
            ("observed", "updatedAt", r._stamp(NOW - 61)),
            ("observed", "mode", "unknown"),
        )
        for section, field, value in cases:
            with self.subTest(section=section, field=field, value=value):
                raw = snapshot(policy())
                raw[section][field] = value
                self.assertEqual(r.public_snapshot(raw, NOW)["status"], "unavailable")
        for stamp in ("invalid", r._stamp(NOW + 6), r._stamp(NOW - 61)):
            raw = snapshot(policy())
            raw["generatedAt"] = stamp
            self.assertEqual(r.public_snapshot(raw, NOW)["status"], "unavailable")
        raw = snapshot(policy())
        raw["schemaVersion"] = True
        self.assertEqual(r.public_snapshot(raw, NOW)["status"], "unavailable")

    def test_persisted_verification_read_failure_has_sanitized_partial_result(self):
        original = self.store.get

        def get():
            if self.store.saves:
                raise r.Failed("the resource policy could not be read from its verified source")
            return original()

        self.store.get = get
        answer, code = self.call()
        self.assertSavedNotConfirmed(answer, code)
        self.assertEqual(len(self.store.saves), 1)
        self.assertFalse(self.commands)

    def test_plan_detail_names_the_change_in_owner_units_and_its_production_effect(self):
        answer, code = self.call("plan", "ram-budget-gib", 12)
        self.assertEqual(code, 0, answer)
        detail = answer["detail"]
        self.assertTrue(detail.startswith("Pressure RAM Limit changes from 8 GiB to 12 GiB."))
        # 32 GiB host, 6 GiB reserve:  production's pool falls from 18 GiB to 14 GiB.
        self.assertIn(
            "Warning: This change leaves production up to 14 GiB of protected memory "
            "(now up to 18 GiB).",
            detail,
        )
        self.assertNotIn("serialized", detail)
        self.assertNotIn("  ", detail.replace("\u00a0 ", ""))
        answer, _ = self.call("plan", "borrow-idle", 0)
        self.assertTrue(answer["detail"].startswith("Borrow Idle Memory changes from On to Off."))
        self.assertNotIn("protected memory", answer["detail"])
        answer, _ = self.call("plan", "ram-reserve-gib", 4)
        self.assertIn("up to 20 GiB of protected memory (now up to 18 GiB)", answer["detail"])
        self.assertNotIn("Warning", answer["detail"])

    def test_refusals_name_the_rule_and_the_allowed_maximum(self):
        answer, code = self.call("plan", "ram-budget-gib", 27)
        self.assertEqual(code, 2)
        self.assertIn("at most 26 GiB", answer["detail"])
        self.assertIn("32 GiB", answer["detail"])
        answer, code = self.call("plan", "ram-reserve-gib", 25)
        self.assertEqual(code, 2)
        self.assertIn("reserve can be at most 24 GiB", answer["detail"])
        answer, code = self.call("plan", "ram-budget-gib", 5)
        self.assertEqual(code, 2)
        self.assertIn("Lower Critical RAM Limit first", answer["detail"])
        answer, code = self.call("plan", "cpu-cores", 15)
        self.assertEqual(code, 2)
        self.assertIn("1 through 14", answer["detail"])
        self.assertNotIn("schema or host safety bounds", answer["detail"])

    def test_snapshot_reasons_explain_every_unavailable_or_drift_status(self):
        self.assertEqual(r.public_snapshot({}, NOW, policy())["reasons"], ["receipt-missing"])
        raw = snapshot(policy())
        raw["generatedAt"] = r._stamp(NOW - 61)
        self.assertEqual(r.public_snapshot(raw, NOW)["reasons"], ["receipt-stale"])
        raw = snapshot(policy())
        del raw["observed"]["mode"]
        self.assertEqual(r.public_snapshot(raw, NOW)["reasons"], ["receipt-incomplete"])
        raw = snapshot(policy(), ramTargetBytes=33 * r.GIB)
        self.assertEqual(r.public_snapshot(raw, NOW)["reasons"], ["receipt-invalid"])
        raw = snapshot(policy(), cpuMismatches=2)
        result = r.public_snapshot(raw, NOW)
        self.assertEqual((result["status"], result["reasons"]), ("drift", ["cpu-mismatch"]))
        self.assertEqual(r.public_snapshot(snapshot(policy()), NOW, policy())["reasons"], [])

    def test_saved_policy_ahead_of_the_controller_is_named(self):
        saved = {**policy(), "cpu_weight": 100}
        result = r.public_snapshot(snapshot(policy()), NOW, saved)
        self.assertEqual(result["status"], "drift")
        self.assertEqual(result["desiredRevision"], r.revision(saved))
        self.assertEqual(result["policyRevision"], r.revision(policy()))
        self.assertIn("policy-not-in-effect", result["reasons"])
        # Review uses the saved policy's revision, which the broker accepts.
        target = f"cpu-weight.200.{result['desiredRevision']}"
        self.store.p = saved
        self.assertEqual(r.handle(self.ctx, "resource-policy", target, "plan")[1], 0)

    def test_controller_reasons_pass_only_through_the_allowlist(self):
        raw = snapshot(policy())
        raw["status"] = "unavailable"
        raw["reasons"] = ["no-coding-groups", "private-path", 7, ["x"], "no-coding-groups"]
        result = r.public_snapshot(raw, NOW)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reasons"], ["no-coding-groups"])
        self.assertNotIn("private", json.dumps(result))

    def test_counts_that_show_a_limit_hurting_are_projected_only_when_measured(self):
        raw = snapshot(
            policy(),
            availableBytes=12 * r.GIB,
            productionProtectedGroups=3,
            productionUnprotectedGroups=1,
            oomKillDelta=2,
            excludedUnits=1,
        )
        observed = r.public_snapshot(raw, NOW)["observed"]
        self.assertEqual(observed["availableBytes"], 12 * r.GIB)
        self.assertEqual(observed["productionUnprotectedGroups"], 1)
        self.assertEqual(observed["oomKillDelta"], 2)
        self.assertEqual(observed["excludedUnits"], 1)
        observed = r.public_snapshot(snapshot(policy(), oomKillDelta=-1), NOW)["observed"]
        self.assertNotIn("oomKillDelta", observed)
        self.assertNotIn("availableBytes", observed)

    def test_a_zero_memory_total_from_an_older_controller_stays_unknown(self):
        result = r.public_snapshot(snapshot(policy(), ramTotalBytes=0), NOW)
        self.assertNotIn("ramTotalBytes", result["observed"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reasons"], ["receipt-incomplete"])

    def test_status_with_an_unreadable_saved_policy_says_so(self):
        def get():
            raise r.Failed("the resource policy could not be read from its verified source")

        self.store.get = get
        answer, code = r.handle(self.ctx, "resource-status", "coding", "plan")
        self.assertEqual(code, 0)
        self.assertIn("saved-policy-unreadable", answer["resourcePolicy"]["reasons"])
        self.assertNotIn("desiredRevision", answer["resourcePolicy"])

    def test_muster_contract_fixture_matches_the_broker(self):
        """Muster's ResourcePolicyContractTests decode this same file (RESOURCES-44)."""
        expected = contract()
        if os.environ.get("WRITE_RESOURCE_CONTRACT") == "1":
            CONTRACT.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
        self.assertEqual(json.loads(CONTRACT.read_text()), expected)


CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "muster/MusterTests/Fixtures/resource_policy_contract.json"
)


def contract():
    """Receipts as the broker really answers them, for Muster's decoder to read."""
    p = policy()
    saved_ahead = {**p, "cpu_weight": 100}
    idle = snapshot(p, codingGroups=0, ramTargetBytes=0)
    idle.update(status="unavailable", reasons=["no-coding-groups"])
    fractional = snapshot(p)
    fractional["generatedAt"] = r._stamp(NOW - 0.25)
    fractional["observed"]["updatedAt"] = r._stamp(NOW - 0.602189)
    return {
        "now": r._stamp(NOW),
        "fieldTitles": r.FIELD_TITLES,
        "bounds": {
            "ramBudgetMaxGiB": (32 * r.GIB - p["reserve_bytes"]) // r.GIB,
            "ramReserveMaxGiB": (32 * r.GIB - p["budget_bytes"]) // r.GIB,
            "cpuCoresMax": 16 - 2,
            "productionProtectionGiB": r.production_protection_bytes(p, 32 * r.GIB) / r.GIB,
        },
        "scenarios": {
            "applied": r.public_snapshot(snapshot(p), NOW, p),
            "savedAhead": r.public_snapshot(snapshot(p), NOW, saved_ahead),
            "receiptMissing": r.public_snapshot({}, NOW, p),
            "idle": r.public_snapshot(idle, NOW, p),
            "fractional": r.public_snapshot(fractional, NOW, p),
        },
    }


if __name__ == "__main__":
    unittest.main()
