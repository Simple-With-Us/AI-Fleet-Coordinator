"""The signed Mac facts file:  used only when it verifies, otherwise ignored and reported."""
import json
import os
import unittest

from open_loops import macfacts
from open_loops.facts import build_facts

from .builders import FAKE_KEY, NOW, Sandbox, World, ago, doctor_checkout, doctor_report
from .scenes import standard_world

ENV = {macfacts.KEY_ENV: FAKE_KEY}


def mac_document(**over):
    lanes_report = doctor_report([doctor_checkout("/work/lanes/Example/claude-stale", unpushed=3, last_change_at=ago(30))], generated_at=ago(1))
    doc = {
        "schema": 1, "generated_at": ago(2), "sig": "00",
        "sources": {"lanes": {"status": "ok", "reason": None}, "autopush": {"status": "ok", "reason": None}},
        "lanes": {"attention": [{"path": None, "repo": "Simple-With-Us/example-repo", "branch": "claude/stale", "unpushed": 3,
                                 "dirty_tracked": 0, "dirty_untracked": 0, "never_pushed": False, "last_change_at": ago(30)}],
                  "total": 1},
        "autopush": {"document": {"lanes": [{"lane": "claude-snap", "repo": "Simple-With-Us/example-repo", "branch": "claude/snap",
                                             "snapshot_only": True, "idle_hours": 1.0}], "refused": []}},
    }
    doc.update(over)
    return doc


class Load(Sandbox):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.world.programs(self.bin, ["fleet-facts"])
        self.path = self.write("gb-mac-facts.json", json.dumps(mac_document()))

    def load(self, spec=None, env=ENV, **kw):
        return macfacts.load(spec or str(self.path), run=self.world.runner(), now=NOW, environ=env, **kw)

    def test_a_verified_fresh_file_is_used(self):
        got = self.load()
        self.assertEqual(got["status"], "ok", got)
        self.assertEqual(got["document"]["lanes"]["total"], 1)
        verify = [c for c in self.world.calls if c["argv"][1:2] == ["verify"]]
        self.assertEqual(len(verify), 1)
        self.assertEqual(verify[0]["argv"][3:], ["--path", "gb/mac-facts.json"], "pinned to the Grok Bot copy's path")
        self.assertEqual(verify[0]["env"], {macfacts.KEY_ENV: FAKE_KEY})        # the key goes to the child's environment only
        self.assertNotIn(FAKE_KEY, " ".join(verify[0]["argv"]))

    def test_an_unverifiable_file_is_treated_as_absent_and_reported(self):
        self.world.verify_rc = 1
        got = self.load()
        self.assertEqual(got["status"], "absent")
        self.assertTrue(got["reason"].startswith("ignored:  unverified:"))
        self.assertIsNone(got["document"])

    def test_no_key_means_the_signature_cannot_be_checked(self):
        got = self.load(env={})
        self.assertEqual(got["status"], "absent")
        self.assertIn("FLEET_FACTS_SIGNING_KEY is not set", got["reason"])
        self.assertEqual(self.world.calls, [])                                    # verify was not even started

    def test_a_fleet_facts_without_verify_is_not_a_pass(self):
        self.world.verify_rc = 2
        self.assertIn("no verify command", self.load()["reason"])

    def test_a_missing_fleet_facts_is_not_a_pass(self):
        (self.bin / "fleet-facts").unlink()
        self.assertIn("not installed", self.load()["reason"])

    def test_stale_future_and_undated_files_are_ignored(self):
        old = self.write("old.json", json.dumps(mac_document(generated_at=ago(12))))
        self.assertIn("stale, built", self.load(str(old))["reason"])
        future = self.write("future.json", json.dumps(mac_document(generated_at=ago(-3))))
        self.assertIn("future", self.load(str(future))["reason"])
        undated = mac_document()
        del undated["generated_at"]
        self.assertIn("does not say when", self.load(str(self.write("undated.json", json.dumps(undated))))["reason"])

    def test_not_json_and_not_an_object_are_ignored(self):
        self.assertIn("not JSON", self.load(str(self.write("bad.json", "{nope")))["reason"])
        self.assertIn("not a JSON object", self.load(str(self.write("list.json", "[1]")))["reason"])

    def test_the_bytes_that_were_verified_are_the_bytes_that_are_used(self):
        seen = {}
        original = self.world.handle

        def spy(argv):
            if argv[1:2] == ["verify"]:
                with open(argv[2], "rb") as fh:
                    seen["bytes"] = fh.read()
                seen["mode"] = os.stat(argv[2]).st_mode & 0o777
                seen["dir_mode"] = os.stat(os.path.dirname(argv[2])).st_mode & 0o777
            return original(argv)
        self.world.handle = spy
        got = self.load()
        self.assertEqual(seen["bytes"], self.path.read_bytes())
        self.assertEqual((seen["mode"], seen["dir_mode"]), (0o600, 0o700))
        self.assertEqual(got["status"], "ok")

    def test_a_url_is_fetched_only_from_github_hosts_with_no_redirect_and_the_token_stays_private(self):
        seen = []

        def fetch(url, headers):
            seen.append((url, dict(headers)))
            return self.path.read_bytes()
        good = "https://raw.githubusercontent.com/Simple-With-Us/fleet-loops-data/main/gb/mac-facts.json"
        env = dict(ENV, GH_TOKEN="fake-token")
        self.assertEqual(self.load(good, env=env, fetch=fetch)["status"], "ok")
        self.assertEqual(seen[0][1]["Authorization"], "Bearer fake-token")
        for bad in ("http://raw.githubusercontent.com/x", "https://example.test/x", "https://user:pw@raw.githubusercontent.com/x",
                    "ftp://raw.githubusercontent.com/x"):
            seen.clear()
            got = self.load(bad, env=env, fetch=fetch)
            self.assertEqual(got["status"], "absent", bad)
            self.assertEqual(seen, [], bad)                                       # nothing was requested, so no token left the host
        self.assertIsNone(macfacts._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test/"))

    def test_a_fetch_error_is_ignored_with_its_reason(self):
        def fetch(url, headers):
            raise macfacts.FetchError("HTTP 404")
        got = self.load("https://api.github.com/repos/Simple-With-Us/fleet-loops-data/contents/gb/mac-facts.json", fetch=fetch)
        self.assertEqual((got["status"], got["reason"]), ("absent", "ignored:  HTTP 404"))

    def test_an_unreadable_path_and_a_folder_are_ignored(self):
        self.assertIn("cannot be read", self.load(str(self.root / "missing.json"))["reason"])
        self.assertIn("not a regular file", self.load(str(self.root))["reason"])

    def test_part_adapter(self):
        doc = mac_document()
        self.assertEqual(macfacts.part(doc, "lanes")["status"], "ok")
        self.assertEqual(macfacts.part(doc, "backup")["status"], "absent")
        doc["sources"]["lanes"] = {"status": "ok", "reason": None}
        del doc["lanes"]
        self.assertEqual(macfacts.part(doc, "lanes")["status"], "failed")
        doc["sources"]["autopush"] = {"status": "weird"}
        self.assertEqual(macfacts.part(doc, "autopush")["status"], "failed")


class InBuild(Sandbox):
    """gather --no-mac --mac-facts:  the file supplies lanes and autopush, and a bad file leaves them Missing."""

    def facts(self, world, spec, env=ENV, **kw):
        return build_facts(no_mac=True, mac_facts=spec, run=world.runner(), now=NOW, sleep=lambda s: None, environ=env, **kw)

    def setUp(self):
        super().setUp()
        self.world = standard_world(self.home, self.bin)
        self.path = self.write("gb-mac-facts.json", json.dumps(mac_document()))

    def test_lanes_and_c1_come_from_the_signed_file(self):
        f = self.facts(self.world, str(self.path))
        self.assertEqual(f["sources"]["mac_facts"]["status"], "ok")
        self.assertEqual(f["sources"]["lanes"]["status"], "ok")
        self.assertEqual(f["not_on_github"]["snapshot_only"]["count"], 1)
        self.assertEqual(f["not_on_github"]["snapshot_only"]["items"][0]["lane"], "claude-snap")
        self.assertEqual([i["lane"] for i in f["unpushed_over_a_day"]["items"]], ["claude/stale"])     # no path in the GB copy:  named by branch
        self.assertEqual(f["unpushed_over_a_day"]["count"], 1)
        self.assertEqual(f["missing"], [])
        names = [os.path.basename(c["argv"][0]) for c in self.world.calls]
        self.assertNotIn("lane", names)                                           # nothing local on a host with no Mac
        self.assertNotIn("fleet-autopush", names)

    def test_a_tampered_file_leaves_the_mac_parts_missing(self):
        self.world.verify_rc = 1
        f = self.facts(self.world, str(self.path))
        self.assertEqual(f["sources"]["mac_facts"]["status"], "absent")
        self.assertEqual({m["source"] for m in f["missing"]}, {"mac_facts", "lanes", "autopush"})
        self.assertIsNone(f["not_on_github"]["count"])
        self.assertIsNone(f["unpushed_over_a_day"]["count"])
        self.assertNotIn("claude-snap", json.dumps(f))

    def test_c6_rides_in_the_file_when_f7_includes_it_and_separates_the_answered_from_the_waiting(self):
        doc = mac_document()
        doc["sources"]["needs_owner"] = {"status": "ok", "reason": None}
        doc["needs_owner"] = {"document": [{"identifier": "FFX-10", "seat": "CLAUDE", "state": "notified", "low_stakes": [1]},
                                           {"identifier": "FFX-12", "seat": "CODEX", "state": "replied", "replied": {"kind": "reaction"}}]}
        f = self.facts(self.world, str(self.write("with-c6.json", json.dumps(doc))))
        self.assertEqual(f["sources"]["needs_owner"]["status"], "ok")
        self.assertEqual(f["decisions"]["answered_awaiting_close"], 1)
        self.assertNotIn("FFX-12", [i["identifier"] for i in f["decisions"]["items"]])
        names = [os.path.basename(c["argv"][0]) for c in self.world.calls]
        self.assertNotIn("needs-owner", names)                                    # still nothing local

    def test_a_file_without_c6_leaves_answered_unknown_and_is_not_missing(self):
        f = self.facts(self.world, str(self.path))
        self.assertEqual(f["sources"]["needs_owner"]["status"], "absent")
        self.assertIsNone(f["decisions"]["answered_awaiting_close"])
        self.assertNotIn("needs_owner", [m["source"] for m in f["missing"]])

    def test_a_signed_file_whose_own_source_failed_is_missing_too(self):
        doc = mac_document()
        doc["sources"]["autopush"] = {"status": "failed", "reason": "fleet-autopush exited 5"}
        del doc["autopush"]
        f = self.facts(self.world, str(self.write("partial.json", json.dumps(doc))))
        self.assertEqual(f["sources"]["autopush"]["status"], "failed")
        self.assertEqual([m["source"] for m in f["missing"]], ["autopush"])
        self.assertIsNone(f["not_on_github"]["count"])


if __name__ == "__main__":
    unittest.main()
