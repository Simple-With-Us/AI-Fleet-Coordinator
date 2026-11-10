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
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from unittest import mock

# Every variable through which the lane tools are pointed at a folder or a registry on this machine.
FLEET_ROOT_VARS: tuple[str, ...] = (
    "FLEET_CODE_ROOT",
    "FLEET_APPS_ROOT",
    "FLEET_LANES_ROOT",
    "FLEET_LANES_EXTERNAL_ROOT",
    "FLEET_SERVER_VOLUME",
    "FLEET_APPS_JSON",
    "FLEET_LAYOUT",
)


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
