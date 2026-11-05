"""Which shell shapes find the git shim:  real shells in a fake home, and the answers that are not a plain yes or no (AFC-618)."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fleet_lanes import doctor as D
from fleet_lanes import shim_reach as S
from fleet_lanes.tests import test_doctor_placement as TD

SHIM_TEXT = "#!/bin/sh\n# Fleet git shim:  a stand-in\nexec /usr/bin/git \"$@\"\n"
PREPEND = 'export PATH="$HOME/bin:$PATH"\n'


def fake_run(answers: dict):
    """Stands in for the shells:  each answer is what `command -v git` printed for that shell and flag, or an exception to raise."""

    def run(argv, **_kw):
        answer = answers[(os.path.basename(argv[0]), argv[1])]
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(stdout=answer, returncode=0)

    return run


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.shim = Path(self.home, "bin", "git")
        self.shim.parent.mkdir()
        self.shim.write_text(SHIM_TEXT, encoding="utf-8")
        self.shim.chmod(0o755)

    def rows(self, **kw) -> dict:
        return {row["shape"]: row for row in S.shim_reach(self.home, {"PATH": os.environ["PATH"], "FLEET_HOST_KIND": "mac"}, **kw)["rows"]}


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class RealShells(Case):
    def test_a_login_shell_finds_the_shim_only_through_its_own_dotfile(self) -> None:
        self.assertFalse(self.rows()["bash -lc"]["reaches"])
        Path(self.home, ".bash_profile").write_text(PREPEND, encoding="utf-8")
        rows = self.rows()
        self.assertTrue(rows["bash -lc"]["reaches"])
        self.assertEqual(rows["bash -lc"]["git"], str(self.shim))
        self.assertFalse(rows["bash -c"]["reaches"], "a non-login shell reads no profile")

    @unittest.skipUnless(shutil.which("zsh"), "needs zsh")
    def test_the_documented_zprofile_line_reaches_zsh_lc(self) -> None:
        self.assertFalse(self.rows()["zsh -lc"]["reaches"])
        readme = (Path(D.__file__).parent / "README.md").read_text(encoding="utf-8")
        self.assertIn(PREPEND.strip(), readme)
        Path(self.home, ".zprofile").write_text(PREPEND, encoding="utf-8")
        rows = self.rows()
        self.assertTrue(rows["zsh -lc"]["reaches"])
        self.assertFalse(rows["zsh -c"]["reaches"])

    def test_a_shim_first_on_a_path_the_probe_does_not_get_does_not_count(self) -> None:
        env = {"PATH": f"{self.shim.parent}:{os.environ['PATH']}", "FLEET_HOST_KIND": "mac"}
        rows = {r["shape"]: r for r in S.shim_reach(self.home, env)["rows"]}
        self.assertFalse(rows["bash -c"]["reaches"], "the probe starts from the default PATH, not this process's")


class Answers(Case):
    ALL_REAL = {(shell, flag): "/usr/bin/git\n" for shell in ("zsh", "bash") for flag in ("-c", "-lc", "-ic")}

    def test_every_common_shape_has_a_row(self) -> None:
        with mock.patch.object(S.shutil, "which", side_effect=lambda name, path=None: f"/bin/{name}" if name in ("zsh", "bash") else "/usr/bin/git"):
            rows = self.rows(run=fake_run(self.ALL_REAL))
        self.assertEqual(set(rows), {"zsh -c", "zsh -lc", "zsh -ic", "bash -c", "bash -lc", "bash -ic", "launchd job, default PATH"})
        self.assertFalse(any(row["reaches"] for row in rows.values()))

    def test_a_shape_that_could_not_be_asked_is_not_a_no(self) -> None:
        answers = dict(self.ALL_REAL)
        answers[("zsh", "-ic")] = subprocess.TimeoutExpired("zsh", 15)
        answers[("bash", "-ic")] = OSError("no")
        with mock.patch.object(S.shutil, "which", side_effect=lambda name, path=None: f"/bin/{name}"):
            rows = self.rows(run=fake_run(answers))
        self.assertIsNone(rows["zsh -ic"]["reaches"])
        self.assertEqual(rows["zsh -ic"]["note"], "timed out")
        self.assertIsNone(rows["bash -ic"]["reaches"])
        self.assertIs(rows["zsh -lc"]["reaches"], False)

    def test_a_missing_shell_is_not_a_no(self) -> None:
        with mock.patch.object(S.shutil, "which", return_value=None):
            rows = self.rows(run=fake_run({}))
        self.assertIsNone(rows["zsh -c"]["reaches"])
        self.assertIn("no zsh", rows["zsh -c"]["note"])

    def test_noise_from_an_interactive_dotfile_does_not_hide_the_answer(self) -> None:
        answers = dict(self.ALL_REAL)
        answers[("zsh", "-ic")] = f"Welcome back\nnvm: loaded\n{self.shim}\n"
        with mock.patch.object(S.shutil, "which", side_effect=lambda name, path=None: f"/bin/{name}"):
            self.assertTrue(self.rows(run=fake_run(answers))["zsh -ic"]["reaches"])

    def test_no_shim_installed_reads_as_that(self) -> None:
        self.shim.unlink()
        with mock.patch.object(S.shutil, "which", side_effect=lambda name, path=None: f"/bin/{name}"):
            reach = S.shim_reach(self.home, {"PATH": "/usr/bin", "FLEET_HOST_KIND": "mac"}, run=fake_run(self.ALL_REAL))
        self.assertFalse(reach["installed"])
        self.assertIn("no git shim is installed", "\n".join(S.render(reach)))

    def test_the_rendered_section_names_each_answer_and_the_readme(self) -> None:
        reach = {"installed": True, "rows": [S._row("zsh -lc", str(self.shim)), S._row("bash -lc", "/usr/bin/git"), S._row("zsh -ic", None, "timed out")]}  # noqa: SLF001
        text = "\n".join(S.render(reach))
        self.assertIn("1 of 3 shapes", text)
        self.assertRegex(text, r"zsh -lc\s+yes")
        self.assertRegex(text, r"bash -lc\s+no")
        self.assertRegex(text, r"zsh -ic\s+unknown\s+timed out")
        self.assertIn("How far the shim reaches", text)


class ThroughTheDoctor(TD.Case):
    REACH = {"installed": True, "rows": [{"shape": "zsh -lc", "git": "/usr/bin/git", "reaches": False, "note": ""}]}

    def run_main(self, *args: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(S, "shim_reach", return_value=self.REACH) as called:
            rc = D.main(["--no-gh", "--home", str(self.home), "--tmp-root", str(self.home / "no-tmp"), *args], env=self.vars, stdout=out, stderr=err)
        return rc, out.getvalue(), called

    def test_strict_placement_reports_it_and_it_fails_no_check(self) -> None:
        rc, text, called = self.run_main("--strict-placement")
        self.assertEqual(rc, 0, text)
        self.assertIn("SHIM REACH", text)
        called.assert_called_once()

    def test_the_flag_alone_reports_it_in_the_json(self) -> None:
        rc, text, _ = self.run_main("--shim-reach", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["shim_reach"], self.REACH)

    def test_a_copy_of_the_tools_without_the_module_still_runs(self) -> None:
        """The server's reviewed CLI bundle carries the doctor and not this module, and `lane` has to run there."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(D, "_shim_reach_module", return_value=None):
            rc = D.main(["--no-gh", "--home", str(self.home), "--tmp-root", str(self.home / "no-tmp"), "--strict-placement"], env=self.vars, stdout=out, stderr=err)
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("no shim_reach module", out.getvalue())
        self.assertNotIn("SHIM REACH", out.getvalue())

    def test_the_doctor_does_not_import_the_module_when_it_loads(self) -> None:
        self.assertNotIn("from . import shim_reach as", Path(D.__file__).read_text(encoding="utf-8"))

    def test_a_plain_report_starts_no_shells(self) -> None:
        rc, text, called = self.run_main()
        self.assertEqual(rc, 0)
        called.assert_not_called()
        self.assertNotIn("SHIM REACH", text)


if __name__ == "__main__":
    unittest.main()
