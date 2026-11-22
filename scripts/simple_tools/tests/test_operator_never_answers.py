"""The Operator never answers on behalf of a platform (owner ruling, Nov 10 2026).

The owner's words, after complaining five or six times in a week:  "The operator should never be
answering on behalf of a platform."  The Operator used to post a headless agent's words in a chat topic
as `[OPERATOR·wake] re=<id> ...`, and those replies answered questions as if the owner had asked it to
and promised things it never does.  The same day the listener stopped starting models at all (the
claude, muse and openrouter wake types were retired), so the daemon has no answering code left, and
these tests pin the rule three ways:

  1. the two wakes a seat can have post nothing:  an http routine completes a wake and an inbox seat
     captures a mention, and the Operator posts nothing for either;  a seat that still names a retired
     wake type is refused, only captures, starts nothing and posts nothing;
  2. the Operator's own alerts and notes (the held-wake digest, the owner DM note) still go out;
  3. the structure:  the daemon has no function that posts a wake's answer, no function can build a
     wake reply, a route page or an owner ack, and the set of functions that send a message as the
     Operator, and of modules that post a Zulip message at all, is pinned, so a new sender is a
     deliberate change to this file and never an accident.

A fourth set pins the owner's Nov 10, 2026 ruling that the Operator asks nobody a clarifying question:
no candidate list, no registration question and no clarifying-question exemption in the hosted tools.
"""

from __future__ import annotations

import ast
import re
import textwrap
import unittest
from pathlib import Path

from simple_tools import daemon as D
from simple_tools import heldalert as HELDALERT
from simple_tools import wakes as W
from simple_tools.tests.fake_routine import FakeRoutine
from simple_tools.tests.test_daemon import MENTION, DaemonHarness

TOOLS = Path(D.__file__).resolve().parent
#: What a wake reply or a route page began with:  `[WORD·wake] re=` and `[WORD·route→SEAT] re=`.
TAG = re.compile("^\\s*\\[[A-Za-z0-9_-]+·(wake|route)")


class Quiet:
    """The assertions every wake and every capture has to pass."""

    def assert_nothing_was_posted(self) -> None:
        self.assertEqual(self.operator_posts(), [], "the Operator posted for a wake")
        self.assertEqual(self.seat_requests(), [], "a seat's own bot wrote for a wake")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [], "a message was attempted")
        self.assertEqual(self.fake.requests_to("POST", "typing"), [], "a typing indicator was raised")
        for message in self.fake.messages:
            self.assertIsNone(TAG.match(str(message.get("content") or "")), message.get("content"))



# --------------------------------------------------------------------------------------------
# 1. One test per kind of seat
# --------------------------------------------------------------------------------------------


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


class CaptureSeatTests(Quiet, DaemonHarness):
    """A seat with no routine only captures:  the mention reaches its inbox, no wake row is written, and
    nothing is posted.  A seat that still names a retired wake type is refused and does exactly that."""

    default_wake = "inbox"

    def capture_one(self) -> D.Daemon:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual([row["id"] for row in self.inbox()], [mid])
        return daemon

    def test_an_inbox_seat_captures_a_mention_and_nothing_is_posted(self) -> None:
        self.capture_one()
        self.assertEqual(self.ledger(), [], "a capture seat is never woken")
        self.assertEqual(self.routine.received, [])
        self.assert_nothing_was_posted()

    def test_a_retired_wake_type_is_refused_captures_and_starts_nothing(self) -> None:
        for value in ("claude", "muse", "openrouter"):
            with self.subTest(wake=value):
                self.write_config(wake=value)
                daemon = self.daemon()
                self.assertEqual(daemon.config.seats["CLAUDE"].wake, "inbox")
                self.assertTrue(
                    daemon.config.seats["CLAUDE"].config_problems[0].startswith(
                        'seat.CLAUDE.wake = "%s" is retired' % value
                    )
                )
                daemon.shutdown()
        # The last refused config, end to end:  the mention is captured and nothing ever starts.
        self.capture_one()
        self.assertEqual([r for r in self.ledger() if r["state"] == "started"], [])
        self.assertEqual(self.ledger(), [])
        self.assertEqual(self.dumps(), [], "no model ran")
        self.assertEqual(self.routine.received, [])
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
        # The escalation note's sender stays although no wake can raise one any more (a model's
        # escalate result was its only caller):  called directly, it still sends as the Operator.
        daemon = self.started()
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION + " the key please")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        now = self.clock.time()
        pending = W.Pending(
            "w-note", "CLAUDE", {"channel": "simple-tools", "topic": "t", "type": "stream"},
            owner=False, now=now, due=now,
        )
        pending.add({"id": mid, "ts": now}, False)
        sent = daemon.dm_owner(daemon.seats["CLAUDE"], pending, "Codex asked for the key.", "high", [])
        (note,) = self.operator_posts()
        self.assertEqual(note["type"], "stream")
        self.assertEqual(
            (note["display_recipient"], note["subject"]), ("Side-Chats", "Operator escalation notes")
        )
        self.assertTrue(
            note["content"].startswith("@**Jay Wedgeworth**\n[OPERATOR·note] re=%d\n" % mid),
            note["content"],
        )
        self.assertIn("Codex asked for the key.", note["content"])
        self.assertEqual(sent, {"owner_dm": note["id"]})
        self.assertEqual(self.seat_requests(), [])


# --------------------------------------------------------------------------------------------
# 3. The structure
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
                "_operator_send": {
                    "dm_owner",
                    "_run_broadcast_audit_body",
                    "_held_alert",
                    # Protected PR-review event notice, not a wake's answer or model output.
                    "_review_alert_message_send",
                },
                "_operator_edit": {"_held_alert_edit"},
                # Nothing else:  the Operator asks nobody (see OperatorAsksNobodyTests).
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

    #: The functions that took a model's result and acted on it:  posted its reply (`post`, a refusal
    #: since Nov 10, 2026), acted on it (`act`), routed it (`route_check`, `handle_route`), filed it on
    #: the board (`file_board`) or sent it to a chat (`select_chat`).  They went with the retired wake
    #: types, and none may come back.
    ANSWERING = {"post", "act", "route_gate", "route_check", "handle_route", "file_board", "select_chat"}

    def test_the_daemon_has_no_function_that_answers_a_wake(self) -> None:
        tree = parse(TOOLS / "daemon.py")
        daemon_class = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Daemon"
        )
        methods = {
            node.name
            for node in daemon_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(methods & self.ANSWERING, set(), "the Operator never answers a wake")
        self.assertIn("dm_owner", methods, "the escalation note's sender stays (owner-only DM)")
        self.assertFalse(hasattr(W, "OPERATOR_NEVER_ANSWERS"), "nothing is left to refuse")

    #: Every module that posts a Zulip message, and whose message it is.  A new one fails this test.
    MESSAGE_POSTERS = {
        "cli.py": "the seat's own words, through its own CLI",
        "mcp/tools.py": "the seat's own words, through its own MCP tools",
        "dm_policy.py": "the Side-Chats helper a seat's DM is routed to",
        "listener_cli.py": "`daemon probe`, a diagnostic a person runs",
        "handbook_service.py": "the Operator's own handbook change post (automatic, answers nothing)",
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

    def test_the_typing_indicator_is_not_started_by_anything(self) -> None:
        # The typing indicator (typing_signal.py) went with the retired wake types:  nothing may
        # construct one again.
        constructors = []
        for path in modules():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "TypingSignal":
                    constructors.append(path.name)
        self.assertEqual(constructors, [], "something raises the Operator's typing indicator again")
        self.assertFalse((TOOLS / "typing_signal.py").exists())


# --------------------------------------------------------------------------------------------
# 4. The Operator asks nobody
# --------------------------------------------------------------------------------------------


class OperatorAsksNobodyTests(unittest.TestCase):
    """Owner ruling, Nov 10, 2026:  "forget the operator asking clarification since overcomplicates
    for minimal benefit".  The candidate-list DM (which session a letter means), the registration
    question ("where do you run?") and the hosted tools' clarifying-question exemption are gone, and
    these cases fail if any of the removed functions or modules comes back."""

    #: The functions that asked.  No module may define one or call one again.
    ASKERS = {
        "ask_operator_for_candidates",
        "send_candidate_list",
        "_send_candidate_list",
        "send_location_question",
        "maybe_ask_registrations",
        "_ask_registration_row",
        "_maybe_registration_answer",
        "operator_dm_allowed",
    }
    #: The modules that asked, and the registration question's queue.
    ASKING_MODULES = ("operator_registration_ask", "operator_registration")

    def test_no_module_defines_or_calls_an_asker(self) -> None:
        for path in modules():
            for node in ast.walk(parse(path)):
                name = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = node.name
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name):
                    name = node.id
                self.assertNotIn(
                    name, self.ASKERS, "%s:%d asks again" % (path.name, getattr(node, "lineno", 0))
                )

    def test_the_asking_modules_are_gone_and_nothing_imports_them(self) -> None:
        for module in self.ASKING_MODULES:
            self.assertFalse((TOOLS / ("%s.py" % module)).exists(), module)
        for path in modules():
            for node in ast.walk(parse(path)):
                names: list[str] = []
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [alias.name for alias in node.names]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                for name in names:
                    self.assertNotIn(
                        name.rsplit(".", 1)[-1],
                        self.ASKING_MODULES,
                        "%s:%d imports %s" % (path.name, node.lineno, name),
                    )

    def test_the_hosted_tools_offer_no_clarifying_question(self) -> None:
        hosted = TOOLS.parent / "simple-tools-mcp" / "src" / "hosted-tools.js"
        self.assertTrue(hosted.exists(), hosted)
        text = hosted.read_text(encoding="utf-8")
        self.assertNotIn("clarifying", text.lower())
        self.assertNotIn("clarifying_question", text)


if __name__ == "__main__":
    unittest.main()
