#!/usr/bin/env python3
"""Idempotent fleet hold-label provisioning.  No network."""

from __future__ import annotations

import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "auto-merge-reusable.yml"


def load():
    path = Path(__file__).with_name("provision.py")
    spec = importlib.util.spec_from_file_location("hold_label_provision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fake:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        return self.answers.pop(0)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.repo = "Simple-With-Us/FleetLink"

    def test_present_labels_are_not_rewritten(self):
        names = [spec["name"] for spec in self.mod.STANDARD_LABELS]
        fake = Fake(
            [(200, {"name": name, "color": "ffffff", "description": "kept"}) for name in names]
        )
        result = self.mod.ensure_hold_labels(self.repo, fake)
        self.assertFalse(result["denied"])
        self.assertEqual([row["result"] for row in result["labels"]], ["present"] * len(names))
        self.assertTrue(all(call[0] == "GET" for call in fake.calls))

    def test_missing_label_is_created_once_and_a_second_run_does_not_create(self):
        answers = []
        for spec in self.mod.STANDARD_LABELS:
            if spec["name"] == "needs-human-merge":
                answers.append((404, {"message": "Not Found"}))
                answers.append((201, {"name": spec["name"], "color": spec["color"]}))
            else:
                answers.append((200, {"name": spec["name"]}))
        fake = Fake(answers)
        result = self.mod.ensure_hold_labels(self.repo, fake)
        created = [row for row in result["labels"] if row["result"] == "created"]
        self.assertEqual(created, [{"name": "needs-human-merge", "result": "created"}])
        posts = [call for call in fake.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["name"], "needs-human-merge")
        self.assertEqual(posts[0][2]["color"], "FBCA04")
        self.assertIn("person merges", posts[0][2]["description"])
        again = Fake([(200, {"name": spec["name"]}) for spec in self.mod.STANDARD_LABELS])
        second = self.mod.ensure_hold_labels(self.repo, again)
        self.assertTrue(all(row["result"] == "present" for row in second["labels"]))
        self.assertTrue(all(call[0] == "GET" for call in again.calls))

    def test_create_race_is_present(self):
        fake = Fake([(404, None), (422, {"message": "already_exists"})])
        result = self.mod.ensure_hold_labels(self.repo, fake, labels=self.mod.STANDARD_LABELS[:1])
        self.assertEqual(result["labels"], [{"name": "do-not-automerge", "result": "present"}])
        self.assertFalse(result["denied"])

    def test_permission_denied_creates_nothing_further(self):
        fake = Fake([(403, {"message": "Resource not accessible by integration"})])
        result = self.mod.ensure_hold_labels(self.repo, fake)
        self.assertTrue(result["denied"])
        self.assertEqual(result["labels"], [])
        self.assertEqual(len(fake.calls), 1)

    def test_unexpected_read_does_not_create(self):
        fake = Fake([(500, {"message": "boom"})])
        with self.assertRaises(self.mod.ProvisionError) as caught:
            self.mod.ensure_hold_labels(self.repo, fake)
        self.assertEqual(caught.exception.code, "label_read_failed")
        self.assertEqual([call[0] for call in fake.calls], ["GET"])
        self.assertNotIn("boom", str(caught.exception))

    def test_case_only_difference_is_present(self):
        fake = Fake([(200, {"name": "Needs-Human-Merge"})])
        label = self.mod.STANDARD_LABELS[-1]
        result = self.mod.ensure_hold_labels(self.repo, fake, labels=(label,))
        self.assertEqual(result["labels"][0]["result"], "present")
        self.assertEqual(fake.calls[0][0], "GET")

    def test_bad_repo_never_calls(self):
        fake = Fake([])
        with self.assertRaises(self.mod.ProvisionError) as caught:
            self.mod.ensure_hold_labels("not a repo", fake)
        self.assertEqual(caught.exception.code, "invalid_repo")
        self.assertEqual(fake.calls, [])

    def test_standard_set_covers_the_workflow_default_and_the_guard_spellings(self):
        names = {spec["name"] for spec in self.mod.STANDARD_LABELS}
        self.assertTrue(
            {"do-not-automerge", "needs-human-merge", "do-not-auto-merge", "do-not-merge"} <= names
        )
        for spec in self.mod.STANDARD_LABELS:
            self.assertRegex(spec["color"], r"^[0-9A-F]{6}$")

    def test_redirect_and_upstream_text_stay_out_of_the_result(self):
        token = "hold-label-test-token"
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["authorization"] = self.headers.get("Authorization")
                body = ('{"message":"' + token + '"}').encode()
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/elsewhere")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        original = self.mod._API
        self.mod._API = f"http://{host}:{port}"
        try:
            with self.assertRaises(self.mod.ProvisionError) as caught:
                self.mod.ensure_hold_labels(
                    self.repo,
                    lambda method, path, payload: self.mod._github(token, method, path, payload),
                    labels=self.mod.STANDARD_LABELS[:1],
                )
        finally:
            self.mod._API = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(caught.exception.code, "label_read_failed")
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(seen.get("authorization"), "Bearer " + token)

    def test_reusable_workflow_runs_this_file_from_its_own_commit(self):
        text = WORKFLOW.read_text()
        self.assertIn("ensure-hold-labels:", text)
        self.assertIn("job.workflow_sha", text)
        self.assertIn("job.workflow_repository", text)
        self.assertIn("scripts/hold_labels/provision.py", text)
        self.assertIn("issues: write", text)


if __name__ == "__main__":
    unittest.main()
