"""The registry CLI and the guard that checks it must name the same file.

The registry-module dispatch resolves the registry from the environment mapping it is
given, and it refuses to create one:  a second registry would hand out letters that
collide with the one the Operator keeps.  The CLI resolves the same file on its own.
If the two resolve differently, the guard checks a file the CLI never opens, and the
refusal it makes means nothing.

These tests pin the agreement for the shapes that can differ:  a home directory that is
not the process's, a state-root override, a tilde in that override, and the registry
override itself.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from simple_tools import live as L, operator_registry as OR


def _guard_db(env):
    """The path the registry-module dispatch checks, the way it checked it before."""
    return OR.registry_db_path(L.state_root(env), env)


class RegistryDbResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_home_from_the_callers_env_is_the_file_both_sides_open(self):
        env = {"HOME": self.home}
        self.assertEqual(OR.default_db(env), _guard_db(env))

    def test_a_state_root_override_is_the_file_both_sides_open(self):
        env = {"HOME": self.home, OR.ENV_STATE_DIR: os.path.join(self.home, "state")}
        self.assertEqual(OR.default_db(env), _guard_db(env))

    def test_a_tilde_in_the_state_root_override_is_expanded_on_both_sides(self):
        env = {"HOME": self.home, OR.ENV_STATE_DIR: "~/state"}
        self.assertEqual(OR.default_db(env), _guard_db(env))
        self.assertNotIn("~", OR.default_db(env))

    def test_the_registry_override_wins_on_both_sides(self):
        db = os.path.join(self.home, "shared", "registry.sqlite3")
        env = {"HOME": self.home, OR.ENV_REGISTRY_PATH: db}
        self.assertEqual(OR.default_db(env), _guard_db(env))
        self.assertEqual(OR.default_db(env), db)

    def test_no_env_is_this_process_environment(self):
        self.assertEqual(OR.default_db(), OR.default_db(os.environ))

    def test_the_caller_s_env_wins_over_the_process_environment(self):
        # The defect this pins:  the CLI used to read the process environment only, so a
        # caller with its own mapping was answered about a file it never opens.
        env = {"HOME": self.home, OR.ENV_REGISTRY_PATH: os.path.join(self.home, "shared.sqlite3")}
        self.assertNotEqual(OR.default_db(env), OR.default_db(os.environ))
        self.assertTrue(OR.default_db(env).endswith("shared.sqlite3"))


class GuardAndCliAgreeTest(unittest.TestCase):
    """The dispatch that refuses to create a registry resolves through the same function."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def test_the_registry_module_dispatch_resolves_through_the_cli_resolver(self):
        import inspect

        from simple_tools import claude_letter, platform_letter

        for module in (claude_letter, platform_letter):
            source = inspect.getsource(module.registry_main)
            self.assertIn("OR.default_db(env)", source, module.__name__)
            self.assertNotIn("registry_db_path(L.state_root(env), env)", source, module.__name__)


if __name__ == "__main__":
    unittest.main()
