"""The reviewed generic-fixture registry: exact-digest admission, fail-closed
scope, and the absolute controls that survive registration."""

import hashlib
import unittest
from unittest.mock import patch

from git_relay import patch_secretscan as scanner

REPO = "Simple-With-Us/AI-Fleet-Coordinator"
PATH = "scripts/linear-board/test_board.py"
FIXTURE = 'CLIENT_SECRET = "mock-client-secret-value"\n'
DIGEST = hashlib.sha256(FIXTURE.encode()).hexdigest()
ENTRY = (REPO, PATH, DIGEST)


class RegisteredGenericFixtureTests(unittest.TestCase):
    def run_scan(self, text, known=(), entry=ENTRY, repo=REPO, path=PATH):
        with patch.object(scanner, "_REVIEWED_GENERIC_FIXTURES", frozenset({entry})):
            return scanner.scan_file(repo, path, text, known)

    def test_exact_registered_bytes_pass_generic_only_scan(self):
        self.assertIsNone(self.run_scan(FIXTURE))

    def test_shipped_registry_pins_the_reviewed_entry(self):
        # The digest here is the one the durable record in
        # docs/security/reviewed-generic-fixtures.md names; a byte change of
        # the reviewed file is a new review, never a silent update.
        self.assertIn(
            (
                REPO,
                PATH,
                "9e96737087b55d3ba6f82f7fb8e5e0a5b17828d10f2759e73dae65063211cfe3",
            ),
            scanner._REVIEWED_GENERIC_FIXTURES,
        )

    def test_wrong_repo_path_or_digest_fail_closed(self):
        for entry in [
            ("Other/Repo", PATH, DIGEST),
            (REPO, "scripts/linear-board/other.py", DIGEST),
            (REPO, PATH, hashlib.sha256(b"other").hexdigest()),
        ]:
            with self.subTest(entry=entry):
                self.assertEqual(self.run_scan(FIXTURE, entry=entry), "assigned credential")

    def test_dedicated_shapes_stay_absolute_at_exact_registered_digest(self):
        # The dangerous payload's own bytes are exactly registered, so a
        # refusal proves the dedicated-shape control fired, not the
        # unregistered-file generic path.
        payload = FIXTURE + "Authorization: Bearer " + "a" * 24 + "\n"
        entry = (REPO, PATH, hashlib.sha256(payload.encode()).hexdigest())
        self.assertEqual(self.run_scan(payload, entry=entry), "bearer token")

    def test_zulip_shaped_keys_stay_absolute_at_exact_registered_digest(self):
        token = "Ab1" * 10 + "C2"  # exactly 32 chars, mixed case and digits
        self.assertEqual(len(token), 32)
        self.assertTrue(scanner._zulip_shaped(token))
        payload = FIXTURE + token + "\n"
        entry = (REPO, PATH, hashlib.sha256(payload.encode()).hexdigest())
        self.assertEqual(self.run_scan(payload, entry=entry), "a Zulip-shaped API key")

    def test_loaded_credentials_stay_absolute(self):
        self.assertEqual(
            self.run_scan(FIXTURE, known=("mock-client-secret-value",)),
            "a loaded credential",
        )


if __name__ == "__main__":
    unittest.main()
