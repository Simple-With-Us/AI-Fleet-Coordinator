"""The OpenRouter HTTP transport, below the daemon:  the request it sends, how it reads every kind of
answer and failure, and the key it reads from Fleet-OPS.  Everything runs against a fake OpenRouter on
127.0.0.1;  nothing here reaches openrouter.ai, and the key a test plants is made at run time by
repeating a pattern, so no key-shaped literal is in the source.

The `openrouter` wake type is retired (owner, Nov 10, 2026:  the listener starts no model), so no seat
config names this runtime any more, and its pins went with it;  the transport and the key reader stay
for the optional decider (`[decider.consult_backup]`), which is the one caller its declaration selects.
The caller states the system message and the schema:  the wake contract and the wake result schema
were the retired wake's.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from simple_tools import adapters as A
from simple_tools import config as C
from simple_tools import service_identity as SI
from simple_tools import zulip as Z
from simple_tools.tests.fake_openrouter import FakeOpenRouter, Reply, completion

MODEL = "meta/muse-spark-1.3-contributor"
PROMPT_MARKER = "UNIQUE-PROMPT-MARKER-4471"
SYSTEM = "You answer with one JSON object and nothing else."
GOOD = {
    "action": "reply",
    "reply": "On it.  The cutover is in review.",
    "board": None,
    "owner_note": None,
    "risk": None,
    "route": None,
    "chat_target": None,
    "chat_inspect": None,
}
#: A strict schema for GOOD's keys:  every property required, nothing else allowed.
SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {key: {"type": ["string", "null"]} for key in GOOD},
        "required": list(GOOD),
        "additionalProperties": False,
    },
    separators=(",", ":"),
)


def fake_key() -> str:
    """A recognisable fake key, made at run time by repeating a pattern (no key-shaped literal)."""
    return ("FAKEORKEY" * 4)[:32]


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.key = fake_key()
        self.fake = FakeOpenRouter(self.key, completion(GOOD))
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.runner = A.OpenRouterRunner(base_url=self.fake.url, timeout=5.0)

    def call(self, prompt: str = "hello %s" % PROMPT_MARKER, model: str = MODEL, **kw: Any) -> A.RunResult:
        runner = kw.pop("runner", self.runner)
        return runner.run(model, prompt, SCHEMA, kw.pop("key", self.key), system=SYSTEM, **kw)

    # ---- the request ----------------------------------------------------------------------------
    def test_the_request_is_one_post_with_no_tools_and_the_strict_schema(self) -> None:
        result = self.call()
        self.assertTrue(result.ok, result.error)
        (seen,) = self.fake.requests()
        self.assertEqual(seen.path, "/api/v1/chat/completions")
        body = seen.body
        self.assertEqual(body["model"], MODEL)
        # The caller's system message is the system message and its prompt the user message.
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "hello %s" % PROMPT_MARKER},
            ],
        )
        # Nothing is offered to call, and nothing else that acts is switched on.
        for name in ("tools", "tool_choice", "functions", "function_call", "plugins", "web_search_options"):
            self.assertNotIn(name, body)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["provider"], {"require_parameters": True})
        # The strict json_schema is the schema the caller passed.
        response_format = body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "wake_result")
        self.assertIs(response_format["json_schema"]["strict"], True)
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema, json.loads(SCHEMA))
        self.assertLessEqual(body["temperature"], 0.3)
        self.assertEqual(body["max_tokens"], A.OPENROUTER_MAX_TOKENS)
        self.assertLessEqual(A.OPENROUTER_MAX_TOKENS, 8192)
        self.assertEqual(body["reasoning"]["effort"], "minimal", "the lowest tier the models accept")

    def test_the_key_is_sent_as_a_bearer_header_and_nowhere_else(self) -> None:
        self.call()
        (seen,) = self.fake.requests()
        self.assertEqual(seen.bearer, "Bearer " + self.key)
        self.assertNotIn(self.key.encode(), seen.raw, "the key is not in the request body")
        self.assertIn("fleet", seen.headers["x-title"].lower())
        self.assertTrue(seen.headers["content-type"].startswith("application/json"))

    def test_there_is_one_attempt_and_no_retry(self) -> None:
        self.fake.reply(Reply(status=503, body={"error": {"code": 503}}))
        result = self.call()
        self.assertEqual(result.error, "HTTP 503 (no provider available)")
        self.assertEqual(len(self.fake.requests()), 1, "a retry would spend the caller's time on a provider that said no")

    # ---- a good answer --------------------------------------------------------------------------
    def test_a_good_answer_is_parsed_and_counted(self) -> None:
        result = self.call()
        self.assertTrue(result.ok)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.exit, 0)
        self.assertEqual(result.parsed, GOOD)
        # The model that served the call is recorded (an alias resolves to a dated id), with counts only.
        self.assertEqual(result.models, ["meta/muse-spark-1.3-contributor-20260902"])
        self.assertEqual(
            result.usage,
            {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280, "reasoning_tokens": 12},
        )
        self.assertEqual(result.cost_usd, 0.0, "no dollar figure is kept (owner 2026-10-13)")
        self.assertFalse(result.limited)
        self.assertIsNone(result.refused)

    def test_a_fenced_answer_parses_like_a_muse_answer(self) -> None:
        self.fake.reply(Reply(body=completion("```json\n%s\n```" % json.dumps(GOOD))))
        self.assertEqual(self.call().parsed, GOOD)

    def test_content_parts_are_joined(self) -> None:
        text = json.dumps(GOOD)
        parts = [{"type": "text", "text": text[:20]}, {"type": "text", "text": text[20:]}]
        self.fake.reply(Reply(body=completion(parts)))
        self.assertEqual(self.call().parsed, GOOD)

    def test_any_json_answer_is_returned_and_its_shape_is_the_callers_to_check(self) -> None:
        for answer in (
            {"action": "reply"},  # keys missing
            dict(GOOD, action="nuke"),  # not in the enum
            dict(GOOD, surprise=1),  # an extra key
            dict(GOOD, reply="x" * 2000),  # over any length cap
        ):
            self.fake.reply(Reply(body=completion(answer)))
            result = self.call()
            self.assertTrue(result.ok, "the runner reads it:  the shape check is the caller's")
            self.assertEqual(result.parsed, answer)

    # ---- every failure --------------------------------------------------------------------------
    def test_each_http_error_class_is_a_fixed_phrase_and_never_the_body(self) -> None:
        phrases = {
            400: "request rejected",
            401: "key rejected",
            402: "credits exhausted",
            403: "forbidden or blocked by moderation",
            404: "no provider supports the request",
            408: "provider timed out",
            413: "request too large",
            422: "request unprocessable",
            429: "rate limited",
            500: "provider error",
            502: "provider failed",
            503: "no provider available",
            504: "provider error",
        }
        echo = "echo of the prompt %s and the key %s" % (PROMPT_MARKER, self.key)
        for status, phrase in phrases.items():
            self.fake.reply(Reply(status=status, body={"error": {"code": status, "message": echo}}))
            result = self.call()
            self.assertEqual(result.error, "HTTP %d (%s)" % (status, phrase))
            self.assertEqual(result.http_status, status)
            self.assertFalse(result.ok)
            self.assertFalse(result.limited, "a 402 is a failure reason, never a quota hold")
            self.assertIsNone(result.refused)
            everything = repr(vars(result))
            self.assertNotIn(PROMPT_MARKER, everything)
            self.assertNotIn(self.key, everything)

    def test_a_redirect_is_a_failure_and_is_never_followed_with_the_key(self) -> None:
        self.fake.reply(Reply(status=302, body="", headers={"Location": self.fake.url + "/elsewhere"}))
        result = self.call()
        self.assertEqual(result.error, "HTTP 302 (redirects are refused)")
        self.assertEqual(len(self.fake.requests()), 1, "the redirect was not followed")

    def test_a_wrong_key_is_a_401_from_the_server(self) -> None:
        result = self.call(key=("OTHERKEY" * 4)[:32])
        self.assertEqual(result.error, "HTTP 401 (key rejected)")

    def test_bodies_that_are_not_an_answer_are_each_a_fixed_phrase(self) -> None:
        cases = [
            (Reply(body="<html>gateway</html>"), "no parseable result"),
            (Reply(body=[1, 2]), "no parseable result"),
            (Reply(body={"error": {"code": 502, "message": PROMPT_MARKER}}), "provider error (code 502)"),
            (Reply(body={"error": {"message": PROMPT_MARKER}}), "provider error"),
            (Reply(body={"id": "x", "choices": []}), "no parseable result"),
            (Reply(body=completion("I will help with that.")), "no parseable result"),
            (Reply(body=completion("")), "no parseable result"),
            (Reply(body=completion(None)), "no parseable result"),
            (
                Reply(body=completion(GOOD, tool_calls=[{"id": "1", "function": {"name": "x"}}])),
                "the model returned a tool call (none were offered)",
            ),
            (Reply(body=completion(GOOD, refusal="I cannot")), "the model refused"),
            (Reply(body=completion(GOOD, finish="length")), "answer cut off at max_tokens"),
            (Reply(body=completion(GOOD, finish="content_filter")), "answer blocked by a content filter"),
            (Reply(body=completion(GOOD, finish="error")), "provider error mid-answer"),
        ]
        for reply, phrase in cases:
            self.fake.reply(reply)
            result = self.call()
            self.assertEqual(result.error, phrase, reply.body)
            self.assertIsNone(result.parsed)
            self.assertNotIn(PROMPT_MARKER, repr(vars(result)))

    def test_an_oversize_response_is_refused(self) -> None:
        self.fake.reply(Reply(body=b"x" * (A.OPENROUTER_RESPONSE_CAP + 10)))
        self.assertEqual(self.call().error, "response too large")

    def test_a_server_that_does_not_answer_times_out_and_is_cut_off(self) -> None:
        runner = A.OpenRouterRunner(base_url=self.fake.url, timeout=0.6)
        self.fake.reply(Reply(delay=4.0))
        started = time.monotonic()
        result = self.call(runner=runner)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(result.error.startswith("timed out after"), result.error)
        self.assertTrue(result.killed)
        self.assertFalse(result.ok)

    def test_a_server_that_drips_bytes_is_cut_off_at_the_overall_deadline(self) -> None:
        """Each byte lands inside any per-read timeout, so only an overall deadline ends this one."""
        runner = A.OpenRouterRunner(base_url=self.fake.url, timeout=0.8)
        self.fake.reply(Reply(body=completion(GOOD), drip=0.03))
        started = time.monotonic()
        result = self.call(runner=runner)
        self.assertLess(time.monotonic() - started, 3.0, "a per-read timeout alone would wait for every byte")
        self.assertTrue(result.error.startswith("timed out after"), result.error)

    def test_a_network_failure_is_the_error_class_only(self) -> None:
        url = self.fake.url
        self.fake.stop()  # (the cleanup stops it again, which a finished loopback server allows)
        result = A.OpenRouterRunner(base_url=url, timeout=2.0).run(
            MODEL, "p", SCHEMA, self.key, system=SYSTEM
        )
        self.assertEqual(result.error, "network: ConnectionRefusedError")
        self.assertIsNone(result.http_status)
        self.assertNotIn(self.key, repr(vars(result)))

    def test_the_url_guard_refuses_what_could_carry_the_key_elsewhere(self) -> None:
        self.assertIsNone(A.openrouter_url_problem(A.OPENROUTER_BASE_URL))
        self.assertTrue(A.OPENROUTER_BASE_URL.startswith("https://openrouter.ai/"))
        for bad in (
            "http://example.com/api/v1",
            "ftp://127.0.0.1/api/v1",
            "https://user:pw@openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1\r\nX: y",
            "https:///api/v1",
        ):
            self.assertIsNotNone(A.openrouter_url_problem(bad), bad)
            result = A.OpenRouterRunner(base_url=bad).run(MODEL, "p", SCHEMA, self.key, system=SYSTEM)
            self.assertTrue(result.error.startswith("the OpenRouter URL is refused"), result.error)
            self.assertNotIn(self.key, result.error)
        self.assertEqual(self.fake.requests(), [])

    def test_the_caller_must_state_the_system_message(self) -> None:
        # There is no default:  the wake contract it used to read went with the retired wake types.
        with self.assertRaises(TypeError):
            self.runner.run(MODEL, "p", SCHEMA, self.key)
        self.assertEqual(self.fake.requests(), [])

    def test_the_default_runner_uses_the_production_endpoint(self) -> None:
        # The base URL is a module constant, not a config key:  production builds the runner with none.
        self.assertIsNone(A.OpenRouterRunner().base_url)
        self.assertEqual(A.OPENROUTER_BASE_URL, "https://openrouter.ai/api/v1")
        self.assertNotIn("openrouter_base_url", Path(C.__file__).read_text(encoding="utf-8"))


class KeyCase(unittest.TestCase):
    PROJECT = str(uuid.uuid5(uuid.NAMESPACE_DNS, "fleet-ops-test-project"))

    def setUp(self) -> None:
        self.key = fake_key()
        self.calls: list[tuple[tuple[str, ...], str | None, str, str]] = []
        self.values: dict[str, str] = {SI.OPENROUTER_KEY_NAME: self.key}

        def fetch(names: Any, env: Any, home: Any, path: str = "/zulip") -> dict[str, str]:
            self.calls.append((tuple(names), env.get("INFISICAL_FLEET_OPS_PROJECT_ID"), str(home), path))
            return {n: self.values[n] for n in names if n in self.values}

        patch = mock.patch.object(Z, "_default_infisical_fetch", fetch)
        patch.start()
        self.addCleanup(patch.stop)
        self.env = {"INFISICAL_FLEET_OPS_PROJECT_ID": self.PROJECT, "HOME": "/home/x"}

    def test_it_reads_the_one_named_value_from_fleet_ops_wake(self) -> None:
        self.assertEqual(SI.openrouter_api_key(self.env), self.key)
        self.assertEqual(self.calls, [(("OPERATOR_OPENROUTER_API_KEY",), self.PROJECT, "/home/x", "/wake")])

    def test_the_reader_is_handed_a_path_not_a_string_for_home(self) -> None:
        """fleet-secrets' locator does `home / ".local"`, so a str home fails in production only, where
        the vault is real, and never in a test that fakes the reader.  Both callers must give a Path."""
        seen: list[Any] = []

        def fetch(names: Any, env: Any, home: Any, path: str = "/zulip") -> dict[str, str]:
            seen.append(home)
            return {SI.OPENROUTER_KEY_NAME: self.key}

        with mock.patch.object(Z, "_default_infisical_fetch", fetch):
            SI.openrouter_api_key(self.env, home="/home/x")
            SI.openrouter_api_key(self.env)
            A.OpenRouterKey(self.env, "/home/x").get()
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(isinstance(home, Path) for home in seen), seen)

    def test_the_project_id_may_come_from_the_file_a_shell_has(self) -> None:
        """`test-wake --run` runs in a shell that may lack the LaunchAgent's variable;  the Fleet-OPS reads
        everywhere else take the id from ~/.simple-tools/fleet-ops-project-id as well."""
        home = Path(tempfile.mkdtemp(prefix="fleet-listener-or-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / ".simple-tools").mkdir()
        (home / ".simple-tools" / "fleet-ops-project-id").write_text(self.PROJECT + "\n")
        self.assertEqual(SI.openrouter_api_key({"HOME": str(home)}, home=home), self.key)
        self.assertEqual(self.calls[-1][1], self.PROJECT, "the id reached the reader as its environment")
        (home / ".simple-tools" / "fleet-ops-project-id").write_text("not-a-uuid-" + self.key + "\n")
        with self.assertRaises(Z.CredentialError) as ctx:
            SI.openrouter_api_key({"HOME": str(home)}, home=home)
        self.assertNotIn(self.key, str(ctx.exception), "a malformed id is never echoed")

    def test_another_apps_openrouter_key_is_never_read(self) -> None:
        env = dict(self.env, CT_OPENROUTER_API_KEY="ct-" * 8, ST_OPENROUTER_API_KEY="st-" * 8)
        self.values.update({"CT_OPENROUTER_API_KEY": "ct-" * 8, "ST_OPENROUTER_API_KEY": "st-" * 8})
        self.assertEqual(SI.openrouter_api_key(env), self.key)
        asked = [name for call in self.calls for name in call[0]]
        self.assertEqual(asked, ["OPERATOR_OPENROUTER_API_KEY"])
        source = Path(SI.__file__).read_text(encoding="utf-8") + Path(A.__file__).read_text(encoding="utf-8")
        for name in ("CT_OPENROUTER_API_KEY", "ST_OPENROUTER_API_KEY"):
            self.assertNotIn(name, source)

    def test_every_failure_names_the_secret_and_never_a_value(self) -> None:
        def gone(*_a: Any) -> dict[str, str]:
            raise Z.CredentialError("fleet-secrets timeout")

        def boom(*_a: Any) -> dict[str, str]:
            raise RuntimeError("the value is %s" % self.key)

        cases: list[tuple[dict[str, str], Any, str]] = [
            ({"HOME": "/h"}, gone, "INFISICAL_FLEET_OPS_PROJECT_ID"),
            (dict(self.env, SIMPLE_TOOLS_INFISICAL="off"), gone, "turns Infisical off"),
            (self.env, lambda *_a: {}, "not set"),
            (self.env, lambda *_a: {SI.OPENROUTER_KEY_NAME: "${OPENROUTER}"}, "unresolved reference"),
            (self.env, lambda *_a: {SI.OPENROUTER_KEY_NAME: "two words"}, "not a single printable token"),
            (self.env, lambda *_a: {SI.OPENROUTER_KEY_NAME: "short"}, "not a single printable token"),
            (self.env, lambda *_a: {SI.OPENROUTER_KEY_NAME: "line\nbreak" + self.key}, "not a single"),
            (self.env, gone, "fleet-secrets timeout"),
            (self.env, boom, "RuntimeError"),
        ]
        for env, fetcher, expected in cases:
            with mock.patch.object(Z, "_default_infisical_fetch", fetcher):
                with self.assertRaises(Z.CredentialError) as ctx:
                    SI.openrouter_api_key(env)
            message = str(ctx.exception)
            self.assertIn(expected, message)
            if env.get("INFISICAL_FLEET_OPS_PROJECT_ID"):
                self.assertIn("OPERATOR_OPENROUTER_API_KEY", message, "it names the secret")
            self.assertNotIn(self.key, message)

    def test_an_injected_fetch_is_used_as_the_operator_reader_allows(self) -> None:
        self.assertEqual(
            SI.openrouter_api_key(self.env, fetch=lambda names: {SI.OPENROUTER_KEY_NAME: self.key}), self.key
        )
        self.assertEqual(self.calls, [], "the injected reader replaced the vault read")

    def test_quotes_around_a_synced_value_are_stripped(self) -> None:
        self.values[SI.OPENROUTER_KEY_NAME] = '"%s"' % self.key
        self.assertEqual(SI.openrouter_api_key(self.env), self.key)

    def test_the_holder_hides_the_key_in_both_forms_when_it_reads_it(self) -> None:
        hidden: list[list[str]] = []
        holder = A.OpenRouterKey(self.env, "/home/x", hide=hidden.append)
        self.assertEqual(holder.get(), self.key)
        self.assertEqual(hidden, [[self.key, "Bearer " + self.key]])
        self.assertIsNone(holder.known_unavailable())

    def test_a_failed_read_is_remembered_for_a_window_with_no_network(self) -> None:
        now = [100.0]
        holder = A.OpenRouterKey(self.env, "/home/x", clock=lambda: now[0], retry_seconds=60.0)
        del self.values[SI.OPENROUTER_KEY_NAME]
        with self.assertRaises(A.OpenRouterUnavailable) as ctx:
            holder.get()
        self.assertIn("OPERATOR_OPENROUTER_API_KEY", ctx.exception.reason)
        reads = len(self.calls)
        # Inside the window:  no read, and the router thread can ask without any.
        self.assertIn("not set", holder.known_unavailable() or "")
        with self.assertRaises(A.OpenRouterUnavailable):
            holder.get()
        self.assertEqual(len(self.calls), reads)
        # After the window the vault is asked again, and a recovered vault clears the failure.
        now[0] += 61
        self.values[SI.OPENROUTER_KEY_NAME] = self.key
        self.assertIsNone(holder.known_unavailable())
        self.assertEqual(holder.get(), self.key)
        self.assertIsNone(holder.known_unavailable())


class ParseResultCase(unittest.TestCase):
    """`parse_muse_result` reads the transport's answer text (moved from the deleted muse wake tests)."""

    def test_parse_result_is_lenient_about_a_fence(self) -> None:
        self.assertEqual(A.parse_muse_result('```json\n{"action":"none"}\n```'), {"action": "none"})
        self.assertIsNone(A.parse_muse_result("no object here"))
        self.assertIsNone(A.parse_muse_result(None))


if __name__ == "__main__":
    unittest.main()
