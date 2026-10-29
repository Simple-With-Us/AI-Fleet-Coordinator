"""Tests for zulip_linkifiers.py:  the plan diff, and that TARGET's patterns link what CHECKS expect.

Zulip runs re2 and wraps each pattern so a match starts after whitespace or `'"(,:<` (or at the
start) and is not followed by a word character;  `linkify` mimics that wrapper and realm order with
Python's `re`, which agrees with re2 on these patterns (no backreferences or lookarounds inside).
"""

import contextlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zulip_linkifiers as L


def expand(template: str, groups: dict) -> str:
    return re.sub(r"\{\+?(\w+)\}", lambda m: groups[m.group(1)], template)


def linkify(text: str) -> str | None:
    """The href of the first TARGET rule that matches `text`, in realm order after `order`, or None."""
    rules = [r for r in L.TARGET if r[0] != L.GENERIC_ID] + [
        r for r in L.TARGET if r[0] == L.GENERIC_ID
    ]
    for _, pattern, url in rules:
        match = re.search(r"(?:^|(?<=[\s'\"(,:<]))(?:" + pattern + r")(?!\w)", text)
        if match:
            return expand(url, match.groupdict())
    return None


class TargetTest(unittest.TestCase):
    def test_checks_render_as_expected(self):
        for text, href in L.CHECKS:
            with self.subTest(text=text):
                self.assertEqual(linkify(text), href)

    def test_no_false_links(self):
        for text in ("UTF-8", "SHA-256", "C-3PO", "fleetlink/", "see CC-12x", "bc-12"):
            with self.subTest(text=text):
                self.assertIsNone(linkify(text))

    def test_linear_rule_does_not_claim_sentry_ids(self):
        for project in L.SENTRY_PROJECTS:
            with self.subTest(project=project):
                self.assertIn("sentry.io", linkify(f"{project}-1A"))

    def test_existing_ids_are_unique(self):
        ids = [rule_id for rule_id, _, _ in L.TARGET if rule_id is not None]
        self.assertEqual(len(ids), len(set(ids)))


class OrderTest(unittest.TestCase):
    def test_generic_rule_moves_last(self):
        live = [{"id": i} for i in (5117, L.GENERIC_ID, 6001)]
        self.assertEqual(L.order(live), [5117, 6001, L.GENERIC_ID])

    def test_already_last_needs_nothing(self):
        self.assertIsNone(L.order([{"id": 5117}, {"id": L.GENERIC_ID}]))

    def test_generic_rule_is_found_by_pattern_under_a_new_id(self):
        pattern = next(p for i, p, _ in L.TARGET if i == L.GENERIC_ID)
        live = [
            {"id": 5117, "pattern": "SWU/x"},
            {"id": 7001, "pattern": pattern},
            {"id": 6001, "pattern": "Y#(?P<id>[0-9]+)"},
        ]
        self.assertEqual(L.order(live), [5117, 6001, 7001])


class TeamDriftTest(unittest.TestCase):
    def test_no_drift(self):
        self.assertEqual(L.team_drift(list(L.LINEAR_TEAMS)), ([], []))

    def test_new_and_removed_teams(self):
        keys = [k for k in L.LINEAR_TEAMS if k != "AR"] + ["NEW"]
        self.assertEqual(L.team_drift(keys), (["NEW"], ["AR"]))


class LinearTeamKeysTest(unittest.TestCase):
    def run_with(self, **run):
        with (
            mock.patch.object(L.shutil, "which", return_value="/usr/bin/simple-board"),
            mock.patch.object(L.subprocess, "run", **run),
        ):
            return L.linear_team_keys()

    def test_keys_are_read_from_the_board_output(self):
        out = json.dumps({"teams": {"nodes": [{"key": "AFC"}, {"key": "C"}]}})
        proc = mock.Mock(stdout=out)
        self.assertEqual(self.run_with(return_value=proc), ["AFC", "C"])

    def test_runs_the_checked_simple_board_command(self):
        # #860 r4:  the guard checks shutil.which("simple-board"), so the subprocess must run
        # that command too — running the retired `board` would silently disable the team-drift check.
        out = json.dumps({"teams": {"nodes": [{"key": "AFC"}]}})
        with (
            mock.patch.object(L.shutil, "which", return_value="/usr/bin/simple-board"),
            mock.patch.object(L.subprocess, "run", return_value=mock.Mock(stdout=out)) as run,
        ):
            self.assertEqual(L.linear_team_keys(), ["AFC"])
        self.assertEqual(run.call_args[0][0][:3], ["simple-board", "api", "query"])

    def test_timeout_means_unavailable_not_a_crash(self):
        timeout = L.subprocess.TimeoutExpired(cmd="board", timeout=60)
        self.assertIsNone(self.run_with(side_effect=timeout))

    def test_unrunnable_board_means_unavailable(self):
        self.assertIsNone(self.run_with(side_effect=PermissionError("board")))

    def test_unparseable_output_means_unavailable(self):
        self.assertIsNone(self.run_with(return_value=mock.Mock(stdout="not json")))

    def test_missing_board_means_unavailable(self):
        with mock.patch.object(L.shutil, "which", return_value=None):
            self.assertIsNone(L.linear_team_keys())


class PlanTest(unittest.TestCase):
    def live(self, **overrides):
        rules = [
            {"id": i, "pattern": p, "url_template": u} for i, p, u in L.TARGET if i is not None
        ]
        for rule in rules:
            rule.update(overrides.get(str(rule["id"]), {}))
        return rules

    def test_matching_live_rules_need_nothing(self):
        self.assertEqual(L.plan(self.live()), ([], []))

    def test_missing_rule_is_added(self):
        live = [r for r in self.live() if r["id"] != 5143]
        self.assertEqual([s[0] for s in L.plan(live)[0]], ["add"])

    def test_changed_rule_is_edited_and_extra_rule_reported(self):
        live = self.live(**{"5122": {"url_template": "https://example.invalid/{id}"}})
        live.append({"id": 9999, "pattern": "X#(?P<id>[0-9]+)", "url_template": "https://x/{id}"})
        steps, unlisted = L.plan(live)
        self.assertIn(("edit", 5122, *[(p, u) for i, p, u in L.TARGET if i == 5122][0]), steps)
        self.assertEqual([r["id"] for r in unlisted], [9999])

    def renumbered(self, old_id, new_id, **changes):
        live = self.live()
        rule = next(r for r in live if r["id"] == old_id)
        rule.update(id=new_id, **changes)
        return live

    def test_rule_renumbered_by_zulip_is_matched_by_pattern(self):
        self.assertEqual(L.plan(self.renumbered(5143, 7001)), ([], []))

    def test_renumbered_rule_with_a_stale_url_is_edited_under_its_new_id(self):
        live = self.renumbered(5122, 7002, url_template="https://example.invalid/{id}")
        steps, unlisted = L.plan(live)
        self.assertEqual([(s[0], s[1]) for s in steps], [("edit", 7002)])
        self.assertEqual(unlisted, [])

    def test_row_without_an_id_is_matched_by_pattern_not_added_again(self):
        row = (None, "NEW#(?P<id>[0-9]+)", "https://example.invalid/{id}")
        live = self.live() + [{"id": 8000, "pattern": row[1], "url_template": row[2]}]
        with mock.patch.object(L, "TARGET", [*L.TARGET, row]):
            self.assertEqual(L.plan(live), ([], []))
            self.assertEqual([s[0] for s in L.plan(self.live())[0]], ["add"])


class FakeZulip:
    """Just enough of the realm API:  POST appends a rule, PATCH linkifiers reorders, and one POST can fail."""

    def __init__(self, live, fail_post=None):
        self.live = list(live)
        self.fail_post = fail_post
        self.posts = 0
        self.calls = []

    def linkifiers(self):
        return list(self.live)

    def call(self, method, path, data=None):
        self.calls.append((method, path))
        if method == "POST":
            self.posts += 1
            if self.posts == self.fail_post:
                raise L.ZulipError("POST realm/filters:  HTTP 400 refused")
            rule = {"id": 9000 + self.posts, **data}
            self.live.append(rule)
        elif method == "PATCH" and path == "realm/linkifiers":
            by_id = {rule["id"]: rule for rule in self.live}
            self.live = [by_id[i] for i in json.loads(data["ordered_linkifier_ids"])]
        return {}


class ApplyTest(unittest.TestCase):
    def two_missing(self):
        live = [
            {"id": i, "pattern": p, "url_template": u}
            for i, p, u in L.TARGET
            if i not in (5142, 5143)
        ]
        return FakeZulip(live, fail_post=2), live

    def test_failure_part_way_still_moves_the_generic_rule_last(self):
        zulip, live = self.two_missing()
        steps, _ = L.plan(live)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(L.ZulipError):
            L.apply_plan(zulip, steps, [])
        self.assertEqual(zulip.live[-1]["id"], L.GENERIC_ID)
        self.assertEqual(len(zulip.live), len(live) + 1)

    def test_rerun_after_a_failure_adds_only_what_is_left(self):
        zulip, live = self.two_missing()
        steps, _ = L.plan(live)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(L.ZulipError):
            L.apply_plan(zulip, steps, [])
        rerun, unlisted = L.plan(zulip.linkifiers())
        self.assertEqual([s[0] for s in rerun], ["add"])
        self.assertEqual(unlisted, [])
        zulip.fail_post = None
        with contextlib.redirect_stdout(io.StringIO()):
            L.apply_plan(zulip, rerun, [])
        self.assertEqual(L.plan(zulip.linkifiers()), ([], []))
        self.assertIsNone(L.order(zulip.linkifiers()))


class ZulipErrorTests(unittest.TestCase):
    def test_call_raises_zulip_error_not_system_exit(self):
        import urllib.error

        body = io.BytesIO(b'{"msg": "refused"}')
        err = urllib.error.HTTPError("https://x/api/v1/realm/filters", 400, "Bad Request", {}, body)
        zulip = L.Zulip("e", "k", L.REALM)
        with mock.patch.object(L.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(L.ZulipError) as caught:
                zulip.call("POST", "realm/filters", {"pattern": "P-X-1"})
        self.assertIsInstance(caught.exception, Exception)
        self.assertNotIsInstance(caught.exception, SystemExit)
        self.assertIn("HTTP 400", str(caught.exception))

    def test_main_reports_zulip_errors_with_exit_1_not_a_traceback(self):
        with (
            mock.patch.object(L, "credentials", return_value=("e", "k", L.REALM)),
            mock.patch.object(
                L.Zulip, "linkifiers", side_effect=L.ZulipError("GET x: HTTP 400 bad")
            ),
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(L.main([]), 1)
            self.assertIn("zulip:", err.getvalue())


class CredentialTests(unittest.TestCase):
    """Writes use only the realm-admin bot (AFC-359);  no owner-key option exists."""

    def run_main(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                L.main(list(argv))
        return cm.exception.code

    def test_apply_without_admin_is_refused(self):
        self.assertEqual(self.run_main("--apply"), 2)

    def test_removed_owner_key_option_is_rejected(self):
        self.assertEqual(self.run_main("--apply", "--owner" + "-rc", "x"), 2)

    def test_admin_and_rc_are_mutually_exclusive(self):
        self.assertEqual(self.run_main("--admin", "--rc", "x"), 2)

    def test_admin_reads_env_first_and_ignores_member_env(self):
        env = {
            "ZULIP_OPERATOR_EMAIL": "admin-bot@example.test",
            "ZULIP_OPERATOR_API_KEY": "k",
            "ZULIP_EMAIL": "member@example.test",
            "ZULIP_API_KEY": "m",
        }
        with (
            mock.patch.dict("os.environ", env),
            mock.patch.object(L, "_fleet_secret", return_value="") as fs,
        ):
            email, key, site = L.credentials(None, admin=True)
        self.assertEqual((email, key), ("admin-bot@example.test", "k"))
        self.assertEqual(site, L.REALM)
        fs.assert_called_once_with("ZULIP_OPERATOR_SITE")

    def test_admin_falls_back_to_zulipadmin_folder_first(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="v\n")

        env = {
            k: "" for k in ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE")
        }
        with mock.patch.dict("os.environ", env), mock.patch("subprocess.run", fake_run):
            L.credentials(None, admin=True)
        self.assertTrue(calls)
        self.assertTrue(all(c[3:5] == ["--path", "/zulip_operator"] for c in calls))

    def test_admin_without_credentials_exits_naming_the_folder(self):
        env = {
            k: "" for k in ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE")
        }
        with (
            mock.patch.dict("os.environ", env),
            mock.patch.object(L, "_fleet_secret", return_value=""),
        ):
            with self.assertRaises(SystemExit) as cm:
                L.credentials(None, admin=True)
        self.assertIn("/zulip_operator", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
