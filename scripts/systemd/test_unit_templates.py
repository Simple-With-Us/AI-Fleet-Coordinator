#!/usr/bin/env python3
"""Static assertions on the tracked systemd units under scripts/systemd/.

Why this suite exists:  a system unit that runs as a named account must not
spell its paths with systemd's %h specifier.  Measured on the fleet host
(systemd 259), %h expands to the ROOT account's home inside a system unit
even when the unit sets User=<account>:  the specifier is resolved from the
manager's context, not from the unit's User=, so a unit that names %h under a
named account silently runs the root tree.  The tracked fleet CLI updater
unit therefore spells its paths out and sets HOME explicitly, and this suite
is what keeps it that way.

A USER unit is the opposite case and must keep %h:  there %h is the user's
own home, and the unit that uses it is a user unit.  The rule this suite
enforces is therefore narrow on purpose:  a system unit that names a
non-root account may not use %h, and nothing else is forbidden.

The second assertion keeps the compliance inventory honest.  The inventory
row for this unit quotes its ExecStart, so a unit that changes without the
row changing is exactly the drift the fleet has already paid for once.

No systemd, no host, no network:  the suite reads tracked files only.
"""

from __future__ import annotations

import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
UNIT_DIR = HERE
INVENTORY = os.path.join(REPO, "docs", "compliance", "inventory.md")
UPDATER_UNIT = "fleet-cli-git-update.service"
DOCTOR_UNIT = "fleet-tools-doctor.service"
INVENTORY_ROW = "SRV-S05"
DOCTOR_INVENTORY_ROW = "SRV-S04"


def parse_unit(path: str) -> dict[str, list[tuple[str, str]]]:
    """Read a unit file into {section: [(key, value)]}.  Comments are dropped."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                sections.setdefault(current, [])
                continue
            if current is None or "=" not in line:
                continue
            key, _, value = line.partition("=")
            sections[current].append((key.strip(), value.strip()))
    return sections


def values(sections: dict[str, list[tuple[str, str]]], section: str, key: str) -> list[str]:
    return [value for name, value in sections.get(section, []) if name == key]


def services() -> list[str]:
    return sorted(name for name in os.listdir(UNIT_DIR) if name.endswith(".service"))


class SystemdUnitTemplates(unittest.TestCase):
    def test_systemd_unit_files_are_present(self):
        self.assertIn(UPDATER_UNIT, services())
        self.assertIn(DOCTOR_UNIT, services())

    def test_system_unit_with_a_named_account_never_uses_percent_h(self):
        for name in services():
            path = os.path.join(UNIT_DIR, name)
            sections = parse_unit(path)
            accounts = values(sections, "Service", "User")
            if not accounts:
                # A user unit (or a root unit):  %h is the right home there.
                continue
            for account in accounts:
                if account == "root":
                    # A root system unit:  %h IS the root home, so it is correct.
                    continue
                for section, pairs in sections.items():
                    for key, value in pairs:
                        self.assertNotIn(
                            "%h",
                            value,
                            f"{name}:  a system unit running as {account} must not use %h "
                            f"in [{section}] {key};  it expands to the root account's home "
                            "even when User= names another account",
                        )

    def test_updater_unit_spells_its_paths_out(self):
        path = os.path.join(UNIT_DIR, UPDATER_UNIT)
        sections = parse_unit(path)
        accounts = values(sections, "Service", "User")
        self.assertEqual(len(accounts), 1)
        self.assertNotEqual(accounts[0], "root")
        self.assertEqual(values(sections, "Service", "Group"), accounts)

        environments = values(sections, "Service", "Environment")
        homes = [value.split("=", 1)[1] for value in environments if value.startswith("HOME=")]
        self.assertEqual(len(homes), 1, "the unit must set HOME explicitly, once")
        home = homes[0]
        self.assertTrue(home.startswith("/"), "HOME must be an absolute path")

        starts = values(sections, "Service", "ExecStart")
        self.assertEqual(len(starts), 1, "the unit must have exactly one ExecStart")
        command = starts[0]
        for token in command.split():
            self.assertNotIn("%h", token)
            self.assertNotIn("~", token)
        self.assertIn(
            home,
            command,
            "the command must run inside the account home the unit sets",
        )
        self.assertIn(
            f"--home {home}",
            command,
            "the --home argument must be the account home the unit sets",
        )
        self.assertNotIn(
            " --home ",
            command.replace(f"--home {home}", ""),
            "the unit must pass --home exactly once",
        )

    def test_doctor_unit_spells_its_paths_out(self):
        path = os.path.join(UNIT_DIR, DOCTOR_UNIT)
        sections = parse_unit(path)
        accounts = values(sections, "Service", "User")
        self.assertEqual(accounts, ["jay"])
        environments = values(sections, "Service", "Environment")
        self.assertIn("HOME=/home/jay", environments)
        self.assertIn("AGENT_SEAT=CLAUDE", environments)
        command = values(sections, "Service", "ExecStart")[0]
        self.assertIn("/home/jay/.local/share/fleet-cli/repo/scripts/fleet-tools-doctor.py", command)
        self.assertIn("--post", command)
        self.assertNotIn("--home", command)

    def test_compliance_inventory_quotes_the_current_exec_start(self):
        path = os.path.join(UNIT_DIR, UPDATER_UNIT)
        command = values(parse_unit(path), "Service", "ExecStart")[0]
        with open(INVENTORY, encoding="utf-8") as handle:
            rows = [line for line in handle if line.startswith(f"| {INVENTORY_ROW} ")]
        self.assertEqual(len(rows), 1, f"expected exactly one {INVENTORY_ROW} inventory row")
        self.assertIn(
            command,
            rows[0],
            f"the {INVENTORY_ROW} inventory row must quote the unit's current "
            "ExecStart;  a unit that changes without the row changing is drift",
        )

    def test_compliance_inventory_quotes_the_doctor_exec_start(self):
        path = os.path.join(UNIT_DIR, DOCTOR_UNIT)
        command = values(parse_unit(path), "Service", "ExecStart")[0]
        with open(INVENTORY, encoding="utf-8") as handle:
            rows = [line for line in handle if line.startswith(f"| {DOCTOR_INVENTORY_ROW} ")]
        self.assertEqual(len(rows), 1, f"expected exactly one {DOCTOR_INVENTORY_ROW} inventory row")
        self.assertIn(
            command,
            rows[0],
            f"the {DOCTOR_INVENTORY_ROW} inventory row must quote the unit's current "
            "ExecStart;  a unit that changes without the row changing is drift",
        )


if __name__ == "__main__":
    unittest.main()
