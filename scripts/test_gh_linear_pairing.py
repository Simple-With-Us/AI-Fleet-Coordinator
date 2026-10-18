"""Tests for gh_linear_pairing.py:  key parsing, the drift classes, plan order, and the gated applier.

Everything is offline:  GitHub and Linear are fakes, and no subprocess or network call is made.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_linear_pairing as P

REPOS = {P.repo_name(r) for r in P.REPOS}


def gh(
    repo="Simple-With-Us/BotFleet",
    number=1,
    state="open",
    title="Real work",
    author="jaywedgeworth22",
    labels=(),
    archived=False,
):
    return P.GhIssue(
        repo=repo,
        number=number,
        node_id=f"I_{repo}_{number}",
        url=f"https://github.com/{repo}/issues/{number}",
        state=state,
        state_reason=None,
        title=title,
        author=author,
        labels=list(labels),
        created_at="2026-10-01T00:00:00Z",
        archived=archived,
    )


def lin(identifier="BF-1", team="BF", state="unstarted", footer=(), attached=(), labelled=True):
    return P.LinIssue(
        id=f"id-{identifier}",
        identifier=identifier,
        team=team,
        state_type=state,
        title="t",
        created_at="2026-10-01T00:00:00Z",
        labelled=labelled,
        footer_keys=set(footer),
        attached_keys=set(attached),
    )


class ParsingTest(unittest.TestCase):
    def test_footer_forms(self):
        text = (
            "body\n<!-- migrated-from: gh:Simple-With-Us/BotFleet#987 -->\n"
            "<!-- migrated-from: board:abc123 gh:jaywedgeworth22/Fleet-OPS#4 -->"
        )
        self.assertEqual(P.footer_keys(text), {("botfleet", 987), ("fleet-ops", 4)})

    def test_key_outside_a_footer_is_ignored(self):
        self.assertEqual(P.footer_keys("see gh:Simple-With-Us/BotFleet#1 in passing"), set())

    def test_url_key(self):
        self.assertEqual(
            P.url_key("https://github.com/jaywedgeworth22/Socratic-Trade/issues/12"),
            ("socratic-trade", 12),
        )
        self.assertEqual(
            P.url_key("https://github.com/Simple-With-Us/BotFleet/issues/5#issuecomment-1"),
            ("botfleet", 5),
        )
        self.assertIsNone(P.url_key("https://github.com/Simple-With-Us/BotFleet/pull/5"))
        self.assertIsNone(P.url_key("https://linear.app/simple-with-us/issue/BF-5"))

    def test_noise(self):
        self.assertEqual(
            P.noise_kind(gh("Simple-With-Us/Socratic-Trade", title="Merge shepherd status")),
            "shepherd",
        )
        self.assertEqual(
            P.noise_kind(gh(labels=["effort-board"], author="github-actions[bot]")), "effort-board"
        )
        self.assertEqual(
            P.noise_kind(gh(title="Dependency Dashboard", author="renovate[bot]")), "renovate"
        )
        self.assertIsNone(P.noise_kind(gh(title="Merge shepherd status")))  # only Socratic-Trade's
        self.assertIsNone(P.noise_kind(gh(labels=["effort-board"])))  # a person's issue is real


class ClassifyTest(unittest.TestCase):
    def test_footer_pair_is_unlinked_until_attached(self):
        p = P.classify([gh()], [lin(footer=[("botfleet", 1)])], REPOS)
        self.assertEqual(len(p.pairs), 1)
        self.assertEqual(len(p.drift["unlinked"]), 1)
        p = P.classify([gh()], [lin(attached=[("botfleet", 1)])], REPOS)
        self.assertEqual(p.drift["unlinked"], [])
        self.assertEqual(p.totals["linked"], 1)

    def test_owner_is_only_a_hint(self):
        p = P.classify(
            [gh("jaywedgeworth22/Fleet-OPS", 4)],
            [lin("OPS-1", "OPS", footer=[("fleet-ops", 4)])],
            REPOS,
        )
        self.assertEqual(len(p.pairs), 1)

    def test_missing_twin_excludes_noise(self):
        issues = [
            gh(number=2),
            gh("Simple-With-Us/Socratic-Trade", 9, title="Merge shepherd status"),
        ]
        p = P.classify(issues, [], REPOS)
        self.assertEqual([d["github"] for d in p.drift["missing_twin"]], [issues[0].url])
        self.assertEqual([i.number for i in p.noise_unpaired["shepherd"]], [9])

    def test_two_twins_conflict(self):
        p = P.classify(
            [gh()],
            [lin("BF-1", footer=[("botfleet", 1)]), lin("BF-2", footer=[("botfleet", 1)])],
            REPOS,
        )
        self.assertEqual(p.pairs, [])
        self.assertEqual(p.drift["conflict"][0]["linear"], ["BF-1", "BF-2"])

    def test_one_twin_claiming_two_issues_conflicts(self):
        p = P.classify(
            [gh(number=1), gh(number=2)], [lin(footer=[("botfleet", 1), ("botfleet", 2)])], REPOS
        )
        self.assertEqual(len(p.drift["conflict"]), 1)

    def test_attachment_beats_footer(self):
        p = P.classify(
            [gh(number=1), gh(number=2)],
            [lin(footer=[("botfleet", 1)], attached=[("botfleet", 2)])],
            REPOS,
        )
        self.assertEqual(p.pairs[0][0].number, 2)
        self.assertEqual(len(p.drift["footer_disagrees"]), 1)

    def test_state_drift_both_ways(self):
        p = P.classify(
            [gh(number=1, state="open"), gh(number=2, state="closed")],
            [
                lin("BF-1", state="completed", footer=[("botfleet", 1)]),
                lin("BF-2", state="started", footer=[("botfleet", 2)]),
            ],
            REPOS,
        )
        self.assertEqual([d["linear"] for d in p.drift["gh_open_linear_closed"]], ["BF-1"])
        self.assertEqual([d["linear"] for d in p.drift["gh_closed_linear_open"]], ["BF-2"])

    def test_orphans(self):
        p = P.classify(
            [],
            [
                lin("CC-164", "CC"),
                lin("FL-12", "FL", footer=[("fleetlink-legacy", 3)]),
                lin("BF-9", footer=[("botfleet", 404)]),
                lin("AFC-1", "AFC", labelled=False),
            ],
            REPOS,
        )
        self.assertEqual(
            {d["linear"]: d["why"] for d in p.drift["orphan_linear"]},
            {
                "CC-164": "no import footer",
                "FL-12": "repo not paired",
                "BF-9": "GitHub issue not found",
            },
        )

    def test_team_mismatch_and_exceptions(self):
        p = P.classify(
            [gh(number=1), gh("Simple-With-Us/Simple-With-Us", 13), gh("Simple-With-Us/Clutch", 4)],
            [
                lin("AFC-9", "AFC", footer=[("botfleet", 1)]),
                lin("AFC-56", "AFC", footer=[("simple-with-us", 13)]),
                lin("C-4", "C", footer=[("clutch", 4)]),
            ],
            REPOS,
        )
        self.assertEqual(
            [(d["linear"], d["expected"]) for d in p.drift["team_mismatch"]], [("AFC-9", "BF")]
        )


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.issues = [
            gh(number=1, state="closed"),
            gh(number=2),
            gh(number=3),
            gh("Simple-With-Us/Socratic-Trade", 7, title="Merge shepherd status"),
            gh("Simple-With-Us/Socratic-Trade", 8, title="Merge shepherd status"),
            gh(number=4),
            gh("Simple-With-Us/Autorotate", 5, archived=True),
        ]
        self.twins = [
            lin("BF-1", state="completed", footer=[("botfleet", 1)]),
            lin("BF-2", footer=[("botfleet", 2)]),
            lin("BF-3", state="duplicate", footer=[("botfleet", 3)]),
            lin("ST-7", "ST", footer=[("socratic-trade", 7)]),
            lin("AR-5", "AR", state="completed", footer=[("autorotate", 5)]),
        ]
        self.plans = P.build_plans(P.classify(self.issues, self.twins, REPOS), self.issues)

    def test_link_order_real_open_first_noise_last(self):
        # open/open, then drifted (by repo, then number), then closed/closed, then noise
        self.assertEqual(
            [op["linear"] for op in self.plans["link"]], ["BF-2", "AR-5", "BF-3", "BF-1", "ST-7"]
        )

    def test_attachment_id_is_stable(self):
        self.assertEqual(P.attachment_id("I_x"), P.attachment_id("I_x"))
        self.assertNotEqual(P.attachment_id("I_x"), P.attachment_id("I_y"))

    def test_create_only_real_missing(self):
        self.assertEqual([(op["number"], op["team"]) for op in self.plans["create"]], [(4, "BF")])

    def test_close_parity_and_shepherd_but_not_archived(self):
        closes = {op["number"]: op["state_reason"] for op in self.plans["close-github"]}
        self.assertEqual(closes, {3: "not_planned", 7: "not_planned", 8: "not_planned"})

    def test_cancel_shepherd_twins(self):
        self.assertEqual([op["linear"] for op in self.plans["cancel-linear"]], ["ST-7"])


class FakeReader:
    def __init__(self, holders=None, state="unstarted"):
        self.holders, self.state, self.calls = holders or {}, state, []

    def call(self, query, variables=None):
        self.calls.append(query)
        if "attachmentsForURL" in query:
            return {
                "attachmentsForURL": {
                    "nodes": [
                        {"issue": {"identifier": i}} for i in self.holders.get(variables["url"], [])
                    ]
                }
            }
        if "teams" in query:
            return {
                "teams": {
                    "nodes": [
                        {
                            "id": "team-bf",
                            "key": "BF",
                            "states": {
                                "nodes": [
                                    {"id": "s-todo", "name": "Todo", "type": "unstarted"},
                                    {"id": "s-done", "name": "Done", "type": "completed"},
                                    {"id": "s-cxl", "name": "Canceled", "type": "canceled"},
                                ]
                            },
                        }
                    ]
                }
            }
        if "issueLabels" in query:
            return {"issueLabels": {"nodes": [{"id": "label-gh"}]}}
        if "issue(id" in query:
            return {
                "issue": {
                    "id": variables["id"],
                    "identifier": "BF-2",
                    "team": {"key": "BF"},
                    "state": {"type": self.state},
                }
            }
        raise AssertionError(query)


class FakeGitHub:
    def __init__(self, state="open"):
        self.state, self.calls = state, []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {
            "state": self.state,
            "title": "T",
            "body": "B",
            "number": 4,
            "created_at": "2026-10-12T00:00:00Z",
            "html_url": "https://github.com/Simple-With-Us/BotFleet/issues/4",
            "user": {"login": "jay"},
        }, {}


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.mutations = []

    def mutate(self, doc, variables, key):
        self.mutations.append(
            (doc.split("(")[0].split()[-1] if "mutation" in doc else doc, variables, key)
        )
        if "attachmentLinkGitHubIssue" in doc:
            return {"attachmentLinkGitHubIssue": {"success": True}}
        if "issueCreate" in doc:
            return {
                "issueCreate": {"success": True, "issue": {"id": "new-id", "identifier": "BF-99"}}
            }
        return {"issueUpdate": {"success": True}}

    def applier(self, plan, reader, github):
        path = self.dir / f"plan-{plan}.jsonl"
        return P.Applier(
            path, reader, github, mutate=self.mutate, sleep=lambda s: None, log=lambda m: None
        )

    def link_op(self, n=2):
        return {
            "op": "link",
            "linear": f"BF-{n}",
            "linear_id": f"id-BF-{n}",
            "noise": None,
            "github": f"https://github.com/Simple-With-Us/BotFleet/issues/{n}",
            "attachment_id": f"att-{n}",
        }

    def test_link_then_rerun_skips(self):
        a = self.applier("link", FakeReader(), FakeGitHub())
        self.assertEqual(a.run([self.link_op()]), {"ok": 1})
        self.assertEqual(a.run([self.link_op()]), {})
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.mutations[0][1]["id"], "att-2")

    def test_link_already_attached_or_held_elsewhere(self):
        url = self.link_op()["github"]
        self.assertEqual(
            self.applier("link", FakeReader({url: ["BF-2"]}), FakeGitHub()).run([self.link_op()]),
            {"already": 1},
        )
        self.assertEqual(
            self.applier("link2", FakeReader({url: ["BF-7"]}), FakeGitHub()).run([self.link_op()]),
            {"conflict": 1},
        )
        self.assertEqual(self.mutations, [])

    def test_limit_counts_successful_writes(self):
        a = self.applier("link", FakeReader(), FakeGitHub())
        self.assertEqual(a.run([self.link_op(n) for n in (2, 3, 4)], limit=2), {"ok": 2})

    def test_only_real_skips_noise(self):
        op = dict(self.link_op(), noise="shepherd")
        self.assertEqual(
            self.applier("link", FakeReader(), FakeGitHub()).run([op], only_real=True), {}
        )

    def test_create_makes_twin_then_links(self):
        op = {
            "op": "create",
            "github": "https://github.com/Simple-With-Us/BotFleet/issues/4",
            "repo": "Simple-With-Us/BotFleet",
            "number": 4,
            "team": "BF",
            "attachment_id": "att-4",
        }
        self.assertEqual(self.applier("create", FakeReader(), FakeGitHub()).run([op]), {"ok": 1})
        created = self.mutations[0][1]["input"]
        self.assertEqual(
            (created["teamId"], created["stateId"], created["labelIds"]),
            ("team-bf", "s-todo", ["label-gh"]),
        )
        self.assertIn(
            "<!-- migrated-from: gh:Simple-With-Us/BotFleet#4 -->", created["description"]
        )
        self.assertEqual(
            self.mutations[1][1], {"issueId": "new-id", "url": op["github"], "id": "att-4"}
        )

    def test_create_for_closed_issue_is_done(self):
        op = {
            "op": "create",
            "github": "https://github.com/Simple-With-Us/BotFleet/issues/4",
            "repo": "Simple-With-Us/BotFleet",
            "number": 4,
            "team": "BF",
            "attachment_id": "att-4",
        }
        self.applier("create", FakeReader(), FakeGitHub(state="closed")).run([op])
        self.assertEqual(self.mutations[0][1]["input"]["stateId"], "s-done")

    def test_close_github_needs_its_own_gate(self):
        op = {
            "op": "close_github",
            "github": "https://github.com/Simple-With-Us/BotFleet/issues/3",
            "repo": "Simple-With-Us/BotFleet",
            "number": 3,
            "state_reason": "not_planned",
            "noise": None,
        }
        with (
            mock.patch.dict(os.environ, {"PAIRING_GITHUB_WRITES": ""}),
            self.assertRaises(SystemExit),
        ):
            self.applier("close", FakeReader(), FakeGitHub()).run([op])
        fake = FakeGitHub()
        with mock.patch.dict(os.environ, {"PAIRING_GITHUB_WRITES": "1"}):
            self.assertEqual(self.applier("close", FakeReader(), fake).run([op]), {"ok": 1})
        self.assertEqual(
            fake.calls[-1],
            (
                "PATCH",
                "/repos/Simple-With-Us/BotFleet/issues/3",
                {"state": "closed", "state_reason": "not_planned"},
            ),
        )
        with mock.patch.dict(os.environ, {"PAIRING_GITHUB_WRITES": "1"}):
            self.assertEqual(
                self.applier("close2", FakeReader(), FakeGitHub(state="closed")).run([op]),
                {"already": 1},
            )

    def test_cancel_skips_closed_twin(self):
        op = {
            "op": "cancel_linear",
            "linear": "BF-2",
            "linear_id": "id-BF-2",
            "team": "BF",
            "github": "https://github.com/Simple-With-Us/Socratic-Trade/issues/7",
        }
        self.assertEqual(
            self.applier("cancel", FakeReader(state="canceled"), FakeGitHub()).run([op]),
            {"already": 1},
        )
        self.assertEqual(self.applier("cancel2", FakeReader(), FakeGitHub()).run([op]), {"ok": 1})
        self.assertEqual(self.mutations[-1][1], {"id": "id-BF-2", "stateId": "s-cxl"})

    def test_errors_are_recorded_and_retried(self):
        def broken(*a):
            raise RuntimeError("board: LINEAR_BOARD_WRITES is not 1")

        a = P.Applier(
            self.dir / "plan-x.jsonl",
            FakeReader(),
            FakeGitHub(),
            mutate=broken,
            sleep=lambda s: None,
            log=lambda m: None,
        )
        self.assertEqual(a.run([self.link_op()]), {"error": 1})
        rows = [
            json.loads(line) for line in (self.dir / "plan-x.ledger.jsonl").read_text().splitlines()
        ]
        self.assertEqual(rows[0]["result"], "error")
        self.assertEqual(a.done(), set())  # an error is not done, so a rerun tries again


if __name__ == "__main__":
    unittest.main()
