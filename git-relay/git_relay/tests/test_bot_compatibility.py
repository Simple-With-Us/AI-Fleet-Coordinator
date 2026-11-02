"""Synthetic code compatibility for persistent identities, never real provisioning."""

import json
import unittest

from git_relay.policy import SeatPolicy
from git_relay.shared_bearer import SharedBearerVerifier

BOT_IDENTITIES = (
    "ECHO",
    "WISE",
    "JET-CHAT",
    "GROK-WEB",
    "CURSOR",
    "CLAUDE",
    "MA",
    "CODEX",
    "AG",
    "CLUTCH",
    "FX",
    "GROK",
    "MC",
    "MM",
    "OPENCODE",
    "AMP",
    "BF-COMPILER",
    "BF-DEPLOYER",
    "BF-DESIGNER",
    "BF-DIRECTOR",
    "BF-ENGINEER",
    "BF-TRADER",
    "BF-FIXER",
    "BF-HOUSEKEEPER",
    "BF-MONITOR",
    "BF-ORACLE",
    "BF-PLUMBER",
    "BF-PUBLISHER",
    "GB-DIRECTOR",
    "GB-FIXER",
    "GB-DESIGNER",
    "GB-COMPILER",
    "GB-HOUSEKEEPER",
    "GB-PUBLISHER",
    "GB-DEPLOYER",
    "GB-MONITOR",
    "GB-PLUMBER",
    "GB-ORACLE",
    "GB-TRADER",
    "GB-ENGINEER",
)


class BotCompatibility(unittest.TestCase):
    def test_each_bot_can_have_a_distinct_own_app_without_engine_selection(self):
        for identity in BOT_IDENTITIES:
            principal = "swu-jet" if identity == "JET-CHAT" else "swu-" + identity.lower()
            cfg = {
                principal: {
                    "auth": "shared",
                    "identity": identity,
                    "app_slug": principal,
                    "actions": ["wf_pr_list"],
                    "repos": ["example/repo"],
                    "mint_permissions": {"pull_requests": "read", "metadata": "read"},
                }
            }

            class Verifier:
                def __init__(self, assigned):
                    self.assigned = assigned

                def parse_identity(self, token):
                    return self.assigned

                def resolve(self, token):
                    return self.assigned

            policy = SeatPolicy(json.dumps(cfg), verifier=Verifier(identity))
            self.assertEqual(policy.authenticate("synthetic-bearer"), principal)
            self.assertEqual(
                policy.app_identity(principal),
                (principal, {"pull_requests": "read", "metadata": "read"}),
            )
            self.assertIsNone(policy.authorize(principal, "wf_pr_list", "example/repo"))
            self.assertIsNotNone(policy.authorize(principal, "wf_pr_list", "other/repo"))

    def test_existing_bearer_shapes_cover_bot_roles_and_do_not_name_engines(self):
        for identity in BOT_IDENTITIES:
            if identity.startswith("GB-"):
                token = "gbb_" + identity[3:].lower() + "_" + "a" * 43
            else:
                token = "sbt_" + identity.lower() + "_" + "a" * 43
            self.assertEqual(SharedBearerVerifier.parse_identity(token), identity)
        # Existing JET-CHAT login context is accepted without inventing a new bot alias.
        self.assertEqual(
            SharedBearerVerifier.parse_identity("sbt_jet-chat_" + "a" * 43), "JET-CHAT"
        )
