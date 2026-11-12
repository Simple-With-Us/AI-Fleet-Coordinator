#!/usr/bin/env python3
"""Read-payload cases for board, without a live Linear.

Run:  python3 scripts/linear-board/test_read_payload.py
"""

from __future__ import annotations

import importlib.util
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOADER = SourceFileLoader("board_mod", str(HERE / "board"))
SPEC = importlib.util.spec_from_loader("board_mod", LOADER)
board = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(board)


class ReadPayloadTests(unittest.TestCase):
    def test_null_data_with_graphql_errors_raises(self):
        with self.assertRaises(board.GqlError) as caught:
            board.graphql_read_payload(
                {
                    "data": None,
                    "errors": [
                        {
                            "message": "Cannot query field",
                            "path": ["issues"],
                            "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
                        }
                    ],
                },
                200,
                {},
            )
        self.assertIn("GRAPHQL_VALIDATION_FAILED", board.gql_summary(caught.exception))

    def test_an_empty_object_raises(self):
        with self.assertRaises(board.GqlError) as caught:
            board.graphql_read_payload({"data": {}}, 200, {})
        self.assertIn("NO_DATA", board.gql_summary(caught.exception))

    def test_partial_data_keeps_the_payload_and_drops_the_message(self):
        data, errors = board.graphql_read_payload(
            {
                "data": {"viewer": {"name": "Ada"}, "projects": None},
                "errors": [
                    {
                        "message": "a private issue title",
                        "extensions": {"code": "INTERNAL"},
                        "path": ["projects"],
                    }
                ],
            },
            200,
            {},
        )
        self.assertEqual(data["viewer"]["name"], "Ada")
        self.assertEqual(errors, [{"code": "INTERNAL", "path": ["projects"]}])
        self.assertNotIn("private", str(errors))

    def test_a_braced_selection_is_the_inner_field_list(self):
        self.assertEqual(board.selected_fields("{ nodes { id title } }"), "nodes { id title }")
        self.assertEqual(board.selected_fields("nodes { id title }"), "nodes { id title }")

    def test_a_selection_that_escapes_is_refused(self):
        with self.assertRaises(board.ParityError):
            board.selected_fields('id } } mutation { issueDelete(id: "x") { success }')


if __name__ == "__main__":
    unittest.main()
