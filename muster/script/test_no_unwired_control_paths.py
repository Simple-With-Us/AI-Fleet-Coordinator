"""Muster carries no machine power, process kill or remote-host control code.

AFC-616:  the BotFleet Control port brought VM providers (a cloud power-off
and a run-any-script provider), a process monitor that sent
`kill -9` to other processes, a multi-host store with a hard-coded remote
hostname, and a token path in the server control API.  No view or model
reached any of it, so it was removed.  This keeps it from coming back
unreviewed:  a feature that needs one of these paths is a new, reviewed
change to this test, not a quiet revival.

Muster still stops its own child processes when they overrun a deadline,
with `Darwin.kill` on a pid it started;  that is not what this checks.
"""

import pathlib
import re
import unittest

MUSTER = pathlib.Path(__file__).resolve().parent.parent

REMOVED_TYPES = (
    "VMProviderPlugin",
    "VMProviderStore",
    "VMProviderFactory",
    "HetznerCloudVMProvider",
    "CustomScriptVMProvider",
    "BotFleetServerVMProvider",
    "FleetProcessMonitor",
    "FleetProcessClassifier",
    "OfflineVMHelper",
    "OfflineDockerVM",
    "PersistencePolicy",
    "ServerHostStore",
    "ServerHost",
)

FORBIDDEN_SWIFT = (
    # Running the system kill command on another process.
    r'"/bin/kill"',
    # Powering a machine off or on.
    r"\bpoweroff\b",
    r"\bpoweron\b",
    # The server control calls that were removed with the VM providers.
    r"/api/system/control/vms/",
    r"\bsetPersistOnExit\b",
)


def swift_code(path):
    """The file's code with whole-line comments left out."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("//")
    )


class NoUnwiredControlPathTests(unittest.TestCase):
    def sources(self):
        return sorted((MUSTER / "Muster").rglob("*.swift"))

    def test_the_removed_types_are_not_declared_or_used(self):
        for path in self.sources():
            code = swift_code(path)
            for name in REMOVED_TYPES:
                self.assertIsNone(
                    re.search(r"\b%s\b" % re.escape(name), code), "%s names %s" % (path.name, name)
                )

    def test_no_power_kill_or_remote_host_path(self):
        for path in self.sources():
            code = swift_code(path)
            for pattern in FORBIDDEN_SWIFT:
                self.assertIsNone(re.search(pattern, code), "%s matches %s" % (path.name, pattern))

    def test_the_server_control_api_sends_no_credential(self):
        code = swift_code(MUSTER / "Muster" / "FleetServices" / "ControlAPI.swift")
        for pattern in (r"Authorization", r"x-botfleet-gate-token", r"\bsecret", r"\btoken\b"):
            self.assertIsNone(re.search(pattern, code, re.IGNORECASE), pattern)

    def test_the_project_compiles_none_of_the_removed_files(self):
        text = (MUSTER / "project.yml").read_text()
        for name in (
            "VMProvider.swift",
            "ProcessMonitor.swift",
            "OfflineVMHelper.swift",
            "OfflineDockerVM.swift",
            "PersistencePolicy.swift",
            "ServerHost.swift",
        ):
            self.assertNotIn(name, text, name)
            self.assertFalse((MUSTER / "Muster" / "FleetServices" / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
