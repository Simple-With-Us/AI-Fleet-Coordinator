"""Keep the fleet_lanes tests off the real machine.

The lane tools read the fleet roots from the process environment (FLEET_CODE_ROOT, FLEET_APPS_ROOT, FLEET_LANES_ROOT and
friends).  On a server host those variables name the LIVE folders, so a test that builds a fake home but lets the code under
test read os.environ resolves its install paths to the live ones.  That happened on Nov 8, 2026: a full run of this suite on
the server deleted and rebuilt the host's live lane-tools folder and replaced its apps/lane shim, because test_install_tools
built its paths with T.make_paths(fake_home) and ran T.main(...), and both read FLEET_APPS_ROOT from the environment.

Three layers, so one mistake cannot reach the live folders again:

  * Importing the tests package clears the roots from the process (fleet_lanes/tests/__init__.py), so every test module that
    is loaded through the package starts without them.
  * A fixture that installs or deletes calls clear() again in setUp (the caller's environment is restored afterwards) and
    run_main-style helpers use without_fleet_roots(), so a root a test put back cannot leak into a call that must not see it.
  * assert_inside() makes a fixture refuse, loudly, before it writes or deletes, when any path it is about to use is outside
    its temp tree.

test_hermetic.py proves each layer, including that a root set in the environment is ignored.

Which variables are scrubbed.  A FLEET_ variable the tools read is a LOCATION when its value is a folder or a file the code reads or
writes (a root, a state folder, a registry, a credential file, the systemd units folder):  those are FLEET_ROOT_VARS, and a test
that does not pass its own environment would otherwise follow them to the live copy.  Every other FLEET_ variable the tools read is
a flag, a counter, a note, a URL, a program to run or a chat target;  those are NOT_LOCATIONS, each with its reason.  The two
tables together are the audit, and test_hermetic.py scans the package and fails when a FLEET_ name the tools read is in neither,
so a new variable is classified in the pull request that adds it.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from unittest import mock

# Every variable through which the lane tools are pointed at a folder or a file on this machine.
FLEET_ROOT_VARS: tuple[str, ...] = (
    # the trees
    "FLEET_CODE_ROOT",
    "FLEET_APPS_ROOT",
    "FLEET_LANES_ROOT",
    "FLEET_LANES_EXTERNAL_ROOT",
    "FLEET_SERVER_VOLUME",
    # the registry and the layout it selects
    "FLEET_APPS_JSON",
    "FLEET_LAYOUT",
    # state, logs and configuration the tools read or write
    "FLEET_LANE_GUARD_STATE_DIR",
    "FLEET_LANE_GUARD_MODE_FILE",
    "FLEET_PLACEMENT_EXCEPTIONS",
    "FLEET_SYSTEMD_DIR",
    "FLEET_TEMP_BUILD_SCAN_ROOTS",
    "FLEET_SYNC_BEARER_FILE",
    "FLEET_RAG_HOME",
    "FLEET_SCRIPTS_DIR",
)

# The other FLEET_ variables the tools read, and why none of them is a place a test could reach the live machine through.
NOT_LOCATIONS: dict[str, str] = {
    "FLEET_BUILD_SHIM_DENY_NOTE": "the text the build shim shows when it refuses",
    "FLEET_BUILD_SHIM_DEPTH": "a recursion counter the build shim sets for itself",
    "FLEET_GIT_SHIM_DENY_NOTE": "the text the git shim shows when it refuses",
    "FLEET_GIT_SHIM_DEPTH": "a recursion counter the git shim sets for itself",
    "FLEET_GIT_SHIM_PYTHON": "the interpreter the shims run their checks with, a program and not a tree",
    "FLEET_GIT_SHIM_REAL": "the real git the shims call, a program and not a tree;  the tests that need it set their own",
    "FLEET_HOST_KIND": "names the kind of host (mac or server), not a place",
    "FLEET_LANES_TEST_RUN": "marks a test run, not a place",
    "FLEET_LANE_GUARD": "the guard's on/off switch, not a place",
    "FLEET_PYTHON": "the interpreter the lane shim runs, a program and not a tree",
    "FLEET_SYNC_ALERT_STREAM": "the chat channel code-main-sync alerts in, not a folder",
    "FLEET_SYNC_ALERT_TAG": "the tag in that alert's text, not a folder",
    "FLEET_SYNC_ALERT_TOPIC": "the chat topic code-main-sync alerts in, not a folder",
    "FLEET_SYNC_RELAY_URL": "a URL, not a folder",
}


def clear(env: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove the fleet roots from ENV (default: the process environment); return the names that were set."""
    target = os.environ if env is None else env
    return [name for name in FLEET_ROOT_VARS if target.pop(name, None) is not None]


@contextlib.contextmanager
def without_fleet_roots() -> Iterator[None]:
    """The process environment without the fleet roots for the duration; the caller's environment is restored on exit."""
    with mock.patch.dict(os.environ):
        clear()
        yield


def leaked(env: Mapping[str, str] | None = None) -> list[str]:
    """The fleet roots set in ENV (default: the process environment)."""
    source = os.environ if env is None else env
    return [name for name in FLEET_ROOT_VARS if source.get(name) is not None]


def assert_inside(root: str | os.PathLike[str], *paths: str | os.PathLike[str] | None) -> None:
    """Raise AssertionError unless every path is ROOT or below it (symlinks resolved).  None is skipped."""
    base = Path(os.path.realpath(root))
    for path in paths:
        if path is None:
            continue
        real = Path(os.path.realpath(path))
        if real != base and base not in real.parents:
            raise AssertionError(
                f"refusing to continue:  {path} resolves to {real}, outside the test's temp tree {base}.  "
                "A test must never write or delete outside it (the fleet roots in the environment can point at the live folders)."
            )


# Importing this module clears the roots from the whole test process, however the tests were started.
CLEARED_AT_IMPORT: list[str] = clear()
