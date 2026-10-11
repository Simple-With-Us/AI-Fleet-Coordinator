#!/usr/bin/env python3
"""Install fleet-work-backup into this Mac's home.  Idempotent.

  python3 scripts/fleet-work-backup/install.py --dry-run          # show the plan, change nothing
  python3 scripts/fleet-work-backup/install.py --skip-launchd     # copy the script only
  python3 scripts/fleet-work-backup/install.py                    # copy the script and load the LaunchAgent
  python3 scripts/fleet-work-backup/install.py --install-restic   # also 'brew install restic' if it is missing
  python3 scripts/fleet-work-backup/install.py --interpreter /opt/homebrew/bin/python3   # not Apple's python3

What it does, from a checkout of AI-Fleet-Coordinator:
  1. copies fleet_work_backup.py to ~/apps/fleet-work-backup/ (a replaced copy is kept as
     <file>.bak-fwb-<stamp>);
  2. checks the prerequisites and prints what is missing:  restic, the four credential NAMES in
     ~/.secrets/global-api-keys (names only, never values), the interpreter, gitleaks (optional);
  3. copies scripts/launchd/com.jay.fleet-work-backup.plist to ~/Library/LaunchAgents/ and boots it
     out and in when it changed or is not loaded.  RunAtLoad is false, so loading never starts a
     backup.  It refuses to load the agent while a prerequisite is missing, unless --force;
  4. asks the plist's interpreter which executable macOS privacy settings see for it, and prints that
     (the bundle to grant Removable Volumes), then the command that verifies the grant under launchd.

It never creates the B2 key, never writes to Infisical, never runs 'restic init' and never starts
a backup.  The order is in README.md:  key, Infisical, sync, restic install, init, install, grant
Removable Volumes, check-access --launchd.  --check-repo asks restic for the repository config
(read-only) to confirm the init was done.
"""
from __future__ import annotations

import argparse
import filecmp
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
STAMP = time.strftime("%Y%m%d-%H%M%S")
LABEL = "com.jay.fleet-work-backup"
DEFAULT_INTERPRETER = "/usr/bin/python3"
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


def plist_with_interpreter(interpreter: str) -> str:
    """The plist text with ProgramArguments[0] set to ``interpreter`` (a temp file's path)."""
    with open(PLIST_SRC, encoding="utf-8") as fh:
        text = fh.read()
    if interpreter != DEFAULT_INTERPRETER:
        marker = f"<string>{DEFAULT_INTERPRETER}</string>"
        if text.count(marker) != 1:
            raise SystemExit("the plist no longer has exactly one interpreter line;  edit it by hand")
        text = text.replace(marker, f"<string>{interpreter}</string>")
    tmp = os.path.join(tempfile.mkdtemp(prefix="fwb-plist-"), f"{LABEL}.plist")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    return tmp


def interpreter_image(interpreter: str, script: str) -> tuple[str, str]:
    """Ask the interpreter itself for the executable macOS privacy settings see (and its .app bundle):
    this installer's own interpreter is not necessarily the plist's."""
    try:
        p = subprocess.run([interpreter, script, "image"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return "", ""
    lines = p.stdout.splitlines() if p.returncode == 0 else []
    image = lines[0] if lines else ""
    app = next((l.split(": ", 1)[1] for l in lines if l.startswith("app bundle: ")), "")
    return image, app


def prerequisites(fwb, interpreter: str, install_restic: bool, dry: bool) -> list[str]:
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
    try:
        ok = subprocess.run([interpreter, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"],
                            capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if ok:
        say(f"interpreter: {interpreter}")
    else:
        missing.append(f"interpreter {interpreter} (Python 3.9 or later)")
    env, names = fwb.restic_env(cfg)
    if env is None:
        missing.append("credentials in ~/.secrets/global-api-keys or the environment: " + ", ".join(names))
    else:
        say("credentials: all four names are set (values not shown)")
    gl = shutil.which(cfg.gitleaks_bin) if cfg.gitleaks_bin else None
    say(f"gitleaks: {gl or 'not installed (the regex scan still runs, and the run line says warn=gitleaks-missing)'}")
    return missing


def run_launchctl(*args: str) -> int:
    """launchctl with a time limit;  127 when it cannot run, 124 when it hangs."""
    try:
        return subprocess.run(["launchctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def launchd(changed: bool, dry: bool) -> int:
    """Load (or reload) the LaunchAgent.  Returns 0 when it is loaded or nothing was needed, else launchctl's code."""
    domain = f"gui/{os.getuid()}"
    printed = run_launchctl("print", f"{domain}/{LABEL}")
    if printed in (124, 127):
        say(f"launchctl print {LABEL} did not run (rc={printed})")
        return printed
    loaded = printed == 0
    if loaded and not changed:
        say(f"unchanged {LABEL} (loaded)")
        return 0
    if dry:
        say(f"would {'reload' if loaded else 'bootstrap'} {LABEL}")
        return 0
    if loaded:
        run_launchctl("bootout", f"{domain}/{LABEL}")
        time.sleep(1)
    rc = run_launchctl("bootstrap", domain, PLIST_DST)
    say(f"bootstrap {LABEL} rc={rc}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Install fleet-work-backup.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-launchd", action="store_true")
    ap.add_argument("--install-restic", action="store_true", help="run 'brew install restic' when it is missing")
    ap.add_argument("--check-repo", action="store_true", help="ask restic for the repository config (read-only)")
    ap.add_argument("--force", action="store_true", help="load the LaunchAgent even with a prerequisite missing")
    ap.add_argument("--interpreter", default=DEFAULT_INTERPRETER,
                    help="python3 the LaunchAgent runs (default Apple's /usr/bin/python3, whose signature keeps a "
                         "privacy grant across updates;  Homebrew's loses it on every upgrade)")
    a = ap.parse_args()
    if sys.platform != "darwin":
        say("macOS only")
        return 1
    fwb = load_module()
    copy(SCRIPT_SRC, SCRIPT_DST, 0o755, a.dry_run)
    missing = prerequisites(fwb, a.interpreter, a.install_restic, a.dry_run)
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
    status = 0
    if not a.skip_launchd:
        if missing and not a.force:
            say("not loading the LaunchAgent until the above is fixed (or pass --force)")
        else:
            plist = plist_with_interpreter(a.interpreter)
            changed = copy(plist, PLIST_DST, 0o644, a.dry_run)
            status = launchd(changed, a.dry_run)
            if status:
                say(f"the LaunchAgent is NOT loaded (launchctl rc={status})")
    image, app = interpreter_image(a.interpreter, SCRIPT_SRC)
    if image:
        say(f"macOS privacy settings see this executable for {a.interpreter}: {image}")
        say(f"grant Removable Volumes (or Full Disk Access) in System Settings > Privacy & Security to: {app or image}")
    else:
        say(f"could not ask {a.interpreter} for its executable;  run 'python3 {SCRIPT_DST} image' with it")
    say(f"then verify it under launchd, not Terminal:  {a.interpreter} {SCRIPT_DST} check-access --launchd")
    return 1 if status else 0


if __name__ == "__main__":
    sys.exit(main())
