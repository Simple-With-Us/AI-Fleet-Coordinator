"""One answer for every platform when the registry cannot be read (AFC-809).

The answer is built once, in the shared router, and every platform module asks for it, so no
platform can drift from the others.  These tests call each platform's own check with a
registry that is down, and with a registry that has no key, and compare the answers.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from unittest import mock

import os
import pathlib

from simple_tools import antigravity_wake as AGW
from simple_tools import claude_wake as CW
from simple_tools import codex_wake as CXW
from simple_tools import grok_wake as GW
from simple_tools import minimax_wake as MMW
from simple_tools import muse_wake as MW
from simple_tools import platform_letter as PL
from simple_tools import wake_router as WR
from simple_tools import zcode_wake as ZCW

SID = "sess_0123456789abcdef0123456789abcdef"
LETTER = "AA"


class RegistryOutageAnswer(unittest.TestCase):
    """Every platform answers a registry fault the same way."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="afc809", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A host that is configured to reach the registry:  a call that raises is an outage.
        self.env = {
            "HOME": self.tmp,
            WR.registry_api.URL_ENV: "https://registry.example/api",
            WR.registry_api.KEY_VAR: "registry-" + "0" * 16,
        }
        # A host with no registry address and no key:  a deployment fault, not an outage.
        self.bare_env = {"HOME": self.tmp}
        self.target = WR.Target(session_id=SID, letter=LETTER, host="mac", generation=1)

    def _answers(self, answer, env=None):
        """What each platform answers when the registry answers `answer`.

        `None` is a registry that does not answer at all;  a mapping is what it answered.
        `env` is the host's environment:  the default one is configured to reach the registry.
        """
        env = self.env if env is None else env
        if answer is None:

            def lookup(env, op, params=None, *, timeout=25):
                raise OSError("the registry did not answer")

        else:

            def lookup(env, op, params=None, *, timeout=25):
                return answer

        def codex_lookup(env, letter):
            if answer is None:
                raise OSError("the registry did not answer")
            return answer

        def muse_lookup(env, letter):
            if answer is None:
                raise OSError("the registry did not answer")
            return answer

        codex = CXW.CodexDelivery(transport=None, env=env, registry_lookup=codex_lookup)
        with mock.patch.object(WR.registry_api, "call", lookup):
            return {
                "claude": CW._recheck(env, self.target),
                "antigravity": AGW._recheck(env, self.target),
                "grok": GW._recheck(env, self.target),
                "minimax": MMW._recheck(env, self.target),
                "muse": MW._recheck(env, self.target, muse_lookup),
                "zcode": ZCW._recheck(env, self.target),
                "codex": codex._still_registered(self.target),
            }

    def test_every_platform_answers_an_outage_the_same_way(self) -> None:
        """An outage is `busy` with the outage code, in every platform."""
        answers = self._answers(None)
        self.assertEqual(len(answers), 7)
        for name, delivery in answers.items():
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual(
                    (delivery.outcome, delivery.code),
                    ("busy", WR.REGISTRY_UNAVAILABLE),
                )

    def test_every_platform_answers_a_missing_key_the_same_way(self) -> None:
        """A host with no registry address or key is a deployment fault:  `failed`, with the
        house code, in every platform, and the note names the variable that is missing."""
        answers = self._answers(
            {
                "error": "the registry key is not set (SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY)",
                "code": "not_configured",
            }
        )
        self.assertEqual(len(answers), 7)
        for name, delivery in answers.items():
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("failed", WR.NOT_CONFIGURED))
                self.assertIn("SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY", delivery.note)

    def test_a_raised_call_on_an_unconfigured_host_fails_closed(self) -> None:
        """A host with no registry address or key cannot be retried into working, so every
        platform answers `failed` with the house code, and the note names the variable."""
        answers = self._answers(None, env=self.bare_env)
        self.assertEqual(len(answers), 7)
        for name, delivery in answers.items():
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("failed", WR.NOT_CONFIGURED))
                self.assertIn(WR.registry_api.URL_ENV, delivery.note)

    def test_an_unreadable_registry_env_is_answered_on_the_hosts_configuration(self) -> None:
        """A registry env that cannot be read is answered on the host's own configuration:
        a host that is configured answers the outage and spends no attempt, and a host with
        no address or key fails closed and names the variable.  The classification uses the
        same mapping the call uses, so a host that reads its settings from the knobs or the
        shared file is judged on them."""
        def boom(*_args, **_kwargs):
            raise OSError("the env file cannot be read")

        def no_registry(*_args, **_kwargs):
            raise OSError("the registry did not answer")

        with mock.patch.object(PL, "with_registry_env", side_effect=boom):
            with mock.patch.object(WR.registry_api, "call", side_effect=no_registry):
                claude = CW._recheck(self.env, self.target)
        with mock.patch.object(PL, "with_registry_env", side_effect=boom):
            muse = MW._recheck(self.env, self.target, no_registry)
        for name, delivery in (("claude", claude), ("muse", muse)):
            with self.subTest(platform=name, env="configured"):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("busy", WR.REGISTRY_UNAVAILABLE))
        with mock.patch.object(PL, "with_registry_env", side_effect=boom):
            with mock.patch.object(WR.registry_api, "call", side_effect=no_registry):
                bare = CW._recheck(self.bare_env, self.target)
        with mock.patch.object(PL, "with_registry_env", side_effect=boom):
            muse_bare = MW._recheck(self.bare_env, self.target, no_registry)
        for name, delivery in (("claude", bare), ("muse", muse_bare)):
            with self.subTest(platform=name, env="unconfigured"):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("failed", WR.NOT_CONFIGURED))
                self.assertIn(WR.registry_api.URL_ENV, delivery.note)

    def test_the_classification_uses_the_registry_environment_the_call_used(self) -> None:
        """A host that reads its registry settings from the shared loader is judged on
        them:  the loader's values are the configuration, and a call that raises on them is
        an outage, not a deployment fault."""
        loaded = {
            "HOME": self.tmp,
            WR.registry_api.URL_ENV: "https://registry.example/api",
            WR.registry_api.KEY_VAR: "registry-" + "0" * 16,
        }

        def no_registry(*_args, **_kwargs):
            raise OSError("the registry did not answer")

        with mock.patch.object(PL, "with_registry_env", return_value=loaded):
            with mock.patch.object(WR.registry_api, "call", side_effect=no_registry):
                claude = CW._recheck(self.bare_env, self.target)
                muse = MW._recheck(self.bare_env, self.target, no_registry)
        for name, delivery in (("claude", claude), ("muse", muse)):
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("busy", WR.REGISTRY_UNAVAILABLE))

    def test_a_loader_that_could_not_be_read_fails_closed(self) -> None:
        """The loader holds the registry's address and the mapping lost it:  the read failed,
        so the registry's identity is unverifiable here, the delivery fails closed and the
        note names the source that could not be read.  A registry that was configured and
        later became unreachable is the outage, and it is `busy`."""
        home = pathlib.Path(self.tmp) / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        path = home / os.path.join(".local", "bin", "operator-registry-env")
        for directory in (path.parent.parent, path.parent):
            if directory.is_symlink():
                raise RuntimeError("refusing a symlinked directory: %s" % directory)
            directory.mkdir(mode=0o700, exist_ok=True)
        path.write_text("export %s=https://registry.example/api\n" % WR.registry_api.URL_ENV)
        env = {"HOME": str(home)}

        def no_registry(*_args, **_kwargs):
            raise OSError("the registry did not answer")

        with mock.patch.object(PL, "with_registry_env", side_effect=no_registry):
            with mock.patch.object(WR.registry_api, "call", side_effect=no_registry):
                claude = CW._recheck(env, self.target)
                muse = MW._recheck(env, self.target, no_registry)
        for name, delivery in (("claude", claude), ("muse", muse)):
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("failed", WR.NOT_CONFIGURED))
                self.assertIn(WR.REGISTRY_ENV_SOURCE, delivery.note)
                self.assertRegex(delivery.note, r"^[A-Za-z0-9:_-]{0,80}$")

    def test_a_loader_that_holds_no_address_names_the_variable_that_is_missing(self) -> None:
        """The loader was read and holds no address:  the host is not configured, and the note
        names the variable that is missing, not the source that could not be read."""
        home = pathlib.Path(self.tmp) / "home"
        home.mkdir(mode=0o700, exist_ok=True)
        path = home / os.path.join(".local", "bin", "operator-registry-env")
        for directory in (path.parent.parent, path.parent):
            if directory.is_symlink():
                raise RuntimeError("refusing a symlinked directory: %s" % directory)
            directory.mkdir(mode=0o700, exist_ok=True)
        path.write_text("# a loader with nothing of the registry in it\n")
        env = {"HOME": str(home)}

        def no_registry(*_args, **_kwargs):
            raise OSError("the registry did not answer")

        with mock.patch.object(PL, "with_registry_env", side_effect=no_registry):
            with mock.patch.object(WR.registry_api, "call", side_effect=no_registry):
                claude = CW._recheck(env, self.target)
                muse = MW._recheck(env, self.target, no_registry)
        for name, delivery in (("claude", claude), ("muse", muse)):
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual((delivery.outcome, delivery.code), ("failed", WR.NOT_CONFIGURED))
                self.assertIn(WR.registry_api.URL_ENV, delivery.note)
                self.assertNotIn(WR.REGISTRY_ENV_SOURCE, delivery.note)

    def test_every_platform_names_an_odd_answer_the_same_way(self) -> None:
        """An answer that is not a mapping is malformed data:  `failed`, with the house code,
        in every platform."""
        answers = self._answers([])
        for name, delivery in answers.items():
            with self.subTest(platform=name):
                self.assertIsNotNone(delivery)
                self.assertEqual(
                    (delivery.outcome, delivery.code),
                    ("failed", WR.UNREADABLE_ANSWER),
                )


if __name__ == "__main__":
    unittest.main()
