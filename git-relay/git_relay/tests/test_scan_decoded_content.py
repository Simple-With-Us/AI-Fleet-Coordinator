"""The envelope scan leaves file content to the verbs that decode it, and scans everything else as before."""

import unittest

from git_relay.secret_scan import InputTooComplexError, SecretInputError, scan_args

TOKEN = "ghp_" + "a1b2c3d4" * 5


class ScanDecodedContent(unittest.TestCase):
    def test_content_b64_is_left_to_the_verb_only_when_asked(self):
        args = {"files": [{"path": "a", "content_b64": TOKEN}]}
        scan_args(args, _decoded_elsewhere=True)
        with self.assertRaises(SecretInputError):
            scan_args(args)

    def test_every_other_field_is_still_scanned(self):
        for args in (
            {"message": TOKEN, "files": [{"path": "a", "content_b64": "QQ=="}]},
            {"files": [{"path": TOKEN, "content_b64": "QQ=="}]},
            {"files": [{"other_b64": TOKEN}]},
        ):
            with self.assertRaises(SecretInputError):
                scan_args(args, _decoded_elsewhere=True)

    def test_a_non_string_content_is_still_walked(self):
        with self.assertRaises(SecretInputError):
            scan_args({"content_b64": {"nested": TOKEN}}, _decoded_elsewhere=True)

    def test_non_canonical_nested_content_b64_is_scanned(self):
        for args in (
            {"meta": {"content_b64": TOKEN}},
            {"files": [{"path": "a", "nested": {"content_b64": TOKEN}}]},
            {"data": [{"content_b64": TOKEN}]},
            {"files": [{"path": "a", "content_b64": "QQ=="}], "extra": {"content_b64": TOKEN}},
        ):
            with self.assertRaises(SecretInputError):
                scan_args(args, _decoded_elsewhere=True)

    def test_only_the_canonical_paths_skip(self):
        scan_args({"content_b64": TOKEN}, _decoded_elsewhere=True)
        scan_args(
            {"files": [{"path": "a", "content_b64": TOKEN}, {"path": "b", "content_b64": TOKEN}]},
            _decoded_elsewhere=True,
        )

    def test_skipped_content_counts_against_the_node_bound(self):
        with self.assertRaises(InputTooComplexError):
            scan_args({"content_b64": "QQ=="}, _budget=[1, 100], _decoded_elsewhere=True)
        scan_args({"content_b64": "QQ=="}, _budget=[2, 100], _decoded_elsewhere=True)

    def test_skipped_content_is_charged_in_utf8_bytes(self):
        with self.assertRaises(InputTooComplexError):
            scan_args({"content_b64": "\u00e9" * 6}, _budget=[10, 11], _decoded_elsewhere=True)
        scan_args({"content_b64": "\u00e9" * 6}, _budget=[10, 12], _decoded_elsewhere=True)

    def test_skipped_content_still_counts_against_the_byte_bound(self):
        with self.assertRaises(InputTooComplexError):
            scan_args({"content_b64": "A" * 100}, _budget=[100, 10], _decoded_elsewhere=True)


if __name__ == "__main__":
    unittest.main()
