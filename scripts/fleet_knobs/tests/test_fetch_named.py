"""Named knob reads use the scoped REST helper, never an auth-capable CLI fallback."""

from __future__ import annotations

from unittest import mock

from fleet_knobs import source

from ._fake import KnobTestCase, fixture_service_token

PROJECT = "11111111-2222-3333-4444-555555555555"
TOKEN = fixture_service_token()
GENERIC_TOKEN = fixture_service_token()
WRITER_TOKEN = fixture_service_token()


class FakeHelper:
    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    def fetch_project(self, project_id, path, env, *, token=None, names=None, timeout=None):
        if token != env.get(source.ENV_AI_FLEET_COORDINATOR_TOKEN):
            raise AssertionError("explicit scoped reader required")
        self.calls.append((project_id, path, dict(env), names, timeout))
        if self.error:
            raise self.error
        return self.result


class FetchNamedTests(KnobTestCase):
    def setUp(self):
        super().setUp()
        self.helper = FakeHelper(
            {"CLAUDE_ZULIP_KEY": "k", "CLAUDE_ZULIP_EMAIL": "e", "OTHER": "drop"}
        )
        self.loader = mock.patch.object(source, "HELPER_LOADER", return_value=self.helper)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def test_named_fetch_routes_exact_project_path_and_name_set_with_only_scoped_binding(self):
        os_token = source.ENV_KNOBS_TOKEN
        from os import environ

        environ[os_token] = TOKEN
        environ[source.ENV_TOKEN] = GENERIC_TOKEN
        environ["INFISICAL_WRITE_TOKEN"] = WRITER_TOKEN
        names = ("CLAUDE_ZULIP_KEY", "CLAUDE_ZULIP_EMAIL")

        got = source.fetch_named(names, project_id=PROJECT, path="/zulip")

        self.assertEqual(got, {"CLAUDE_ZULIP_KEY": "k", "CLAUDE_ZULIP_EMAIL": "e"})
        self.assertEqual(len(self.helper.calls), 1)
        project, path, child, requested, timeout = self.helper.calls[0]
        self.assertEqual((project, path, requested), (PROJECT, "/zulip", set(names)))
        self.assertGreater(timeout, 0)
        self.assertEqual(child[source.ENV_AI_FLEET_COORDINATOR_TOKEN], TOKEN)
        self.assertNotIn(source.ENV_TOKEN, child)
        self.assertNotIn("INFISICAL_WRITE_TOKEN", child)
        self.assertNotIn(source.ENV_KNOBS_TOKEN, child)

    def test_missing_malformed_or_generic_only_token_fails_before_helper(self):
        for token in (None, "not-a-token", "st.short", "st.bad token"):
            with self.subTest(token_present=token is not None):
                self.new_process()
                if token is None:
                    from os import environ

                    environ.pop(source.ENV_KNOBS_TOKEN, None)
                else:
                    from os import environ

                    environ[source.ENV_KNOBS_TOKEN] = token
                from os import environ

                environ[source.ENV_TOKEN] = TOKEN
                environ["INFISICAL_AUTOMATION_CLIENT_ID"] = "legacy-test-client"
                environ["INFISICAL_AUTOMATION_CLIENT_SECRET"] = "legacy-test-secret"

                with self.assertRaises(source.FetchFailed) as ctx:
                    source.fetch_named(("A",), project_id=PROJECT, path="/zulip")

                self.assertEqual(ctx.exception.reason, "service-token-required")
                self.assertEqual(self.helper.calls, [])

    def test_helper_failure_is_sanitized_and_never_retried(self):
        from os import environ

        environ[source.ENV_KNOBS_TOKEN] = TOKEN
        self.helper.error = RuntimeError("provider body includes secret-like sentinel")

        with self.assertRaises(source.FetchFailed) as ctx:
            source.fetch_named(("A",), project_id=PROJECT, path="/zulip")

        self.assertEqual(ctx.exception.reason, "scoped-read-failed")
        self.assertNotIn("sentinel", str(ctx.exception))
        self.assertEqual(len(self.helper.calls), 1)

    def test_unresolved_bare_reference_fails_closed_but_embedded_reference_is_data(self):
        from os import environ

        environ[source.ENV_KNOBS_TOKEN] = TOKEN
        self.helper.result = {"A": "${prod.some-folder.SENTINEL_REF}"}
        with self.assertRaises(source.FetchFailed) as ctx:
            source.fetch_named(("A",), project_id=PROJECT, path="/zulip")
        self.assertEqual(ctx.exception.reason, "unresolved-reference")
        self.assertNotIn("SENTINEL_REF", str(ctx.exception))

        self.helper.result = {"A": "prefix-${prod.some-folder.B}"}
        self.assertEqual(
            source.fetch_named(("A",), project_id=PROJECT, path="/zulip"),
            {"A": "prefix-${prod.some-folder.B}"},
        )
