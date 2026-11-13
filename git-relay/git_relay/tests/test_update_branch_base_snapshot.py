"""The actual update-branch guard proves an immutable named-ref snapshot before writing."""

import copy
import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.server import PreReadUnavailable
from git_relay.workflow_verbs import CeilingDenied, Refused, WorkflowVerbs

from .test_workflow_verbs import CEILING, REPO, SHA, Minter, Policy

SNAPSHOT = "b" * 40
LIVE = "c" * 40
MOVED = "d" * 40
REF = f"/repos/{REPO}/git/ref/heads/release/2026%20nov"
PR = f"/repos/{REPO}/pulls/7"


class RoutedClient:
    def __init__(self):
        self.pr = {"head": {"sha": SHA}, "base": {"ref": "release/2026 nov", "sha": SNAPSHOT}}
        self.tips = [{"object": {"sha": LIVE}}, {"object": {"sha": LIVE}}]
        self.compare = {"files": [{"filename": "docs/example.md"}]}
        self.calls = []

    def _call(self, method, path, payload=None, bearer=None, **_options):
        self.calls.append((method, path, payload, bearer))
        if method == "PUT" and path == PR + "/update-branch":
            return {"message": "accepted"}
        if method != "GET":
            raise AssertionError("unexpected mutation")
        if path == PR:
            result = self.pr
        elif path == REF:
            result = self.tips.pop(0)
        elif path.startswith(f"/repos/{REPO}/compare/"):
            result = self.compare
        elif path in {f"/repos/{REPO}/git/commits/{SHA}", f"/repos/{REPO}/git/commits/{LIVE}"}:
            commit = path.rsplit("/", 1)[-1]
            result = {"sha": commit, "tree": {"sha": "1" * 40 if commit == SHA else "2" * 40}}
        elif path in {
            f"/repos/{REPO}/git/trees/{'1' * 40}?recursive=1",
            f"/repos/{REPO}/git/trees/{'2' * 40}?recursive=1",
        }:
            result = {
                "sha": path.split("/trees/", 1)[1].split("?", 1)[0],
                "truncated": False,
                "tree": [],
            }
        else:
            raise AssertionError("unexpected proof target")
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class UpdateBranchBaseSnapshotTests(unittest.TestCase):
    def handle(self, client, ceiling=None):
        args = {"repo": REPO, "number": 7, "expected_head_sha": SHA}
        minter = Minter()
        handle = WorkflowVerbs.for_action(
            client,
            Policy({**CEILING, "workflows": "write"} if ceiling is None else ceiling),
            minter,
            "swu-wise",
            "wf_update_branch",
            REPO,
            args=args,
        )
        return handle, args, minter

    def assert_no_write_or_elevation(self, client, minter):
        self.assertNotIn("PUT", [call[0] for call in client.calls])
        self.assertEqual(len(minter.calls), 1)

    def test_stale_pr_snapshot_uses_current_named_ref_and_independent_recheck(self):
        client = RoutedClient()
        handle, args, minter = self.handle(client)
        self.assertTrue(handle.run("wf_update_branch", args)["accepted"])
        self.assertEqual(
            [call[:2] for call in client.calls],
            [
                ("GET", PR),
                ("GET", REF),
                ("GET", f"/repos/{REPO}/compare/{SHA}...{LIVE}"),
                ("GET", REF),
                ("PUT", PR + "/update-branch"),
            ],
        )
        self.assertEqual(client.calls[-1][2], {"expected_head_sha": SHA})
        self.assertTrue(all(call[3] == handle.bearer for call in client.calls[:-1]))
        self.assertEqual(len(minter.calls), 2)
        self.assertNotIn("workflows", minter.calls[0][2])
        self.assertEqual(minter.calls[1][2]["workflows"], "write")

    def test_matching_pr_snapshot_still_requires_both_ref_reads(self):
        client = RoutedClient()
        client.pr["base"]["sha"] = LIVE
        handle, args, _ = self.handle(client)
        self.assertTrue(handle.run("wf_update_branch", args)["accepted"])
        self.assertEqual([c[1] for c in client.calls].count(REF), 2)

    def test_base_move_during_compare_refuses_before_elevation_or_write(self):
        client = RoutedClient()
        client.tips[1] = {"object": {"sha": MOVED}}
        handle, args, minter = self.handle(client)
        with self.assertRaisesRegex(Refused, "PR base not matched"):
            handle.run("wf_update_branch", args)
        self.assert_no_write_or_elevation(client, minter)
        self.assertEqual([c[1] for c in client.calls].count(REF), 2)

    def test_head_mismatch_refuses_before_reading_base_or_proof(self):
        client = RoutedClient()
        client.pr["head"]["sha"] = MOVED
        handle, args, minter = self.handle(client)
        with self.assertRaisesRegex(Refused, "PR head not matched"):
            handle.run("wf_update_branch", args)
        self.assertEqual([c[:2] for c in client.calls], [("GET", PR)])
        self.assert_no_write_or_elevation(client, minter)

    def test_malformed_pr_metadata_still_refuses_before_base_read(self):
        for pr in (
            None,
            {},
            {"head": []},
            {"head": {"sha": SHA}, "base": []},
            {"head": {"sha": SHA}, "base": {"ref": "", "sha": SNAPSHOT}},
            {"head": {"sha": SHA}, "base": {"ref": "main", "sha": "not-a-sha"}},
        ):
            with self.subTest(pr=pr):
                client = RoutedClient()
                client.pr = pr
                handle, args, minter = self.handle(client)
                with self.assertRaises(Refused):
                    handle.run("wf_update_branch", args)
                self.assertEqual(len(client.calls), 1)
                self.assert_no_write_or_elevation(client, minter)

    def test_malformed_initial_or_final_ref_metadata_never_writes(self):
        bad_tips = [None, {}, {"object": None}, {"object": []}] + [
            {"object": {"sha": value}} for value in (None, True, 3, "main", "g" * 40, "a" * 41)
        ]
        for phase in (0, 1):
            for tip in bad_tips:
                with self.subTest(phase=phase, tip=tip):
                    client = RoutedClient()
                    client.tips[phase] = tip
                    handle, args, minter = self.handle(client)
                    with self.assertRaisesRegex(Refused, "PR base not matched"):
                        handle.run("wf_update_branch", args)
                    self.assert_no_write_or_elevation(client, minter)
                    if phase == 0:
                        self.assertFalse(any("/compare/" in c[1] for c in client.calls))

    def test_unavailable_initial_or_final_ref_is_retryable_preread_only(self):
        for phase in ("pr", 0, 1):
            for status in (0, 408, 503):
                with self.subTest(phase=phase, status=status):
                    client = RoutedClient()
                    failure = RelayGitHubError(status, "synthetic unavailable")
                    if phase == "pr":
                        client.pr = failure
                    else:
                        client.tips[phase] = failure
                    handle, args, minter = self.handle(client)
                    with self.assertRaises(PreReadUnavailable):
                        handle.run("wf_update_branch", args)
                    self.assert_no_write_or_elevation(client, minter)

    def test_workflows_write_ceiling_still_required_even_for_clean_proof(self):
        for files in ([], [{"filename": ".github/workflows/ci.yml"}]):
            with self.subTest(files=files):
                client = RoutedClient()
                client.compare = {"files": files}
                handle, args, minter = self.handle(client, CEILING)
                with self.assertRaises(CeilingDenied):
                    handle.run("wf_update_branch", args)
                self.assert_no_write_or_elevation(client, minter)

    def test_full_tree_fallback_proves_current_tip_not_stale_snapshot(self):
        capped = {"files": [{"filename": f"docs/{index}.md"} for index in range(300)]}
        oversized = RelayGitHubError(0, "synthetic size", code="response_too_large")
        for compare in ({}, capped, oversized):
            with self.subTest(compare=type(compare).__name__):
                client = RoutedClient()
                client.compare = compare
                handle, args, minter = self.handle(client)
                self.assertTrue(handle.run("wf_update_branch", args)["accepted"])
                self.assertIn(f"/repos/{REPO}/git/commits/{LIVE}", [c[1] for c in client.calls])
                self.assertEqual(minter.calls[1][2]["workflows"], "write")


if __name__ == "__main__":
    unittest.main()
