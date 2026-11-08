import hashlib
import secrets
import string
import unittest
from unittest.mock import patch

from git_relay import patch_secretscan as scanner
from git_relay import reviewed_preservation as candidate


class ReviewedPreservationTests(unittest.TestCase):
    repo = "Example/Private"
    path = "tests/fixture.py"

    def setUp(self):
        self.line = ("Authorization: Bearer " + "a" * 24 + "\n").encode()
        self.base = self.line + b"safe = old\n"
        self.edit = {"start": len(self.line), "end": len(self.base), "text": "safe = new\n"}
        self.result = self.line + b"safe = new\n"

    def record(self, base, result, repo=None, path=None):
        return (
            repo or self.repo,
            path or self.path,
            hashlib.sha256(base).hexdigest(),
            hashlib.sha256(result).hexdigest(),
        )

    def run_candidate(self, base, edits, result, known=()):
        with patch.object(
            candidate, "REVIEWED_PRESERVATIONS", frozenset({self.record(base, result)})
        ):
            return candidate.apply_reviewed_patch(self.repo, self.path, base, edits, known)

    def test_registry_empty_and_default_dedicated_refusal_unchanged(self):
        self.assertEqual(candidate.REVIEWED_PRESERVATIONS, frozenset())
        with self.assertRaises(ValueError):
            candidate.apply_reviewed_patch(self.repo, self.path, self.base, [self.edit])

    def test_exact_whole_source_line_and_digest_proof(self):
        self.assertEqual(self.run_candidate(self.base, [self.edit], self.result), self.result)

    def test_record_does_not_affect_raw_file_scanner(self):
        with patch.object(
            candidate, "REVIEWED_PRESERVATIONS", frozenset({self.record(self.base, self.result)})
        ):
            self.assertIsNotNone(scanner.scan_file(self.repo, self.path, self.result.decode()))

    def test_wrong_repo_path_base_and_output_digests_fail_closed(self):
        records = [
            self.record(self.base, self.result, repo="Other/Repo"),
            self.record(self.base, self.result, path="elsewhere.py"),
            self.record(b"other\n", self.result),
            self.record(self.base, self.result + b"x"),
        ]
        for record in records:
            with (
                self.subTest(record=record),
                patch.object(candidate, "REVIEWED_PRESERVATIONS", frozenset({record})),
            ):
                with self.assertRaises(ValueError):
                    candidate.apply_reviewed_patch(self.repo, self.path, self.base, [self.edit])

    def test_loaded_values_short_or_removed_are_absolute(self):
        for known in [("a",), ("old",), ("new",)]:
            with self.subTest(known=known), self.assertRaises(ValueError):
                self.run_candidate(self.base, [self.edit], self.result, known)

    def test_new_match_cannot_be_blessed_by_result_digest(self):
        base = b"ordinary\n"
        edit = {"start": len(base), "end": len(base), "text": self.line.decode()}
        with self.assertRaises(ValueError):
            self.run_candidate(base, [edit], base + self.line)

    def test_replacement_identical_bytes_are_new(self):
        edit = {"start": 0, "end": len(self.line), "text": self.line.decode() + "safe\n"}
        with self.assertRaises(ValueError):
            self.run_candidate(self.base, [edit], self.line + b"safe\nsafe = old\n")

    def test_copy_and_move_are_new(self):
        for edits, result in [
            (
                [{"start": len(self.base), "end": len(self.base), "text": self.line.decode()}],
                self.base + self.line,
            ),
            (
                [
                    {"start": 0, "end": len(self.line), "text": ""},
                    {"start": len(self.base), "end": len(self.base), "text": self.line.decode()},
                ],
                b"safe = old\n" + self.line,
            ),
        ]:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                self.run_candidate(self.base, edits, result)

    def test_new_match_assembled_at_edit_boundary_refused(self):
        base = ("Authorization: Bearer " + "a" * 10 + "\n").encode()
        edit = {"start": len(base) - 1, "end": len(base) - 1, "text": "a" * 14}
        with self.assertRaises(ValueError):
            self.run_candidate(base, [edit], self.line)

    def test_unchanged_match_with_changed_containing_line_refused(self):
        edit = {"start": 0, "end": 0, "text": "prefix "}
        with self.assertRaises(ValueError):
            self.run_candidate(self.base, [edit], b"prefix " + self.base)

    def test_deletion_joins_existing_fragments_refused(self):
        base = ("Authorization: Bearer " + "a" * 10 + "\n" + "a" * 14 + "\n").encode()
        point = base.index(b"\n")
        with self.assertRaises(ValueError):
            self.run_candidate(base, [{"start": point, "end": point + 1, "text": ""}], self.line)

    def test_invalid_intervals_duplicate_insertions_and_unknown_fields(self):
        for edits in [
            [dict(start=-1, end=0, text="x")],
            [dict(start=0, end=0, text="x")] * 2,
            [dict(start=True, end=1, text="x")],
            [dict(start=0, end=0, text="x", approved=True)],
            [dict(start=0, end=2, text="x"), dict(start=1, end=3, text="y")],
        ]:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                self.run_candidate(self.base, edits, self.result)

    def test_utf8_offsets_and_codepoint_splitting(self):
        base = "é\n".encode() + self.base
        result = base + "安全\n".encode()
        self.assertEqual(
            self.run_candidate(base, [dict(start=len(base), end=len(base), text="安全\n")], result),
            result,
        )
        with self.assertRaises(ValueError):
            self.run_candidate(base, [dict(start=1, end=1, text="x")], result)

    def test_all_dedicated_families_need_unchanged_source_origin(self):
        shaped = ""
        while not scanner._zulip_shaped(shaped):
            shaped = "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
            )
        lines = [
            "gh" + "p_" + "a" * 40,
            "AK" + "IA" + "A" * 16,
            "-----BEGIN " + "PRIVATE KEY-----",
            shaped,
        ]
        for line in lines:
            base = (line + "\nold\n").encode()
            result = (line + "\nnew\n").encode()
            edit = dict(start=len(base) - 4, end=len(base), text="new\n")
            with self.subTest(line=line[:4]):
                self.assertEqual(self.run_candidate(base, [edit], result), result)
                with self.assertRaises(ValueError):
                    self.run_candidate(
                        base, [dict(start=0, end=0, text="changed ")], b"changed " + base
                    )

    def test_match_budget_remains_bounded(self):
        base = self.line * 4097 + b"old\n"
        with self.assertRaises(ValueError):
            self.run_candidate(
                base,
                [dict(start=len(base) - 4, end=len(base), text="new\n")],
                self.line * 4097 + b"new\n",
            )

    def test_generic_default_behavior_kept(self):
        base = ("apiKey = " + "stsProviderCredential(provider);\nold\n").encode()
        result = base[:-4] + b"new\n"
        self.assertEqual(
            candidate.apply_reviewed_patch(
                self.repo, self.path, base, [dict(start=len(base) - 4, end=len(base), text="new\n")]
            ),
            result,
        )


class PreservationWorkflowTests(unittest.TestCase):
    def setup_case(self, prefix):
        import base64

        from git_relay.tests.test_file_patch import PatchTests, blob

        args, replies = PatchTests().setup_case()
        old = base64.b64decode(replies[3]["content"])
        raw = prefix + old
        result = raw.replace(b"other = old", b"other = new")
        for edit in args["edits"]:
            edit["start"] += len(prefix)
            edit["end"] += len(prefix)
        args["base_blob_sha"] = blob(raw)
        replies[3].update(content=base64.b64encode(raw).decode(), sha=blob(raw))
        replies[5]["tree"][0]["sha"] = blob(raw)
        replies[8]["sha"] = blob(result)
        record = (
            args["repo"],
            args["path"],
            hashlib.sha256(raw).hexdigest(),
            hashlib.sha256(result).hexdigest(),
        )
        return args, replies, record

    def test_server_pinned_same_path_and_atomic_expected_head_retained(self):
        from git_relay.tests.test_workflow_verbs import SHA, Client, Multi

        args, replies, record = self.setup_case(
            ("Authorization: Bearer " + "a" * 24 + "\n").encode()
        )
        c = Client(*replies)
        with patch.object(candidate, "REVIEWED_PRESERVATIONS", frozenset({record})):
            out = Multi(c).run("wf_file_patch", args)
        self.assertEqual(out["commit"], "e" * 40)
        writes = [x for x in c.calls if x[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]["variables"]["input"]["expectedHeadOid"], SHA)

    def test_provider_guard_and_loaded_bearer_stay_absolute(self):
        from git_relay.tests.test_workflow_verbs import Client, Multi
        from git_relay.workflow_verbs import Refused

        for prefix in [("sk-proj-" + "a" * 30 + "\n").encode(), b"opaque\n"]:
            args, replies, record = self.setup_case(prefix)
            c = Client(*replies)
            with (
                self.subTest(prefix=prefix[:3]),
                patch.object(candidate, "REVIEWED_PRESERVATIONS", frozenset({record})),
            ):
                with self.assertRaises(Refused):
                    Multi(c).run("wf_file_patch", args)
            self.assertTrue(all(x[0] == "GET" for x in c.calls))

    def test_mismatched_path_blob_and_head_do_not_gain_review(self):
        from git_relay.tests.test_workflow_verbs import Client, Multi
        from git_relay.workflow_verbs import Refused

        for index in (1, 3):
            args, replies, record = self.setup_case(
                ("Authorization: Bearer " + "a" * 24 + "\n").encode()
            )
            if index == 1:
                replies[1] = {"object": {"sha": "f" * 40}}
            else:
                replies[3]["path"] = "different/path"
            c = Client(*replies)
            with patch.object(candidate, "REVIEWED_PRESERVATIONS", frozenset({record})):
                with self.assertRaises(Refused):
                    Multi(c).run("wf_file_patch", args)
            self.assertTrue(all(x[0] == "GET" for x in c.calls))
