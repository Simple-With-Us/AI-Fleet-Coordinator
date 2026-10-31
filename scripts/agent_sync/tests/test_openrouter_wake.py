"""The OpenRouter HTTP wake runtime, below the daemon:  the request it sends, how it reads every kind of
answer and failure, the key it reads from Fleet-OPS, the config that names it, and the pin on its request
shape.  Everything runs against a fake OpenRouter on 127.0.0.1;  nothing here reaches openrouter.ai, and
the key a test plants is made at run time by repeating a pattern, so no key-shaped literal is in the source.

The daemon-level flows (a muse failure falling back, the Operator posting the reply, `test-wake`) are in
test_openrouter_daemon.py.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import adapters as A
from agent_sync import config as C
from agent_sync import live as L
from agent_sync import service_identity as SI
from agent_sync import wakes as W
from agent_sync import zulip as Z
from agent_sync.tests.fake_openrouter import FakeOpenRouter, Reply, completion

MODEL = "meta/muse-spark-1.3-contributor"
FALLBACK_MODEL = "~deepseek/deepseek-flash-latest"
PROMPT_MARKER = "UNIQUE-PROMPT-MARKER-4471"
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
        return runner.run(model, prompt, W.schema_text(strict=True), kw.pop("key", self.key), **kw)

    # ---- the request ----------------------------------------------------------------------------
    def test_the_request_is_one_post_with_no_tools_and_the_strict_schema(self) -> None:
        result = self.call()
        self.assertTrue(result.ok, result.error)
        (seen,) = self.fake.requests()
        self.assertEqual(seen.path, "/api/v1/chat/completions")
        body = seen.body
        self.assertEqual(body["model"], MODEL)
        # The contract is the system message and the daemon's prompt is the user message.
        with open(W.CONTRACT_PATH, encoding="utf-8") as fh:
            contract = fh.read().rstrip()
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": contract},
                {"role": "user", "content": "hello %s" % PROMPT_MARKER},
            ],
        )
        # Nothing is offered to call, and nothing else that acts is switched on.
        for name in ("tools", "tool_choice", "functions", "function_call", "plugins", "web_search_options"):
            self.assertNotIn(name, body)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["provider"], {"require_parameters": True})
        # The strict json_schema is the SAME strict schema the muse runtime sends.
        response_format = body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "wake_result")
        self.assertIs(response_format["json_schema"]["strict"], True)
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema, json.loads(W.schema_text(strict=True)))
        self.assertEqual(schema["required"], list(schema["properties"]), "strict:  every key is required")
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
        self.assertEqual(len(self.fake.requests()), 1, "a retry would spend the worker on a provider that said no")

    # ---- a good answer --------------------------------------------------------------------------
    def test_a_good_answer_is_parsed_and_counted_and_passes_the_validator(self) -> None:
        result = self.call()
        self.assertTrue(result.ok)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.exit, 0)
        self.assertEqual(result.parsed, GOOD)
        obj, why = W.validate(result.parsed)
        self.assertIsNone(why)
        self.assertEqual(obj["action"], "reply")
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

    def test_a_schema_invalid_answer_reaches_the_same_validator_and_is_refused_there(self) -> None:
        for answer in (
            {"action": "reply"},  # keys missing
            dict(GOOD, action="nuke"),  # not in the enum
            dict(GOOD, surprise=1),  # an extra key
            dict(GOOD, reply="x" * 2000),  # over the length cap
            dict(GOOD, action="board", board=None),  # a board action with no board
        ):
            self.fake.reply(Reply(body=completion(answer)))
            result = self.call()
            self.assertTrue(result.ok, "the runner reads it:  the shape check is the validator's")
            obj, why = W.validate(result.parsed)
            self.assertIsNotNone(why, answer)
            self.assertEqual(obj["action"], "none")

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
            MODEL, "p", W.schema_text(strict=True), self.key
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
            result = A.OpenRouterRunner(base_url=bad).run(MODEL, "p", W.schema_text(strict=True), self.key)
            self.assertTrue(result.error.startswith("the OpenRouter URL is refused"), result.error)
            self.assertNotIn(self.key, result.error)
        self.assertEqual(self.fake.requests(), [])

    def test_a_missing_contract_fails_before_any_call(self) -> None:
        with mock.patch.object(A, "CONTRACT_PATH", "/nonexistent/wake-contract.md"):
            result = self.call()
        self.assertEqual(result.error, "wake contract unavailable")
        self.assertEqual(self.fake.requests(), [])

    def test_the_default_runner_uses_the_production_endpoint(self) -> None:
        # The base URL is a module constant, not a config key:  the daemon builds the runner with none.
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
        everywhere else take the id from ~/.agent-sync/fleet-ops-project-id as well."""
        home = Path(tempfile.mkdtemp(prefix="fleet-listener-or-home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / ".agent-sync").mkdir()
        (home / ".agent-sync" / "fleet-ops-project-id").write_text(self.PROJECT + "\n")
        self.assertEqual(SI.openrouter_api_key({"HOME": str(home)}, home=home), self.key)
        self.assertEqual(self.calls[-1][1], self.PROJECT, "the id reached the reader as its environment")
        (home / ".agent-sync" / "fleet-ops-project-id").write_text("not-a-uuid-" + self.key + "\n")
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
            (dict(self.env, AGENT_SYNC_INFISICAL="off"), gone, "turns Infisical off"),
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


class ConfigCase(unittest.TestCase):
    def seat(self, **section: Any) -> C.Config:
        return C.from_dict(
            {"daemon": {"owner_user_id": 12}, "seat": {"MM": dict({"bot": "MiniMax"}, **section)}}
        )

    def test_the_wake_type_and_the_fallback_are_known(self) -> None:
        self.assertIn("openrouter", C.KNOWN_WAKES)
        self.assertIn("openrouter", C.WORKER_WAKES)
        self.assertEqual(C.KNOWN_FALLBACKS, ("mcode", "openrouter"))
        self.assertEqual(C.OPENROUTER_DEFAULT_MODEL, MODEL)
        self.assertEqual(C.OPENROUTER_FALLBACK_MODEL, FALLBACK_MODEL)

    def test_the_primary_defaults_to_the_muse_contributor_tier_through_openrouter(self) -> None:
        cfg = self.seat(wake="openrouter")
        self.assertEqual(cfg.errors, [])
        seat = cfg.seats["MM"]
        self.assertEqual((seat.wake, seat.model, seat.fallback), ("openrouter", MODEL, None))
        self.assertEqual(seat.wake_max_usd, 0.0)
        self.assertTrue(seat.held_back["catch_up"])

    def test_the_mac_snippets_from_the_docs_load_clean(self) -> None:
        both = self.seat(
            wake="openrouter", model=MODEL, fallback="openrouter", fallback_model=FALLBACK_MODEL
        )
        self.assertEqual(both.errors, [])
        self.assertEqual(
            (both.seats["MM"].wake, both.seats["MM"].fallback, both.seats["MM"].fallback_model),
            ("openrouter", "openrouter", FALLBACK_MODEL),
        )
        keep_muse = self.seat(wake="muse", fallback="openrouter")
        self.assertEqual(keep_muse.errors, [])
        self.assertEqual(keep_muse.seats["MM"].fallback_model, FALLBACK_MODEL, "the fallback default")
        claude = self.seat(wake="claude", fallback="openrouter")
        self.assertEqual(claude.errors, [])

    def test_good_slugs_pass_and_bad_ones_are_refused_in_the_style_of_the_other_keys(self) -> None:
        for good in (
            MODEL,
            FALLBACK_MODEL,
            "deepseek/deepseek-v4-flash",
            "Qwen/Qwen3-8B",
            "~anthropic/claude-sonnet-latest",
        ):
            self.assertEqual(self.seat(wake="openrouter", model=good).errors, [], good)
            self.assertEqual(self.seat(wake="muse", fallback="openrouter", fallback_model=good).errors, [], good)
        for bad in (
            "muse",
            "muse spark",
            "a/b/c",
            "x:online/y",
            "meta/muse:nitro",
            "~/x",
            "meta/",
            "/x",
            "~~a/b",
            " meta/x",
            "meta/x\n",
            "meta/" + "x" * 90,
            "$(id)/x",
            5,
        ):
            wake = self.seat(wake="openrouter", model=bad)
            self.assertTrue(any("seat.MM.model must be an OpenRouter model" in e for e in wake.errors), bad)
            self.assertEqual(wake.seats["MM"].wake, "inbox", "fail soft:  the seat still captures")
            fb = self.seat(wake="muse", fallback="openrouter", fallback_model=bad)
            self.assertTrue(any("seat.MM.fallback_model must be an OpenRouter model" in e for e in fb.errors), bad)
            self.assertEqual(fb.seats["MM"].wake, "inbox")

    def test_a_muse_seat_still_takes_a_cli_alias_for_its_model_and_not_a_slug(self) -> None:
        self.assertEqual(self.seat(wake="muse", model="muse-spark-1.3-contributor").errors, [])
        self.assertTrue(self.seat(wake="muse", model=MODEL).errors)

    def test_the_refused_combinations(self) -> None:
        refused = {
            # The flag stays for mcode only:  an OpenRouter call has no tools to acknowledge.
            "uncontained": dict(wake="muse", fallback="openrouter", fallback_uncontained_ok=True),
            "uncontained, false": dict(wake="muse", fallback="openrouter", fallback_uncontained_ok=False),
            "fallback_model without the fallback": dict(wake="muse", fallback_model=FALLBACK_MODEL),
            "fallback_model for mcode": dict(wake="muse", fallback="mcode", fallback_model=FALLBACK_MODEL),
            "the same model twice": dict(wake="openrouter", fallback="openrouter", fallback_model=MODEL),
            "an inbox seat": dict(wake="inbox", fallback="openrouter"),
            "an http seat": dict(
                wake="http", fallback="openrouter", routine={"url_env": "A_URL", "key_env": "A_KEY"}
            ),
            "mcode behind openrouter": dict(wake="openrouter", fallback="mcode"),
            "a provider on an openrouter wake": dict(wake="openrouter", provider="OpenRouter"),
            "a muse path on an openrouter wake": dict(wake="openrouter", muse="/usr/bin/muse"),
            "an unknown fallback": dict(wake="muse", fallback="openrouters"),
        }
        for label, section in refused.items():
            cfg = self.seat(**section)
            self.assertTrue(cfg.errors, label)
            self.assertEqual(cfg.seats["MM"].wake, "inbox", label)
            self.assertIsNone(cfg.seats["MM"].fallback, label)
            self.assertTrue(any("seat.MM." in e for e in cfg.errors), label)

    def test_the_uncontained_flag_is_refused_with_the_reason_and_still_works_for_mcode(self) -> None:
        cfg = self.seat(wake="muse", fallback="openrouter", fallback_uncontained_ok=True)
        self.assertTrue(any("nothing to acknowledge" in e for e in cfg.errors), cfg.errors)
        mcode = self.seat(wake="muse", fallback="mcode", fallback_uncontained_ok=True)
        self.assertEqual(mcode.errors, [])
        self.assertTrue(mcode.seats["MM"].fallback_uncontained_ok)

    def test_a_wake_can_back_itself_with_openrouter_but_no_other_runtime_can(self) -> None:
        self.assertEqual(self.seat(wake="openrouter", fallback="openrouter").errors, [])
        for wake in ("muse", "claude"):
            self.assertTrue(self.seat(wake=wake, fallback=wake).errors, wake)

    def test_no_base_url_key_exists(self) -> None:
        cfg = self.seat(wake="openrouter", openrouter_base_url="https://evil.example/api/v1")
        # An unknown key is not an error in this file's style, and it is never read:  the URL is a constant.
        self.assertEqual(cfg.seats["MM"].wake, "openrouter")
        self.assertFalse(hasattr(cfg.seats["MM"], "openrouter_base_url"))
        self.assertFalse(hasattr(cfg.seats["MM"], "base_url"))


class PinCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fleet-listener-or-pin-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = L.SeatPaths(self.tmp, "MM")
        L.private_dir(self.paths.seat_dir)
        self.now = time.time()

    def pin(self, model: str = MODEL, role: str = A.OPENROUTER_ROLE_PRIMARY) -> None:
        A.record_openrouter_candidate(self.paths, model, self.now, role)
        self.assertIsNone(A.openrouter_pin_candidate(self.paths, model, self.now + 5, role))

    def state(self, model: str = MODEL, role: str = A.OPENROUTER_ROLE_PRIMARY) -> dict[str, Any]:
        return A.openrouter_pin_state(self.paths, model, role)

    def test_the_pin_files_are_named_per_role(self) -> None:
        self.assertTrue(self.paths.wake_pin_openrouter.endswith("wake-pin-openrouter.json"))
        self.assertTrue(
            self.paths.wake_pin_candidate_openrouter.endswith("wake-pin-candidate-openrouter.json")
        )
        self.assertTrue(
            self.paths.wake_pin_openrouter_fallback.endswith("wake-pin-openrouter-fallback.json")
        )
        self.assertTrue(
            self.paths.wake_pin_candidate_openrouter_fallback.endswith(
                "wake-pin-candidate-openrouter-fallback.json"
            )
        )

    def test_never_verified_holds_and_says_why(self) -> None:
        state = self.state()
        self.assertEqual((state["pinned"], state["why"]), (False, "not_pinned"))
        self.assertFalse(A.openrouter_pinned(self.paths, MODEL))

    def test_pinned_only_after_a_person_pins_a_passing_candidate(self) -> None:
        A.record_openrouter_candidate(self.paths, MODEL, self.now)
        self.assertFalse(self.state()["pinned"], "a passing test alone never pins")
        self.assertIsNone(A.openrouter_pin_candidate(self.paths, MODEL, self.now + 5))
        self.assertTrue(self.state()["pinned"])
        self.assertFalse(os.path.exists(self.paths.wake_pin_candidate_openrouter), "the candidate is spent")
        pin = L.read_json(self.paths.wake_pin_openrouter)
        self.assertEqual((pin["model"], pin["role"]), (MODEL, "primary"))
        self.assertEqual(pin["endpoint"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertRegex(pin["fingerprint"], r"^[0-9a-f]{64}$")

    def test_a_pin_holds_no_credential(self) -> None:
        self.pin()
        text = Path(self.paths.wake_pin_openrouter).read_text(encoding="utf-8")
        for needle in (fake_key(), "Bearer", "OPERATOR_OPENROUTER_API_KEY", "Authorization"):
            self.assertNotIn(needle, text)

    def test_a_pin_for_one_role_never_stands_in_for_the_other(self) -> None:
        self.pin(MODEL, A.OPENROUTER_ROLE_PRIMARY)
        self.assertTrue(self.state(MODEL, A.OPENROUTER_ROLE_PRIMARY)["pinned"])
        self.assertEqual(self.state(MODEL, A.OPENROUTER_ROLE_FALLBACK)["why"], "not_pinned")
        self.pin(FALLBACK_MODEL, A.OPENROUTER_ROLE_FALLBACK)
        self.assertTrue(self.state(FALLBACK_MODEL, A.OPENROUTER_ROLE_FALLBACK)["pinned"])
        # Copying one role's pin over the other is stale:  the role is in the fingerprint.
        shutil.copy(self.paths.wake_pin_openrouter, self.paths.wake_pin_openrouter_fallback)
        self.assertEqual(self.state(MODEL, A.OPENROUTER_ROLE_FALLBACK)["why"], "pin_stale")

    def test_a_changed_model_is_stale_and_not_self_healed(self) -> None:
        self.pin()
        state = self.state(FALLBACK_MODEL)
        self.assertEqual((state["pinned"], state["why"]), (False, "pin_stale"))
        self.assertEqual(state["pinned_model"], MODEL)
        self.assertEqual(self.state(MODEL)["pinned"], True, "the original model is still pinned")
        self.assertEqual(L.read_json(self.paths.wake_pin_openrouter)["model"], MODEL, "nothing re-pinned")

    def test_a_pin_with_no_fingerprint_verified_nothing_about_this_request(self) -> None:
        L.write_json(self.paths.wake_pin_openrouter, {"model": MODEL, "at": self.now})
        self.assertEqual(self.state()["why"], "pin_unverified")

    def test_the_fingerprint_covers_the_model_the_role_and_the_policy(self) -> None:
        base = A.openrouter_wake_fingerprint(MODEL)
        self.assertEqual(base, A.openrouter_wake_fingerprint(MODEL), "stable")
        self.assertNotEqual(base, A.openrouter_wake_fingerprint(FALLBACK_MODEL))
        self.assertNotEqual(base, A.openrouter_wake_fingerprint(MODEL, A.OPENROUTER_ROLE_FALLBACK))
        self.pin()
        with mock.patch.object(A, "OPENROUTER_WAKE_POLICY_VERSION", "openrouter-wake-v2"):
            self.assertNotEqual(base, A.openrouter_wake_fingerprint(MODEL))
            self.assertEqual(self.state()["why"], "pin_stale")

    def test_the_fingerprint_covers_every_part_of_the_request_shape(self) -> None:
        base = A.openrouter_wake_fingerprint(MODEL)
        self.pin()
        changes = {
            "max_tokens": ("OPENROUTER_MAX_TOKENS", A.OPENROUTER_MAX_TOKENS + 1),
            "temperature": ("OPENROUTER_TEMPERATURE", 0.9),
            "reasoning": ("OPENROUTER_REASONING", {"effort": "high"}),
            "schema name": ("OPENROUTER_SCHEMA_NAME", "other_name"),
            "endpoint host": ("OPENROUTER_BASE_URL", "https://other.example/api/v1"),
            "endpoint path": ("OPENROUTER_PATH", "/responses"),
            "timeout": ("OPENROUTER_TIMEOUT", A.OPENROUTER_TIMEOUT + 1),
            "response cap": ("OPENROUTER_RESPONSE_CAP", A.OPENROUTER_RESPONSE_CAP + 1),
        }
        for label, (name, value) in changes.items():
            with mock.patch.object(A, name, value):
                self.assertNotEqual(base, A.openrouter_wake_fingerprint(MODEL), label)
                self.assertEqual(self.state()["why"], "pin_stale", label)
            self.assertTrue(self.state()["pinned"], "restored:  %s" % label)

    def test_the_fingerprint_covers_the_body_template_itself(self) -> None:
        base = A.openrouter_wake_fingerprint(MODEL)
        real = A.openrouter_request_body

        def with_a_tool(model: str, system: str, user: str, schema: str) -> dict[str, Any]:
            body = real(model, system, user, schema)
            body["tools"] = [{"type": "function", "function": {"name": "x"}}]
            return body

        def without_require_parameters(model: str, system: str, user: str, schema: str) -> dict[str, Any]:
            body = real(model, system, user, schema)
            body["provider"] = {}
            return body

        for variant in (with_a_tool, without_require_parameters):
            with mock.patch.object(A, "openrouter_request_body", variant):
                self.assertNotEqual(base, A.openrouter_wake_fingerprint(MODEL), variant.__name__)

    def test_the_fingerprint_covers_the_contract_and_the_schema(self) -> None:
        base = A.openrouter_wake_fingerprint(MODEL)
        self.pin()
        contract = Path(self.tmp) / "wake-contract.md"
        contract.write_text(Path(W.CONTRACT_PATH).read_text(encoding="utf-8") + "\nAlso obey this.\n")
        with mock.patch.object(A, "CONTRACT_PATH", str(contract)):
            self.assertNotEqual(base, A.openrouter_wake_fingerprint(MODEL))
            self.assertEqual(self.state()["why"], "pin_stale")
        schema = json.loads(Path(W.SCHEMA_PATH).read_text(encoding="utf-8"))
        schema["properties"]["risk"] = {"enum": ["low", "high", None]}
        schema_file = Path(self.tmp) / "wake-schema.json"
        schema_file.write_text(json.dumps(schema))
        with mock.patch.object(W, "SCHEMA_PATH", str(schema_file)):
            self.assertNotEqual(base, A.openrouter_wake_fingerprint(MODEL))
            self.assertEqual(self.state()["why"], "pin_stale")
        self.assertTrue(self.state()["pinned"])

    def test_an_unreadable_contract_is_unverifiable_and_not_a_changed_pin(self) -> None:
        self.pin()
        A.record_openrouter_candidate(self.paths, MODEL, self.now)  # a fresh pass, made while it was readable
        with mock.patch.object(A, "CONTRACT_PATH", "/nonexistent/wake-contract.md"):
            self.assertIsNone(A.openrouter_wake_fingerprint(MODEL))
            state = self.state()
            self.assertEqual(state["why"], "pin_unreadable")
            entry = A.openrouter_pin_red_entry(state, "MM", A.OPENROUTER_ROLE_PRIMARY)
            self.assertIn("unverifiable", entry)
            self.assertNotIn("; run restore", entry, "an unreadable contract is not fixed by re-pinning")
            self.assertIn("restore the wake contract", entry)
            self.assertIn("cannot be verified", entry)
            problem = A.openrouter_pin_candidate(self.paths, MODEL, self.now + 5)
            self.assertIn("unavailable", problem or "")

    def test_the_candidate_must_be_fresh_for_this_model_and_this_request(self) -> None:
        self.assertIn("no passing test-wake --run", A.openrouter_pin_candidate(self.paths, MODEL, self.now))
        A.record_openrouter_candidate(self.paths, MODEL, self.now)
        self.assertIn(
            "over a day old", A.openrouter_pin_candidate(self.paths, MODEL, self.now + 2 * 86400) or ""
        )
        self.assertIn(
            "over a day old",
            A.openrouter_pin_candidate(self.paths, MODEL, self.now - 10) or "",
            "a candidate dated in the future is refused too",
        )
        self.assertIn(
            "model changed since the test",
            A.openrouter_pin_candidate(self.paths, FALLBACK_MODEL, self.now + 5) or "",
        )
        with mock.patch.object(A, "OPENROUTER_MAX_TOKENS", A.OPENROUTER_MAX_TOKENS + 1):
            self.assertIn(
                "request changed since the test",
                A.openrouter_pin_candidate(self.paths, MODEL, self.now + 5) or "",
            )
        self.assertFalse(os.path.exists(self.paths.wake_pin_openrouter), "none of the refusals pinned")
        self.assertIsNone(A.openrouter_pin_candidate(self.paths, MODEL, self.now + 5))

    def test_a_candidate_for_the_other_role_cannot_be_pinned_here(self) -> None:
        A.record_openrouter_candidate(self.paths, MODEL, self.now, A.OPENROUTER_ROLE_PRIMARY)
        self.assertIn(
            "no passing test-wake --run",
            A.openrouter_pin_candidate(self.paths, MODEL, self.now + 5, A.OPENROUTER_ROLE_FALLBACK) or "",
        )

    def test_the_remediation_names_the_runtime_of_the_role(self) -> None:
        stale = {"why": "pin_stale", "pinned_model": MODEL, "model": MODEL}
        primary = A.openrouter_pin_red_entry(stale, "MM", A.OPENROUTER_ROLE_PRIMARY)
        fallback = A.openrouter_pin_red_entry(stale, "MM", A.OPENROUTER_ROLE_FALLBACK)
        self.assertIn("test-wake --seat MM --runtime openrouter --run", primary)
        self.assertIn("test-wake --seat MM --runtime openrouter --pin", primary)
        self.assertTrue(primary.startswith("wake pin is stale:"))
        self.assertIn("test-wake --seat MM --runtime fallback --run", fallback)
        self.assertIn("test-wake --seat MM --runtime fallback --pin", fallback)
        self.assertTrue(fallback.startswith("fallback pin is stale:"))
        moved = A.openrouter_pin_red_entry(
            {"why": "pin_stale", "pinned_model": MODEL, "model": FALLBACK_MODEL}, "MM", "primary"
        )
        self.assertIn("the OpenRouter model is %s (pinned %s)" % (FALLBACK_MODEL, MODEL), moved)

    def test_openrouter_is_a_never_capped_provider(self) -> None:
        self.assertIn("openrouter", A.QUOTA_NEVER_CAP_PROVIDERS)
        self.assertIsNone(A.quota_block("OpenRouter", 1.0, "/nonexistent"))
        self.assertIsNone(A.quota_red("OpenRouter", 1.0, "/nonexistent"))


if __name__ == "__main__":
    unittest.main()
