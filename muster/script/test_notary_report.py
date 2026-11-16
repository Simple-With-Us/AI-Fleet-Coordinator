"""Apple's notarization answers reach the release log only through an allow-list."""

import io
import json
import unittest
import uuid

import notary_report as report

# Made at run time, so no identifier is committed.
SUBMISSION = str(uuid.uuid4())


class SubmissionTests(unittest.TestCase):
    def test_a_well_formed_answer_gives_the_id_and_the_status(self):
        text = json.dumps({"id": SUBMISSION, "status": "Accepted", "message": "ignored"})
        self.assertEqual(report.parse_submission(text), (SUBMISSION, "Accepted"))

    def test_an_id_that_could_be_read_as_an_option_never_passes(self):
        for submission in ("-v", "--keychain-profile=x", "", None, 7, SUBMISSION + "0"):
            with self.subTest(submission=submission):
                text = json.dumps({"id": submission, "status": "Invalid"})
                self.assertIsNone(report.parse_submission(text))

    def test_a_malformed_answer_or_status_never_passes(self):
        for text in ("not json", "[]", json.dumps({"id": SUBMISSION, "status": "x" * 80})):
            with self.subTest(text=text[:20]):
                self.assertIsNone(report.parse_submission(text))

    def test_the_command_exits_one_on_a_bad_answer(self):
        status = report.main(["notary_report", "submission"], io.StringIO("{}"))
        self.assertEqual(status, 1)


class LogTests(unittest.TestCase):
    def test_only_the_allow_listed_fields_are_kept(self):
        log = {
            "status": "Invalid",
            "statusSummary": "Archive contains critical validation errors",
            "jobId": SUBMISSION,
            "sha256": "f" * 64,
            "archiveFilename": "Muster.zip",
            "issues": [
                {
                    "severity": "error",
                    "path": "Muster.zip/Muster.app/Contents/MacOS/Muster",
                    "message": "The executable does not have the hardened runtime enabled.",
                    "docUrl": "https://developer.apple.com/example",
                    "architecture": "arm64",
                }
            ],
        }
        lines = report.summarize_log(json.dumps(log))
        self.assertEqual(
            lines,
            [
                "status: Invalid",
                "summary: Archive contains critical validation errors",
                "issue: error Muster.zip/Muster.app/Contents/MacOS/Muster: "
                "The executable does not have the hardened runtime enabled.",
            ],
        )
        self.assertNotIn("f" * 64, "\n".join(lines))

    def test_an_unreadable_log_says_so_and_nothing_else(self):
        self.assertEqual(
            report.summarize_log("<html>"), ["The notarization log could not be read."]
        )

    def test_fields_of_the_wrong_type_are_named_not_printed(self):
        log = {"status": 0, "statusSummary": False, "issues": [7, {"message": ["x"]}]}
        self.assertEqual(
            report.summarize_log(json.dumps(log)),
            [
                "status: not readable",
                "summary: not readable",
                "issue: not readable",
                "issue: unknown no path: no message",
            ],
        )
        self.assertEqual(
            report.summarize_log(json.dumps({"status": "Invalid", "issues": "x"})),
            ["status: Invalid", "issues: not readable"],
        )

    def test_oversized_or_deeply_nested_answers_are_not_read(self):
        huge = json.dumps({"status": "Invalid", "statusSummary": "a" * (report.MAX_BYTES + 1)})
        deep = "[" * 100000 + "]" * 100000
        for text in (huge, deep):
            with self.subTest(size=len(text)):
                self.assertEqual(
                    report.summarize_log(text), ["The notarization log could not be read."]
                )
                self.assertIsNone(report.parse_submission(text))

    def test_a_long_field_is_clipped(self):
        lines = report.summarize_log(json.dumps({"status": "Invalid", "statusSummary": "a" * 900}))
        self.assertEqual(len(lines[1]), len("summary: ") + report.FIELD_LIMIT)


if __name__ == "__main__":
    unittest.main()
