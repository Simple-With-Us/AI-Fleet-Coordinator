"""Q5: git-relay is the ONE path that mints per-bot GitHub App tokens.

Owner decision 2026-10-22.  The Operator GitHub token broker (OPS-966) is kept
for review history and is not a minting path:  it stays disabled by default,
and both of its minting primitives now fail closed naming git-relay.  These
tests are the executable form of that decision - they fail the moment a second
minting path appears, or the moment the broker can mint again.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_sync.github_token_router import (
    MINTING_OWNER,
    RoutingError,
    TokenRouter,
    refuse_minting,
)
from agent_sync.operator_github import Broker, Refused, signature
from agent_sync.operator_github_policy import (
    app_credentials_loader,
    caller_key_loader,
    load_policy,
)
from agent_sync.operator_github_state import NonceLedger

REPO_ROOT = Path(__file__).resolve().parents[3]
# The GitHub endpoint that mints an App installation token.  Exactly one module
# in this repository may be allowed to reach it.
MINT_ENDPOINT = "access_tokens"
GUARD_CALL = "refuse_minting"
OWNER_MODULE = "git-relay/git_relay/app_tokens.py"


def load_repo_token():
    """Import the standalone OPS-966 prototype by path (not on sys.path)."""
    path = REPO_ROOT / "scripts" / "operator" / "github_tokens" / "repo_token.py"
    spec = importlib.util.spec_from_file_location("ops966_repo_token", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OnlyGitRelayMints(unittest.TestCase):
    """No second minting path may exist anywhere in the repository."""

    def _mint_sites(self):
        sites = {}
        for path in REPO_ROOT.rglob("*.py"):
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if MINT_ENDPOINT in text:
                sites[path.relative_to(REPO_ROOT).as_posix()] = GUARD_CALL in text
        return sites

    def test_scan_actually_finds_the_mint_sites(self):
        # If this fails the scan is broken, not the code:  a scan that matches
        # nothing would make every other assertion here vacuous.
        sites = self._mint_sites()
        self.assertIn(OWNER_MODULE, sites, "git-relay's minter must be visible to the scan")
        self.assertFalse(sites[OWNER_MODULE], "git-relay owns minting and must not be guarded")

    def test_git_relay_is_the_only_unguarded_minting_module(self):
        unguarded = sorted(rel for rel, guarded in self._mint_sites().items() if not guarded)
        self.assertEqual(unguarded, [OWNER_MODULE])

    def test_every_other_mint_site_fails_closed(self):
        for rel, guarded in self._mint_sites().items():
            if rel != OWNER_MODULE:
                self.assertTrue(guarded, f"{rel} can mint a seat GitHub App token")


class OperatorMintPrimitivesRefuse(unittest.TestCase):
    """Both real Operator mint primitives refuse, before any credential read."""

    def _router(self, module, loader):
        return module.TokenRouter(loader, Path(tempfile.mkdtemp()) / "cache")

    def test_operator_router_refuses_and_names_git_relay(self):
        loader_calls = []

        def loader(slug):
            loader_calls.append(slug)
            raise AssertionError("credentials must never be read once the guard fires")

        router = self._router(importlib.import_module("agent_sync.github_token_router"), loader)
        with self.assertRaises(RoutingError) as ctx:
            router.token_for("swu-echo", "a/b", {"contents": "read"})
        self.assertIn(MINTING_OWNER, str(ctx.exception))
        self.assertIn("will not mint", str(ctx.exception))
        self.assertEqual(loader_calls, [])

    def test_repo_token_prototype_refuses_too(self):
        module = load_repo_token()
        self.assertEqual(module.MINTING_OWNER, MINTING_OWNER)
        loader_calls = []

        def loader(slug):
            loader_calls.append(slug)
            raise AssertionError("credentials must never be read once the guard fires")

        router = self._router(module, loader)
        # This prototype carries its own RoutingError class, so assert on its own.
        with self.assertRaises(module.RoutingError) as ctx:
            router.token_for("swu-echo", "a/b", {"contents": "read"})
        self.assertIn(MINTING_OWNER, str(ctx.exception))
        self.assertEqual(loader_calls, [])

    def test_the_guard_takes_no_configuration(self):
        # The refusal is a hard constant:  no environment variable, flag or
        # module toggle can turn minting back on here.
        source = __import__("inspect").getsource(refuse_minting)
        self.assertNotIn("environ", source)
        self.assertNotIn("getenv", source)
        noisy = {
            "GITHUB_TOKEN_ROUTER_ENABLED": "1",
            "OPERATOR_GITHUB_BROKER_ENABLED": "1",
            "OPS966_MINTING": "1",
            "GIT_RELAY_APP_MINT": "1",
        }
        saved = {k: os.environ.get(k) for k in noisy}
        try:
            os.environ.update(noisy)
            with self.assertRaises(RoutingError):
                refuse_minting()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class OperatorBrokerCannotMint(unittest.TestCase):
    """The broker itself: disabled by default, and unable to mint even if forced on."""

    def test_broker_is_disabled_by_default(self):
        broker = Broker(
            key_for=lambda seat: "k" * 64,
            policy_for=load_policy,
            router=None,
            bot_identity=lambda slug: {"login": slug + "[bot]", "id": 1},
        )
        self.assertFalse(broker.enabled)

    def test_enabled_broker_with_the_real_router_still_refuses(self):
        loader_calls = []

        def loader(slug):
            loader_calls.append(slug)
            raise AssertionError("a live mint path was reached")

        class FakeSecrets:
            def caller_key(self, seat):
                return "k" * 64

            def app_credentials(self, slug):
                return ("123", "pem")

        secrets = FakeSecrets()
        router = importlib.import_module("agent_sync.github_token_router")
        broker = Broker(
            key_for=caller_key_loader(secrets),
            policy_for=load_policy,
            router=TokenRouter(
                loader, Path(tempfile.mkdtemp()) / "cache", signer=lambda *a: "MOCK"
            ),
            bot_identity=lambda slug: {"login": slug + "[bot]", "id": 7},
            enabled=True,
            nonce_ledger=NonceLedger(tempfile.mkdtemp()),
        )
        self.assertTrue(broker.enabled, "this test is only meaningful when forced on")

        path = "/operator/v1/github/CODEX/token"
        body = json.dumps(
            {
                "repo": "Simple-With-Us/AI-Fleet-Coordinator",
                "permissions": {"contents": "read"},
            },
            sort_keys=True,
        ).encode()
        nonce = "a" * 64
        sig = signature("k" * 64, path, "1000", nonce, body)
        broker.clock = lambda: 1000

        with self.assertRaises(Refused) as caught:
            broker.issue(path, "1000", nonce, sig, body)
        self.assertNotIsInstance(caught.exception, AssertionError)
        self.assertEqual(loader_calls, [], "the guard must fire before any credential read")
        # The routing core named in the refusal keeps the pointer to git-relay.
        self.assertIn(MINTING_OWNER, router.MINTING_REFUSED)
        self.assertEqual(router.MINTING_OWNER, MINTING_OWNER)

    def test_policy_loaders_still_exist_but_never_reach_a_mint(self):
        # The reviewed OPS-966 code is retained, not deleted:  its policy table
        # and loaders still answer, they simply no longer lead to a token.
        module = importlib.import_module("agent_sync.operator_github_policy")

        class FakeSecrets:
            def caller_key(self, seat):
                return "k" * 64

            def app_credentials(self, slug):
                return ("123", "pem")

        creds_for = app_credentials_loader(FakeSecrets())
        self.assertEqual(creds_for("swu-echo"), ("123", "pem"))
        self.assertEqual(load_policy("CODEX").app_slug, "swu-codex")
        self.assertTrue(hasattr(module, "load_policy"))


if __name__ == "__main__":
    unittest.main()