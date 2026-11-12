"""Which common shell shapes find the git shim (AFC-618).  It changes nothing, and it does start those shells.

    python3 -m fleet_lanes.lane ls --strict-placement    (or `--shim-reach`, which fails no check either way)

The shim works only where it is first on PATH, and PATH depends on how the shell was started:  on the Mac, `~/bin` first came from
`.zshrc`, so `zsh -lc`, `bash -lc` and launchd jobs found /usr/bin/git.  Each shape starts from launchd's default PATH, the way a
process started by a GUI app does, with only HOME and USER beside it, so what it reports is what that shape's own dotfiles arrange.
The dotfiles do run, so this stays behind a flag and never runs in the watcher's hourly sweep.  README.md, "How far the shim reaches",
names the one line the owner can approve, and says the watcher is the backstop for what no dotfile reaches.

Python 3.9 safe.  Tests: fleet_lanes/tests/test_shim_reach.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

from . import placement as P

SHIM_MARK = b"Fleet git shim"
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # launchd's default, and what a shell gets before its dotfiles run
SHELL_SHAPES = (("zsh -c", "zsh", "-c"), ("zsh -lc", "zsh", "-lc"), ("zsh -ic", "zsh", "-ic"),
                ("bash -c", "bash", "-c"), ("bash -lc", "bash", "-lc"), ("bash -ic", "bash", "-ic"))
PROBE_TIMEOUT_S = 15.0


def is_shim(path: str | None) -> bool:
    try:
        with open(path or "", "rb") as fh:
            return SHIM_MARK in fh.read(4096)
    except OSError:
        return False


def _row(shape: str, git: str | None, note: str = "") -> dict:
    """`reaches` is None when the shape could not be asked, which is not the same as no."""
    reached = None if note else is_shim(git)
    return {"shape": shape, "git": git, "reaches": reached, "note": note}


def _ask(shell: str, flag: str, home: str, env: Mapping[str, str], run: Callable[..., Any]) -> tuple[str | None, str]:
    exe = shutil.which(shell, path=BASE_PATH) or shutil.which(shell, path=env.get("PATH") or "")
    if not exe:
        return None, f"no {shell} here"
    child = {"HOME": home, "USER": env.get("USER", ""), "PATH": BASE_PATH, "TERM": "dumb"}
    try:
        res = run([exe, flag, "command -v git"], env=child, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=PROBE_TIMEOUT_S, check=False, cwd=home)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, type(exc).__name__
    # An interactive shell's dotfiles can print whatever they like before this answer.
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip().startswith("/")]
    return (lines[-1], "") if lines else (None, "found no git")


def shim_reach(home: str | os.PathLike[str], env: Mapping[str, str] | None = None, *,
               run: Callable[..., Any] = subprocess.run) -> dict:
    """One row per shape, and whether a shim exists at all, so `no` everywhere with no shim installed reads as what it is."""
    env = os.environ if env is None else env
    home = os.fspath(home)
    rows = [_row(label, *_ask(shell, flag, home, env, run)) for label, shell, flag in SHELL_SHAPES]
    if P.host_kind(env) == P.HOST_MAC and (sys.platform == "darwin" or env.get(P.ENV_HOST_KIND)):
        rows.append(_row("launchd job, default PATH", shutil.which("git", path=BASE_PATH)))
    installed = is_shim(os.path.join(home, "bin", "git")) or any(row["reaches"] for row in rows)
    return {"installed": installed, "rows": rows}


def render(reach: Mapping[str, Any]) -> list[str]:
    rows = reach["rows"]
    out = [f"SHIM REACH ({sum(1 for r in rows if r['reaches'])} of {len(rows)} shapes find the git shim)"]
    if not reach["installed"]:
        out.append("  no git shim is installed for this account")
    for r in rows:
        word = "yes" if r["reaches"] else "unknown" if r["reaches"] is None else "no"
        out.append(f"  {r['shape']:<26} {word:<8} {r['note'] or r['git'] or ''}")
    if any(not r["reaches"] for r in rows):
        out.append('  What the shim misses, the watcher reports afterwards.  scripts/fleet_lanes/README.md, "How far the shim reaches".')
    return out
