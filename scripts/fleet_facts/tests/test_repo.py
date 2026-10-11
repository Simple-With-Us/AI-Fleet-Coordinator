"""The repository gate and the sha-aware writer, against a scripted `gh`."""
import base64
import os
import unittest

from fleet_facts import repo as r

from .ghfake import REPO, FakeGitHub, blob_sha

GH = "gh"


def good() -> FakeGitHub:
    return FakeGitHub()


class Guard(unittest.TestCase):
    def test_a_private_repository_with_nothing_attached_passes(self):
        gh = good()
        self.assertEqual(r.guard(gh, GH, REPO), [])
        self.assertEqual(gh.write_calls(), [])

    def test_a_public_or_internal_repository_is_refused(self):
        for visibility in ("PUBLIC", "INTERNAL"):
            gh = good()
            gh.visibility = visibility
            with self.assertRaises(r.Refused) as caught:
                r.guard(gh, GH, REPO)
            self.assertIn("not private", caught.exception.reason)
            self.assertEqual(len(gh.calls), 1, "nothing past the visibility check was even asked")

    def test_an_archived_repository_or_a_fork_is_refused(self):
        for attr in ("archived", "fork"):
            gh = good()
            setattr(gh, attr, True)
            with self.assertRaises(r.Refused):
                r.guard(gh, GH, REPO)

    def test_an_unreadable_repository_is_refused(self):
        gh = good()
        gh.repo_view_error = True
        with self.assertRaises(r.Refused):
            r.guard(gh, GH, REPO)

    def test_a_webhook_is_refused_and_an_unreadable_list_is_too(self):
        gh = good()
        gh.hooks = [{"id": 1, "name": "web", "config": {"url": "https://example.test/hook"}}]
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("1 webhook", caught.exception.reason)
        self.assertNotIn("example.test", caught.exception.reason)
        gh = good()
        gh.hooks_error = True
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("cannot list", caught.exception.reason)

    def test_a_deploy_key_is_refused(self):
        gh = good()
        gh.keys = [{"id": 5, "title": "ci"}]
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("deploy key", caught.exception.reason)
        gh = good()
        gh.keys_error = True
        with self.assertRaises(r.Refused):
            r.guard(gh, GH, REPO)

    def test_an_app_installed_on_all_repositories_is_refused_by_name(self):
        gh = good()
        gh.installs = [{"id": i, "app_slug": "example-app-%d" % i, "repository_selection": "all"} for i in range(1, 6)]
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        reason = caught.exception.reason
        self.assertIn("5 installed apps can read the repository", reason)
        self.assertIn("example-app-1", reason)
        self.assertIn("FLEET_LOOPS_DATA_ALLOWED_APPS", reason)

    def test_a_long_app_list_is_shortened(self):
        gh = good()
        gh.installs = [{"id": i, "app_slug": "app-%02d" % i, "repository_selection": "all"} for i in range(1, 14)]
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("and 5 more", caught.exception.reason)

    def test_a_selected_app_is_refused_only_when_this_repository_is_among_its_repositories(self):
        gh = good()
        gh.installs = [{"id": 7, "app_slug": "picky-app", "repository_selection": "selected"}]
        gh.selected = {7: ["example-org/other-one", "example-org/other-two"]}
        self.assertEqual(r.guard(gh, GH, REPO), [])
        gh.selected = {7: ["example-org/other-one", REPO.upper()]}
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("picky-app", caught.exception.reason)

    def test_a_selected_app_whose_repositories_cannot_be_read_is_assumed_to_reach_the_repository(self):
        gh = good()
        gh.installs = [{"id": 7, "app_slug": "picky-app", "repository_selection": "selected"}]
        gh.selected = {7: None}
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("picky-app", caught.exception.reason)

    def test_an_unreadable_installation_list_is_refused(self):
        gh = good()
        gh.installs_error = True
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO)
        self.assertIn("cannot list the apps", caught.exception.reason)

    def test_a_selection_value_nobody_knows_is_treated_as_reaching(self):
        gh = good()
        gh.installs = [{"id": 1, "app_slug": "odd-app", "repository_selection": "everything"}]
        with self.assertRaises(r.Refused):
            r.guard(gh, GH, REPO)

    def test_apps_the_owner_named_pass_and_are_reported_but_only_those(self):
        gh = good()
        gh.installs = [{"id": 1, "app_slug": "Fine-App", "repository_selection": "all"},
                       {"id": 2, "app_slug": "other-app", "repository_selection": "all"}]
        with self.assertRaises(r.Refused) as caught:
            r.guard(gh, GH, REPO, allowed_apps=["fine-app"])
        self.assertIn("other-app", caught.exception.reason)
        self.assertNotIn("Fine-App", caught.exception.reason)
        self.assertEqual(r.guard(gh, GH, REPO, allowed_apps=["fine-app", " OTHER-APP "]), ["Fine-App", "other-app"])

    def test_a_bad_repository_name_is_refused(self):
        for name in ("noslash", "a/b/c", "../x", "owner/na me", ""):
            with self.assertRaises(r.Refused):
                r.guard(good(), GH, name)

    def test_a_listing_with_a_field_names_get_explicitly(self):
        """gh turns a request into a POST when a field is added, and these endpoints answer a POST with 404 (the fake does too)."""
        gh = good()
        gh.installs = [{"id": 7, "app_slug": "picky-app", "repository_selection": "selected"}]
        gh.selected = {7: ["example-org/other-one"]}
        self.assertEqual(r.guard(gh, GH, REPO), [])
        with_fields = [c for c in gh.calls if "-f" in c]
        self.assertEqual(len(with_fields), 2, "the installation list and the selected app's repository list")
        for call in with_fields:
            self.assertEqual(call[call.index("--method") + 1], "GET", call)
        broken = good()
        broken.installs = [{"id": 1, "app_slug": "x", "repository_selection": "all"}]
        result = broken(["gh", "api", "--paginate", "-f", "per_page=100", "orgs/example-org/installations"], timeout=1)
        self.assertEqual(result.rc, 1, "the fake refuses a field without --method GET, like the real gh")

    def test_the_gate_never_writes(self):
        gh = good()
        gh.installs = [{"id": 1, "app_slug": "x", "repository_selection": "all"}]
        with self.assertRaises(r.Refused):
            r.guard(gh, GH, REPO)
        self.assertEqual(gh.write_calls(), [])
        self.assertEqual(gh.puts, [])


class Paths(unittest.TestCase):
    def test_allowed_paths(self):
        for rel in ("mac-facts.json", "gb/mac-facts.json", "gb/full-list.md", "gb/sweeps/2026-10-11.json"):
            self.assertEqual(r.check_path(rel), rel)

    def test_everything_else_is_refused(self):
        for rel in ("", "/mac-facts.json", "../mac-facts.json", "gb/../mac-facts.json", "gb/../../x", ".github/workflows/x.yml",
                    "gb/.github/workflows/x.yml", "gb/.hidden", "other.json", "docs/routines/a.md", "gb", "gb/", "gb//x", "mac-facts.json/x",
                    "gb/a b", "gb/x\n", None, 7):
            with self.assertRaises(r.Refused, msg=repr(rel)):
                r.check_path(rel)


class Writing(unittest.TestCase):
    def setUp(self):
        self.gh = good()
        self.sleeps = []
        self.writer = r.Writer(self.gh, GH, REPO, sleep=self.sleeps.append)

    def test_a_new_file_is_created_without_a_sha(self):
        self.assertEqual(self.writer.put("gb/mac-facts.json", b"one\n", "m"), "created")
        (put,) = self.gh.puts
        self.assertNotIn("sha", put["body"])
        self.assertEqual(base64.b64decode(put["body"]["content"]), b"one\n")
        self.assertEqual(put["body"]["message"], "m")
        self.assertEqual(self.gh.written("gb/mac-facts.json"), b"one\n")

    def test_an_existing_file_is_updated_with_its_blob_sha(self):
        self.gh.files["gb/mac-facts.json"] = b"old\n"
        self.assertEqual(self.writer.put("gb/mac-facts.json", b"new\n", "m"), "updated")
        self.assertEqual(self.gh.puts[0]["body"]["sha"], blob_sha(b"old\n"))
        self.assertEqual(self.gh.written("gb/mac-facts.json"), b"new\n")

    def test_identical_content_is_not_written_again(self):
        self.gh.files["gb/full-list.md"] = b"same\n"
        self.assertEqual(self.writer.put("gb/full-list.md", b"same\n", "m"), "unchanged")
        self.assertEqual(self.gh.puts, [])

    def test_the_body_is_in_a_private_file_and_never_on_the_command_line(self):
        data = b"secret-looking-but-fixture-bytes"
        self.writer.put("gb/full-list.md", data, "m")
        (put,) = self.gh.puts
        argv = " ".join(put["argv"])
        self.assertNotIn(base64.b64encode(data).decode(), argv)
        self.assertIn("--input", put["argv"])
        self.assertEqual(put["mode"], 0o600)
        self.assertFalse(os.path.exists(put["file"]), "the body file is removed")
        self.assertFalse(os.path.exists(os.path.dirname(put["file"])), "and so is its folder")

    def test_no_call_ever_forces_anything(self):
        self.gh.files["gb/mac-facts.json"] = b"old"
        self.gh.put_fail["gb/mac-facts.json"] = ["409", "409"]
        with self.assertRaises(r.WriteFailed):
            self.writer.put("gb/mac-facts.json", b"new", "m")
        for call in self.gh.calls:
            text = " ".join(call).lower()
            self.assertNotIn("force", text)
            self.assertNotIn("--method delete", text)
            self.assertNotIn("-x delete", text)

    def test_a_conflict_is_retried_once_with_the_fresh_sha_and_then_stops(self):
        self.gh.files["gb/mac-facts.json"] = b"old"
        original_get = self.gh._get

        def racing_get(rel):
            result = original_get(rel)
            if len([c for c in self.gh.calls if "contents/gb/mac-facts.json" in " ".join(c) and "-X" not in c]) == 1:
                self.gh.files[rel] = b"someone else"      # changed right after we read the sha
            return result
        self.gh._get = racing_get
        self.assertEqual(self.writer.put("gb/mac-facts.json", b"new", "m"), "updated")
        self.assertEqual([p["body"].get("sha") for p in self.gh.puts], [blob_sha(b"old"), blob_sha(b"someone else")])
        self.assertEqual(self.gh.written("gb/mac-facts.json"), b"new")

    def test_a_conflict_with_nothing_changed_is_a_failure_not_a_loop(self):
        self.gh.files["gb/mac-facts.json"] = b"old"
        self.gh.put_fail["gb/mac-facts.json"] = ["422"]
        with self.assertRaises(r.WriteFailed) as caught:
            self.writer.put("gb/mac-facts.json", b"new", "m")
        self.assertIn("refused (HTTP 422)", caught.exception.reason)
        self.assertEqual(len(self.gh.puts), 1)

    def test_a_server_error_is_retried_once_after_a_pause(self):
        self.gh.put_fail["gb/mac-facts.json"] = ["502"]
        self.assertEqual(self.writer.put("gb/mac-facts.json", b"x", "m"), "created")
        self.assertEqual(len(self.gh.puts), 2)
        self.assertEqual(self.sleeps, [5])

    def test_two_server_errors_in_a_row_fail(self):
        self.gh.put_fail["gb/mac-facts.json"] = ["503", "503"]
        with self.assertRaises(r.WriteFailed) as caught:
            self.writer.put("gb/mac-facts.json", b"x", "m")
        self.assertIn("HTTP 503", caught.exception.reason)
        self.assertEqual(len(self.gh.puts), 2)

    def test_a_failed_read_is_retried_once_then_fails(self):
        self.gh.get_fail["gb/mac-facts.json"] = ["500", "500"]
        with self.assertRaises(r.WriteFailed):
            self.writer.put("gb/mac-facts.json", b"x", "m")
        self.assertEqual(self.gh.puts, [])

    def test_a_forbidden_path_is_refused_before_any_call(self):
        with self.assertRaises(r.Refused):
            self.writer.put(".github/workflows/x.yml", b"x", "m")
        self.assertEqual(self.gh.calls, [])

    def test_a_directory_in_the_way_is_not_overwritten(self):
        self.gh._get = lambda rel: r.Result(0, '[{"type": "file"}]', "")
        with self.assertRaises(r.WriteFailed):
            self.writer.put("gb/mac-facts.json", b"x", "m")
        self.assertEqual(self.gh.puts, [])


class Streams(unittest.TestCase):
    def test_json_stream_reads_pages_in_a_row(self):
        self.assertEqual(list(r.json_stream('{"a": 1}\n{"a": 2}  [3]')), [{"a": 1}, {"a": 2}, [3]])
        self.assertEqual(list(r.json_stream("")), [])
        with self.assertRaises(ValueError):
            list(r.json_stream("{nope"))

    def test_git_blob_sha_matches_git(self):
        self.assertEqual(r.git_blob_sha(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(r.git_blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")


if __name__ == "__main__":
    unittest.main()
