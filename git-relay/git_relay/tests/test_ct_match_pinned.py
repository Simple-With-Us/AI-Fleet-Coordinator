"""Match census invariants on the real server outbound file guard; no held file."""

import hashlib
import unittest
from unittest.mock import patch
from pathlib import Path
import json

from git_relay import patch_secretscan as relay
from git_relay.reviewed_fixtures import unreviewed_file_secret


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
        self.assertEqual(len(relay._REVIEWED_GENERIC_FIXTURES), 4)
        self.assertEqual(len(relay._REVIEWED_PUBLIC_INTEGRITIES), 2)
        self.assertEqual(len(relay._REVIEWED_GENERIC_MATCHES), 2)
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
