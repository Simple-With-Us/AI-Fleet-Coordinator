"""The Z Code Bash-tool seat default: malformed profiles refuse byte-for-byte unchanged, the
managed block keeps the seat shims in front of PATH, stray diagnostics never carry profile text,
and a symlinked profile is a named gap, not a failure.  Covers the AFC-839 review round (Jet's
source review plus the Sentry findings) on apply/verify_zcode_seat_default."""

import os
import tempfile
import unittest

from fleet_lanes.install_tools import (
    Paths,
    Refused,
    _zcode_bashrc_classify,
    _zcode_bashrc_desired,
    apply_zcode_seat_default,
    render_zcode_bashrc_block,
    render_zcode_seat_shim,
    verify_zcode_seat_default,
)

SEAT = "ZC"
BLOCK = render_zcode_bashrc_block()


def write(path, text, mode=0o644):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def paths_for(home, tool_target="/usr/bin/env"):
    os.makedirs(os.path.join(home, ".fleet-zcode-seat-bin"), exist_ok=True)
    return Paths(home=home, stable=home)


class ZcodeBashrcClassifyTests(unittest.TestCase):
    def test_a_current_block_reads_none(self):
        text = "# user profile\n" + BLOCK + "\n# tail\n"
        self.assertEqual(_zcode_bashrc_classify(text), ("none", None, []))

    def test_a_reversed_marker_pair_is_refused(self):
        # Jet: classify checked counts but not ordering;  a reversed pair spliced block lines into
        # unrelated profile text.  It is refused now.
        lines = BLOCK.rstrip("\n").split("\n")
        reversed_block = "\n".join(reversed(lines))
        text = "profile start\n" + reversed_block + "\nprofile end"
        with self.assertRaises(Refused):
            _zcode_bashrc_classify(text)

    def test_an_overlapping_hand_block_is_refused(self):
        # Jet: overlapping handwritten and managed ranges would duplicate or drop lines on removal.
        block_lines = BLOCK.rstrip("\n").split("\n")
        head, tail = 2, 9
        managed = [block_lines[0].replace("--- begin", "--- begin ")] + block_lines[1:]
        hand = [
            "# --- fleet: Z Code session seat declaration",
            "AGENT_SEAT=ZC",
            "# --- end fleet: Z Code session seat declaration",
        ]
        text = "\n".join(
            ["# profile"] + managed[: head + 3] + hand + managed[head + 3 :] + ["# tail"]
        )
        with self.assertRaises(Refused):
            _zcode_bashrc_classify(text)

    def test_stray_diagnostics_carry_line_numbers_not_profile_text(self):
        text = "AGENT_SEAT=ZC\nexport SECRET_TOKEN=hunter2\n" + BLOCK
        _, _, stray = _zcode_bashrc_classify(text)
        self.assertEqual(len(stray), 1, stray)
        self.assertIn("line 1:", stray[0])
        # the summary names the line number and what matched, and never the line itself
        self.assertIn("mentions AGENT_SEAT outside any managed block", stray[0])
        self.assertNotIn("hunter2", stray[0])
        self.assertNotIn("AGENT_SEAT=", stray[0])


class ZcodeBashrcDesiredTests(unittest.TestCase):
    def test_malformed_profiles_refuse_and_keep_bytes(self):
        # Jet: the desired text spliced by indexes could duplicate unrelated profile lines.  Now the
        # classification refuses first, and the profile bytes are untouched.
        block_lines = BLOCK.rstrip("\n").split("\n")
        reversed_text = "profile start\n" + "\n".join(reversed(block_lines)) + "\nprofile end"
        before = reversed_text
        with self.assertRaises(Refused):
            _zcode_bashrc_desired(reversed_text)
        self.assertEqual(reversed_text, before)

    def test_a_current_block_desires_no_change(self):
        text = "# user profile\n" + BLOCK + "\n# tail"
        new, action, _ = _zcode_bashrc_desired(text)
        self.assertEqual((new, action), (text, "none"))


class ApplyZcodeSeatDefaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.paths = paths_for(self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def _place_tools_on_path(self):
        bin_dir = os.path.join(self.home, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for tool in ("simple-chat", "simple-board", "recall", "simple-git"):
            write(os.path.join(bin_dir, tool), "#!/bin/sh\nexit 0\n", 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_reversed_profile_is_refused_and_stays_byte_for_byte(self):
        self._place_tools_on_path()
        bashrc = os.path.join(self.home, ".bashrc")
        block_lines = BLOCK.rstrip("\n").split("\n")
        before = "profile start\n" + "\n".join(reversed(block_lines)) + "\nprofile end"
        write(bashrc, before)
        result = apply_zcode_seat_default(self.paths)
        self.assertEqual(result.status, "refused", result.detail)
        self.assertEqual(open(bashrc, encoding="utf-8").read(), before)

    def test_a_refusal_installs_no_shims(self):
        self._place_tools_on_path()
        bashrc = os.path.join(self.home, ".bashrc")
        block_lines = BLOCK.rstrip("\n").split("\n")
        before = "profile start\n" + "\n".join(reversed(block_lines)) + "\nprofile end"
        write(bashrc, before)
        result = apply_zcode_seat_default(self.paths)
        self.assertEqual(result.status, "refused", result.detail)
        seat_bin = os.path.join(self.home, ".fleet-zcode-seat-bin")
        self.assertEqual(
            [
                n
                for n in os.listdir(seat_bin)
                if n in ("simple-chat", "simple-board", "recall", "simple-git")
            ],
            [],
            "no seat shims on a refused profile",
        )

    def test_an_apply_installs_the_block_and_the_shims(self):
        self._place_tools_on_path()
        result = apply_zcode_seat_default(self.paths)
        self.assertEqual(result.status, "installed", result.detail)
        bashrc = os.path.join(self.home, ".bashrc")
        installed = open(bashrc, encoding="utf-8").read()
        self.assertIn(BLOCK, installed)
        # The seat shims sit in front of PATH after the block: the shim dir is deduped out of PATH and
        # re-fronted on every sourcing, so a later dotfile prepend cannot leave them behind (Sentry,
        # AFC-839 round);  the loop itself is exercised end to end in
        # fleet_lanes/tests/test_install_tools.py (a later prepend cannot shadow the shims).
        self.assertIn('export PATH="$_zc_seat_bin${_zc_kept:+:$_zc_kept}"', installed)
        self.assertNotIn('case ":$PATH:" in', installed)

    def test_a_second_apply_is_unchanged(self):
        self._place_tools_on_path()
        self.assertEqual(apply_zcode_seat_default(self.paths).status, "installed")
        result = apply_zcode_seat_default(self.paths)
        self.assertEqual(result.status, "unchanged", result.detail)


class VerifyZcodeSeatDefaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.paths = paths_for(self.home)
        bin_dir = os.path.join(self.home, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for tool in ("simple-chat", "simple-board", "recall", "simple-git"):
            write(os.path.join(bin_dir, tool), "#!/bin/sh\nexit 0\n", 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        apply_zcode_seat_default(self.paths)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_fresh_install_verifies_clean(self):
        checks = verify_zcode_seat_default(self.paths)
        self.assertEqual(
            [c.status for c in checks], ["PASS"] * len(checks), [c.detail for c in checks]
        )

    def test_a_symlinked_bashrc_is_a_named_warn_not_a_fail(self):
        # Sentry: a symlinked profile read as a permanent FAIL while apply refuses it by design.
        # The gap is named as a WARN:  the host reads healthy with the remediation stated.
        target = os.path.join(self.home, "real-bashrc")
        write(target, "# the real profile\n")
        bashrc = os.path.join(self.home, ".bashrc")
        os.unlink(bashrc)
        os.symlink(target, bashrc)
        checks = verify_zcode_seat_default(self.paths)
        bashrc_checks = [c for c in checks if c.name == "bashrc block"]
        self.assertTrue(bashrc_checks)
        self.assertEqual(
            [c.status for c in bashrc_checks],
            ["WARN"] * len(bashrc_checks),
            [c.detail for c in bashrc_checks],
        )
        self.assertTrue(any("symlink" in c.detail for c in bashrc_checks))

    def test_shim_render_is_byte_stable(self):
        target = "/usr/bin/env"
        self.assertEqual(
            render_zcode_seat_shim("simple-chat", target),
            render_zcode_seat_shim("simple-chat", target),
        )


if __name__ == "__main__":
    unittest.main()
