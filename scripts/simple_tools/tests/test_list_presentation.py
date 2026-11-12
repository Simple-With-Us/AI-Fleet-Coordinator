import unittest

from simple_tools.mcp import list_presentation as LP


class ListPresentationTests(unittest.TestCase):
    def test_split_full_defaults_false(self):
        work, full = LP.split_full_arg({"limit": 5, "full": False})
        self.assertFalse(full)
        self.assertNotIn("full", work)

    def test_effective_limit_compact_default(self):
        self.assertEqual(LP.effective_limit({}, schema_default=20, full=False), LP.COMPACT_DEFAULT_LIMIT)
        self.assertEqual(LP.effective_limit({"limit": 40}, schema_default=20, full=False), 40)
        self.assertEqual(LP.effective_limit({}, schema_default=20, full=True), 20)

    def test_truncate_text_marks_tail(self):
        text, cut = LP.truncate_text("x" * 400, 280)
        self.assertTrue(cut)
        self.assertLessEqual(len(text), 283)

    def test_compact_linear_graphql_slices_nodes_and_bodies(self):
        raw = {
            "issues": {
                "nodes": [
                    {"identifier": "A", "description": "d" * 400},
                    {"identifier": "B", "description": "short"},
                ]
            }
        }
        compacted, meta = LP.compact_linear_graphql(raw, top_n=1)
        self.assertEqual(len(compacted["issues"]["nodes"]), 1)
        self.assertTrue(meta["truncated"])
        self.assertLess(len(compacted["issues"]["nodes"][0]["description"]), 400)

    def test_compact_notes_list(self):
        notes = [{"title": f"n{i}", "id": str(i)} for i in range(25)]
        out = LP.compact_notes_list_result({"notes": notes, "total": 25})
        self.assertEqual(len(out["notes"]), LP.COMPACT_DEFAULT_LIMIT)
        self.assertTrue(out["compact_truncated"])


if __name__ == "__main__":
    unittest.main()
