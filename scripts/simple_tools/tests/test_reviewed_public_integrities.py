"""Exact public-integrity admission; synthetic values only, no network."""

import base64
import hashlib
import json
import unittest
from unittest.mock import patch

from simple_tools import secretscan
from simple_tools.tests.test_secretscan_fleet import samples


def synthetic_lock():
    # A canonical 64-byte SRI with one generated 32-character mixed-case span.
    encoded = "Ab1" * 10 + "C2" + "/" + "A" * 53 + "=="
    assert base64.b64encode(base64.b64decode(encoded, validate=True)).decode() == encoded
    return json.dumps({"lockfileVersion": 3, "packages": {
        "node_modules/synthetic": {"integrity": "sha512-" + encoded}
    }}) + "\n"


class ReviewedPublicIntegrityTests(unittest.TestCase):
    repo = "example-org/example-repo"
    path = "app/package-lock.json"

    def approve(self, body):
        entry = (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
        return patch.object(secretscan, "_REVIEWED_PUBLIC_INTEGRITIES", frozenset({entry}))

    def test_shipped_registry_is_only_two_reviewed_complete_ct_files(self):
        self.assertEqual(secretscan._REVIEWED_PUBLIC_INTEGRITIES, frozenset({
            ("Simple-With-Us/Congress.Trade", self.path,
             "72462e4c39a4a9a2ba6927214072e0f607565db11c3aa1be034e31eb197af7b9"),
            ("Simple-With-Us/Congress.Trade", self.path,
             "eb44b787f36b8149d5173d84e39d23d5c2b235721cc8c6eab9a6970d5ade5bd1"),
        }))

    def test_exact_review_only_and_general_scan_remains_strict(self):
        body = synthetic_lock()
        self.assertEqual(secretscan.scan(body), "a Zulip-shaped API key")
        self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, body))
        with self.approve(body):
            self.assertIsNone(secretscan.scan_file(self.repo, self.path, body))
            self.assertEqual(secretscan.scan(body), "a Zulip-shaped API key")
            self.assertIn(secretscan.WITHHELD, secretscan.redact_fields(body)[0])

    def test_any_destination_or_byte_drift_fails_closed(self):
        body = synthetic_lock()
        with self.approve(body):
            for repo, path, text in [
                (self.repo.upper(), self.path, body),
                (self.repo, "other/package-lock.json", body),
                (self.repo, self.path + ".copy", body),
                (self.repo, self.path, body + " "),
                (self.repo, self.path, body[:-1]),
                (self.repo, self.path, body.replace("synthetic", "different")),
                (self.repo, self.path, json.loads(body)["packages"]["node_modules/synthetic"]["integrity"]),
            ]:
                with self.subTest(repo=repo, path=path):
                    self.assertEqual(secretscan.scan_file(repo, path, text), "a Zulip-shaped API key")

    def test_every_loaded_value_wins_including_short_generator_values(self):
        body = synthetic_lock()
        with self.approve(body):
            for loaded in ("synthetic", "Ab1", "A"):
                with self.subTest(length=len(loaded)):
                    self.assertEqual(
                        secretscan.scan_file(self.repo, self.path, body,
                                            (value for value in ("", "not-present", loaded))),
                        "a loaded credential",
                    )
            self.assertIsNone(secretscan.scan_file(self.repo, self.path, body, iter(("", "not-present"))))

    def test_other_detectors_survive_even_an_exact_attestation(self):
        for label, value in samples():
            body = synthetic_lock() + value
            with self.subTest(label=label), self.approve(body):
                self.assertEqual(secretscan.scan_file(self.repo, self.path, body), label)
        for value in ("gh" + "p_" + "a" * 40, "-----BEGIN " + "PRIVATE KEY-----"):
            body = synthetic_lock() + value
            with self.approve(body):
                self.assertEqual(secretscan.scan_file(self.repo, self.path, body), secretscan.scan(body))
                self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, body))

    def test_real_reviewed_path_is_not_a_general_integrity_exemption(self):
        body = synthetic_lock()
        self.assertEqual(secretscan.scan_file("Simple-With-Us/Congress.Trade", self.path, body),
                         "a Zulip-shaped API key")
