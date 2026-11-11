"""fleet_lanes — one methodical folder structure for every fleet checkout on the Mac.

Modules:
  layout   registry loading, seat aliases, sanctioned and forbidden roots, location
           classification, and lane and branch naming rules (pure; no subprocess, no network)

Layout v2 (owner 2026-10-09): lanes live at ~/apps/lanes/<Repo>/<seat>-<slug>, where <Repo> is the
repo's folder name under ~/Code.  The FLEET_LAYOUT environment variable can still say `flat`
(~/apps/<prefix>-<seat>-<slug>), a legacy opt-out that is no longer documented as a way to make lanes.

Tests (standard library only):

    cd scripts && python3 -m unittest fleet_lanes.tests.test_layout -v
"""

__version__ = "0.1.0"

import os as _os
import sys as _sys

TEST_RUN_VAR = "FLEET_LANES_TEST_RUN"


def _without_git_shim(path: str) -> str:
    """`path` less every folder whose `git` is the installed git shim, so a test's `git` is git on a Mac with the shim installed too."""

    def holds_shim(folder: str) -> bool:
        try:
            with open(_os.path.join(folder, "git"), "rb") as fh:
                return b"Fleet git shim" in fh.read(4096)
        except OSError:
            return False

    return _os.pathsep.join(folder for folder in path.split(_os.pathsep) if not holds_shim(folder))


def _mark_test_run() -> None:
    """A test run says so to every child, so a hook or shim a test starts cannot write the real event log (placement.log_event).  A test
    that builds a child's environment by hand gets it too, because that is how the suite filled the log:  fixture rows named the owner's
    home, and a shim child had no way to know it was in a test."""
    import subprocess as _subprocess  # not at the top:  a real hook starts Python on every matching command

    _os.environ.setdefault(TEST_RUN_VAR, "1")
    _os.environ["PATH"] = _without_git_shim(_os.environ.get("PATH", ""))
    start = _subprocess.Popen.__init__

    def marked(self, *args, **kwargs):
        if kwargs.get("env") is not None:
            kwargs["env"] = {**kwargs["env"], TEST_RUN_VAR: "1"}
        start(self, *args, **kwargs)

    _subprocess.Popen.__init__ = marked


if "unittest" in _sys.modules:
    _mark_test_run()
