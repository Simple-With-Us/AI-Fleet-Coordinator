"""The F3 helpers are loaded by path, text is made inert, secrets are withheld, and a missing helper fails closed."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fleet_facts import safety

SCRIPTS = Path(__file__).resolve().parents[2]


class Clean(unittest.TestCase):
    def test_outside_text_is_one_inert_line(self):
        text = safety.clean("@**all** [x](https://evil.example/p)\n`rm -rf ~` /poll /TODO", 200)
        for bad in ("@", "`", "://", "\n"):
            self.assertNotIn(bad, text)
        self.assertNotIn("/poll", text.lower())
        self.assertNotIn("/todo", text.lower())

    def test_idempotent_and_capped(self):
        once = safety.clean("[a](https://x.test) @all " * 30, 80)
        self.assertEqual(safety.clean(once, 80), once)
        self.assertLessEqual(len(once), 80)

    def test_empty_stays_empty(self):
        self.assertEqual(safety.clean(None), "")
        self.assertEqual(safety.clean("  \n "), "")

    def test_render_wraps_in_a_code_span_and_is_idempotent_with_clean(self):
        plain = safety.clean("hello @world", 50)
        self.assertEqual(safety.render(plain, 60), safety.render("hello @world", 60))
        self.assertTrue(safety.render("x").startswith("`"))

    def test_safe_ref_keeps_plain_branches_exact(self):
        for ref in ("claude/FFX-1-todo-list", "main", "feature/poll_x", "release-1.2.3"):
            self.assertEqual(safety.safe_ref(ref), (ref, True))
        cleaned, exact = safety.safe_ref("a b@c")
        self.assertFalse(exact)
        self.assertNotIn("@", cleaned)
        self.assertEqual(safety.safe_ref(None), (None, True))
        self.assertFalse(safety.safe_ref("../up")[1])

    def test_safe_path_keeps_plain_paths_exact_so_a_tool_can_act_on_them(self):
        for path in ("/home/example/apps/lanes/Example/claude-todo-list", "/home/example/apps/lanes/Example/todo-app",
                     "~/apps/lanes/Example/poll_x", "/srv/with space/x"):
            self.assertEqual(safety.safe_path(path), (path, True))
        cleaned, exact = safety.safe_path("/x/@**all**/`y`")
        self.assertFalse(exact)
        self.assertNotIn("@", cleaned)
        self.assertEqual(safety.safe_path("/x/../etc")[1], False)
        self.assertEqual(safety.safe_path(None), (None, True))
        # clean() would have mangled the first one, which is why paths use safe_path:
        self.assertNotEqual(safety.clean("/home/example/apps/lanes/Example/todo-app"), "/home/example/apps/lanes/Example/todo-app")

    def test_safe_token(self):
        self.assertEqual(safety.safe_token("CLAUDE"), "CLAUDE")
        self.assertEqual(safety.safe_token("dependabot[bot]"), "dependabot[bot]")
        self.assertNotIn("@", safety.safe_token("x@y"))
        self.assertIsNone(safety.safe_token(None))


class Redact(unittest.TestCase):
    def test_secret_shaped_fields_are_withheld_and_located(self):
        token = "ghp_" + "a1B2c3D4" * 5
        clean, paths = safety.redact({"prs": [{"title": "fine"}, {"title": "token " + token}], "n": 3})
        self.assertNotIn(token, json.dumps(clean))
        self.assertEqual(clean["prs"][0]["title"], "fine")
        self.assertEqual(clean["n"], 3)
        self.assertEqual(paths, ["$.prs[1].title"])


class MissingHelpers(unittest.TestCase):
    def test_a_copy_without_the_helpers_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(SCRIPTS / "fleet_facts", Path(tmp) / "fleet_facts", ignore=shutil.ignore_patterns("tests", "__pycache__"))
            shutil.copy(SCRIPTS / "fleet-facts", Path(tmp) / "fleet-facts")
            proc = subprocess.run([sys.executable, "-I", str(Path(tmp) / "fleet-facts"), "gather", "--no-mac", "--part", "lanes", "--json"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 3)
            self.assertEqual(proc.stdout, "")
            self.assertIn("helper", proc.stderr)
            help_proc = subprocess.run([sys.executable, "-I", str(Path(tmp) / "fleet-facts"), "--help"], capture_output=True, text=True)
            self.assertEqual(help_proc.returncode, 0)        # --help needs no helper


if __name__ == "__main__":
    unittest.main()
