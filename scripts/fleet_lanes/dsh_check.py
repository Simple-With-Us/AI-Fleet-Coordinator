"""dsh_check: prove, with the pinned `dsh` and on a scratch copy, that a home-level cordis patch MERGES and PARSES, and
that the plugins it names resolve.

    check_patch(dsh_bin, patch_text, profiles_dir, expect=("@deepseek-ai/dsh-hooks-claude-code",))

Two checks, in this order:

  1. `unresolved`: each plugin package must resolve under the REAL `<DSH_HOME>/profiles/node_modules` (a link that
     points at a path that is gone does not).  The engine repairs those links only when it boots, and the home-level
     patch is read by the profiles that run: a `patchReload: live` profile (clutch-web's `web`) re-applies it on every
     write, a `startup` profile at its next start.  A plugin whose link dangles is therefore loaded into a process
     that cannot find it.
  2. `dump_config`: copies ONLY `<DSH_HOME>/profiles` (the profile definitions and their plugin links; never
     settings.yaml or the credentials file) into a throwaway DSH_HOME, writes the patch there, and runs
     `dsh --profile <name> --dump-config` for each profile.  It passes when every run exits 0 and its output names every
     expected plugin.  This is a MERGE-AND-PARSE check:  `--dump-config` composes the patch layers without booting or
     evaluating `!!js` and never resolves a package, so the plugin name appears in the dump whenever the patch under test
     contains it.  It catches a patch the engine would reject (an empty or comment-only patch exits 1); it does not
     prove the plugin loads.

The output is searched in memory and never printed, saved or returned.  Nothing under the real DSH_HOME is read except
the profile folders and their `package.json`, and nothing there is written.

Python 3.9 safe.  Tests: fleet_lanes/tests/test_dsh_check.py.
"""
from __future__ import annotations

import json
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


def patch_reload_modes(profiles_dir: str) -> Dict[str, str]:
    """Profile name -> its `dsh.profile.patchReload` (`live` or `startup`; `live (default)` when the profile does not
    say, because dsh documents live as the default for a custom profile; `unknown` when package.json cannot be read)."""
    modes: Dict[str, str] = {}
    for name in profile_names(profiles_dir):
        try:
            with open(os.path.join(profiles_dir, name, "package.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            mode = doc["dsh"]["profile"].get("patchReload")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            modes[name] = "unknown"
            continue
        modes[name] = mode if mode in ("live", "startup") else ("live (default)" if mode is None else "unknown")
    return modes


def reload_note(profiles_dir: str) -> str:
    """One sentence on when each profile reads the home-level patch."""
    modes = patch_reload_modes(profiles_dir)
    if not modes:
        return "no profile found, so when the patch is read is unknown"
    live = [n for n, m in modes.items() if m.startswith("live")]
    other = [n for n, m in modes.items() if not m.startswith("live")]
    parts = ["patchReload: " + ", ".join("%s %s" % (n, m) for n, m in modes.items())]
    if live:
        parts.append("this write reaches the running %s engine at once (it watches the file)" % "/".join(live))
    if other:
        parts.append("%s read it at their next start" % "/".join(other))
    return "; ".join(parts)


def unresolved(profiles_dir: str, packages: Sequence[str]) -> List[str]:
    """One line per plugin package that does not resolve under `<profiles>/node_modules` (a missing link, or a link
    to a path that is gone).  Empty means every one resolves."""
    root = os.path.join(profiles_dir, "node_modules")
    out: List[str] = []
    for pkg in packages:
        path = os.path.join(root, *pkg.split("/"))
        if os.path.exists(path):
            continue
        if os.path.islink(path):
            out.append("%s is a link to %s, which no longer exists" % (pkg, os.readlink(path)))
        else:
            out.append("%s is not linked under %s" % (pkg, root))
    return out


def check_patch(dsh_bin: str, patch_text: str, profiles_dir: str, expect: Sequence[str],
                timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    """(ok, detail): the plugins must resolve under the real profiles' node_modules, then the patch must merge and
    parse (dump_config).  The detail of a pass ends with when each profile reads the patch."""
    bad = unresolved(profiles_dir, expect)
    if bad:
        return False, ("%s.  Restart clutch-web first (the engine repairs these links when it starts), check that the "
                       "links resolve, then run this again" % "; ".join(bad))
    ok, detail = dump_config(dsh_bin, patch_text, profiles_dir, expect, timeout=timeout)
    return (True, detail + "; " + reload_note(profiles_dir)) if ok else (False, detail)


def _env(scratch: str, dsh_home: str) -> Dict[str, str]:
    """The environment of the scratch run: PATH (node is found through it), a scratch HOME and DSH_HOME, and nothing
    else, so no inherited DSH_*, CLUTCH_* or credential variable reaches the engine."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "HOME": os.path.join(scratch, "home"),
            "DSH_HOME": dsh_home, "NO_COLOR": "1"}


def dump_config(dsh_bin: str, patch_text: str, profiles_dir: str, expect: Sequence[str],
                timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    """(ok, detail).  `detail` names the profiles that passed, or the first thing that failed (no engine output).
    Merge-and-parse only:  see the module docstring."""
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
                return False, "profile %s: the dump does not name %s" % (name, ", ".join(missing))
            passed.append(name)
        return True, "%s: the patch merges and the dump names %s (a merge-and-parse check, not a plugin load)" % (
            ", ".join(passed), ", ".join(expect))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
