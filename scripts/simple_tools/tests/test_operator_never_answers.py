"""The Operator never answers on behalf of a platform (owner ruling, Nov 10 2026).

The owner's words, after complaining five or six times in a week:  "The operator should never be
answering on behalf of a platform."  The Operator used to post a headless agent's words in a chat topic
as `[OPERATOR·wake] re=<id> ...`, and those replies answered questions as if the owner had asked it to
and promised things it never does.  `Daemon.post` is a refusal now, for every seat and every wake type,
and these tests pin the rule four ways:

  1. every adapter (claude, muse, openrouter, the mcode fallback, an http routine) completes a wake and
     the Operator posts nothing, with the refusal on the ledger row;
  2. the Operator's own alerts and notes (the held-wake digest, the owner DM note) still go out;
  3. `Daemon.post` is a pure refusal (no Operator client, no request, no kept text);
  4. the structure:  no function can build a wake reply, a route page or an owner ack, and the set of
     functions that send a message as the Operator, and of modules that post a Zulip message at all, is
     pinned, so a new sender is a deliberate change to this file and never an accident.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
import unittest
from pathlib import Path
from typing import Any

from simple_tools import adapters as A
from simple_tools import daemon as D
from simple_tools import heldalert as HELDALERT
from simple_tools import wakes as W
from simple_tools.tests.fake_routine import FakeRoutine
from simple_tools.tests.test_daemon import MENTION, DaemonHarness
from simple_tools.tests.test_openrouter_daemon import OpenRouterHarness, or_output

TOOLS = Path(D.__file__).resolve().parent
#: What a wake reply or a route page began with:  `[WORD·wake] re=` and `[WORD·route→SEAT] re=`.
TAG = re.compile("^\\s*\\[[A-Za-z0-9_-]+·(wake|route)")
REASON = "operator_never_answers"


class Quiet:
    """The assertions every adapter's completed wake has to pass."""

    def assert_nothing_was_posted(self) -> None:
        self.assertEqual(self.operator_posts(), [], "the Operator posted for a wake")
        self.assertEqual(self.seat_requests(), [], "a seat's own bot wrote for a wake")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [], "a message was attempted")
        self.assertEqual(self.fake.requests_to("POST", "typing"), [], "a typing indicator was raised")
        for message in self.fake.messages:
            self.assertIsNone(TAG.match(str(message.get("content") or "")), message.get("content"))

    def assert_the_reply_was_dropped(self, row: dict[str, Any]) -> None:
        self.assertEqual(row["state"], "done", row)
        self.assertEqual(row["post_error"], REASON)
        self.assertIsNone(row["posted_id"])
        self.assertNotIn("posted_by", row)
        refusals = [e for e in self.log_events("post-refused") if e.get("wake_id") == row["wake_id"]]
        self.assertEqual([e["reason"] for e in refusals], [REASON])


# --------------------------------------------------------------------------------------------
# 1. One test per adapter
# --------------------------------------------------------------------------------------------


class ClaudeWakeTests(Quiet, DaemonHarness):
    def test_a_completed_claude_wake_posts_nothing(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(self.final_adapter(), "claude")
        self.assert_the_reply_was_dropped(self.terminal_wakes()[-1])
        self.assert_nothing_was_posted()

    def final_adapter(self) -> str:
        return str(self.terminal_wakes()[-1]["adapter"])

    def test_every_action_the_model_can_take_posts_nothing_but_the_owner_note(self) -> None:
        daemon = self.started()
        owner = self.fake.user_named("Jay Wedgeworth")
        cases = {
            "a reply to a peer": ("Codex", dict(action="reply", reply="Done.  Anything else?", board=None,
                                                owner_note=None, risk="low", route=None)),
            "a reply to the owner": (owner, dict(action="reply", reply="On it.", board=None,
                                                 owner_note=None, risk=None, route=None)),
            "a route the owner asked for": (owner, dict(action="none", reply=None, board=None, owner_note=None,
                                                        risk=None, route={"seat": "CODEX", "reason": "look"})),
            "an escalation by an owner trigger": (owner, dict(action="escalate", reply=None, board=None,
                                                              owner_note="deploy now", risk=None, route=None)),
        }
        for n, (label, (sender, output)) in enumerate(cases.items()):
            with self.subTest(label):
                self.set_claude(mode="ok", output=output)
                topic = "case %d" % n
                self.fake.add_message(sender, "simple-tools", topic, MENTION + " please")
                self.pump_until(daemon, lambda n=n: sum(1 for r in self.ledger() if r["state"] == "queued") >= n + 1)
                self.wake_cycle(daemon, 25)
        self.assertEqual(len(self.terminal_wakes()), len(cases))
        self.assertEqual([r["state"] for r in self.terminal_wakes()], ["done"] * len(cases))
        self.assert_nothing_was_posted()

    def test_a_peer_escalation_posts_only_the_owner_note_and_never_a_reply(self) -> None:
        daemon = self.started()
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "I can't share credentials.",
                                           "board": None, "risk": "high", "owner_note": "asked for a key"})
        self.fake.add_message("Codex", "simple-tools", "t", MENTION + " the key please")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        posts = self.operator_posts()
        self.assertEqual([(p["type"], p["content"].split("\n")[0][:15]) for p in posts],
                         [("private", "[OPERATOR·note]")])
        self.assertEqual(self.fake.requests_to("POST", "typing"), [])
        self.assertEqual(self.seat_requests(), [])
        self.assertNotIn("credentials", posts[0]["content"], "the dropped reply is not in the note")


class MuseWakeTests(Quiet, OpenRouterHarness):
    def setUp(self) -> None:
        super().setUp()
        self.mm_config("muse")
        self.pin_muse()

    def test_a_completed_muse_wake_posts_nothing(self) -> None:
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual((row["adapter"], self.muse_runs()), ("muse", 1))
        self.assert_the_reply_was_dropped(row)
        self.assert_nothing_was_posted()


class OpenRouterWakeTests(Quiet, OpenRouterHarness):
    def setUp(self) -> None:
        super().setUp()
        self.mm_config("openrouter")
        self.pin_openrouter()

    def test_a_completed_openrouter_wake_posts_nothing(self) -> None:
        daemon = self.mm()
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual((row["adapter"], len(self.router.requests())), ("openrouter", 1))
        self.assert_the_reply_was_dropped(row)
        self.assert_nothing_was_posted()


class FakeMcode:
    """An mcode runner that answers with a valid wake result, and counts its runs."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self, argv: Any, env: Any, cwd: str, prompt: str, schema: Any = None) -> A.RunResult:
        self.runs += 1
        out = A.RunResult()
        output = or_output("On it.  The cutover is in review.")
        out.result = {"result": json.dumps(output)}
        out.parsed = output
        out.exit = 0
        out.models = ["fake-mcode"]
        return out


class McodeFallbackWakeTests(Quiet, OpenRouterHarness):
    def setUp(self) -> None:
        super().setUp()
        self.mm_config("muse", extra='fallback = "mcode"\nfallback_uncontained_ok = true')
        self.pin_muse()
        mcode = self.bin / "mcode"
        mcode.write_text("#!/bin/sh\n")
        mcode.chmod(0o755)
        A.record_mcode_pin(self.seat_paths("MM"), str(mcode), self.clock.time())
        self.set_muse(mode="silent_exit2")  # the primary fails (no answer), so the mcode fallback answers

    def test_a_completed_mcode_fallback_wake_posts_nothing(self) -> None:
        mcode = FakeMcode()
        daemon = self.mm(mcode_runner=mcode)
        self.mm_wake(daemon)
        row = self.final()
        self.assertEqual((row["adapter"], mcode.runs), ("mcode", 1))
        self.assert_the_reply_was_dropped(row)
        self.assert_nothing_was_posted()
        (event,) = self.log_events("wake-fallback")
        self.assertEqual(event["fallback"], "mcode")


class HttpRoutineWakeTests(Quiet, DaemonHarness):
    """The `http` wake forwards the trigger to the seat's own routine, which replies in Zulip itself as
    the seat.  The Operator is not in it, and nothing here posts."""

    SEAT = textwrap.dedent("""\
        [seat.CLAUDE]
        bot = "Claude"
        wake = "http"
        routine = {}
        """)

    def test_a_completed_routine_wake_posts_nothing(self) -> None:
        routine = FakeRoutine()
        routine.start()
        self.addCleanup(routine.stop)
        self.write_config(rewake=True, seats=self.SEAT)
        daemon = self.started(
            env=self.env(CLAUDE_ROUTINE_URL=routine.url + "/hook?token=t", CLAUDE_ROUTINE_KEY="rk-test-key")
        )
        mid = self.fake.add_message("Codex", "other", "elsewhere", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.wake_cycle(daemon)
        self.assertTrue(routine.wait_for(1, timeout=10), "the routine was never posted to")
        self.assertEqual(routine.received[0].json()["message_id"], mid)
        row = self.terminal_wakes()[-1]
        self.assertEqual((row["state"], row["adapter"]), ("done", "http"))
        self.assertNotIn("post_error", row, "nothing of the daemon's was posted, so nothing was refused")
        self.assert_nothing_was_posted()


# --------------------------------------------------------------------------------------------
# 2. What the Operator still sends
# --------------------------------------------------------------------------------------------


class OperatorStillSendsTests(DaemonHarness):
    def test_the_held_wake_digest_still_goes_to_alerts(self) -> None:
        daemon = self.started()
        self.warm_operator(daemon)  # the digest runs on the router thread, which never waits for a build
        self.seed_held()
        daemon._held_alert(self.clock.time())
        posts = self.operator_posts()
        self.assertEqual([(m["display_recipient"], m["subject"]) for m in posts],
                         [(HELDALERT.CHANNEL, HELDALERT.TOPIC)])
        self.assertIn("CLAUDE", posts[0]["content"])
        self.assertEqual(self.seat_requests(), [])

    def test_the_owner_dm_note_still_goes_out(self) -> None:
        daemon = self.started()
        self.set_claude(mode="ok", output={"action": "escalate", "reply": "No.", "board": None,
                                           "risk": "high", "owner_note": "Codex asked for the key."})
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION + " the key please")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        (note,) = self.operator_posts()
        self.assertEqual(note["type"], "private")
        self.assertTrue(note["content"].startswith("[OPERATOR·note] re=%d\n" % mid), note["content"])
        self.assertIn("Codex asked for the key.", note["content"])
        row = self.terminal_wakes()[-1]
        self.assertEqual((row["state"], row["owner_dm"]), ("done", note["id"]))


# --------------------------------------------------------------------------------------------
# 3. `Daemon.post` is a pure refusal
# --------------------------------------------------------------------------------------------


class PostIsARefusalTests(DaemonHarness):
    def pending(self, **item: Any) -> W.Pending:
        now = self.clock.time()
        base = {"channel": "simple-tools", "topic": "t", "type": "stream"}
        pending = W.Pending("w-refused", "CLAUDE", {**base, **item}, owner=True, now=now, due=now)
        pending.add({"id": 900, "ts": now}, True)
        return pending

    def test_post_refuses_without_an_operator_client_a_request_or_any_text(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        builds = self.operator_builds
        before = len(self.fake.requests)
        for kind in ("reply", "route", "owner_ack"):
            for pending in (self.pending(), self.pending(type="private", recipients=[10, 12], channel="", topic="")):
                with self.subTest(kind=kind, type=pending.type):
                    pending.post_refusal = None
                    self.assertIsNone(daemon.post(runner, pending, kind))
                    self.assertEqual(pending.post_refusal, REASON)
        self.assertEqual(self.operator_builds, builds, "no Operator client was built for a refusal")
        self.assertEqual(len(self.fake.requests), before, "no request of any kind was made")
        self.assertEqual(self.operator_posts(), [])
        events = self.log_events("post-refused")
        self.assertEqual({e["reason"] for e in events}, {REASON})
        self.assertEqual({e["kind"] for e in events}, {"reply", "route", "owner_ack"})
        self.assertEqual(self.log_events("post-failed") + self.log_events("post-refused-dm"), [])

    def test_the_refusal_does_not_depend_on_the_operator_being_reachable(self) -> None:
        self.operator_available = False
        daemon = self.started()
        pending = self.pending()
        self.assertIsNone(daemon.post(daemon.seats["CLAUDE"], pending))
        self.assertEqual(pending.post_refusal, REASON, "not `operator_unavailable`:  the rule, not the outage")
        self.assertEqual(self.operator_builds, 0)

    def test_a_refusal_never_marks_a_wake_failed_or_runs_a_retry_loop(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.clock.advance(7200)
        daemon.tick()
        daemon.run_jobs("CLAUDE")
        states = [r["state"] for r in self.ledger()]
        self.assertEqual(states, ["queued", "accepted", "started", "done"], "one run, no retry, no second row")
        self.assertEqual(len(self.dumps()), 1)
        self.assertEqual(len(self.log_events("post-refused")), 1)


# --------------------------------------------------------------------------------------------
# 4. The structure
# --------------------------------------------------------------------------------------------


def modules() -> list[Path]:
    return sorted(p for p in TOOLS.rglob("*.py") if "tests" not in p.relative_to(TOOLS).parts)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


class Callers(ast.NodeVisitor):
    """The function each call to one of `names` sits in, as `{method name: {enclosing function}}`."""

    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.stack: list[str] = []
        self.found: dict[str, set[str]] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in self.names:
            self.found.setdefault(str(name), set()).add(self.stack[-1] if self.stack else "<module>")
        self.generic_visit(node)


class StructureTests(unittest.TestCase):
    def test_no_code_can_build_a_wake_reply_a_route_page_or_an_owner_ack(self) -> None:
        for path in modules():
            tree = parse(path)
            for node in ast.walk(tree):
                names = {
                    getattr(node, "name", None),
                    getattr(node, "id", None),
                    getattr(node, "attr", None),
                }
                self.assertFalse(
                    names & {"compose_reply", "compose_route", "OWNER_ACK"},
                    "%s:%s builds or names a wake reply, a route page or an owner ack" % (path.name, getattr(node, "lineno", 0)),
                )

    #: The only strings in non-test code that name a `·wake` or `·route` tag.
    TAG_ALLOWED = {
        # The router and the loop guard READ the tag on posts other bots wrote.
        "router.py",
        # The http routine's `reply_prefix`:  the seat's own routine posts it, as itself.
        "adapters.py",
    }

    def test_no_string_formats_a_wake_or_route_tag_outside_the_two_readers_of_it(self) -> None:
        tag = re.compile("·(wake|route)")
        for path in modules():
            tree = parse(path)
            docs = docstring_nodes(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                    if tag.search(node.value) and path.name not in self.TAG_ALLOWED:
                        self.fail("%s:%d formats or matches a wake/route tag:  %r" % (path.name, node.lineno, node.value[:60]))

    def test_the_daemon_sends_as_the_operator_from_these_functions_and_no_others(self) -> None:
        tree = parse(TOOLS / "daemon.py")
        callers = Callers({"_operator_send", "_operator_edit", "send_candidate_list", "send_location_question"})
        callers.visit(tree)
        self.assertEqual(
            callers.found,
            {
                # The owner's DM notes and alerts, which are the Operator's own and answer nothing.
                "_operator_send": {"dm_owner", "_run_broadcast_audit_body", "_held_alert"},
                "_operator_edit": {"_held_alert_edit"},
                # The Operator asking which session a letter means, and its own registration protocol.
                "send_candidate_list": {"ask_operator_for_candidates"},
                "send_location_question": {"_ask_registration_row", "_maybe_registration_answer"},
            },
            "a new function sends as the Operator:  it must not answer a wake, and this list is the place to say so",
        )

    def test_only_the_two_choke_points_touch_the_voice_client(self) -> None:
        tree = parse(TOOLS / "daemon.py")
        found: dict[str, set[str]] = {}
        stack: list[str] = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("post", "edit")
                    and ast.unparse(func.value) == "self.operator_voice"
                ):
                    found.setdefault(func.attr, set()).add(stack[-1])
                self.generic_visit(node)

        V().visit(tree)
        self.assertEqual(found, {"post": {"_operator_send"}, "edit": {"_operator_edit"}})

    def test_post_is_a_refusal_with_no_path_to_the_operator(self) -> None:
        function = next(
            node for node in ast.walk(parse(TOOLS / "daemon.py")) if isinstance(node, ast.FunctionDef) and node.name == "post"
        )
        body = ast.Module(body=function.body[1:], type_ignores=[])  # after the docstring
        used = {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(body) if isinstance(n, ast.Name)
        }
        self.assertFalse(used & {"_operator_send", "_operator_edit", "operator_voice", "OPV", "Z", "client"}, used)
        self.assertEqual([n for n in ast.walk(body) if isinstance(n, ast.Return) and n.value is not None], [])
        self.assertEqual([n for n in ast.walk(body) if isinstance(n, ast.Raise)], [])

    #: Every module that posts a Zulip message, and whose message it is.  A new one fails this test.
    MESSAGE_POSTERS = {
        "cli.py": "the seat's own words, through its own CLI",
        "mcp/tools.py": "the seat's own words, through its own MCP tools",
        "dm_policy.py": "the Side-Chats helper a seat's DM is routed to",
        "listener_cli.py": "`daemon probe`, a diagnostic a person runs",
        "handbook_service.py": "the Operator's own handbook change post (automatic, answers nothing)",
        "operator_candidates.py": "the candidate list:  the Operator asking which session a letter means",
        "operator_registration_ask.py": "the Operator's registration question and its confirmation",
        "operator_post.py": "the Operator's voice, behind the daemon's two choke points",
    }

    def test_the_modules_that_post_a_zulip_message_are_the_known_ones(self) -> None:
        posters: set[str] = set()
        for path in modules():
            for node in ast.walk(parse(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "post"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "messages"
                ):
                    posters.add(str(path.relative_to(TOOLS)))
        self.assertEqual(posters, set(self.MESSAGE_POSTERS), "a new module posts messages:  say whose they are")

    def test_the_typing_indicator_is_not_started_by_anything_but_the_dead_plumbing(self) -> None:
        # `_typing_signal` answers None, so `typing.start()` in `run_wake` never runs;  nothing else may
        # construct the signal.
        constructors = []
        for path in modules():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "TypingSignal":
                    constructors.append(path.name)
        self.assertEqual(constructors, [], "something raises the Operator's typing indicator again")


if __name__ == "__main__":
    unittest.main()
