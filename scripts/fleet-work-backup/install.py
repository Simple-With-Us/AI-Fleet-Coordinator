#!/usr/bin/env python3
"""Install fleet-work-backup into this Mac's home.  Idempotent.

  python3 scripts/fleet-work-backup/install.py --dry-run          # show the plan, change nothing
  python3 scripts/fleet-work-backup/install.py --skip-launchd     # copy the script only
  python3 scripts/fleet-work-backup/install.py                    # copy the script and load the LaunchAgent
  python3 scripts/fleet-work-backup/install.py --install-restic   # also 'brew install restic' if it is missing

What it does, from a checkout of AI-Fleet-Coordinator:
  1. copies fleet_work_backup.py to ~/apps/fleet-work-backup/ (a replaced copy is kept as
     <file>.bak-fwb-<stamp>);
  2. checks the prerequisites and prints what is missing:  restic, the four credential NAMES in
     ~/.secrets/global-api-keys (names only, never values), gitleaks (optional);
  3. copies scripts/launchd/com.jay.fleet-work-backup.plist to ~/Library/LaunchAgents/ and boots it
     out and in when it changed or is not loaded.  RunAtLoad is false, so loading never starts a
     backup.  It refuses to load the agent while a prerequisite is missing, unless --force.

It never creates the B2 key, never writes to Infisical, never runs 'restic init' and never starts
a backup.  The order is in README.md:  key, Infisical, sync, restic install, init, install, grant
Removable Volumes, check-access.  --check-repo asks restic for the repository config (read-only)
to confirm the init was done.
"""
from __future__ import annotations

import argparse
import filecmp
import importlib.util
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
STAMP = time.strftime("%Y%m%d-%H%M%S")
LABEL = "com.jay.fleet-work-backup"
SCRIPT_SRC = os.path.join(HERE, "fleet_work_backup.py")
SCRIPT_DST = os.path.join(HOME, "apps", "fleet-work-backup", "fleet_work_backup.py")
PLIST_SRC = os.path.join(os.path.dirname(HERE), "launchd", f"{LABEL}.plist")
PLIST_DST = os.path.join(HOME, "Library", "LaunchAgents", f"{LABEL}.plist")


def say(msg: str) -> None:
    print(f"install-fleet-work-backup: {msg}")


def load_module():
    spec = importlib.util.spec_from_file_location("fleet_work_backup", SCRIPT_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_work_backup"] = mod
    spec.loader.exec_module(mod)
    return mod


def copy(src: str, dst: str, mode: int, dry: bool) -> bool:
    """Copy src over dst when they differ.  Returns True when dst changed (or would)."""
    if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
        say(f"unchanged {dst}")
        return False
    if dry:
        say(f"would {'update' if os.path.exists(dst) else 'create'} {dst}")
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        shutil.copy2(dst, f"{dst}.bak-fwb-{STAMP}")
    shutil.copy2(src, dst)
    os.chmod(dst, mode)
    say(f"installed {dst}")
    return True


def prerequisites(fwb, install_restic: bool, dry: bool) -> list[str]:
    cfg = fwb.config_from_env()
    missing: list[str] = []
    if not shutil.which(cfg.restic_bin):
        if install_restic and not dry:
            say("running: brew install restic")
            subprocess.run(["brew", "install", "restic"], check=False)
        elif install_restic:
            say("would run: brew install restic")
    if shutil.which(cfg.restic_bin):
        say(f"restic: {shutil.which(cfg.restic_bin)}")
    else:
        missing.append("restic (brew install restic, or pass --install-restic)")
    env, names = fwb.restic_env(cfg)
    if env is None:
        missing.append("credentials in ~/.secrets/global-api-keys or the environment: " + ", ".join(names))
    else:
        say("credentials: all four names are set (values not shown)")
    gl = shutil.which(cfg.gitleaks_bin) if cfg.gitleaks_bin else None
    say(f"gitleaks: {gl or 'not installed (the regex scan still runs)'}")
    return missing


def launchd(changed: bool, dry: bool) -> None:
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
    if loaded and not changed:
        say(f"unchanged {LABEL} (loaded)")
        return
    if dry:
        say(f"would {'reload' if loaded else 'bootstrap'} {LABEL}")
        return
    if loaded:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        time.sleep(1)
    rc = subprocess.run(["launchctl", "bootstrap", domain, PLIST_DST]).returncode
    say(f"bootstrap {LABEL} rc={rc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install fleet-work-backup.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-launchd", action="store_true")
    ap.add_argument("--install-restic", action="store_true", help="run 'brew install restic' when it is missing")
    ap.add_argument("--check-repo", action="store_true", help="ask restic for the repository config (read-only)")
    ap.add_argument("--force", action="store_true", help="load the LaunchAgent even with a prerequisite missing")
    a = ap.parse_args()
    if sys.platform != "darwin":
        say("macOS only")
        return 1
    fwb = load_module()
    copy(SCRIPT_SRC, SCRIPT_DST, 0o755, a.dry_run)
    missing = prerequisites(fwb, a.install_restic, a.dry_run)
    if a.check_repo:
        cfg = fwb.config_from_env()
        env, _ = fwb.restic_env(cfg)
        if env is None or not shutil.which(cfg.restic_bin):
            missing.append("--check-repo needs restic and credentials")
        else:
            rc = fwb.run_restic(cfg, env, "cat", "config", timeout=120).returncode
            say("repository: " + ("initialised" if rc == 0 else "NOT initialised or unreachable: run fleet_work_backup.py init"))
            if rc != 0:
                missing.append("restic repository (fleet_work_backup.py init)")
    for m in missing:
        say(f"MISSING {m}")
    if not a.skip_launchd:
        if missing and not a.force:
            say("not loading the LaunchAgent until the above is fixed (or pass --force)")
        else:
            changed = copy(PLIST_SRC, PLIST_DST, 0o644, a.dry_run)
            launchd(changed, a.dry_run)
    say("next: grant Removable Volumes to the interpreter, then run 'python3 "
        f"{SCRIPT_DST} check-access'")
    say(f"      the interpreter to grant is {os.path.realpath('/opt/homebrew/bin/python3')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
