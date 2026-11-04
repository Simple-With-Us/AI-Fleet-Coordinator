#!/usr/bin/env python3
"""The decision behind the fleet git shim (shims/git).  Exit 77 means block; every other exit runs the real git.

    git_shim.py <the argv git was given>

The shim calls this only for a command that can make a checkout: `clone`, `worktree`, an alias, or an inline `-c alias.`.
Destinations are judged by guard.evaluate_git_argv, the same rules as the hook, with one difference:  here the mode file
decides every rule class, temp and ~/Code included, and `detect` logs without printing, because callers parse git's
output.

It fails open.  A crash, an unreadable file or more than TIME_LIMIT_S seconds exits 0, and the real git runs.  Fleet sources
only, so brew, SwiftPM and npm pass.  An unknown or unresolvable destination logs nothing and passes.  A dangling lanes
symlink still reads as the lanes root, so an unmounted disk blocks no work.

A shell alias (`!...`) is read as a shell command, with the guard's own parser.  It has to be:  git puts its exec path
first on PATH for such an alias, so a `git` inside it finds the real one and never comes back through this shim.

Python 3.9 safe.  Tests: fleet_lanes/tests/test_git_shim.py, with real git in a fake home.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence

BLOCK_EXIT = 77
TIME_LIMIT_S = 3
MAX_ALIAS_DEPTH = 8
_GLOBAL_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path",
                           "--attr-source", "--config-env"})


def _split_global(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """(global options, subcommand and its arguments)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GLOBAL_VALUE:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    return list(args[:i]), list(args[i:])


def _inline_alias(global_opts: Sequence[str], word: str) -> str | None:
    """The value of `-c alias.WORD=...` on the command line, the last one winning, as git does."""
    value = None
    want = f"alias.{word}=".casefold()
    for i, a in enumerate(global_opts):
        if a == "-c" and i + 1 < len(global_opts):
            text = global_opts[i + 1]
            if text[: len(want)].casefold() == want:
                value = text[len(want):]
    return value


def _configured_alias(real_git: str, global_opts: Sequence[str], word: str) -> str | None:
    try:
        res = subprocess.run([real_git, *global_opts, "config", "--get", f"alias.{word}"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


class ShellAlias(str):
    """The text of a `!` alias, without the `!`."""


def expand_aliases(args: Sequence[str], real_git: str, *, configured=_configured_alias) -> list[str] | ShellAlias | None:
    """`args` with aliases replaced, a ShellAlias for a `!` alias, or None for a chain that does not end."""
    global_opts, rest = _split_global(args)
    seen: set[str] = set()
    for _ in range(MAX_ALIAS_DEPTH):
        if not rest:
            return [*global_opts, *rest]
        word = rest[0]
        if word in ("clone", "worktree") or word in seen:
            return [*global_opts, *rest]
        seen.add(word)
        value = _inline_alias(global_opts, word)
        if value is None:
            value = configured(real_git, global_opts, word)
        if value is None:
            return [*global_opts, *rest]
        if value.startswith("!"):
            return ShellAlias(value[1:])
        try:
            more_global, more_rest = _split_global(shlex.split(value))
        except ValueError:
            return None
        global_opts, rest = [*global_opts, *more_global], [*more_rest, *rest[1:]]
    return None


def decide(args: Sequence[str], cwd: str, env: Mapping[str, str], *, stderr=None) -> int:
    """The exit code for this argv.  Writes the deny reason when it blocks, and logs anything it would block."""
    from fleet_lanes import guard, placement

    stderr = sys.stderr if stderr is None else stderr
    home = env.get("HOME") or os.path.expanduser("~")
    mode = placement.read_mode(home, env)
    if mode.mode == placement.MODE_OFF:
        return 0
    expanded = expand_aliases(args, env.get("FLEET_GIT_SHIM_REAL") or "git")
    if expanded is None:
        return 0
    ctx = guard.make_context(env, home=home)
    if isinstance(expanded, ShellAlias):
        verdict = guard.evaluate_shell_text(expanded, cwd, ctx)
    else:
        verdict = guard.evaluate_git_argv(expanded, cwd, ctx)
    if verdict is None:
        return 0
    cls, decision = verdict
    blocked = mode.blocks(cls)
    placement.log_event(home, {
        "via": "shim", "action": "block" if blocked else "detect", "class": cls, "rule": decision.rule_id,
        "dest": decision.destination, "source": decision.source, "cwd": cwd, "seat": env.get("AGENT_SEAT", ""),
        "host": placement.host_kind(env),
    }, env)
    if not blocked:
        return 0
    stderr.write(f"fatal: {decision.reason}\n")
    return BLOCK_EXIT


def _alarm(_signum, _frame):
    os._exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(TIME_LIMIT_S)
        here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        if here not in sys.path:
            sys.path.insert(0, here)
        return decide(args, os.getcwd(), os.environ)
    except Exception as exc:  # noqa: BLE001 - the shim must never stop git
        try:
            from fleet_lanes import placement

            home = os.environ.get("HOME") or os.path.expanduser("~")
            placement.log_event(home, {"via": "shim", "action": "fail-open", "detail": type(exc).__name__})
        except Exception:  # noqa: BLE001
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
