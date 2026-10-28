"""``fleet_knobs set`` against the fake ``infisical``:  validation, the token, the copy, the audit."""

from __future__ import annotations

import io
import json
import os
import stat

from fleet_knobs import __main__ as cli
from fleet_knobs import knobs, lkg, setter, source

from ._fake import SIGNING, KnobTestCase

WRITE_TOKEN = "fakeWriteTokenSentinel0123456789abcdef"
READ_TOKEN = "fakeReadTokenSentinel0123456789abcdef"
LIVE = {"FLEET_AUTOPUSH_MODE": "off", "FLEET_FACTS_SIGNING_KEY": SIGNING}


class SetTestCase(KnobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake.set(secrets=LIVE)
        self.audit_file = str(self.home / ".agent-sync" / "listener" / "knobs.jsonl")

    def set_calls(self) -> list:
        return [c for c in self.fake.calls() if c[:2] == ["secrets", "set"]]

    def audit_rows(self) -> list:
        if not os.path.exists(self.audit_file):
            return []
        with open(self.audit_file, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def run_set(self, name, value, mode, environ=None, **kw):
        env = dict(os.environ)
        env.update(environ or {})
        return setter.set_knob(name, value, mode, environ=env, audit_file=self.audit_file, **kw)


class ValidationTests(SetTestCase):
    def test_refuses_unknown_secret_and_bad_values(self):
        cases = [
            ("NOT_A_KNOB", "x"),
            ("FLEET_FACTS_SIGNING_KEY", "a" * 40),
            ("FLEET_AUTOPUSH_MODE", "everything"),
            ("FLEET_AUTOPUSH_IDLE_HOURS", "0"),
            ("FLEET_AUTOPUSH_IDLE_HOURS", "999"),
            ("FLEET_AUTOPUSH_IDLE_COMMIT", "maybe"),
            ("OWNER_LINEAR_USER_ID", "not-a-uuid"),
            ("FLEET_AUTOPUSH_REPOS", "@/etc/passwd"),
            ("FLEET_AUTOPUSH_REPOS", "a;b"),
            ("CLAIM_EXPIRY_MODE", "act\nFLEET_AUTOPUSH_MODE=full"),
        ]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                answer, code = self.run_set(
                    name, value, "yes", {setter.ENV_WRITE_TOKEN: WRITE_TOKEN}
                )
                self.assertEqual((code, answer["result"]), (2, "refused"))
                self.assertNotIn(value, answer["detail"])
        self.assertEqual(self.set_calls(), [])

    def test_refuses_without_plan_or_yes(self):
        answer, code = self.run_set("FLEET_AUTOPUSH_MODE", "report", None)
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(self.set_calls(), [])
        self.assertEqual(self.audit_rows(), [])

    def test_canonical_text_is_written(self):
        _spec, typed, text = setter.check(
            "ASK_CAPTURE_CAPS", '{"per_session": 2, "per_seat_day": 9}'
        )
        self.assertEqual(text, "per_seat_day=9,per_session=2")
        self.assertEqual(typed, {"per_session": 2, "per_seat_day": 9})
        self.assertEqual(setter.check("CONSULT_ENABLED", "YES")[2], "true")


class PlanTests(SetTestCase):
    def test_plan_shows_old_new_and_argv_and_writes_nothing(self):
        answer, code = self.run_set("FLEET_AUTOPUSH_MODE", "report", "plan")
        self.assertEqual((code, answer["result"]), (0, "planned"))
        self.assertEqual((answer["old"], answer["new"]), ("off", "report"))
        self.assertEqual(
            answer["commands"],
            [
                [
                    "infisical",
                    "secrets",
                    "set",
                    "FLEET_AUTOPUSH_MODE=report",
                    "--env=prod",
                    "--path=/",
                    f"--projectId={source.PROJECT_ID}",
                    "--type=shared",
                    "--silent",
                ]
            ],
        )
        self.assertEqual(self.set_calls(), [])
        self.assertEqual(self.audit_rows(), [])


class WriteTests(SetTestCase):
    def test_no_write_token_refuses(self):
        answer, code = self.run_set("FLEET_AUTOPUSH_MODE", "report", "yes", platform="linux")
        self.assertEqual((code, answer["detail"]), (2, "no write token"))
        self.assertEqual(self.set_calls(), [])
        (row,) = self.audit_rows()
        self.assertEqual((row["key"], row["result"]), ("FLEET_AUTOPUSH_MODE", "refused"))

    def test_token_only_in_the_child_env_and_never_in_output(self):
        environ = {
            setter.ENV_WRITE_TOKEN: WRITE_TOKEN,
            source.ENV_TOKEN: READ_TOKEN,
            source.ENV_AUTOMATION_ID: "fake-id",
            source.ENV_AUTOMATION_SECRET: "fake-secret",
            "AGENT_SEAT": "CLAUDE",
        }
        answer, code = self.run_set(
            "FLEET_AUTOPUSH_MODE",
            "report",
            "yes",
            environ,
            reader=lambda name: ("off", "infisical"),
        )
        self.assertEqual((code, answer["result"]), (0, "done"), answer)
        (call,) = self.set_calls()
        self.assertIn("FLEET_AUTOPUSH_MODE=report", call)
        self.assertFalse([a for a in call if WRITE_TOKEN in a])
        self.assertEqual(self.fake.tokens()[-1], WRITE_TOKEN)
        seen = self.fake.automation_seen()[-1]
        self.assertEqual(seen, {"id": None, "secret": None})
        self.assertNotIn(WRITE_TOKEN, json.dumps(answer))
        with open(self.audit_file, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(WRITE_TOKEN, text)
        self.assertEqual(stat.S_IMODE(os.stat(self.audit_file).st_mode), 0o600)
        (row,) = self.audit_rows()
        self.assertEqual(
            {k: row[k] for k in ("seat", "key", "old", "new", "result")},
            {
                "seat": "CLAUDE",
                "key": "FLEET_AUTOPUSH_MODE",
                "old": "off",
                "new": "report",
                "result": "done",
            },
        )

    def test_keychain_is_read_on_the_mac(self):
        seen = []

        def runner(argv, *, timeout, env=None, want_stdout=False):
            seen.append(argv)
            if argv[0] == setter.SECURITY:
                return 0, (WRITE_TOKEN + "\n").encode()
            return setter.run(argv, timeout=timeout, env=env, want_stdout=want_stdout)

        answer, code = self.run_set(
            "FLEET_AUTOPUSH_MODE",
            "report",
            "yes",
            platform="darwin",
            runner=runner,
            reader=lambda name: ("off", "infisical"),
        )
        self.assertEqual(code, 0, answer)
        self.assertEqual(
            seen[0],
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "fleet-knobs-write",
                "-a",
                "INFISICAL_TOKEN",
                "-w",
            ],
        )
        self.assertEqual(self.fake.tokens()[-1], WRITE_TOKEN)

    def test_cli_failure_is_failed(self):
        self.fake.set(mode="fail", code=4, leak=WRITE_TOKEN)
        answer, code = self.run_set(
            "FLEET_AUTOPUSH_MODE",
            "report",
            "yes",
            {setter.ENV_WRITE_TOKEN: WRITE_TOKEN},
            reader=lambda name: ("off", "infisical"),
        )
        self.assertEqual(
            (code, answer["result"], answer["detail"]),
            (1, "failed", "infisical secrets set exited 4"),
        )
        self.assertNotIn(WRITE_TOKEN, json.dumps(answer))

    def test_secret_attempt_audits_no_value(self):
        self.run_set(
            "FLEET_FACTS_SIGNING_KEY", SIGNING, "yes", {setter.ENV_WRITE_TOKEN: WRITE_TOKEN}
        )
        (row,) = self.audit_rows()
        self.assertEqual((row["old"], row["new"]), (None, None))
        with open(self.audit_file, encoding="utf-8") as fh:
            self.assertNotIn(SIGNING, fh.read())


class LastKnownGoodTests(SetTestCase):
    def test_live_reread_refreshes_the_copy(self):
        # The fake answers the new value after the write, as Infisical would.
        def reader(name):
            self.fake.set(secrets=dict(LIVE, FLEET_AUTOPUSH_MODE="report"))
            return setter.current(name)

        answer, code = self.run_set(
            "FLEET_AUTOPUSH_MODE",
            "report",
            "yes",
            {setter.ENV_WRITE_TOKEN: WRITE_TOKEN},
            reader=reader,
        )
        self.assertEqual((code, answer["lkg"]), (0, "refreshed"), answer)
        doc, note = lkg.read()
        self.assertEqual((note, doc["knobs"]["FLEET_AUTOPUSH_MODE"]), ("ok", "report"))
        self.assertNotIn("FLEET_FACTS_SIGNING_KEY", doc["knobs"])

    def test_failed_reread_patches_the_copy_and_keeps_its_age(self):
        knobs.resolve("FLEET_AUTOPUSH_MODE")  # a good read writes the copy
        knobs.reset_for_tests()
        before, _ = lkg.read()
        answer, code = self.run_set(
            "FLEET_AUTOPUSH_MODE",
            "snapshot",
            "yes",
            {setter.ENV_WRITE_TOKEN: WRITE_TOKEN},
            reader=lambda name: ("off", "lkg"),
        )
        self.assertEqual((code, answer["lkg"]), (0, "patched"), answer)
        after, _ = lkg.read()
        self.assertEqual(after["knobs"]["FLEET_AUTOPUSH_MODE"], "snapshot")
        self.assertEqual(after["saved_epoch"], before["saved_epoch"])

    def test_no_copy_is_reported(self):
        answer, code = self.run_set(
            "FLEET_AUTOPUSH_MODE",
            "report",
            "yes",
            {setter.ENV_WRITE_TOKEN: WRITE_TOKEN},
            reader=lambda name: ("off", "lkg"),
        )
        self.assertEqual((code, answer["lkg"]), (0, "not-refreshed:absent"))


class CliTests(SetTestCase):
    def test_set_plan_json(self):
        out = io.StringIO()
        code = cli.main(["set", "CLAIM_EXPIRY_MODE", "ping-only", "--plan", "--json"], out=out)
        answer = json.loads(out.getvalue())
        self.assertEqual((code, answer["result"], answer["new"]), (0, "planned", "ping-only"))
        self.assertEqual(self.set_calls(), [])

    def test_set_refuses_secret_text(self):
        out = io.StringIO()
        code = cli.main(["set", "FLEET_FACTS_SIGNING_KEY", SIGNING, "--plan"], out=out)
        self.assertEqual(code, 2)
        self.assertNotIn(SIGNING, out.getvalue())
