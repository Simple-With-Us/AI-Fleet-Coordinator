"""What Muster's Controls tab reads from the engine:  registry.json, ``status --json --values``
and the plan of a write, and the facts it shows the owner (who reads a knob, which way a change
moves it, what a value needs)."""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
from unittest import mock

from fleet_knobs import __main__ as cli
from fleet_knobs import inventory, setter, values
from fleet_knobs.registry import BY_NAME, REGISTRY

from ._fake import SIGNING, KnobTestCase, fixture_service_token

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
REPO = SCRIPTS.parent
JSON_FILE = SCRIPTS / "fleet_knobs" / "registry.json"
# The answer Muster's Swift tests read.  This test keeps it the shape the engine prints.
STATUS_FIXTURE = SCRIPTS / "fleet_knobs" / "tests" / "fixtures" / "status_values.json"
WRITE_TOKEN = fixture_service_token()


class RegistryFactsTests(KnobTestCase):
    def test_registry_json_is_the_generated_one(self):
        self.assertEqual(
            JSON_FILE.read_text(encoding="utf-8"),
            inventory.json_text(),
            "scripts/fleet_knobs/registry.json is out of step with registry.py;  from scripts/ run "
            "`python3 -m fleet_knobs inventory --write-json fleet_knobs/registry.json`",
        )

    def test_every_knob_has_a_group_and_a_title_case_label(self):
        for spec in REGISTRY:
            with self.subTest(name=spec.name):
                self.assertTrue(spec.group)
                self.assertTrue(spec.label)
                for word in re.split(r"[ -]", spec.label):
                    self.assertTrue(word[:1].isupper() or word[:1].isdigit(), spec.label)

    def test_every_named_reader_exists_and_names_its_knob(self):
        for spec in REGISTRY:
            for plain, path in spec.consumers:
                with self.subTest(name=spec.name, reader=plain):
                    target = REPO / path
                    self.assertTrue(target.is_file(), path)
                    self.assertIn(spec.name, target.read_text(encoding="utf-8"), path)

    def test_a_knob_with_no_reader_says_so(self):
        unread = {s.name for s in REGISTRY if not s.consumers}
        self.assertTrue(all(BY_NAME[n].hosts == "unknown" for n in unread))
        # The autopush, ask-capture, lane-sweep, claim-expiry and consult knobs have no reader in
        # this repository.  A reader added later names itself here and leaves this set.
        self.assertIn("LANE_SWEEP_MODE", unread)
        self.assertNotIn("NEEDS_OWNER_NOTIFY_PAUSED", unread)

    def test_requirements_name_real_knobs_and_examples_are_accepted(self):
        for spec in REGISTRY:
            for _value, other in spec.requires:
                self.assertIn(other, BY_NAME, spec.name)
            if spec.example:
                with self.subTest(name=spec.name):
                    self.assertEqual(values.parse(spec, spec.example), spec.example)

    def test_inventory_json_cli_checks_and_writes(self):
        target = self.root / "registry.json"
        out = io.StringIO()
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(cli.main(["inventory", "--check-json", str(target)], out), 1)
            self.assertEqual(cli.main(["inventory", "--write-json", str(target)], out), 0)
            self.assertEqual(cli.main(["inventory", "--check-json", str(target)], out), 0)
        self.assertEqual(cli.main(["inventory", "--json"], out), 0)
        self.assertEqual(out.getvalue(), inventory.json_text())

    def test_the_json_never_carries_a_secret_default(self):
        doc = json.loads(inventory.json_text())
        secret = [k for k in doc["knobs"] if k["secret"]]
        self.assertEqual([k["name"] for k in secret], ["FLEET_FACTS_SIGNING_KEY"])
        self.assertIsNone(secret[0]["default"])
        self.assertEqual(secret[0]["local_rule"], "A local file cannot change this.")


class DirectionTests(KnobTestCase):
    def test_direction_follows_the_registry_order(self):
        mode = BY_NAME["LANE_SWEEP_MODE"]
        self.assertEqual(values.direction(mode, "report", "retire"), "wider")
        self.assertEqual(values.direction(mode, "act", "report"), "narrower")
        self.assertEqual(values.direction(mode, "act", "act"), "same")
        paused = BY_NAME["NEEDS_OWNER_NOTIFY_PAUSED"]
        self.assertEqual(values.direction(paused, False, True), "narrower")
        self.assertEqual(values.direction(paused, True, False), "wider")
        hours = BY_NAME["FLEET_AUTOPUSH_IDLE_HOURS"]  # a higher number is narrower here
        self.assertEqual(values.direction(hours, 2, 4), "narrower")
        cap = BY_NAME["FLEET_FACTS_PUBLISH_CAP_MINUTES"]
        self.assertEqual(values.direction(cap, 20, 30), "wider")
        repos = BY_NAME["FLEET_AUTOPUSH_REPOS"]
        self.assertEqual(values.direction(repos, ("a/b",), ("a/b", "c/d")), "wider")
        self.assertEqual(values.direction(repos, ("a/b", "c/d"), ("c/d", "e/f")), "mixed")
        caps = BY_NAME["NEEDS_OWNER_CAPS"]
        self.assertEqual(values.direction(caps, {"per_day": 30}, {"per_day": 10}), "narrower")
        self.assertEqual(
            values.direction(caps, {"per_day": 30, "per_hour": 6}, {"per_day": 40, "per_hour": 1}),
            "mixed",
        )
        self.assertEqual(values.direction(BY_NAME["OPERATOR_API_URL"], "", "https://a.b"), "none")
        self.assertEqual(values.direction(mode, None, "act"), "unknown")


class ProcessLocalFailureTests(KnobTestCase):
    def test_a_process_with_no_reader_token_leaves_no_marker_for_its_siblings(self):
        # Muster's Controls tab runs the engine with no reader token.  Its failed read must not
        # make the Mac's jobs, which hold one, skip their own fetch for the next minute.
        from fleet_knobs import knobs, source

        self.fake.set(secrets={"LANE_SWEEP_MODE": "act"})
        token = os.environ.pop(source.ENV_KNOBS_TOKEN)
        knobs.resolve("LANE_SWEEP_MODE")
        self.assertEqual(knobs.report()["fetch_reason"], "service-token-required")
        self.assertFalse((self.state_dir / "fetch-failed.json").exists())
        os.environ[source.ENV_KNOBS_TOKEN] = token
        knobs.reset_for_tests()
        self.assertEqual(knobs.resolve("LANE_SWEEP_MODE").source, "infisical")

    def test_a_real_read_failure_still_leaves_the_marker(self):
        from fleet_knobs import knobs

        self.fake.set(mode="fail")
        knobs.resolve("LANE_SWEEP_MODE")
        self.assertTrue((self.state_dir / "fetch-failed.json").exists())


class StatusValuesTests(KnobTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(sys, "stderr", io.StringIO()):
            code = cli.main(list(argv), out)
        return code, out.getvalue()

    def test_values_are_carried_only_when_asked_and_never_a_secret(self):
        self.fake.set(
            secrets={"LANE_SWEEP_MODE": "ACT", "FLEET_FACTS_SIGNING_KEY": SIGNING},
        )
        code, out = self.run_cli("status", "--json", "--values")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        rows = {r["name"]: r for r in doc["knobs"]}
        # The engine's canonical text, so a value typed by hand in Infisical reads the same.
        self.assertEqual(rows["LANE_SWEEP_MODE"]["value"], "act")
        self.assertEqual(rows["CONSULT_ENABLED"]["value"], "false")
        self.assertNotIn("value", rows["FLEET_FACTS_SIGNING_KEY"])
        self.assertTrue(rows["FLEET_FACTS_SIGNING_KEY"]["set"])
        self.assertNotIn(SIGNING, out)
        self.assertEqual(doc["schema"], cli.STATUS_SCHEMA)
        self.assertEqual(doc["write_token"], "missing")
        self.assertIsInstance(doc["checked_epoch"], int)
        code, plain = self.run_cli("status", "--json")
        self.assertNotIn('"value"', plain)

    def test_a_write_token_is_reported_by_shape_only(self):
        with mock.patch.dict(os.environ, {setter.ENV_WRITE_TOKEN: WRITE_TOKEN}):
            _code, out = self.run_cli("status", "--json", "--values")
        self.assertEqual(json.loads(out)["write_token"], "present")
        self.assertNotIn(WRITE_TOKEN, out)

    def test_the_swift_fixture_has_the_shape_the_engine_prints(self):
        self.fake.set(secrets={"LANE_SWEEP_MODE": "act"})
        _code, out = self.run_cli("status", "--json", "--values")
        live = json.loads(out)
        fixture = json.loads(STATUS_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(set(fixture), set(live))
        self.assertEqual(fixture["schema"], live["schema"])
        live_row_keys = {frozenset(r) for r in live["knobs"]}
        for row in fixture["knobs"]:
            with self.subTest(name=row["name"]):
                self.assertIn(row["name"], BY_NAME)
                keys = set(row)
                # A row may carry a reason;  every other key is one the engine prints.
                self.assertTrue(
                    any(keys - {"reason"} == set(k) - {"reason"} for k in live_row_keys), row
                )


class PlanTruthTests(KnobTestCase):
    def setUp(self):
        super().setUp()
        from fleet_secrets import core

        verifier = mock.patch.object(core, "verify_service_token", return_value=("/",))
        verifier.start()
        self.addCleanup(verifier.stop)
        self.audit_file = str(self.home / "knobs.jsonl")

    def plan(self, name, value, token=True, **kw):
        env = dict(os.environ)
        if token:
            env[setter.ENV_WRITE_TOKEN] = WRITE_TOKEN
        return setter.set_knob(name, value, "plan", environ=env, audit_file=self.audit_file, **kw)

    def unset_plan(self, name, token=True, **kw):
        env = dict(os.environ)
        if token:
            env[setter.ENV_WRITE_TOKEN] = WRITE_TOKEN
        return setter.unset_knob(name, "plan", environ=env, audit_file=self.audit_file, **kw)

    def test_a_plan_with_no_write_token_is_refused_before_approval(self):
        self.fake.set(secrets={"LANE_SWEEP_MODE": "report"})
        answer, code = self.plan("LANE_SWEEP_MODE", "act", token=False)
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(answer["write_token"], "missing")
        self.assertIn("No write token", answer["detail"])
        answer, code = self.unset_plan("LANE_SWEEP_MODE", token=False)
        self.assertEqual((code, answer["result"]), (2, "refused"))

    def test_a_plan_says_where_it_lands_which_way_it_moves_and_what_it_does(self):
        self.fake.set(secrets={"LANE_SWEEP_MODE": "report"})
        answer, code = self.plan("LANE_SWEEP_MODE", "retire")
        self.assertEqual((code, answer["result"]), (0, "planned"), answer)
        self.assertEqual(answer["write_token"], "present")
        self.assertEqual(answer["direction"], "wider")
        self.assertEqual(answer["stored"], "infisical")
        self.assertEqual(answer["hosts"], "unknown")
        self.assertEqual(answer["default"], "report")
        self.assertIn("This makes the fleet do more than it does now.", answer["detail"])
        self.assertIn("Phase 3 deletions", answer["detail"])
        self.assertNotIn(WRITE_TOKEN, json.dumps(answer))

    def test_an_unchanged_value_is_refused(self):
        self.fake.set(secrets={"LANE_SWEEP_MODE": "act"})
        answer, code = self.plan("LANE_SWEEP_MODE", "ACT")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertIn("already holds this value", answer["detail"])

    def test_an_unmet_requirement_is_named(self):
        self.fake.set(secrets={"CLAIM_EXPIRY_MODE": "dry-run"})
        answer, _code = self.plan("CLAIM_EXPIRY_MODE", "act")
        self.assertIn("act also needs CLAIM_EXPIRY_LAUNCH_AT", answer["detail"])
        self.fake.set(
            secrets={
                "CLAIM_EXPIRY_MODE": "dry-run",
                "CLAIM_EXPIRY_LAUNCH_AT": "2026-11-05T09:00:00Z",
            }
        )
        answer, _code = self.plan("CLAIM_EXPIRY_MODE", "act")
        self.assertNotIn("also needs", answer["detail"])

    def test_a_cap_name_the_knob_does_not_have_is_refused(self):
        answer, code = self.plan("NEEDS_OWNER_CAPS", "per_dya=5")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertIn("per_day, per_hour, urgent_per_hour", answer["detail"])

    def test_a_copy_reading_warns_that_the_write_may_fail(self):
        answer, code = self.plan("LANE_SWEEP_MODE", "act", reader=lambda n: ("report", "lkg", True))
        self.assertEqual(code, 0)
        self.assertIn("Infisical could not be read just now", answer["detail"])

    def test_unset_names_the_value_that_will_apply(self):
        self.fake.set(secrets={"LANE_SWEEP_MODE": "act"})
        answer, code = self.unset_plan("LANE_SWEEP_MODE")
        self.assertEqual((code, answer["result"]), (0, "planned"), answer)
        self.assertEqual((answer["new"], answer["new_source"]), ("report", "default"))
        self.assertEqual(answer["direction"], "narrower")

    def test_unset_of_a_knob_infisical_does_not_hold_is_refused(self):
        self.fake.set(secrets={"LANE_SWEEP_MODE": "act"})
        answer, code = self.unset_plan("CONSULT_ENABLED")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertIn("not set in Infisical", answer["detail"])
        # When Infisical cannot be read, nothing proves the knob is unset, so it is planned.
        answer, code = self.unset_plan("CONSULT_ENABLED", reader=lambda n: (False, "default", True))
        self.assertEqual((code, answer["result"]), (0, "planned"), answer)

    def test_a_fallback_default_from_a_failed_read_is_not_a_current_value(self):
        # Sentry on #1384:  with Infisical unreachable and no copy, the engine answers the
        # default, stale.  That is not what Infisical holds, so the plan says so.
        answer, code = self.plan(
            "LANE_SWEEP_MODE",
            "act",
            reader=lambda n: ("report", "default", True, "unreachable:timeout,lkg-absent"),
        )
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["direction"], "unknown")
        self.assertIn("The current value could not be read", answer["detail"])
        self.assertNotIn("do more than it does now", answer["detail"])

    def test_a_requirement_is_not_called_unset_when_infisical_was_not_read(self):
        from fleet_knobs import knobs

        self.fake.set(mode="fail")
        knobs.resolve("CLAIM_EXPIRY_LAUNCH_AT")  # the failed read this process already made
        answer, _code = self.plan(
            "CLAIM_EXPIRY_MODE",
            "act",
            reader=lambda n: ("dry-run", "default", True, "unreachable:x"),
        )
        self.assertIn(
            "act also needs CLAIM_EXPIRY_LAUNCH_AT, and whether that is set is not known.",
            answer["detail"],
        )

    def test_an_invalid_value_infisical_holds_can_be_reset(self):
        # Sentry on #1384:  a value the spec refuses reads as the default, but Infisical holds
        # it, so a reset has something to remove and is planned.
        self.fake.set(secrets={"LANE_SWEEP_MODE": "everything"})
        answer, code = self.unset_plan("LANE_SWEEP_MODE")
        self.assertEqual((code, answer["result"]), (0, "planned"), answer)
        self.assertEqual(answer["old_source"], "default")

    def test_a_compliance_reset_says_the_rule_will_read_unknown(self):
        self.fake.set(secrets={"COMPLIANCE_CERT_EXPIRY_DAYS": "30"})
        answer, code = self.unset_plan("COMPLIANCE_CERT_EXPIRY_DAYS")
        self.assertEqual(code, 0, answer)
        self.assertIn("read unknown", answer["detail"])
