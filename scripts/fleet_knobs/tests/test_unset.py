"""fleet_knobs unset + audit-failure surfacing."""
from __future__ import annotations
import io, json, os
from fleet_knobs import __main__ as cli
from fleet_knobs import setter
from ._fake import KnobTestCase
WRITE_TOKEN = "fakeWriteTokenSentinel0123456789abcdef"
LIVE = {"FLEET_AUTOPUSH_MODE": "off"}
class UnsetTests(KnobTestCase):
    def setUp(self):
        super().setUp()
        self.fake.set(secrets=LIVE)
        self.audit_file = str(self.home / ".agent-sync" / "listener" / "knobs.jsonl")
    def run_unset(self, name, mode, environ=None, **kw):
        env = dict(os.environ); env.update(environ or {})
        return setter.unset_knob(name, mode, environ=env, audit_file=self.audit_file, **kw)
    def test_plan_shows_delete_and_writes_nothing(self):
        answer, code = self.run_unset("FLEET_AUTOPUSH_MODE", "plan", reader=lambda n: ("off", "infisical"))
        self.assertEqual((code, answer["result"], answer["verb"]), (0, "planned", "unset"))
        self.assertEqual(answer["commands"][0][:3], ["infisical", "secrets", "delete"])
        self.assertIn("FLEET_AUTOPUSH_MODE", answer["commands"][0])
    def test_refuses_unknown_secret_and_no_mode(self):
        for name, mode in [("NOT_A_KNOB", "yes"), ("FLEET_FACTS_SIGNING_KEY", "yes"), ("FLEET_AUTOPUSH_MODE", None)]:
            answer, code = self.run_unset(name, mode, {setter.ENV_WRITE_TOKEN: WRITE_TOKEN})
            self.assertEqual((code, answer["result"]), (2, "refused"), answer)
    def test_yes_requires_token_and_audits(self):
        answer, code = self.run_unset("FLEET_AUTOPUSH_MODE", "yes", platform="linux", reader=lambda n: ("off", "infisical"))
        self.assertEqual((code, answer["detail"]), (2, "no write token"))
        answer, code = self.run_unset("FLEET_AUTOPUSH_MODE", "yes", {setter.ENV_WRITE_TOKEN: WRITE_TOKEN}, reader=lambda n: ("off", "lkg"))
        self.assertEqual((code, answer["result"]), (0, "done"), answer)
        self.assertEqual(answer.get("audit"), "written")
        rows = [json.loads(x) for x in open(self.audit_file) if x.strip()]
        self.assertEqual(rows[-1]["result"], "done"); self.assertIsNone(rows[-1]["new"])
    def test_audit_failure_surfaced_on_set(self):
        blocker = self.home / "blocker"; blocker.write_text("x")
        bad = str(blocker / "knobs.jsonl")
        env = dict(os.environ); env[setter.ENV_WRITE_TOKEN] = WRITE_TOKEN
        answer, code = setter.set_knob("FLEET_AUTOPUSH_MODE", "report", "yes", environ=env, audit_file=bad, reader=lambda n: ("off", "infisical"))
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer.get("audit"), "failed")
    def test_cli_unset_plan_json(self):
        out = io.StringIO()
        code = cli.main(["unset", "FLEET_AUTOPUSH_MODE", "--plan", "--json"], out=out)
        answer = json.loads(out.getvalue())
        self.assertEqual((code, answer["verb"], answer["result"]), (0, "unset", "planned"))
