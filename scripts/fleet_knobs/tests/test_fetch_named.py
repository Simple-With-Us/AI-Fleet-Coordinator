"""source.fetch_named:  the CLI is asked for just the named secrets of one folder, prod only (OPS-818)."""

from __future__ import annotations

from fleet_knobs import source

from ._fake import UNRELATED, KnobTestCase

PROJECT = "11111111-2222-3333-4444-555555555555"


class FetchNamedTests(KnobTestCase):
    def test_asks_the_cli_for_only_the_named_secrets_in_prod(self):
        names = ("CLAUDE_ZULIP_KEY", "CLAUDE_ZULIP_EMAIL")
        self.fake.set(
            shape="api",
            secrets={
                "CLAUDE_ZULIP_KEY": "k",
                "CLAUDE_ZULIP_EMAIL": "e",
                "CODEX_ZULIP_KEY": UNRELATED,
            },
        )
        got = source.fetch_named(names, project_id=PROJECT, path="/zulip")
        self.assertEqual(
            got, {"CLAUDE_ZULIP_KEY": "k", "CLAUDE_ZULIP_EMAIL": "e"}
        )  # the unrelated one is dropped
        (argv,) = self.fake.calls()
        self.assertEqual(argv[:4], ["secrets", "get", "CLAUDE_ZULIP_EMAIL", "CLAUDE_ZULIP_KEY"])
        for flag in ("--env=prod", f"--projectId={PROJECT}", "--path=/zulip", "--output=json"):
            self.assertIn(flag, argv)
        self.assertNotIn("export", argv)

    def test_failure_is_a_reason_token_never_output(self):
        self.fake.set(mode="fail", leak="sentinel-leak")
        with self.assertRaises(source.FetchFailed) as ctx:
            source.fetch_named(("A",), project_id=PROJECT, path="/zulip")
        self.assertEqual(ctx.exception.reason, "exit-1")
        self.assertNotIn("sentinel-leak", str(ctx.exception))

    def test_a_cli_that_is_not_installed_is_cli_missing(self):
        self.fake.bin_dir.joinpath("infisical").unlink()
        with self.assertRaises(source.FetchFailed) as ctx:
            source.fetch_named(("A",), project_id=PROJECT, path="/zulip")
        self.assertEqual(ctx.exception.reason, "cli-missing")

    def test_garbage_output_is_bad_output(self):
        self.fake.set(mode="garbage")
        with self.assertRaises(source.FetchFailed) as ctx:
            source.fetch_named(("A",), project_id=PROJECT, path="/zulip")
        self.assertEqual(ctx.exception.reason, "bad-output")
