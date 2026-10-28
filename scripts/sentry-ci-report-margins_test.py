#!/usr/bin/env python3
"""Assert Backup fleet GitHub repositories uses the 600-minute GitHub-delay margin.

Also asserts the per-caller contract the shared reporter added:  the cron map
and the observed-workflow list come from the environment when the caller
supplies them,  and the built-in constants are the fallback.

Run: python3 scripts/sentry-ci-report-margins_test.py
"""

from __future__ import annotations

import ast
import importlib.util
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
    assert shared.find_cron_schedule_drift(set(observed)) == [], shared.find_cron_schedule_drift(set(observed))
    drifted = shared.find_cron_schedule_drift({"Push To Production"})
    assert drifted == ["Nightly Build", "Weekly Audit"], drifted
    print("DRIFT_USES_OBSERVED_OK", drifted)

    # A caller-supplied app identity replaces the built-in one;  the slug is
    # the default when the caller names only the display name.
    named = _load_module({"SENTRY_APP": "Congress.Trade"})
    assert named.APP == "Congress.Trade", named.APP
    assert named.APP_IDENTITY == "congress-trade", named.APP_IDENTITY
    pinned = _load_module({"SENTRY_APP": "Congress.Trade", "SENTRY_APP_IDENTITY": "ct"})
    assert pinned.APP_IDENTITY == "ct", pinned.APP_IDENTITY
    print("APP_IDENTITY_OK", pinned.APP_IDENTITY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
