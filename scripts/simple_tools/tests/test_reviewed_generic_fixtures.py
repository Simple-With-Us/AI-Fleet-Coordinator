"""Exact full-content review boundary, generic assignments only."""

import hashlib
import unittest
from unittest.mock import patch

from simple_tools import secretscan


class ReviewedGenericTests(unittest.TestCase):
    def setUp(self):
        self.repo = "example-org/example-repo"
        self.path = "test/fixture.ts"
        self.body = "const token = '" + "a" * 24 + "';\n"

    def approve(self, body):
        return patch.object(secretscan, "_REVIEWED_GENERIC_FIXTURES", frozenset({
            (self.repo, self.path, hashlib.sha256(body.encode()).hexdigest())
        }))

    def test_registry_contains_only_the_full_content_review(self):
        self.assertEqual(secretscan._REVIEWED_GENERIC_FIXTURES, frozenset({
            (
                "Simple-With-Us/CodeCaps",
                "ios/CodeCapsCompanion/Widgets/WidgetPresentation.swift",
                "d6bd7e7ff0a69af9cfd4002f1e21db90f66b87315a4dc69cbd2fc1c585be0b2b",
            ),
            (
                "Simple-With-Us/CodeCaps",
                "ios/CodeCapsCompanion/App/Models/CompanionQuotaModel.swift",
                "1e44f5721cf37b067a3a1a2091fc316d2a9f95546effc22d756126eca687894b",
            ),
            (
                "Simple-With-Us/Usage-Monitor",
                "scripts/lib/run-session-token-collector.mjs",
                "e96f73542ff0a5923af4d53981f96693bb67f80444e54cf94a520737ac5418f4",
            ),
            (
                "Simple-With-Us/AI-Fleet-Coordinator",
                "scripts/linear-board/test_board.py",
                "9e96737087b55d3ba6f82f7fb8e5e0a5b17828d10f2759e73dae65063211cfe3",
            ),
            (
                "Simple-With-Us/AI-Fleet-Coordinator",
                "git-relay/git_relay/server.py",
                "82b53da38ce0a110a3f25b947fbcabcfcef031a8295795d72f46f48d55d79305",
            ),
        }))
        muse = {
            ("scripts/muse-code-usage-collector.mjs", "92618a94e0b5efd9dfcc2ed97b9f39797e1d90773cc5816bfcb25843991ce920", ("assigned secret", 8103, 8130, 8111, 8130)),
            ("scripts/__tests__/muse-code-usage-collector.test.mjs", "aeb48889d7147e35f0059a7448be7c58916a075f6153b78303b20947ca7a6fe0", ("assigned secret", 8630, 8656, 8640, 8656)),
        }
        for path, digest, span in muse:
            key = ("Simple-With-Us/Usage-Monitor", path, digest)
            self.assertNotIn(key, secretscan._REVIEWED_GENERIC_FIXTURES)
            self.assertEqual(secretscan._REVIEWED_GENERIC_MATCHES[key], frozenset({span}))
        self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, self.body))

    def test_approved_path_does_not_approve_other_bytes_or_quoted_credentials(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            self.body,
            "let token = '" + "b" * 32 + "'\n",
            "let token = Widget" + "Presentation.shortCadence9(cadence: cadence)\n",
            "let token = widget" + "presentation.shortcadence(cadence: cadence)\n",
            "let token = Correct" + ".Horse.Battery\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_letters_only_dotted_calls_are_not_secrets(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        self.assertIsNone(secretscan.scan_file(
            repo, path,
            "let token = Widget" + "Presentation.shortCadence(cadence: cadence, label: label)\n"))

    def test_labeled_call_argument_chains_are_not_secrets(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "let s = Reset" + "AlarmCadence.periodSeconds(token: snapshot.wind" + "ow.window, label: snapshot.wind" + "ow.label)\n",
            "foo(token: config.api" + "Key, label: label)\n",
        ):
            self.assertIsNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_form_still_refuses_nonsource_values(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "token: snapshot.window" + ".window\n",
            "foo(token: \"snapshot.window" + ".window\")\n",
            "let token = snapshot.window" + ".window\n",
            "foo(token: " + "a" * 24 + ")\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_multiline_and_second_position(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "foo(\n    token: snapshot.wind" + "ow.window,\n    label: label)\n",
            "foo(a: 1, token: snapshot.wind" + "ow.window)\n",
        ):
            self.assertIsNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_requires_call_paren(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "Hello, token: snapshot.wind" + "ow.window\n",
            "(done), token: snapshot.wind" + "ow.window\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_earlier_string_arg_and_closure(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "foo(label: \"x\", token: snapshot.wind" + "ow.window)\n",
            "foo({ bar in\n    baz(x) }, token: snapshot.wind" + "ow.window)\n",
        ):
            self.assertIsNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_balanced_call_before_comma_refuses(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        body = "bar(x), token: snapshot.wind" + "ow.window\n"
        self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_needs_a_call_paren_not_prose_or_quoted_paren(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        for body in (
            "see (the note, token: snapshot.wind" + "ow.window)\n",
            "\"x(\", token: snapshot.wind" + "ow.window)\n",
        ):
            self.assertIsNotNone(secretscan.scan_file(repo, path, body))
        ok = "foo(a: \"x(\", token: snapshot.wind" + "ow.window)\n"
        self.assertIsNone(secretscan.scan_file(repo, path, ok))

    def test_labeled_argument_balanced_grouping_paren_before_label(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        body = "foo((a + b), c, token: snapshot.wind" + "ow.window)\n"
        self.assertIsNone(secretscan.scan_file(repo, path, body))
        body = "foo(a, (see the note, token: snapshot.wind" + "ow.window))\n"
        self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_labeled_argument_requires_colon_separator(self):
        repo, path, _digest = next(iter(secretscan._REVIEWED_GENERIC_FIXTURES))
        body = "foo(token=snapshot.wind" + "ow.window)\n"
        self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_only_exact_bytes_repo_path(self):
        with self.approve(self.body):
            self.assertIsNone(secretscan.scan_file(self.repo, self.path, self.body))
            self.assertIsNotNone(secretscan.scan(self.body))
            for repo, path, body in [(self.repo + "2", self.path, self.body),
                                     (self.repo, "other.ts", self.body),
                                     (self.repo, self.path, self.body + "\n")]:
                self.assertIsNotNone(secretscan.scan_file(repo, path, body))

    def test_dedicated_provider_loaded_values_absolute(self):
        for body, known in [(self.body + "gh" + "p_" + "a" * 40, []),
                            (self.body + "sk-" + "b" * 30, []),
                            (self.body, ["aaa"]),
                            (self.body + "Authorization: Bearer " + "c" * 30, [])]:
            with self.approve(body):
                self.assertIsNotNone(secretscan.scan_file(self.repo, self.path, body, known))



import pathlib


class NativeOriginCaptureProbeEntryTests(unittest.TestCase):
    """Entry-specific coverage for the match-pinned probe fixture admission.

    The reviewed fixture file is admitted by exact full-file digest plus the
    exact nonempty effective-match census, never by the legacy whole-file
    route.  The credential-shaped assignment is a deliberate synthetic
    negative control:  the probe test plants it in a caller-controlled tool
    name and asserts the probe never echoes it.  See
    docs/security/reviewed-fixtures/native-origin-capture-probe-2026-11-25.md.

    The shipped-entry pins and test_exact_reviewed_bytes_admit_once_merged
    concern the actual reviewed fixture.  Every other test here uses a tiny
    synthetic body with a monkeypatched registry:  they pin mechanism
    behavior for this entry's shape, and are not executed-actual-fixture
    regressions.  Actual-fixture qualification runs host-side on the
    combined source tree before publication.
    """

    REPO = "Simple-With-Us/AI-Fleet-Coordinator"
    PATH = "scripts/mcp-servers/test_native_origin_capture.py"
    DIGEST = "a9c9dbb7f15cc0bb160a4bbb78c906bfd7ec70f2d84c2ab9b2e92645f22242b6"
    KEY = (REPO, PATH, DIGEST)
    CENSUS = frozenset({("assigned secret", 9436, 9471, 9446, 9471)})
    ROOT = pathlib.Path(__file__).resolve().parents[3]

    @staticmethod
    def census_of(body):
        found = set()
        for label, pattern in secretscan._PATTERNS:
            if label in secretscan._GENERIC_ASSIGNMENTS and "value" in pattern.groupindex:
                for match in pattern.finditer(body):
                    found.add((label, *match.span(), *match.span("value")))
        return frozenset(found)

    def pin(self, body, census):
        digest = hashlib.sha256(body.encode()).hexdigest()
        return patch.object(
            secretscan, "_REVIEWED_GENERIC_MATCHES", {(self.REPO, self.PATH, digest): census}
        )

    def test_shipped_entry_is_match_pinned_not_legacy(self):
        self.assertNotIn(self.KEY, secretscan._REVIEWED_GENERIC_FIXTURES)
        self.assertEqual(secretscan._REVIEWED_GENERIC_MATCHES[self.KEY], self.CENSUS)

    def test_synthetic_exact_pinned_bytes_admit_and_raw_scan_still_refuses(self):
        body = 'secret = "' + "a" * 22 + '"\n'
        self.assertEqual(len(self.census_of(body)), 1)
        with self.pin(body, self.census_of(body)):
            self.assertIsNone(secretscan.scan_file(self.REPO, self.PATH, body))
            self.assertEqual(secretscan.scan(body), "assigned secret")

    def test_synthetic_wrong_repo_path_or_digest_fail_closed(self):
        body = 'secret = "' + "a" * 22 + '"\n'
        census = self.census_of(body)
        with self.pin(body, census):
            for repo, path, text in [
                (self.REPO + "2", self.PATH, body),
                (self.REPO, self.PATH + "2", body),
                (self.REPO, self.PATH, body + "\n"),
            ]:
                with self.subTest(repo=repo, path=path):
                    self.assertEqual(
                        secretscan.scan_file(repo, path, text), "assigned secret"
                    )

    def test_synthetic_tampered_or_incomplete_census_refuses(self):
        body = 'secret = "' + "a" * 22 + '"\n'
        census = self.census_of(body)
        (label, m0, m1, v0, v1), = census
        for bad in (frozenset(), frozenset({(label, m0, m1, v0, v1 + 1)}),
                    frozenset({("assigned credential", m0, m1, v0, v1)})):
            with self.subTest(census=bad), self.pin(body, bad):
                self.assertEqual(
                    secretscan.scan_file(self.REPO, self.PATH, body), "assigned secret"
                )

    def test_synthetic_additional_generic_match_beyond_census_refuses(self):
        body = 'secret = "' + "a" * 22 + '"\n'
        grown = body + 'token = "' + "b" * 20 + '"\n'
        # Pin the grown bytes with only the original one-match census.
        with self.pin(grown, self.census_of(body)):
            self.assertEqual(
                secretscan.scan_file(self.REPO, self.PATH, grown), "assigned secret"
            )

    def test_synthetic_loaded_values_stay_absolute_including_short_contained(self):
        body = 'secret = "' + "a" * 22 + '"\n'
        with self.pin(body, self.census_of(body)):
            for known in (["a" * 22], ["aaaa"]):
                with self.subTest(known=known):
                    self.assertEqual(
                        secretscan.scan_file(self.REPO, self.PATH, body, known=known),
                        "a loaded credential",
                    )

    def test_synthetic_provider_bearer_and_zulip_stay_absolute_at_exact_digest(self):
        body = 'secret = "' + "a" * 22 + '"\n'
        zulip = "Ab1" * 10 + "C2"
        cases = [
            (body + "Authorization: Bearer " + "c" * 24 + "\n", "bearer token"),
            (body + zulip + "\n", "a Zulip-shaped API key"),
            (body + "eyJ" + "d" * 10 + ".eyJ" + "e" * 10 + "." + "f" * 10 + "\n",
             "access token (JWT)"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected), self.pin(payload, self.census_of(payload)):
                self.assertEqual(
                    secretscan.scan_file(self.REPO, self.PATH, payload), expected
                )

    def test_scanner_copies_stay_byte_identical(self):
        simple = self.ROOT / "scripts" / "simple_tools" / "secretscan.py"
        relay = self.ROOT / "git-relay" / "git_relay" / "patch_secretscan.py"
        self.assertEqual(simple.read_bytes(), relay.read_bytes())

    # Main's pre-probe bytes at the same path: the probe patch adds 159
    # lines to an existing file, so this older digest is the expected state
    # until the probe merges.  It needs no admission (it scans clean
    # unregistered) and is pinned here so only these two known states skip
    # or pass - any other digest is a loud failure, never a silent skip.
    PRE_PROBE_DIGEST = "0a46521889e6df8142c28d9d5fdbed6278db740eaeaedefff5dfb1de0c9b0110"

    def test_exact_reviewed_bytes_admit_once_merged(self):
        real = self.ROOT / self.PATH
        self.assertTrue(real.is_file(), "established native-origin fixture is missing")
        text = real.read_text()
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest == self.PRE_PROBE_DIGEST:
            self.skipTest("probe patch not merged yet; pre-probe bytes present")
        self.assertEqual(
            digest,
            self.DIGEST,
            "probe test file drifted from every known digest; a byte change "
            "is a new file and needs a new review",
        )
        self.assertIsNone(secretscan.scan_file(self.REPO, self.PATH, text))
        self.assertEqual(secretscan.scan(text), "assigned secret")
