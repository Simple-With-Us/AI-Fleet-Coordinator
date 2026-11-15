"""secretscan main() CLI: exit codes, bounded input handling, and the no-echo rule.

Secrets below are built when the test runs, from a prefix and random filler, so no
line of this file is a key-shaped literal (the gitleaks job runs on this repo, and
so does the harness's own rule).
"""
from __future__ import annotations

import io
import json
import secrets
import unittest

from simple_tools.secretscan import main


def alnum(n: int) -> str:
    """n letters and digits with at least one capital, one lower-case letter and one digit."""
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    body = "".join(secrets.choice(chars) for _ in range(n - 3))
    return body + "Aa7"


def run_cli(argv, data: bytes):
    out = io.StringIO()
    rc = main(argv, stdin=io.BytesIO(data), stdout=out)
    lines = out.getvalue().splitlines()
    return rc, json.loads(lines[0]) if lines else None, out.getvalue()


class CliTest(unittest.TestCase):
    def test_safe_input_scans_clean(self) -> None:
        rc, doc, _ = run_cli([], b"release notes for the weekly sync, nothing sensitive")
        self.assertEqual(rc, 0)
        self.assertEqual(doc, {"ok": True, "field": "text", "code": "safe", "kind": None})

    def test_refused_secret_reports_kind_only_and_never_echoes(self) -> None:
        value = alnum(40)
        text = ("api_key = '" + value + "'").encode()
        rc, doc, raw = run_cli([], text)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["ok"], False)
        self.assertEqual(doc["code"], "refused_secret")
        self.assertEqual(doc["kind"], "assigned secret")
        # The no-echo rule: no substring of the scanned value appears in output.
        self.assertNotIn(value, raw)
        self.assertNotIn(text.decode(), raw)

    def test_field_allow_list(self) -> None:
        for field in ("request", "body", "topic", "status_text"):
            rc, doc, _ = run_cli(["--field", field], b"benign content")
            self.assertEqual(rc, 0)
            self.assertEqual(doc["field"], field)
        rc, doc, _ = run_cli(["--field", "not_a_field"], b"benign content")
        self.assertEqual(rc, 2)
        self.assertEqual(doc["code"], "invalid_field")

    def test_unknown_arguments_are_refused_without_echo(self) -> None:
        rc, doc, raw = run_cli(["--verbose"], b"benign content")
        self.assertEqual(rc, 2)
        self.assertEqual(doc["code"], "invalid_arguments")
        self.assertNotIn("--verbose", raw)

    def test_empty_input_is_refused(self) -> None:
        rc, doc, _ = run_cli([], b"")
        self.assertEqual(rc, 2)
        self.assertEqual(doc["code"], "invalid_input")

    def test_oversized_input_is_refused(self) -> None:
        rc, doc, _ = run_cli([], b"x" * (256 * 1024 + 1))
        self.assertEqual(rc, 2)
        self.assertEqual(doc["code"], "input_too_large")

    def test_exactly_at_the_cap_is_scanned(self) -> None:
        rc, doc, _ = run_cli([], b"x" * (256 * 1024))
        self.assertEqual(rc, 0)
        self.assertEqual(doc["code"], "safe")

    def test_invalid_utf8_is_refused(self) -> None:
        rc, doc, _ = run_cli([], b"\xff\xfe\x00\x01")
        self.assertEqual(rc, 2)
        self.assertEqual(doc["code"], "invalid_utf8")

    def test_every_result_is_one_json_line(self) -> None:
        for argv, data in (([], b"clean"), (["--field", "url"], b"clean"), ([], b"")):
            _, _, raw = run_cli(argv, data)
            self.assertTrue(raw.endswith("\n"))
            self.assertEqual(len(raw.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
