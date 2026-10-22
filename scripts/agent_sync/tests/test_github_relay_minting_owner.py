"""Q5: git-relay is the ONE path that mints per-bot GitHub App tokens.

Owner decision 2026-10-22.  The Operator GitHub token broker (OPS-966) is kept
for review history and is not a minting path:  it stays disabled by default,
and both of its minting primitives now fail closed naming git-relay.  These
tests are the executable form of that decision - they fail the moment a second
minting path appears, or the moment the broker can mint again.
"""

import ast
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_sync.github_token_router import (
    MINTING_OWNER,
    RoutingError,
    TokenRouter,
    refuse_minting,
)
from agent_sync.operator_github import Broker, CallerPolicy, Refused, signature
from agent_sync.operator_github_policy import (
    app_credentials_loader,
    caller_key_loader,
    load_policy,
)
from agent_sync.operator_github_state import NonceLedger

REPO_ROOT = Path(__file__).resolve().parents[3]
# The GitHub endpoint that mints an App installation token.  Exactly one module
# in this repository may be allowed to reach it.
#
# The literal is split on purpose.  This module has to hold the endpoint to be
# able to search for it, and the scan below treats "mentions the endpoint" as a
# mint site - so writing it out in one piece would make the verifier its own
# violation and would require an exemption list, which is exactly the kind of
# hole this scan exists to close.  Split, this file contains no mint site and
# needs no exemption.
MINT_ENDPOINT = "access_" + "tokens"
GUARD_CALL = "refuse_minting"
OWNER_MODULE = "git-relay/git_relay/app_tokens.py"
# This module, by path, for the assertion that it stays out of its own scan.
SCAN_MODULE = "scripts/agent_sync/tests/test_github_relay_minting_owner.py"
# Synthetic owner/repository for the broker fixture.  A committed test fixture
# must not carry a real private org/repo name (Kody rule: synthetic hostnames
# and paths in test fixtures).  The policy below is synthetic to match, so the
# request still passes the broker's repository check and reaches the mint guard
# - otherwise the test would prove only that an unauthorized repo is refused.
SYNTHETIC_REPO = "acme-org/acme-repo"
SYNTHETIC_POLICY = CallerPolicy(
    app_slug="swu-codex",
    repositories={SYNTHETIC_REPO: {"contents": "read", "metadata": "read"}},
)


def load_repo_token():
    """Import the standalone OPS-966 prototype by path (not on sys.path)."""
    path = REPO_ROOT / "scripts" / "operator" / "github_tokens" / "repo_token.py"
    spec = importlib.util.spec_from_file_location("ops966_repo_token", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MintScanError(RuntimeError):
    """The mint-site inventory could not be built, so no claim may rest on it."""


def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
    """True when `node` is the leading statement of a module/class/function body.

    Prose about minting is not a mint site.  Without this, a module or function
    docstring that merely names the endpoint would be indistinguishable from a
    live call.
    """
    body = getattr(parent, "body", None)
    return bool(body) and body[0] is node


def _module_level_mentions(tree: ast.AST) -> bool:
    """True when the endpoint appears in a string that lives outside every function.

    A module-level constant (MINT_PATH = "/app/installations/1/" + MINT_ENDPOINT)
    or a module-level call is a mint site in its own right:  no per-function
    guard can cover code that runs at import time, so a guarded function in the
    same file must not be able to vouch for it.  The endpoint is written in two
    pieces here for the same reason MINT_ENDPOINT itself is:  this module must
    not contain the literal, or it becomes its own mint site.

    The walk descends into classes but stops at every function boundary, so a
    mention inside a function stays that function's business (_mint_functions).
    """
    stack: list[tuple[ast.AST, ast.AST | None]] = [
        (child, tree) for child in ast.iter_child_nodes(tree)
    ]
    while stack:
        node, parent = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # A docstring is a Constant wrapped in a leading Expr statement.
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and _is_docstring(node, parent)
        ):
            continue
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and MINT_ENDPOINT in node.value
        ):
            return True
        stack.extend((child, node) for child in ast.iter_child_nodes(node))
    return False


def _has_direct_guard(node: ast.AST) -> bool:
    """True when refuse_minting() is a statement of the function body itself.

    Scoped to `node.body` on purpose.  An unscoped search over the whole subtree
    accepts a guard that can never run - `if False: refuse_minting()` - or one
    that is swallowed (`try: refuse_minting() except Exception: pass`), and
    either of those lets a live mint path pass as "guarded".
    """
    for stmt in getattr(node, "body", []):
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and (
                getattr(stmt.value.func, "id", None) == GUARD_CALL
                or getattr(stmt.value.func, "attr", None) == GUARD_CALL
            )
        ):
            return True
    return False


def _mint_functions(text: str) -> list[tuple[str, bool]]:
    """(function name, guarded) for every function that owns a mint site.

    Guardedness is decided per CALL SITE, never per file:  a file that merely
    mentions refuse_minting once must not be able to hide a second, unguarded
    mint path behind that mention.  Every function whose own source mentions
    the mint endpoint must call refuse_minting as a statement of its own body.

    The guard itself is skipped:  refuse_minting is the refusal, not a mint
    site, and its docstring talks about minting by design.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise MintScanError(f"cannot parse Python source: {exc}") from exc
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == GUARD_CALL:
            continue
        # Prose is prose:  a function that only documents the endpoint in its
        # docstring does not call it, exactly as a module docstring does not
        # make a module a mint site (see _module_level_mentions).  Search the
        # statements, not the whole source segment.
        stmts = node.body
        if (
            stmts
            and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)
        ):
            stmts = stmts[1:]
        source = "\n".join(ast.get_source_segment(text, stmt) or "" for stmt in stmts)
        if MINT_ENDPOINT not in source:
            continue
        found.append((node.name, _has_direct_guard(node)))
    return found


def scan_mint_sites(root: Path) -> dict[str, bool]:
    """Map repo-relative path -> is every mint site in it guarded.

    Raises MintScanError rather than skipping a file it cannot read or parse:
    a partial inventory would let this module's claims pass vacuously.
    """
    sites: dict[str, bool] = {}
    for path in sorted(root.rglob("*.py")):
        if ".git" in path.parts or "node_modules" in path.parts:
            continue
        try:
            # errors="replace" keeps the ASCII endpoint detectable inside a
            # file that is not valid UTF-8, so a decoding problem can neither
            # hide a mint site nor fail this whole gate for an unrelated file
            # somewhere in the tree (.venv, a fixture, a vendored copy).
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise MintScanError(
                f"cannot read {path.relative_to(root).as_posix()}: {exc}"
            ) from exc
        if MINT_ENDPOINT not in text:
            continue
        functions = _mint_functions(text)
        tree = ast.parse(text)
        # A mention outside every function (module constant, module-level call)
        # runs at import time and no per-function guard can cover it, so it
        # makes the file unguarded on its own.
        module_level = _module_level_mentions(tree)
        if not module_level and not functions:
            # The endpoint appears only in prose - a module docstring, a
            # function docstring or a comment.  Prose is not a mint site, so
            # the file has nothing to guard and is not an inventory entry.
            continue
        sites[path.relative_to(root).as_posix()] = not module_level and all(
            guarded for _name, guarded in functions
        )
    return sites


class OnlyGitRelayMints(unittest.TestCase):
    """No second minting path may exist anywhere in the repository."""

    def _mint_sites(self):
        try:
            return scan_mint_sites(REPO_ROOT)
        except MintScanError as exc:
            self.fail(str(exc))

    def test_scan_actually_finds_the_mint_sites(self):
        # If this fails the scan is broken, not the code:  a scan that matches
        # nothing would make every other assertion here vacuous.
        sites = self._mint_sites()
        self.assertIn(OWNER_MODULE, sites, "git-relay's minter must be visible to the scan")
        self.assertFalse(sites[OWNER_MODULE], "git-relay owns minting and must not be guarded")

    def test_the_verifier_is_not_its_own_mint_site(self):
        # The scan holds the endpoint so it can search for it;  keep that from
        # turning the verifier into a second unguarded mint site.
        self.assertNotIn(SCAN_MODULE, self._mint_sites())

    def test_git_relay_is_the_only_unguarded_minting_module(self):
        unguarded = sorted(rel for rel, guarded in self._mint_sites().items() if not guarded)
        self.assertEqual(unguarded, [OWNER_MODULE])

    def test_every_other_mint_site_fails_closed(self):
        for rel, guarded in self._mint_sites().items():
            if rel != OWNER_MODULE:
                self.assertTrue(guarded, f"{rel} can mint a seat GitHub App token")


class MintScanItself(unittest.TestCase):
    """The scan must catch what it claims to catch, not merely run green."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_second_unguarded_mint_site_in_a_guarded_file_is_found(self):
        # The exact hole Kody reported:  one guarded function plus one
        # unguarded second mint path in the same file.  A per-file "does the
        # text mention refuse_minting" check calls this file guarded.
        self._write(
            "mixed.py",
            "def guarded():\n"
            f"    refuse_minting()\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n"
            "def second():\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"mixed.py": False})

    def test_a_guarded_mint_site_passes(self):
        self._write(
            "closed.py",
            "def guarded():\n"
            f"    refuse_minting()\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"closed.py": True})

    def test_a_non_utf8_file_is_scanned_leniently_rather_than_failing_the_gate(self):
        # A .venv copy, a fixture or a vendored file somewhere in the tree may
        # not be valid UTF-8.  That must not turn four unrelated minting tests
        # red - and it must not hide an ASCII endpoint either.
        (self.root / "bad.py").write_bytes(
            b"# caf\xe9 latin-1 comment\n"
            b"def f():\n"
            b"    return post('/app/installations/1/" + MINT_ENDPOINT.encode() + b"')\n"
        )
        self.assertEqual(scan_mint_sites(self.root), {"bad.py": False})

    def test_a_genuine_io_error_still_fails_closed(self):
        # Decoding is lenient;  a real I/O error is not.  A partial inventory
        # must never look like a clean bill of health.  A directory named
        # *.py is an unreadable path whatever the test runs as.
        (self.root / "blocked.py").mkdir()
        with self.assertRaises(MintScanError) as caught:
            scan_mint_sites(self.root)
        self.assertIn("blocked.py", str(caught.exception))

    def test_a_function_docstring_naming_the_endpoint_is_not_a_mint_site(self):
        # Prose is prose.  This function never calls the endpoint, so it must
        # not be reported as an unguarded mint site - the same rule the module
        # already applies to a module-level docstring.
        self._write(
            "prose.py",
            'def documented():\n'
            '    """Never call /app/installations/1/' + MINT_ENDPOINT + ' here."""\n'
            "    return 1\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {})

    def test_a_module_level_mint_site_is_unguarded_despite_a_guarded_function(self):
        # Code that runs at import time cannot be covered by a guard living
        # inside some function, so the file is unguarded.
        self._write(
            "module_level.py",
            "MINT_PATH = '/app/installations/1/" + MINT_ENDPOINT + "'\n"
            "def guarded():\n"
            f"    {GUARD_CALL}()\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"module_level.py": False})

    def test_a_docstring_that_only_names_the_endpoint_is_not_a_mint_site(self):
        # Prose about minting must not turn every module into a mint site, or
        # the verdict stops meaning anything.
        self._write(
            "documented.py",
            '"""This module posts to /app/installations/1/'
            + MINT_ENDPOINT
            + ' - see below."""\n'
            "def guarded():\n"
            f"    {GUARD_CALL}()\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"documented.py": True})

    def test_a_guard_that_can_never_run_does_not_count_as_guarded(self):
        # `if False: refuse_minting()` is present but unreachable, so the live
        # mint path below it is exactly the second path this scan exists to find.
        self._write(
            "dead_guard.py",
            "def live():\n"
            "    if False:\n"
            f"        {GUARD_CALL}()\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"dead_guard.py": False})

    def test_a_swallowed_guard_does_not_count_as_guarded(self):
        self._write(
            "swallowed.py",
            "def live():\n"
            "    try:\n"
            f"        {GUARD_CALL}()\n"
            "    except Exception:\n"
            "        pass\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"swallowed.py": False})

    def test_a_guard_in_a_nested_helper_does_not_guard_the_caller(self):
        # The guard must dominate this function's own mint call.
        self._write(
            "nested.py",
            f"def {GUARD_CALL}():\n"
            "    raise SystemExit('refused')\n"
            "def live():\n"
            "    helper()\n"
            "    return post('/app/installations/1/" + MINT_ENDPOINT + "')\n",
        )
        self.assertEqual(scan_mint_sites(self.root), {"nested.py": False})


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
            policy_for=lambda seat: SYNTHETIC_POLICY,
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
                "repo": SYNTHETIC_REPO,
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

    def test_the_refused_broker_call_reached_the_guard_not_the_repository_check(self):
        # The synthetic policy authorizes SYNTHETIC_REPO, so the only thing that
        # can refuse this call is the mint guard.  Without this the fixture
        # could pass while the guard never ran.
        module = importlib.import_module("agent_sync.operator_github")

        seen = []

        def loader(slug):
            seen.append(slug)
            raise AssertionError("a live mint path was reached")

        broker = Broker(
            key_for=lambda seat: "k" * 64,
            policy_for=lambda seat: SYNTHETIC_POLICY,
            router=TokenRouter(loader, Path(tempfile.mkdtemp()) / "cache"),
            bot_identity=lambda slug: {"login": slug + "[bot]", "id": 7},
            enabled=True,
            nonce_ledger=NonceLedger(tempfile.mkdtemp()),
        )
        path = "/operator/v1/github/CODEX/token"
        body = json.dumps(
            {"repo": SYNTHETIC_REPO, "permissions": {"contents": "read"}}, sort_keys=True
        ).encode()
        nonce = "b" * 64
        broker.clock = lambda: 1000
        with self.assertRaises(module.Refused) as caught:
            broker.issue(path, "1000", nonce, signature("k" * 64, path, "1000", nonce, body), body)
        message = str(caught.exception)
        # A repository the policy does not allow is refused earlier, as a 403
        # naming that fact.  Dying at the mint instead - a 502, with the guard's
        # own wording withheld per the no-internal-detail rule - is what proves
        # the call got all the way to TokenRouter.token_for and was stopped there.
        self.assertEqual(caught.exception.status, 502, message)
        self.assertNotIn("repository not authorized", message)
        self.assertNotIn("permission not authorized", message)
        self.assertEqual(seen, [])

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