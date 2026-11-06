#!/usr/bin/env python3
"""The decision behind the fleet git shim (shims/git).  Exit 77 means block; every other exit runs the real git.

    git_shim.py <the argv git was given>

The shim calls this only for a command that can make a checkout: `clone`, `worktree`, an alias, or an inline `-c alias.`.
Destinations are judged by guard.evaluate_git_argv, the same rules as the hook, with one difference:  here the mode file
decides every rule class, temp and ~/Code included, and `detect` logs without printing, because callers parse git's
output.

It fails open.  A crash, an unreadable file or more than TIME_LIMIT_S seconds exits 0, and the real git runs, with a fail-open
event in the log for the watcher.  Only fleet sources are judged, and a repo is fleet here by its origin as well as by where it
sits.  A package manager that clones a fleet repo as a git dependency (npm, pnpm, Yarn, Bun, pip, uv, SwiftPM, Homebrew) makes the
same argv an agent would, so it passes only through a row of placement-exceptions.json, and those rows cover a clone and never a
worktree.  An unknown or unresolvable destination logs nothing and passes.  A dangling lanes symlink still reads as the lanes
root, so an unmounted disk blocks no work.

A shell alias (`!...`) is read as a shell command, with the guard's own parser, and its arguments fill $1 and so on.  It has to be
read:  git puts its exec path first on PATH for such an alias, so a `git` inside it finds the real one and never comes back through
this shim.

Python 3.9 safe.  Tests: fleet_lanes/tests/test_git_shim.py, with real git in a fake home.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence

BLOCK_EXIT = 77
ENV_DENY_NOTE = "FLEET_GIT_SHIM_DENY_NOTE"  # the file the shim prints a deny reason from, since Python has none of git's streams
TIME_LIMIT_S = 3
MAX_ALIAS_DEPTH = 64
_GLOBAL_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path",
                           "--attr-source", "--config-env", "--shallow-file"})
# Builtins an alias cannot shadow, and every one this module sends to the guard.
_TERMINAL_WORDS = frozenset({"clone", "worktree", "archive", "checkout-index", "fetch", "pull", "remote"})


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
    """The text of a `!` alias, without the `!`.  `args` are what follow it on the command line."""

    args: tuple[str, ...] = ()


def _shell_alias(text: str, args: Sequence[str]) -> ShellAlias:
    alias = ShellAlias(text)
    alias.args = tuple(args)
    return alias


def expand_aliases(args: Sequence[str], real_git: str, *, configured=_configured_alias) -> list[str] | ShellAlias | None:
    """`args` with aliases replaced, a ShellAlias for a `!` alias, or None for a chain that does not end."""
    global_opts, rest = _split_global(args)
    seen: set[str] = set()
    for _ in range(MAX_ALIAS_DEPTH):
        if not rest:
            return [*global_opts, *rest]
        word = rest[0]
        if word in _TERMINAL_WORDS or word in seen:
            return [*global_opts, *rest]
        seen.add(word)
        value = _inline_alias(global_opts, word)
        if value is None:
            value = configured(real_git, global_opts, word)
        if value is None:
            return [*global_opts, *rest]
        if value.startswith("!"):
            return _shell_alias(value[1:], rest[1:])
        try:
            more_global, more_rest = _split_global(shlex.split(value))
        except ValueError:
            return None
        global_opts, rest = [*global_opts, *more_global], [*more_rest, *rest[1:]]
    return None


def with_fleet_roots(env: Mapping[str, str]) -> dict[str, str]:
    """The environment with the fleet roots filled in, in the order docs/protocols/lane-map.md gives:  the environment, then the home
    folder, then the volume the host names in FLEET_SERVER_VOLUME.  A candidate counts only when the folder is there."""
    out = dict(env)
    volume = (env.get("FLEET_SERVER_VOLUME") or "").strip()
    home = env.get("HOME") or ""
    if not volume:
        return out
    for key, rel in (("FLEET_CODE_ROOT", "Code"), ("FLEET_APPS_ROOT", "apps"), ("FLEET_LANES_ROOT", "apps/lanes")):
        if (env.get(key) or "").strip() or (home and os.path.isdir(os.path.join(home, rel))):
            continue
        candidate = os.path.join(volume, rel)
        if os.path.isdir(candidate):
            out[key] = candidate
    return out


def _origin_reader(real_git: str, env: Mapping[str, str]):
    """Reads a repo's identity:  its origin URL when it has an origin, and otherwise every remote it does have, so renaming the remote
    does not make a fleet repo someone else's."""

    def read(path: str) -> list[str]:
        clean = {k: v for k, v in env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
        try:
            res = subprocess.run([real_git, "-C", path, "config", "--get-regexp", r"^remote\..*\.url$"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, timeout=1.0, check=False, env=clean)
        except (OSError, subprocess.SubprocessError):
            return []
        rows = [line.split(None, 1) for line in res.stdout.splitlines()] if res.returncode == 0 else []
        urls = {row[0]: row[1].strip() for row in rows if len(row) == 2}
        return [urls["remote.origin.url"]] if "remote.origin.url" in urls else list(urls.values())

    return read


def _for_judging(args: Sequence[str], cwd: str, env: Mapping[str, str]) -> list[str]:
    """The argv as the guard should read it:  GIT_DIR and GIT_WORK_TREE in the environment spelled as the flags, and a fetch from a
    named remote spelled as that remote's URL, since this one process has not seen the `remote add` that named it."""
    global_opts, rest = _split_global(args)
    for name, flag in (("GIT_WORK_TREE", "--work-tree"), ("GIT_DIR", "--git-dir")):
        value = (env.get(name) or "").strip()
        if value and not any(a == flag or a.startswith(f"{flag}=") for a in global_opts):
            global_opts = [flag, value, *global_opts]
    if rest and rest[0] in ("fetch", "pull"):
        named = [a for a in rest[1:] if not a.startswith("-")]
        name = named[0] if named else "origin"
        if "/" not in name and ":" not in name:
            try:
                # Through the same options as the command, so -C, --git-dir and GIT_DIR all find the repo it will really write to.
                res = subprocess.run([env.get("FLEET_GIT_SHIM_REAL") or "git", *global_opts, "config", "--get", f"remote.{name}.url"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=1.0, check=False, cwd=cwd)
                url = res.stdout.strip() if res.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                url = ""
            if url:
                rest = [rest[0], url]
    return [*global_opts, *rest]


def decide(args: Sequence[str], cwd: str, env: Mapping[str, str], *, stderr=None) -> int:
    """The exit code for this argv.  Writes the deny reason when it blocks, and logs anything it would block."""
    from fleet_lanes import guard, placement

    stderr = sys.stderr if stderr is None else stderr  # None too when fd 2 is closed, and that changes nothing below
    env = with_fleet_roots(env)
    home = env.get("HOME") or os.path.expanduser("~")
    mode = placement.read_mode(home, env)
    if mode.mode == placement.MODE_OFF:
        return 0
    expanded = expand_aliases(args, env.get("FLEET_GIT_SHIM_REAL") or "git")
    if expanded is None:
        return 0
    ctx = guard.make_context(env, home=home)
    origin_of = _origin_reader(env.get("FLEET_GIT_SHIM_REAL") or "git", env)
    others: list = []
    if isinstance(expanded, ShellAlias):
        verdict = guard.evaluate_shell_text(expanded, cwd, ctx, args=expanded.args, origin_of=origin_of, others=others)
    else:
        verdict = guard.evaluate_git_argv(_for_judging(expanded, cwd, env), cwd, ctx, origin_of=origin_of, others=others)
    if verdict is None:
        return 0
    deny = None
    for cls, decision in (verdict, *others):
        if cls == placement.CLASS_ISOLATION and mode.isolation == placement.MODE_OFF:
            continue  # the owner has not ruled on harness worktrees, and `block code` must not rule for them
        blocked = cls != guard.CLASS_EXCUSED and mode.blocks(cls)
        event = {
            "via": "shim", "action": "excused" if cls == guard.CLASS_EXCUSED else "block" if blocked else "detect", "class": cls,
            "rule": decision.rule_id, "dest": decision.destination, "source": decision.source, "cwd": cwd,
            "seat": env.get("AGENT_SEAT", ""), "host": placement.host_kind(env),
        }
        if ctx.exceptions_problem:
            event["exceptions"] = "unusable"  # judged with no rows, so the watcher reports the file and not this
        placement.log_event(home, event, env)
        if blocked and deny is None:
            deny = decision
    if deny is None:
        return 0
    note = env.get(ENV_DENY_NOTE)
    try:
        if note:
            with open(note, "w", encoding="utf-8") as fh:
                fh.write(f"fatal: {deny.reason}\n")
        else:
            stderr.write(f"fatal: {deny.reason}\n")
    except (OSError, ValueError, AttributeError):
        pass  # no way to say why changes nothing:  the event is logged and the command stays blocked
    return BLOCK_EXIT


def _alarm(_signum, _frame):
    """Out of time.  Leave a trace with no imports and nothing slow, then run the real git.  A second alarm kills this process if
    the write itself hangs, and any exit other than BLOCK_EXIT still runs git."""
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(1)
    try:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        folder = os.environ.get("FLEET_LANE_GUARD_STATE_DIR") or os.path.join(home, ".local", "state", "fleet")
        if os.environ.get("FLEET_LANES_TEST_RUN"):
            import pwd  # only in a test run:  it must not reach the real log (placement.writes_real_log_in_a_test)

            if os.path.realpath(folder) == os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local", "state", "fleet")):
                os._exit(0)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        fd = os.open(os.path.join(folder, "lane-guard-events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.write(fd, f'{{"action": "fail-open", "at": "{at}", "detail": "time-limit", "via": "shim"}}\n'.encode())
        os.close(fd)
    finally:
        os._exit(0)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    previous = signal.getsignal(signal.SIGALRM)
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
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    sys.exit(main())
