import unittest
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
AFC_PROJECT="11111111-2222-3333-4444-555555555555"
ENV={"OPERATOR_ZULIP_USER_ID":str(OPERATOR_BOT_ID),"OPERATOR_ZULIP_EMAIL":OPERATOR_EMAIL,"INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID":AFC_PROJECT,"ZULIP_SITE":"https://realm.example.test"}
def check_bot(user):
    return operator_bot(user,ENV,12)


class Tests(unittest.TestCase):
    def test_exact_bot(self):
        self.assertTrue(
            check_bot({"owner_id":12,"is_bot": True, "user_id": OPERATOR_BOT_ID, "email": OPERATOR_EMAIL})
        )

    def test_wrong_id(self):
        self.assertFalse(check_bot({"owner_id":12,"is_bot": True, "user_id": 1, "email": OPERATOR_EMAIL}))

    def test_old_admin(self):
        self.assertFalse(
            check_bot(
                {
                    "owner_id":12,"is_bot": True,
                    "user_id": OPERATOR_BOT_ID,
                    "email": "fixture-old-admin@example.test",
                }
            )
        )

    def test_not_bot(self):
        self.assertFalse(
            check_bot({"owner_id":12,"is_bot": False, "user_id": OPERATOR_BOT_ID, "email": OPERATOR_EMAIL})
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
        self.assertFalse(check_bot({"owner_id":99,"is_bot":True,"user_id":OPERATOR_BOT_ID,"email":OPERATOR_EMAIL}))
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
