import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_sync import config as C
from agent_sync import identity as I
from agent_sync import zulip as Z
from agent_sync.service_identity import (
    operator_bot,
    operator_credentials,
)


OPERATOR_BOT_ID=43210
OPERATOR_EMAIL="operator@example.test"
AFC_PROJECT=str(uuid.uuid5(uuid.NAMESPACE_URL, 'fixture-project-afc'))
ENV={"OPERATOR_ZULIP_USER_ID":str(OPERATOR_BOT_ID),"OPERATOR_ZULIP_EMAIL":OPERATOR_EMAIL,"INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID":AFC_PROJECT,"ZULIP_SITE":"https://realm.example.test"}
def check_bot(user):
    return operator_bot(user,ENV,12)


class Tests(unittest.TestCase):
    def test_exact_bot(self):
        self.assertTrue(
            check_bot({"bot_owner_id":12,"is_bot": True, "user_id": OPERATOR_BOT_ID, "email": OPERATOR_EMAIL})
        )

    def test_wrong_id(self):
        self.assertFalse(check_bot({"bot_owner_id":12,"is_bot": True, "user_id": 1, "email": OPERATOR_EMAIL}))

    def test_old_admin(self):
        self.assertFalse(
            check_bot(
                {
                    "bot_owner_id":12,"is_bot": True,
                    "user_id": OPERATOR_BOT_ID,
                    "email": "fixture-old-admin@example.test",
                }
            )
        )

    def test_not_bot(self):
        self.assertFalse(
            check_bot({"bot_owner_id":12,"is_bot": False, "user_id": OPERATOR_BOT_ID, "email": OPERATOR_EMAIL})
        )

    def test_not_agent_roster(self):
        with self.assertRaises(I.UnknownSeat):
            I.strict_seat("OPERATOR")

    def test_credential_names(self):
        seen = []

        def fetch(names):
            seen.extend(names)
            return {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "test-key"}

        c = operator_credentials(ENV, fetch=fetch)
        self.assertEqual(c.email, OPERATOR_EMAIL)
        self.assertEqual(seen, ["ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY"])

    def test_missing_site_names_the_real_setting(self):
        with self.assertRaisesRegex(Z.CredentialError, "ZULIP_SITE"):
            operator_credentials(dict(ENV, ZULIP_SITE=""), fetch=lambda names: {
                "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "test-key"})

    def test_unresolved_reference_is_refused_without_leaking_value(self):
        for field in ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY"):
            values = {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "test-key"}
            values[field] = "${PRIVATE-REFERENCE}"
            with self.assertRaises(Z.CredentialError) as caught:
                operator_credentials(ENV, fetch=lambda names: values)
            self.assertIn(field, str(caught.exception))
            self.assertNotIn("PRIVATE-REFERENCE", str(caught.exception))

    def test_wrong_email_fails(self):
        with self.assertRaises(Z.CredentialError):
            operator_credentials(
                ENV,
                fetch=lambda n: {
                    "ZULIP_OPERATOR_EMAIL": "other@test",
                    "ZULIP_OPERATOR_API_KEY": "test",
                },
            )

    def test_error_scrubbed(self):
        def fetch(n):
            raise ValueError("DO-NOT-LOG-SECRET")

        with self.assertRaises(Z.CredentialError) as c:
            operator_credentials(ENV, fetch=fetch)
        self.assertNotIn("DO-NOT-LOG", str(c.exception))

    def test_the_production_reader_is_pinned_to_the_afc_project_and_the_two_names(self):
        seen = {}

        def fake(names, env, home):
            seen["names"] = names
            seen["env"] = dict(env)
            return {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "test-key"}

        with mock.patch.object(Z, "_default_infisical_fetch", fake):
            c = operator_credentials(dict(ENV, HOME="/tmp"), Path("/tmp"))
        self.assertEqual(c.email, OPERATOR_EMAIL)
        self.assertEqual(
            seen["names"], ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY")
        )
        # the AFC /zulip store, not Fleet-OPS /zulip and not the ambient environment
        self.assertEqual(seen["env"][Z.ENV_INFISICAL_PROJECT], AFC_PROJECT)

    def test_wrong_owner_refused(self):
        self.assertFalse(check_bot({"bot_owner_id":99,"is_bot":True,"user_id":OPERATOR_BOT_ID,"email":OPERATOR_EMAIL}))
    def test_missing_project_refused(self):
        with self.assertRaises(Z.CredentialError):operator_credentials({k:v for k,v in ENV.items() if k != "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID"})
    def test_missing_identity_refused(self):
        with self.assertRaises(Z.CredentialError):operator_credentials({})

    def test_no_env_fallback(self):
        from types import SimpleNamespace as NS

        with self.assertRaises(Z.CredentialError):
            C.seat_credentials(NS(seat="OPERATOR", creds="env"), {}, None)

    def test_infisical_switch_off_is_refused_before_any_fetch(self):
        reached = []

        def fetch(names):
            reached.append(names)
            return {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "test-key"}

        with self.assertRaises(Z.CredentialError) as c:
            operator_credentials(dict(ENV, AGENT_SYNC_INFISICAL="0"), fetch=fetch)
        self.assertIn("AGENT_SYNC_INFISICAL", str(c.exception))
        self.assertEqual(reached, [])


class ListenerConnectTests(unittest.TestCase):
    """The pinned Operator keeps the administrator account's own address, so the roster tag
    (seat_tag_for) reads ADMIN, not OPERATOR:  the seat must connect on the service identity."""

    def runner(self, email, env):
        from types import SimpleNamespace as NS
        from agent_sync import daemon as D

        cfg = NS(seat="OPERATOR", bot="Operator", creds="infisical", wake="inbox")
        runner = D.SeatRunner.__new__(D.SeatRunner)
        runner.daemon = NS(
            env=dict(env),
            config=NS(owner_user_id=12, notes_relay_enabled=True, notes_relay_seat="OPERATOR",
                      outgoing_webhook_seats=()),
            realm="https://realm.example.test",
        )
        runner.cfg = cfg
        runner.seat = "OPERATOR"
        runner.creds = NS(email=email, key="test-key", site="https://realm.example.test")
        runner.me = None
        runner.me_person = {}
        runner.streams = {}
        runner.queue = None
        runner.client = NS(get=lambda *a, **k: {"email": email, "role": 400, "is_bot": True,
                                               "bot_owner_id": 12, "user_id": OPERATOR_BOT_ID,
                                               "bot_type": 1})
        return runner

    def test_pinned_operator_connects_despite_admin_roster_tag(self):
        from agent_sync import daemon as D

        email = "admin-bot@example.test"
        env = dict(ENV, OPERATOR_ZULIP_EMAIL=email)
        runner = self.runner(email, env)
        # The roster tag alone would refuse this seat:  it is ADMIN, not OPERATOR.
        self.assertEqual(I.seat_tag_for({"is_bot": True, "email": email}), "ADMIN")
        # It gets past the credential/role/roster checks and only then reaches the subscriptions
        # fetch, which this fake client does not serve:  passing that point is the assertion.
        with self.assertRaises(Exception) as c:
            runner.connect()
        self.assertNotIsInstance(c.exception, D.RoleRefused)

    def test_lookalike_operator_is_still_refused(self):
        from agent_sync import daemon as D

        email = "admin-bot@example.test"
        runner = self.runner(email, dict(ENV, OPERATOR_ZULIP_USER_ID="999"))
        with self.assertRaises(D.RoleRefused):
            runner.connect()


class ConfigTests(unittest.TestCase):
    def config(self, **changes):
        seat = dict(bot="Operator", creds="infisical", wake="inbox")
        seat.update(changes)
        return C.from_dict({"daemon": {"owner_user_id": 12}, "seat": {"OPERATOR": seat}})

    def test_operator_inbox_config(self):
        self.assertEqual(self.config().errors, [])

    def test_operator_cannot_wake_model(self):
        self.assertTrue(self.config(wake="claude").errors)

    def test_operator_cannot_use_file(self):
        self.assertTrue(self.config(creds="file").errors)

    def test_operator_partition_mac(self):
        from pathlib import Path

        partition, error = C.load_partition(
            str(Path(__file__).resolve().parents[3] / "docs/protocols/agent-sync-partition.toml")
        )
        self.assertIsNone(error)
        self.assertEqual(partition["OPERATOR"], "mac")


if __name__ == "__main__":
    unittest.main()


class HostNotesAcceptanceTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from agent_sync.mcp.tools import Tools
        self.bot = {"bot_owner_id": 12, "is_bot": True, "user_id": OPERATOR_BOT_ID,
                    "email": OPERATOR_EMAIL}
        self.tools = Tools.__new__(Tools)
        self.tools.agent = SimpleNamespace(rt=SimpleNamespace(env=dict(ENV)), users=lambda: [self.bot])
        self.tools.notes_consumer = "OPERATOR"
        self.tools.owner_user_id = 12

    def test_host_accepts_only_pinned_operator_and_owner(self):
        self.assertTrue(self.tools._relay_consumer(OPERATOR_BOT_ID))
        self.assertEqual(self.tools._relay_consumer_id(), OPERATOR_BOT_ID)
        for field, wrong in (("bot_owner_id", 99), ("email", "operator-bot@example.test"),
                             ("user_id", 999), ("is_bot", False)):
            original = dict(self.bot)
            self.bot[field] = wrong
            self.assertFalse(self.tools._relay_consumer(self.bot["user_id"]))
            self.assertIsNone(self.tools._relay_consumer_id())
            self.bot = original

    def test_missing_pins_or_owner_fail_closed(self):
        for pins in ({}, dict(ENV, OPERATOR_ZULIP_USER_ID=""), dict(ENV, OPERATOR_ZULIP_EMAIL="")):
            self.tools.agent.rt.env = pins
            self.assertFalse(self.tools._relay_consumer(OPERATOR_BOT_ID))
            self.assertIsNone(self.tools._relay_consumer_id())
        self.tools.agent.rt.env = ENV
        self.tools.owner_user_id = 0
        self.assertFalse(self.tools._relay_consumer(OPERATOR_BOT_ID))
