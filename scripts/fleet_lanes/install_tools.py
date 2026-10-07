"""Install the lane tools and the temp-checkout guard hook from a STABLE copy, wire the hook into each
platform that has a hook surface, and PROVE the install works.

Why a stable copy.  A hook that lives inside a lane breaks when the lane retires.  One that lives in
~/Code/AI-Fleet-Coordinator goes stale, because that tree lags main.  So `apply tools` copies the
package into one directory the owner never edits, and every hook command points at a shim there.

Why a proof.  The guard fails open on every error (see lane_guard_hook.py), so a broken install
looks exactly like a working one: exit 0, no output.  `verify` therefore never trusts that an entry
exists.  It takes the hook command string back OUT of the platform's config file, runs it the way a
shell would with a payload the guard must deny, asserts the deny bytes of that platform's format,
runs a payload it must allow, and asserts empty stdout.  A command that cannot be found, exits
non-zero, times out, or prints nothing is a FAIL.

    cd scripts
    python3 -m fleet_lanes.install_tools plan   [--home H] [PLATFORM ...]         # read-only
    python3 -m fleet_lanes.install_tools apply  [--home H] tools | PLATFORM ... | all
    python3 -m fleet_lanes.install_tools verify [--home H] [tools | PLATFORM ... | all]
    python3 -m fleet_lanes.install_tools --self-test

PLATFORM is claude, codex, grok, antigravity, cursor or muse (muse is listed but never written: its
hook surface is UNVERIFIED).  `all` is `tools` plus every supported platform.  `apply` needs a target;
`plan` defaults to all (muse listed) and `verify` to all.

Options (a subcommand accepts the ones that make sense for it):

    --home H            read and write under H instead of $HOME (tests and demos use a temp fake home).
                        H must be an existing directory; an empty value is a usage error (exit 64), never
                        the real home
    --stable-dir D      stable dir, default <home>/apps/lane-tools
    --source S          directory that contains fleet_lanes/, default the checkout this runs from
    --registry F        fleet-apps.json to install, default the one beside the source
    --sha SHA           source sha to record in VERSION, default `git rev-parse HEAD` of the source
    --timeout SEC       per-probe timeout, default 5
    --no-minimal-path   probe once, not also under PATH=/usr/bin:/bin:/usr/sbin:/sbin
    --no-verify         (apply) skip the verify that normally follows
    --strict            (apply, verify) a WARN counts as a failure.  For codex it can never pass: the
                        trust record's hash is not recomputed, so `trust` is always at best a WARN
    --replace-extra     (plan, apply) tools: also delete files in an existing stable dir that this tool
                        never writes.  The dir must still be one this tool wrote (see Safety)
    --follow-symlinks   (plan, apply) platforms: write through a config symlink that resolves outside
                        --home.  Without it such a link is refused

Layout written under --home (default $HOME):

    apps/lane-tools/                  the stable dir (--stable-dir overrides)
        fleet_lanes/*.py              the package, no tests
        fleet-apps.json               the registry the shims point FLEET_APPS_JSON at
        VERSION                       sha=, package=, files=, tree= (digest of every file above)
        lane-guard-hook               executable shim; takes --format FMT, reads the hook JSON on stdin
    apps/lane                         executable shim for `python3 -m fleet_lanes.lane`; written only when
                                      the package has lane.py, so it can never be a dead command

Both shims run `/usr/bin/env python3 -I -c ...`.  `-I` drops the working directory and every PYTHON*
variable, so a lane that has its own half-edited fleet_lanes in the cwd (scripts/ for example) can
never shadow the installed copy.  No `-B`: Python writes __pycache__ into the stable dir on first use,
which keeps the per-Bash-call hook fast, and the tree digest ignores it.  The hook shim finds its
own directory from $0, so the staged copy can be proven before it is swapped in.

Minimum Python is 3.9, the `/usr/bin/python3` of macOS, which GUI-launched agent apps resolve under
a minimal PATH.  This module and the whole hook import path (layout, guard, lane_guard_hook) are 3.9
safe: no match, no `X | Y` outside annotations, no tomllib, no dataclass(slots=).

Safety:
  * `plan` is read-only.  It prints only OUR entry of a hooks list (a diff of it) and the number of
    other hooks, which are never shown, so a command line with a credential cannot reach the screen.
    The rest of a config file (settings.json has an `env` block) is never printed either.
  * The stable dir is replaced as a whole, so it is owned only if it is empty, or if its VERSION has the
    `sha=` and a 64-hex `tree=` line, its lane-guard-hook carries this tool's generated mark, and every
    entry in it is a file this tool writes (VERSION, fleet-apps.json, lane-guard-hook,
    fleet_lanes/*.py).  Anything else is a foreign dir and is refused, whatever else it contains: a project
    with its own VERSION file is not a stable dir.  `--replace-extra` waives only the last rule.  A dir that
    holds `.git`, the source, the registry or the home is refused with or without it.  `plan` prints
    `WILL DELETE n files` for what the replacement removes.
  * A config file is touched only when it parses as a JSON object and a rewrite preserves it.  Duplicate
    keys, NaN and Infinity, and numbers that would change value (1e999, 1e-400, more digits than a double)
    are refused, never reformatted.  `verify` reads duplicates the way the platform does (the last wins) and
    fails only on the numbers the platform's own parser rejects.  A read-only config (mode without the owner
    write bit) is refused unless the hook is already in it.
  * A symlinked config is written through when it resolves inside --home.  One that resolves outside is
    refused unless `--follow-symlinks`, and `plan` prints `symlink -> <real path>`.  A dangling link, in
    the file or any directory above it, is always refused: writing would create its target's directories.
  * Backup `<file>.bak-lane-guard-<stamp>` (original bytes and mode) first, then an atomic replace from a
    temp file in the same directory.  The original is re-read just before the replace and the write
    aborts if it changed (Claude Code rewrites settings.json itself).  Nothing changed, nothing written:
    a second apply makes no new backup.
  * Merges only append.  Codex records trust by position (hooks.json:pre_tool_use:<group>:<hook>),
    so existing hooks keep their trust and the new hook is the last group.  An entry that is `updated`
    changes the hook, so its old trust record is stale; apply says so.
  * `apply PLATFORM` refuses unless the installed shim passes the probes for that platform's own format
    right now, in every run (the command line never skips this, also under `apply all`).  `apply tools`
    stages the new copy, probes the staged copy in every supported format, and swaps it in only if all pass.
  * `verify` is not just the probes.  The probes run the hook command directly, so they cannot see that
    the platform will not run it.  It also FAILs on `disableAllHooks: true` and on the guard's kill switch
    (FLEET_LANE_GUARD, off values from guard.py) in the config's env block, WARNs when the caller's own
    environment has the switch, and for Codex FAILs on `[features] hooks` that is not true.
    UNVERIFIED: that Claude passes the settings env block to hook processes.
  * Nothing here creates a .janitor-keep marker.

Exit codes: 0 success, 1 a FAIL or a refusal, 64 usage error (matches the doctor).  A target named on
the command line that is not PASS (muse, a platform that is not installed) exits 1; the same target
reached through `all` is reported and skipped.

Tests: fleet_lanes/tests/test_install_tools.py.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Sequence

__all__ = [
    "EXIT_OK", "EXIT_FAIL", "EXIT_USAGE", "HOOK_NAME", "PLATFORMS", "SUPPORTED_KEYS",
    "DENY_COMMAND", "ALLOW_COMMAND", "Paths", "Source", "Platform", "Check", "Refused",
    "make_paths", "resolve_source", "render_hook_shim", "render_lane_shim", "hook_command",
    "build_files", "plan_tools", "apply_tools", "verify_tools", "plan_platform", "apply_platform",
    "verify_platform", "probe_payload", "check_deny_output", "guard_switch", "self_test", "main",
]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64

PACKAGE = "fleet_lanes"
HOOK_NAME = "lane-guard-hook"
LANE_NAME = "lane"
STABLE_DIRNAME = "lane-tools"
REGISTRY_NAME = "fleet-apps.json"
VERSION_NAME = "VERSION"
GENERATED_MARK = "Generated by fleet_lanes.install_tools"
REQUIRED_MODULES = ("__init__.py", "layout.py", "guard.py", "lane_guard_hook.py")
LANE_MODULE = "lane.py"
AG_GROUP = "lane-guard"
GROK_FILE = "lane-guard.json"

HOOK_TIMEOUT_S = 5            # timeout written into each platform config (seconds)
PROBE_TIMEOUT_S = 5.0         # per probe subprocess
MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"   # what a GUI-launched app typically has
BACKUP_TAG = "bak-lane-guard"

# The probe commands.  They are the fixture rows `spec-gh-hoghunter` (deny) and `spec-third-party`
# (allow) of tests/fixtures_guard.py, copied here because the stable dir has no tests.  The test
# module asserts they still equal those rows.  The allow command gets past the guard's trigger
# regex, so the evaluator really runs and decides.
DENY_COMMAND = "gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify"
DENY_MARKER = "hh-verify"
ALLOW_COMMAND = "git clone https://github.com/someone/else.git /tmp/else"

# Environment variables that would mask a broken install if they leaked into a probe.
_SCRUB_ENV = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH", "PYTHONUSERBASE", "PYTHONUTF8",
    "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV", "FLEET_APPS_JSON", "FLEET_LAYOUT", "FLEET_LANES_ROOT",
    "FLEET_LANE_GUARD", "AGENT_SEAT",
)

# The guard's own kill switch (guard.py: Owner-only note).  Imported from guard.py when that works, so the
# two cannot drift; these literals are the fallback for a guard.py that cannot be imported, which is exactly
# when `verify` must still run.  The tests assert they equal guard.py's.
_FALLBACK_ENV_GUARD = "FLEET_LANE_GUARD"
_FALLBACK_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})


def guard_switch() -> tuple[str, frozenset]:
    """(name of the guard's kill-switch environment variable, the values that mean off)."""
    try:
        from . import guard
        return guard.ENV_GUARD, frozenset(guard._OFF_VALUES)
    except Exception:
        return _FALLBACK_ENV_GUARD, _FALLBACK_OFF_VALUES


def _is_off(value: object) -> bool:
    """True when `value` (a string, or a JSON boolean or number) switches the guard off."""
    return str(value).strip().lower() in guard_switch()[1]


_OURS_RE = re.compile(r"(?:^|[\s/'\"=])" + re.escape(HOOK_NAME) + r"(?=$|[\s'\"])")


class Refused(Exception):
    """A safe refusal.  Nothing was written."""


class ShapeError(Refused):
    """A config file is JSON but not shaped the way the platform's hook schema is."""


# --------------------------------------------------------------------------- paths and source

@dataclass(frozen=True)
class Paths:
    home: str
    stable: str

    @property
    def apps_dir(self) -> str:
        return os.path.join(self.home, "apps")

    @property
    def lane_shim(self) -> str:
        return os.path.join(self.apps_dir, LANE_NAME)

    @property
    def hook_shim(self) -> str:
        return os.path.join(self.stable, HOOK_NAME)

    @property
    def package_dir(self) -> str:
        return os.path.join(self.stable, PACKAGE)

    @property
    def registry(self) -> str:
        return os.path.join(self.stable, REGISTRY_NAME)

    @property
    def version(self) -> str:
        return os.path.join(self.stable, VERSION_NAME)


def make_paths(home: str, stable_dir: str | None = None) -> Paths:
    """`home` is required: only the CLI supplies a default, so a test cannot reach the real home by
    leaving the argument out."""
    if not home:
        raise ValueError("home is required")
    h = os.path.abspath(home)
    s = os.path.abspath(stable_dir) if stable_dir else os.path.join(h, "apps", STABLE_DIRNAME)
    return Paths(home=h, stable=s)


@dataclass(frozen=True)
class Source:
    scripts_dir: str     # the directory that CONTAINS fleet_lanes/
    registry: str        # fleet-apps.json to copy
    sha: str             # git sha of the source checkout, or "unknown"


_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def git_head(path: str, timeout: float = 10.0) -> str:
    """HEAD of the checkout that holds `path`, read-only; "unknown" when git cannot say."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        p = subprocess.run(["git", "--no-optional-locks", "-C", path, "rev-parse", "HEAD"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, timeout=timeout)
        sha = p.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return sha if p.returncode == 0 and _SHA_RE.match(sha) else "unknown"


def resolve_source(scripts_dir: str | None = None, registry: str | None = None, sha: str | None = None,
                   sha_fn: Callable[[str], str] = git_head) -> Source:
    """The tree to install from.  The defaults are the checkout this module runs from; `sha` is
    injectable (--sha, tests)."""
    sd = os.path.abspath(scripts_dir) if scripts_dir else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if registry:
        reg = os.path.abspath(registry)
    else:
        up = os.path.join(os.path.dirname(sd), REGISTRY_NAME)
        reg = up if os.path.isfile(up) else os.path.join(sd, REGISTRY_NAME)
    return Source(scripts_dir=sd, registry=reg, sha=sha if sha else sha_fn(sd))


# --------------------------------------------------------------------------- shims

_PY_BOOT = ('import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); '
            'runpy.run_module("{module}", run_name="__main__", alter_sys=True)')


def render_hook_shim() -> str:
    """lane-guard-hook.  Relocatable: it derives its directory from $0, so a staged copy runs itself."""
    boot = _PY_BOOT.format(module=f"{PACKAGE}.lane_guard_hook")
    return (
        "#!/bin/sh\n"
        f"# {GENERATED_MARK}.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.\n"
        "# Runs the temp-checkout guard from THIS directory's copy of fleet_lanes, never from the caller's\n"
        "# working directory (-I).  The guard allows on any internal error, so `install_tools verify`\n"
        "# is the proof that it denies.  Arguments (--format FMT) pass through.\n"
        'case "$0" in\n'
        '  */*) D=${0%/*} ;;\n'
        '  *) D=. ;;\n'
        'esac\n'
        'D=$(cd "$D" && pwd -P) || exit 70\n'
        'PYTHONPATH=$D\n'
        'FLEET_APPS_JSON=$D/fleet-apps.json\n'
        'export PYTHONPATH FLEET_APPS_JSON\n'
        f"exec /usr/bin/env python3 -I -c '{boot}' \"$D\" \"$@\"\n"
    )


def render_lane_shim(stable: str) -> str:
    boot = _PY_BOOT.format(module=f"{PACKAGE}.{LANE_NAME}")
    return (
        "#!/bin/sh\n"
        f"# {GENERATED_MARK}.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.\n"
        "# Same as `python3 -m fleet_lanes.lane`, but from the stable copy and not from the working directory.\n"
        f"D={shlex.quote(stable)}\n"
        'PYTHONPATH=$D\n'
        'FLEET_APPS_JSON=$D/fleet-apps.json\n'
        'export PYTHONPATH FLEET_APPS_JSON\n'
        f"exec /usr/bin/env python3 -I -c '{boot}' \"$D\" \"$@\"\n"
    )


def hook_command(paths: Paths, fmt: str) -> str:
    """The exact string written into a platform config."""
    return f"{shlex.quote(paths.hook_shim)} --format {fmt}"


def is_ours(command: object) -> bool:
    return isinstance(command, str) and bool(_OURS_RE.search(command))


# --------------------------------------------------------------------------- the stable dir

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_digest(files: Mapping[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8") + b"\0" + _sha256(files[rel]).encode("ascii") + b"\n")
    return h.hexdigest()


def _package_version(init_text: str) -> str:
    m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", init_text, re.M)
    return m.group(1) if m else "unknown"


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def build_files(src: Source, paths: Paths) -> dict[str, bytes]:
    """Relative path (under the stable dir) to bytes, VERSION included.  Raises Refused when the
    source is not a usable package."""
    pkg = os.path.join(src.scripts_dir, PACKAGE)
    if not os.path.isdir(pkg):
        raise Refused(f"source has no {PACKAGE}/ directory: {src.scripts_dir}")
    names = sorted(n for n in os.listdir(pkg)
                   if n.endswith(".py") and os.path.isfile(os.path.join(pkg, n)))
    missing = [m for m in REQUIRED_MODULES if m not in names]
    if missing:
        raise Refused(f"source {PACKAGE}/ lacks required modules: {', '.join(missing)}")
    try:
        reg_bytes = _read_bytes(src.registry)
        reg = json.loads(reg_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"registry {src.registry} is unreadable: {exc}")
    if not isinstance(reg, dict) or not isinstance(reg.get("apps"), list) or not isinstance(reg.get("seats"), list):
        raise Refused(f"registry {src.registry} needs top-level 'apps' and 'seats' lists")
    files: dict[str, bytes] = {f"{PACKAGE}/{n}": _read_bytes(os.path.join(pkg, n)) for n in names}
    files[REGISTRY_NAME] = reg_bytes
    files[HOOK_NAME] = render_hook_shim().encode("utf-8")
    init_text = files[f"{PACKAGE}/__init__.py"].decode("utf-8", "replace")
    version = (f"sha={src.sha}\npackage={_package_version(init_text)}\nfiles={len(files)}\n"
               f"tree={tree_digest(files)}\n")
    files[VERSION_NAME] = version.encode("utf-8")
    return files


def file_mode(rel: str) -> int:
    return 0o755 if rel == HOOK_NAME else 0o644


def read_tree(root: str) -> dict[str, bytes]:
    """Every file under `root` except __pycache__, as relative path to bytes."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if os.path.islink(full) or fn == ".DS_Store":
                continue
            out[os.path.relpath(full, root).replace(os.sep, "/")] = _read_bytes(full)
    return out


_TREE_RE = re.compile(r"^[0-9a-f]{64}$")
_PKG_FILE_RE = re.compile(re.escape(PACKAGE) + r"/[^/]+\.py")
_TOP_FILES = (VERSION_NAME, REGISTRY_NAME, HOOK_NAME)


def _expected_shape(rel: str) -> bool:
    """A path this tool could have written into a stable dir (in this or an earlier version)."""
    return rel in _TOP_FILES or _PKG_FILE_RE.fullmatch(rel) is not None


def scan_stable(root: str) -> tuple[dict[str, bytes], list[str]]:
    """(the files of the stable dir that this tool writes, as relative path to bytes; every other entry as a
    relative path).  Unlike `read_tree` this sees EVERYTHING that an atomic replace would delete: symlinks,
    other directories (their files, or `dir/` when empty), special files.  Only files of a shape this tool
    writes are read, so a foreign file is never opened.  `fleet_lanes/__pycache__` and `.DS_Store` are
    not reported."""
    files: dict[str, bytes] = {}
    extras: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        reld = os.path.relpath(dirpath, root).replace(os.sep, "/")
        reld = "" if reld == "." else reld
        walk: list[str] = []
        for d in sorted(dirnames):
            rel = f"{reld}/{d}" if reld else d
            if os.path.islink(os.path.join(dirpath, d)):
                extras.append(rel)
            elif d == "__pycache__" and reld == PACKAGE:
                continue
            else:
                walk.append(d)
        dirnames[:] = walk
        if reld not in ("", PACKAGE) and not walk and not [f for f in filenames if f != ".DS_Store"]:
            extras.append(reld + "/")
        for fn in sorted(filenames):
            if fn == ".DS_Store":
                continue
            rel = f"{reld}/{fn}" if reld else fn
            full = os.path.join(dirpath, fn)
            try:
                regular = stat.S_ISREG(os.lstat(full).st_mode)
            except OSError:
                regular = False
            if regular and _expected_shape(rel):
                files[rel] = _read_bytes(full)
            else:
                extras.append(rel)
    return files, sorted(extras)


def not_ours(existing: Mapping[str, bytes]) -> str | None:
    """Why a non-empty stable dir cannot be one this tool wrote, or None when it can.  A VERSION file alone
    proves nothing: many projects have one.  It must carry the sha= and tree= lines this tool writes, and the
    hook shim next to it must carry this tool's generated mark."""
    raw = existing.get(VERSION_NAME)
    if raw is None:
        return f"it has no {VERSION_NAME}"
    ver = parse_version(raw.decode("utf-8", "replace"))
    if not ver.get("sha"):
        return f"its {VERSION_NAME} has no sha= line"
    if not _TREE_RE.match(ver.get("tree", "")):
        return f"its {VERSION_NAME} has no tree= digest"
    shim = existing.get(HOOK_NAME)
    if shim is None:
        return f"it has no {HOOK_NAME}"
    if GENERATED_MARK.encode() not in shim[:400]:
        return f"its {HOOK_NAME} was not generated by this tool"
    return None


def _is_within(child: str, root: str) -> bool:
    """`child` is `root` or lies below it (both already resolved)."""
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def parse_version(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _write_tree(root: str, files: Mapping[str, bytes]) -> None:
    for rel, data in files.items():
        full = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(full, file_mode(rel))
    for dirpath, _dirs, _files in os.walk(root):
        os.chmod(dirpath, 0o755)


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes, mode: int) -> None:
    """Temp file in the same directory, fsync, chmod, os.replace.  The temp file is removed on any failure."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".lane-guard-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def describe_foreign(path: str) -> str:
    if os.path.islink(path):
        return f"a symlink to {os.readlink(path)}"
    return "a directory" if os.path.isdir(path) else "a file this tool did not write"


def _is_generated(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return GENERATED_MARK.encode() in fh.read(400)
    except OSError:
        return False


@dataclass
class FileStatus:
    rel: str
    status: str          # new, changed, same, stale


@dataclass
class ToolsPlan:
    stable: str
    state: str                      # missing, current, stale, foreign
    files: list[FileStatus]
    version_text: str
    hook_shim_text: str
    lane_shim_text: str | None      # None when the source has no lane.py
    lane_shim_state: str            # new, changed, same, skipped, stale, foreign
    sha: str
    problems: list[str] = field(default_factory=list)
    lane_detail: str = ""           # for a foreign lane shim: what is at the path instead


def _stable_location_problems(src: Source, paths: Paths) -> list[str]:
    """The stable dir is replaced wholesale, so it must not hold anything the run still needs."""
    stable = os.path.realpath(paths.stable)
    out: list[str] = []
    for label, p in (("source", src.scripts_dir), ("registry", src.registry), ("home", paths.home)):
        if _is_within(os.path.realpath(p), stable):
            out.append(f"{paths.stable} would hold the {label} ({p}); replacing it would delete the {label}")
    return out


def plan_tools(src: Source, paths: Paths, *, replace_extra: bool = False) -> ToolsPlan:
    """Read-only: what `apply tools` would do.  A stable dir that is not empty must be one this tool wrote
    (`not_ours`) and may hold only files of a shape this tool writes, unless `replace_extra`, which waives
    only that second rule: an unrelated directory is never replaced."""
    files = build_files(src, paths)
    problems: list[str] = []
    state = "missing"
    statuses: list[FileStatus] = []
    existing: dict[str, bytes] = {}
    extras: list[str] = []
    if os.path.islink(paths.stable):
        problems.append(f"{paths.stable} is a symlink; the stable dir must be a real directory")
        state = "foreign"
    elif os.path.isdir(paths.stable):
        state = "current"
        problems.extend(_stable_location_problems(src, paths))
        if os.path.lexists(os.path.join(paths.stable, ".git")):
            problems.append(f"{paths.stable} contains a .git entry: it is a checkout, never a lane-tools dir")
        existing, extras = scan_stable(paths.stable)
        if existing or extras:
            why = not_ours(existing)
            if why:
                problems.append(f"{paths.stable} is not empty and is not a lane-tools dir this tool wrote ({why})")
            elif extras and not replace_extra:
                shown = ", ".join(extras[:3]) + (f" and {len(extras) - 3} more" if len(extras) > 3 else "")
                problems.append(f"{paths.stable} holds {len(extras)} entr{'y' if len(extras) == 1 else 'ies'} "
                                f"this tool never writes ({shown}); pass --replace-extra to have them deleted, "
                                "or choose another --stable-dir")
        if problems:
            state = "foreign"
    elif os.path.lexists(paths.stable):
        problems.append(f"{paths.stable} exists and is not a directory")
        state = "foreign"
    for rel in sorted(files):
        if rel not in existing:
            statuses.append(FileStatus(rel, "new"))
        elif existing[rel] != files[rel]:
            statuses.append(FileStatus(rel, "changed"))
        else:
            statuses.append(FileStatus(rel, "same"))
    for rel in sorted((set(existing) - set(files)) | set(extras)):
        statuses.append(FileStatus(rel, "stale"))
    if state == "current" and any(s.status != "same" for s in statuses):
        state = "stale"
    lane_text: str | None = None
    lane_state = "skipped"
    lane_detail = ""
    if f"{PACKAGE}/{LANE_MODULE}" in files:
        lane_text = render_lane_shim(paths.stable)
        if not os.path.lexists(paths.lane_shim):
            lane_state = "new"
        elif os.path.isfile(paths.lane_shim) and not os.path.islink(paths.lane_shim) and _is_generated(paths.lane_shim):
            same = _read_bytes(paths.lane_shim) == lane_text.encode("utf-8")
            lane_state = "same" if same and os.access(paths.lane_shim, os.X_OK) else "changed"
        else:
            # Another owner (for example a symlink to a checkout's bin/lane).  Left alone: it must not
            # stop the guard from installing.
            lane_state = "foreign"
            lane_detail = describe_foreign(paths.lane_shim)
    elif os.path.lexists(paths.lane_shim) and _is_generated(paths.lane_shim):
        lane_state = "stale"
    return ToolsPlan(stable=paths.stable, state=state, files=statuses,
                     version_text=files[VERSION_NAME].decode("utf-8"),
                     hook_shim_text=render_hook_shim(), lane_shim_text=lane_text,
                     lane_shim_state=lane_state, sha=src.sha, problems=problems, lane_detail=lane_detail)


# --------------------------------------------------------------------------- running probes

@dataclass
class RunResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    error: str = ""


def run_shell(command: str, stdin: bytes, env: Mapping[str, str], timeout: float) -> RunResult:
    """`/bin/sh -c command` with `stdin`, the way a platform runs a hook command.  The process group
    is killed on a timeout.  The working directory is inherited on purpose: the shims must not care."""
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=dict(env), start_new_session=True)
    except OSError as exc:
        return RunResult(None, b"", b"", error=f"cannot start /bin/sh: {exc}")
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        return RunResult(None, out or b"", err or b"", timed_out=True)
    return RunResult(proc.returncode, out, err)


def probe_env(home: str, path: str | None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for k in _SCRUB_ENV:
        env.pop(k, None)
    env["HOME"] = home
    if path is not None:
        env["PATH"] = path
    return env


def probe_payload(shape: str, command: str, cwd: str) -> dict:
    """One hook payload per platform payload shape (the shapes guard._extract reads)."""
    if shape == "antigravity":      # UNVERIFIED: run_command with tool_input.CommandLine and Cwd
        return {"hook_event_name": "PreToolUse", "tool_name": "run_command",
                "tool_input": {"CommandLine": command, "Cwd": cwd}}
    if shape == "cursor":           # beforeShellExecution: command, cwd and workspace_roots at the top level
        return {"hook_event_name": "beforeShellExecution", "command": command, "cwd": cwd,
                "workspace_roots": [cwd]}
    return {"session_id": "lane-verify", "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command, "description": "lane-guard verify"}, "cwd": cwd}


def check_deny_output(fmt: str, out: bytes) -> str | None:
    """None when `out` is the platform's deny for the DENY_COMMAND probe, else the reason it is not."""
    if not out.strip():
        return "no output (the guard allowed it, or crashed and failed open)"
    try:
        body = json.loads(out.decode("utf-8"))
    except ValueError:
        return "stdout is not JSON"
    if not isinstance(body, dict):
        return "stdout JSON is not an object"
    if fmt in ("claude", "codex", "muse"):
        hso = body.get("hookSpecificOutput")
        if set(body) != {"hookSpecificOutput"} or not isinstance(hso, dict):
            return "expected exactly {hookSpecificOutput: {...}}"
        if hso.get("hookEventName") != "PreToolUse" or hso.get("permissionDecision") != "deny":
            return "hookSpecificOutput is not a PreToolUse deny"
        reason = hso.get("permissionDecisionReason")
    elif fmt in ("grok", "antigravity"):
        if set(body) != {"decision", "reason"} or body.get("decision") != "deny":
            return 'expected exactly {"decision": "deny", "reason": ...}'
        reason = body.get("reason")
    elif fmt == "cursor":
        if body.get("permission") != "deny" or not isinstance(body.get("user_message"), str):
            return 'expected {"permission": "deny", "user_message": ..., "agent_message": ...}'
        reason = body.get("agent_message")
    else:
        return f"unknown format {fmt!r}"
    if not isinstance(reason, str) or DENY_MARKER not in reason:
        return "deny reason does not name the probe destination"
    return None


def _brief(data: bytes, limit: int = 300) -> str:
    text = repr(data[:limit])
    return text + (f"...(+{len(data) - limit} bytes)" if len(data) > limit else "")


@dataclass
class Check:
    target: str
    name: str
    status: str            # PASS, FAIL, WARN, SKIP
    detail: str = ""

    def line(self) -> str:
        return f"{self.status:<5} {self.target:<12} {self.name:<24} {self.detail}".rstrip()


def _variants(minimal_path: bool) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = [("login-PATH", None)]
    if minimal_path:
        out.append(("minimal-PATH", MINIMAL_PATH))
    return out


def probe_command(command: str, *, target: str, fmt: str, shape: str, home: str, timeout: float,
                  minimal_path: bool = True) -> list[Check]:
    """Run `command` (a hook command string) against the deny and the allow payload, once per PATH."""
    checks: list[Check] = []
    for vname, vpath in _variants(minimal_path):
        env = probe_env(home, vpath)
        for kind, cmd in (("deny", DENY_COMMAND), ("allow", ALLOW_COMMAND)):
            res = run_shell(command, json.dumps(probe_payload(shape, cmd, home)).encode("utf-8"), env, timeout)
            problem: str | None = None
            if res.error:
                problem = res.error
            elif res.timed_out:
                problem = f"timed out after {timeout:g}s"
            elif res.returncode != 0:
                problem = f"exit {res.returncode}, expected 0"
            elif kind == "deny":
                problem = check_deny_output(fmt, res.stdout)
            elif res.stdout:
                problem = "allow probe produced output"
            observed = f"exit={res.returncode} stdout={_brief(res.stdout)}"
            if res.stderr:
                observed += f" stderr={_brief(res.stderr, 200)}"
            if problem:
                checks.append(Check(target, f"{kind}/{vname}", "FAIL", f"{problem}; {observed}"))
            else:
                checks.append(Check(target, f"{kind}/{vname}", "PASS", observed))
    return checks


# --------------------------------------------------------------------------- platforms and config shapes

@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    fmt: str                          # the --format value
    shape: str                        # payload shape the probes use
    style: str                        # claude (nested groups), cursor (flat list), antigravity (named groups)
    event: str                        # the config event that gates a tool call
    matcher: str | None               # tool name the entry must match
    platform_dir: str                 # must exist, relative to home
    config_rel: str                   # config file, relative to home
    supported: bool = True
    new_mode: int = 0o644
    hook_extra: tuple = ()            # extra keys of the hook object, as (key, value) pairs
    notes: tuple = ()
    unverified: tuple = ()


PLATFORMS: dict[str, Platform] = {p.key: p for p in (
    Platform(
        "claude", "Claude Code (CLI and desktop app)", "claude", "claude", "claude", "PreToolUse", "Bash",
        ".claude", ".claude/settings.json", new_mode=0o600,
        hook_extra=(("timeout", HOOK_TIMEOUT_S), ("statusMessage", "Checking checkout location")),
        notes=("New last group under hooks.PreToolUse, matcher Bash.  Existing groups, order and formatting stay.",
               "Keeps the file's mode (settings.json is 0600 and its env block can hold secrets).",
               "Start a new session after applying: a running session may keep the hooks it started with."),
        unverified=("Whether a running session reloads an edited settings.json.",),
    ),
    Platform(
        "codex", "Codex (CLI and desktop app)", "codex", "claude", "claude", "PreToolUse", "Bash",
        ".codex", ".codex/hooks.json",
        hook_extra=(("timeout", HOOK_TIMEOUT_S), ("statusMessage", "Checking checkout location")),
        notes=("Same output format as Claude.  The entry is appended as the LAST group, so the trust Codex "
               "recorded for the existing hooks (config.toml hooks.state, keyed by position) stays valid.",
               "Codex re-trusts hooks by hash after an edit.  Review and trust the new hook with /hooks in "
               "Codex.  This tool never edits config.toml; verify reports whether a trust record exists and "
               "whether [features] hooks is on."),
        unverified=("That a hook with no trust record is inactive.",
                    "How Codex computes trusted_hash.  The record is found by its header only, so it is reported as "
                    "`hash UNCHECKED` (a WARN, never a PASS) and --strict always fails for codex.",
                    "What Codex does when [features] hooks is absent from config.toml (verify fails only when the key "
                    "is present and not true)."),
    ),
    Platform(
        "grok", "Grok and Grok Build", "grok", "claude", "claude", "PreToolUse", "Bash",
        ".grok", ".grok/hooks/" + GROK_FILE,
        hook_extra=(("timeout", HOOK_TIMEOUT_S),),
        notes=("Its own file, hooks/lane-guard.json.  imported-from-claude.json looks generated and is left alone.",
               "Output is {decision, reason}; do not rely on Grok reading the Claude settings file."),
        unverified=("Grok also loads ~/.claude/settings.json hooks when compat.claude.hooks is on, so it may "
                    "run both entries (harmless: a deny from either wins).",
                    "That a hooks/*.json file written by this tool is picked up without a Grok restart."),
    ),
    Platform(
        "antigravity", "Antigravity (agy CLI)", "antigravity", "antigravity", "antigravity", "PreToolUse",
        "run_command", ".gemini", ".gemini/config/hooks.json",
        hook_extra=(("timeout", HOOK_TIMEOUT_S), ("type", "command")),
        notes=("Own named group 'lane-guard' in ~/.gemini/config/hooks.json; the other groups are untouched.",),
        unverified=("Where the matcher key goes in an Antigravity PreToolUse entry (placed flat in the entry; "
                    "the hook allows every non-shell tool, so a misplaced matcher is harmless).",
                    "That the IDE builds fire PreToolUse at all (CLI documented, IDE reported not to).",
                    "The payload field names run_command sends (the guard reads CommandLine and Cwd)."),
    ),
    Platform(
        "cursor", "Cursor", "cursor", "cursor", "cursor", "beforeShellExecution", None,
        ".cursor", ".cursor/hooks.json",
        hook_extra=(("timeout", HOOK_TIMEOUT_S), ("failClosed", False)),
        notes=("beforeShellExecution with failClosed false: a broken hook must not block ALL shell use.  The "
               "cost is that a broken install fails open, so only `verify` proves it works.",
               "Worktrees Cursor creates itself never go through a shell hook."),
        unverified=("That Cursor re-reads hooks.json without a restart.",),
    ),
    Platform(
        "muse", "Muse Code", "muse", "claude", "claude", "PreToolUse", "Bash",
        ".config/muse", ".config/muse/settings.json", supported=False,
        notes=("Listed only.  Nothing is written.",),
        unverified=("Muse documents a hooks key in settings.json but not its deny output or exit contract, "
                    "and its settings file has no hooks key today.  Unsupported until someone verifies it.",),
    ),
)}
# What the owner still has to do after a platform was written.  Printed by `apply`.
NEXT_STEPS: dict[str, str] = {
    "codex": "review and trust the new hook with /hooks in Codex; until then it is probably inactive",
    "claude": "start a new Claude Code session (a running one may keep the hooks it started with)",
}
PLATFORM_ORDER: tuple[str, ...] = tuple(PLATFORMS)
SUPPORTED_KEYS: tuple[str, ...] = tuple(k for k in PLATFORM_ORDER if PLATFORMS[k].supported)


def config_path(paths: Paths, plat: Platform) -> str:
    return os.path.join(paths.home, *plat.config_rel.split("/"))


def _matcher_gates(matcher: object, tool: str | None) -> bool:
    if tool is None:
        return True
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


@dataclass
class Entry:
    command: str
    where: str
    problems: list[str]
    holder: dict            # the dict that holds "command"
    group: dict | None      # the dict that holds "matcher" (None for cursor)
    group_index: int | None = None
    hook_index: int | None = None


def count_ours(node: object) -> int:
    """Every `command` string in the document that is ours, anywhere (to spot an entry in the wrong place)."""
    if isinstance(node, dict):
        return sum((1 if k == "command" and is_ours(v) else count_ours(v)) for k, v in node.items())
    if isinstance(node, list):
        return sum(count_ours(v) for v in node)
    return 0


def _locate_claude(doc: dict, plat: Platform) -> list[Entry]:
    hooks = doc.get("hooks")
    if hooks is None:
        return []
    if not isinstance(hooks, dict):
        raise ShapeError("'hooks' is not an object")
    groups = hooks.get(plat.event)
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise ShapeError(f"'hooks.{plat.event}' is not a list")
    out: list[Entry] = []
    for gi, group in enumerate(groups):
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list):
            continue
        for hi, hook in enumerate(inner):
            if isinstance(hook, dict) and is_ours(hook.get("command")):
                problems: list[str] = []
                if not _matcher_gates(group.get("matcher"), plat.matcher):
                    problems.append(f"matcher {group.get('matcher')!r} never matches {plat.matcher}")
                if hook.get("type", "command") != "command":
                    problems.append(f"hook type is {hook.get('type')!r}, not 'command'")
                out.append(Entry(hook["command"], f"hooks.{plat.event}[{gi}].hooks[{hi}]", problems, hook, group, gi, hi))
    return out


def _locate_cursor(doc: dict, plat: Platform) -> list[Entry]:
    hooks = doc.get("hooks")
    if hooks is None:
        return []
    if not isinstance(hooks, dict):
        raise ShapeError("'hooks' is not an object")
    lst = hooks.get(plat.event)
    if lst is None:
        return []
    if not isinstance(lst, list):
        raise ShapeError(f"'hooks.{plat.event}' is not a list")
    return [Entry(h["command"], f"hooks.{plat.event}[{i}]", [], h, None, None, i)
            for i, h in enumerate(lst) if isinstance(h, dict) and is_ours(h.get("command"))]


def _locate_antigravity(doc: dict, plat: Platform) -> list[Entry]:
    out: list[Entry] = []
    for name, grp in doc.items():
        if not isinstance(grp, dict):
            continue
        lst = grp.get(plat.event)
        if lst is None:
            continue
        if not isinstance(lst, list):
            raise ShapeError(f"'{name}.{plat.event}' is not a list")
        for i, h in enumerate(lst):
            if isinstance(h, dict) and is_ours(h.get("command")):
                problems: list[str] = []
                if grp.get("enabled") is False:
                    problems.append(f"group '{name}' is disabled")
                if not _matcher_gates(h.get("matcher"), plat.matcher):
                    problems.append(f"matcher {h.get('matcher')!r} never matches {plat.matcher}")
                out.append(Entry(h["command"], f"{name}.{plat.event}[{i}]", problems, h, h, name, i))  # type: ignore[arg-type]
    return out


_LOCATE: dict[str, Callable[[dict, Platform], list[Entry]]] = {
    "claude": _locate_claude, "cursor": _locate_cursor, "antigravity": _locate_antigravity,
}


def locate(doc: dict, plat: Platform) -> list[Entry]:
    """Our hook entries, found through the same event and matcher accessors the merge uses."""
    return _LOCATE[plat.style](doc, plat)


def _hook_object(plat: Platform, command: str) -> dict:
    hook: dict = {}
    if plat.style == "antigravity":
        hook["matcher"] = plat.matcher
    if plat.style == "claude":
        hook["type"] = "command"
    hook["command"] = command
    for k, v in plat.hook_extra:
        hook[k] = v
    return hook


def new_document(plat: Platform, command: str) -> dict:
    hook = _hook_object(plat, command)
    if plat.style == "claude":
        return {"hooks": {plat.event: [{"matcher": plat.matcher, "hooks": [hook]}]}}
    if plat.style == "cursor":
        return {"version": 1, "hooks": {plat.event: [hook]}}
    return {AG_GROUP: {plat.event: [hook], "enabled": True}}


def merge_document(doc: dict, plat: Platform, command: str) -> tuple[dict, str, list[str]]:
    """(new document, action, notes).  action is added, updated or unchanged.  Only appends or edits
    our own entry; raises ShapeError when the document cannot hold a hook list."""
    new = copy.deepcopy(doc)
    entries = locate(new, plat)
    notes: list[str] = []
    if entries:
        first = entries[0]
        changed = False
        if first.holder.get("command") != command:
            first.holder["command"] = command
            notes.append("hook command updated to the current path and format")
            changed = True
        if plat.style == "claude" and first.group is not None and not _matcher_gates(first.group.get("matcher"), plat.matcher):
            first.group["matcher"] = plat.matcher
            notes.append(f"group matcher corrected to {plat.matcher}")
            changed = True
        if plat.style == "antigravity":
            grp = new[first.group_index]  # type: ignore[index]
            if grp.get("enabled") is False:
                grp["enabled"] = True
                notes.append(f"group '{first.group_index}' enabled")
                changed = True
            if not _matcher_gates(first.holder.get("matcher"), plat.matcher):
                first.holder["matcher"] = plat.matcher
                notes.append(f"matcher corrected to {plat.matcher}")
                changed = True
        if len(entries) > 1:
            notes.append(f"{len(entries) - 1} duplicate {HOOK_NAME} entr{'y' if len(entries) == 2 else 'ies'} left in place")
        return new, ("updated" if changed else "unchanged"), notes
    hook = _hook_object(plat, command)
    if plat.style == "claude":
        hooks = new.setdefault("hooks", {})
        groups = hooks.get(plat.event)
        if groups is None:
            groups = hooks[plat.event] = []
        groups.append({"matcher": plat.matcher, "hooks": [hook]})
    elif plat.style == "cursor":
        if not new:
            new["version"] = 1               # Cursor's hooks.json schema version; only a fresh file needs it
        hooks = new.setdefault("hooks", {})
        lst = hooks.get(plat.event)
        if lst is None:
            lst = hooks[plat.event] = []
        lst.append(hook)
    else:
        grp = new.get(AG_GROUP)
        if grp is None:
            grp = new[AG_GROUP] = {}
        if not isinstance(grp, dict):
            raise ShapeError(f"'{AG_GROUP}' exists and is not an object")
        lst = grp.get(plat.event)
        if lst is None:
            lst = grp[plat.event] = []
        if not isinstance(lst, list):
            raise ShapeError(f"'{AG_GROUP}.{plat.event}' is not a list")
        lst.append(hook)
        grp["enabled"] = True
    return new, "added", notes


def _is_extension(old: object, new: object) -> bool:
    """True when `new` is `old` plus appended list items and new dict keys (the only change a merge 'add' may make)."""
    if isinstance(old, dict):
        return isinstance(new, dict) and all(k in new and _is_extension(v, new[k]) for k, v in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and len(new) >= len(old) and all(_is_extension(a, b) for a, b in zip(old, new))
    return old == new


def subtree(doc: dict, plat: Platform) -> tuple[str, object]:
    """The part of a config this tool edits, for diffs.  Nothing else of the file is ever printed."""
    if plat.style == "antigravity":
        picked = {k: v for k, v in doc.items() if k == AG_GROUP or (isinstance(v, dict) and count_ours(v))}
        return AG_GROUP, picked
    hooks = doc.get("hooks")
    return f"hooks.{plat.event}", (hooks.get(plat.event) if isinstance(hooks, dict) else None)


# Defense in depth.  `plan` no longer prints any line of the owner's config that is not our own entry
# (see plan_view), so these patterns only see our entry and the old command of an entry being updated.
_KEYWORDS = r"(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key|credential|authorization)"
_SECRET_PAIR = re.compile(r'(?i)("[^"]*' + _KEYWORDS + r'[^"]*"\s*:\s*)"(?:[^"\\]|\\.)*"')
_SECRET_KV = re.compile(
    r"(?i)(\b[\w-]*" + _KEYWORDS + r"s?[\w-]*)(['\"]?\s*[:=]\s*)"
    r"((?:(?:bearer|basic|digest|token)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&'\"]+))")
_SECRET_FLAG = re.compile(
    r"(?i)(?<![\w-])(--?[\w-]*(?:passw(?:or)?d|token|secret|api[_-]?key|credential)[\w-]*)(=|\s+)"
    r"(?!-)(\"[^\"]*\"|'[^']*'|\S+)")
_USER_PASS = re.compile(r"(?<![\w-])(-u|--user)(\s+|=)(?!-)(\S*:\S+)")
_SECRET_INLINE = re.compile(r"(?i)((?:token|secret|passw\w*|api[_-]?key|authorization|bearer)\W{1,4})[A-Za-z0-9_\-./+=]{8,}")
_TOKEN_SHAPES = re.compile(
    r"\b(?:(?:sk-|pk-|gh[pousr]_|xox[abprs]-)[A-Za-z0-9_\-]{10,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{8,}|pk_(?:live|test)_[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|A[CK][0-9a-f]{32}\b|AIza[0-9A-Za-z_-]{30,}|AKIA[A-Z0-9]{12,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,})")
_LONG_SECRET = re.compile(r"(?<![\w/.~+%-])[A-Za-z0-9/+]{40}(?![\w/.~+%-])")


def _mask_kv(m: "re.Match[str]") -> str:
    if "<redacted>" in m.group(3):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}<redacted>"


def _mask_flag(m: "re.Match[str]") -> str:
    return m.group(0) if "<redacted>" in m.group(3) else f"{m.group(1)}{m.group(2)}<redacted>"


def _mask_long(m: "re.Match[str]") -> str:
    """40 characters of base64 alphabet that mix digits and both cases: an AWS secret access key.  Ordinary
    paths and words fail one of the tests."""
    t = m.group(0)
    secret = re.search(r"\d", t) and re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and t.count("/") <= 3
    return "<redacted>" if secret else t


def redact(line: str) -> str:
    line = _SECRET_PAIR.sub(r'\1"<redacted>"', line)
    line = _SECRET_INLINE.sub(r"\1<redacted>", line)
    line = _SECRET_KV.sub(_mask_kv, line)
    line = _SECRET_FLAG.sub(_mask_flag, line)
    line = _USER_PASS.sub(r"\1\2<redacted>", line)
    line = _TOKEN_SHAPES.sub("<redacted>", line)
    return _LONG_SECRET.sub(_mask_long, line)


_HOOK_KEYS = ("type", "command", "timeout", "statusMessage", "matcher", "failClosed")
_OTHER_HOOK = "(other hook, not shown)"
_OTHER_GROUP = "(other hook group, not shown)"


def _ours_view(hook: dict) -> dict:
    view = {k: hook[k] for k in _HOOK_KEYS if k in hook}
    extra = [k for k in hook if k not in _HOOK_KEYS]
    if extra:
        view["(other keys)"] = f"{len(extra)} not shown"
    return view


def _is_our_hook(h: object) -> bool:
    return isinstance(h, dict) and is_ours(h.get("command"))


def _hook_list_view(lst: object) -> tuple[object, int]:
    """A list of hook objects with every one that is not ours replaced by a placeholder."""
    if not isinstance(lst, list):
        return None, 0
    out: list = []
    hidden = 0
    for h in lst:
        if _is_our_hook(h):
            out.append(_ours_view(h))
        else:
            out.append(_OTHER_HOOK)
            hidden += 1
    return out, hidden


def plan_view(doc: dict, plat: Platform) -> tuple[str, object, int]:
    """(label, the part of the config this tool edits, number of hooks hidden).  Only OUR entries appear;
    every other hook, group and key is replaced by a placeholder, so a command line that carries a
    credential can never reach the screen, and nothing needs to guess what a credential looks like."""
    label, node = subtree(doc, plat)
    hidden = 0
    if node is None:
        return label, None, 0
    if plat.style == "claude":
        view: list = []
        for g in node if isinstance(node, list) else []:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list) or not any(_is_our_hook(h) for h in inner):
                view.append(_OTHER_GROUP)
                hidden += len(inner) if isinstance(inner, list) else 1
                continue
            kept, n = _hook_list_view(inner)
            hidden += n
            gv: dict = {}
            if "matcher" in g:
                gv["matcher"] = g["matcher"]
            gv["hooks"] = kept
            view.append(gv)
        return label, view, hidden
    if plat.style == "cursor":
        kept, hidden = _hook_list_view(node)
        return label, kept, hidden
    groups: dict = {}
    for name, grp in (node.items() if isinstance(node, dict) else []):
        if not isinstance(grp, dict):
            continue
        gv = {}
        if "enabled" in grp:
            gv["enabled"] = grp["enabled"]
        lst, n = _hook_list_view(grp.get(plat.event))
        hidden += n
        if lst is not None:
            gv[plat.event] = lst
        other = [k for k in grp if k not in ("enabled", plat.event)]
        if other:
            gv["(other keys)"] = f"{len(other)} not shown"
        groups[name] = gv
    return label, groups, hidden


# --------------------------------------------------------------------------- reading and writing a config

@dataclass
class Loaded:
    path: str                 # as named
    real: str                 # symlinks resolved
    exists: bool
    raw: bytes
    doc: dict
    indent: object
    ensure_ascii: bool
    trailing_newline: bool
    exact: bool               # the original text equals our re-serialization of it
    mode: int | None
    note: str = ""
    duplicate_keys: list = field(default_factory=list)       # keys repeated in one object: a rewrite keeps only the last
    lossy_numbers: list = field(default_factory=list)        # number literals a rewrite would change
    unparseable_numbers: list = field(default_factory=list)  # NaN, Infinity, 1e999: strict JSON parsers reject the file


def _detect_indent(text: str) -> object:
    for line in text.splitlines()[1:]:
        if line[:1] == "\t":
            return "\t"
        stripped = line.lstrip(" ")
        if stripped and len(line) != len(stripped):
            return len(line) - len(stripped)
    return 2


class _Scan:
    """Hooks for json.loads that record what a plain parse would hide.  Nothing is raised: `plan` and `apply`
    refuse on the records (a rewrite would not preserve the file) and `verify` fails only on the numbers a
    platform's own parser would reject, because every JSON parser keeps the last duplicate."""

    def __init__(self) -> None:
        self.duplicates: list[str] = []
        self.lossy: list[str] = []
        self.unparseable: list[str] = []

    def pairs(self, pairs: list) -> dict:
        out: dict = {}
        for k, v in pairs:
            if k in out and k not in self.duplicates:
                self.duplicates.append(k)
            out[k] = v
        return out

    def constant(self, name: str) -> float:
        self.unparseable.append(name)
        return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(name, math.nan)

    def number(self, text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            self.unparseable.append(text)
            return 0.0
        if math.isinf(value) or math.isnan(value):
            self.unparseable.append(text)
            return value
        try:
            same = Decimal(text) == Decimal(repr(value))
        except InvalidOperation:
            same = False
        if not same:
            self.lossy.append(text)
        return value


def _first(items: Sequence[str], n: int = 3) -> str:
    shown = ", ".join(repr(i) for i in items[:n])
    return shown + (f" and {len(items) - n} more" if len(items) > n else "")


def load_config(path: str) -> Loaded:
    """Parse a config file.  Raises Refused when it is not a JSON object, or is a dangling symlink.  A
    missing file is an empty document; an empty or whitespace-only file is treated as {}.  What a rewrite
    would not preserve (duplicate keys, numbers that change value) is recorded on the result."""
    real = os.path.realpath(path)
    if os.path.islink(path) and not os.path.exists(path):
        raise Refused(f"{path} is a dangling symlink (-> {os.readlink(path)}); refusing to create files through it")
    if os.path.isdir(real):
        raise Refused(f"{path} is a directory")
    if not os.path.exists(real):
        return Loaded(path, real, False, b"", {}, 2, True, True, True, None)
    try:
        raw = _read_bytes(real)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused(f"{path} cannot be read as UTF-8 text: {exc}")
    mode = os.stat(real).st_mode & 0o7777
    if not text.strip():
        return Loaded(path, real, True, raw, {}, 2, True, True, False, mode, note="file is empty; treated as {}")
    scan = _Scan()
    try:
        doc = json.loads(text, object_pairs_hook=scan.pairs, parse_constant=scan.constant, parse_float=scan.number)
    except (ValueError, RecursionError) as exc:
        raise Refused(f"{path} is not valid JSON ({exc}); refusing to touch it")
    if not isinstance(doc, dict):
        raise Refused(f"{path} is JSON but not an object at the top level; refusing to touch it")
    flags = dict(duplicate_keys=scan.duplicates, lossy_numbers=scan.lossy, unparseable_numbers=scan.unparseable)
    indent = _detect_indent(text)
    trailing = text.endswith("\n")
    if not scan.unparseable:
        for ea in (True, False):
            if json.dumps(doc, indent=indent, ensure_ascii=ea) + ("\n" if trailing else "") == text:
                return Loaded(path, real, True, raw, doc, indent, ea, trailing, True, mode, **flags)
    ea = False
    try:                                       # a lone surrogate cannot be written as raw text: escape instead
        json.dumps(doc, indent=indent, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        ea = True
    return Loaded(path, real, True, raw, doc, indent, ea, trailing, False, mode,
                  note="original formatting does not round-trip; apply re-indents the whole file (content unchanged)",
                  **flags)


def dump_config(doc: dict, cfg: Loaded) -> str:
    return json.dumps(doc, indent=cfg.indent, ensure_ascii=cfg.ensure_ascii, allow_nan=False) \
        + ("\n" if cfg.trailing_newline else "")


@dataclass
class PlatformPlan:
    platform: Platform
    path: str
    action: str                 # add, update, unchanged, skip, refuse, unsupported
    command: str
    reason: str = ""
    diff: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    new_text: str | None = None
    cfg: Loaded | None = None
    link: str | None = None     # "symlink -> <real path>" when the config is reached through a symlink
    hidden: int = 0             # hooks of other owners under the edited event; counted, never printed
    label: str = ""


def _json_lines(obj: object) -> list[str]:
    if obj is None:
        return []
    return json.dumps(obj, indent=2, ensure_ascii=False).splitlines()


def _link_check(path: str, home: str, rel: str, follow_symlinks: bool) -> tuple[str | None, str | None, str | None]:
    """(refusal, write refusal, note) for a config that may be reached through symlinks.  A dangling link
    anywhere between the home and the file is always a refusal: writing would create its target's
    directories.  A path that resolves outside the home is a refusal only for a WRITE, and only without
    `follow_symlinks`; inside the home it is written through."""
    cur = home
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur) and not os.path.exists(cur):
            return f"{cur} is a dangling symlink (-> {os.readlink(cur)}); refusing to create files through it", None, None
    home_real = os.path.realpath(home)
    real = os.path.realpath(path)
    if real == os.path.join(home_real, *rel.split("/")):
        return None, None, None
    outside = None
    if not _is_within(real, home_real) and not follow_symlinks:
        outside = f"{path} resolves to {real}, outside {home_real}; pass --follow-symlinks to write through it"
    return None, outside, f"symlink -> {real}"


def _switch_warnings(doc: dict) -> list[str]:
    """Settings in a config that make a platform ignore every hook, or the guard turn itself off."""
    name = guard_switch()[0]
    out: list[str] = []
    if doc.get("disableAllHooks") is True:
        out.append("the file has disableAllHooks: true, so the platform runs NO hooks and the guard is inert")
    env = doc.get("env")
    if isinstance(env, dict) and name in env and _is_off(env[name]):
        out.append(f"the env block sets {name}={env[name]!r}, the guard's kill switch, so the guard is off for every command")
    return out


def plan_platform(plat: Platform, paths: Paths, *, follow_symlinks: bool = False) -> PlatformPlan:
    """Read-only: what `apply PLATFORM` would do, with a unified diff of the hooks this tool edits.  The diff
    shows only our own entry; other hooks are counted and elided."""
    path = config_path(paths, plat)
    cmd = hook_command(paths, plat.fmt)
    if not plat.supported:
        return PlatformPlan(plat, path, "unsupported", cmd, "no verified hook surface; nothing is written")
    if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
        return PlatformPlan(plat, path, "skip", cmd, f"~/{plat.platform_dir} does not exist (platform not installed)")
    refusal, outside, link = _link_check(path, paths.home, plat.config_rel, follow_symlinks)
    if refusal:
        return PlatformPlan(plat, path, "refuse", cmd, refusal)
    try:
        cfg = load_config(path)
        new_doc, action, notes = merge_document(cfg.doc, plat, cmd)
    except Refused as exc:
        return PlatformPlan(plat, path, "refuse", cmd, str(exc))
    if action != "unchanged":
        # Writing is what these checks protect, so a file that is already right is never refused for them.
        if outside:
            return PlatformPlan(plat, path, "refuse", cmd, outside, link=link)
        if cfg.unparseable_numbers:
            return PlatformPlan(plat, path, "refuse", cmd,
                                f"{path} holds numbers JSON cannot represent ({_first(cfg.unparseable_numbers)}); "
                                "strict parsers reject the file and a rewrite would change them; fix the file first")
        if cfg.duplicate_keys:
            return PlatformPlan(plat, path, "refuse", cmd,
                                f"{path} has duplicate key(s) {_first(cfg.duplicate_keys)} in one object; a rewrite "
                                "would silently keep only the last value; fix the file first")
        if cfg.lossy_numbers:
            return PlatformPlan(plat, path, "refuse", cmd,
                                f"{path} holds number(s) a rewrite would change ({_first(cfg.lossy_numbers)}); "
                                "refusing to touch it")
        if cfg.exists and cfg.mode is not None and not cfg.mode & 0o200:
            return PlatformPlan(plat, path, "refuse", cmd,
                                f"{path} is read-only (mode {cfg.mode:04o}); not overriding the owner's choice")
        parent = os.path.dirname(cfg.real)
        if os.path.isdir(parent) and not os.access(parent, os.W_OK | os.X_OK):
            return PlatformPlan(plat, path, "refuse", cmd, f"the directory {parent} is not writable")
    new_text = dump_config(new_doc, cfg)
    try:
        new_text.encode("utf-8")
    except UnicodeEncodeError:
        return PlatformPlan(plat, path, "refuse", cmd, f"the merged document for {path} cannot be encoded as UTF-8")
    if cfg.note:
        notes.append(cfg.note)
    notes.extend("WARNING: " + w for w in _switch_warnings(new_doc))
    if plat.key == "codex" and action == "updated":
        notes.append("the existing Codex trust record for this hook is stale after this change; review and "
                     "re-trust it with /hooks in Codex")
    label, before, hidden = plan_view(cfg.doc, plat)
    _label, after, _h = plan_view(new_doc, plat)
    diff: list[str] = []
    if action != "unchanged":
        diff = [redact(ln) for ln in difflib.unified_diff(
            _json_lines(before), _json_lines(after), fromfile=f"{path} {label} (current)",
            tofile=f"{path} {label} (after apply)", n=3, lineterm="")]
    act = {"added": "add", "updated": "update", "unchanged": "unchanged"}[action]
    return PlatformPlan(plat, path, act, cmd, diff=diff, notes=notes, new_text=new_text, cfg=cfg, link=link,
                        hidden=hidden, label=label)


# --------------------------------------------------------------------------- apply: tools

@dataclass
class Result:
    target: str
    status: str            # added, updated, unchanged, installed, skipped, refused, failed, unsupported
    detail: str = ""
    backup: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("added", "updated", "unchanged", "installed")

    def line(self) -> str:
        return f"{self.status.upper():<10} {self.target:<12} {self.detail}".rstrip()


def _swap_dir(stage: str, final: str) -> None:
    old: str | None = None
    if os.path.lexists(final):
        old = f"{final}.old-{os.getpid()}"
        if os.path.lexists(old):
            shutil.rmtree(old, ignore_errors=True)
        os.rename(final, old)
    try:
        os.rename(stage, final)
    except BaseException:
        if old is not None:
            try:
                os.rename(old, final)
            except OSError:
                pass
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)


def probe_formats() -> list[tuple[str, str]]:
    """(--format, payload shape) of every supported platform, once each.  The staging probes run all of
    them, so a copy whose output is broken for ANY platform is never swapped in."""
    out: list[tuple[str, str]] = []
    for plat in PLATFORMS.values():
        if plat.supported and (plat.fmt, plat.shape) not in out:
            out.append((plat.fmt, plat.shape))
    return out


def apply_tools(src: Source, paths: Paths, *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True,
                probes: bool = True, emit: Callable[[str], None] = lambda s: None,
                replace_extra: bool = False) -> Result:
    """Copy or refresh the stable dir atomically.  The new copy is staged beside the final dir and
    probed first (every supported platform's format); it is swapped in only if the probes pass.
    Between the two renames of the swap the dir is briefly absent, and a hook call that lands there
    fails open.  A non-empty dir that this tool did not write is refused (see `plan_tools`)."""
    try:
        if os.path.realpath(src.scripts_dir) == os.path.realpath(paths.stable):
            raise Refused("the source is the stable dir itself; run this from a checkout of origin/main")
        plan = plan_tools(src, paths, replace_extra=replace_extra)
        if plan.problems:
            raise Refused("; ".join(plan.problems))
        files = build_files(src, paths)
    except Refused as exc:
        return Result("tools", "refused", str(exc))
    lane_text = plan.lane_shim_text
    if plan.state == "current" and plan.lane_shim_state in ("same", "skipped", "foreign") and os.access(paths.hook_shim, os.X_OK):
        return Result("tools", "unchanged", f"{paths.stable} is current (sha {plan.sha[:12]})")
    os.makedirs(paths.apps_dir, exist_ok=True)
    os.makedirs(os.path.dirname(paths.stable), exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".lane-tools-stage-", dir=os.path.dirname(paths.stable))
    try:
        _write_tree(stage, files)
        if probes:
            staged_cmd = shlex.quote(os.path.join(stage, HOOK_NAME))
            bad: list[Check] = []
            for fmt, shape in probe_formats():
                got = probe_command(f"{staged_cmd} --format {fmt}", target="staged", fmt=fmt, shape=shape,
                                    home=paths.home, timeout=timeout, minimal_path=minimal_path)
                bad.extend(c for c in got if c.status == "FAIL")
            if bad:
                for c in bad:
                    emit(c.line())
                raise Refused("the staged copy failed its own probes; the installed copy was not touched")
        shutil.rmtree(os.path.join(stage, PACKAGE, "__pycache__"), ignore_errors=True)  # probes wrote it with the stage path
        _swap_dir(stage, paths.stable)
    except Refused as exc:
        shutil.rmtree(stage, ignore_errors=True)
        return Result("tools", "refused", str(exc))
    except BaseException as exc:
        shutil.rmtree(stage, ignore_errors=True)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return Result("tools", "failed", f"{type(exc).__name__}: {exc}")
    extra = ""
    try:
        if plan.lane_shim_state == "foreign":
            extra = f"; lane shim NOT written: {paths.lane_shim} is {plan.lane_detail}, left alone"
        elif lane_text is not None:
            atomic_write(paths.lane_shim, lane_text.encode("utf-8"), 0o755)
            extra = f"; lane shim {paths.lane_shim}"
        elif plan.lane_shim_state == "stale":
            os.unlink(paths.lane_shim)
            extra = "; removed a stale lane shim (this install has no lane.py)"
        else:
            extra = f"; no lane shim (the package has no {LANE_MODULE} yet)"
    except OSError as exc:
        return Result("tools", "failed", f"stable dir installed, but the lane shim failed: {exc}")
    return Result("tools", "installed", f"{paths.stable} sha {plan.sha[:12]}, {len(files)} files{extra}")


# --------------------------------------------------------------------------- apply: platforms

def _reread_matches(real: str, raw: bytes, existed: bool) -> bool:
    try:
        now = _read_bytes(real) if os.path.exists(real) else None
    except OSError:
        return False
    return now == raw if existed else now is None


def _make_backup(real: str, raw: bytes, mode: int | None, stamp: str) -> str:
    for n in range(1000):
        name = f"{real}.{BACKUP_TAG}-{stamp}" + (f"-{n}" if n else "")
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            if mode is not None:
                os.chmod(name, mode)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
        return name
    raise OSError("could not find a free backup name")


def apply_platform(plat: Platform, paths: Paths, *, now: Callable[[], float] = time.time,
                   timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True, shim_proven: bool = False,
                   follow_symlinks: bool = False) -> Result:
    """Merge the hook into the platform's config, but only if the installed shim passes the probes for this
    platform's own format right now.  The `apply` command never sets `shim_proven` (it skips those probes):
    `apply tools` proves a staged copy, and an `unchanged` tools result proves nothing about this run.  Only
    callers that have just proven the shim themselves (the self-test, the tests) pass it."""
    plan = plan_platform(plat, paths, follow_symlinks=follow_symlinks)
    if plan.action == "unsupported":
        return Result(plat.key, "unsupported", plan.reason)
    if plan.action == "skip":
        return Result(plat.key, "skipped", plan.reason)
    if plan.action == "refuse":
        return Result(plat.key, "refused", plan.reason)
    if not shim_proven:
        if not (os.path.isfile(paths.hook_shim) and os.access(paths.hook_shim, os.X_OK)):
            return Result(plat.key, "refused", f"the hook shim {paths.hook_shim} is missing or not executable; run `apply tools` first")
        checks = probe_command(plan.command, target=plat.key, fmt=plat.fmt, shape=plat.shape, home=paths.home,
                               timeout=timeout, minimal_path=minimal_path)
        bad = [c for c in checks if c.status == "FAIL"]
        if bad:
            return Result(plat.key, "refused",
                          "the installed hook shim fails its probes, so it will not be wired in: " + bad[0].detail)
    if plan.action == "unchanged":
        return Result(plat.key, "unchanged", f"{plan.path} already has the hook")
    cfg = plan.cfg
    assert cfg is not None and plan.new_text is not None
    # Self-checks on the document we are about to write.
    try:
        reparsed = json.loads(plan.new_text)
    except ValueError as exc:
        return Result(plat.key, "refused", f"the merged document does not serialize to valid JSON: {exc}")
    found = locate(reparsed, plat)
    if len(found) < 1 or found[0].command != plan.command or found[0].problems:
        return Result(plat.key, "refused", "the merged document does not hold the hook where the platform reads it")
    if plan.action == "add" and not _is_extension(cfg.doc, reparsed):
        return Result(plat.key, "refused", "internal check failed: the merge would change existing content")
    real = cfg.real
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    backup: str | None = None
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        if cfg.exists:
            backup = _make_backup(real, cfg.raw, cfg.mode, stamp)
        if not _reread_matches(real, cfg.raw, cfg.exists):
            if backup:
                os.unlink(backup)
            return Result(plat.key, "failed", f"{plan.path} changed while it was being merged; nothing written, retry")
        atomic_write(real, plan.new_text.encode("utf-8"), cfg.mode if cfg.mode is not None else plat.new_mode)
    except OSError as exc:
        return Result(plat.key, "failed", f"{plan.path} not changed: {type(exc).__name__}: {exc}", backup)
    # Validate what is on disk now; put the original back if it is not what we meant to write.
    try:
        after = json.loads(_read_bytes(real).decode("utf-8"))
        ok = bool(locate(after, plat)) and locate(after, plat)[0].command == plan.command
    except (OSError, ValueError):
        ok = False
    if not ok:
        try:
            if cfg.exists:
                atomic_write(real, cfg.raw, cfg.mode if cfg.mode is not None else plat.new_mode)
            else:
                os.unlink(real)
        except OSError:
            pass
        return Result(plat.key, "failed", f"{plan.path} failed validation after the write; the original was restored", backup)
    detail = f"{plan.path}" + (f" (backup {backup})" if backup else " (new file)")
    if plan.notes:
        detail += "; " + "; ".join(plan.notes)
    return Result(plat.key, "added" if plan.action == "add" else "updated", detail, backup)


# --------------------------------------------------------------------------- verify

def verify_tools(paths: Paths, *, timeout: float = PROBE_TIMEOUT_S) -> list[Check]:
    """Health of the stable dir and the shims.  The guard quietly falls back to built-in rows when the
    registry is missing, so the probes alone would not notice."""
    out: list[Check] = []
    t = "tools"
    if not os.path.isdir(paths.stable):
        return [Check(t, "stable-dir", "FAIL", f"{paths.stable} does not exist; run `apply tools`")]
    out.append(Check(t, "stable-dir", "PASS", paths.stable))
    try:
        ver = parse_version(_read_bytes(paths.version).decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        ver = {}
        out.append(Check(t, "VERSION", "FAIL", f"{paths.version}: {exc}"))
    else:
        out.append(Check(t, "VERSION", "PASS" if ver.get("tree") else "FAIL",
                         f"sha={ver.get('sha', '?')[:12]} package={ver.get('package', '?')} files={ver.get('files', '?')}"))
    try:
        have = read_tree(paths.stable)
    except OSError as exc:
        have = {}
        out.append(Check(t, "files", "FAIL", f"cannot read {paths.stable}: {exc}"))
    else:
        body = {k: v for k, v in have.items() if k != VERSION_NAME}
        gone = [m for m in REQUIRED_MODULES if f"{PACKAGE}/{m}" not in body]
        if gone:
            out.append(Check(t, "files", "FAIL", "missing required modules: " + ", ".join(gone)))
        elif ver.get("tree") and tree_digest(body) != ver["tree"]:
            out.append(Check(t, "files", "FAIL", "the files no longer match VERSION's tree digest (modified, added or removed)"))
        else:
            out.append(Check(t, "files", "PASS", f"{len(body)} files match the VERSION digest"))
    try:
        reg = json.loads(_read_bytes(paths.registry).decode("utf-8"))
        good = isinstance(reg, dict) and bool(reg.get("apps")) and bool(reg.get("seats"))
        out.append(Check(t, REGISTRY_NAME, "PASS" if good else "FAIL",
                         "parses and has apps and seats" if good else "parses but has no apps or seats"))
    except (OSError, ValueError) as exc:
        out.append(Check(t, REGISTRY_NAME, "FAIL", f"{paths.registry}: {exc}"))
    out.append(_check_shim(t, HOOK_NAME, paths.hook_shim, render_hook_shim()))
    if f"{PACKAGE}/{LANE_MODULE}" in have and os.path.lexists(paths.lane_shim) and not (
            os.path.isfile(paths.lane_shim) and not os.path.islink(paths.lane_shim) and _is_generated(paths.lane_shim)):
        out.append(Check(t, LANE_NAME, "WARN", f"{paths.lane_shim} is {describe_foreign(paths.lane_shim)}; left alone, "
                                               "so `lane` there is NOT the stable copy"))
    elif f"{PACKAGE}/{LANE_MODULE}" in have:
        c = _check_shim(t, LANE_NAME, paths.lane_shim, render_lane_shim(paths.stable))
        out.append(c)
        if c.status == "PASS":
            res = run_shell(f"{shlex.quote(paths.lane_shim)} --help", b"", probe_env(paths.home, None), timeout)
            ok = res.returncode == 0 and not res.timed_out
            out.append(Check(t, "lane --help", "PASS" if ok else "FAIL",
                             f"exit={res.returncode} stdout={_brief(res.stdout, 120)}"
                             + (f" stderr={_brief(res.stderr, 200)}" if res.stderr else "")
                             + ("; timed out" if res.timed_out else "")))
    else:
        out.append(Check(t, LANE_NAME, "WARN", f"no {LANE_MODULE} in the stable copy yet, so there is no lane shim"))
    return out


def _check_shim(target: str, name: str, path: str, expected: str) -> Check:
    if not os.path.isfile(path):
        return Check(target, f"shim {name}", "FAIL", f"{path} is missing")
    mode = os.stat(path).st_mode
    if not (mode & 0o111) or not os.access(path, os.X_OK):
        return Check(target, f"shim {name}", "FAIL", f"{path} is not executable (mode {mode & 0o777:o})")
    try:
        if _read_bytes(path) != expected.encode("utf-8"):
            return Check(target, f"shim {name}", "FAIL", f"{path} differs from what this install_tools writes; re-run `apply tools`")
    except OSError as exc:
        return Check(target, f"shim {name}", "FAIL", f"{path}: {exc}")
    return Check(target, f"shim {name}", "PASS", f"{path} executable, content current")


def _toml_value(text: str) -> str:
    """The value text of a TOML line, comment stripped.  Enough for the true/false switches read here."""
    return re.split(r"\s#", text.strip() + " ", maxsplit=1)[0].strip()


def _toml_name(text: str) -> str:
    return re.sub(r"""["'\s]""", "", text)


def toml_features_hooks(text: str) -> str | None:
    """The raw value of `hooks` in config.toml's [features] table, or None when it is not set.  Reads
    `[features]` then `hooks = V`, the dotted `features.hooks = V` and the inline `features = { hooks = V }`.
    A small line scanner, not a TOML parser (3.9 has none); a multi-line string that looks like a table
    header would fool it, which a Codex config does not contain."""
    table = ""
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head = re.match(r"^\[\[?\s*([^\]]*?)\s*\]\]?\s*(?:#.*)?$", line)
        if head:
            table = _toml_name(head.group(1))
            continue
        kv = re.match(r"""^([A-Za-z0-9_.\-"'\s]+?)\s*=\s*(.*)$""", line)
        if not kv:
            continue
        key = _toml_name(kv.group(1))
        full = f"{table}.{key}" if table else key
        if full == "features.hooks":
            found = _toml_value(kv.group(2))
        elif full == "features":
            inline = re.search(r"\bhooks\s*=\s*([^\s,}#]+)", kv.group(2))
            if inline:
                found = inline.group(1)
    return found


def _codex_checks(paths: Paths, cfg_path: str, entry: Entry) -> list[Check]:
    """Would Codex run the hook?  The feature flag in config.toml must not be off, and the hook needs a trust
    record.  The record's hash is NOT recomputed (how Codex computes it is UNVERIFIED), so a record is a
    WARN at best, and `--strict` therefore always fails for codex until the owner has trusted the hook."""
    toml = os.path.join(paths.home, ".codex", "config.toml")
    key = f"{cfg_path}:pre_tool_use:{entry.group_index}:{entry.hook_index}"
    header = f'[hooks.state."{key}"]'
    try:
        text = _read_bytes(toml).decode("utf-8", "replace")
    except OSError:
        return [Check("codex", "trust", "WARN", f"cannot read {toml}; Codex trust is UNVERIFIED (review the hook with /hooks)")]
    out: list[Check] = []
    flag = toml_features_hooks(text)
    if flag is not None and flag != "true":
        out.append(Check("codex", "features", "FAIL",
                         f"{toml} sets [features] hooks = {flag}, so Codex ignores hooks.json and the hook never runs"))
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == header:
            for body in lines[i + 1:]:
                if body.lstrip().startswith("["):
                    break
                if re.match(r'\s*trusted_hash\s*=\s*"sha256:[0-9a-f]+"', body):
                    out.append(Check("codex", "trust", "WARN",
                                     "config.toml has a hooks.state record for this hook, but its hash is UNCHECKED: "
                                     "an edited or stale hook would show the same record.  Confirm with /hooks in Codex"))
                    return out
            break
    out.append(Check("codex", "trust", "WARN",
                     f"NOT TRUSTED YET: no hooks.state record for {key}.  Codex re-trusts by hash after an edit, so review "
                     "the hook with /hooks in Codex.  Until then it is probably inactive (UNVERIFIED)"))
    return out


def verify_platform(plat: Platform, paths: Paths, *, timeout: float = PROBE_TIMEOUT_S,
                    minimal_path: bool = True) -> list[Check]:
    """Take the hook command back out of the platform's config file and run it against the deny and
    allow payloads, then check that the platform will actually run it: no disableAllHooks, no kill switch
    in the config's env block (or, as a warning, in the caller's environment), and for Codex the feature
    flag and the trust record.  Finds the entry through the same event and matcher accessors the merge uses."""
    t = plat.key
    if not plat.supported:
        return [Check(t, "platform", "FAIL", "UNSUPPORTED: no verified hook surface, so there is nothing to verify")]
    path = config_path(paths, plat)
    if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
        return [Check(t, "platform", "SKIP", f"~/{plat.platform_dir} does not exist (platform not installed)")]
    if not os.path.exists(os.path.realpath(path)):
        return [Check(t, "config", "FAIL", f"{path} does not exist; no hook installed")]
    try:
        cfg = load_config(path)
        entries = locate(cfg.doc, plat)
    except Refused as exc:
        return [Check(t, "config", "FAIL", str(exc))]
    if cfg.unparseable_numbers:
        return [Check(t, "config", "FAIL", f"{path} holds numbers JSON cannot represent ({_first(cfg.unparseable_numbers)}); "
                                           "the platform's JSON parser rejects the whole file, so no hook runs")]
    elsewhere = count_ours(cfg.doc) - len(entries)
    if not entries:
        hint = (f"; {elsewhere} {HOOK_NAME} command(s) sit elsewhere in the file, where the platform does not gate "
                f"tool calls on them" if elsewhere > 0 else "")
        return [Check(t, "config", "FAIL", f"no {HOOK_NAME} hook under {plat.event} in {path}{hint}")]
    out: list[Check] = []
    for idx, e in enumerate(entries):
        label = "entry" if len(entries) == 1 else f"entry#{idx + 1}"
        if e.problems:
            out.append(Check(t, label, "FAIL", f"{e.where}: " + "; ".join(e.problems)))
            continue
        out.append(Check(t, label, "PASS", f"{e.where} command={e.command}"))
        out.extend(probe_command(e.command, target=t, fmt=plat.fmt, shape=plat.shape, home=paths.home,
                                 timeout=timeout, minimal_path=minimal_path))
    if elsewhere > 0:
        out.append(Check(t, "stray", "WARN", f"{elsewhere} more {HOOK_NAME} command(s) in the file are outside {plat.event} and do nothing"))
    for problem in _switch_warnings(cfg.doc):
        out.append(Check(t, "hook-switch", "FAIL", f"{path}: {problem}; the probes above run the command directly, "
                                                    "so they cannot see this"))
    name, _off = guard_switch()
    if _is_off(os.environ.get(name, "")):
        out.append(Check(t, "guard-switch-env", "WARN",
                         f"{name}={os.environ.get(name)!r} is set in the environment this command runs in: the probes "
                         "scrub it, but an agent started from this environment gets a guard that is off"))
    if plat.key == "codex" and entries and entries[0].group_index is not None:
        out.extend(_codex_checks(paths, os.path.join(paths.home, ".codex", "hooks.json"), entries[0]))
    return out


# --------------------------------------------------------------------------- command line

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python3 -m fleet_lanes.install_tools", description=__doc__.split("\n\n")[0])
    p.add_argument("--self-test", action="store_true",
                   help="build a temp fake home, apply, verify, corrupt the guard, and assert verify FAILS")
    p.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_S, help="seconds per probe subprocess (default 5)")
    p.add_argument("--no-minimal-path", action="store_true",
                   help="skip the second probe run under PATH=/usr/bin:/bin:/usr/sbin:/sbin")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--home", default=None, help="home directory to read and write (default $HOME; must be an existing, non-empty path)")
        sp.add_argument("--stable-dir", default=None, help="stable dir (default <home>/apps/lane-tools)")
        sp.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="seconds per probe subprocess")
        sp.add_argument("--no-minimal-path", action="store_true", default=argparse.SUPPRESS,
                        help="skip the second probe run under PATH=/usr/bin:/bin:/usr/sbin:/sbin")

    def sourcing(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--source", default=None, help="directory that contains fleet_lanes/ (default: this checkout)")
        sp.add_argument("--registry", default=None, help="fleet-apps.json to install (default: the source's)")
        sp.add_argument("--sha", default=None, help="source git sha to record in VERSION (default: git rev-parse HEAD)")

    def writing(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--replace-extra", action="store_true",
                        help="tools: also delete files in an existing lane-tools dir that this tool never writes (the dir must still be one it wrote)")
        sp.add_argument("--follow-symlinks", action="store_true",
                        help="platforms: write through a config symlink that resolves outside --home")

    sp = sub.add_parser("plan", help="read-only: show the files, shims, and the JSON merge as a diff")
    common(sp)
    sourcing(sp)
    writing(sp)
    sp.add_argument("targets", nargs="*", help="tools, a platform, or all (default: all, plus muse listed)")
    sp = sub.add_parser("apply", help="install tools and/or wire the hook into platforms, then verify")
    common(sp)
    sourcing(sp)
    writing(sp)
    sp.add_argument("--no-verify", action="store_true", help="do not run verify after applying")
    sp.add_argument("--strict", action="store_true", help="treat a WARN (for example Codex trust pending) as a failure")
    sp.add_argument("targets", nargs="*", help="tools, a platform, or all (required)")
    sp = sub.add_parser("verify", help="prove the installed hooks deny and allow correctly")
    common(sp)
    sp.add_argument("--strict", action="store_true", help="treat a WARN (for example Codex trust pending) as a failure")
    sp.add_argument("targets", nargs="*", help="tools, a platform, or all (default: all)")
    return p


def _expand(raw: Sequence[str], default: Sequence[str]) -> tuple[list[str], set[str]]:
    """(ordered targets, explicitly named targets).  `all` is tools plus every supported platform."""
    valid = {"tools", "all"} | set(PLATFORM_ORDER)
    for n in raw:
        if n not in valid:
            raise SystemExit(_usage_error(f"unknown target {n!r}; choose from tools, all, {', '.join(PLATFORM_ORDER)}"))
    names = list(raw) or list(default)
    explicit = {n for n in names if n != "all"} if raw else set()
    want: set[str] = set()
    for n in names:
        if n == "all":
            want.update(("tools",) + SUPPORTED_KEYS)
        else:
            want.add(n)
    order = [k for k in ("tools",) + PLATFORM_ORDER if k in want]
    return order, explicit


def _usage_error(message: str) -> int:
    sys.stderr.write(f"python3 -m fleet_lanes.install_tools: error: {message}\n")
    return EXIT_USAGE


def _resolve_home(args: argparse.Namespace) -> str:
    """The home to read and write.  Only an ABSENT --home means $HOME: an empty one (`--home "$UNSET"`) is a
    usage error, so a script bug can never fall through to the real home, and the home must exist so a typo
    cannot create a tree."""
    if args.home is None:
        env = os.environ.get("HOME")
        if env is None:
            home = os.path.expanduser("~")
        elif not env.strip():
            raise SystemExit(_usage_error("HOME is set but empty; pass --home DIR"))
        else:
            home = env
        what = "HOME"
    else:
        if not args.home.strip():
            raise SystemExit(_usage_error("--home must not be empty"))
        home = os.path.expanduser(args.home)
        what = "--home"
    if not os.path.isdir(home):
        raise SystemExit(_usage_error(f"{what} {home} is not an existing directory"))
    if os.path.realpath(home) == os.sep:
        raise SystemExit(_usage_error(f"{what} resolves to /; refusing to use the filesystem root as a home"))
    return home


def _resolve_stable(args: argparse.Namespace) -> str | None:
    if args.stable_dir is None:
        return None
    if not args.stable_dir.strip():
        raise SystemExit(_usage_error("--stable-dir must not be empty"))
    return os.path.expanduser(args.stable_dir)


def _print_checks(checks: Sequence[Check], emit: Callable[[str], None]) -> None:
    for c in checks:
        emit(c.line())


def _verify_targets(targets: Sequence[str], explicit: set[str], paths: Paths, *, timeout: float,
                    minimal_path: bool, strict: bool, emit: Callable[[str], None]) -> int:
    all_checks: list[Check] = []
    for tgt in targets:
        got = verify_tools(paths, timeout=timeout) if tgt == "tools" else verify_platform(
            PLATFORMS[tgt], paths, timeout=timeout, minimal_path=minimal_path)
        # A platform that is not installed is a skip when reached through `all`, a FAIL when named.
        if tgt != "tools" and tgt in explicit and len(got) == 1 and got[0].status == "SKIP":
            got = [Check(got[0].target, got[0].name, "FAIL", got[0].detail + " (named explicitly)")]
        _print_checks(got, emit)
        all_checks.extend(got)
    fails = sum(1 for c in all_checks if c.status == "FAIL")
    warns = sum(1 for c in all_checks if c.status == "WARN")
    passes = sum(1 for c in all_checks if c.status == "PASS")
    skipped = sum(1 for c in all_checks if c.status == "SKIP")
    emit(f"verify: {passes} PASS, {fails} FAIL, {warns} WARN, {skipped} SKIP"
         + ("  -> NOT HEALTHY" if fails or (strict and warns) else ("  -> healthy" + (" (see WARN)" if warns else ""))))
    return EXIT_FAIL if fails or (strict and warns) else EXIT_OK


def _cmd_plan(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    home = _resolve_home(args)
    paths = make_paths(home, _resolve_stable(args))
    targets, explicit = _expand(args.targets, ("all", "muse"))
    src = resolve_source(args.source, args.registry, args.sha)
    rc = EXIT_OK
    emit(f"plan (read-only)  home={paths.home}  stable={paths.stable}  source={src.scripts_dir}  sha={src.sha}")
    for tgt in targets:
        emit("")
        if tgt == "tools":
            try:
                tp = plan_tools(src, paths, replace_extra=args.replace_extra)
            except Refused as exc:
                emit(f"== tools  REFUSE: {exc}")
                rc = EXIT_FAIL
                continue
            emit(f"== tools  stable dir {tp.stable}  [{tp.state}]")
            for fs in tp.files:
                if fs.status not in ("same", "stale"):
                    emit(f"   {fs.status:<8} {fs.rel}")
            same = sum(1 for fs in tp.files if fs.status == "same")
            emit(f"   {same} file(s) already identical; the whole dir is replaced atomically (stage, probe, swap)")
            stale = [fs.rel for fs in tp.files if fs.status == "stale"]
            if stale and not tp.problems:
                emit(f"   WILL DELETE {len(stale)} file{'' if len(stale) == 1 else 's'} the new copy does not contain:")
                for rel in stale[:20]:
                    emit(f"     {rel}")
                if len(stale) > 20:
                    emit(f"     ... and {len(stale) - 20} more")
            emit("   VERSION would read:")
            for ln in tp.version_text.splitlines():
                emit(f"     {ln}")
            emit("   the shims run `/usr/bin/env python3 -I`; the minimum is Python 3.9 (macOS /usr/bin/python3), "
                 "and a GUI-launched app may resolve python3 under PATH=/usr/bin:/bin only")
            emit(f"   shim {paths.hook_shim}:")
            for ln in tp.hook_shim_text.splitlines():
                emit(f"     | {ln}")
            if tp.lane_shim_state == "foreign":
                emit(f"   shim {paths.lane_shim}: LEFT ALONE, it is {tp.lane_detail}; the stable dir and hook still install")
            elif tp.lane_shim_text is None:
                emit(f"   shim {paths.lane_shim}: SKIPPED, the source package has no {LANE_MODULE}"
                     + ("; a stale generated lane shim would be removed" if tp.lane_shim_state == "stale" else ""))
            else:
                emit(f"   shim {paths.lane_shim} [{tp.lane_shim_state}]:")
                for ln in tp.lane_shim_text.splitlines():
                    emit(f"     | {ln}")
            for prob in tp.problems:
                emit(f"   REFUSE: {prob}")
                rc = EXIT_FAIL
            continue
        plat = PLATFORMS[tgt]
        pp = plan_platform(plat, paths, follow_symlinks=args.follow_symlinks)
        emit(f"== {plat.key}  {plat.label}  {pp.path}  [{pp.action}]")
        if pp.link:
            emit(f"   {pp.link}")
        if pp.reason:
            emit(f"   {pp.reason}")
        if pp.action not in ("unsupported", "skip", "refuse"):
            emit(f"   command: {pp.command}")
        if pp.hidden:
            one = pp.hidden == 1
            emit(f"   {pp.hidden} other hook{'' if one else 's'} under {pp.label} {'is' if one else 'are'} "
                 "left untouched and not shown")
        for ln in pp.diff:
            emit(f"   {ln}")
        for n in pp.notes + list(plat.notes):
            emit(f"   note: {n}")
        for u in plat.unverified:
            emit(f"   UNVERIFIED: {u}")
        if pp.action == "refuse" or (tgt in explicit and pp.action in ("unsupported", "skip")):
            rc = EXIT_FAIL
    return rc


def _cmd_apply(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    if not args.targets:
        return _usage_error("apply needs a target: tools, a platform, or all")
    home = _resolve_home(args)
    paths = make_paths(home, _resolve_stable(args))
    targets, explicit = _expand(args.targets, ())
    minimal = not args.no_minimal_path
    results: list[Result] = []
    for tgt in targets:
        if tgt == "tools":
            res = apply_tools(resolve_source(args.source, args.registry, args.sha), paths, timeout=args.timeout,
                              minimal_path=minimal, emit=emit, replace_extra=args.replace_extra)
        else:
            # Never `shim_proven`: each platform's own command is probed right now, in every run.
            res = apply_platform(PLATFORMS[tgt], paths, timeout=args.timeout, minimal_path=minimal,
                                 follow_symlinks=args.follow_symlinks)
        results.append(res)
        emit(res.line())
    failed = [r for r in results if not r.ok and not (r.status == "skipped" and r.target not in explicit)]
    rc = EXIT_FAIL if failed else EXIT_OK
    for r in results:
        if r.status in ("added", "updated") and r.target in NEXT_STEPS:
            emit(f"NEXT       {r.target:<12} {NEXT_STEPS[r.target]}")
    if not args.no_verify:
        todo = [r.target for r in results if r.ok]
        if todo:
            emit("")
            emit("verifying what was just applied ...")
            vrc = _verify_targets(todo, explicit, paths, timeout=args.timeout, minimal_path=minimal,
                                  strict=args.strict, emit=emit)
            rc = max(rc, vrc)
    return rc


def _cmd_verify(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    home = _resolve_home(args)
    paths = make_paths(home, _resolve_stable(args))
    targets, explicit = _expand(args.targets, ("all",))
    return _verify_targets(targets, explicit, paths, timeout=args.timeout, minimal_path=not args.no_minimal_path,
                           strict=args.strict, emit=emit)


# --------------------------------------------------------------------------- self-test

def _seed_fake_home(home: str) -> None:
    """Platform dirs with the real files' SHAPES and dummy commands."""
    def put(rel: str, doc: object) -> None:
        full = os.path.join(home, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    other = {"type": "command", "command": "/bin/true", "timeout": 3}
    put(".claude/settings.json", {"env": {"EXAMPLE": "x"}, "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [dict(other, statusMessage="dummy")]}]}})
    put(".codex/hooks.json", {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [dict(other)]}]}})
    put(".cursor/hooks.json", {"version": 1, "hooks": {"sessionStart": [{"command": "/bin/true", "timeout": 8, "failClosed": False}]}})
    put(".gemini/config/hooks.json", {"other-hook": {"Stop": [dict(other)], "enabled": True}})
    put(".grok/hooks/imported-from-claude.json", {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [dict(other)]}]}})


def self_test(emit: Callable[[str], None], *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True,
              source: Source | None = None) -> int:
    """Apply everything into a temp fake home, verify it, then break the installed guard on purpose and
    require verify to FAIL, then repair it and require verify to pass again.  Prints the non-PASS lines
    of each phase and a count; `verify` prints every line."""
    src = source or resolve_source()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lane-tools-selftest-") as tmp:
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        _seed_fake_home(home)
        paths = make_paths(home)
        emit(f"self-test: fake home {home}")
        res = apply_tools(src, paths, timeout=timeout, minimal_path=minimal_path, emit=emit)
        emit(res.line())
        if not res.ok:
            return EXIT_FAIL
        for key in SUPPORTED_KEYS:
            r = apply_platform(PLATFORMS[key], paths, timeout=timeout, minimal_path=minimal_path, shim_proven=True)
            emit(r.line())
            if not r.ok:
                failures.append(f"apply {key}: {r.detail}")
        targets = ["tools"] + list(SUPPORTED_KEYS)

        def run_verify(label: str, show: tuple) -> tuple[int, list[Check]]:
            checks: list[Check] = []
            for tgt in targets:
                checks.extend(verify_tools(paths, timeout=timeout) if tgt == "tools"
                              else verify_platform(PLATFORMS[tgt], paths, timeout=timeout, minimal_path=minimal_path))
            counts = {st: sum(1 for c in checks if c.status == st) for st in ("PASS", "FAIL", "WARN", "SKIP")}
            emit(f"self-test: verify {label}: " + ", ".join(f"{n} {st}" for st, n in counts.items()))
            _print_checks([c for c in checks if c.status in show], emit)
            return counts["FAIL"], checks

        fails, _ = run_verify("after install, expect no FAIL", ("FAIL", "WARN"))
        if fails:
            failures.append(f"verify reported {fails} FAIL on a fresh install")
        guard = os.path.join(paths.package_dir, "guard.py")
        hidden = guard + ".disabled"
        os.rename(guard, hidden)
        try:
            _fails, checks = run_verify("with guard.py renamed, expect FAIL on every platform", ("FAIL",))
            for key in SUPPORTED_KEYS:
                if not any(c.target == key and c.name.startswith("deny/") and c.status == "FAIL" for c in checks):
                    failures.append(f"verify did NOT fail {key} with the guard module renamed: a broken install would read as healthy")
            if not any(c.target == "tools" and c.status == "FAIL" for c in checks):
                failures.append("verify tools did not notice the missing module")
        finally:
            os.rename(hidden, guard)
        fails, _ = run_verify("after repair, expect no FAIL", ("FAIL",))
        if fails:
            failures.append(f"verify reported {fails} FAIL after the guard was restored")
    if failures:
        for f in failures:
            emit(f"self-test FAIL: {f}")
        return EXIT_FAIL
    emit("self-test PASS: the install verifies, a renamed guard module makes verify FAIL, and the repair verifies again")
    return EXIT_OK


# --------------------------------------------------------------------------- entry point

def main(argv: Sequence[str] | None = None, stdout=None, stderr=None) -> int:
    out = sys.stdout if stdout is None else stdout

    def emit(line: str) -> None:
        out.write(line + "\n")
        out.flush()

    old_stderr = sys.stderr
    if stderr is not None:
        sys.stderr = stderr
    try:
        parser = _build_parser()
        try:
            args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
        try:
            if args.self_test:
                return self_test(emit, timeout=args.timeout, minimal_path=not args.no_minimal_path)
            if not args.cmd:
                parser.print_usage(sys.stderr)
                return EXIT_USAGE
            handler = {"plan": _cmd_plan, "apply": _cmd_apply, "verify": _cmd_verify}[args.cmd]
            return handler(args, emit)
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
        except Refused as exc:
            emit(f"REFUSED {exc}")
            return EXIT_FAIL
    finally:
        sys.stderr = old_stderr


if __name__ == "__main__":
    sys.exit(main())
