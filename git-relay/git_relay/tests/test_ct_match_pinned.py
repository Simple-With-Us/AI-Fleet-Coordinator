"""Match census invariants on the real server outbound file guard; no held file."""

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay import patch_secretscan as relay
from git_relay.reviewed_fixtures import unreviewed_file_secret


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
        self.assertEqual(len(relay._REVIEWED_GENERIC_MATCHES), 4)
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
        key, census = next(
            (key, census)
            for key, census in relay._REVIEWED_GENERIC_MATCHES.items()
            if key[0] == "Simple-With-Us/Congress.Trade"
        )
        self.assertEqual(
            key,
            (
                "Simple-With-Us/Congress.Trade",
                "app/src/security/__tests__/originLock.test.ts",
                "cc1f0a0d5565309e9268ecd4e37d15d09c5c2e85f4de3cd54638be87bee555c5",
            ),
        )
        self.assertEqual(census, frozenset({("assigned secret", 547, 580, 557, 580)}))
        self.assertNotIn(key, relay._REVIEWED_GENERIC_FIXTURES)

    def test_real_outbound_guard_uses_pinned_scanner_and_loaded_refusal(self):
        body = "const secret = '" + "a" * 24 + "';"
        key = (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: self.metadata(relay, body)}):
            self.assertIsNone(unreviewed_file_secret(self.repo, self.path, body.encode()))
            self.assertIsNotNone(
                unreviewed_file_secret(self.repo, self.path, body.encode(), ["aaa"])
            )
            self.assertIsNotNone(unreviewed_file_secret(self.repo + "2", self.path, body.encode()))
            self.assertIsNotNone(unreviewed_file_secret(self.repo, self.path + "2", body.encode()))
            self.assertIsNotNone(
                unreviewed_file_secret(self.repo, self.path, (body + "\n").encode())
            )
        with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: frozenset()}):
            with patch.object(relay, "_REVIEWED_GENERIC_FIXTURES", frozenset({key})):
                self.assertIsNotNone(unreviewed_file_secret(self.repo, self.path, body.encode()))


class DocCensusTests(unittest.TestCase):
    def test_exact_doc_record_is_finite_and_separate_from_legacy(self):
        key = (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "docs/security/reviewed-generic-fixtures.md",
            "825f87972a207b3ad52a99250f230db13ef55777be78eb566208d674d24419c6",
        )
        self.assertEqual(
            relay._REVIEWED_GENERIC_MATCHES[key],
            frozenset(
                {
                    ("assigned credential", 5876, 5917, 5893, 5917),
                    ("assigned credential", 5935, 5972, 5952, 5972),
                    ("assigned secret", 6085, 6115, 6093, 6115),
                    ("assigned secret", 6278, 6312, 6286, 6312),
                }
            ),
        )
        self.assertNotIn(key, relay._REVIEWED_GENERIC_FIXTURES)

    def test_four_match_census_requires_every_match_and_guards(self):
        repo, path = "fixture-org/fixture-repo", "review.md"
        body = "\n".join("const secret = '" + char * 24 + "';" for char in "abcd")
        census = MatchPinnedReview().metadata(relay, body)
        self.assertEqual(len(census), 4)
        for text, matches, known, accepts in [
            (body, census, [], True),
            (body, frozenset(list(census)[:3]), [], False),
            (body + "\nconst token = '" + "e" * 24 + "';", census, [], False),
            ("\n" + body, census, [], False),
            (body, census, ["aaa"], False),
            (body + " gh" + "p_" + "f" * 40, census, [], False),
        ]:
            key = (repo, path, hashlib.sha256(text.encode()).hexdigest())
            with patch.object(relay, "_REVIEWED_GENERIC_MATCHES", {key: matches}):
                self.assertEqual(
                    unreviewed_file_secret(repo, path, text.encode(), known) is None, accepts
                )


class DurableReviewTests(unittest.TestCase):
    def test_document_provenance_and_pending_target_census(self):
        root = Path(__file__).resolve().parents[3]
        record = json.loads(
            (root / "docs/security/ct-match-pinned-remediation-review.json").read_text()
        )
        doc = record["reviewed_document"]
        source = record["provenance_source"]
        self.assertEqual(source["path"], "scripts/linear-board/test_board.py")
        self.assertEqual(
            source["sha256"], "9e96737087b55d3ba6f82f7fb8e5e0a5b17828d10f2759e73dae65063211cfe3"
        )
        self.assertEqual(source["source_match_lines"], [51, 2070, 930, 3737])
        raw = (root / doc["path"]).read_bytes()
        current_digest = hashlib.sha256(raw).hexdigest()
        # The future target is deliberately not smuggled into this source repair.
        # The exact main original remains a complete prefix, with all four
        # original match coordinates unchanged. Once the frozen candidate is
        # independently released, the same test also binds its full digest.
        self.assertIn(current_digest, {doc["current_main_prefix"]["sha256"], doc["sha256"]})
        expected = frozenset(tuple(row) for row in doc["effective_matches"])
        computed = MatchPinnedReview().metadata(relay, raw.decode("utf-8"))
        self.assertEqual(computed, expected)
        key = (doc["repository"], doc["path"], doc["sha256"])
        self.assertEqual(relay._REVIEWED_GENERIC_MATCHES[key], computed)
        if current_digest == doc["sha256"]:
            self.assertEqual(len(raw), doc["bytes"])
        else:
            self.assertEqual(len(raw), doc["current_main_prefix"]["bytes"])
            self.assertIsNotNone(relay.scan_file(doc["repository"], doc["path"], raw.decode()))
        ct = record["reviewed_CT_fixture"]
        ct_key = (ct["repository"], ct["path"], ct["sha256"])
        self.assertEqual(
            relay._REVIEWED_GENERIC_MATCHES[ct_key],
            frozenset(tuple(row) for row in ct["effective_matches"]),
        )


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
        # Main has moved on since this candidate was reviewed, so the checkout
        # no longer has to hold either reviewed version. The registry and the
        # record stay bound to each other; the byte checks run only when the
        # checkout actually holds the admitted candidate.
        raw = (root / doc["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() == self.key[2]:
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
