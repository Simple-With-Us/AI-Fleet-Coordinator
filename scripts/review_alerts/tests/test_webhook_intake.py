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
    def test_check_run_and_merge_group(self):
        r = w.map_event("check_run", "d", R(check_run={"id": 7, "pull_requests": [{"number": 4}, {"number": 3}]}), AL)
        self.assertEqual([h.number for h in r.hints], [3, 4])
        r = w.map_event("merge_group", "d", R(merge_group={"head_ref": "refs/heads/gh-readonly-queue/main/pr-77-abc"}), AL)
        self.assertEqual((r.hints[0].number, r.hints[0].hint_kind), (77, "merge_group"))
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
        self.assertLessEqual(len(r.reason), w.MAX_REASON); self.assertLessEqual(len(r.event_name), 64)
if __name__ == "__main__": unittest.main()
