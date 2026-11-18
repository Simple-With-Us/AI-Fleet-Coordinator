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


class FailureTests(unittest.TestCase):
    """A failed submit's own words never reach the log, only a fixed sentence and a status."""

    # Shaped like notarytool's answers;  the key id and issuer are made at run time.
    KEY_ID = uuid.uuid4().hex[:10].upper()
    ISSUER = str(uuid.uuid4())

    def run_failure(self, text):
        out = io.StringIO()
        real_stdout = report.sys.stdout
        report.sys.stdout = out
        try:
            status = report.main(["notary_report", "failure"], io.StringIO(text))
        finally:
            report.sys.stdout = real_stdout
        self.assertEqual(status, 0)
        return out.getvalue()

    def test_a_missing_agreement_is_named(self):
        text = ("Error: HTTP status code: 403. A required agreement is missing or has expired. "
                "This request requires an in-effect agreement that has not been signed or has expired.")
        name, _, status = report.classify_failure(text)
        self.assertEqual((name, status), ("agreement", "403"))

    def test_an_agreement_error_code_alone_is_named(self):
        name, _, _ = report.classify_failure('{"code": "FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED"}')
        self.assertEqual(name, "agreement")

    def test_a_refused_key_is_named(self):
        text = "Error: HTTP status code: 401. Unable to authenticate. Invalid credentials."
        self.assertEqual(report.classify_failure(text)[::2], ("not_authorized", "401"))

    def test_a_bare_forbidden_is_named(self):
        self.assertEqual(report.classify_failure("Error: HTTP status code: 403.")[::2], ("forbidden", "403"))

    def test_other_kinds(self):
        cases = {
            "Error: invalid private key at path": "key_unreadable",
            "Error: HTTP status code: 503. Service Unavailable": "apple_unavailable",
            "NSURLErrorDomain Code=-1001 The request timed out.": "network",
            "": "unknown",
            "something new from Apple": "unknown",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(report.classify_failure(text)[0], kind)

    def test_the_printed_line_carries_no_text_from_the_answer(self):
        text = ("Error: HTTP status code: 401. Unable to authenticate. key id %s issuer %s "
                "path /Users/runner/work/_temp/AuthKey_%s.p8" % (self.KEY_ID, self.ISSUER, self.KEY_ID))
        line = self.run_failure(text)
        self.assertIn("notarytool failure: not_authorized (HTTP 401).", line)
        for secret in (self.KEY_ID, self.ISSUER, "AuthKey", "/Users/runner"):
            self.assertNotIn(secret, line)

    def test_an_unknown_answer_prints_only_the_fixed_sentence(self):
        line = self.run_failure("odd answer %s" % self.ISSUER)
        self.assertEqual(line.strip(), "notarytool failure: unknown.  notarytool gave no answer this script recognizes.")


if __name__ == "__main__":
    unittest.main()
