"""The reviewed seat policy (deploy/seat-policy.json) as the relay and ST-MCP read it.

The file is what `deploy/seat_policy.py install` writes into GIT_RELAY_SEATS_JSON, so
these checks run it through the real SeatPolicy and the real bearer parser, and hold
it to the Worker's own identity lists:  a seat the Worker can mint a bearer for has an
entry here, with its own App, unless the file says why not.
"""

import importlib.util
import json
import re
import unittest
from pathlib import Path

from git_relay.app_tokens import MINT_PERMISSION_CEILING
from git_relay.policy import SeatPolicy
from git_relay.shared_bearer import SharedBearerVerifier
from git_relay.tests.test_bot_compatibility import BOT_IDENTITIES
from git_relay.workflow_verbs import SPECS

RELAY = Path(__file__).resolve().parents[2]
ROOT = RELAY.parent
WORKER = ROOT / "scripts" / "agent-sync-mcp" / "src"

_spec = importlib.util.spec_from_file_location("seat_policy", RELAY / "deploy" / "seat_policy.py")
seat_policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seat_policy)

DOC = seat_policy.load()
SEATS = seat_policy.render(DOC)

# The three Apps whose slug is not swu-<identity>, each read from the App's own GitHub
# description ("Bot identity for the ... agent seat").
NOT_SWU_IDENTITY = {"MA": "swu-rob", "GROK": "swu-grok-build", "MM": "swu-minimax"}
# Not a bearer identity of its own App:  a second login on Jet's bot (Q7).
EXCLUDED = {"JET-CHAT"}


def worker_identities() -> set[str]:
    """Every identity the Worker can mint a bearer for, read from its sources."""
    config = (WORKER / "config.js").read_text(encoding="utf-8")
    known = re.search(r"export const KNOWN_SEATS = Object\.freeze\(\[([^\]]*)\]\)", config)
    names = set(re.findall(r'"([A-Z][A-Z0-9-]*)"', known.group(1)))
    bearer = (WORKER / "bearer-identities.js").read_text(encoding="utf-8")
    row = r'^\s+"?([A-Z][A-Z0-9-]*)"?: Object\.freeze\(\{ prefix:'
    names |= set(re.findall(row, bearer, re.MULTILINE))
    personas = ROOT / "scripts" / "agent_sync" / "mcp" / "gb-identities.json"
    names |= set(json.loads(personas.read_text(encoding="utf-8"))["enabled"])
    return names


class _Verifier:
    """Resolves a bearer to the identity it names, as the Worker does for a live one."""

    def parse_identity(self, token):
        return SharedBearerVerifier.parse_identity(token)

    def resolve(self, token):
        return SharedBearerVerifier.parse_identity(token)


def bearer_for(identity: str) -> str:
    if identity.startswith("GB-"):
        return "gbb_" + identity[3:].lower() + "_" + "a" * 43
    return "sbt_" + identity.lower() + "_" + "a" * 43


class SeatPolicyFile(unittest.TestCase):
    def test_the_relay_loads_the_rendered_policy(self):
        SeatPolicy(seat_policy.compact(SEATS), verifier=_Verifier())

    def test_every_worker_bearer_identity_has_its_own_entry(self):
        identities = {cfg["identity"] for cfg in SEATS.values()}
        self.assertEqual(identities, worker_identities() - EXCLUDED)
        self.assertEqual(identities, set(BOT_IDENTITIES))
        self.assertFalse(identities & EXCLUDED)

    def test_each_identity_acts_as_its_own_app(self):
        for slug, cfg in SEATS.items():
            expected = NOT_SWU_IDENTITY.get(cfg["identity"], "swu-" + cfg["identity"].lower())
            self.assertEqual(slug, expected, cfg["identity"])
            self.assertEqual(cfg["app_slug"], slug)

    def test_a_bearer_reaches_only_its_own_seat(self):
        policy = SeatPolicy(seat_policy.compact(SEATS), verifier=_Verifier())
        for slug, cfg in SEATS.items():
            self.assertEqual(policy.authenticate(bearer_for(cfg["identity"])), slug)
            self.assertEqual(policy.app_identity(slug)[0], slug)
        self.assertIsNone(policy.authenticate(bearer_for("JET-CHAT")))

    def test_no_entry_holds_a_token(self):
        for cfg in SEATS.values():
            self.assertEqual(cfg["auth"], "shared")
            self.assertNotIn("token", cfg)

        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys(value)
            elif isinstance(node, list):
                for value in node:
                    yield from keys(value)

        self.assertNotIn("token", set(keys(DOC)))

    def test_every_action_fits_the_seat_ceiling(self):
        rank = {"read": 1, "write": 2}
        for cfg in SEATS.values():
            ceiling = cfg["mint_permissions"]
            self.assertTrue(set(ceiling) <= set(MINT_PERMISSION_CEILING))
            for action in cfg["actions"]:
                if action not in SPECS:
                    continue
                for name, level in SPECS[action][0].items():
                    need = f"{action} needs {name}:{level}"
                    self.assertIn(name, ceiling, need)
                    self.assertGreaterEqual(rank[ceiling[name]], rank[level], need)

    def test_repositories_are_exact_and_current(self):
        for cfg in SEATS.values():
            for repo in cfg["repos"]:
                self.assertNotIn("*", repo)
                self.assertTrue(repo.startswith("Simple-With-Us/"), repo)
            self.assertIn("Simple-With-Us/Fleet-OPS", cfg["repos"])

    def test_echo_keeps_its_narrower_repositories(self):
        echo = SEATS["swu-echo"]
        fleet = SEATS["swu-claude"]
        self.assertEqual(echo["actions"], fleet["actions"])
        self.assertEqual(echo["mint_permissions"], fleet["mint_permissions"])
        self.assertEqual(
            echo["repos"],
            [
                "Simple-With-Us/AI-Fleet-Coordinator",
                "Simple-With-Us/CodeCaps",
                "Simple-With-Us/Fleet-OPS",
            ],
        )

    def test_render_refuses_a_malformed_file(self):
        good = json.loads(json.dumps(DOC))
        for mutate, message in (
            (lambda d: d["seats"].append(dict(d["seats"][0])), "listed twice"),
            (lambda d: d["seats"][0].update(profile="nope"), "unknown profile"),
            (lambda d: d["profiles"]["echo"].update(actions="echo"), "concrete profile"),
            (lambda d: d["seats"][0].pop("identity"), "identity, app_slug and profile"),
        ):
            doc = json.loads(json.dumps(good))
            mutate(doc)
            with self.assertRaisesRegex(seat_policy.PolicyFileError, message):
                seat_policy.render(doc)

    def test_diff_names_seats_and_fields_only(self):
        old = {k: dict(v) for k, v in SEATS.items() if k != "swu-ag"}
        old["swu-echo"] = dict(old["swu-echo"], repos=["Simple-With-Us/CodeCaps"])
        lines = seat_policy.diff(old, SEATS)
        self.assertIn("added    swu-ag (AG)", lines)
        self.assertTrue(any(line.startswith("changed  swu-echo: repos +") for line in lines))
        self.assertIn("same     swu-claude", lines)


if __name__ == "__main__":
    unittest.main()
