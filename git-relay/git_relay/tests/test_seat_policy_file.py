"""The reviewed seat policy (deploy/seat-policy.json) as the relay and ST-MCP read it.

The file is what `deploy/seat_policy.py install` writes into GIT_RELAY_SEATS_JSON, so
these checks run it through the real SeatPolicy and the real bearer parser, and hold
it to the Worker's own identity lists:  a seat the Worker can mint a bearer for has an
entry here, with its own App, unless the file says why not.  Fixtures that only need
some repository use a synthetic one.
"""

import importlib.util
import inspect
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from git_relay.app_tokens import MINT_PERMISSION_CEILING
from git_relay.policy import SeatPolicy
from git_relay.server import ACTION_REQUIRED_PERMISSIONS
from git_relay.shared_bearer import SharedBearerVerifier
from git_relay.tests.test_bot_compatibility import BOT_IDENTITIES
from git_relay.workflow_verbs import SPECS

RELAY = Path(__file__).resolve().parents[2]
ROOT = RELAY.parent
WORKER = ROOT / "scripts" / "agent-sync-mcp" / "src"

_spec = importlib.util.spec_from_file_location("seat_policy", RELAY / "deploy" / "seat_policy.py")
seat_policy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = seat_policy  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(seat_policy)

DOC = seat_policy.load()
SEATS = seat_policy.render(DOC)
ORG = "Simple-With-Us/"
SYNTHETIC_REPO = "example-org/example-repo"

# The five Apps whose slug is not swu-<identity>, each read from the App's own GitHub
# description ("Bot identity for the ... agent seat").
NOT_SWU_IDENTITY = {
    "MA": "swu-rob",
    "GROK": "swu-grok-build",
    "MM": "swu-minimax",
    "JET-CHAT": "swu-jet",
    "AMP": "swu-amp-agent",
}
# Explicit policy exclusions, documented in the deployed policy's header.
# Original Jet is wake-only and has no GitHub App entry.
EXCLUDED = {"JET"}
# The one retired seat (host record 2026-11-01):  pinned legacy scope, excluded from the widening.
RETIRED_SLUG = "swu-bf-builder"


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
    """The bearer parser for parse_identity;  resolve() stands for the Worker's answer, so
    it confirms the named identity only while `confirmed` is true."""

    def __init__(self, confirmed: bool = True):
        self.confirmed = confirmed

    def parse_identity(self, token):
        return SharedBearerVerifier.parse_identity(token)

    def resolve(self, token):
        return SharedBearerVerifier.parse_identity(token) if self.confirmed else None


def bearer_for(identity: str) -> str:
    if identity.startswith("GB-"):
        return "gbb_" + identity[3:].lower() + "_" + "a" * 43
    return "sbt_" + identity.lower() + "_" + "a" * 43


def fake_stat(uid: int, mode: int):
    return SimpleNamespace(st_uid=uid, st_mode=mode)


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

    def test_a_confirmed_bearer_reaches_only_its_own_seat(self):
        policy = SeatPolicy(seat_policy.compact(SEATS), verifier=_Verifier())
        for slug, cfg in SEATS.items():
            self.assertEqual(policy.authenticate(bearer_for(cfg["identity"])), slug)
            self.assertEqual(policy.app_identity(slug)[0], slug)
        self.assertIsNone(policy.authenticate(bearer_for("JET")))

    def test_only_full_jet_login_reaches_jet_app_even_if_old_bearer_is_verified(self):
        policy = SeatPolicy(seat_policy.compact(SEATS), verifier=_Verifier())
        self.assertEqual(policy.authenticate(bearer_for("JET-CHAT")), "swu-jet")
        self.assertIsNone(policy.authenticate(bearer_for("JET")))
        self.assertEqual(SEATS["swu-jet"]["identity"], "JET-CHAT")
        self.assertEqual(SEATS["swu-jet"]["app_slug"], "swu-jet")
        self.assertEqual(sum(c["app_slug"] == "swu-jet" for c in SEATS.values()), 1)
        for action in ("wf_repo_get", "wf_pr_get"):
            self.assertIsNone(
                policy.authorize("swu-jet", action, "Simple-With-Us/AI-Fleet-Coordinator")
            )

    def test_amp_acts_only_as_its_own_app(self):
        self.assertNotIn(
            "swu-amp", SEATS
        )  # that slug is reserved for an account, no App exists under it
        self.assertIn("AMP", worker_identities())
        self.assertIn("AMP", DOC["//"])
        policy = SeatPolicy(seat_policy.compact(SEATS), verifier=_Verifier())
        self.assertEqual(policy.authenticate(bearer_for("AMP")), "swu-amp-agent")
        self.assertEqual(SEATS["swu-amp-agent"]["identity"], "AMP")
        self.assertEqual(sum(row["identity"] == "AMP" for row in SEATS.values()), 1)
        self.assertEqual(sum(row["app_slug"] == "swu-amp-agent" for row in SEATS.values()), 1)
        for other in ("OPENCODE", "MM", "JET"):
            self.assertNotEqual(policy.authenticate(bearer_for(other)), "swu-amp-agent")

    def test_a_bearer_the_worker_does_not_confirm_reaches_no_seat(self):
        # The token shape alone names a seat;  only the Worker's answer may admit it.
        policy = SeatPolicy(seat_policy.compact(SEATS), verifier=_Verifier(confirmed=False))
        for cfg in SEATS.values():
            self.assertIsNone(policy.authenticate(bearer_for(cfg["identity"])), cfg["identity"])

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
                if action == "open_pr":
                    continue  # the owner's PR-only token, never the seat's App
                if action in SPECS:
                    needs = SPECS[action][0]
                else:
                    self.assertIn(action, ACTION_REQUIRED_PERMISSIONS, f"{action} has no needs")
                    needs = ACTION_REQUIRED_PERMISSIONS[action]
                for name, level in needs.items():
                    need = f"{action} needs {name}:{level}"
                    self.assertIn(name, ceiling, need)
                    self.assertGreaterEqual(rank[ceiling[name]], rank[level], need)

    def test_repositories_are_the_two_reviewed_namespaces(self):
        # 2026-11-01 every-repo widening (owner instruction):  each seat reaches every
        # repository in the two verified namespaces - the org and Jay's personal account -
        # through the owner/* form the relay engine matches (policy._repo_allowed).  A
        # repository the org moved in keeps its old owner's name only as a redirect, so the
        # org namespace uses the current owner.  Nothing broader passes:  no bare "*", no
        # other owner, and no per-seat variation.
        reviewed = ["Simple-With-Us/*", "jaywedgeworth22/*"]
        for slug, cfg in SEATS.items():
            if slug == RETIRED_SLUG:
                continue
            self.assertEqual(cfg["repos"], reviewed, slug)
            self.assertEqual(len(cfg["repos"]), len(set(cfg["repos"])))
            for pattern in cfg["repos"]:
                self.assertTrue(pattern.endswith("/*"), pattern)
                self.assertNotEqual(pattern, "*")

    def test_every_seat_shares_the_widened_scope(self):
        # Echo's profile survives so its seat entry stays stable, but after the widening it
        # resolves to the fleet set;  every ACTIVE seat carries exactly the two reviewed
        # namespaces (the retired seat's pin is the next test).
        self.assertEqual(DOC["profiles"]["echo"]["repos"], "fleet")
        fleet = SEATS["swu-claude"]
        echo = SEATS["swu-echo"]
        self.assertEqual(echo["actions"], fleet["actions"])
        self.assertEqual(echo["mint_permissions"], fleet["mint_permissions"])
        for slug, cfg in SEATS.items():
            if slug == RETIRED_SLUG:
                continue
            self.assertEqual(cfg["repos"], fleet["repos"], slug)
            self.assertEqual(
                cfg["actions"],
                fleet["actions"] + (["wf_repo_get", "wf_pr_get"] if slug == "swu-jet" else []),
                slug,
            )
            self.assertEqual(cfg["mint_permissions"], fleet["mint_permissions"], slug)

    def test_the_retired_builder_keeps_its_pinned_legacy_scope(self):
        # BF-BUILDER is retired (host record 2026-11-01):  the every-repo widening must not
        # reactivate or widen a retired identity, so its profile pins the exact pre-widening
        # fleet scope as literal copies.  Any drift in the file or a second seat attaching
        # to the pin fails here.
        legacy_repos = [
            "Simple-With-Us/AI-Fleet-Coordinator",
            "Simple-With-Us/Autorotate",
            "Simple-With-Us/BotFleet",
            "Simple-With-Us/Clutch",
            "Simple-With-Us/CodeCaps",
            "Simple-With-Us/Congress.Trade",
            "Simple-With-Us/ContactLogo",
            "Simple-With-Us/DealDex",
            "Simple-With-Us/Fleet-OPS",
            "Simple-With-Us/FleetLink",
            "Simple-With-Us/HogHunter",
            "Simple-With-Us/Kodus-Config",
            "Simple-With-Us/Personal-Site",
            "Simple-With-Us/Simple-With-Us",
            "Simple-With-Us/Socratic-Trade",
            "Simple-With-Us/Usage-Monitor",
            "Simple-With-Us/congress-trading-shared",
            "Simple-With-Us/homebrew-tap",
        ]
        cfg = SEATS[RETIRED_SLUG]
        fleet = SEATS["swu-claude"]
        self.assertEqual(cfg["repos"], legacy_repos)
        self.assertEqual(cfg["actions"], fleet["actions"])
        self.assertEqual(cfg["mint_permissions"], fleet["mint_permissions"])
        pinned = [row["identity"] for row in DOC["seats"] if row["profile"] == "retired-builder"]
        self.assertEqual(pinned, ["BF-BUILDER"])
        self.assertNotIn("*", ",".join(cfg["repos"]))

    def test_the_reviewed_scope_is_pinned(self):
        # The rendered policy's digest, pinned here apart from the file:  any change to a
        # seat's repositories, actions or permission ceiling fails until this line is edited
        # too, so a widening can never ride in on the policy file alone.  When a policy
        # change is meant, run `seat_policy.py install` for the new digest and update it here.
        self.assertEqual(seat_policy.digest(SEATS), "0ec869183fdbacbd")

    def test_render_refuses_a_malformed_file(self):
        good = json.loads(json.dumps(DOC))
        for mutate, message in (
            (lambda d: d["seats"].append(dict(d["seats"][0])), "listed twice"),
            (lambda d: d["seats"][0].update(profile="nope"), "unknown profile"),
            (lambda d: d["profiles"]["echo"].update(actions="echo"), "concrete profile"),
            (lambda d: d["seats"][0].pop("identity"), "identity, app_slug and profile"),
            # A per-seat narrowing the renderer would drop must be refused, not ignored.
            (lambda d: d["seats"][0].update(repos=[SYNTHETIC_REPO]), "unknown keys"),
            (lambda d: d["profiles"]["fleet"].update(ceiling={}), "unknown keys"),
            (lambda d: d.update(extra=1), "unknown keys"),
        ):
            doc = json.loads(json.dumps(good))
            mutate(doc)
            with self.assertRaisesRegex(seat_policy.PolicyFileError, message):
                seat_policy.render(doc)

    def test_root_refuses_a_copy_another_account_can_write(self):
        target = Path("/tmp/fixture/deploy/seat_policy.py")
        root_dir = fake_stat(0, stat.S_IFDIR | 0o755)
        cases = {
            "a file another account owns": fake_stat(1000, stat.S_IFREG | 0o644),
            "a root-owned file the group can write": fake_stat(0, stat.S_IFREG | 0o664),
            "a root-owned file anyone can write": fake_stat(0, stat.S_IFREG | 0o646),
            "a symlink": fake_stat(0, stat.S_IFLNK | 0o777),
        }
        for label, file_stat in cases.items():
            with self.subTest(label):

                def lstat(path, file_stat=file_stat):
                    return file_stat if Path(path) == target else root_dir

                with self.assertRaisesRegex(SystemExit, "root-owned"):
                    seat_policy.require_root_owned([target], lstat=lstat)

        def writable_folder(path):
            if Path(path) == target:
                return fake_stat(0, stat.S_IFREG | 0o644)
            if Path(path) == target.parent:
                return fake_stat(0, stat.S_IFDIR | 0o775)
            return root_dir

        with self.assertRaisesRegex(SystemExit, "root-owned"):
            seat_policy.require_root_owned([target], lstat=writable_folder)

        def all_root(path):
            return fake_stat(0, stat.S_IFREG | 0o644) if Path(path) == target else root_dir

        seat_policy.require_root_owned([target], lstat=all_root)

    def test_the_root_guard_reads_links_with_os_lstat(self):
        # The default must be os.lstat:  os.stat would follow a symlink to its target.
        default = inspect.signature(seat_policy.require_root_owned).parameters["lstat"].default
        self.assertIs(default, os.lstat)
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder, "real.py")
            real.write_text("# a file\n", encoding="utf-8")
            link = Path(folder, "seat_policy.py")
            link.symlink_to(real)

            def root_owned_but_real_mode(path):
                # Ownership faked to root so only the real os.lstat mode decides.
                if Path(path) == link:
                    return fake_stat(0, os.lstat(path).st_mode)
                return fake_stat(0, stat.S_IFDIR | 0o755)

            with self.assertRaisesRegex(SystemExit, "root-owned"):
                seat_policy.require_root_owned([link], lstat=root_owned_but_real_mode)

    def test_the_unit_is_the_only_deployment_source(self):
        show = "\n".join(
            [
                "WorkingDirectory=/tmp/fixture/relay/code",
                "EnvironmentFiles=/tmp/fixture/relay/relay.env (ignore_errors=no)",
                "Environment=PATH=/usr/bin GIT_RELAY_HOST=localhost GIT_RELAY_PORT=18080",
            ]
        )
        cat = "[Service]\nLoadCredential=other-token:/tmp/fixture/other-token\nLoadCredential=git-relay-apps-token:/tmp/fixture/apps-token\n"
        unit = seat_policy.read_unit("relay.service", show=show, cat=cat)
        self.assertEqual(unit.relay, "/tmp/fixture/relay/code")
        self.assertEqual(unit.config, "/tmp/fixture/relay/relay.env")
        self.assertEqual(unit.apps_token, "/tmp/fixture/apps-token")
        self.assertEqual(unit.health, "http://localhost:18080/healthz")
        # No assumed bind address:  a unit that sets none cannot be health-checked.
        no_bind = seat_policy.read_unit(
            "relay.service", show=show.split(" GIT_RELAY_HOST")[0], cat=cat
        )
        with self.assertRaisesRegex(SystemExit, "no health URL"):
            _ = no_bind.health
        for broken_show, broken_cat in (
            ("EnvironmentFiles=/tmp/fixture/relay/relay.env (ignore_errors=no)", cat),
            (show, "[Service]\n"),
            (show + "\nEnvironmentFiles=/tmp/fixture/relay/second.env (ignore_errors=no)", cat),
            # `systemctl show` may print several EnvironmentFiles space-joined on one line.
            (
                show.replace(
                    "relay.env (ignore_errors=no)",
                    "relay.env (ignore_errors=no) /tmp/fixture/relay/second.env (ignore_errors=yes)",
                ),
                cat,
            ),
        ):
            with self.assertRaises(SystemExit):
                seat_policy.read_unit("relay.service", show=broken_show, cat=broken_cat)

    def test_blob_id_and_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder, "empty")
            empty.write_bytes(b"")
            # `git hash-object` of an empty file.
            self.assertEqual(seat_policy.blob_id(empty), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertRegex(seat_policy.digest(SEATS), r"^[0-9a-f]{16}$")
        narrowed = {k: v for k, v in SEATS.items() if k != "swu-ag"}
        self.assertNotEqual(seat_policy.digest(narrowed), seat_policy.digest(SEATS))

    def test_diff_names_seats_and_fields_only(self):
        old = {k: dict(v) for k, v in SEATS.items() if k != "swu-ag"}
        old["swu-echo"] = dict(old["swu-echo"], repos=[SYNTHETIC_REPO])
        lines = seat_policy.diff(old, SEATS)
        self.assertIn("added    swu-ag (AG)", lines)
        self.assertTrue(any(line.startswith("changed  swu-echo: repos +") for line in lines))
        self.assertIn("same     swu-claude", lines)


if __name__ == "__main__":
    unittest.main()


class SeatPolicyOauthAssertRender(unittest.TestCase):
    """The oauth_assert per-seat opt-in renders only as an explicit true."""

    DOC_MIN = {
        "profiles": {
            "p": {
                "actions": ["open_pr"],
                "mint_permissions": {"metadata": "read"},
                "repos": [SYNTHETIC_REPO],
            }
        },
        "seats": [
            {"identity": "CODEX", "app_slug": "swu-codex", "profile": "p"},
            {"identity": "ECHO", "app_slug": "swu-echo", "profile": "p", "oauth_assert": True},
        ],
    }

    def test_flag_renders_only_when_true(self):
        seats = seat_policy.render(self.DOC_MIN)
        self.assertNotIn("oauth_assert", seats["swu-codex"])
        self.assertIs(seats["swu-echo"]["oauth_assert"], True)

    def test_non_true_flag_is_refused(self):
        for bad in (False, "true", 1):
            doc = json.loads(json.dumps(self.DOC_MIN))
            doc["seats"][1]["oauth_assert"] = bad
            with self.assertRaises(seat_policy.PolicyFileError):
                seat_policy.render(doc)

    def test_the_reviewed_file_still_renders_for_the_deployed_policy(self):
        # The reviewed file itself carries no oauth_assert yet:  the relay
        # accepts the rendered JSON unchanged, assertions refused everywhere.
        policy = SeatPolicy(seat_policy.compact(SEATS), assertion_verifier=None)
        self.assertIsNone(
            policy.authenticate(
                "gra1_codex_1800000000_1800000090_AbCdEfGhIjKlMnOp_"
                "6c83d4e7a1b73c7c8afcc28641b50e8afe2b53ec21ed554f4c96947ca255f334"
            )
        )


class HostedOAuthReviewedPolicyTests(unittest.TestCase):
    def test_full_hosted_oauth_flags_only(self):
        import json
        import pathlib

        data = json.loads(
            (
                pathlib.Path(__file__).resolve().parents[2] / "deploy" / "seat-policy.json"
            ).read_text()
        )
        flagged = {row["identity"] for row in data["seats"] if row.get("oauth_assert")}
        self.assertEqual(
            flagged, {"CLAUDE", "CODEX", "CURSOR", "ECHO", "GROK-WEB", "JET-CHAT", "WISE"}
        )
        self.assertNotIn("JET", flagged)
