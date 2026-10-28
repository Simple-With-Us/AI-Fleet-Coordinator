#!/usr/bin/env python3
"""check-ci-runner-policy.py -- flag a live local Mac Actions runner that
contradicts FLEET-PROTOCOL.md's local-Mac self-hosted runner ban (CI Runners section).

Compares the live process inventory (default ~/apps/MAC-LOCAL-PROCESSES.md,
which is NOT tracked in this repo) against the ban stated in this repo's
FLEET-PROTOCOL.md § CI Runner Infrastructure Policy. Exits 1 and prints one line
per offender if any inventory row looks like a self-hosted GitHub Actions
runner (an `actions.runner...` label, or a `mac-xcode*` / `trading-live-mac*`
process) whose status column says it is live (Always-on / Up / Running /
Enabled) rather than explicitly Retired / Disabled / Uninstalled / Banned.

This is a sanity check, not a full audit: it only catches an inventory row
that contradicts itself against the documented policy text. It does not
inspect GitHub Actions workflow YAML (`runs-on:` labels) in any repo -- do
that with `gh api repos/<org>/<repo>/contents/.github/workflows/<file>.yml`
per workflow when auditing a specific app.

Usage:
    python3 scripts/check-ci-runner-policy.py
    python3 scripts/check-ci-runner-policy.py --inventory /path/to/MAC-LOCAL-PROCESSES.md

See board item 7fa3b630 (fleet-infra) for the finding this responds to.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

DEFAULT_INVENTORY = pathlib.Path("~/apps/MAC-LOCAL-PROCESSES.md").expanduser()
# The protocol document is being renamed;  the mirror holds it under exactly one of the two names (check-generated-mirror.py).
# Remove the old name when the rename is complete.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_POLICY = next(
    (p for p in (_ROOT / "FLEET-PROTOCOL.md",) if p.is_file()),
    _ROOT / "FLEET-PROTOCOL.md",
)

RUNNER_ROW_RE = re.compile(
    r"actions\.runner|self-hosted runner|mac-xcode|trading-live-mac", re.IGNORECASE
)
LIVE_STATUS_RE = re.compile(r"\b(Always-on|Up|Running|Enabled)\b", re.IGNORECASE)
DEAD_STATUS_RE = re.compile(
    r"Retired|Disabled|Uninstalled|Banned|de-registered|deregistered|Old Mac CI label",
    re.IGNORECASE,
)


POLICY_BAN_RE = re.compile(
    r"Local Mac (?:(?:self-hosted )?runners? are permanently banned|Runner PERMANENTLY BANNED)",
    re.IGNORECASE,
)


def policy_states_ban(policy_text: str) -> bool:
    return bool(POLICY_BAN_RE.search(policy_text))


def scan_inventory(text: str) -> list[tuple[int, str]]:
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if "|" not in line or not RUNNER_ROW_RE.search(line):
            continue
        if DEAD_STATUS_RE.search(line):
            continue  # explicitly retired/disabled/banned in its own row -- fine
        if LIVE_STATUS_RE.search(line):
            offenders.append((lineno, line.strip()))
    return offenders


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", type=pathlib.Path, default=DEFAULT_INVENTORY)
    ap.add_argument("--policy", type=pathlib.Path, default=DEFAULT_POLICY)
    args = ap.parse_args(argv)

    if not args.policy.exists():
        print(f"policy file not found: {args.policy}", file=sys.stderr)
        return 2
    policy_text = args.policy.read_text()
    if not policy_states_ban(policy_text):
        print(
            "WARNING: FLEET-PROTOCOL.md no longer states the local-Mac-runner ban "
            "verbatim; this lint has nothing to check against. Update the "
            "pattern in POLICY_BAN_RE if the wording changed on purpose.",
            file=sys.stderr,
        )
        return 0

    if not args.inventory.exists():
        print(
            f"inventory file not found: {args.inventory} "
            "(pass --inventory, e.g. the live copy on this Mac; this script "
            "is a no-op in CI where that file does not exist)",
            file=sys.stderr,
        )
        return 0

    offenders = scan_inventory(args.inventory.read_text())
    if offenders:
        print(
            "CI runner policy contradiction: row(s) below look like a LIVE "
            "local Mac Actions runner while FLEET-PROTOCOL.md bans it:\n"
        )
        for lineno, line in offenders:
            print(f"  {args.inventory}:{lineno}: {line}")
        print(
            "\nReconcile: retire the runner (uninstall the LaunchAgent, "
            "de-register from GitHub), or amend the policy explicitly if the "
            "ban is being deliberately relaxed. See board item 7fa3b630."
        )
        return 1

    print("OK: no live local Mac Actions runner rows contradict the ban.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
