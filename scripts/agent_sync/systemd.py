"""Linux user service installation.  Activation and credential provisioning stay with the owner."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping
from typing import TextIO

from . import launchd
from . import live as L

UNIT = "agent-sync-listener.service"
DEFAULT_PYTHON = "/usr/bin/python3"


def unit_path(home: str) -> str:
    return os.path.join(home, ".config", "systemd", "user", UNIT)


def quote(value: str) -> str:
    """Quote a systemd argument; disable specifier and environment expansion."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("service paths must not contain control characters")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def unit_text(home: str, python: str, program: str) -> str:
    root = os.path.join(home, ".agent-sync")

    # Environment= and path directives do not expand dollar signs like ExecStart does.
    def literal(value: str) -> str:
        return quote(value).replace("$$", "$")

    return "\n".join(
        [
            "[Unit]",
            "Description=Agent-Sync Listener",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            "WorkingDirectory=" + home.replace("%", "%%"),
            "Environment=" + literal("HOME=" + home),
            "Environment=" + literal("AGENT_SYNC_STATE_DIR=" + root),
            'Environment="AGENT_SYNC_INSTANCE=mac"',
            "Environment="
            + literal("PATH=" + os.path.join(home, ".local/bin") + ":/usr/local/bin:/usr/bin:/bin"),
            "EnvironmentFile=-" + os.path.join(root, "listener.env").replace("%", "%%"),
            "ExecStart=" + " ".join(quote(v) for v in (python, program, "daemon", "run")),
            "Restart=on-failure",
            "RestartSec=30",
            "UMask=0077",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def sample_config() -> str:
    return (
        "# Linux listener sample.  Contains no secrets and enables no seats.\n"
        "# Configure only seats assigned to the interactive mac partition in the fleet partition.\n"
        "# Approved authentication and init precede activation; headless wakes require a probe.\n"
        '[daemon]\ninstance = "mac"\nowner_user_id = 0\nnotify_banners = false\n'
    )


def install(
    env: Mapping[str, str],
    home: str,
    out: TextIO,
    *,
    dry_run: bool = False,
    python: str | None = None,
    program: str | None = None,
) -> int:
    target = unit_path(home)
    config = os.path.join(home, ".agent-sync", L.CONFIG_NAME)
    data = unit_text(home, python or DEFAULT_PYTHON, program or launchd.program_path(home))
    if not dry_run:
        L.private_dir(L.listener_dir(os.path.join(home, ".agent-sync")))
    if os.path.exists(config):
        out.write("config already present, left as it is: %s\n" % config)
    else:
        out.write(
            "%swrite sample config %s (mode 600; no seats enabled)\n"
            % ("would: " if dry_run else "", config)
        )
        if not dry_run:
            launchd.write_private(config, sample_config())
    out.write("%swrite user service %s\n" % ("would: " if dry_run else "", target))
    if not dry_run:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = target + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    out.write(
        "This command does not run systemctl or provision credentials.  Provision the authorized authentication source before activation.\n"
    )
    out.write(
        "Optional owner-provisioned environment file: %s (mode 600).\n"
        % shlex.quote(os.path.join(home, ".agent-sync/listener.env"))
    )
    out.write(
        "After authentication, configuration and wake verification:\n  systemctl --user daemon-reload\n  systemctl --user enable --now %s\n"
        % UNIT
    )
    if dry_run:
        out.write("dry run: nothing was changed\n")
    return 0


def uninstall(home: str, out: TextIO, *, dry_run: bool = False) -> int:
    out.write(
        "This command does not run systemctl.  Stop the listener first:\n  systemctl --user disable --now %s\n"
        % UNIT
    )
    target = unit_path(home)
    if os.path.exists(target):
        out.write("%sremove %s\n" % ("would: " if dry_run else "", target))
        if not dry_run:
            os.unlink(target)
    out.write("State in ~/.agent-sync is kept.  Afterwards run: systemctl --user daemon-reload\n")
    if dry_run:
        out.write("dry run: nothing was changed\n")
    return 0


def status(home: str) -> dict:
    info = {"installed": os.path.isfile(unit_path(home)), "active_state": "unknown"}
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", UNIT, "--property=ActiveState", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        state = result.stdout.strip()
        if result.returncode == 0 and state in {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
            "maintenance",
        }:
            info["active_state"] = state
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info
