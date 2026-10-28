#!/usr/bin/env python3
"""Assert Backup fleet GitHub repositories uses the 600-minute GitHub-delay margin.

Also asserts the per-caller contract the shared reporter added:  the cron map
and the observed-workflow list come from the environment when the caller
supplies them,  and the built-in constants are the fallback.  A SUPPLIED but
unusable value is a different case from an ABSENT one,  and the reporter must
never answer a broken input with this repository's own built-in data.

Run: python3 scripts/sentry-ci-report-margins_test.py
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("sentry-ci-report.py")


def _assign_value(tree: ast.AST, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if name in targets:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {SCRIPT.name}")


@contextlib.contextmanager
def _env(overrides: dict[str, str | None]):
    """Set environment variables for the duration of the block.

    A None value REMOVES the variable,  so "the caller supplied nothing" can
    be tested even when the ambient environment carries one.
    """
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_module(env: dict[str, str]):
    """Import the reporter under a controlled environment.

    The file name carries a dash,  so it cannot be imported by name;  the
    module-level code reads the environment once at import time,  which is
    exactly the contract under test.
    """
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("sentry_ci_report_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    default = _assign_value(tree, "DEFAULT_CHECKIN_MARGIN")
    overrides = _assign_value(tree, "CHECKIN_MARGIN_OVERRIDES")
    schedules = _assign_value(tree, "CRON_SCHEDULES")

    assert default == 15, default
    expected = {
        "Backup fleet GitHub repositories": 600,
    }
    assert overrides == expected, overrides
    assert schedules.get("Backup fleet GitHub repositories") == "0 7 * * *", schedules
    print("MARGIN_PARSE_OK", overrides)
    print("BACKUP_REPOS_CRON_OK", schedules["Backup fleet GitHub repositories"])

    # The built-in constants stay the fallback when the caller passes nothing.
    with _env({"SENTRY_CRON_SCHEDULES": None, "SENTRY_OBSERVED_WORKFLOWS": None}):
        bare = _load_module({})
        assert bare.EFFECTIVE_CRON_SCHEDULES == schedules, bare.EFFECTIVE_CRON_SCHEDULES
        assert bare.EFFECTIVE_OBSERVED_WORKFLOWS == [], bare.EFFECTIVE_OBSERVED_WORKFLOWS
        assert bare.APP == "AI-Fleet-Coordinator", bare.APP
        assert bare.APP_IDENTITY == "ai-fleet-coordinator", bare.APP_IDENTITY
    print("FALLBACK_OK", bare.APP_IDENTITY)

    # A caller that goes through the shared reusable passes its own map and its
    # own observed-workflow list.
    caller_schedules = {
        "Nightly Build": "30 2 * * *",
        "Weekly Audit": "0 4 * * 1",
    }
    observed = ["Nightly Build", "Weekly Audit", "Push To Production"]
    shared = _load_module(
        {
            "SENTRY_CRON_SCHEDULES": '{"Nightly Build": "30 2 * * *", "Weekly Audit": "0 4 * * 1"}',
            "SENTRY_OBSERVED_WORKFLOWS": '["Nightly Build", "Weekly Audit", "Push To Production"]',
        }
    )
    assert shared.EFFECTIVE_CRON_SCHEDULES == caller_schedules, shared.EFFECTIVE_CRON_SCHEDULES
    assert shared.EFFECTIVE_OBSERVED_WORKFLOWS == observed, shared.EFFECTIVE_OBSERVED_WORKFLOWS
    # The built-in literal is untouched:  the fallback survives for a per-repo run.
    assert shared.CRON_SCHEDULES == schedules, shared.CRON_SCHEDULES
    print("ENV_CONTRACT_OK", sorted(shared.EFFECTIVE_CRON_SCHEDULES))

    # The drift guard reads the caller's observed names,  not this repository's
    # workflow files:  the reporter runs from a checkout of its own repo.
    assert shared.find_cron_schedule_drift(set(observed)) == [], shared.find_cron_schedule_drift(
        set(observed)
    )
    drifted = shared.find_cron_schedule_drift({"Push To Production"})
    assert drifted == ["Nightly Build", "Weekly Audit"], drifted
    print("DRIFT_USES_OBSERVED_OK", drifted)

    # An ABSENT cron map still falls back to this repository's own literal.
    with _env({"SENTRY_CRON_SCHEDULES": None, "SENTRY_OBSERVED_WORKFLOWS": None}):
        absent_cron = _load_module({})
        assert absent_cron.EFFECTIVE_CRON_SCHEDULES == schedules, absent_cron.EFFECTIVE_CRON_SCHEDULES
        assert absent_cron.EFFECTIVE_CRON_SCHEDULES != {}, absent_cron.EFFECTIVE_CRON_SCHEDULES
    print("CRON_ABSENT_FALLBACK_OK", sorted(absent_cron.EFFECTIVE_CRON_SCHEDULES))

    # A SUPPLIED but unusable cron map must NOT fall back to that literal:  the
    # literal is this repository's own,  and mirroring it for another caller
    # would create monitors for workflows that caller does not have.
    for broken_cron in ("not json at all", '["Nightly Build"]', "42"):
        with _env({"SENTRY_CRON_SCHEDULES": None, "SENTRY_OBSERVED_WORKFLOWS": None}):
            broken = _load_module({"SENTRY_CRON_SCHEDULES": broken_cron})
            assert broken.EFFECTIVE_CRON_SCHEDULES == {}, broken.EFFECTIVE_CRON_SCHEDULES
            assert broken.EFFECTIVE_CRON_SCHEDULES != schedules, broken.EFFECTIVE_CRON_SCHEDULES
            # The built-in literal itself is untouched:  only the EFFECTIVE map
            # is empty,  and only for this run.
            assert broken.CRON_SCHEDULES == schedules, broken.CRON_SCHEDULES
    print("CRON_INVALID_EMPTY_OK", broken.EFFECTIVE_CRON_SCHEDULES)

    # An ABSENT observed-workflow list is an empty list.
    with _env({"SENTRY_OBSERVED_WORKFLOWS": None, "SENTRY_CRON_SCHEDULES": None}):
        absent_observed = _load_module({})
        assert absent_observed.EFFECTIVE_OBSERVED_WORKFLOWS == [], absent_observed.EFFECTIVE_OBSERVED_WORKFLOWS
        assert absent_observed.observed_workflows_from_env() == [], absent_observed.EFFECTIVE_OBSERVED_WORKFLOWS
    print("OBSERVED_ABSENT_EMPTY_OK", absent_observed.EFFECTIVE_OBSERVED_WORKFLOWS)

    # A SUPPLIED but unusable observed-workflow list RAISES:  it must not read
    # as "the caller observed nothing",  which would silently disable the
    # cron-drift guard.
    with _env({"SENTRY_OBSERVED_WORKFLOWS": None, "SENTRY_CRON_SCHEDULES": None}):
        probe = _load_module({})
        for broken_observed in ("not json at all", '{"Nightly Build": "30 2 * * *"}', "42"):
            with _env({"SENTRY_OBSERVED_WORKFLOWS": broken_observed}):
                try:
                    probe.observed_workflows_from_env()
                except ValueError as exc:
                    assert "SENTRY_OBSERVED_WORKFLOWS" in str(exc), exc
                else:
                    raise AssertionError(f"expected ValueError for {broken_observed!r}")
    print("OBSERVED_INVALID_RAISES_OK", broken_observed)

    # With no observed list the drift guard cannot tell:  it warns and returns
    # None,  because the local scan cannot exist in the reusable's sparse
    # checkout and reporting no drift from an unknown set would hide a rename.
    with _env({"SENTRY_OBSERVED_WORKFLOWS": None, "SENTRY_CRON_SCHEDULES": None}):
        empty = _load_module({})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            known = empty.known_workflow_names()
        assert known is None, known
        assert "::warning::" in buffer.getvalue(), buffer.getvalue()
        assert "observed" in buffer.getvalue(), buffer.getvalue()
    print("NO_OBSERVED_CANNOT_TELL_OK", known)

    # The local scan still works for a per-repo run that has a full checkout.
    local = bare.discover_workflow_names()
    assert local is not None, local
    assert "Sentry CI Report" in local, local
    assert bare.discover_workflow_names(Path("/nonexistent-workflows-dir")) is None
    print("LOCAL_SCAN_OK", len(local))

    # A caller-supplied app identity replaces the built-in one;  the slug is
    # the default when the caller names only the display name.  The identity
    # here is synthetic,  so no real production app identity sits in this file.
    named = _load_module({"SENTRY_APP": "Example.App"})
    assert named.APP == "Example.App", named.APP
    assert named.APP_IDENTITY == "example-app", named.APP_IDENTITY
    pinned = _load_module({"SENTRY_APP": "Example.App", "SENTRY_APP_IDENTITY": "ct"})
    assert pinned.APP_IDENTITY == "ct", pinned.APP_IDENTITY
    print("APP_IDENTITY_OK", pinned.APP_IDENTITY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
