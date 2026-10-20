"""Linux user service installation.  Activation and credential provisioning stay with the owner."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from typing import TextIO

from . import launchd
from . import live as L

UNIT = "agent-sync-listener.service"
DEFAULT_PYTHON = "/usr/bin/python3"
MIN_PYTHON = (3, 11)


def interpreter_version(python: str) -> tuple[int, int] | None:
    """The candidate interpreter's major.minor, or None when it ran without reporting a usable one.

    A timeout or a launch failure is raised, never collapsed into an unknown version:  an unverified
    interpreter must not pass for a supported one.
    """
    result = subprocess.run(
        [python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def too_old(python: str) -> bool:
    """True only when the interpreter is confirmed older than MIN_PYTHON; a failed probe propagates."""
    version = interpreter_version(python)
    return version is not None and version < MIN_PYTHON


def probe_failure_exit_code(error: OSError | subprocess.TimeoutExpired) -> int:
    """A hung interpreter is 124, a missing one 127, any other launch failure 1."""
    if isinstance(error, subprocess.TimeoutExpired):
        return 124
    if isinstance(error, FileNotFoundError):
        return 127
    return 1


def _confirmed_too_old(python: str, out: TextIO) -> bool:
    """True when the interpreter is confirmed older; a probe that times out or cannot launch aborts."""
    try:
        return too_old(python)
    except (OSError, subprocess.TimeoutExpired) as error:
        code = probe_failure_exit_code(error)
        out.write(
            "note: %s could not be probed to confirm its Python version (exit %d); "
            "pass --python PATH (3.11 or newer)\n" % (python, code)
        )
        raise SystemExit(code) from error


def fsync_directory(directory: str) -> None:
    """Flush a rename into the directory itself; a crash must not lose the unit it replaced."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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


def unit_text(
    home: str, python: str, program: str, *, root: str | None = None, config: str | None = None
) -> str:
    root = root or os.path.join(home, ".agent-sync")
    config = config or os.path.join(root, L.CONFIG_NAME)

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
            "Environment=" + literal("AGENT_SYNC_CONFIG=" + config),
            "Environment="
            + literal("PATH=" + os.path.join(home, ".local/bin") + ":/usr/local/bin:/usr/bin:/bin"),
            # No EnvironmentFile:  a service must not read credentials from a local file.  Seats read the
            # process environment first, then fleet-secrets / Infisical (launchd behaves the same way).
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
        '[daemon]\ninstance = "mac"\nowner_user_id = 0\neligible_user_ids = []\nnotify_banners = false\n'
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
    root = os.path.abspath(
        os.path.expanduser(env.get(L.ENV_STATE_DIR) or os.path.join(home, ".agent-sync"))
    )
    config = os.path.abspath(L.config_path(root, env))
    probed = False
    if python is None:
        if os.access(DEFAULT_PYTHON, os.X_OK) and not _confirmed_too_old(DEFAULT_PYTHON, out):
            python = DEFAULT_PYTHON
            # The default path already probed this interpreter; a second spawn can fail transiently and
            # would abort an install whose runtime was just confirmed.
            probed = True
        else:
            # The unit would fail to start under an interpreter the operator never chose, and may name a
            # virtual-environment or /usr/local Python the user manager cannot reach.  Say so, never silently.
            python = sys.executable
            out.write(
                f"note: {DEFAULT_PYTHON} is missing or older than Python 3.11; the unit's ExecStart uses "
                f"{python} instead (inspect it before enabling, or pass --python PATH)\n"
            )
    if os.access(python, os.X_OK):
        if not probed and _confirmed_too_old(python, out):
            out.write(f"note: {python} is older than Python 3.11; pass --python PATH (3.11 or newer)\n")
    else:
        out.write(f"note: {python} is not executable; pass --python PATH (3.11 or newer)\n")
    data = unit_text(home, python, program or launchd.program_path(home), root=root, config=config)
    unit_dir = os.path.dirname(target)
    probe = unit_dir
    while probe != os.path.dirname(home):
        # $HOME itself is the operator's own layout and may legitimately be a symlink; only the
        # directory chain the unit is created below is this guard's business.
        if probe == home:
            break
        if os.path.islink(probe):
            raise ValueError("refusing symlink in service directory")
        if probe == os.path.dirname(probe):
            break
        probe = os.path.dirname(probe)
    if not dry_run:
        L.private_dir(L.listener_dir(root))
    if os.path.exists(config):
        out.write(f"config already present, left as it is: {config}\n")
    else:
        out.write(
            "{}write sample config {} (mode 600; no seats enabled)\n".format(
                "would: " if dry_run else "", config
            )
        )
        if not dry_run:
            launchd.write_private(config, sample_config())
    out.write("{}write user service {}\n".format("would: " if dry_run else "", target))
    if not dry_run:
        L.private_dir(unit_dir)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=unit_dir, delete=False
        ) as fh:
            temporary = fh.name
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        fsync_directory(unit_dir)
    out.write(
        "This command does not run systemctl or provision credentials.  Provision the authorized authentication source before activation.\n"
    )
    out.write(
        "The unit reads no credential file:  seats read the service environment first, then fleet-secrets / Infisical.\n"
    )
    out.write(
        f"After authentication, configuration and wake verification:\n  systemctl --user daemon-reload\n  systemctl --user enable --now {UNIT}\n"
    )
    if dry_run:
        out.write("dry run: nothing was changed\n")
    return 0


def uninstall(home: str, out: TextIO, *, dry_run: bool = False) -> int:
    out.write(
        f"This command does not run systemctl.  Stop the listener first:\n  systemctl --user disable --now {UNIT}\n"
    )
    target = unit_path(home)
    if os.path.exists(target):
        out.write("{}remove {}\n".format("would: " if dry_run else "", target))
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
            check=False,
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
    except FileNotFoundError:
        info["query_exit_code"] = 127
    except subprocess.TimeoutExpired:
        info["query_exit_code"] = 124
    except OSError:
        info["query_exit_code"] = 1
    return info
