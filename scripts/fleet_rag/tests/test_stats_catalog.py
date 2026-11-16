"""Compatibility stats must include the default Linear corpus source."""
import ast
import pathlib
import unittest


class StatsCatalog(unittest.TestCase):
    def test_linear_in_actual_stats_source_catalog(self):
        script = pathlib.Path(__file__).resolve().parents[2] / "fleet-rag.py"
        tree = ast.parse(script.read_text())
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "KNOWN_SOURCES" for t in node.targets)]
        self.assertEqual(len(assignments), 1)
        self.assertIn("linear", ast.literal_eval(assignments[0].value))
