"""The Operator voice's direct-message mode (owner ruling, Tue, Nov 10 2026).

No seat or agent ever sends a direct message, and the Operator's own are only clarification questions
about a wake.  `SIMPLE_TOOLS_OPERATOR_DM_MODE` stages that for `operator_post.OperatorVoice`:

  * `legacy` (the default, and what an unset, empty or unrecognised value means) is today's behaviour;
  * `clarify` refuses every `direct` message unless the caller passes `clarify=True`.

Stream posts are never affected, a refusal sends nothing and builds no client, and it carries no message
text anywhere.  An edit cannot rewrite a direct message the voice itself posted: the voice remembers the
ids of those (a bounded memory, ids only) and refuses `edit` of one in `clarify` mode unless the caller
passes `clarify=True`.  The daemon's five direct-message sites still send in `legacy` mode, which is the
point of the staging, and its two choke points are tested in test_operator_voice.py.

The voice is driven on its own (a fake client, no network), exactly as in test_operator_voice.py.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import unittest
from collections.abc import Callable
from typing import Any

from simple_tools import operator_post as OPV
from simple_tools import zulip as Z
from simple_tools.tests.listener_harness import OPERATOR_EMAIL, OPERATOR_USER_ID
from simple_tools.tests.test_operator_voice import OWNER, FakeOperatorClient, operator_me

MODE = "SIMPLE_TOOLS_OPERATOR_DM_MODE"
PINS = {"OPERATOR_ZULIP_USER_ID": str(OPERATOR_USER_ID), "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL}

# Distinctive on purpose: a refusal that leaks the message shows up as one of these strings somewhere.
BODY_MARK = "ZXQ-body-7731-do-not-leak"
EDIT_MARK = "QQX-edit-4410-do-not-leak"
RECIPIENT_MARK = 424242
DM: dict[str, Any] = {
    "type": "direct",
    "to": [OWNER, RECIPIENT_MARK],
    "content": "hello " + BODY_MARK,
}
STREAM: dict[str, Any] = {"type": "stream", "to": "7", "topic": "wakes", "content": "hello stream"}


class AnsweringClient(FakeOperatorClient):
    """A fake client whose answer to a post the test chooses (the shared fake always answers id 77)."""

    def __init__(self, me: dict[str, Any], answer: Callable[[Any], Any]) -> None:
        super().__init__(me)
        self.answer = answer

    def post(self, path: str, params: Any = None, **kw: Any) -> Any:
        super().post(path, params, **kw)
        return self.answer(params)


class OperatorDmModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.built: list[FakeOperatorClient] = []
        self.logged: list[tuple[str, dict[str, Any]]] = []
        self.answer: Callable[[Any], Any] | None = None

    def factory(self, owner: int) -> Any:
        client: FakeOperatorClient = (
            AnsweringClient(operator_me(), self.answer)
            if self.answer is not None
            else FakeOperatorClient(operator_me())
        )
        self.built.append(client)
        return client

    def voice(self, mode: str | None = None, **extra: str) -> OPV.OperatorVoice:
        env = dict(PINS, **extra)
        if mode is not None:
            env[MODE] = mode
        return OPV.OperatorVoice(
            env=env,
            owner_user_id=lambda: OWNER,
            factory=self.factory,
            log=lambda event, **fields: self.logged.append((event, fields)),
        )

    def sent(self) -> list[tuple[str, str, Any]]:
        """Every write that reached a Zulip client (the `users/me` read is not a write)."""
        return [call for client in self.built for call in client.calls if call[0] != "GET"]

    def warnings(self) -> list[tuple[str, dict[str, Any]]]:
        return [row for row in self.logged if row[0] == "operator-dm-mode-invalid"]

    # ---- legacy is today's behaviour, exactly ------------------------------------------------------
    def test_with_no_setting_a_direct_message_goes_out_as_it_does_today(self) -> None:
        voice = self.voice()
        self.assertEqual(voice.post(DM), {"id": 77})
        self.assertEqual(self.sent(), [("POST", "messages", DM)])
        self.assertEqual([event for event, _ in self.logged], ["operator-voice-connected"])

    def test_legacy_in_any_spelling_sends_and_says_nothing(self) -> None:
        for value in ("legacy", "LEGACY", "  Legacy \n", "", "   "):
            with self.subTest(value=value):
                self.built.clear()
                self.logged.clear()
                voice = self.voice(value)
                self.assertEqual(voice.post(DM), {"id": 77})
                self.assertEqual(self.sent(), [("POST", "messages", DM)])
                self.assertEqual(self.warnings(), [], "a value that means legacy is not a warning")

    def test_clarify_true_changes_nothing_in_legacy_mode_and_never_reaches_zulip(self) -> None:
        voice = self.voice("legacy")
        self.assertEqual(voice.post(DM, clarify=True), {"id": 77})
        self.assertEqual(
            self.sent(), [("POST", "messages", DM)], "the keyword is not a Zulip parameter"
        )

    def test_legacy_posts_carry_the_same_keywords_they_always_did(self) -> None:
        voice = self.voice()
        voice.post(STREAM, timeout=9.0)
        voice.post(DM, wait=True)
        self.assertEqual([call[:2] for call in self.sent()], [("POST", "messages")] * 2)

    # ---- clarify refuses a direct message ----------------------------------------------------------
    def test_clarify_refuses_a_direct_message_and_sends_nothing(self) -> None:
        voice = self.voice("clarify")
        for wait in (True, False):
            with self.subTest(wait=wait), self.assertRaises(OPV.OperatorRefused) as caught:
                voice.post(DM, wait=wait)
            self.assertEqual(caught.exception.code, OPV.CODE_DM_CLARIFY_ONLY)
            self.assertEqual(caught.exception.exit_code, 3, "a refusal, like the scanner's")
            self.assertIsInstance(caught.exception, Z.SimpleToolsError)
        self.assertEqual(self.sent(), [], "nothing reached Zulip")
        self.assertEqual(
            self.built, [], "a refused message costs no vault read and builds no client"
        )
        self.assertEqual(self.logged, [], "the voice logs nothing about it")

    def test_the_refusal_token_is_stable(self) -> None:
        self.assertEqual(OPV.CODE_DM_CLARIFY_ONLY, "operator_dm_clarify_only")
        voice = self.voice("clarify")
        with self.assertRaises(OPV.OperatorRefused) as caught:
            voice.post(DM)
        self.assertEqual(caught.exception.code, "operator_dm_clarify_only")
        self.assertIn(MODE, caught.exception.reason, "the reason names the setting that caused it")
        self.assertNotEqual(caught.exception.code, OPV.CODE_SECRET, "not the scanner's token")

    def test_both_spellings_of_a_direct_message_type_are_refused(self) -> None:
        voice = self.voice("clarify")
        for kind in ("direct", "private", "Direct", " PRIVATE "):
            with self.subTest(kind=kind), self.assertRaises(OPV.OperatorRefused):
                voice.post(dict(DM, type=kind))
        self.assertEqual(self.sent(), [])

    def test_clarify_true_lets_the_one_question_through_unchanged(self) -> None:
        voice = self.voice("clarify")
        self.assertEqual(voice.post(DM, clarify=True), {"id": 77})
        self.assertEqual(
            self.sent(), [("POST", "messages", DM)], "the keyword is not a Zulip parameter"
        )

    def test_only_the_value_true_counts_as_a_clarification(self) -> None:
        voice = self.voice("clarify")
        for value in (False, None, 0, 1, "yes", "true", [True], {"clarify": True}):
            with self.subTest(value=value), self.assertRaises(OPV.OperatorRefused):
                voice.post(DM, clarify=value)
        self.assertEqual(self.sent(), [])

    def test_clarify_never_affects_stream_posts_or_edits(self) -> None:
        voice = self.voice("clarify")
        self.assertEqual(voice.post(STREAM), {"id": 77})
        self.assertEqual(voice.post(dict(STREAM, type="channel")), {"id": 77})
        self.assertEqual(voice.post(STREAM, clarify=True), {"id": 77})
        voice.edit(901, "digest")
        self.assertEqual(
            self.sent(),
            [
                ("POST", "messages", STREAM),
                ("POST", "messages", dict(STREAM, type="channel")),
                ("POST", "messages", STREAM),
                ("PATCH", "messages/901", {"content": "digest"}),
            ],
        )

    def test_the_value_is_trimmed_and_case_folded(self) -> None:
        for value in ("Clarify", "  CLARIFY\n", "clarify"):
            with self.subTest(value=value), self.assertRaises(OPV.OperatorRefused):
                self.voice(value).post(DM)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.warnings(), [])

    # ---- an unrecognised value is legacy, with a warning -------------------------------------------
    def test_an_unrecognised_value_behaves_as_legacy_and_warns_once(self) -> None:
        for value in ("clarfy", "strict", "on", "1", "true", "legacy,clarify"):
            with self.subTest(value=value):
                self.built.clear()
                self.logged.clear()
                voice = self.voice(value)
                self.assertEqual(
                    voice.post(DM), {"id": 77}, "a typo must not start refusing messages"
                )
                self.assertEqual(voice.post(DM), {"id": 77})
                self.assertEqual(self.sent(), [("POST", "messages", DM)] * 2)
                self.assertEqual(
                    self.warnings(),
                    [("operator-dm-mode-invalid", {"setting": MODE, "using": "legacy"})],
                    "one warning, naming the setting and the mode in use",
                )

    def test_the_unrecognised_value_itself_is_never_logged(self) -> None:
        self.voice("zz-bad-value-91").post(DM)
        self.assertNotIn(
            "zz-bad-value-91", repr(self.logged), "it is free text from the host environment"
        )
        self.assertEqual(len(self.warnings()), 1)

    # ---- a reload applies at once ------------------------------------------------------------------
    def test_a_reload_moves_the_mode_without_a_new_voice(self) -> None:
        voice = self.voice("legacy")
        self.assertEqual(voice.post(DM), {"id": 77})
        voice.update_env(dict(PINS, **{MODE: "clarify"}))
        with self.assertRaises(OPV.OperatorRefused):
            voice.post(DM)
        self.assertEqual(voice.post(DM, clarify=True), {"id": 77})
        voice.update_env(dict(PINS))
        self.assertEqual(voice.post(DM), {"id": 77}, "the setting is gone, so legacy again")
        self.assertEqual(len(self.built), 1, "the same verified client throughout")
        self.assertEqual(self.warnings(), [])
        voice.update_env(dict(PINS, **{MODE: "nonsense"}))
        self.assertEqual(voice.post(DM), {"id": 77})
        self.assertEqual(len(self.warnings()), 1, "the reload is told about the bad value")

    # ---- an edit cannot rewrite a direct message the voice sent -----------------------------------
    def spy_on_client(self, voice: OPV.OperatorVoice) -> list[bool]:
        """Count every `client()` call, so a test can say a refusal built nothing."""
        calls: list[bool] = []
        real = voice.client

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append(True)
            return real(*args, **kwargs)

        voice.client = counting  # type: ignore[method-assign]
        return calls

    def patches(self) -> list[tuple[str, str, Any]]:
        """Every edit that reached a Zulip client."""
        return [call for call in self.sent() if call[0] == "PATCH"]

    def test_an_edit_of_a_dm_the_voice_posted_is_refused_in_clarify(self) -> None:
        voice = self.voice("clarify")
        self.assertEqual(voice.post(DM, clarify=True), {"id": 77})
        calls = self.spy_on_client(voice)
        for wait in (True, False):
            with self.subTest(wait=wait), self.assertRaises(OPV.OperatorRefused) as caught:
                voice.edit(77, "rewritten " + EDIT_MARK, wait=wait)
            exc = caught.exception
            self.assertEqual(exc.code, OPV.CODE_DM_CLARIFY_ONLY, "the same token as a refused post")
            self.assertEqual(exc.exit_code, 3)
            seen = " ".join([str(exc), repr(exc), exc.reason, repr(self.logged)])
            self.assertNotIn(EDIT_MARK, seen, "the new text must not leave through a refusal")
            self.assertNotIn(BODY_MARK, seen, "nor the text of the message it would have rewritten")
        with self.assertRaises(OPV.OperatorRefused):
            voice.edit("77", "x")  # the edit itself reads this id as 77, so the gate must too
        self.assertEqual(calls, [], "the refusal built no client")
        self.assertEqual(self.patches(), [], "nothing was edited")

    def test_clarify_true_lets_an_edit_of_a_dm_through_and_is_not_sent_to_zulip(self) -> None:
        voice = self.voice("clarify")
        voice.post(DM, clarify=True)
        voice.edit(77, "kept", clarify=True)
        self.assertEqual(self.patches(), [("PATCH", "messages/77", {"content": "kept"})])

    def test_only_the_value_true_opens_the_edit_gate(self) -> None:
        voice = self.voice("clarify")
        voice.post(DM, clarify=True)
        for value in (False, None, 0, 1, "yes", "true", [True]):
            with self.subTest(value=value), self.assertRaises(OPV.OperatorRefused):
                voice.edit(77, "x", clarify=value)
        self.assertEqual(self.patches(), [])

    def test_in_legacy_an_edit_of_a_dm_goes_through_as_it_does_today(self) -> None:
        for value in (None, "legacy", "", "nonsense"):
            with self.subTest(value=value):
                self.built.clear()
                voice = self.voice(value)
                voice.post(DM)
                voice.edit(77, "kept")
                self.assertEqual(self.patches(), [("PATCH", "messages/77", {"content": "kept"})])

    def test_the_memory_outlives_a_mode_change(self) -> None:
        voice = self.voice("legacy")
        voice.post(DM)  # sent, and remembered, while the mode is legacy
        voice.edit(77, "fine")
        voice.update_env(dict(PINS, **{MODE: "clarify"}))
        with self.assertRaises(OPV.OperatorRefused):
            voice.edit(77, "no")
        voice.update_env(dict(PINS))
        voice.edit(77, "again")
        self.assertEqual([call[2]["content"] for call in self.patches()], ["fine", "again"])

    def test_an_edit_of_anything_else_is_allowed_in_clarify(self) -> None:
        # A stream post's id (the held-wake digest is one) and an id the voice never saw, such as a
        # direct message sent before a restart.
        self.answer = lambda params: {"id": 78 if params["type"] == "stream" else 77}
        voice = self.voice("clarify")
        voice.post(STREAM)
        voice.post(DM, clarify=True)
        voice.edit(78, "digest")
        voice.edit(901, "digest")
        self.assertEqual([call[1] for call in self.patches()], ["messages/78", "messages/901"])
        with self.assertRaises(OPV.OperatorRefused):
            voice.edit(77, "the direct message, as a control")

    def test_the_memory_is_bounded_and_holds_ids_only(self) -> None:
        self.assertEqual(OPV.DM_ID_MEMORY, 256)
        counter = itertools.count(1)
        self.answer = lambda params: {"id": next(counter)}
        voice = self.voice("clarify")
        for _ in range(OPV.DM_ID_MEMORY + 44):
            voice.post(DM, clarify=True)  # ids 1 to 300
        for forgotten in (1, 44):
            voice.edit(forgotten, "ok")  # the oldest are forgotten first
        for kept in (45, 300):
            with self.subTest(kept=kept), self.assertRaises(OPV.OperatorRefused):
                voice.edit(kept, "x")
        self.assertEqual(len(voice._dm_ids), OPV.DM_ID_MEMORY)
        self.assertTrue(all(isinstance(k, int) and v is None for k, v in voice._dm_ids.items()))
        self.assertNotIn(BODY_MARK, repr(vars(voice)), "ids only, no message text is kept")

    def test_an_answer_without_an_id_is_tolerated_and_remembers_nothing(self) -> None:
        for answer in (
            {},
            {"id": None},
            {"id": True},
            {"id": "77"},
            {"result": "success"},
            None,
            ["id"],
        ):
            with self.subTest(answer=answer):
                self.built.clear()
                self.answer = lambda params, answer=answer: answer
                voice = self.voice("clarify")
                self.assertEqual(voice.post(DM, clarify=True), answer)
                self.assertEqual(len(voice._dm_ids), 0)
                for message_id in (1, 77):
                    voice.edit(message_id, "fine")

    def test_a_post_that_failed_remembers_nothing(self) -> None:
        voice = self.voice("clarify")
        voice.client().post_error = Z.ApiError("no", code="BAD_REQUEST", status=400)
        with self.assertRaises(Z.ApiError):
            voice.post(DM, clarify=True)
        voice.edit(77, "fine")
        self.assertEqual(self.patches(), [("PATCH", "messages/77", {"content": "fine"})])

    def test_the_refusal_methods_are_public_and_cost_nothing(self) -> None:
        voice = self.voice("clarify")
        with self.assertRaises(OPV.OperatorRefused):
            voice.refuse_unclarified_dm(DM)
        voice.refuse_unclarified_dm(DM, clarify=True)
        voice.refuse_unclarified_dm(STREAM)
        voice.refuse_unclarified_edit(77)  # nothing is remembered yet
        voice.refuse_unclarified_edit("not an id")  # the edit itself fails on it, as before
        voice.refuse_unclarified_edit(None)
        self.assertEqual(self.built, [], "no client was built, no vault was read")
        self.assertEqual(self.logged, [])
        self.voice("legacy").refuse_unclarified_dm(DM)

    # ---- no message content anywhere ---------------------------------------------------------------
    def test_a_refusal_carries_and_logs_no_message_content(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        voice = self.voice("clarify")
        with (
            self.assertNoLogs(level="DEBUG"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(OPV.OperatorRefused) as caught,
        ):
            voice.post(DM)
        exc = caught.exception
        seen = " ".join(
            [
                str(exc),
                repr(exc),
                exc.reason,
                str(exc.args),
                repr(self.logged),
                out.getvalue(),
                err.getvalue(),
            ]
        )
        self.assertNotIn(BODY_MARK, seen, "the message text must not leave through a refusal")
        self.assertNotIn(str(RECIPIENT_MARK), seen, "nor its recipients")
        self.assertEqual(self.logged, [], "no log row of any kind")
        self.assertEqual(self.sent(), [])

    # ---- the pieces --------------------------------------------------------------------------------
    def test_parse_dm_mode(self) -> None:
        cases: list[tuple[Any, tuple[str, bool]]] = [
            ({}, ("legacy", True)),
            ({MODE: ""}, ("legacy", True)),
            ({MODE: "legacy"}, ("legacy", True)),
            ({MODE: "clarify"}, ("clarify", True)),
            ({MODE: " Clarify "}, ("clarify", True)),
            ({MODE: "clarfy"}, ("legacy", False)),
            ({MODE: "off"}, ("legacy", False)),
            ({MODE: 1}, ("legacy", False)),
            (None, ("legacy", True)),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(OPV.parse_dm_mode(env), expected)

    def test_is_direct(self) -> None:
        for kind, expected in (
            ("direct", True),
            ("private", True),
            ("DIRECT", True),
            ("stream", False),
            ("channel", False),
            ("", False),
            (None, False),
        ):
            with self.subTest(kind=kind):
                self.assertIs(OPV.is_direct({"type": kind}), expected)
        self.assertIs(OPV.is_direct({}), False)


if __name__ == "__main__":
    unittest.main()
