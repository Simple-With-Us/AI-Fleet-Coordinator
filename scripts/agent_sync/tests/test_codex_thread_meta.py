"""CODEX opt-in attribution against fake Zulip and native-shaped MCP envelopes only."""

import argparse
import hashlib
import io
import json
from unittest import mock

from agent_sync import cli
from agent_sync import zulip as Z
from agent_sync.mcp import stdio as S
from agent_sync.mcp import tools as T
from agent_sync.state import State
from agent_sync.tests.mcp_harness import McpHarness, zulip_shaped

A = "019f0000-0000-7000-8000-000000000001"
B = "019f0000-0000-7000-8000-000000000002"


class CodexThreadMetaTests(McpHarness):
    def setUp(self):
        super().setUp()
        key = zulip_shaped()
        self.extra_secrets.append(key)
        self.fake.add_bot("codex-bot@zulip.test", "Codex", key)
        self.add_infisical_bot("CODEX", email="codex-bot@zulip.test", key=key)
        self.env = self.mcp_env(AGENT_SEAT="CODEX", CLAUDE_CODE_SESSION_ID=None)

    def direct(self, session=None, source="none"):
        rt = cli.Runtime(
            env=dict(self.env),
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            home=self.home,
            sleep=self.clock.sleep,
        )
        agent = cli.Agent(rt, argparse.Namespace(as_seat="CODEX", session=session, json=True))
        return T.Tools(
            agent,
            me=agent.me(),
            process_tag=agent.tag,
            session_source=source,
            codex_thread_meta=True,
            clock=self.clock.time,
            sleep=self.clock.sleep,
        )

    def native(self, session, name="whoami", arguments=None, thread=A, **extra):
        arguments = dict(arguments or {})
        if name in T.WRITE_TOOLS:
            arguments.setdefault("identity", "CODEX")  # AFC-331:  a write names its identity
        reply = session.request(
            "tools/call",
            {"name": name, "arguments": arguments, "_meta": {"threadId": thread, **extra}},
        )
        return reply["result"]

    def test_envelope_thread_id_not_session_id_controls_resume_and_new(self):
        session = self.session(self.env, argv=("--codex-thread-meta",))
        first = self.native(session, sessionId=B)["structuredContent"]
        resumed = self.native(session, sessionId=A)["structuredContent"]
        new = self.native(session, thread=B, sessionId=A)["structuredContent"]
        self.assertEqual(first, resumed)
        self.assertEqual(first["thread_id"], A)
        self.assertEqual(first["session_source"], "codex_thread_meta_sha256")
        self.assertEqual(first["tag_kind"], "sha256_derived")
        self.assertEqual(
            first["tag_prefix"], "[CODEX·" + hashlib.sha256(A.encode()).hexdigest()[:8] + "]"
        )
        self.assertNotEqual(first["tag_prefix"], new["tag_prefix"])
        self.assertEqual(new["thread_id"], B)

    def test_whoami_results_match_the_advertised_output_schema(self):
        cases = (
            ({}, ("--codex-thread-meta",), True),
            ({"AGENT_SESSION": "owner session"}, ("--codex-thread-meta",), True),
            ({"AGENT_SESSION": "owner session"}, ("--codex-thread-meta",), False),
            ({}, ("--codex-thread-meta", "--session", "flag session"), True),
            ({}, ("--codex-thread-meta", "--session", "flag session"), False),
            ({}, (), False),
        )
        for overrides, argv, native in cases:
            with self.subTest(overrides=overrides, argv=argv, native=native):
                session = self.session({**self.env, **overrides}, argv=argv)
                advertised = session.request("tools/list")["result"]["tools"]
                schema = next(t["outputSchema"] for t in advertised if t["name"] == "whoami")
                result = self.native(session) if native else session.call("whoami")
                self.assertNotIn("isError", result)
                problem = T.schema_problem(schema, result["structuredContent"], "structuredContent")
                self.assertIsNone(problem, problem)

    def test_explicit_flag_wins_owner_env_and_claude_env_without_rewriting_bytes(self):
        owner, flag = "Owner Session Mixed CASE", "Flag Session Mixed CASE"
        env = self.mcp_env(AGENT_SEAT="CODEX", AGENT_SESSION=owner, CLAUDE_CODE_SESSION_ID=A)
        with mock.patch.object(S.CLI, "Agent", wraps=cli.Agent) as factory:
            session = self.session(env, argv=("--codex-thread-meta", "--session", flag))
            info = self.native(session)["structuredContent"]
            self.assertEqual(factory.call_args.args[1].session, flag)
            self.assertEqual(factory.call_args.args[0].env["AGENT_SESSION"], owner)
        self.assertEqual(info["session_source"], "flag")
        self.assertEqual(info["tag_kind"], "explicit")
        self.assertEqual(info["thread_id"], A)
        session = self.session(env, argv=("--codex-thread-meta",))
        info = self.native(session)["structuredContent"]
        self.assertEqual(info["session_source"], "env")
        self.assertEqual(info["tag_prefix"], "[CODEX·ownerses]")
        self.assertIsNone(session.call("whoami")["structuredContent"]["thread_id"])

    def test_automatic_missing_invalid_or_argument_only_metadata_fails_closed(self):
        session = self.session(self.env, argv=("--codex-thread-meta",))
        for bad in (None, "", "new-session", A.upper(), 123, [], {}, A + "x"):
            with self.subTest(bad=bad):
                self.assertTrue(self.native(session, thread=bad)["isError"])
        self.assertTrue(session.call("whoami")["isError"])
        self.assertTrue(session.call("whoami", {"_meta": {"threadId": A}})["isError"])
        self.assertFalse((self.state_dir / "CODEX/nosession/state.json").exists())
        self.assertEqual(self.native(session)["structuredContent"]["thread_id"], A)

    def test_mode_rejects_other_seat_and_unusable_owner_session_before_credentials(self):
        args = cli.build_parser().parse_args(["mcp", "--codex-thread-meta"])
        for env, expected in ((self.mcp_env(), 3), ({**self.env, "AGENT_SESSION": "!!!"}, 2)):
            rt = cli.Runtime(
                env=env,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                home=self.home,
            )
            before = len(self.fake.requests)
            self.assertEqual(S.run(rt, args), expected)
            self.assertEqual(len(self.fake.requests), before)

    def test_metadata_and_arguments_cannot_impersonate_a_seat(self):
        session = self.session(self.env, argv=("--codex-thread-meta",))
        info = self.native(session, seat="JET", as_seat="CLAUDE")["structuredContent"]
        self.assertEqual(info["seat"], "CODEX")
        for key in ("seat", "as", "as_seat", "rc", "no_tag"):
            self.assertTrue(self.native(session, arguments={key: "JET"})["isError"])

    def test_short_hash_collision_keeps_state_and_implicit_writes_isolated(self):
        tools, real_sha = self.direct(), T._sha

        def colliding(text):
            return "deadbeef" + "0" * 56 if text in (A, B) else real_sha(text)

        with mock.patch.object(T, "_sha", side_effect=colliding):
            replies = [
                tools.call("zulip_post", {"topic": "AFC-296", "text": "same"}, meta={"threadId": x})
                for x in (A, B)
            ]
        ids = [r["structuredContent"]["id"] for r in replies]
        self.assertNotEqual(*ids)
        for thread, own, other in ((A, ids[0], ids[1]), (B, ids[1], ids[0])):
            state = State(self.state_dir, "CODEX", "codex-thread-" + thread)
            self.assertIn(own, state.posted())
            self.assertNotIn(other, state.posted())
        self.assertEqual(tools.agent.state.path, self.state_dir / "CODEX/nosession/state.json")
        self.assertIsNone(tools.agent.tag)
        self.assertEqual(tools.idem_path, str(self.state_dir / "CODEX/mcp-idem.json"))
        self.assertEqual(tools.spacing_path, str(self.state_dir / "CODEX/mcp-write.json"))

    def test_explicit_tag_has_full_native_state_and_errors_reset_context(self):
        tools, states = self.direct("Owner-Session", "flag"), []
        base = tools.agent.state

        def broken(args):
            states.append(tools.agent.state.path)
            tools.agent.state.advance_cursor("agent-sync", "AFC-296", 9)
            raise RuntimeError("synthetic failure")

        with mock.patch.object(tools, "_tool_zulip_topics", side_effect=broken):
            for thread in (A, B):
                self.assertTrue(
                    tools.call("zulip_topics", {}, meta={"threadId": thread})["isError"]
                )
                self.assertIs(tools.agent.state, base)
                self.assertEqual(tools.agent.tag, "ownerses")
                self.assertIsNone(tools.thread_id)
                self.assertEqual(tools.session_source, "flag")
        self.assertNotEqual(*states)
        info = tools.call("whoami", {}, meta={"threadId": B})["structuredContent"]
        self.assertEqual(info["thread_id"], B)
        self.assertEqual(info["tag_kind"], "explicit")

    def test_missing_metadata_post_and_argument_session_send_no_writes(self):
        session = self.session(self.env, argv=("--codex-thread-meta",))
        before = len(self.fake.messages)
        args = {"topic": "AFC-296", "text": "must not send", "session": A}
        self.assertTrue(session.call("zulip_post", args)["isError"])
        self.assertTrue(self.native(session, "zulip_post", args, thread=None)["isError"])
        self.assertEqual(len(self.fake.messages), before)
        self.assertFalse((self.state_dir / "CODEX/nosession/state.json").exists())

    def test_explicit_idempotency_key_stays_seat_wide_across_native_chats(self):
        session = self.session(self.env, argv=("--codex-thread-meta",))
        args = {
            "topic": "AFC-296",
            "text": "one intentional write",
            "idempotency_key": "same-intent",
        }
        before = len(self.fake.messages)
        first = self.native(session, "zulip_post", args)["structuredContent"]
        second = self.native(session, "zulip_post", args, thread=B)["structuredContent"]
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.fake.messages), before + 1)

    def test_sibling_posts_remain_visible_and_resume_uses_only_own_ledger(self):
        real_sha = T._sha

        def colliding(text):
            return "deadbeef" + "0" * 56 if text in (A, B) else real_sha(text)

        for owner in (None, "same explicit owner tag"):
            with self.subTest(owner=owner), mock.patch.object(T, "_sha", side_effect=colliding):
                tools = self.direct(owner, "flag" if owner else "none")
                topic = "owner" if owner else "derived"
                args = {"topic": topic, "text": "A authored this"}
                own = tools.call("zulip_post", args, meta={"threadId": A})["structuredContent"][
                    "id"
                ]
                read = {"topic": topic}
                self.assertEqual(
                    tools.call("zulip_read_topic", read, meta={"threadId": B})["structuredContent"][
                        "ids"
                    ],
                    [own],
                )
                self.assertEqual(
                    tools.call("zulip_read_topic", read, meta={"threadId": A})["structuredContent"][
                        "ids"
                    ],
                    [],
                )
                resumed = self.direct(owner, "flag" if owner else "none")
                self.assertEqual(
                    resumed.call("zulip_read_topic", read, meta={"threadId": A})[
                        "structuredContent"
                    ]["ids"],
                    [],
                )
                self.assertEqual(
                    resumed.call("zulip_read_topic", read, meta={"threadId": B})[
                        "structuredContent"
                    ]["ids"],
                    [own],
                )
                self.assertEqual(tools.posted_ids, set())
                self.fake.messages[-1]["content"] += " @**Codex**"
                # Inbox retains its shared mention view, including own and sibling messages.
                for thread in (A, B):
                    self.assertIn(
                        own,
                        tools.call("zulip_inbox", {}, meta={"threadId": thread})[
                            "structuredContent"
                        ]["ids"],
                    )

    def test_unknown_native_write_does_not_adopt_sibling_body_or_resend(self):
        real_sha = T._sha

        def colliding(text):
            return "deadbeef" + "0" * 56 if text in (A, B) else real_sha(text)

        for owner in (None, "same explicit owner tag"):
            with self.subTest(owner=owner), mock.patch.object(T, "_sha", side_effect=colliding):
                tools = self.direct(owner, "flag" if owner else "none")
                args = {
                    "topic": "unknown-owner" if owner else "unknown-derived",
                    "text": "identical body",
                }
                with mock.patch.object(
                    tools, "_run_cli", side_effect=Z.NetworkError("mock timeout", maybe_sent=True)
                ):
                    first = tools.call("zulip_post", args, meta={"threadId": A})
                self.assertEqual(first["_meta"][T.ERROR_META_KEY]["code"], "outcome_unknown")
                sibling = tools.call("zulip_post", args, meta={"threadId": B})["structuredContent"][
                    "id"
                ]
                before = len(self.fake.requests_to("POST", "messages"))
                with mock.patch.object(
                    tools,
                    "_reconcile",
                    side_effect=AssertionError("ambiguous body search forbidden"),
                ):
                    for thread in (A, A):
                        retry = tools.call("zulip_post", args, meta={"threadId": thread})
                        self.assertEqual(
                            retry["_meta"][T.ERROR_META_KEY]["code"], "outcome_unknown"
                        )
                        self.assertFalse(retry["_meta"][T.ERROR_META_KEY]["retryable"])
                self.assertEqual(len(self.fake.requests_to("POST", "messages")), before)
                self.assertEqual(
                    State(self.state_dir, "CODEX", "codex-thread-" + A).posted(), set()
                )
                self.assertIn(sibling, State(self.state_dir, "CODEX", "codex-thread-" + B).posted())
                self.assertEqual(
                    tools.call("zulip_read_topic", {"topic": args["topic"]}, meta={"threadId": A})[
                        "structuredContent"
                    ]["ids"],
                    [sibling],
                )

    def test_codex_thread_ledger_outlasts_the_posted_limit(self):
        for seat, retain in (("CODEX", True), ("CODEX", False), ("CLAUDE", False)):
            with self.subTest(seat=seat, retain=retain):
                state = State(self.state_dir, seat, "codex-thread-" + A, retain_posted=retain)
                state.record_posted(1)
                data = json.loads(state.path.read_text())
                data["posted"] = list(range(1, 501))
                state.path.write_text(json.dumps(data))
                state.record_posted(501)
                self.assertEqual(state.posted(), set(range(1 if retain else 2, 502)))

    def test_native_ownership_survives_ledger_limit_and_resume(self):
        real_sha = T._sha

        def colliding(text):
            return "deadbeef" + "0" * 56 if text in (A, B) else real_sha(text)

        for owner in (None, "reused owner tag"):
            with self.subTest(owner=owner), mock.patch.object(T, "_sha", side_effect=colliding):
                tools = self.direct(owner, "flag" if owner else "none")
                topic = "retained-owner" if owner else "retained-derived"
                args = {"topic": topic, "text": "earliest owned post"}
                first = tools.call("zulip_post", args, meta={"threadId": A})["structuredContent"][
                    "id"
                ]
                state = State(self.state_dir, "CODEX", "codex-thread-" + A)
                # Seed confirmed receipts to avoid 500 real fsyncs; the next
                # fake-Zulip post exercises the production retention policy.
                data = json.loads(state.path.read_text())
                data["posted"] = list(range(first, first + 500))
                state.path.write_text(json.dumps(data))
                self.fake.next_id = first + 500
                last = tools.call(
                    "zulip_post", {"topic": topic, "text": "post 501"}, meta={"threadId": A}
                )["structuredContent"]["id"]
                sibling = tools.call(
                    "zulip_post", {"topic": topic, "text": "sibling post"}, meta={"threadId": B}
                )["structuredContent"]["id"]
                self.assertIn(first, state.posted())
                self.assertIn(last, state.posted())
                resumed = self.direct(owner, "flag" if owner else "none")
                read = {"topic": topic}
                self.assertEqual(
                    resumed.call("zulip_read_topic", read, meta={"threadId": A})[
                        "structuredContent"
                    ]["ids"],
                    [sibling],
                )
                self.assertEqual(
                    resumed.call("zulip_read_topic", read, meta={"threadId": B})[
                        "structuredContent"
                    ]["ids"],
                    [first, last],
                )

    def test_native_topic_read_snapshots_current_thread_once_per_page(self):
        tools = self.direct()
        topic = "snapshot-page"
        own = tools.call(
            "zulip_post", {"topic": topic, "text": "A authored"}, meta={"threadId": A}
        )["structuredContent"]["id"]
        sibling = tools.call(
            "zulip_post", {"topic": topic, "text": "B authored"}, meta={"threadId": B}
        )["structuredContent"]["id"]
        page = self.fake.messages[-2:] * 25
        original = State.posted
        reads = []

        def recorded(state):
            reads.append(state.path)
            return original(state)

        with (
            mock.patch.object(tools, "_page", return_value=page),
            mock.patch.object(State, "posted", autospec=True, side_effect=recorded),
        ):
            for thread, visible in ((A, sibling), (B, own), (A, sibling)):
                reads.clear()
                result = tools.call(
                    "zulip_read_topic", {"topic": topic, "limit": 50}, meta={"threadId": thread}
                )["structuredContent"]
                self.assertEqual(result["ids"], [visible] * 25)
                self.assertEqual(
                    reads, [self.state_dir / "CODEX" / ("codex-thread-" + thread) / "state.json"]
                )
            reads.clear()
            result = tools.call(
                "zulip_read_topic",
                {"topic": topic, "limit": 50, "include_self": True},
                meta={"threadId": A},
            )["structuredContent"]
            self.assertEqual(result["ids"], [own, sibling] * 25)
            self.assertEqual(reads, [])
