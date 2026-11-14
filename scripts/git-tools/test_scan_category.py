import base64
import unittest
from unittest import mock

import test_git_remote as fixture

git = fixture.git


class CategoryTests(unittest.TestCase):
    def test_fixed_public_vocabulary_matches_loaded_scanner(self):
        scanner = git._scanner()
        labels = {label for label, pattern in scanner._PATTERNS}
        self.assertEqual(git._SCAN_CATEGORIES, labels | {"a Zulip-shaped API key", "a loaded credential"})

    def test_public_labels_only(self):
        for label in git._SCAN_CATEGORIES:
            self.assertEqual(git._scan_category(label), label)
        for value in [
            "secret-value-not-a-category",
            "AWS access key\nleak",
            {"kind": "secret"},
            None,
            1,
        ]:
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
        args = {
            "repo": "o/r",
            "files": [
                {"path": "test.py", "content_b64": base64.b64encode(b"synthetic text").decode()}
            ],
        }
        with mock.patch.object(git, "_scanner", return_value=scanner):
            result = git._scan_arguments(args, "synthetic-bearer", "gr_wf_files_put")
        self.assertEqual(
            result, r"secret_scan:files\u005b0\u005d.content_b64 [category=assigned secret]"
        )
        scanner.scan_file.assert_called_once_with(
            "o/r", "test.py", "synthetic text", ["synthetic-bearer"]
        )

    def test_decisions_match_pristine_for_synthetic_cases(self):
        # Same scanner and traversal, no network and no refused production inputs.
        def pristine(arguments, bearer, tool, scanner) -> str | None:
            """The first argument field the fleet secret scanner flags, as `secret_scan:<field>` (the field name only,
            never the text or any part of it), or None.  Every string at any depth is checked, and the bearer itself
            always matches."""
            files = {}
            if isinstance(arguments, dict) and tool in {"gr_wf_file_put", "gr_wf_files_put"}:
                repo = arguments.get("repo")
                rows = [arguments] if tool == "gr_wf_file_put" else arguments.get("files", [])
                if isinstance(repo, str) and isinstance(rows, list):
                    for i, row in enumerate(rows):
                        if isinstance(row, dict) and isinstance(row.get("path"), str):
                            field = "content_b64" if tool == "gr_wf_file_put" else f"files[{i}].content_b64"
                            files[field] = (repo, row["path"])
            stack = [("", arguments)]
            while stack:
                path, value = stack.pop()
                if isinstance(value, str):
                    if path.endswith("content_b64"):
                        # Base64 of ordinary bytes looks like a token, and it hides a real one, so the decoded text is what is
                        # scanned.  Anything that does not decode is scanned as it came.
                        try:
                            value = base64.b64decode(value, validate=True).decode("utf-8", "replace")
                        except (git.binascii.Error, ValueError):
                            pass
                    known = [bearer] if bearer else []
                    if path in files and callable(getattr(scanner, "scan_file", None)):
                        hit = scanner.scan_file(*files[path], value, known)
                    else:
                        hit = scanner.scan(value, known=known)
                    if hit is not None:
                        return "secret_scan:" + (path or "arguments")
                elif isinstance(value, dict):
                    stack.extend((f"{path}.{k}" if path else str(k), v) for k, v in value.items())
                elif isinstance(value, (list, tuple)):
                    stack.extend((f"{path}[{i}]", v) for i, v in enumerate(value))
            return None

        cases = [
            {"text": "ordinary"},
            {"text": "synthetic-bearer"},
            {"repo": "o/r", "files": [{"path": "file-aware.py", "content_b64": base64.b64encode(b"ordinary").decode()}]},
            {"text": "synthetic-token"},
            {"nested": [{"value": "synthetic-token"}]},
            {
                "repo": "o/r",
                "files": [
                    {"path": "x.py", "content_b64": base64.b64encode(b"synthetic-token").decode()}
                ],
            },
            {"repo": "o/r", "files": [{"path": "x.py", "content_b64": "invalid-base64!"}]},
        ]

        class Scanner:
            def scan(self, value, known=()):
                if any(token in value for token in known):
                    return "a loaded credential"
                return "assigned secret" if "synthetic-token" in value else None

            def scan_file(self, repo, path, value, known=()):
                if repo == "o/r" and path == "file-aware.py":
                    return "assigned secret"
                return self.scan(value, known)

        for case in cases:
            with mock.patch.object(git, "_scanner", return_value=Scanner()):
                before = pristine(case, "synthetic-bearer", "gr_wf_files_put", Scanner())
                after = git._scan_arguments(case, "synthetic-bearer", "gr_wf_files_put")
            self.assertEqual(before is None, after is None)
            if before is not None:
                expected_path = fixture.json.dumps(
                    before.removeprefix("secret_scan:"), ensure_ascii=True
                )[1:-1]
                expected_path = expected_path.replace("[", "\\u005b").replace("]", "\\u005d")
                self.assertEqual(after.split(" [category=", 1)[0], "secret_scan:" + expected_path)

    def test_untrusted_path_cannot_spoof_category_or_control_output(self):
        scanner = mock.Mock()
        scanner.scan.return_value = "unclassified"
        for key in [
            "x [category=assigned secret]",
            "x\n[category=assigned secret]",
            r"x\u005bcategory=assigned secret\u005d",
        ]:
            with self.subTest(key=key), mock.patch.object(git, "_scanner", return_value=scanner):
                result = git._scan_arguments({key: "synthetic text"}, "")
            self.assertEqual(result.count("[category="), 1)
            self.assertTrue(result.endswith(" [category=unclassified]"))
            self.assertNotIn("\n", result)
        self.assertIn(r"\\u005b", result)  # Literal backslashes cannot impersonate escaping.

    def test_real_scanner_target_never_sent(self):
        transport = fixture.FakeTransport()
        code, out, err = fixture.run_cli(
            [
                "call",
                "gr_wf_issue_create",
                "--args-json",
                fixture.json.dumps({"title": "token " + fixture.BEARER}),
            ],
            env={"SIMPLE_TOOLS_BEARER": fixture.BEARER},
            transport=transport,
            scanner="real",
        )
        self.assertEqual(code, 2)
        self.assertIn("category=a loaded credential", err)
        self.assertNotIn(fixture.BEARER, err + out)
        self.assertEqual([c[0] for c in transport.calls], ["identities"])


if __name__ == "__main__":
    unittest.main()
