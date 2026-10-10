"""dsh_check: prove that a home-level cordis patch mounts what we think, with the pinned `dsh`, on a scratch copy.

Clutch's engine reads `$DSH_HOME/cordis.patch.yml` once, at start, and applies it to every profile.  A bad patch
therefore breaks the NEXT restart of `clutch-web`, not the running one, so the proof has to come before the write:

    dump_config(dsh_bin, patch_text, profiles_dir, expect=("@deepseek-ai/dsh-hooks-claude-code",))

copies ONLY `<DSH_HOME>/profiles` (the profile definitions and their plugin links; never settings.yaml or the
credentials file) into a throwaway DSH_HOME, writes the patch there, and runs `dsh --profile <name> --dump-config`
for each profile.  It passes when every run exits 0 and its output names every expected plugin.  The output is
searched in memory and never printed, saved or returned.  Nothing under the real DSH_HOME is read except the
profile folders, and nothing there is written.

Python 3.9 safe.  Tests: fleet_lanes/tests/test_dsh_check.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_TIMEOUT = 120.0
PATCH_NAME = "cordis.patch.yml"


def find_dsh(home: str) -> Optional[str]:
    """The pinned engine binary of the standalone Clutch clone, or None."""
    cand = os.path.join(home, "apps", "clutch-runtime", "node_modules", ".bin", "dsh")
    return cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None


def profile_names(profiles_dir: str) -> List[str]:
    """Folder names under `profiles` that define a profile (they hold a cordis.yml), never `node_modules`."""
    try:
        names = sorted(os.listdir(profiles_dir))
    except OSError:
        return []
    return [n for n in names if n != "node_modules" and os.path.isfile(os.path.join(profiles_dir, n, "cordis.yml"))]


def _env(scratch: str, dsh_home: str) -> Dict[str, str]:
    """The environment of the scratch run: PATH (node is found through it), a scratch HOME and DSH_HOME, and nothing
    else, so no inherited DSH_*, CLUTCH_* or credential variable reaches the engine."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "HOME": os.path.join(scratch, "home"),
            "DSH_HOME": dsh_home, "NO_COLOR": "1"}


def dump_config(dsh_bin: str, patch_text: str, profiles_dir: str, expect: Sequence[str],
                timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    """(ok, detail).  `detail` names the profiles that passed, or the first thing that failed (no engine output)."""
    profiles = profile_names(profiles_dir)
    if not profiles:
        return False, "no profile with a cordis.yml under %s, so there is nothing to dump" % profiles_dir
    scratch = tempfile.mkdtemp(prefix="fleet-dsh-check-")
    try:
        dsh_home = os.path.join(scratch, "dsh")
        os.makedirs(os.path.join(scratch, "home"))
        os.makedirs(dsh_home)
        shutil.copytree(profiles_dir, os.path.join(dsh_home, "profiles"), symlinks=True)
        with open(os.path.join(dsh_home, PATCH_NAME), "w", encoding="utf-8") as fh:
            fh.write(patch_text)
        passed: List[str] = []
        for name in profiles:
            try:
                proc = subprocess.run([dsh_bin, "--profile", name, "--dump-config"], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(scratch, dsh_home),
                                      timeout=timeout)
            except subprocess.TimeoutExpired:
                return False, "profile %s: dsh --dump-config timed out after %gs" % (name, timeout)
            except OSError as exc:
                return False, "cannot run %s: %s" % (dsh_bin, exc.strerror or exc)
            if proc.returncode != 0:
                tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
                return False, "profile %s: dsh --dump-config exited %d (%s)" % (name, proc.returncode, tail[0][:160])
            text = proc.stdout.decode("utf-8", "replace")
            missing = [e for e in expect if e not in text]
            if missing:
                return False, "profile %s: the dump does not mount %s" % (name, ", ".join(missing))
            passed.append(name)
        return True, "%s mount%s %s" % (", ".join(passed), "" if len(passed) != 1 else "s", ", ".join(expect))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
