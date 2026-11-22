"""Match census invariants on the real server outbound file guard; no held file."""

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay import patch_secretscan as relay


def review_record_admission(record):
    """Return (key, census) from a durable match-pinned review record, or raise ValueError."""
    if not isinstance(record, dict):
        raise ValueError("review record must be an object")
    doc = record.get("reviewed_file")
    if not isinstance(doc, dict):
        raise ValueError("reviewed_file must be an object")
    fields = {}
    for field in ("repository", "path", "sha256"):
        value = doc.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"reviewed_file.{field} must be a non-empty string")
        fields[field] = value
    rows = doc.get("effective_matches")
    if not isinstance(rows, list):
        raise ValueError(
            "reviewed_file.effective_matches must be a list of label-plus-four-integer rows"
        )
    census = []
    for i, row in enumerate(rows):
        if not (
            isinstance(row, (list, tuple))
            and len(row) == 5
            and isinstance(row[0], str)
            and row[0]
            and all(isinstance(n, int) and not isinstance(n, bool) for n in row[1:])
        ):
            raise ValueError(
                f"reviewed_file.effective_matches[{i}] must be a label plus four integers"
            )
        census.append((row[0], *row[1:]))
    return (fields["repository"], fields["path"], fields["sha256"]), frozenset(census)


class MatchPinnedReview(unittest.TestCase):
    repo, path = "fixture-org/fixture-repo", "tests/fixture.ts"

    def metadata(self, scanner, body):
        return frozenset(
            (label, *m.span(), *m.span("value"))
            for label, pattern in scanner._PATTERNS
            if label in scanner._GENERIC_ASSIGNMENTS
            for m in pattern.finditer(body)
            if not scanner._source_assignment(body, m)
        )

    def test_only_exact_match_census_can_be_excluded(self):
        for scanner in (relay,):
            body = "const secret = '" + "a" * 24 + "';"
            reviewed = self.metadata(scanner, body)
            self.assertEqual(len(reviewed), 1)
            for text, metadata, accepted in [
                (body, reviewed, True),
                (body, frozenset(), False),
                (body, frozenset({("assigned secret", 0, 1, 0, 1)}), False),
                (body + " const token = '" + "b" * 24 + "';", reviewed, False),
                ("//shift\n" + body, reviewed, False),
                (body.replace("secret", "password"), reviewed, False),
            ]:
                # Deliberately update digest but NOT pinned match metadata.
                key = (self.repo, self.path, hashlib.sha256(text.encode()).hexdigest())
                with patch.object(scanner, "_REVIEWED_GENERIC_MATCHES", {key: metadata}):
                    hit = scanner.scan_file(self.repo, self.path, text)
                    if accepted:
                        self.assertIsNone(hit)
                    else:
                        self.assertIsNotNone(hit)

    def test_exact_repo_path_digest_and_absolute_dedicated_loaded_guards(self):
        for scanner in (relay,):
            base = "const secret = '" + "a" * 24 + "';"
            for suffix, known in [
                ("", []),
                (" gh" + "p_" + "c" * 40, []),
                (" Aa1" + "b" * 29, []),
                ("", ["aaa"]),
            ]:
                body = base + suffix
                key = (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
                with patch.object(
                    scanner,
                    "_REVIEWED_GENERIC_MATCHES",
                    {key: self.metadata(scanner, base)},
                ):
                    hit = scanner.scan_file(self.repo, self.path, body, known)
                    if suffix or known:
                        self.assertIsNotNone(hit)
                    else:
                        self.assertIsNone(hit)
                    for repo, path, text in [
                        (self.repo + "2", self.path, body),
                        (self.repo, self.path + "2", body),
                        (self.repo, self.path, body + "\n"),
                    ]:
                        self.assertIsNotNone(scanner.scan_file(repo, path, text))
                    self.assertIsNotNone(scanner.scan(body))

    def test_actual_registry_and_preserved_admissions(self):
        self.assertEqual(len(relay._REVIEWED_GENERIC_FIXTURES), 5)
        self.assertEqual(len(relay._REVIEWED_PUBLIC_INTEGRITIES), 4)
        self.assertEqual(len(relay._REVIEWED_GENERIC_MATCHES), 5)
        server_key = (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "git-relay/git_relay/server.py",
            "182f99d6c71bb9616e4ac9381d2fadfbe26e8760fc3dac873a6b3c58d2f2c752",
        )
        self.assertEqual(
            relay._REVIEWED_GENERIC_MATCHES[server_key],
            frozenset({("assigned secret", 51282, 51325, 51290, 51325)}),
        )
        self.assertNotIn(server_key, relay._REVIEWED_GENERIC_FIXTURES)


class ServerPyRetargetAdmissionTests(unittest.TestCase):
    """The edit_pr ref-validation server.py candidate 0b9d5296: exact bytes, exact census, stale metadata refuses."""

    key = (
        "Simple-With-Us/AI-Fleet-Coordinator",
        "git-relay/git_relay/server.py",
        "0b9d5296287c1c3dd5b7d508af8ae2afffb9da83ff03f64a3331ec5f1df01f20",
    )
    legacy_digest = "82b53da38ce0a110a3f25b947fbcabcfcef031a8295795d72f46f48d55d79305"

    def test_durable_record_binds_registry_and_checkout(self):
        root = Path(__file__).resolve().parents[3]
        record = json.loads(
            (
                root / "docs/security/server-py-retarget-match-pinned-review-20261118.json"
            ).read_text()
        )
        doc = record["reviewed_file"]
        self.assertEqual((doc["repository"], doc["path"], doc["sha256"]), self.key)
        census = frozenset(tuple(row) for row in doc["effective_matches"])
        self.assertEqual(relay._REVIEWED_GENERIC_MATCHES[self.key], census)
        self.assertEqual(record["provenance_source"]["sha256"], self.legacy_digest)
        # The checkout holds a complete version of the file: the reviewed main
        # version, the proposed candidate, or a later main version that carries
        # no assignable match at all, so there is nothing left to admit.  A
        # third version that does carry a match is refused by the scanner,
        # which the scan assertion below proves.
        raw = (root / doc["path"]).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in {self.legacy_digest, self.key[2]}:
            self.assertEqual(
                MatchPinnedReview().metadata(relay, raw.decode("utf-8")),
                frozenset(),
            )
        self.assertIsNone(relay.scan_file(doc["repository"], doc["path"], raw.decode("utf-8")))

    def test_stale_or_incomplete_census_refuses_the_proposed_digest(self):
        census = relay._REVIEWED_GENERIC_MATCHES[self.key]
        body = "const secret = '" + "a" * 24 + "';"
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        real = MatchPinnedReview().metadata(relay, body)
        self.assertEqual(len(real), 1)
        for metadata in (frozenset(), frozenset({("assigned secret", 0, 1, 0, 1)})):
            with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: metadata}):
                self.assertIsNotNone(relay.scan_file(key[0], key[1], body))
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: real}):
            self.assertIsNone(relay.scan_file(key[0], key[1], body))
        # The genuine registry entry still pins exactly one reviewed span.
        self.assertEqual(census, frozenset({("assigned secret", 52346, 52389, 52354, 52389)}))

    def test_extra_match_in_candidate_bytes_cannot_ride_the_digest(self):
        body = "const secret = '" + "a" * 24 + "'; const token = '" + "b" * 24 + "';"
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        with patch.object(
            relay,
            "_REVIEWED_GENERIC_MATCHES",
            {key: frozenset(list(MatchPinnedReview().metadata(relay, body))[:1])},
        ):
            self.assertIsNotNone(relay.scan_file(key[0], key[1], body))
        self.assertEqual(relay.scan(body), "assigned secret")


class UpgradeReviewedHubAdmissionTests(unittest.TestCase):
    """The Usage-Monitor upgrade-reviewed-hub.py v3 candidate: exact digest, exact one-match census.

    The key and census are read from the durable review record so this fixture
    embeds no repository identifier of its own.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[3]
        cls.record = json.loads(
            (
                root
                / "docs/security/usage-monitor-upgrade-reviewed-hub-match-pinned-review-20261118.json"
            ).read_text()
        )
        cls.key, cls.census = review_record_admission(cls.record)

    def test_registry_pins_exact_census(self):
        self.assertEqual(relay._REVIEWED_GENERIC_MATCHES[self.key], self.census)
        self.assertNotIn(self.key, relay._REVIEWED_GENERIC_FIXTURES)

    def test_durable_record_binds_registry(self):
        doc = self.record["reviewed_file"]
        self.assertEqual(doc["bytes"], 36664)
        self.assertEqual(doc["lines"], 814)
        self.assertEqual(
            self.record["provenance_source"]["sha256"],
            "e98aaf3f5c7141bd2e80ab2b310ebe216c8ec4b43480ac6e5178a61b47407fea",
        )

    def test_stale_or_incomplete_census_refuses_the_proposed_digest(self):
        body = "const secret = '" + "a" * 24 + "';"
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        real = MatchPinnedReview().metadata(relay, body)
        self.assertEqual(len(real), 1)
        for metadata in (frozenset(), frozenset({("assigned secret", 0, 1, 0, 1)})):
            with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: metadata}):
                self.assertIsNotNone(relay.scan_file(key[0], key[1], body))
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: real}):
            self.assertIsNone(relay.scan_file(key[0], key[1], body))
        self.assertEqual(relay._REVIEWED_GENERIC_MATCHES[self.key], self.census)

    def test_loaded_credential_stays_absolute_on_the_admission_path(self):
        body = "prefix " + "KnownCredentialValue123"
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: self.census}):
            self.assertEqual(
                relay.scan_file(key[0], key[1], body, known=("KnownCredentialValue123",)),
                "a loaded credential",
            )

    def test_dedicated_pattern_stays_absolute_on_the_admission_path(self):
        body = "token = ghp_" + "A" * 30
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: self.census}):
            self.assertEqual(relay.scan_file(key[0], key[1], body), "GitHub token")


class ServerPyAdmissionTests(unittest.TestCase):
    """The edit_pr-base server.py candidate: exact bytes, exact census, stale metadata refuses."""

    key = (
        "Simple-With-Us/AI-Fleet-Coordinator",
        "git-relay/git_relay/server.py",
        "182f99d6c71bb9616e4ac9381d2fadfbe26e8760fc3dac873a6b3c58d2f2c752",
    )
    legacy_digest = "82b53da38ce0a110a3f25b947fbcabcfcef031a8295795d72f46f48d55d79305"

    def test_durable_record_binds_registry_and_checkout(self):
        root = Path(__file__).resolve().parents[3]
        record = json.loads(
            (root / "docs/security/server-py-match-pinned-review-20261118.json").read_text()
        )
        doc = record["reviewed_file"]
        self.assertEqual((doc["repository"], doc["path"], doc["sha256"]), self.key)
        census = frozenset(tuple(row) for row in doc["effective_matches"])
        self.assertEqual(relay._REVIEWED_GENERIC_MATCHES[self.key], census)
        self.assertEqual(record["provenance_source"]["sha256"], self.legacy_digest)

    def test_census_holds_against_the_admitted_bytes_when_present(self):
        root = Path(__file__).resolve().parents[3]
        doc = json.loads(
            (root / "docs/security/server-py-match-pinned-review-20261118.json").read_text()
        )["reviewed_file"]
        raw = (root / doc["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.key[2]:
            # Loud on purpose: the admitted candidate bytes are not preserved in
            # any published commit, so the census is UNREPRODUCED until they are.
            self.skipTest(
                "UNREPRODUCED: checkout does not hold the admitted server.py bytes "
                + self.key[2][:8]
            )
        self.assertEqual(len(raw), doc["bytes"])
        self.assertIsNone(relay.scan_file(doc["repository"], doc["path"], raw.decode("utf-8")))

    def test_stale_or_incomplete_census_refuses_the_admitted_digest(self):
        census = relay._REVIEWED_GENERIC_MATCHES[self.key]
        body = "const secret = '" + "a" * 24 + "';"
        # Real candidate bytes are not needed for the refusal invariants: any
        # content whose digest key carries wrong metadata must refuse.
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        real = MatchPinnedReview().metadata(relay, body)
        self.assertEqual(len(real), 1)
        for metadata in (frozenset(), frozenset({("assigned secret", 0, 1, 0, 1)})):
            with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: metadata}):
                self.assertIsNotNone(relay.scan_file(key[0], key[1], body))
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: real}):
            self.assertIsNone(relay.scan_file(key[0], key[1], body))
        # The genuine registry entry still pins exactly one reviewed span.
        self.assertEqual(census, frozenset({("assigned secret", 51282, 51325, 51290, 51325)}))

    def test_extra_match_in_candidate_bytes_cannot_ride_the_digest(self):
        # Any added assignment changes the digest, so the admission misses and
        # the normal scan refuses; proven with a synthetic stand-in body.
        body = "const secret = '" + "a" * 24 + "'; const token = '" + "b" * 24 + "';"
        key = (self.key[0], self.key[1], hashlib.sha256(body.encode()).hexdigest())
        with patch.object(
            relay,
            "_REVIEWED_GENERIC_MATCHES",
            {key: frozenset(list(MatchPinnedReview().metadata(relay, body))[:1])},
        ):
            self.assertIsNotNone(relay.scan_file(key[0], key[1], body))
        self.assertEqual(relay.scan(body), "assigned secret")


class ReviewRecordShapeTests(unittest.TestCase):
    def test_valid_record_returns_key_and_census(self):
        record = {
            "reviewed_file": {
                "repository": "org/repo",
                "path": "file.py",
                "sha256": "abc",
                "effective_matches": [["assigned secret", 1, 2, 3, 4]],
            }
        }
        self.assertEqual(
            review_record_admission(record),
            (("org/repo", "file.py", "abc"), frozenset({("assigned secret", 1, 2, 3, 4)})),
        )

    def test_malformed_records_raise_precise_valueerror(self):
        cases = [
            (None, "review record must be an object"),
            ([], "review record must be an object"),
            ({}, "reviewed_file must be an object"),
            ({"reviewed_file": "x"}, "reviewed_file must be an object"),
            (
                {"reviewed_file": {"path": "p", "sha256": "s"}},
                "reviewed_file.repository must be a non-empty string",
            ),
            (
                {"reviewed_file": {"repository": "", "path": "p", "sha256": "s"}},
                "reviewed_file.repository must be a non-empty string",
            ),
            (
                {"reviewed_file": {"repository": "r", "path": "", "sha256": "s"}},
                "reviewed_file.path must be a non-empty string",
            ),
            (
                {"reviewed_file": {"repository": "r", "path": "p", "sha256": ""}},
                "reviewed_file.sha256 must be a non-empty string",
            ),
            (
                {"reviewed_file": {"repository": "r", "path": "p", "sha256": "s"}},
                "reviewed_file.effective_matches must be a list of label-plus-four-integer rows",
            ),
            (
                {
                    "reviewed_file": {
                        "repository": "r",
                        "path": "p",
                        "sha256": "s",
                        "effective_matches": [["assigned secret", 1, 2, 3]],
                    }
                },
                "reviewed_file.effective_matches[0] must be a label plus four integers",
            ),
            (
                {
                    "reviewed_file": {
                        "repository": "r",
                        "path": "p",
                        "sha256": "s",
                        "effective_matches": [["assigned secret", 1, 2, 3, True]],
                    }
                },
                "reviewed_file.effective_matches[0] must be a label plus four integers",
            ),
        ]
        for record, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as caught:
                    review_record_admission(record)
                self.assertEqual(str(caught.exception), message)
