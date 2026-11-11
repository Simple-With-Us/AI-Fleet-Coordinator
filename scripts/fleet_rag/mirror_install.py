"""Install the Mac launchd job that pushes skill and memory trees (AFC-687).

``apply`` writes the plist and a config with ``enabled`` false, and does not
call launchctl.  ``--enable`` is the only path that sets ``enabled`` true and
bootstraps the agent.  This repository never passes ``--enable``.

The Mac side holds no Infisical token.  SSH uses the owner's existing key.
The config stores the ssh target and the remote paths, never a secret.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_rag.mirror import MirrorError, _check_root, _check_target

LABEL = "com.jay.fleet-rag-mirror"
TEMPLATE = Path(__file__).resolve().parents[1] / "launchd" / f"{LABEL}.plist"


def render_plist(
    template: str,
    *,
    home: Path,
    python: Path,
    mirror_script: Path,
    config: Path,
    log: Path,
) -> str:
    values = {
        "@HOME@": str(home),
        "@PYTHON@": str(python),
        "@MIRROR@": str(mirror_script),
        "@CONFIG@": str(config),
        "@LOG@": str(log),
    }
    for value in values.values():
        if "@" in value or "\n" in value or not value.startswith("/"):
            raise MirrorError("bad-install-path")
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(key, value)
    if "@" in rendered:
        raise MirrorError("plist-unfilled")
    return rendered


def write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def apply(
    *,
    home: Path,
    ssh_target: str,
    remote_root: str,
    remote_script: str,
    enable: bool = False,
    python: Path | None = None,
    remote_python: str = "/usr/bin/python3",
    mirror_script: Path | None = None,
    config_path: Path | None = None,
    plist_path: Path | None = None,
    log_path: Path | None = None,
    template: Path | None = None,
    bootstrap=None,
) -> dict:
    """Write the config and plist.  Bootstrap only when ``enable`` is true."""
    target = _check_target(ssh_target)
    root = _check_root(remote_root)
    if not remote_script.startswith("/") or ".." in Path(remote_script).parts:
        raise MirrorError("bad-remote-script")
    home = home.expanduser().resolve()
    python = python or Path("/opt/homebrew/bin/python3")
    mirror_script = mirror_script or Path(__file__).with_name("mirror.py")
    config_path = config_path or (
        home / "Library" / "Application Support" / "fleet-rag" / "mirror.json"
    )
    plist_path = plist_path or (home / "Library" / "LaunchAgents" / f"{LABEL}.plist")
    log_path = log_path or (home / "Library" / "Logs" / "fleet-rag-mirror.log")
    template = template or TEMPLATE
    payload = {
        "enabled": bool(enable),
        "home": str(home),
        "ssh_target": target,
        "remote_root": root,
        "remote_python": remote_python,
        "remote_script": remote_script,
    }
    write_config(config_path, payload)
    rendered = render_plist(
        template.read_text(encoding="utf-8"),
        home=home,
        python=python,
        mirror_script=mirror_script,
        config=config_path,
        log=log_path,
    )
    plist_path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    plist_path.write_text(rendered, encoding="utf-8")
    os.chmod(plist_path, 0o644)
    bootstrapped = False
    if enable:
        if bootstrap is None:
            bootstrap = _launchctl_bootstrap
        code = bootstrap(plist_path)
        if code != 0:
            raise MirrorError("bootstrap-failed")
        bootstrapped = True
    return {
        "config": config_path,
        "plist": plist_path,
        "enabled": bool(enable),
        "bootstrapped": bootstrapped,
    }


def _launchctl_bootstrap(plist: Path) -> int:
    uid = os.getuid()
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
        check=False,
    )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Fleet RAG mirror LaunchAgent.")
    parser.add_argument("--ssh-target", required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--remote-script", required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", type=Path, default=Path("/opt/homebrew/bin/python3"))
    parser.add_argument("--remote-python", default="/usr/bin/python3")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--plist", type=Path)
    parser.add_argument("--enable", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = apply(
            home=args.home,
            ssh_target=args.ssh_target,
            remote_root=args.remote_root,
            remote_script=args.remote_script,
            enable=args.enable,
            python=args.python,
            remote_python=args.remote_python,
            config_path=args.config,
            plist_path=args.plist,
        )
    except MirrorError as exc:
        sys.stderr.write(f"mirror-install: {exc}\n")
        return 1
    sys.stderr.write(
        f"mirror-install: wrote {result['plist']} enabled={str(result['enabled']).lower()}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
