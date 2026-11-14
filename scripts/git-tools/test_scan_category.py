import base64
import unittest
from unittest import mock
import test_git_remote as fixture

git = fixture.git

class CategoryTests(unittest.TestCase):
    def test_public_labels_only(self):
        for label in git._SCAN_CATEGORIES:
            self.assertEqual(git._scan_category(label), label)
        for value in ["secret-value-not-a-category", "AWS access key\nleak", {"kind": "secret"}, None, 1]:
            self.assertEqual(git._scan_category(value), "unclassified")

    def test_all_hits_still_refuse_and_clean_stays_clean(self):
        for hit in list(git._SCAN_CATEGORIES) + ["unknown-secret-value", {}, False, 0]:
            scanner = mock.Mock()
            scanner.scan.return_value = hit
            with mock.patch.object(git, "_scanner", return_value=scanner):
                result = git._scan_arguments({"text": "ordinary"}, "")
            self.assertIsNotNone(result)
            self.assertIn("secret_scan:text", result)
            self.assertNotIn("unknown-secret-value", result)
        scanner = mock.Mock()
        scanner.scan.return_value = None
        with mock.patch.object(git, "_scanner", return_value=scanner):
            self.assertIsNone(git._scan_arguments({"text": "ordinary"}, ""))

    def test_file_decode_scope_and_loaded_guard(self):
        scanner = mock.Mock()
        scanner.scan_file.return_value = "assigned secret"
        args = {"repo": "o/r", "files": [{"path": "test.py", "content_b64": base64.b64encode(b"synthetic text").decode()}]}
        with mock.patch.object(git, "_scanner", return_value=scanner):
            result = git._scan_arguments(args, "synthetic-bearer", "gr_wf_files_put")
        self.assertEqual(result, "secret_scan:files[0].content_b64 [category=assigned secret]")
        scanner.scan_file.assert_called_once_with("o/r", "test.py", "synthetic text", ["synthetic-bearer"])

    def test_decisions_match_pristine_for_synthetic_cases(self):
        # Same scanner and traversal, no network and no refused production inputs.
        def pristine(arguments, scanner):
            # Pinned baseline behavior for these synthetic cases: decode file bytes,
            # select file-aware scan, then return field only on every non-None hit.
            stack = [("", arguments)]
            while stack:
                path, value = stack.pop()
                if isinstance(value, str):
                    if path.endswith("content_b64"):
                        try:
                            value = base64.b64decode(value, validate=True).decode("utf-8", "replace")
                        except (ValueError, git.binascii.Error):
                            pass
                    if scanner.scan(value) is not None:
                        return "secret_scan:" + (path or "arguments")
                elif isinstance(value, dict):
                    stack.extend((f"{path}.{k}" if path else str(k), v) for k, v in value.items())
                elif isinstance(value, (list, tuple)):
                    stack.extend((f"{path}[{i}]", v) for i, v in enumerate(value))
            return None
        cases = [
            {"text": "ordinary"},
            {"text": "synthetic-token"},
            {"nested": [{"value": "synthetic-token"}]},
            {"repo": "o/r", "files": [{"path": "x.py", "content_b64": base64.b64encode(b"synthetic-token").decode()}]},
            {"repo": "o/r", "files": [{"path": "x.py", "content_b64": "invalid-base64!"}]},
        ]
        class Scanner:
            def scan(self, value, known=()):
                return "assigned secret" if "synthetic-token" in value else None
            def scan_file(self, repo, path, value, known=()):
                return self.scan(value, known)
        for case in cases:
            with mock.patch.object(git, "_scanner", return_value=Scanner()):
                before = pristine(case, Scanner())
                after = git._scan_arguments(case, "", "gr_wf_files_put")
            self.assertEqual(before is None, after is None)
            if before is not None:
                self.assertEqual(after.split(" [category=", 1)[0], before)

    def test_real_scanner_target_never_sent(self):
        transport = fixture.FakeTransport()
        code, out, err = fixture.run_cli(
            ["call", "gr_wf_issue_create", "--args-json", fixture.json.dumps({"title": "token " + fixture.BEARER})],
            env={"SIMPLE_TOOLS_BEARER": fixture.BEARER}, transport=transport, scanner="real")
        self.assertEqual(code, 2)
        self.assertIn("category=a loaded credential", err)
        self.assertNotIn(fixture.BEARER, err + out)
        self.assertEqual([c[0] for c in transport.calls], ["identities"])

if __name__ == "__main__":
    unittest.main()
