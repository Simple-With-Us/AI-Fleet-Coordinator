import unittest

from git_relay.provenance_patch import apply_patch


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.line = ("const apiKey = " + "stsProviderCredential(provider);\n").encode()
        self.base = self.line + b"other = old;\n"

    def test_unchanged_assignment_and_new_safe_content(self):
        out = apply_patch(
            self.base, [dict(start=len(self.line), end=len(self.base), text="other = newer;\n")]
        )
        self.assertEqual(out, self.line + b"other = newer;\n")

    def test_copy_and_replacement_are_new(self):
        for start, end in [(len(self.base), len(self.base)), (0, len(self.line))]:
            with self.assertRaises(ValueError):
                apply_patch(
                    self.base, [dict(start=start, end=end, text=self.line.decode() + "// copied\n")]
                )

    def test_new_token_and_boundary_assembly(self):
        for base, edit in [
            (b"apiKey = abc", dict(start=12, end=12, text="d" * 20)),
            (b"tok\n" + b"en = " + b"a" * 20, dict(start=3, end=4, text="")),
        ]:
            with self.assertRaises(ValueError):
                apply_patch(base, [edit])

    def test_unchanged_dedicated_and_loaded_values_refused(self):
        token = ("gh" + "p_" + "a" * 40).encode()
        for base, known in [(token + b"\nx", []), (self.base, ["stsProviderCredential"])]:
            with self.assertRaises(ValueError):
                apply_patch(base, [dict(start=len(base), end=len(base), text="y")], known)

    def test_multibyte_offsets(self):
        base = "é\n".encode() + self.base
        self.assertEqual(
            apply_patch(base, [dict(start=len(base), end=len(base), text="安全\n")]),
            base + "安全\n".encode(),
        )
        with self.assertRaises(ValueError):
            apply_patch(base, [dict(start=1, end=1, text="x")])

    def test_invalid_ranges_and_duplicate_insertions(self):
        for edits in [
            [dict(start=-1, end=0, text="x")],
            [dict(start=0, end=3, text="x"), dict(start=2, end=4, text="y")],
            [dict(start=0, end=0, text="x")] * 2,
        ]:
            with self.assertRaises(ValueError):
                apply_patch(self.base, edits)


class MoreProvenanceTests(unittest.TestCase):
    def test_reordered_or_inserted_match_never_inherits_origin(self):
        line = ("token = " + "a" * 24 + ";\n").encode()
        base = line + b"ordinary\n"
        with self.assertRaises(ValueError):
            apply_patch(
                base,
                [
                    dict(start=0, end=len(line), text=""),
                    dict(start=len(base), end=len(base), text=line.decode()),
                ],
            )

    def test_deletion_creates_assignment_across_unchanged_fragments(self):
        base = ("tok\nen = " + "a" * 24 + "\nordinary\n").encode()
        with self.assertRaises(ValueError):
            apply_patch(base, [dict(start=3, end=4, text="")])

    def test_duplicate_original_intervals_keep_their_own_origin(self):
        line = ("token = " + "a" * 24 + ";\n").encode()
        base = line + line + b"ordinary\n"
        self.assertEqual(
            apply_patch(base, [dict(start=len(base), end=len(base), text="safe\n")]),
            base + b"safe\n",
        )

    def test_quoted_bearer_in_base_is_not_a_generic_baseline(self):
        base = ("Authorization: Bearer " + "a" * 24 + "\n").encode()
        with self.assertRaises(ValueError):
            apply_patch(base, [dict(start=len(base), end=len(base), text="safe")])


class RemovedSecretTests(unittest.TestCase):
    def test_removing_dedicated_or_loaded_secret_still_refuses_base(self):
        base = ("gh" + "p_" + "a" * 40 + "\n").encode()
        with self.assertRaises(ValueError):
            apply_patch(base, [dict(start=0, end=len(base), text="safe\n")])
        base = b"known-secret-value\n"
        with self.assertRaises(ValueError):
            apply_patch(
                base, [dict(start=0, end=len(base), text="safe\n")], known=["known-secret-value"]
            )
