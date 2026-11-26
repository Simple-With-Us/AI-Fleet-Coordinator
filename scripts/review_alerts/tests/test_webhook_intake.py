import hashlib, hmac, unittest
from review_alerts import webhook_intake as w

S = b"s3cret-test"
AL = frozenset({"o/r"})
def sig(b): return "sha256=" + hmac.new(S, b, hashlib.sha256).hexdigest()
def R(**k): return {"repository": {"full_name": "o/r"}, **k}

class T(unittest.TestCase):
    def test_verify(self):
        b = b'{"a":1}'
        self.assertTrue(w.verify(b, sig(b), S))
        self.assertFalse(w.verify(b + b" ", sig(b), S))
        self.assertFalse(w.verify(b, None, S))
        self.assertFalse(w.verify(b, "sha1=00", S))
        self.assertFalse(w.verify(b, sig(b)[:-1], S))
        self.assertFalse(w.verify(b, sig(b).upper().replace("SHA256", "sha256"), S))
        self.assertFalse(w.verify(b, " " + sig(b), S))
        self.assertFalse(w.verify(b, sig(b), b""))
        big = b"x" * (w.MAX_BODY + 1)
        self.assertFalse(w.verify(big, sig(big), S))
    def test_review_hint_fields(self):
        sha = "a" * 40
        r = w.map_event("pull_request_review", "d1", R(action="submitted", pull_request={"number": 5, "head": {"sha": sha}}, review={"id": 99, "submitted_at": "2026-11-25T23:00:00Z"}), AL)
        self.assertEqual((r.outcome, r.event_name, r.delivery_id, r.repo, r.subject_number), ("mapped", "pull_request_review", "d1", "o/r", 5))
        self.assertEqual(r.hints[0], w.Hint("o/r", 5, "pr", "pr_review", "d1", "2026-11-25T23:00:00Z", sha, "99", "submitted"))
    def test_missing_time_stays_empty(self):
        r = w.map_event("pull_request", "d", R(number=5, pull_request={"number": 5}), AL)
        self.assertEqual(r.hints[0].source_time, "")
    def test_gates(self):
        p = R(number=5, pull_request={"number": 5})
        self.assertEqual(w.map_event("pull_request", "d", p, frozenset()).outcome, "ignored")
        self.assertEqual(w.map_event("pull_request", "d", p).outcome, "ignored")
        self.assertEqual(w.map_event("push", "d", p, AL).reason, "event-not-supported")
        self.assertEqual(w.map_event("pull_request", "d", {"repository": {"full_name": "x/y"}, "number": 1}, AL).reason, "repo-not-allowlisted")
        self.assertEqual(w.map_event("pull_request", "d", {}, AL).outcome, "invalid")
        self.assertEqual(w.map_event("pull_request", "d", R(), AL).outcome, "invalid")
        self.assertEqual(w.map_event(None, "d", p, AL).outcome, "invalid")
        self.assertEqual(w.map_event("x" * 65, "d", p, AL).event_name, "")
    def test_no_dedupe(self):
        p = R(number=5, pull_request={"number": 5})
        a = w.map_event("pull_request", "same", p, AL); b = w.map_event("pull_request", "same", p, AL)
        self.assertEqual((a.outcome, b.outcome), ("mapped", "mapped"))
    def test_discovery_held(self):
        for ev, k in (("check_run", "check_run"), ("check_suite", "check_suite")):
            r = w.map_event(ev, "d", R(**{k: {"pull_requests": []}}), AL)
            self.assertEqual((r.outcome, r.hints), ("held", ()))
        r = w.map_event("merge_group", "d", R(merge_group={"head_ref": "refs/heads/odd"}), AL)
        self.assertEqual(r.outcome, "held")
    def test_check_run_multi_and_merge_group_held(self):
        r = w.map_event("check_run", "d", R(check_run={"id": 7, "pull_requests": [{"number": 4}, {"number": 3}]}), AL)
        self.assertEqual([h.number for h in r.hints], [3, 4])
        sha = "b" * 40
        r = w.map_event("merge_group", "d", R(merge_group={"head_sha": sha, "head_ref": "refs/heads/gh-readonly-queue/main/pr-77-abc"}), AL)
        self.assertEqual((r.outcome, r.hints, r.advisory_head_sha), ("held", (), sha))
    def test_delivery_id_strict_never_truncated(self):
        p = R(number=5, pull_request={"number": 5})
        for bad in ("", None, 5, "x" * 129, "has space", "a\nb"):
            r = w.map_event("pull_request", bad, p, AL)
            self.assertEqual((r.outcome, r.reason, r.delivery_id, r.hints), ("invalid", "delivery-id-invalid", "", ()))
        did = "x" * 128
        r = w.map_event("pull_request", did, p, AL)
        self.assertEqual((r.delivery_id, r.hints[0].source_event_id), (did, did))
    def test_malformed_nested_shapes(self):
        cases = [
            ("pull_request", R(pull_request="str")),
            ("pull_request_review", R(pull_request=[1], review={})),
            ("pull_request", R(pull_request={"number": 5}, review=7, action=3)),
            ("check_run", R(check_run={"pull_requests": 5})),
            ("check_run", R(check_run="x")),
            ("check_suite", R(check_suite={"pull_requests": [{"number": 3}, "bad"]})),
            ("check_run", R(check_run={"pull_requests": [{"number": 3}, {"number": "4"}]})),
            ("issues", R(issue="str")),
            ("merge_group", R(merge_group=3)),
            ("pull_request", {"repository": "o/r", "number": 1}),
            ("pull_request", R(pull_request={"number": 5, "head": 4}, review="x")),
        ]
        for ev, p in cases:
            r = w.map_event(ev, "d", p, AL)
            self.assertEqual((r.outcome, r.hints), ("invalid", ()), (ev, p, r))
    def test_bounds(self):
        for n in (10**70, 0, -1, True, 1.5, w.MAX_NUMBER + 1):
            self.assertEqual(w.map_event("pull_request", "d", R(pull_request={"number": n}), AL).outcome, "invalid", n)
            self.assertEqual(w.map_event("issues", "d", R(issue={"number": n}), AL).outcome, "invalid", n)
        self.assertEqual(w.map_event("pull_request", "d", R(pull_request={"number": w.MAX_NUMBER}), AL).outcome, "mapped")
        big = {"repository": {"full_name": "o/" + "r" * 200}, "number": 1}
        self.assertEqual(w.map_event("pull_request", "d", big, frozenset({big["repository"]["full_name"]})).outcome, "invalid")
        r = w.map_event("pull_request", "d", R(pull_request={"number": 5, "updated_at": "t" * 41}), AL)
        self.assertEqual((r.outcome, r.reason), ("invalid", "time-oversized"))
        r = w.map_event("pull_request", "d", R(action="a" * 41, pull_request={"number": 5}), AL)
        self.assertEqual(r.outcome, "invalid")
        many = [{"number": i} for i in range(1, w.MAX_TARGETS + 2)]
        r = w.map_event("check_run", "d", R(check_run={"pull_requests": many}), AL)
        self.assertEqual((r.outcome, r.reason, r.hints), ("invalid", "too-many-targets", ()))
        ok = [{"number": i} for i in range(1, w.MAX_TARGETS + 1)]
        self.assertEqual(len(w.map_event("check_run", "d", R(check_run={"pull_requests": ok}), AL).hints), w.MAX_TARGETS)
    def test_issue_subjects(self):
        a = w.map_event("issues", "d", R(issue={"number": 9}), AL).hints[0]
        b = w.map_event("issue_comment", "d", R(issue={"number": 10, "pull_request": {}}), AL).hints[0]
        c = w.map_event("issue_comment", "d", R(issue={"number": 11}), AL).hints[0]
        self.assertEqual((a.subject, a.hint_kind), ("issue", "issue"))
        self.assertEqual((b.subject, b.hint_kind), ("pr", "pr_comment"))
        self.assertEqual((c.subject, c.hint_kind), ("issue", "issue_comment_issue"))
        self.assertEqual(w.map_event("issues", "d", R(issue={}), AL).outcome, "invalid")
    def test_reason_bounded(self):
        r = w.map_event("x" * 500, "d" * 500, R(), AL)
        self.assertLessEqual(len(r.reason), w.MAX_REASON); self.assertEqual((r.event_name, r.delivery_id), ("", ""))
    def test_trailing_newline_rejected_everywhere(self):
        b = b"{}"
        self.assertFalse(w.verify(b, sig(b) + "\n", S))
        p = R(number=5, pull_request={"number": 5})
        self.assertEqual(w.map_event("pull_request", "abc\n", p, AL).reason, "delivery-id-invalid")
        bad = {"repository": {"full_name": "o/r\n"}, "number": 5, "pull_request": {"number": 5}}
        self.assertEqual(w.map_event("pull_request", "d", bad, frozenset({"o/r\n"})).reason, "repo-missing-or-malformed")
        sha = "a" * 40
        r = w.map_event("pull_request", "d", R(pull_request={"number": 5, "head": {"sha": sha + "\n"}}), AL)
        self.assertEqual((r.outcome, r.hints[0].head_sha), ("mapped", None))
        r = w.map_event("merge_group", "d", R(merge_group={"head_sha": sha + "\n"}), AL)
        self.assertEqual((r.outcome, r.advisory_head_sha), ("held", None))
if __name__ == "__main__": unittest.main()
