"""build():  parts, isolation of a crashing part, redaction, the document shape."""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from fleet_facts import gather as G
from fleet_facts import safety
from fleet_facts.runner import failed, ok

from .fakes import FakeRunner, out

NOW = datetime(2026, 10, 11, 14, 30, 0, tzinfo=timezone.utc)


class Parts(unittest.TestCase):
    def test_default_is_every_part_and_order_is_kept(self):
        self.assertEqual(G.normalize_parts(None), list(G.PARTS))
        self.assertEqual(G.normalize_parts(["lanes,github", "github", "backup"]), ["lanes", "github", "backup"])
        self.assertEqual(set(G.PARTS), {"github", "linear", "lanes", "autopush", "backup", "asks", "report", "needs_owner", "consult"})

    def test_unknown_part_is_refused(self):
        with self.assertRaises(ValueError):
            G.normalize_parts(["github", "everything"])

    def test_mac_parts_are_the_local_ones(self):
        self.assertEqual(set(G.MAC_PARTS), set(G.PARTS) - {"github", "linear"})


class Build(unittest.TestCase):
    def build(self, parts, **kw):
        return G.build(parts, run=FakeRunner(lambda a: out("{}")), now=NOW, sleep=lambda s: None, **kw)

    def test_no_mac_marks_every_mac_part_absent_and_runs_none(self):
        runner = FakeRunner(lambda a: out("{}"))
        doc = G.build(list(G.MAC_PARTS), no_mac=True, run=runner, now=NOW)
        self.assertEqual(runner.calls, [])
        self.assertTrue(doc["no_mac"])
        for part in G.MAC_PARTS:
            self.assertEqual(doc["sources"][part], {"status": "absent", "reason": "--no-mac"}, part)
            self.assertNotIn(part, doc)

    def test_document_shape(self):
        doc = self.build(["lanes"], no_mac=True)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["tool"], "fleet-facts")
        self.assertEqual(doc["generated_at"], "2026-10-11T14:30:00Z")
        self.assertEqual(doc["parts"], ["lanes"])
        self.assertEqual(doc["redacted"], [])
        self.assertTrue(doc["host"])

    def test_a_crashing_part_is_failed_and_the_rest_still_run(self):
        with mock.patch.object(G.github_prs, "gather", side_effect=RuntimeError("boom")), \
             mock.patch.object(G.board, "gather", return_value=ok({"claims": []})):
            doc = self.build(["github", "linear"])
        self.assertEqual(doc["sources"]["github"]["status"], "failed")
        self.assertIn("RuntimeError", doc["sources"]["github"]["reason"])
        self.assertNotIn("boom", doc["sources"]["github"]["reason"])
        self.assertEqual(doc["sources"]["linear"]["status"], "ok")
        self.assertEqual(doc["linear"], {"claims": []})

    def test_a_failed_part_keeps_its_partial_data(self):
        with mock.patch.object(G.github_prs, "gather", return_value=failed("incomplete", {"prs": [1]})):
            doc = self.build(["github"])
        self.assertEqual(doc["sources"]["github"], {"status": "failed", "reason": "incomplete"})
        self.assertEqual(doc["github"], {"prs": [1]})

    def test_missing_helpers_stop_the_run(self):
        with mock.patch.object(G.github_prs, "gather", side_effect=safety.HelpersMissing("gone")):
            with self.assertRaises(safety.HelpersMissing):
                self.build(["github"])

    def test_secret_shaped_text_is_withheld_from_the_document(self):
        token = "ghp_" + "Zy9X8w7V" * 5
        data = {"prs": [{"title": "leaks " + token}]}
        with mock.patch.object(G.github_prs, "gather", return_value=ok(data)):
            doc = self.build(["github"])
        self.assertNotIn(token, json.dumps(doc))
        self.assertEqual(doc["redacted"], ["$.github.prs[0].title"])


if __name__ == "__main__":
    unittest.main()
