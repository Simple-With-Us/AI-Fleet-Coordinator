"""A missing SEAT_MCP_TOKEN ends the process with one stderr line and exit 2, not a traceback.  No real reader."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest import mock

from seat_mcp import __main__ as entry
from seat_mcp import auth


class MissingTokenTests(unittest.TestCase):
    def test_exit_2_with_a_one_line_message_that_names_the_folder(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "SEAT_MCP_TOKEN"}
        env["INFISICAL_SCOPED_READER"] = "/nonexistent/infisical-scoped.py"
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(entry, "serve") as serve,
            mock.patch("seat_mcp.auth.subprocess.run") as reader,
            redirect_stderr(err),
        ):
            reader.return_value.returncode = 2
            code = entry.main()
        self.assertEqual(code, 2)
        serve.assert_not_called()
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("SEAT_MCP_TOKEN missing in Infisical /seat-mcp", lines[0])
        self.assertIn("seat-mcp-ro", lines[0])
        # The reader runs under the isolated interpreter with an explicit,
        # bounded argument list:  the token is never in argv.
        argv = reader.call_args.args[0]
        self.assertEqual(argv[:2], [auth.sys.executable, "-I"])
        self.assertEqual(argv[2], env["INFISICAL_SCOPED_READER"])
        self.assertEqual(
            argv[3:],
            [
                "get",
                "SEAT_MCP_TOKEN",
                "--folder",
                "/seat-mcp",
                "--project",
                "fleet-ops",
                "--account",
                "seat-mcp-ro",
            ],
        )

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
