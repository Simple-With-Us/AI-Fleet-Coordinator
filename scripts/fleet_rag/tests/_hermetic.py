"""Shared hermeticity helper: isolate a test from this machine's real credentials and Infisical.

fleet_rag.core reads missing credentials directly through the shared Infisical client.  Tests
must disable both optional Fleet-OPS reads and the separate recall-project login seam so a real
binding can never reach a provider or load credentials into the test process.

Use HermeticTestCase as a drop-in base (it cooperates with your own setUp via super()), or mix
HermeticCredentialsMixin into an existing base class and call self.make_hermetic() yourself:

    from fleet_rag.tests._hermetic import HermeticTestCase

    class MyTests(HermeticTestCase):
        def setUp(self):
            super().setUp()   # gets you make_hermetic() for free
            ...

    cd scripts && python3 -m unittest fleet_rag.tests._hermetic -v
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fleet_rag import core


class HermeticCredentialsMixin:
    """Adds `make_hermetic()`: point every credential/Infisical seam at a throwaway location.

    Call this from setUp (after super().setUp(), so it composes with a class that already does
    its own setUp work).  Everything is undone via addCleanup, in reverse order, once the test
    ends -- including on failure.
    """

    def make_hermetic(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        # Deliberately absent -- these paths are never created under `home`.
        absent_handoff = home / ".secrets" / "no-such-handoff"

        env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "FLEET_RAG_HANDOFF_FILE": str(absent_handoff),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        # Disable optional direct reads independently of the dedicated recall reader below.
        for patcher in (mock.patch.object(core, "fleet_secrets_many", lambda names: {}),):
            patcher.start()
            self.addCleanup(patcher.stop)

        # Belt-and-suspenders per the no-such-handoff paths above: even if some future code path
        # finds a real identity another way, never let a test reach Infisical over the network.
        # None is exactly what infisical_login() already returns once no handoff file resolves
        # to a real one, so this changes no test's expected behavior -- it just makes the "no
        # network" guarantee unconditional instead of incidental.
        login_patch = mock.patch.object(core, "infisical_login", lambda: None)
        login_patch.start()
        self.addCleanup(login_patch.stop)


class HermeticTestCase(HermeticCredentialsMixin, unittest.TestCase):
    """unittest.TestCase that is hermetic against real credentials from setUp onward."""

    def setUp(self) -> None:
        super().setUp()
        self.make_hermetic()


class _SelfTest(HermeticTestCase):
    """Proves the mixin actually redirects every seam away from the real machine."""

    def test_handoff_paths_are_redirected_and_absent(self) -> None:
        self.assertFalse(core.handoff_file().exists())
        self.assertEqual(core.fleet_secrets_many(["TEI_URL"]), {})

    def test_infisical_login_is_stubbed_out(self) -> None:
        self.assertIsNone(core.infisical_login())

    def test_identity_and_load_config_see_no_handoff(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(core._identity("INFISICAL_SHARED"), (None, None))
            with self.assertRaises(core.FleetRagError):
                core.load_config()


if __name__ == "__main__":
    unittest.main()
