#!/usr/bin/env python3
"""Point every agent tool's session working directory at the lanes root.

Owner policy, Oct 22, 2026:  on a server host the project mirrors live on the
fleet-extra volume (`/mnt/fleet-extra/Code`) and the lanes live beside them
(`/mnt/fleet-extra/apps/lanes`), and `~/Code` and `~/apps` resolve to them, so
the policy path `~/apps/lanes/<Repo>/<seat>-<slug>` still holds.  Every tool
session starts in the lanes root, so work lands in a lane and never in an
integration tree.

The knobs differ per tool, because the tools differ.  Codex and the desktop apps
have a location setting; the CLI tools have none, so their working directory
comes from the launcher that starts them.  This script owns those launchers and
settings on a server host.

It is idempotent.  `--apply` writes only what differs and leaves a timestamped
backup next to every file it touches.  `--verify` reports each knob and exits
nonzero when one of them is off, so a seat can prove the policy holds.

Usage:

    python3 scripts/fleet_lanes/server_working_dirs.py --apply
    python3 scripts/fleet_lanes/server_working_dirs.py --verify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/root")).expanduser()

# Where the systemd units live.  Overridable so a test can write them somewhere
# harmless, and so a host that keeps its units elsewhere can point at it.
SYSTEMD_DIR = Path(os.environ.get("FLEET_SYSTEMD_DIR", "/etc/systemd/system"))

# The lanes root, in the order the fleet resolves it:  the environment first (the
# server's .bashrc exports it), then the home path, then the volume path.
def lanes_root() -> Path:
    for candidate in (
        os.environ.get("FLEET_LANES_ROOT"),
        str(HOME / "apps" / "lanes"),
        "/mnt/fleet-extra/apps/lanes",
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return HOME / "apps" / "lanes"


def apps_root() -> Path:
    for candidate in (
        os.environ.get("FLEET_APPS_ROOT"),
        str(HOME / "apps"),
        "/mnt/fleet-extra/apps",
    ):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return HOME / "apps"


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, f"{path}.bak-{stamp()}")


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def which(name: str) -> str:
    return shutil.which(name) or f"/usr/local/bin/{name}"


# --------------------------------------------------------------------------- #
# The knobs
# --------------------------------------------------------------------------- #

BASHRC_BLOCK = """
# --- fleet: agent working directory (owner policy, Oct 22 2026) ----------------
# Every agent tool session on this host starts in the lanes root, so work lands in
# a lane (~/apps/lanes/<Repo>/<seat>-<slug>) and never in an integration tree.
# On this host ~/apps/lanes is the fleet-extra volume (/mnt/fleet-extra/apps/lanes).
if [ -d "$HOME/apps/lanes" ] && [ "$PWD" = "$HOME" ]; then
  cd "$HOME/apps/lanes"
fi
# --- end fleet working directory ----------------------------------------------
"""

OPENCODE_UNIT = """[Unit]
Description=OpenCode server (agent working directory: the lanes root)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={lanes}
Environment=HOME={home}
Environment=PATH={home}/.opencode/bin:{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The config's server block is deprecated in v2 and the port is ignored, so the
# host and port the fleet's clients use are passed explicitly.
ExecStart={opencode} serve --service{flags}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""

CLAUDE_UNIT = """[Unit]
Description=Claude Code remote control (agent working directory: the lanes root)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={lanes}
Environment=HOME={home}
Environment=PATH={home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# The OAuth token is read into memory once and never refreshed, so the process
# must not outlive the 8 hour token life.  RuntimeMaxSec recycles it, which is
# the systemd-native form of the Mac's capped launcher.
ExecStart={claude} remote-control
RuntimeMaxSec=21600
Restart=always
RestartSec=15
KillSignal=SIGTERM
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
"""


def opencode_server_flags() -> str:
    """The host and port the fleet's clients use, from the OpenCode config.

    The `server` block is deprecated in OpenCode v2 and the port is ignored when
    the server starts, so the flags carry it instead.
    """
    config = HOME / ".config" / "opencode" / "opencode.json"
    try:
        data = json.loads(read(config) or "{}")
    except json.JSONDecodeError:
        return ""
    server = data.get("server") or {}
    host, port = server.get("host"), server.get("port")
    if not host or not port:
        return ""
    return f" --hostname {host} --port {port}"


def tmux_cwd(text: str) -> str | None:
    """The directory the tmux session is created in, or None when there is none."""
    match = re.search(r"-c\s+(\S+)", text)
    return match.group(1) if match else None


def same_dir(left: str | Path | None, right: Path) -> bool:
    """True when both paths are the same directory.

    The lanes root has two spellings on a server host (`~/apps/lanes` and the
    volume path), and they are the same directory, so compare the resolved paths
    and never the strings.
    """
    if not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def knob_codex_tmux_boot(root: Path) -> tuple[bool, str]:
    path = HOME / ".local" / "bin" / "codex-tmux-boot"
    if not path.exists():
        return True, "not installed (no codex-tmux-boot)"
    cwd = tmux_cwd(read(path))
    if same_dir(cwd, root):
        return True, f"tmux session cwd is {cwd}"
    return False, f"tmux session cwd is {cwd or 'unset'}, not {root}"


def knob_codex_tmux_autostart(root: Path) -> tuple[bool, str]:
    path = HOME / ".local" / "bin" / "codex-tmux-autostart"
    if not path.exists():
        return True, "not installed (no codex-tmux-autostart)"
    cwd = tmux_cwd(read(path))
    if same_dir(cwd, root):
        return True, f"tmux session cwd is {cwd}"
    return False, f"tmux session cwd is {cwd or 'unset'}, not {root}"


def knob_acp_runtime(path: Path, root: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, f"not installed ({path.name})"
    text = read(path)
    if "FLEET_LANES_ROOT" in text and "cwd=" in text:
        return True, f"the CLI starts in {root}"
    return False, f"the CLI does not start in {root}"


def knob_bashrc(root: Path) -> tuple[bool, str]:
    path = HOME / ".bashrc"
    if not path.exists():
        return True, "not installed (no .bashrc)"
    text = read(path)
    if "fleet: agent working directory" in text:
        return True, f"interactive shells start in {root}"
    return False, "interactive shells do not start in the lanes root"


def knob_unit(path: Path, root: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, f"not installed ({path.name})"
    text = read(path)
    match = re.search(r"^WorkingDirectory=(\S+)", text, flags=re.M)
    wd = match.group(1) if match else None
    if same_dir(wd, root):
        return True, f"WorkingDirectory is {wd}"
    return False, f"WorkingDirectory is {wd or 'unset'}, not {root}"


def knobs(root: Path) -> list[tuple[str, Path, str]]:
    """(label, path, kind) for every knob this host can have."""
    apps = apps_root()
    return [
        ("codex-tmux-boot", HOME / ".local" / "bin" / "codex-tmux-boot", "codex-tmux-boot"),
        ("codex-tmux-autostart", HOME / ".local" / "bin" / "codex-tmux-autostart", "codex-tmux-autostart"),
        ("minimax acp runtime", apps / "muse-acp-runtime" / "minimax-shellular-acp.py", "acp"),
        ("muse acp runtime", apps / "muse-acp-runtime" / "muse-shellular-acp.py", "acp"),
        ("bashrc", HOME / ".bashrc", "bashrc"),
        ("opencode-server.service", SYSTEMD_DIR / "opencode-server.service", "unit"),
        ("claude-remote-control.service", SYSTEMD_DIR / "claude-remote-control.service", "unit"),
    ]


def check(root: Path) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    for label, path, kind in knobs(root):
        if kind == "codex-tmux-boot":
            ok, note = knob_codex_tmux_boot(root)
        elif kind == "codex-tmux-autostart":
            ok, note = knob_codex_tmux_autostart(root)
        elif kind == "acp":
            ok, note = knob_acp_runtime(path, root)
        elif kind == "bashrc":
            ok, note = knob_bashrc(root)
        else:
            ok, note = knob_unit(path, root)
        out.append((label, ok, note))
    return out


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #

def apply_codex(path: Path, root: Path) -> bool:
    """Point the tmux session at the lanes root."""
    text = read(path)
    cwd = tmux_cwd(text)
    if same_dir(cwd, root):
        return False
    if cwd:
        updated = text.replace(f"-c {cwd}", f"-c {root}", 1)
    elif "new-session" in text:
        # No directory named yet: name the lanes root for the new session.
        updated = text.replace("new-session", f"new-session -c {root}", 1)
    else:
        return False
    if updated == text:
        return False
    backup(path)
    write(path, updated)
    return True


def apply_acp(path: Path, root: Path) -> bool:
    """Make the ACP runtime spawn its CLI in the lanes root."""
    text = read(path)
    if "FLEET_LANES_ROOT" in text and "cwd=" in text:
        return False
    updated = text
    if "import os" not in updated:
        updated = re.sub(r"^import json$", "import json\nimport os", updated, count=1, flags=re.M)
    updated = updated.replace(
        "        text=True, bufsize=1,\n    )",
        '        text=True, bufsize=1,\n'
        '        cwd=os.environ.get("FLEET_LANES_ROOT", "%s"),\n    )' % root,
        1,
    )
    updated = updated.replace(
        "        stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1,\n    )",
        "        stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1,\n"
        '        cwd=os.environ.get("FLEET_LANES_ROOT", "%s"),\n    )' % root,
        1,
    )
    if updated == text:
        return False
    backup(path)
    write(path, updated)
    return True


def apply_bashrc(root: Path) -> bool:
    path = HOME / ".bashrc"
    text = read(path)
    if "fleet: agent working directory" in text:
        return False
    backup(path)
    with path.open("a") as handle:
        handle.write(BASHRC_BLOCK)
    return True


def apply_unit(path: Path, body: str) -> bool:
    text = read(path)
    if text == body:
        return False
    backup(path)
    write(path, body)
    return True


def apply(root: Path) -> int:
    changed = 0
    for label, path, kind in knobs(root):
        if kind in ("codex-tmux-boot", "codex-tmux-autostart"):
            touched = path.exists() and apply_codex(path, root)
        elif kind == "acp":
            touched = path.exists() and apply_acp(path, root)
        elif kind == "bashrc":
            touched = apply_bashrc(root)
        elif not path.parent.is_dir():
            print(f"  skipped  {label:26} ({path.parent} does not exist on this host)")
            continue
        elif label.startswith("opencode"):
            body = OPENCODE_UNIT.format(
                lanes=root, home=HOME, opencode=which("opencode"),
                flags=opencode_server_flags(),
            )
            touched = apply_unit(path, body)
        else:
            body = CLAUDE_UNIT.format(lanes=root, home=HOME, claude=which("claude"))
            touched = apply_unit(path, body)
        changed += 1 if touched else 0
        print(f"  {'changed' if touched else 'ok     '}  {label}")
    if changed:
        print("\n  Restart what the change affects:")
        print("    systemctl daemon-reload && systemctl restart opencode-server.service")
        print("    (the Codex tmux session picks the new cwd up when it is next created)")
    return 0


def verify(root: Path) -> int:
    failed = 0
    for label, ok, note in check(root):
        print(f"  {'PASS' if ok else 'FAIL'}  {label:26} {note}")
        failed += 0 if ok else 1
    if failed:
        print(f"\n  {failed} knob(s) are off.  Run with --apply to set them.")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the working directories")
    parser.add_argument("--verify", action="store_true", help="report the working directories")
    args = parser.parse_args(argv)

    root = lanes_root()
    print(f"lanes root: {root}")
    if not root.is_dir():
        print("  the lanes root does not exist on this host")
        return 2

    if args.apply:
        return apply(root)
    return verify(root)


if __name__ == "__main__":
    sys.exit(main())
