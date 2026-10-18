"""pr-sweep tests: no network, no agent CLI.  gh is a fake `exec_fn`; agents and the lane tool are fakes too.

cd scripts && python3 -m unittest discover -s pr_sweep/tests -t . -v
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pr_sweep import audit as A
from pr_sweep import cli as C
from pr_sweep import fleet as F
from pr_sweep import inventory as I
from pr_sweep import plan as P
from pr_sweep import prompt as PR
from pr_sweep import run as R
from pr_sweep import status as S
from pr_sweep.gh import Gh, GhError

NOW = dt.datetime(2026, 10, 17, 18, 0, 0, tzinfo=dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)
OLD = "2026-10-17T12:00:00Z"
RECENT = "2026-10-17T17:55:00Z"
REPO_ROOT = Path(__file__).resolve().parents[3]
SEAT = F.SeatInfo("CODEX", "codex", "codex")

REGISTRY = {
    "owner": "Simple-With-Us",
    "apps": [
        {"repo": "DealDex", "acronym": "DD", "worktreePrefix": "dealdex", "codeDir": "DealDex"},
        {"repo": "BotFleet", "acronym": "BF", "worktreePrefix": "botfleet", "codeDir": "BotFleet"},
    ],
    "seats": [
        {"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]},
        {"tag": "CODEX", "worktreeSuffix": "codex", "branchPrefixes": ["codex/"]},
        {"tag": "MONET", "worktreeSuffix": "monet", "branchPrefixes": ["monet/"], "retired": True},
    ],
}


def pr_node(n: int, **kw: Any) -> dict[str, Any]:
    node = {
        "number": n,
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "BLOCKED",
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "baseRefName": "main",
        "headRefName": f"feat/{n}",
        "headRefOid": "a" * 40,
        "updatedAt": OLD,
        "mergedAt": None,
        "url": f"https://github.com/Simple-With-Us/DealDex/pull/{n}",
        "title": f"PR {n}",
        "author": {"login": "jaywedgeworth22"},
        "autoMergeRequest": None,
        "labels": {"nodes": []},
        "isCrossRepository": False,
        "repository": {
            "name": "DealDex",
            "owner": {"login": "Simple-With-Us"},
            "isArchived": False,
        },
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": "a" * 40,
                        "committedDate": OLD,
                        "statusCheckRollup": {"state": "SUCCESS"},
                    }
                }
            ]
        },
        "reviewThreads": {"totalCount": 0, "nodes": []},
        "reviewRequests": {"nodes": []},
        "latestReviews": {"nodes": []},
    }
    for k, v in kw.items():
        if k == "ci":
            node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {"state": v} if v else None
        elif k == "unresolved":
            node["reviewThreads"] = {"totalCount": v, "nodes": [{"isResolved": False}] * v}
        elif k == "committed":
            node["commits"]["nodes"][0]["commit"]["committedDate"] = v
        else:
            node[k] = v
    return node


class FakeGh:
    """Answers status queries from `prs` ({owner/repo: {n: node}}), search queries from `pages`."""

    def __init__(self) -> None:
        self.prs: dict[str, dict[int, dict[str, Any]]] = {}
        self.pages: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []
        self.fail_repos: set = set()
        self.rest_answers: dict[str, Any] = {}

    def exec_fn(self, argv, stdin):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "api", "graphql"]:
            query = next(a[len("query=") :] for a in argv if a.startswith("query="))
            if "search(" in query:
                return 0, json.dumps({"data": {"search": self.pages.pop(0)}}), ""
            if "timelineItems" in query:
                var = {
                    a.split("=", 1)[0]: a.split("=", 1)[1]
                    for a in argv
                    if "=" in a and not a.startswith("query=")
                }
                node = self.prs.get(f"{var['owner']}/{var['repo']}", {}).get(int(var["n"]))
                pr = (
                    None
                    if node is None
                    else {
                        "state": node["state"],
                        "mergeCommit": None,
                        "commits": node["commits"],
                        "reviewThreads": {"nodes": []},
                        "reviewRequests": {"nodes": []},
                        "latestReviews": {"nodes": []},
                        "timelineItems": {"nodes": []},
                        "comments": {"nodes": []},
                    }
                )
                return 0, json.dumps({"data": {"repository": {"pullRequest": pr}}}), ""
            m = re.search(r'repository\(owner: "([^"]+)", name: "([^"]+)"\)', query)
            full = f"{m.group(1)}/{m.group(2)}"
            if full in self.fail_repos:
                return 1, "", "HTTP 502: Bad Gateway"
            nums = [int(x) for x in re.findall(r"p(\d+): pullRequest", query)]
            repo = {f"p{n}": self.prs.get(full, {}).get(n) for n in nums}
            return 0, json.dumps({"data": {"repository": repo}}), ""
        if argv[:4] == ["gh", "api", "-X", "GET"]:
            return 0, json.dumps(self.rest_answers.get(argv[4], {})), ""
        return 1, "", "unexpected call"

    def gh(self) -> Gh:
        return Gh(self.exec_fn, sleep=lambda s: None)


def fleet() -> F.Fleet:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(REGISTRY, tmp)
    tmp.close()
    return F.load({"FLEET_APPS_JSON": tmp.name})


# --------------------------------------------------------------------------- gh


class GhTests(unittest.TestCase):
    def test_mutations_are_refused_before_any_process(self) -> None:
        calls = []
        gh = Gh(lambda a, s: calls.append(a) or (0, "{}", ""))
        for doc in ("mutation { x }", "  subscription { y }", "# c\nmutation { z }"):
            with self.assertRaises(GhError):
                gh.graphql(doc)
        self.assertEqual(calls, [])

    def test_retries_a_502_then_gives_up(self) -> None:
        calls = []
        gh = Gh(lambda a, s: calls.append(a) or (1, "", "HTTP 502"), sleep=lambda s: None)
        with self.assertRaises(GhError):
            gh.graphql("query { viewer { login } }")
        self.assertEqual(len(calls), 3)

    def test_rest_is_get_only_and_paths_are_checked(self) -> None:
        calls = []
        gh = Gh(lambda a, s: calls.append(a) or (0, "[]", ""))
        gh.rest("repos/a/b/commits/abc")
        self.assertEqual(calls[0][:4], ["gh", "api", "-X", "GET"])
        for bad in ("repos/../x", "-X POST", "repos/a b"):
            with self.assertRaises(GhError):
                gh.rest(bad)


# --------------------------------------------------------------------------- inventory and status


class InventoryTests(unittest.TestCase):
    def test_records_and_pagination(self) -> None:
        fake = FakeGh()
        fake.pages = [
            {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [pr_node(1), {"not": "a pr"}],
            },
            {"pageInfo": {"hasNextPage": False}, "nodes": [pr_node(2, unresolved=2, ci="FAILURE")]},
        ]
        prs = I.inventory(fake.gh(), ["Simple-With-Us"], ["Simple-With-Us/DealDex"])
        self.assertEqual([p["n"] for p in prs], [1, 2])
        self.assertEqual(prs[1]["unresolved"], 2)
        self.assertEqual(prs[1]["ci"], "FAILURE")
        self.assertTrue(prs[0]["fleet"])
        self.assertIn("user:Simple-With-Us", " ".join(fake.calls[0]))

    def test_a_repeated_cursor_stops_instead_of_looping(self) -> None:
        fake = FakeGh()
        page = {"pageInfo": {"hasNextPage": True, "endCursor": "same"}, "nodes": []}
        fake.pages = [dict(page), dict(page), dict(page)]
        with self.assertRaises(GhError):
            I.search_owner(fake.gh(), "Simple-With-Us")

    def test_ci_state(self) -> None:
        self.assertEqual(I.ci_state({"state": "ERROR"}), "FAILURE")
        self.assertEqual(I.ci_state({"state": "EXPECTED"}), "PENDING")
        self.assertEqual(I.ci_state(None), "NONE")


class StatusTests(unittest.TestCase):
    def test_keyed_by_the_requested_numbers_and_a_missing_pr_is_an_error(self) -> None:
        fake = FakeGh()
        fake.prs["Simple-With-Us/DealDex"] = {1: pr_node(1), 3: pr_node(3, state="MERGED")}
        st = S.status_many(
            fake.gh(),
            ["Simple-With-Us/DealDex#1", "Simple-With-Us/DealDex#2", "Simple-With-Us/DealDex#3"],
            NOW,
        )
        self.assertEqual(
            list(st),
            ["Simple-With-Us/DealDex#1", "Simple-With-Us/DealDex#2", "Simple-With-Us/DealDex#3"],
        )
        self.assertEqual(st["Simple-With-Us/DealDex#1"]["state"], "OPEN")
        self.assertIn("error", st["Simple-With-Us/DealDex#2"])
        self.assertEqual(st["Simple-With-Us/DealDex#3"]["state"], "MERGED")
        self.assertEqual(sum(1 for c in fake.calls if "graphql" in c), 1, "one call per repo")

    def test_a_failed_repo_call_marks_every_pr_of_it(self) -> None:
        fake = FakeGh()
        fake.fail_repos.add("Simple-With-Us/BotFleet")
        fake.prs["Simple-With-Us/DealDex"] = {1: pr_node(1)}
        st = S.status_many(
            fake.gh(), ["Simple-With-Us/BotFleet#5", "Simple-With-Us/DealDex#1"], NOW
        )
        self.assertIn("error", st["Simple-With-Us/BotFleet#5"])
        self.assertNotIn("error", st["Simple-With-Us/DealDex#1"])

    def test_an_answer_carrying_another_number_is_an_error(self) -> None:
        fake = FakeGh()
        fake.prs["Simple-With-Us/DealDex"] = {1: pr_node(9)}
        st = S.status_many(fake.gh(), ["Simple-With-Us/DealDex#1"], NOW)
        self.assertIn("carried number 9", st["Simple-With-Us/DealDex#1"]["error"])

    def test_freshness_and_human_reviews(self) -> None:
        f = S.facts(
            pr_node(
                1,
                committed=RECENT,
                latestReviews={
                    "nodes": [
                        {"state": "CHANGES_REQUESTED", "author": {"login": "kody-ai[bot]"}},
                        {"state": "CHANGES_REQUESTED", "author": {"login": "jaywedgeworth22"}},
                    ]
                },
            ),
            NOW,
        )
        self.assertTrue(f["fresh"])
        self.assertEqual(f["human_changes_requested"], ["jaywedgeworth22"])
        self.assertFalse(S.facts(pr_node(2), NOW)["fresh"])

    def test_split_key(self) -> None:
        self.assertEqual(S.split_key("Simple-With-Us/DealDex#12"), ("Simple-With-Us/DealDex", 12))
        for bad in ("DealDex#1", "a/b/c#1", "a/b#x", "a/b"):
            with self.assertRaises(ValueError):
                S.split_key(bad)


class NeedsWorkTests(unittest.TestCase):
    def s(self, **kw: Any) -> dict[str, Any]:
        base = {
            "state": "OPEN",
            "mstate": "BLOCKED",
            "ci": "PENDING",
            "unresolved": 0,
            "review": "",
            "auto": True,
            "base": "main",
        }
        base.update(kw)
        return base

    def test_missing_status_raises_never_means_work(self) -> None:
        for s in (None, {"error": "boom"}):
            with self.assertRaises(S.StatusMissing):
                S.needs_work("merge", s)

    def test_decisions(self) -> None:
        cases = [
            (self.s(state="MERGED"), False),
            (self.s(unresolved=1), True),
            (self.s(mstate="DIRTY"), True),
            (self.s(mstate="BEHIND"), True),
            (self.s(ci="FAILURE"), True),
            (self.s(review="CHANGES_REQUESTED"), True),
            (self.s(mstate="CLEAN", ci="SUCCESS", auto=False), True),
            (self.s(mstate="CLEAN", ci="SUCCESS", auto=True), False),
            (self.s(mstate="BLOCKED", ci="SUCCESS"), True),
            (self.s(), False),
        ]
        for s, want in cases:
            with self.subTest(s=s):
                self.assertEqual(S.needs_work("merge", s)[0], want)
        self.assertFalse(S.needs_work("skip", self.s(unresolved=3))[0])

    def test_stacks_wait_for_an_open_parent_and_retarget_after_it(self) -> None:
        child = self.s(base="feat/1", mstate="CLEAN", ci="SUCCESS", auto=False)
        self.assertEqual(
            S.needs_work("merge", child, parent_state="OPEN"),
            (False, "stacked on an open parent PR: waits for it"),
        )
        self.assertTrue(S.needs_work("merge", child, parent_state="MERGED")[0])
        self.assertTrue(
            S.needs_work("merge", self.s(base="feat/1", unresolved=2), parent_state="OPEN")[0]
        )


# --------------------------------------------------------------------------- plan


def rec(n: int, **kw: Any) -> dict[str, Any]:
    r = {
        "owner": "Simple-With-Us",
        "repo": "DealDex",
        "full": "Simple-With-Us/DealDex",
        "n": n,
        "head": f"feat/{n}",
        "base": "main",
        "author": "jaywedgeworth22",
        "labels": [],
        "draft": False,
        "mstate": "BLOCKED",
        "ci": "SUCCESS",
        "unresolved": 0,
        "review": "",
        "title": f"PR {n}",
    }
    r.update(kw)
    return r


class PlanTests(unittest.TestCase):
    def test_classify_and_tier(self) -> None:
        self.assertEqual(P.classify(rec(1, draft=True))["mode"], "skip")
        self.assertEqual(P.classify(rec(1, labels=["Deferred"]))["mode"], "skip")
        self.assertEqual(P.classify(rec(1, cross_repo=True))["mode"], "skip")
        self.assertEqual(P.classify(rec(1, labels=["do-not-automerge"]))["mode"], "merge")
        self.assertEqual(P.tier(rec(1, author="renovate")), "small")
        self.assertEqual(P.tier(rec(1, review="CHANGES_REQUESTED")), "frontier")
        self.assertEqual(P.tier(rec(1, unresolved=5)), "frontier")
        self.assertEqual(P.tier(rec(1)), "mid")

    def test_stacks_stay_together_parent_first(self) -> None:
        prs = [rec(3, base="feat/2"), rec(2, base="feat/1"), rec(1), rec(9)]
        chains = P.build_chains(prs, {"DealDex": 2})
        self.assertEqual(len(chains), 2)
        stack = next(c for c in chains if any(p["n"] == 3 for p in c["items"]))
        self.assertEqual([p["n"] for p in stack["items"]][:3], [1, 2, 3])

    def test_chain_limits_and_counts(self) -> None:
        prs = [rec(n) for n in range(1, 6)] + [
            dict(rec(7), repo="BotFleet", full="Simple-With-Us/BotFleet")
        ]
        plan = P.make_plan(prs, {"DealDex": 3})
        self.assertEqual(
            plan["chains_per_repo"], {"Simple-With-Us/BotFleet": 1, "Simple-With-Us/DealDex": 3}
        )
        self.assertEqual(plan["counts"]["work"], 6)
        self.assertEqual(P.make_plan([rec(1, draft=True)])["skipped"][0]["why"], "draft")


# --------------------------------------------------------------------------- prompt and rules


class PromptTests(unittest.TestCase):
    def test_rules_parse_and_fill_completely(self) -> None:
        rules = PR.parse_rules(PR.load_sections()["RULES"])
        self.assertEqual(sorted(rules), list(range(1, 15)))
        text = PR.worker_prompt(
            rec(5, labels=["do-not-automerge"]), SEAT, trailer="End with: X-Trailer"
        )
        self.assertNotIn("{{", text)
        self.assertIn("sub-agent of the CODEX seat", text)
        self.assertIn("git checkout -B codex/pr<N>-sweep", text)
        self.assertIn("End with: X-Trailer", text)
        self.assertIn("lane retire pr<N>-sweep", text)
        self.assertIn("labels: do-not-automerge", text)
        for owner_ruling in (
            "The default is MERGE",
            "only stop AUTO-merge",
            "kody-ai[bot] (never a human",
            "reply to EVERY thread",
            "requesting a Codex fix",
            "never use --admin",
            "A MAJOR dependency bump",
        ):
            self.assertIn(owner_ruling, text)

    def test_common_rules_and_deploy(self) -> None:
        common = PR.common_rules(SEAT)
        self.assertIn("1. IDENTITY", common)
        self.assertIn("13. WRITING", common)
        self.assertNotIn("10. OWNER-NEEDED", common)
        dep = PR.deploy_prompt("Simple-With-Us/DealDex", [{"n": 4, "title": "x"}], SEAT)
        self.assertIn("DEPLOY CHECK for Simple-With-Us/DealDex", dep)
        self.assertIn("#4 x", dep)

    def test_stack_and_repair_notes(self) -> None:
        text = PR.worker_prompt(rec(3, base="feat/2"), SEAT, parent=rec(2), round_no=2)
        self.assertIn("STACK: this PR is stacked on branch feat/2 (PR #2)", text)
        self.assertIn("repair round 2", text)

    def test_no_adapter_skill_or_doc_carries_a_copy_of_a_rule(self) -> None:
        rules = PR.parse_rules(PR.load_sections()["RULES"])
        fragments = []
        for text in rules.values():
            body = re.sub(r"\{\{[A-Z_]+\}\}", "", text.split(": ", 1)[-1])
            fragments += [s.strip() for s in re.split(r"(?<=[.;])\s+", body) if len(s.strip()) > 70]
        places = list((REPO_ROOT / "scripts" / "pr_sweep" / "adapters").rglob("*"))
        places += [
            REPO_ROOT / "docs" / "fleet-skills" / "pr-sweep" / "SKILL.md",
            REPO_ROOT / "docs" / "PR-SWEEP.md",
        ]
        for path in places:
            if not path.is_file():
                continue
            body = path.read_text(encoding="utf-8")
            for frag in fragments:
                self.assertNotIn(
                    frag,
                    body,
                    f"{path} copies a rule from rules.md; point at `pr-sweep prompt` instead",
                )


# --------------------------------------------------------------------------- the run


class CommandTests(unittest.TestCase):
    def test_stdin_argument_and_file_shapes(self) -> None:
        argv, stdin = R.build_command(
            {
                "argv": ["claude", "-p"],
                "stdin": True,
                "model_flag": "--model",
                "models": {"mid": "sonnet"},
                "auto_flags": ["--yes"],
            },
            "PROMPT",
            tier="mid",
        )
        self.assertEqual((argv, stdin), (["claude", "-p", "--model", "sonnet"], "PROMPT"))
        argv, stdin = R.build_command(
            {"argv": ["agent", "-p", "{prompt}"], "auto_flags": ["--force"]},
            "PROMPT",
            auto_approve=True,
        )
        self.assertEqual((argv, stdin), (["agent", "-p", "--force", "PROMPT"], None))
        argv, _ = R.build_command(
            {"argv": ["grok", "--prompt-file", "{prompt_file}"]}, "P", prompt_file=Path("/x/p.txt")
        )
        self.assertEqual(argv, ["grok", "--prompt-file", "/x/p.txt"])
        with self.assertRaises(R.RunError):
            R.build_command({"argv": ["tool"]}, "P")
        self.assertIn("<prompt: 6 chars>", R.preview(["agent", "PROMPT"], "PROMPT", None))

    def test_every_shipped_platform_builds(self) -> None:
        for name, cfg in R.load_platforms()["platforms"].items():
            with self.subTest(name):
                argv, _ = R.build_command(
                    cfg, "PROMPT TEXT", prompt_file=Path("/tmp/p"), auto_approve=True
                )
                self.assertTrue(argv)
                self.assertNotIn("AGENT_LAUNCH_SEAT", " ".join(argv))
        with self.assertRaises(R.RunError):
            R.platform("clutch")
        with self.assertRaises(R.RunError):
            R.platform("nope")


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGh()
        self.launched: list[str] = []
        self.lane_calls: list[list[str]] = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs = Path(tmp.name)

    def sweep(self, apply: bool, merge_on_launch: bool = True, **kw: Any) -> R.Sweep:
        def launch(argv, stdin, timeout, log):
            m = re.search(r"PR #(\d+)", stdin or "")
            self.launched.append(m.group(1) if m else "deploy")
            if m and merge_on_launch:
                self.fake.prs["Simple-With-Us/DealDex"][int(m.group(1))]["state"] = "MERGED"
            return 0, ""

        def lane(argv):
            self.lane_calls.append(list(argv))
            return 0, ""

        cfg = {"argv": ["codex", "exec"], "stdin": True, "trailer": "T"}
        return R.Sweep(
            gh=self.fake.gh(),
            fleet=fleet(),
            seat=SEAT,
            platform_name="codex",
            cfg=cfg,
            apply=apply,
            launch=launch,
            lane=lane,
            clock=lambda: NOW,
            log_root=self.logs,
            **kw,
        )

    def test_dry_run_launches_nothing_and_shows_the_command(self) -> None:
        self.fake.prs["Simple-With-Us/DealDex"] = {
            1: pr_node(1, unresolved=1),
            2: pr_node(2, autoMergeRequest={"x": 1}, ci="PENDING"),
        }
        rep = self.sweep(False).sweep([rec(1), rec(2)])
        acts = {o["pr"]: o["action"] for o in rep["outcomes"]}
        self.assertEqual(
            acts,
            {
                "Simple-With-Us/DealDex#1": "would-launch",
                "Simple-With-Us/DealDex#2": "nothing-to-do",
            },
        )
        self.assertEqual(self.launched, [])
        self.assertIn(
            "codex exec",
            rep["outcomes"][0]["command"]
            if rep["outcomes"][0]["action"] == "would-launch"
            else rep["outcomes"][1]["command"],
        )

    def test_missing_status_launches_nothing_and_is_an_error(self) -> None:
        self.fake.prs["Simple-With-Us/DealDex"] = {}
        rep = self.sweep(True).sweep([rec(1)])
        self.assertEqual(rep["outcomes"][0]["action"], "status-missing")
        self.assertEqual(self.launched, [])
        self.assertTrue(rep["errors"])

    def test_fresh_activity_by_someone_else_skips(self) -> None:
        self.fake.prs["Simple-With-Us/DealDex"] = {1: pr_node(1, unresolved=1, updatedAt=RECENT)}
        rep = self.sweep(True).sweep([rec(1)])
        self.assertEqual(rep["outcomes"][0]["action"], "in-flight-skipped")
        self.assertEqual(self.launched, [])

    def test_apply_launches_reads_back_retires_the_lane_audits_and_deploy_checks(self) -> None:
        self.fake.prs["Simple-With-Us/DealDex"] = {1: pr_node(1, unresolved=1)}
        self.fake.rest_answers = {}
        rep = self.sweep(True).sweep([rec(1)])
        self.assertEqual(self.launched, ["1", "deploy"])
        out = rep["outcomes"][0]
        self.assertEqual(
            (out["action"], out["final"]["state"], out["lane"]), ("launched", "MERGED", "retired")
        )
        self.assertEqual(self.lane_calls, [["retire", "pr1-sweep", "--repo", "DealDex"]])
        self.assertEqual(rep["deploys"][0]["repo"], "Simple-With-Us/DealDex")

    def test_repair_rounds_rerun_only_what_still_needs_work_and_ignore_their_own_activity(
        self,
    ) -> None:
        self.fake.prs["Simple-With-Us/DealDex"] = {
            1: pr_node(1, unresolved=1),
            2: pr_node(2, unresolved=1),
        }
        sweep = self.sweep(True, merge_on_launch=False, rounds=2, deploy=False)
        orig = sweep.launch

        def launch(argv, stdin, timeout, log):
            rc = orig(argv, stdin, timeout, log)
            n = int(re.search(r"PR #(\d+)", stdin).group(1))
            node = self.fake.prs["Simple-With-Us/DealDex"][n]
            node["updatedAt"] = "2026-10-17T17:59:00Z"  # the worker's own push: fresh, but ours
            if n == 1:
                node["state"] = "MERGED"
            return rc

        sweep.launch = launch
        rep = sweep.sweep([rec(1), rec(2)])
        rounds = [(o["pr"][-1], o["round"], o["action"]) for o in rep["outcomes"]]
        self.assertIn(("2", 2, "launched"), rounds)
        self.assertNotIn(("1", 2, "launched"), rounds)


# --------------------------------------------------------------------------- audit


class AuditTests(unittest.TestCase):
    def pr(self, **kw: Any) -> dict[str, Any]:
        base = {
            "state": "MERGED",
            "commits": {"nodes": [{"commit": {"committedDate": "2026-10-17T10:00:00Z"}}]},
            "reviewThreads": {"nodes": []},
            "reviewRequests": {"nodes": []},
            "latestReviews": {"nodes": []},
            "timelineItems": {"nodes": []},
            "comments": {"nodes": []},
        }
        base.update(kw)
        return base

    def rules(self, res: dict[str, Any]) -> list[str]:
        return sorted({v["rule"] for v in res["violations"]})

    def test_clean_pr(self) -> None:
        self.assertEqual(A.check_pr(self.pr(), []), {"violations": [], "warnings": []})

    def test_force_push_and_dismissals(self) -> None:
        tl = {
            "nodes": [
                {
                    "__typename": "HeadRefForcePushedEvent",
                    "createdAt": "2026-10-17T11:00:00Z",
                    "actor": {"login": "x"},
                },
                {
                    "__typename": "ReviewDismissedEvent",
                    "createdAt": "2026-10-17T11:00:00Z",
                    "actor": {"login": "jaywedgeworth22"},
                    "review": {"author": {"login": "humanreviewer"}},
                },
                {
                    "__typename": "ReviewDismissedEvent",
                    "createdAt": "2026-10-17T11:00:00Z",
                    "actor": {"login": "jaywedgeworth22"},
                    "review": {"author": {"login": "kody-ai"}},
                },
                {
                    "__typename": "ReviewDismissedEvent",
                    "createdAt": "2026-10-17T11:00:00Z",
                    "actor": {"login": "stranger"},
                    "review": {"author": {"login": "kody-ai"}},
                },
            ]
        }
        res = A.check_pr(self.pr(timelineItems=tl), [])
        self.assertEqual([v["rule"] for v in res["violations"]], ["V1", "V2", "V2"])
        since = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)
        self.assertEqual(A.check_pr(self.pr(timelineItems=tl), [], since=since)["violations"], [])

    def test_threads_resolved_without_a_reply_or_later_commit(self) -> None:
        threads = {
            "nodes": [
                {
                    "isResolved": True,
                    "path": "a.py",
                    "resolvedBy": {"login": "jaywedgeworth22"},
                    "comments": {
                        "nodes": [
                            {"author": {"login": "kody-ai"}, "createdAt": "2026-10-17T11:00:00Z"}
                        ]
                    },
                },
                {
                    "isResolved": True,
                    "path": "b.py",
                    "comments": {
                        "nodes": [
                            {"author": {"login": "kody-ai"}, "createdAt": "2026-10-17T11:00:00Z"},
                            {
                                "author": {"login": "jaywedgeworth22"},
                                "createdAt": "2026-10-17T11:05:00Z",
                            },
                        ]
                    },
                },
                {
                    "isResolved": True,
                    "path": "c.py",
                    "comments": {
                        "nodes": [
                            {"author": {"login": "kody-ai"}, "createdAt": "2026-10-17T09:00:00Z"}
                        ]
                    },
                },
            ]
        }
        res = A.check_pr(self.pr(reviewThreads=threads), [])
        self.assertEqual([v["evidence"].split(" ")[2] for v in res["violations"]], ["a.py"])

    def test_merged_with_open_requests_markers_and_safety_files(self) -> None:
        pr = self.pr(
            latestReviews={
                "nodes": [{"state": "CHANGES_REQUESTED", "author": {"login": "person"}}]
            },
            reviewRequests={"nodes": [{"requestedReviewer": {"login": "another"}}]},
            reviewThreads={"nodes": [{"isResolved": False, "comments": {"nodes": []}}]},
        )
        files = [
            {
                "filename": "x.py",
                "status": "modified",
                "patch": "@@\n+<<<<<<< HEAD\n+ok\n+=======\n",
            },
            {"filename": ".github/CODEOWNERS", "status": "modified", "patch": ""},
            {"filename": ".oxlint-baseline.json", "status": "modified", "patch": ""},
            {"filename": "tests/test_x.py", "status": "removed", "patch": ""},
        ]
        res = A.check_pr(pr, files)
        self.assertEqual(self.rules(res), ["V4", "V5"])
        self.assertEqual(sorted(w["rule"] for w in res["warnings"]), ["V6", "V6", "V7"])

    def test_fix_requests_to_bots(self) -> None:
        comments = {
            "nodes": [
                {"author": {"login": "jaywedgeworth22"}, "body": "@codex review", "createdAt": OLD},
                {
                    "author": {"login": "jaywedgeworth22"},
                    "body": "@codex fix the lint",
                    "createdAt": OLD,
                },
                {"author": {"login": "kody-ai[bot]"}, "body": "@codex fix", "createdAt": OLD},
            ]
        }
        self.assertEqual(self.rules(A.check_pr(self.pr(comments=comments), [])), ["V8"])


# --------------------------------------------------------------------------- the CLI


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str, env: dict[str, str] | None = None, gh: Gh | None = None):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(REGISTRY, tmp)
        tmp.close()
        e = {"FLEET_APPS_JSON": tmp.name, "AGENT_SEAT": "CODEX"}
        e.update(env or {})
        e = {k: v for k, v in e.items() if v is not None}
        out, err = io.StringIO(), io.StringIO()
        rc = C.main(list(argv), env=e, gh=gh or FakeGh().gh(), stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def test_prompt_offline_and_seat_rules(self) -> None:
        rc, out, _ = self.run_cli(
            "prompt", "Simple-With-Us/DealDex#4", "--offline", "--platform", "claude"
        )
        self.assertEqual(rc, 0)
        self.assertIn("CODEX seat", out)
        self.assertIn("Co-Authored-By: Claude", out)
        self.assertEqual(
            self.run_cli(
                "prompt", "Simple-With-Us/DealDex#4", "--offline", env={"AGENT_SEAT": None}
            )[0],
            64,
        )
        self.assertEqual(
            self.run_cli(
                "prompt", "Simple-With-Us/DealDex#4", "--offline", env={"AGENT_SEAT": "MONET"}
            )[0],
            64,
        )
        self.assertEqual(
            self.run_cli("prompt", "Simple-With-Us/DealDex#4", "--offline", "--platform", "nope")[
                0
            ],
            64,
        )

    def test_status_missing_is_exit_69(self) -> None:
        rc, out, err = self.run_cli("status", "Simple-With-Us/DealDex#4")
        self.assertEqual(rc, 69)
        self.assertEqual(json.loads(out)["missing"], ["Simple-With-Us/DealDex#4"])

    def test_run_needs_a_mode_and_a_known_platform(self) -> None:
        self.assertEqual(self.run_cli("run", "--platform", "codex")[0], 64)
        self.assertEqual(
            self.run_cli("run", "--platform", "clutch", "--dry-run", "Simple-With-Us/DealDex#4")[0],
            64,
        )

    def test_shim_runs_isolated(self) -> None:
        import subprocess

        shim = REPO_ROOT / "scripts" / "pr-sweep"
        proc = subprocess.run(
            [str(shim), "--help"], capture_output=True, text=True, cwd="/", check=False
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("needs-work", proc.stdout)


if __name__ == "__main__":
    unittest.main()
