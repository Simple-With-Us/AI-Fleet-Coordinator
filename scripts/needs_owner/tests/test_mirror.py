"""The Grok Bot mirror:  an OWNER topic a persona opened with a fenced `needs-owner-request` block becomes a Linear item."""
from __future__ import annotations

import json
import unittest

from needs_owner import f3
from needs_owner import links as linkmod
from needs_owner import replies as R

from .test_notifier import NotifierCase
from .world import KEYCAP, OWNER_ID

TOPIC = "OWNER GB-PLUMBER Plumber needs a key"
FENCE = "`" * 3


def request_text(**changes) -> str:
    data = {"question": "Rotate the Plumber's key?", "options": ["Approve", "Decline"], "seat": "GB-PLUMBER", "low_stakes": [1]}
    data.update(changes)
    data = {k: v for k, v in data.items() if v is not ...}
    return "I need an answer from Jay.\n\n%sneeds-owner-request\n%s\n%s\n" % (FENCE, json.dumps(data), FENCE)


class MirrorCase(NotifierCase):
    def persona(self, topic: str = TOPIC, content: str | None = None, seat: str = "GB-PLUMBER", **kw) -> int:
        return self.w.add_message(topic, content if content is not None else request_text(), seat=seat, **kw)

    def files(self) -> list[dict]:
        return [c for c in self.w.calls("board") if c["argv"][0] == "file"]

    def reply_calls(self) -> list[dict]:
        return [c for c in self.w.calls("agent-sync") if c["argv"][0] == "reply" and c.get("code") == 0]

    def mirrored_items(self) -> list[dict]:
        return [r for r in self.w.run("list", "--all", "--json").json() if r["for_seat"]]


class MirrorTests(MirrorCase):
    def test_a_persona_request_is_filed_by_claude_for_the_persona_and_linked_back_once(self) -> None:
        message = self.persona()
        result = self.run_notify()
        (mirrored,) = self.events(result, "mirrored")
        self.assertEqual((mirrored["msg"], mirrored["seat"], mirrored["linkback"]), (message, "GB-PLUMBER", "ok"))
        (item,) = self.mirrored_items()
        self.assertEqual((item["identifier"], item["seat"], item["for_seat"], item["state"]), (mirrored["id"], "CLAUDE", "GB-PLUMBER", "notified"))
        self.assertEqual((item["notified_msg_id"], item["topic"]), (message, TOPIC), "the persona's own message is the notice")
        self.assertEqual((item["options"], item["low_stakes"], item["severity"]), (["Approve", "Decline"], [1], "P2"))
        self.assertTrue(item["context"].startswith("for GB-PLUMBER"))
        # filed by CLAUDE, with AGENT_SEAT=CLAUDE, through needs-owner file --mirror-of (so: no topic of its own)
        (filed,) = self.files()
        self.assertEqual(filed["seat"], "CLAUDE")
        self.assertEqual(filed["argv"][filed["argv"].index("--by") + 1], "CLAUDE")
        self.assertIn("Mirror of: https://simplewithus.zulipchat.com/#narrow/channel/12-agent-sync/topic/", filed["files"]["--desc-file"]["text"])
        self.assertIn("/near/%d" % message, filed["files"]["--desc-file"]["text"])
        self.assertEqual([c for c in self.w.calls("agent-sync") if c["argv"][0] == "post"], [], "no new topic")
        (reply,) = self.reply_calls()
        self.assertEqual(reply["argv"][reply["argv"].index("--id") + 1], str(message))
        self.assertIn(item["identifier"], reply["stdin"])
        self.assertIn(item["url"], reply["stdin"])
        # it is a real needs-owner item:  in the Linear view, counted, never closed
        self.assertEqual(self.issue_status(item["identifier"]), "Todo")

    def test_a_request_is_mirrored_once_whatever_happens_to_the_state(self) -> None:
        self.persona()
        self.run_notify()
        self.assertEqual(len(self.files()), 1)
        again = self.run_notify()
        self.assertEqual(self.events(again, "mirrored"), [])
        self.assertEqual(len(self.files()), 1, "the topic is not even read again")
        (self.w.notifier_dir / "notify-state.json").unlink()  # a lost state file
        third = self.run_notify()
        self.assertEqual(len(self.mirrored_items()), 1, "the open item with this message id is found by its block")
        self.assertEqual(len(self.reply_calls()), 1, "and nothing is linked back twice")
        self.assertEqual(self.events(third, "mirrored"), [])

    def test_the_request_is_read_again_only_when_the_topic_changes(self) -> None:
        self.persona(content="No request here, just chatter.")
        self.run_notify()
        reads = lambda: len([c for c in self.w.calls("agent-sync") if c["argv"][0] == "read" and "--since" in c["argv"]])  # noqa: E731
        first = reads()
        self.run_notify()
        self.assertEqual(reads(), first, "unchanged topic, no read")
        late = self.persona(content=request_text())  # the block arrives in a later message of the same persona
        result = self.run_notify()
        self.assertEqual([e["msg"] for e in self.events(result, "mirrored")], [late])

    def test_an_items_own_owner_topic_is_not_a_request_topic(self) -> None:
        record = self.file("Filed by a seat?", "Yes", "No", seat="CODEX")
        self.w.add_message(record["topic"], request_text(seat="GB-PLUMBER"), seat="GB-PLUMBER")
        result = self.run_notify()
        self.assertEqual(self.events(result, "mirrored"), [])
        self.assertEqual(self.files()[1:], [])

    def test_only_a_real_persona_can_ask(self) -> None:
        self.persona(topic="OWNER from codex", seat="CODEX", content=request_text(seat="CODEX"))
        self.w.add_message("OWNER from a person", request_text())
        self.persona(topic="OWNER spoofed email", email="plumber-grok-bot@evil.example", content=request_text())
        self.persona(topic="OWNER the terminal bot", seat="GROK-BOT", content=request_text(seat="GROK-BOT"))
        self.persona(topic="OWNER cursor opened it", seat="CURSOR", content="hello")
        self.persona(topic="OWNER cursor opened it", content=request_text())  # a persona's block in a topic someone else opened
        result = self.run_notify()
        self.assertEqual(self.events(result, "mirrored"), [])
        self.assertEqual(self.events(result, "request-rejected"), [])
        self.assertEqual(self.files(), [])
        self.assertEqual(self.reply_calls(), [])

    def test_a_request_for_another_seat_is_rejected_with_one_reply(self) -> None:
        message = self.persona(content=request_text(seat="GB-DIRECTOR"))
        result = self.run_notify()
        (rejected,) = self.events(result, "request-rejected")
        self.assertEqual((rejected["msg"], rejected["reason"]), (message, "seat_is_not_the_sender"))
        (reply,) = self.reply_calls()
        self.assertIn("seat_is_not_the_sender", reply["stdin"])
        self.assertEqual(self.files(), [])
        self.assertEqual(self.mirrored_items(), [])
        self.run_notify()
        self.assertEqual(len(self.reply_calls()), 1, "told once")

    def test_a_corrected_request_after_a_wrong_one_is_the_one_that_counts(self) -> None:
        bad = self.persona(content=request_text(seat="GB-DIRECTOR"))
        first = self.run_notify()
        self.assertEqual([e["msg"] for e in self.events(first, "request-rejected")], [bad])
        fixed = self.persona(content=request_text())
        second = self.run_notify()
        self.assertEqual([e["msg"] for e in self.events(second, "mirrored")], [fixed])
        self.assertEqual(len(self.reply_calls()), 2, "one rejection, one link back")

    def test_every_way_a_request_can_be_wrong_is_a_reason_word(self) -> None:
        cases = {
            "not_json": "{not json",
            "unknown_key": json.dumps({"question": "Q?", "options": ["A"], "seat": "GB-PLUMBER", "act_without_asking": True}),
            "options_count": json.dumps({"question": "Q?", "options": [], "seat": "GB-PLUMBER"}),
            "options_repeat": json.dumps({"question": "Q?", "options": ["A", "a"], "seat": "GB-PLUMBER"}),
            "low_stakes_option": json.dumps({"question": "Q?", "options": ["A", "B"], "seat": "GB-PLUMBER", "low_stakes": [3]}),
            "recommend_option": json.dumps({"question": "Q?", "options": ["A", "B"], "seat": "GB-PLUMBER", "recommend": 0}),
            "urgent_with_low_stakes": json.dumps({"question": "Q?", "options": ["A", "B"], "seat": "GB-PLUMBER", "low_stakes": [1], "urgent": True}),
            "text_not_accepted": json.dumps({"question": "", "options": ["A", "B"], "seat": "GB-PLUMBER"}),
            "secret_or_personal_data": json.dumps({"question": "Mail jay@example.org?", "options": ["A", "B"], "seat": "GB-PLUMBER"}),
        }
        for reason, body in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(R.RequestProblem) as caught:
                    R.parse_request("%sneeds-owner-request\n%s\n%s" % (FENCE, body, FENCE), "GB-PLUMBER")
                self.assertEqual(caught.exception.reason, reason)
        two = "%sneeds-owner-request\n{}\n%s\n\n%sneeds-owner-request\n{}\n%s" % (FENCE, FENCE, FENCE, FENCE)
        with self.assertRaises(R.RequestProblem) as caught:
            R.parse_request(two, "GB-PLUMBER")
        self.assertEqual(caught.exception.reason, "two_blocks")
        self.assertIsNone(R.parse_request("no block", "GB-PLUMBER"))
        self.assertIsNone(R.parse_request("%sjson\n{}\n%s" % (FENCE, FENCE), "GB-PLUMBER"), "another fence is not a request")
        self.assertIsNone(R.parse_request("%sneeds-owner-request\n{}" % FENCE, "GB-PLUMBER"), "a fence never closed hides nothing")

    def test_a_good_request_parses_to_clean_fields(self) -> None:
        got = R.parse_request(request_text(question="  Rotate   the key?  ", recommend=1, context="Quota low", urgent=False,
                                           low_stakes=[2], app="ignored", format="x"), "gb-plumber")
        self.assertEqual((got.seat, got.question, got.options, got.low_stakes, got.recommend, got.context, got.urgent),
                         ("GB-PLUMBER", "Rotate the key?", ["Approve", "Decline"], [2], 1, "Quota low", False))

    def test_a_request_with_a_secret_or_a_run_of_digits_is_not_filed(self) -> None:
        self.persona(content=request_text(question="Use account 123456789 for it?"))
        result = self.run_notify()
        self.assertEqual(self.events(result, "request-rejected")[0]["reason"], "secret_or_personal_data")
        self.assertEqual(self.files(), [])

    def test_urgent_context_and_recommendation_carry_over(self) -> None:
        self.persona(content=request_text(urgent=True, low_stakes=None, recommend=2, context="The deploy key expires today"))
        self.run_notify()
        (item,) = self.mirrored_items()
        self.assertEqual((item["severity"], item["urgent"], item["recommend"], item["low_stakes"]), ("P1", True, 2, []))
        self.assertEqual(item["context"], "for GB-PLUMBER: The deploy key expires today")

    def test_no_more_than_three_a_run_and_the_rest_on_the_next(self) -> None:
        for n in range(5):
            self.persona(topic="OWNER GB-PLUMBER request %d" % n, content=request_text(question="Request number %d?" % n))
        first = self.run_notify()
        self.assertEqual(len(self.events(first, "mirrored")), 3)
        second = self.run_notify()
        self.assertEqual(len(self.events(second, "mirrored")), 2)
        self.assertEqual(len(self.mirrored_items()), 5)

    def test_a_full_cap_holds_the_request_for_later(self) -> None:
        self.persona()
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = {"urgent_per_hour": 3, "per_hour": 0, "per_day": 30}
        result = self.run_notify()
        self.assertTrue(self.events(result, "deferred"))
        self.assertEqual(self.files(), [])
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = {"urgent_per_hour": 3, "per_hour": 6, "per_day": 30}
        self.assertEqual(len(self.events(self.run_notify(), "mirrored")), 1)

    def test_a_closed_item_for_the_same_message_is_not_filed_again_or_looped_on(self) -> None:
        message = self.persona()
        self.run_notify()
        (item,) = self.mirrored_items()
        self.w.close(item["identifier"])
        (self.w.notifier_dir / "notify-state.json").unlink()
        result = self.run_notify()
        self.assertEqual([e["msg"] for e in self.events(result, "mirror-refused")], [message])
        calls = len(self.files())
        self.run_notify()
        self.assertEqual(len(self.files()), calls, "refused once and remembered")

    def test_a_tap_and_a_typed_reply_on_a_mirrored_item_wake_the_persona(self) -> None:
        message = self.persona()
        self.run_notify()
        (item,) = self.mirrored_items()
        self.w.react(message, OWNER_ID, KEYCAP[1])
        result = self.run_notify()
        (replied,) = self.events(result, "replied")
        self.assertEqual((replied["kind"], replied["ref"], replied["option"]), ("reaction", message, 1))
        (note,) = [c for c in self.sent_by_notifier() if "--to" in c["argv"]]
        self.assertEqual(note["argv"][note["argv"].index("--to") + 1], "GB-PLUMBER")
        self.assertEqual(note["argv"][note["argv"].index("--topic") + 1], TOPIC)
        self.assertFalse((self.w.notifier_dir / "pending-CLAUDE.json").exists(), "CLAUDE filed it, but the persona is who acts")
        self.assertEqual(self.state_of(item["identifier"])["state"], "replied")


class LinkTests(unittest.TestCase):
    def test_the_link_reads_back_to_the_same_topic_and_message(self) -> None:
        for topic in ("OWNER GB-PLUMBER needs a key", "OWNER 50% sure? a.b #1 ✔ café \U0001f600", "OWNER x.y.z", "OWNER a/b", "OWNER 100%", "OWNER  two  spaces"):
            for stream in (12, None):
                with self.subTest(topic=topic, stream=stream):
                    link = linkmod.make_topic_link(topic, 630000001, stream_id=stream)
                    parsed = linkmod.parse_topic_link(link)
                    self.assertEqual((parsed.topic, parsed.message_id, parsed.channel), (topic, 630000001, "agent-sync"))
        self.assertTrue(linkmod.make_topic_link("OWNER x", 5, stream_id=12).startswith(
            "https://simplewithus.zulipchat.com/#narrow/channel/12-agent-sync/topic/OWNER.20x/near/5"))

    def test_the_realm_comes_from_the_environment(self) -> None:
        link = linkmod.make_topic_link("OWNER x", 5, environ={"AGENT_SYNC_REALM": "https://other.zulipchat.com"})
        self.assertTrue(link.startswith("https://other.zulipchat.com/"))

    def test_f3_is_still_the_one_that_cleans(self) -> None:
        self.assertEqual(f3.textsafe.render_untrusted("@**all** hi", 40, code_span=False).count("@**all**"), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
