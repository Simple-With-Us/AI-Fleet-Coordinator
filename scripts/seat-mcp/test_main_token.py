"""A missing SEAT_MCP_TOKEN ends the process with one stderr line and exit 2, not a traceback.  No real fleet-secrets."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

from seat_mcp import __main__ as entry


class MissingTokenTests(unittest.TestCase):
    def test_exit_2_with_a_one_line_message_that_names_the_folder(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "SEAT_MCP_TOKEN"}
        env["FLEET_SECRETS_BIN"] = "/nonexistent/fleet-secrets"
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(entry, "serve") as serve,
            redirect_stderr(err),
        ):
            code = entry.main()
        self.assertEqual(code, 2)
        serve.assert_not_called()
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("SEAT_MCP_TOKEN missing in Infisical /seat-mcp", lines[0])

    def test_token_in_the_environment_starts_the_server(self) -> None:
        with (
            mock.patch.dict(os.environ, {"SEAT_MCP_TOKEN": "t"}),
            mock.patch.object(entry, "serve") as serve,
            mock.patch.object(entry, "ensure_jobs_dir"),
        ):
            self.assertIsNone(entry.main())
        serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
