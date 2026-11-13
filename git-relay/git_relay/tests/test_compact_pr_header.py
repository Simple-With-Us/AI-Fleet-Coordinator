"""Direct relay callers supply the compact PR header; no formatter dependency is required."""

import json
import unittest

from git_relay.github_client import GitHubClient
from git_relay.tests.test_git_relay import FakeOpener


class CompactPrHeaderTests(unittest.TestCase):
    def test_direct_pr_writes_preserve_compact_header_and_kody_first_line(self):
        header = "[CODEX:\u200a\u200a\u200aEE.1.S]"
        body = "@kody start-review\n" + header + "\nFull SHA " + "a" * 40
        reply = {
            "number": 7,
            "id": 9,
            "html_url": "https://github.com/example/fixture-repo/pull/7",
            "base": {"ref": "main", "repo": {"full_name": "example/fixture-repo"}},
            "head": {"ref": "feature", "label": "example:feature"},
        }
        for operation in ("open", "edit", "comment", "reply"):
            with self.subTest(operation=operation):
                opener = FakeOpener(reply)
                client = GitHubClient("synthetic", base_url="https://api.test", opener=opener)
                if operation == "open":
                    client.open_pr(
                        "example/fixture-repo", "main", "feature", header + " Title", body
                    )
                elif operation == "edit":
                    client.edit_pr("example/fixture-repo", 7, header + " Title", body)
                elif operation == "comment":
                    client.comment_pr("example/fixture-repo", 7, body)
                else:
                    client.reply_thread("example/fixture-repo", 7, 9, body)
                payload = json.loads(opener.requests[0].data)
                self.assertEqual(payload["body"], body)
                if "title" in payload:
                    self.assertEqual(payload["title"], header + " Title")


if __name__ == "__main__":
    unittest.main()
