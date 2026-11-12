#!/usr/bin/env python3
"""Tests that autofix_claude_failure_log.sh redacts credentials before logging."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "autofix_claude_failure_log.sh")


def fixture_token() -> str:
    """Made-up credential shape for redaction tests; not a real secret."""
    return "not-a-real-" + "fixture-credential-placeholder"


def run_log(result_field: str) -> str:
    payload = [
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "error": result_field,
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "is_error": True, "content": result_field}]
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": result_field}]}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        return subprocess.check_output(["bash", SCRIPT, path], text=True)
    finally:
        os.unlink(path)


class RedactionTests(unittest.TestCase):
    def assert_redacted(self, field: str) -> None:
        out = run_log(field)
        token = fixture_token()
        self.assertNotIn(token, out)
        self.assertIn("[REDACTED]", out)

    def test_authorization_bearer_header(self) -> None:
        self.assert_redacted(f"401 Authorization: Bearer {fixture_token()} rejected")

    def test_bare_bearer(self) -> None:
        self.assert_redacted(f"sent bearer {fixture_token()}")

    def test_api_key_colon_space(self) -> None:
        self.assert_redacted(f"header api-key: {fixture_token()}")

    def test_x_api_key_equals(self) -> None:
        self.assert_redacted(f"x-api-key={fixture_token()}")

    def test_json_quoted_api_key(self) -> None:
        self.assert_redacted(f'{{"api_key": "{fixture_token()}"}}')

    def test_sk_key(self) -> None:
        self.assert_redacted(f"key sk-{fixture_token()}")

    def test_plain_text_kept(self) -> None:
        out = run_log("model not found: meta/muse-spark")
        self.assertIn("model not found: meta/muse-spark", out)


if __name__ == "__main__":
    unittest.main()
