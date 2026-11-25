"""require_session / require_letter / require_generation picking (review-alert path).

The registry is a fake, no wake is enqueued and nothing is delivered:  every
test drives Router.pick directly.  Fixtures mirror test_wake_router.py's
row()/FakeRegistry shapes.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from simple_tools import wake_router as WR

SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"


def row(sid: str, letter: str, title: str, host: str = "server", **over) -> dict:
    base = {
        "native_session_id": sid,
        "letter": letter,
        "title": title,
        "verified_host": host,
        "seat": "CLAUDE",
        "state": "active",
        "retired_at": None,
        "closed_at": None,
        "generation": 1,
    }
    base.update(over)
    return base


class FakeRegistry:
    """Stands in for registry_api.call."""

    def __init__(self, rows: list | None = None, letters: dict | None = None) -> None:
        self.rows = rows or []
        self.letters = letters or {}

    def __call__(self, env, op, params=None, *, timeout=25):
        if op == "resolve":
            found = self.letters.get(params["letter"])
            if found is None:
                live = [r for r in self.rows if r["letter"] == params["letter"]]
                found = next((r for r in live if not r["retired_at"]), None)
            if found is None:
                return {"label": params["letter"], "found": False}
            return {**found, "label": params["letter"], "found": True}
        if op == "history":
            return {"items": list(self.rows), "count": len(self.rows)}
        raise AssertionError(op)


def platform() -> WR.Platform:
    return WR.Platform(
        seat="CLAUDE",
        platform="claude",
        mention="@claude",
        target_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$"),
        deliver=lambda target, wake: WR.Delivery(outcome="ok", detail=""),
    )


def wake(**over) -> WR.Wake:
    base = dict(
        wake_id="ra-test",
        seat="CLAUDE",
        message_id=1,
        channel="review-alerts",
        topic="review alert",
        zulip_link="",
        sender="review-alerts",
        dm=False,
        letter="",
        issue_ids=(),
        session_fragments=(),
        sender_fragment="",
        linked_by="",
    )
    base.update(over)
    return WR.Wake(**base)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wrrs", dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.registry = FakeRegistry()
        patcher = mock.patch.object(WR.registry_api, "call", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.router = WR.Router(
            platform(),
            {},
            {},
            Path(self.tmp),
            lambda: ("k" * 48, ""),
            consult=None,
        )


class RequireSessionPickTests(Base):
    def test_distinct_hyphen_positions_cannot_alias_required_full_id(self):
        required = "abcd-efgh"
        other = "abc-defgh"
        self.registry.rows = [row(other, "AB", "pr work")]
        pick = self.router.pick(wake(required_session=required), consult=False)
        self.assertIsNone(pick.target)
        self.assertEqual(pick.reason, "required-session-not-live")

    def test_exact_live_session_is_picked(self):
        self.registry.rows = [row(SID_A, "AB", "pr work"), row(SID_B, "CD", "other")]
        pick = self.router.pick(wake(required_session=SID_A), consult=False)
        self.assertEqual(pick.reason, "required-session")
        self.assertEqual(pick.target.session_id, SID_A)

    def test_unknown_session_holds_retryable(self):
        self.registry.rows = [row(SID_A, "AB", "pr work")]
        pick = self.router.pick(wake(required_session=SID_B), consult=False)
        self.assertEqual(pick.reason, "required-session-not-live")
        self.assertIs(pick.retry, True)
        self.assertIsNone(pick.target)

    def test_no_live_rows_still_holds_required_not_no_live(self):
        # The required branch runs BEFORE the no-live return:  an empty
        # candidate set is a required-session hold, named as such.
        self.registry.rows = []
        pick = self.router.pick(wake(required_session=SID_A), consult=False)
        self.assertEqual(pick.reason, "required-session-not-live")
        self.assertIs(pick.retry, True)

    def test_closed_row_for_the_session_holds(self):
        self.registry.rows = [row(SID_A, "AB", "pr work", closed_at="2026-11-20T00:00:00Z")]
        pick = self.router.pick(wake(required_session=SID_A), consult=False)
        self.assertEqual(pick.reason, "required-session-not-live")

    def test_required_session_beats_addressed_letter(self):
        # A wake naming both must follow the required session, never the
        # letter:  the letter resolve is not even consulted.
        self.registry.rows = [row(SID_A, "AB", "pr work"), row(SID_B, "CD", "other")]
        pick = self.router.pick(wake(required_session=SID_A, letter="CD"), consult=False)
        self.assertEqual(pick.reason, "required-session")
        self.assertEqual(pick.target.session_id, SID_A)

    def test_required_session_never_falls_through_to_topic_or_recency(self):
        self.registry.rows = [row(SID_A, "AB", "review alert topic words")]
        pick = self.router.pick(
            wake(required_session=SID_B, topic="review alert topic words"), consult=False
        )
        self.assertEqual(pick.reason, "required-session-not-live")
        self.assertIsNone(pick.target)

    def test_letter_constraint_mismatch_holds(self):
        self.registry.rows = [row(SID_A, "AB", "pr work")]
        pick = self.router.pick(
            wake(required_session=SID_A, require_letter="XY"), consult=False
        )
        self.assertEqual(pick.reason, "required-letter-mismatch")
        self.assertIs(pick.retry, True)
        self.assertIsNone(pick.target)

    def test_letter_constraint_match_picks(self):
        self.registry.rows = [row(SID_A, "AB", "pr work")]
        pick = self.router.pick(
            wake(required_session=SID_A, require_letter="AB"), consult=False
        )
        self.assertEqual(pick.reason, "required-session")

    def test_generation_constraint_mismatch_holds(self):
        # The chat was re-issued (generation 2):  it is not the chat that
        # opened the PR, so the wake holds rather than follow it.
        self.registry.rows = [row(SID_A, "AB", "pr work", generation=2)]
        pick = self.router.pick(
            wake(required_session=SID_A, require_generation=1), consult=False
        )
        self.assertEqual(pick.reason, "required-generation-mismatch")
        self.assertIs(pick.retry, True)
        self.assertIsNone(pick.target)

    def test_generation_constraint_match_picks(self):
        self.registry.rows = [row(SID_A, "AB", "pr work", generation=2)]
        pick = self.router.pick(
            wake(required_session=SID_A, require_generation=2), consult=False
        )
        self.assertEqual(pick.reason, "required-session")
        self.assertEqual(pick.target.generation, 2)

    def test_constraints_and_conflicting_hints_all_lose_to_session(self):
        # Letter addressed elsewhere, a topic matching another chat, an issue
        # id:  the required session still wins.
        self.registry.rows = [
            row(SID_A, "AB", "unrelated"),
            row(SID_B, "CD", "review alert topic words", issues=[{"id": "OPS-1048"}]),
        ]
        pick = self.router.pick(
            wake(
                required_session=SID_A,
                letter="CD",
                topic="review alert topic words OPS-1048",
                issue_ids=("OPS-1048",),
            ),
            consult=False,
        )
        self.assertEqual(pick.reason, "required-session")
        self.assertEqual(pick.target.session_id, SID_A)


class HttpValidationTests(Base):
    """The /wake handler refuses malformed require_* fields with 400."""

    def _handler(self):
        seats = WR.Seats([self.router])
        return WR.make_handler(seats)

    def test_malformed_require_session_is_refused(self):
        import io
        import json

        handler_cls = self._handler()
        bodies = []

        class Fake(handler_cls):
            def __init__(self):
                self.rfile = io.BytesIO(b"")
                self.wfile = io.BytesIO()
                self.headers = {"Content-Length": "0"}
                self.path = "/wake/CLAUDE"
                self.key = "k" * 48

            def _answer(self, status, obj):
                bodies.append((status, obj))

        # Drive the validation block directly through do_POST with a payload.
        payload = json.dumps(
            {
                "contract": WR.CONTRACT,
                "seat": "CLAUDE",
                "wake_id": "ra-x",
                "require_session": "not a session",
            }
        ).encode()
        fake = Fake()
        fake.headers = {"Content-Length": str(len(payload)), "Authorization": "Bearer " + "k" * 48}
        fake.rfile = io.BytesIO(payload)
        fake.do_POST()
        self.assertEqual(bodies, [(400, {"error": "require_session malformed"})])

    def test_malformed_require_letter_and_generation_are_refused(self):
        import io
        import json

        handler_cls = self._handler()

        def drive(extra):
            bodies = []

            class Fake(handler_cls):
                def __init__(self):
                    self.rfile = io.BytesIO(b"")
                    self.wfile = io.BytesIO()
                    self.path = "/wake/CLAUDE"

                def _answer(self, status, obj):
                    bodies.append((status, obj))

            payload = json.dumps(
                {
                    "contract": WR.CONTRACT,
                    "seat": "CLAUDE",
                    "wake_id": "ra-x",
                    **extra,
                }
            ).encode()
            fake = Fake()
            fake.headers = {"Content-Length": str(len(payload)), "Authorization": "Bearer " + "k" * 48}
            fake.rfile = io.BytesIO(payload)
            fake.do_POST()
            return bodies

        self.assertEqual(
            drive({"require_letter": "ab"}), [(400, {"error": "require_letter malformed"})]
        )
        self.assertEqual(
            drive({"require_generation": "3"}),
            [(400, {"error": "require_generation malformed"})],
        )
        self.assertEqual(
            drive({"require_generation": 0}),
            [(400, {"error": "require_generation malformed"})],
        )


if __name__ == "__main__":
    unittest.main()


PR = "Simple-With-Us/HogHunter#7"


class RequirePrPickTests(Base):
    def test_bound_pr_picks(self):
        self.registry.rows = [
            row(SID_A, "AB", "pr work", bound_prs=[{"repo": "Simple-With-Us/HogHunter",
                                                    "number": 7, "generation": 1}])
        ]
        pick = self.router.pick(wake(required_session=SID_A, require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-session")
        self.assertEqual(pick.target.session_id, SID_A)

    def test_unbound_pr_holds_retryable(self):
        self.registry.rows = [row(SID_A, "AB", "pr work")]
        pick = self.router.pick(wake(required_session=SID_A, require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-pr-unbound")
        self.assertIs(pick.retry, True)
        self.assertIsNone(pick.target)

    def test_other_pr_binding_does_not_count(self):
        self.registry.rows = [
            row(SID_A, "AB", "pr work", bound_prs=[{"repo": "Simple-With-Us/HogHunter",
                                                    "number": 8, "generation": 1}])
        ]
        pick = self.router.pick(wake(required_session=SID_A, require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-pr-unbound")

    def test_other_session_binding_does_not_route_here(self):
        # Session B binds the PR;  a wake naming session A still holds.
        self.registry.rows = [
            row(SID_A, "AB", "pr work"),
            row(SID_B, "CD", "other", bound_prs=[{"repo": "Simple-With-Us/HogHunter",
                                                  "number": 7, "generation": 1}]),
        ]
        pick = self.router.pick(wake(required_session=SID_A, require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-pr-unbound")

    def test_repo_match_is_case_insensitive_number_is_exact(self):
        self.registry.rows = [
            row(SID_A, "AB", "pr work", bound_prs=[{"repo": "simple-with-us/hoghunter",
                                                    "number": "7", "generation": 1}])
        ]
        pick = self.router.pick(wake(required_session=SID_A, require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-session")

    def test_stale_generation_binding_holds(self):
        # The binding was written at generation 1;  the row is now generation
        # 2 (letter re-issued):  the binding is stale and the wake holds.
        self.registry.rows = [
            row(SID_A, "AB", "pr work", generation=2,
                bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 7, "generation": 1}])
        ]
        pick = self.router.pick(wake(required_session=SID_A, require_generation=2,
                                     require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-pr-unbound")

    def test_current_generation_binding_picks(self):
        self.registry.rows = [
            row(SID_A, "AB", "pr work", generation=2,
                bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 7, "generation": 2}])
        ]
        pick = self.router.pick(wake(required_session=SID_A, require_generation=2,
                                     require_pr=PR), consult=False)
        self.assertEqual(pick.reason, "required-session")

    def test_malformed_entries_prove_nothing(self):
        for bad in (["not-a-dict"], [{"repo": 7, "number": 7}],
                    [{"repo": "Simple-With-Us/HogHunter", "number": "seven"}],
                    # Unstamped or non-int generation:  not a wildcard, proves nothing.
                    [{"repo": "Simple-With-Us/HogHunter", "number": 7}],
                    [{"repo": "Simple-With-Us/HogHunter", "number": 7, "generation": "1"}],
                    [{"repo": "Simple-With-Us/HogHunter", "number": 7, "generation": None}]):
            self.registry.rows = [row(SID_A, "AB", "pr work", bound_prs=bad)]
            pick = self.router.pick(wake(required_session=SID_A, require_pr=PR), consult=False)
            self.assertEqual(pick.reason, "required-pr-unbound", bad)


class RequirePrHttpTests(Base):
    """The /wake handler refuses a malformed require_pr with 400."""

    def test_malformed_require_pr_is_refused(self):
        import io
        import json

        handler_cls = WR.make_handler(WR.Seats([self.router]))
        bodies = []

        class Fake(handler_cls):
            def __init__(self):
                self.rfile = io.BytesIO(b"")
                self.wfile = io.BytesIO()
                self.headers = {"Content-Length": "0"}
                self.path = "/wake/CLAUDE"

            def _answer(self, status, obj):
                bodies.append((status, obj))

        payload = json.dumps(
            {
                "contract": WR.CONTRACT,
                "seat": "CLAUDE",
                "wake_id": "ra-x",
                "require_pr": "not a pr",
            }
        ).encode()
        fake = Fake()
        fake.headers = {"Content-Length": str(len(payload)),
                        "Authorization": "Bearer " + "k" * 48}
        fake.rfile = io.BytesIO(payload)
        fake.do_POST()
        self.assertEqual(bodies, [(400, {"error": "require_pr malformed"})])


class DeliveryRecheckTests(Base):
    """_stale re-verifies the PR binding at delivery, not only at pick."""

    def _pick_and_stale(self, after_rows):
        # Pick with a good stamped binding, then the registry row changes
        # before delivery:  _stale must re-verify, not trust the pick.
        self.registry.rows = [
            row(SID_A, "AB", "pr work", generation=1,
                bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 7,
                            "generation": 1}])
        ]
        w = wake(required_session=SID_A, require_pr=PR, require_generation=1)
        pick = self.router.pick(w, consult=False)
        self.assertEqual(pick.reason, "required-session")
        self.registry.rows = after_rows
        return self.router._stale(pick.target, w)

    def test_still_bound_delivers(self):
        reason, _, _ = self._pick_and_stale(
            [row(SID_A, "AB", "pr work", generation=1,
                 bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 7,
                             "generation": 1}])])
        self.assertEqual(reason, "")

    def test_binding_removed_before_delivery_holds(self):
        reason, _, _ = self._pick_and_stale(
            [row(SID_A, "AB", "pr work", generation=1)])
        self.assertEqual(reason, "required-pr-unbound")

    def test_binding_replaced_with_other_pr_holds(self):
        reason, _, _ = self._pick_and_stale(
            [row(SID_A, "AB", "pr work", generation=1,
                 bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 9,
                             "generation": 1}])])
        self.assertEqual(reason, "required-pr-unbound")

    def test_binding_stamp_stale_at_delivery_holds(self):
        # Row re-generated to 2 after the binding was stamped 1.  The stale
        # check itself passes (resolve answers by letter with the CURRENT
        # row:  session/generation still match the pick here only when the
        # row is unchanged), so drive the recheck directly:  same pick, but
        # the resolve answer's binding stamp no longer matches.
        self.registry.rows = [
            row(SID_A, "AB", "pr work", generation=1,
                bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 7,
                            "generation": 1}])
        ]
        w = wake(required_session=SID_A, require_pr=PR, require_generation=1)
        pick = self.router.pick(w, consult=False)
        self.assertEqual(pick.reason, "required-session")
        # The binding is rewritten with a stale stamp (same row generation).
        self.registry.rows = [
            row(SID_A, "AB", "pr work", generation=1,
                bound_prs=[{"repo": "Simple-With-Us/HogHunter", "number": 7,
                            "generation": 0}])
        ]
        reason, _, _ = self.router._stale(pick.target, w)
        self.assertEqual(reason, "required-pr-unbound")

    def test_no_require_pr_wake_skips_recheck(self):
        self.registry.rows = [row(SID_A, "AB", "pr work", generation=1)]
        w = wake(required_session=SID_A)
        pick = self.router.pick(w, consult=False)
        reason, _, _ = self.router._stale(pick.target, w)
        self.assertEqual(reason, "")
