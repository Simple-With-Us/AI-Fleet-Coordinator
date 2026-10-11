"""The Grok Bot copy:  no Mac paths anywhere, quotes cut to 80 characters, join keys left alone."""
import copy
import json
import unittest

from fleet_facts import minimize as m

# Built at run time:  the fixtures check refuses a literal real-looking path in this folder.
USERS = "/" + "Users" + "/someone"
VOLUMES = "/" + "Volumes" + "/Disk"
LANE = USERS + "/apps/lanes/Example/claude-example-lane"


def sample():
    return {
        "schema": 1, "generated_at": "2026-10-11T12:00:00Z", "host": "example-host",
        "sources": {"lanes": {"status": "ok", "reason": None}, "backup": {"status": "failed", "reason": "see " + USERS + "/apps/x/state.json please"}},
        "lanes": {"total": 2, "attention": [
            {"path": LANE, "repo": "example-org/example-repo", "branch": "claude/example-branch", "unpushed": 3,
             "reasons": ["a" * 200], "keep_reason": "keeps ~/apps/notes until Friday"},
            {"path": VOLUMES + "/lanes/Example/codex-other", "branch": "codex/x", "lane": "already-named", "unpushed": 1},
        ]},
        "autopush": {"document": {"lanes": [{"path": LANE, "branch": "claude/example-branch", "snapshot_only": True}],
                                  "refused": [{"lane": "claude-example-lane", "reason": "diverged " * 30}]}},
        "backup": {"report": {"state_file": USERS + "/apps/fleet-work-backup/state.json", "verdict": "ok", "last_line": "x" * 120}},
        "needs_owner": {"document": [{"identifier": "FFX-1", "question": "q" * 300, "topic": "t" * 70}]},
    }


class Minimize(unittest.TestCase):
    def test_no_mac_path_survives_and_the_input_is_untouched(self):
        original = sample()
        before = copy.deepcopy(original)
        out = m.minimize(original)
        self.assertEqual(original, before)
        text = json.dumps(out)
        for needle in ("/" + "Users", "/" + "Volumes", "~/"):
            self.assertNotIn(needle, text)
        self.assertFalse(m.mac_path_in(text))

    def test_a_path_becomes_the_lane_folder_name(self):
        out = m.minimize(sample())
        first, second = out["lanes"]["attention"]
        self.assertNotIn("path", first)
        self.assertEqual(first["lane"], "claude-example-lane")
        self.assertEqual(second["lane"], "already-named", "an existing lane name is kept")
        self.assertEqual(out["autopush"]["document"]["lanes"][0]["lane"], "claude-example-lane")

    def test_the_consumer_still_names_and_skips_lanes_from_the_copy(self):
        from open_loops import views
        out = m.minimize(sample())
        self.assertEqual(views.lane_name(out["lanes"]["attention"][0]), "claude-example-lane")
        view = views.autopush_view(out["autopush"])
        self.assertTrue(view["ok"])
        self.assertEqual(view["rows"][0]["lane"], "claude-example-lane")

    def test_quotes_are_cut_to_eighty_characters(self):
        out = m.minimize(sample())
        reason = out["lanes"]["attention"][0]["reasons"][0]
        self.assertEqual(len(reason), m.QUOTE_CAP)
        self.assertTrue(reason.endswith(m.ELLIPSIS))
        self.assertEqual(len(out["autopush"]["document"]["refused"][0]["reason"]), m.QUOTE_CAP)
        self.assertEqual(len(out["backup"]["report"]["last_line"]), m.QUOTE_CAP)
        self.assertEqual(len(out["needs_owner"]["document"][0]["question"]), m.QUOTE_CAP)

    def test_short_text_is_left_alone(self):
        out = m.minimize(sample())
        self.assertEqual(out["lanes"]["attention"][0]["keep_reason"], "keeps [path] until Friday")
        self.assertEqual(out["sources"]["backup"]["reason"], "see [path] please")
        self.assertEqual(out["lanes"]["total"], 2)

    def test_join_keys_are_never_cut(self):
        doc = {"branch": "claude/" + "b" * 150, "repo": "o/" + "r" * 120, "lane": "l" * 120, "identifier": "FFX-1", "topic": "t" * 70}
        self.assertEqual(m.minimize(doc), doc)

    def test_the_system_folders_a_tool_writes_about_are_scrubbed_too(self):
        text = "log /private/var/folders/ab/T/com.apple.dt.Xcode/foo and /tmp/a.log, cache /var/tmp/x, /opt/homebrew/bin, /etc/hosts"
        out = m.minimize({"reason": text})["reason"]
        self.assertEqual(out, "log [path] and [path], cache [path], [path], [path]")
        self.assertFalse(m.mac_path_in(out))

    def test_a_folder_name_inside_a_branch_or_a_link_is_not_a_path(self):
        doc = {"branch": "fix/tmp/x", "reason": "see https://example.test/etc/x and a/var/b, also /varnish", "repo": "o/opt"}
        self.assertEqual(m.minimize(doc), doc)

    def test_a_tilde_that_is_not_a_path_stays(self):
        self.assertEqual(m.minimize({"note": "about ~5 minutes"}), {"note": "about ~5 minutes"})

    def test_a_path_in_a_key_cannot_be_scrubbed_so_the_copy_is_refused(self):
        with self.assertRaises(m.PathLeak):
            m.minimize({USERS + "/apps/x": 1})
        with self.assertRaises(m.PathLeak):
            m.minimize({"/tmp/x": 1})

    def test_numbers_booleans_and_nulls_pass_through(self):
        doc = {"a": 1, "b": 2.5, "c": True, "d": None, "e": [1, None, False]}
        self.assertEqual(m.minimize(doc), doc)


if __name__ == "__main__":
    unittest.main()
