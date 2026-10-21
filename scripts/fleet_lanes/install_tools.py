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

PLATFORM is claude, codex, grok, antigravity, cursor, minimax (alias mm), muse, clutch, kimi, copilot, opencode
or fx.  The first five get a hook entry in their config file.  minimax is a plugin DIRECTORY that this tool owns:
MiniMax Code reads no settings
file for hooks, it loads them from a real directory under ~/.minimax/plugins/<name>/ (a symlink is skipped), so `apply
minimax` writes ~/.minimax/plugins/fleet-guard/ (.claude-plugin/plugin.json and hooks/hooks.json: PreToolUse, matcher
Bash|Read|Write|Edit|bash|read|write|edit, its own minimax-guard-hook), refuses a directory it did not write, and `verify minimax` runs the command
it reads back out of that hooks.json.  muse is different: Muse Code takes hooks from a PLUGIN, so `apply tools` writes a plugin
bundle into the stable dir (with everything else, atomically), `plan muse` shows it and the OWNER ACTIONS
that finish the job, `apply muse` is `apply tools` (the bundle is part of the stable copy, and an unchanged
copy is left alone) followed by those actions, and `verify muse` runs the hook the way Muse does.  This tool
never edits Muse's config, and the only muse it runs is `muse plugins validate` inside the muse verify (a bare
`verify`, `verify muse`, `apply muse` unless --no-verify, and --self-test, each when muse is on PATH): the muse
found on PATH, with HOME, the XDG dirs, TMPDIR and Muse's credential path inside a throwaway dir, none of the
caller's XDG_ or MUSE_ variables except the plugin switch, and MUSE_NO_AUTO_UPDATE=1 and MUSE_LOGIN=0 (see
muse_validate_env).  That is confined, not proven read-only: the launcher still runs from its own install dir.
`all` is `tools` plus the five config platforms plus minimax (an own directory, no owner step afterwards), plus
copilot, opencode and fx.  `apply` needs a target; `plan` defaults to all plus muse, clutch
and kimi, and `verify` the same.

copilot, opencode and fx (AFC-387).  copilot is a fleet-owned file, ~/.copilot/hooks/fleet-lane-guard.json
(version 1, hooks.PreToolUse, matcher Bash, the fleet hook with --format claude and timeoutSec 5); this tool never
edits ~/.copilot/settings.json, and whether Copilot fires a preToolUse hook at all is UNVERIFIED on this Mac, so the
first install is owner-gated and `verify copilot` ends with a SKIP for the live proof.  opencode is a
local TypeScript plugin in the stable dir (opencode-plugin/fleet-lane-guard.ts) listed by absolute path in the
plugin array of ~/.config/opencode/opencode.json; it inspects tool.execute.before and denies through
permission.ask, and whether permission.ask is reached for a denied call is UNVERIFIED.  fx has NO hook surface at
all: `plan fx` prints a prepared compensating control (a static permission.bash deny list over the temp-destination
shapes, with permission_mode moved off yolo) for the owner to merge by hand, this tool never writes ~/.fx, and
`verify fx` is a FAIL (UNSUPPORTED) with the blocker recorded until Jay approves that control or an exception.

The fleet hook.  `fleet-guard-hook` (fleet_lanes/fleet_guard_hook.py) is the lane guard AND the secret guard in one
command:  the lane guard, then the secret guard (scripts/hooks/secret-guard-pretooluse.py, copied into the stable dir
as fleet_lanes/secret_guard.py: any `ps`, a dump of a secret-bearing file, an authorization value in arguments, a
byte-dump of a key variable), and the first deny wins.  Its shell prefilter is the lane guard's trigger words plus the
words without which no secret-guard rule can fire (SECRET_WORDS, and a `ps` followed by white space), so an ordinary
shell call is answered in `sh` without starting Python.  The harnesses that were added after the first five use it: MiniMax Code (its plugin's hooks.json runs it), Muse Code (its plugin wrapper runs it), and
clutch and kimi, which are marked blocks in a file this tool does not own.  Claude Code and Codex already run the
secret guard as a hook of their own, so they keep `lane-guard-hook` alone.

clutch and kimi.  Each is ONE marked block (`# fleet:begin hooks-fleet-guards` ... `# fleet:end hooks-fleet-guards`)
and nothing outside it is touched:
    clutch  ~/.clutch/dsh/cordis.patch.yml   an `- insert:` entry that mounts @deepseek-ai/dsh-hooks-claude-code with
            configPath = <stable>/clutch/hooks.json (a fleet-only hooks.json, matcher `bash`; never ~/.claude/settings.json,
            which carries Claude-only hooks).  A profile with `patchReload: live` (clutch-web's `web`) watches this file,
            so every write is a live deploy into the running engine; a `startup` profile reads it at its next start.  An
            empty or comment-only patch makes the engine exit, so before it writes, `apply clutch` (1) refuses unless each
            plugin package resolves under the real ~/.clutch/dsh/profiles/node_modules (the engine repairs a dangling link
            only when it boots: restart clutch-web first), and (2) runs the pinned `dsh --profile P --dump-config` on a
            scratch copy of the profiles (dsh_check), a merge-and-parse check, not a plugin load.
    kimi    ~/.kimi-code/config.toml          a `[[hooks]]` entry (event PreToolUse, matcher Bash, the fleet hook with
            `--format kimi`: exit code 2 with the reason on stderr, which Kimi Code documents).  Kimi rewrites its own
            config without comments, so the entry is found by its content, never by the markers (toml_hooks).  When tomllib
            exists the new file must parse, and `kimi doctor` (on PATH, else ~/.kimi-code/bin/kimi) must accept a scratch
            copy of it; with no kimi binary the write is refused unless --no-kimi-check is given.
Neither is in `all`, because each belongs to a tool the owner may not run.  `apply clutch` and `apply kimi` run
`apply tools` first, like muse.  Whether the tool then FIRES the hook is not visible from here (UNVERIFIED): the live
proof is to ask the tool for a `ps` in a new session.

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
        muse-seat                     executable shim: AGENT_SEAT=MC and AGENT_TAG=MC, then the real `muse`
        muse-plugin/fleet-lane-guard/ the Muse Code plugin bundle (.muse-plugin/plugin.json, hooks/lane-guard.sh)
    apps/lane                         executable shim for `python3 -m fleet_lanes.lane`; written only when
                                      the package has lane.py, so it can never be a dead command

Both shims run `/usr/bin/env python3 -I -c ...`.  `-I` drops the working directory and every PYTHON*
variable, so a lane that has its own half-edited fleet_lanes in the cwd (scripts/ for example) can
never shadow the installed copy.  No `-B`: Python writes __pycache__ into the stable dir on first use,
which keeps the per-Bash-call hook fast, and the tree digest ignores it.  The hook shim finds its
own directory from $0, so the staged copy can be proven before it is swapped in.

The hook shim also has a shell PREFILTER, because starting Python costs about 0.2 s on a loaded machine and
the hook runs on every Bash call on five platforms.  The guard itself returns early unless a command holds one
of a few words (guard._TRIGGER).  The shim reads the payload with the shell and, when it holds none of those
words, exits 0 with no output without starting Python.  The words are COPIED from guard.py when the shim is
generated (`read_guard_facts`: the imported guard for this checkout, the syntax tree of guard.py for any other
tree), never typed here; if _TRIGGER is not a plain alternation of lower-case literal words compiled with exactly
re.IGNORECASE, the shim gets no prefilter and `plan` and `apply` say so.  The match is a superset of the guard's
own: every word in any ASCII letter case (bracket pairs, so no `tr`), a JSON `\\u` escape (it can spell any
letter), and the three non-ASCII characters that re.IGNORECASE folds onto a trigger letter (U+0130, U+0131,
U+212A).  It compares bytes: the shim sets LC_ALL=C before it reads the payload, because in a multibyte locale
such as ja_JP.SJIS bash and zsh read an ASCII letter after a UTF-8 character as part of that character, and puts
the caller's LC_ALL back (set, empty or unset) before Python starts.  A payload over guard.MAX_COMMAND_CHARS skips
the match.  Whatever is not provably free of a trigger word reaches Python as the original bytes, so the answer is
the guard's, byte for byte.  A NUL byte cannot live in a shell variable and is dropped; NUL is not valid JSON, so
the guard allowed that payload anyway.  `verify` proves deny through the installed shim, and fails on a shim
whose patterns match nothing.

The Muse Code plugin.  Muse rejects a `matcher` on a plugin hook, so its hook runs on EVERY tool call.  The
bundle's hooks/lane-guard.sh therefore exits 0 at once only when it can PROVE from the raw bytes that the guard
will not see a shell call: the top-level tool key the guard reads (tool_name, or toolName when there is no
tool_name) occurs exactly once, in the first 4096 bytes, with nothing nested or escaped before it, and holds a
string with none of the guard's shell tool words after it.  Everything else (no tool key, a null one, one that
only occurs nested or as a value, duplicates, odd spacing, a `\\u` escape) goes to the stable lane-guard-hook
with --format muse as the original bytes (the Claude deny shape; an allow is empty stdout and exit 0), so the
guard decides.  It matches bytes under LC_ALL=C like the shim.  Muse runs hooks outside its sandbox with a
cleared environment, so every path in the bundle is absolute.  UNVERIFIED: whether MUSE_EXPERIMENTAL_PLUGINS=1
is required, whether a running session picks up an approved plugin, whether `muse plugins install` (it copies the
bundle into its cache) keeps the absolute command, and that Muse puts tool_name before any nested object (if it
does not, every call reaches the shim, which is correct and slower).

muse-seat starts Muse Code as seat MC: it sets AGENT_SEAT=MC and AGENT_TAG=MC and execs the real `muse` that
`command -v muse` finds.  It refuses (exit 127, a message on stderr) when there is no muse, when the muse it
finds is the wrapper itself, and when muse-seat already ran in this chain of processes (it exports
_MUSE_SEAT_ACTIVE), which catches a wrapper script named muse that starts muse-seat again.  So inside a session
muse-seat started, run `muse` itself; AGENT_SEAT is already MC there.

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

Exit codes: 0 success, 1 a FAIL or a refusal, 64 usage error (matches the doctor).  A platform that is
named on the command line and is not installed exits 1; the same platform reached through `all` is
reported and skipped.

Tests: fleet_lanes/tests/test_install_tools.py.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import difflib
import fcntl
import functools
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from . import cordis_patch as CP
from . import dsh_check as DC
from . import marked_block as MB
from . import toml_hooks as TH

__all__ = [
    "ALLOW_COMMAND",
    "ALL_KEYS",
    "DENY_COMMAND",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_USAGE",
    "HOOK_NAME",
    "PLATFORMS",
    "SUPPORTED_KEYS",
    "Check",
    "Paths",
    "Platform",
    "Refused",
    "Source",
    "apply_platform",
    "apply_tools",
    "build_files",
    "check_deny_output",
    "guard_switch",
    "hook_command",
    "main",
    "make_paths",
    "plan_platform",
    "plan_tools",
    "probe_payload",
    "render_hook_shim",
    "render_lane_shim",
    "resolve_source",
    "self_test",
    "verify_platform",
    "verify_tools",
]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 64

PACKAGE = "fleet_lanes"
HOOK_NAME = "lane-guard-hook"
MINIMAX_HOOK_NAME = "minimax-guard-hook"
FLEET_HOOK_NAME = "fleet-guard-hook"  # the lane guard and the secret guard in one command
SECRET_GUARD_MODULE = "secret_guard.py"  # fleet_lanes/secret_guard.py in the stable copy
SECRET_GUARD_SOURCE = ("hooks", "secret-guard-pretooluse.py")  # under the source's scripts dir
CLUTCH_HOOKS_FILE = "clutch/hooks.json"  # the fleet-only hooks.json Clutch's bridge plugin reads
CLUTCH_PATCH_REL = ".clutch/dsh/cordis.patch.yml"
KIMI_CONFIG_REL = ".kimi-code/config.toml"
# MiniMax Code reads no settings file for hooks.  It loads them from a local plugin, a REAL directory (a symlink is
# skipped) under ~/.minimax/plugins/<name>/.  This tool owns that one directory outright.
MINIMAX_PLUGIN_ID = "fleet-guard"
MINIMAX_PLUGIN_REL = ".minimax/plugins/" + MINIMAX_PLUGIN_ID
MINIMAX_MANIFEST_FILE = (
    ".claude-plugin/plugin.json"  # the Claude-compatible flavour: matchers see Claude tool names
)
MINIMAX_HOOKS_FILE = "hooks/hooks.json"  # the default hooks path of that flavour
MINIMAX_HOOK_TIMEOUT_S = (
    10  # seconds, 1 to 10 (MiniMax's range).  A timeout fails OPEN, so the top of the range
)
PLATFORM_ALIASES = {"mm": "minimax"}  # the seat tag works as a target
COPILOT_HOOKS_REL = ".copilot/hooks/fleet-lane-guard.json"  # a fleet-owned file; ~/.copilot/settings.json is not ours
OPENCODE_CONFIG_REL = ".config/opencode/opencode.json"
OPENCODE_PLUGIN_FILE = "opencode-plugin/fleet-lane-guard.ts"  # relative to the stable dir
OPENCODE_PLUGIN_ID = "fleet-lane-guard"
FX_BLOCKER = (
    "UNSUPPORTED: fx has no hook, plugin or extension surface in the CLI (fx --help and the published docs index "
    "name none), so the lane guard cannot run there.  The only enforcement surface is static permission rules in "
    "~/.fx/settings.json, which cannot resolve d=$(mktemp -d), cannot read a repo's fleet status, and cannot see "
    "through a bash -c layer, all of which guard.py handles.  FX stays RED until Jay approves the prepared "
    "compensating control (plan fx prints it) or approves an exception"
)
BLOCK_NAME = "hooks-fleet-guards"
BLOCK_TOOL = "fleet_lanes.install_tools"
LANE_NAME = "lane"
STABLE_DIRNAME = "lane-tools"
REGISTRY_NAME = "fleet-apps.json"
VERSION_NAME = "VERSION"
GENERATED_MARK = "Generated by fleet_lanes.install_tools"
REQUIRED_MODULES = (
    "__init__.py",
    "layout.py",
    "guard.py",
    "lane_guard_hook.py",
    "fleet_guard_hook.py",
    "minimax_guard_hook.py",
)
LANE_MODULE = "lane.py"
AG_GROUP = "lane-guard"
GROK_FILE = "lane-guard.json"

# Muse Code takes hooks from a PLUGIN, not from a settings file.  The bundle and the seat shim are
# written into the stable dir by `apply tools`, with everything else, so they are swapped in atomically.
MUSE_SEAT_NAME = "muse-seat"
MUSE_PLUGIN_ID = "fleet-lane-guard"
MUSE_PLUGIN_DIR = "muse-plugin/" + MUSE_PLUGIN_ID  # relative to the stable dir
MUSE_MANIFEST = MUSE_PLUGIN_DIR + "/.muse-plugin/plugin.json"
MUSE_WRAPPER = MUSE_PLUGIN_DIR + "/hooks/lane-guard.sh"
MUSE_SEAT_TAG = "MC"  # AGENT_SEAT and AGENT_TAG for Muse Code
_MUSE_FILES = (MUSE_SEAT_NAME, MUSE_MANIFEST, MUSE_WRAPPER)
MUSE_EXPERIMENTAL_ENV = "MUSE_EXPERIMENTAL_PLUGINS"

HOOK_TIMEOUT_S = 5  # timeout written into each platform config (seconds)
MUSE_HOOK_TIMEOUT_MS = 15000  # Muse runs the wrapper on EVERY tool call; three times the others, because a timeout's outcome is unknown
PROBE_TIMEOUT_S = 5.0  # per probe subprocess
MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # what a GUI-launched app typically has
BACKUP_TAG = "bak-lane-guard"
BACKUP_MODE = 0o600  # a backup is owner-only whatever the original's mode

# The probe commands.  They are the fixture rows `spec-gh-hoghunter` (deny) and `spec-third-party`
# (allow) of tests/fixtures_guard.py, copied here because the stable dir has no tests.  The test
# module asserts they still equal those rows.  The allow command gets past the guard's trigger
# regex, so the evaluator really runs and decides.
DENY_COMMAND = "gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify"
DENY_MARKER = "hh-verify"
ALLOW_COMMAND = "git clone https://github.com/someone/else.git /tmp/else"
# The secret guard's probe: any `ps` is a hard deny (rule C of scripts/hooks/secret-guard-pretooluse.py), and its
# reason names the command in backticks.
SECRET_DENY_COMMAND = "ps aux"
SECRET_DENY_MARKER = "`ps`"
# Formats that are the plain-text exit-2 contract (the reason on stderr, exit code 2, nothing on stdout).
EXIT2_FORMATS = ("kimi",)

# Environment variables that would mask a broken install if they leaked into a probe.
_SCRUB_ENV = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONSAFEPATH",
    "PYTHONUSERBASE",
    "PYTHONUTF8",
    "PYTHONDONTWRITEBYTECODE",
    "VIRTUAL_ENV",
    "FLEET_APPS_JSON",
    "FLEET_LAYOUT",
    "FLEET_LANES_ROOT",
    "FLEET_LANES_EXTERNAL_ROOT",
    "FLEET_LANE_GUARD",
    "AGENT_SEAT",
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


_OURS_RE = re.compile(
    r"(?:^|[\s/'\"=])(?:"
    + re.escape(HOOK_NAME)
    + "|"
    + re.escape(FLEET_HOOK_NAME)
    + "|"
    + re.escape(MINIMAX_HOOK_NAME)
    + r")(?=$|[\s'\"])"
)


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
    def fleet_shim(self) -> str:
        return os.path.join(self.stable, FLEET_HOOK_NAME)

    @property
    def clutch_hooks(self) -> str:
        return os.path.join(self.stable, *CLUTCH_HOOKS_FILE.split("/"))

    @property
    def clutch_patch(self) -> str:
        return os.path.join(self.home, *CLUTCH_PATCH_REL.split("/"))

    @property
    def kimi_config(self) -> str:
        return os.path.join(self.home, *KIMI_CONFIG_REL.split("/"))

    @property
    def minimax_bundle(self) -> str:
        return os.path.join(self.home, *MINIMAX_PLUGIN_REL.split("/"))

    @property
    def minimax_manifest(self) -> str:
        return os.path.join(self.minimax_bundle, *MINIMAX_MANIFEST_FILE.split("/"))

    @property
    def minimax_hooks(self) -> str:
        return os.path.join(self.minimax_bundle, *MINIMAX_HOOKS_FILE.split("/"))

    @property
    def copilot_hooks(self) -> str:
        return os.path.join(self.home, *COPILOT_HOOKS_REL.split("/"))

    @property
    def opencode_config(self) -> str:
        return os.path.join(self.home, *OPENCODE_CONFIG_REL.split("/"))

    @property
    def opencode_plugin(self) -> str:
        return os.path.join(self.stable, *OPENCODE_PLUGIN_FILE.split("/"))

    @property
    def package_dir(self) -> str:
        return os.path.join(self.stable, PACKAGE)

    @property
    def registry(self) -> str:
        return os.path.join(self.stable, REGISTRY_NAME)

    @property
    def version(self) -> str:
        return os.path.join(self.stable, VERSION_NAME)

    @property
    def muse_seat(self) -> str:
        return os.path.join(self.stable, MUSE_SEAT_NAME)

    @property
    def muse_bundle(self) -> str:
        return os.path.join(self.stable, *MUSE_PLUGIN_DIR.split("/"))

    @property
    def muse_manifest(self) -> str:
        return os.path.join(self.stable, *MUSE_MANIFEST.split("/"))

    @property
    def muse_wrapper(self) -> str:
        return os.path.join(self.stable, *MUSE_WRAPPER.split("/"))


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
    scripts_dir: str  # the directory that CONTAINS fleet_lanes/
    registry: str  # fleet-apps.json to copy
    sha: str  # git sha of the source checkout, or "unknown"


_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def git_head(path: str, timeout: float = 10.0) -> str:
    """HEAD of the checkout that holds `path`, read-only; "unknown" when git cannot say."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        p = subprocess.run(
            ["git", "--no-optional-locks", "-C", path, "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
        )
        sha = p.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return sha if p.returncode == 0 and _SHA_RE.match(sha) else "unknown"


def resolve_source(
    scripts_dir: str | None = None,
    registry: str | None = None,
    sha: str | None = None,
    sha_fn: Callable[[str], str] = git_head,
) -> Source:
    """The tree to install from.  The defaults are the checkout this module runs from; `sha` is
    injectable (--sha, tests)."""
    sd = (
        os.path.abspath(scripts_dir)
        if scripts_dir
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if registry:
        reg = os.path.abspath(registry)
    else:
        up = os.path.join(os.path.dirname(sd), REGISTRY_NAME)
        reg = up if os.path.isfile(up) else os.path.join(sd, REGISTRY_NAME)
    return Source(scripts_dir=sd, registry=reg, sha=sha if sha else sha_fn(sd))


# --------------------------------------------------------------------------- what the shell scripts mirror of the guard


@dataclass(frozen=True)
class GuardFacts:
    """The few facts about guard.py that the generated shell scripts mirror.  They are read once, when a script
    is generated, never when it runs.  Anything that cannot be mirrored exactly is left out and named in
    `problems`: a script then does LESS filtering (more calls reach the guard), never more."""

    words: tuple = ()  # guard._TRIGGER's literal words, lower case; () = no prefilter
    lookalikes: tuple = ()  # non-ASCII characters that re.IGNORECASE folds onto a letter of those words
    max_chars: int = 0  # guard.MAX_COMMAND_CHARS; 0 = unknown, so no size gate
    shell_hints: tuple = ()  # guard._SHELL_TOOL_HINTS plus "bash"; () = the Muse wrapper filters nothing
    problems: tuple = ()  # why something above is missing, for `plan`


_TRIGGER_WORDS_RE = re.compile(r"[a-z][a-z-]*(?:\|[a-z][a-z-]*)*")
_HINT_RE = re.compile(r"[a-z][a-z_]*")


@functools.lru_cache(maxsize=16)
def _fold_lookalikes(letters: str) -> tuple:
    """The non-ASCII characters that match one of `letters` under re.IGNORECASE.  Python folds U+212A (Kelvin
    sign) onto k and U+0130 and U+0131 onto i, so a payload can hold a trigger word without the ASCII spelling.
    Only the Basic Multilingual Plane is scanned (about 30 ms); the tests scan every code point and assert
    nothing above it folds onto an ASCII letter."""
    cls = re.compile("[" + letters + "]", re.IGNORECASE)
    return tuple(
        ch
        for ch in map(chr, range(0x80, 0x10000))
        if not 0xD800 <= ord(ch) < 0xE000 and cls.fullmatch(ch)
    )


def _facts_from_values(
    pattern: object, flags_ok: bool, max_chars: object, hints: object, where: str
) -> GuardFacts:
    problems: list = []
    words: tuple = ()
    look: tuple = ()
    if not isinstance(pattern, str) or not _TRIGGER_WORDS_RE.fullmatch(pattern):
        problems.append(
            f"{where}: guard._TRIGGER is not a plain alternation of lower-case literal words, so the "
            "hook shim has NO prefilter and starts Python on every call (still correct, only slower)"
        )
    elif not flags_ok:
        problems.append(
            f"{where}: guard._TRIGGER is not compiled with exactly re.IGNORECASE, so the hook shim has "
            "NO prefilter and starts Python on every call (still correct, only slower)"
        )
    else:
        words = tuple(pattern.split("|"))
        look = _fold_lookalikes("".join(sorted({c for w in words for c in w if c.isalpha()})))
    cap = (
        max_chars
        if isinstance(max_chars, int) and not isinstance(max_chars, bool) and max_chars > 0
        else 0
    )
    tools: tuple = ()
    if (
        isinstance(hints, (tuple, list))
        and hints
        and all(isinstance(h, str) and _HINT_RE.fullmatch(h) for h in hints)
    ):
        tools = tuple(dict.fromkeys(("bash",) + tuple(hints)))
    else:
        problems.append(
            f"{where}: guard._SHELL_TOOL_HINTS is not a tuple of plain lower-case words, so the Muse "
            "wrapper cannot tell shell tools from others and hands every tool call to the guard"
        )
    return GuardFacts(words, look, cap, tools, tuple(problems))


def _guard_facts_local() -> GuardFacts:
    """From the guard this module sits beside (the usual case: the checkout being installed)."""
    try:
        from . import guard as g

        trigger = g._TRIGGER
        return _facts_from_values(
            trigger.pattern,
            (trigger.flags & ~re.UNICODE) == re.IGNORECASE,
            getattr(g, "MAX_COMMAND_CHARS", None),
            getattr(g, "_SHELL_TOOL_HINTS", None),
            "guard.py",
        )
    except Exception as exc:  # an unreadable guard must not stop an install: it gets no filtering
        return GuardFacts(
            problems=(
                f"guard.py cannot be read for the shell scripts ({type(exc).__name__}: {exc}); "
                "they get no prefilter and no tool filter",
            )
        )


def _is_re_name(node: object, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "re"
    )


def _const_int(node: object) -> object:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = _const_int(node.left), _const_int(node.right)
        if isinstance(left, int) and isinstance(right, int):
            return left * right if isinstance(node.op, ast.Mult) else left + right
    return None


def _guard_facts_from_file(path: str) -> GuardFacts:
    """From another tree's guard.py (a --source elsewhere, or the stable copy), WITHOUT running it: the three
    assignments are read from the syntax tree, and only the plain shapes guard.py uses are understood."""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError) as exc:
        return GuardFacts(
            problems=(
                f"{path} cannot be read for the shell scripts ({type(exc).__name__}: {exc}); "
                "they get no prefilter and no tool filter",
            )
        )
    pattern: object = None
    flags_ok = False
    cap: object = None
    hints: object = None
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        name, value = node.targets[0].id, node.value
        if name == "_TRIGGER":
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "compile"
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == "re"
                and len(value.args) == 2
                and not value.keywords
                and isinstance(value.args[0], ast.Constant)
                and isinstance(value.args[0].value, str)
            ):
                pattern, flags_ok = value.args[0].value, _is_re_name(value.args[1], "IGNORECASE")
        elif name == "MAX_COMMAND_CHARS":
            cap = _const_int(value)
        elif name == "_SHELL_TOOL_HINTS":
            if isinstance(value, ast.Tuple) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                hints = tuple(e.value for e in value.elts)
    return _facts_from_values(
        pattern, flags_ok, cap, hints, os.path.basename(os.path.dirname(path)) + "/guard.py"
    )


def read_guard_facts(scripts_dir: str | None = None) -> GuardFacts:
    """What the shell scripts mirror of the guard that is (or will be) installed from `scripts_dir`, a directory
    that contains fleet_lanes/.  None, or this very package, means the imported guard; any other tree is read as
    text (see `_guard_facts_from_file`)."""
    if scripts_dir is None:
        return _guard_facts_local()
    pkg = os.path.join(scripts_dir, PACKAGE)
    if os.path.realpath(pkg) == os.path.realpath(os.path.dirname(os.path.abspath(__file__))):
        return _guard_facts_local()
    return _guard_facts_from_file(os.path.join(pkg, "guard.py"))


# --------------------------------------------------------------------------- shims

_PY_BOOT = (
    "import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); "
    'runpy.run_module("{module}", run_name="__main__", alter_sys=True)'
)

_JSON_U_ESCAPE = "*'\\u'*"  # shell: any payload holding a backslash and a u (a JSON \uXXXX escape)


def _ci_glob(word: str) -> str:
    """A shell case pattern that finds `word` in any ASCII letter case, using bracket pairs only.  It needs no
    external tool (a `tr` that is missing from a hook's PATH would read as "nothing found").  The scripts run it
    under LC_ALL=C (_C_LOCALE_LINES), so it compares bytes whatever locale the caller has."""
    return "*" + "".join(f"[{c}{c.upper()}]" if c.isalpha() else c for c in word) + "*"


# Both generated scripts match the raw payload BYTES, so they set LC_ALL=C before reading it.  In a multibyte locale
# such as ja_JP.SJIS or zh_CN.GB18030 an ASCII byte can be the second byte of a character, and bash and zsh (not
# dash) then read the last byte of a UTF-8 character and the next letter as one character: a trigger word glued to
# a non-ASCII character would never match.  The caller's value is put back, exactly as it was, before the guard runs.
_C_LOCALE_LINES = [
    "# Match bytes, not characters: in a multibyte locale an ASCII letter can be the second byte of a character.",
    "_lg_lc=${LC_ALL-}",
    "_lg_lcset=${LC_ALL+x}",
    "LC_ALL=C",
]
_RESTORE_LOCALE = 'if [ -n "$_lg_lcset" ]; then LC_ALL=$_lg_lc; else unset LC_ALL; fi'


@functools.lru_cache(maxsize=16)
def _lower_lookalikes(letters: str) -> tuple:
    """The non-ASCII characters whose str.lower() holds one of `letters`.  guard._extract lower()s the tool name
    before it looks for a shell word, and U+0130 lowers to i plus a combining dot (U+212A, the Kelvin sign, lowers to
    k), so such a character could complete a shell word without its ASCII spelling.  Basic Multilingual Plane only,
    like _fold_lookalikes; the tests scan every code point."""
    want = set(letters)
    return tuple(
        ch
        for ch in map(chr, range(0x80, 0x10000))
        if not 0xD800 <= ord(ch) < 0xE000 and want.intersection(ch.lower())
    )


def render_hook_shim(
    facts: GuardFacts | None = None,
    *,
    module: str | None = None,
    purpose: Sequence[str] | None = None,
    extra_words: Sequence[str] = (),
    extra_globs: Sequence[str] = (),
) -> str:
    """lane-guard-hook.  Relocatable: it derives its directory from $0, so a staged copy runs itself.
    `module` and `purpose` make the same script for another hook module (fleet-guard-hook); with empty
    GuardFacts there is no prefilter.

    The prefilter.  The guard reads a command only when it holds one of a few words (guard._TRIGGER); a payload
    without any of them is allowed without being parsed.  Starting Python costs about 0.2 s on a loaded machine
    and this hook runs on every Bash call on five platforms, so the shell makes the same call first.  The match
    is on the RAW payload BYTES (LC_ALL=C while it runs, the caller's LC_ALL back before Python) and is a superset
    of the guard's: every word in any ASCII letter case, a JSON \\u escape (it can spell any letter), and the few
    non-ASCII letters that re.IGNORECASE folds onto them.  A payload over the guard's own size cap skips the match.
    Whatever is not provably free of a trigger goes to Python as the original bytes, and the guard decides exactly
    as it would have without the shell.

    `extra_words` (matched in any ASCII letter case) and `extra_globs` (case patterns, used as given) widen the match
    for a hook that runs a second guard (fleet-guard-hook adds the secret guard's).  They change nothing when the
    lane guard's own words are unknown: `facts.words` empty means no prefilter at all."""
    facts = read_guard_facts() if facts is None else facts
    boot = _PY_BOOT.format(module=module or f"{PACKAGE}.lane_guard_hook")
    out = [
        "#!/bin/sh",
        f"# {GENERATED_MARK}.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.",
    ] + list(
        purpose
        or (
            "# Runs the temp-checkout guard from THIS directory's copy of fleet_lanes, never from the caller's",
            "# working directory (-I).  The guard allows on any internal error, so `install_tools verify`",
            "# is the proof that it denies.  Arguments (--format FMT) pass through.",
        )
    )
    if facts.words:
        out += [
            "# Prefilter: a payload with none of the guard's trigger words (copied from guard.py when this file was",
            "# generated) cannot be denied, so it exits 0 here without starting Python.  The match is on the raw",
            "# payload, in any letter case, and also passes a JSON \\u escape and the non-ASCII letters that fold",
            "# onto a trigger letter.  Everything else reaches Python as the original bytes.",
        ]
    out += [
        "_lg_py() {",
        "  # the caller's LC_ALL, exactly as it was (set, empty or unset), for Python",
        '  if [ -n "${_lg_lcset-}" ]; then LC_ALL=$_lg_lc; else unset LC_ALL; fi',
        "  unset _lg_in _lg_lc _lg_lcset",
        '  case "$0" in',
        "    */*) D=${0%/*} ;;",
        "    *) D=. ;;",
        "  esac",
        '  D=$(cd "$D" && pwd -P) || exit 70',
        "  PYTHONPATH=$D",
        "  FLEET_APPS_JSON=$D/fleet-apps.json",
        "  export PYTHONPATH FLEET_APPS_JSON",
        f'  exec /usr/bin/env python3 -I -c \'{boot}\' "$D" "$@"',
        "}",
    ] + _C_LOCALE_LINES
    if not facts.words:
        out.append('_lg_py "$@"')
        return "\n".join(out) + "\n"
    out += ["if _lg_in=$(cat && printf x); then", "  _lg_in=${_lg_in%x}"]
    indent = "  "
    if facts.max_chars:
        out.append(f'  if [ "${{#_lg_in}}" -le {facts.max_chars} ]; then')
        indent = "    "
    out.append(f"{indent}case $_lg_in in")
    for word in tuple(facts.words) + tuple(w for w in extra_words if w not in facts.words):
        out.append(f"{indent}  {_ci_glob(word)}) ;;")
    for glob in extra_globs:
        out.append(f"{indent}  {glob}) ;;")
    out.append(f"{indent}  {_JSON_U_ESCAPE}) ;;")
    for ch in facts.lookalikes:
        out.append(f"{indent}  *{ch}*) ;;")
    out += [f"{indent}  *) exit 0 ;;", f"{indent}esac"]
    if facts.max_chars:
        out.append("  fi")
    out += ['  printf \'%s\' "$_lg_in" | _lg_py "$@"', "else", '  _lg_py "$@"', "fi"]
    return "\n".join(out) + "\n"


FLEET_SHIM_PURPOSE = (
    "# Runs the lane guard and then the secret guard from THIS directory's copy of fleet_lanes, never from the",
    "# caller's working directory (-I).  The first deny wins.  Both allow on any internal error, so `install_tools",
    "# verify` is the proof that it denies.  The prefilter below is the lane guard's trigger words plus the words",
    "# without which no secret-guard rule can fire.",
    "# Arguments (--format FMT, --exit2) pass through.",
)

# The words without which no rule of the secret guard (scripts/hooks/secret-guard-pretooluse.py) can fire, so a shell
# call with none of them (and no `ps` followed by white space, and no JSON \u escape) cannot be denied by it.  Matched
# in any ASCII letter case, which is a superset of the guard's case-sensitive patterns.  Rule A (a dump of a secret
# file) and rule D (stderr merged into one) need a secret path:  .secrets, credentials, client_info, .env, id_rsa,
# .pem, .p12, .key.  Rule B needs Bearer or Authorization.  Rule C2 needs pgrep.  Rule E needs a variable whose name
# holds KEY, TOKEN, SECRET, PASSWORD, PASSWD, DSN (APIKEY and API_KEY hold KEY).  Rule C is `ps` followed by white space:
# SECRET_PS_GLOBS.  The tests check these against the guard's own tables and run every rule through the shim.
SECRET_WORDS = (
    "bearer",
    "authorization",
    "pgrep",
    ".secrets",
    "credentials",
    "client_info",
    ".env",
    "id_rsa",
    ".pem",
    ".p12",
    "key",
    "token",
    "secret",
    "passw",
    "dsn",
)


def _secret_ps_globs() -> tuple:
    """`ps` followed by white space, as Python's \\s reads it:  an ASCII space, a backslash (a JSON \\t, \\n, \\r, \\f or
    \\v escape; a \\u escape is caught by the general rule), or a non-ASCII white-space character."""
    space = re.compile(r"\s")
    wide = [
        chr(c) for c in range(0x80, 0x10000) if not 0xD800 <= c < 0xE000 and space.fullmatch(chr(c))
    ]
    return ("*[pP][sS]' '*", "*[pP][sS]'\\'*") + tuple(f"*[pP][sS]{ch}*" for ch in wide)


SECRET_PS_GLOBS = _secret_ps_globs()


def render_fleet_hook_shim(facts: GuardFacts | None = None) -> str:
    """fleet-guard-hook: the same script as lane-guard-hook, for fleet_lanes.fleet_guard_hook.  Its prefilter is the
    lane guard's (`facts`, read from the guard) widened by the secret guard's words (SECRET_WORDS, SECRET_PS_GLOBS), so
    an ordinary shell call exits 0 in `sh` without starting Python, and every call either guard could deny reaches it.
    With no lane facts there is no prefilter at all."""
    facts = read_guard_facts() if facts is None else facts
    return render_hook_shim(
        GuardFacts(facts.words, facts.lookalikes, facts.max_chars, (), facts.problems),
        module=f"{PACKAGE}.fleet_guard_hook",
        purpose=FLEET_SHIM_PURPOSE,
        extra_words=SECRET_WORDS,
        extra_globs=SECRET_PS_GLOBS,
    )


def render_clutch_hooks(stable: str) -> str:
    """The fleet-only hooks.json Clutch's @deepseek-ai/dsh-hooks-claude-code bridge reads.  The matcher is the engine's
    shell tool, `bash`; the command is the fleet hook in the Claude contract (hookSpecificOutput deny), which the
    bridge maps to a model-visible refusal.  Absolute paths only."""
    doc = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{shlex.quote(os.path.join(stable, FLEET_HOOK_NAME))} --format claude",
                            "timeout": HOOK_TIMEOUT_S,
                        }
                    ],
                }
            ]
        }
    }
    return json.dumps(doc, indent=2) + "\n"


def render_lane_shim(stable: str) -> str:
    boot = _PY_BOOT.format(module=f"{PACKAGE}.{LANE_NAME}")
    return (
        "#!/bin/sh\n"
        f"# {GENERATED_MARK}.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.\n"
        "# Same as `python3 -m fleet_lanes.lane`, but from the stable copy and not from the working directory.\n"
        f"D={shlex.quote(stable)}\n"
        "PYTHONPATH=$D\n"
        "FLEET_APPS_JSON=$D/fleet-apps.json\n"
        "export PYTHONPATH FLEET_APPS_JSON\n"
        f'exec /usr/bin/env python3 -I -c \'{boot}\' "$D" "$@"\n'
    )


# --------------------------------------------------------------------------- Muse Code: plugin bundle and seat shim


def render_muse_manifest(stable: str) -> str:
    """.muse-plugin/plugin.json.  Muse rejects a `matcher` on a plugin hook, so there is none; the command is an
    argv array with absolute paths only, because Muse runs hooks outside its sandbox with a cleared environment
    (and `muse plugins install` copies the bundle into its own cache, where a relative path would not find the
    stable dir).  Shape taken from a bundle that `muse plugins validate` accepted."""
    doc = {
        "schemaVersion": 1,
        "name": MUSE_PLUGIN_ID,
        "displayName": "Fleet Guards",
        "version": "1.1.0",
        "description": "Denies fleet-repo checkouts in temp directories (the Lane Map temp guard) and the commands the "
        "secret guard blocks (ps, dumps of secret files).",
        "compat": {"source": "native", "manifestDir": ".muse-plugin"},
        "capabilities": {
            "skills": [],
            "commands": [],
            "hooks": [
                {
                    "id": "lane-guard",
                    "event": "PreToolUse",
                    "command": ["/bin/sh", os.path.join(stable, *MUSE_WRAPPER.split("/"))],
                    "timeoutMs": MUSE_HOOK_TIMEOUT_MS,
                    "statusMessage": "Checking fleet guards",
                }
            ],
            "mcpServers": [],
            "reminders": [],
        },
    }
    return json.dumps(doc, indent=2) + "\n"


def muse_hook_argv(stable: str) -> list:
    """The hook command Muse is told to run (what the manifest holds)."""
    return ["/bin/sh", os.path.join(stable, *MUSE_WRAPPER.split("/"))]


MUSE_KEY_WINDOW = (
    4096  # the Muse wrapper reads a tool key only when it starts in the payload's first 4096 bytes
)


def _muse_key_function(key: str, facts: GuardFacts, absent: str | None) -> list:
    """Shell function _lg_plain_<key>: status 0 only when the payload is PROVABLY a call that guard._extract does
    not treat as a shell call because of this key.  `absent`: a key that must not occur at all (the guard reads
    toolName only when there is no top-level tool_name).  The proof, all on raw bytes:

      * the key occurs exactly once, so there is no duplicate whose value Python's json would keep instead;
      * right after it comes `:"` or `: "`, so its value is a string (a null, a number, an object, or odd spacing
        around the colon is passed on);
      * no shell word, in any ASCII letter case, and no non-ASCII letter that lower() turns into a letter of one,
        appears anywhere after it (a superset of "in its value");
      * it starts in the first MUSE_KEY_WINDOW bytes, and between the first `{` and it there is no `{`, `}`, `[`,
        `]` or backslash.  Then no string before it can hide a quote, nothing is nested, and it is a key of the
        top-level object (in valid JSON the first `{` opens the top-level object; if the top level is not an
        object the guard does not see a shell call either, and the guard allows input that is not JSON at all).

    Only `case` globs scan the whole payload, because they stay linear in every shell.  The one expansion that
    looks for the key (`%%`) runs only when the window test passed, since bash takes seconds to run it to a key
    far into a 256 KB payload."""
    k = f"'\"{key}\"'"
    out = [f"_lg_plain_{key}() {{"]
    if absent:
        out.append(f"  case $_lg_in in *'\"{absent}\"'*) return 1 ;; esac")
    out += [
        "  case $_lg_in in",
        f"    *{k}*{k}*) return 1 ;;",
        f"    *{k}':\"'*|*{k}': \"'*) ;;",
        "    *) return 1 ;;",
        "  esac",
        "  case $_lg_in in",
        f"    $_lg_far*{k}*) return 1 ;;",
    ]
    for hint in facts.shell_hints:
        out.append(f"    *{k}{_ci_glob(hint)}) return 1 ;;")
    for ch in _lower_lookalikes(
        "".join(sorted({c for h in facts.shell_hints for c in h if c.isalpha()}))
    ):
        out.append(f"    *{k}*{ch}*) return 1 ;;")
    out += [
        "  esac",
        f"  _lg_t=${{_lg_in%%{k}*}}",
        "  case $_lg_t in *'{'*) ;; *) return 1 ;; esac",
        "  case ${_lg_t#*'{'} in *'{'*|*'}'*|*'['*|*']'*|*'\\'*) return 1 ;; esac",
        "  return 0",
        "}",
    ]
    return out


def render_muse_wrapper(stable: str, facts: GuardFacts | None = None) -> str:
    """hooks/lane-guard.sh.  A Muse plugin hook cannot carry a matcher, so Muse runs this on EVERY tool call.  It
    reads the payload and exits 0 at once only when it can PROVE from the raw bytes that guard._extract will not
    treat it as a shell call: the top-level tool key the guard reads (tool_name, or toolName when there is no
    tool_name) holds a plain string with none of the guard's shell tool words in it (see _muse_key_function).
    Everything else, a missing or null tool key, a key that only occurs nested or as a value, duplicate keys, odd
    spacing, a JSON \\u escape, goes to the stable fleet-guard-hook with --format muse as the original bytes (the
    Claude deny shape; an allow is empty stdout and exit 0), so the guards decide exactly as they would without the
    wrapper.  The match is on bytes under LC_ALL=C, and the guard gets the caller's LC_ALL back."""
    facts = read_guard_facts() if facts is None else facts
    out = [
        "#!/bin/sh",
        f"# {GENERATED_MARK}.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.",
        "# Muse Code plugin hook for the fleet guards (the temp-checkout guard and the secret guard, in",
        "# fleet-guard-hook).  Muse refuses a matcher on a plugin hook, so this runs",
        "# on EVERY tool call and must be cheap: a call that is provably not a shell call exits 0 here, before Python.",
        "# Muse starts hooks outside its sandbox with a cleared environment, so the path below is absolute and nothing",
        "# here relies on PATH or HOME.  Like every hook of this guard it allows when anything is wrong (exit 0,",
        "# no output); `install_tools verify muse` is the proof that it denies.",
        f"_lg_guard={shlex.quote(os.path.join(stable, FLEET_HOOK_NAME))}",
    ] + _C_LOCALE_LINES
    if facts.shell_hints:
        window, doublings = 16, 0
        while window < MUSE_KEY_WINDOW:
            window, doublings = window * 2, doublings + 1
        out += [
            "# Exit 0 early only when the tool key the guard reads is provably a plain string with no shell word in it:",
            "# exactly once, a string value, nothing nested or escaped before it, in the first "
            f"{MUSE_KEY_WINDOW} bytes.  Anything",
            "# else (no key, null, nested, duplicated, odd spacing) is the guard's to decide.",
            "_lg_far=" + "?" * 16,
            f"for _lg_i in {' '.join(str(i) for i in range(1, doublings + 1))}; do _lg_far=$_lg_far$_lg_far; done",
        ]
        out += _muse_key_function("tool_name", facts, None)
        out += _muse_key_function("toolName", facts, "tool_name")
    out += [
        "if _lg_in=$(cat && printf x); then",
        "  _lg_in=${_lg_in%x}",
    ]
    if facts.shell_hints:
        out += [
            "  case $_lg_in in",
            f"    {_JSON_U_ESCAPE}) ;;",
            "    *) if _lg_plain_tool_name || _lg_plain_toolName; then exit 0; fi ;;",
            "  esac",
        ]
    out += [
        f"  {_RESTORE_LOCALE}",
        '  [ -x "$_lg_guard" ] || exit 0',
        '  printf \'%s\' "$_lg_in" | "$_lg_guard" --format muse',
        "else",
        f"  {_RESTORE_LOCALE}",
        '  [ -x "$_lg_guard" ] && "$_lg_guard" --format muse',
        "fi",
        "exit 0",
    ]
    return "\n".join(out) + "\n"


MUSE_SEAT_ACTIVE = (
    "_MUSE_SEAT_ACTIVE"  # exported by muse-seat; set means "muse-seat already ran in this chain"
)


def render_muse_seat() -> str:
    """muse-seat: start Muse Code as seat MC.  Sets AGENT_SEAT and AGENT_TAG, then execs the real `muse` that
    `command -v muse` finds.  It refuses (exit 127) when there is none, when the one it finds is this very script (a
    symlink named muse earlier on PATH, caught by -ef), and when it already ran in this chain of processes: a
    wrapper SCRIPT named muse that starts muse-seat again is a different file, so -ef cannot see it, and without the
    exported _MUSE_SEAT_ACTIVE it would exec itself forever.  The cost of that guard: inside a session muse-seat
    started, muse-seat refuses to start again; AGENT_SEAT is already MC there, so `muse` itself is the way."""
    return (
        "#!/bin/sh\n"
        f"# {GENERATED_MARK}.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.\n"
        f"# Starts Muse Code as the {MUSE_SEAT_TAG} seat: sets AGENT_SEAT and AGENT_TAG, then runs the real `muse`.\n"
        "# Arguments pass through.  The seat is set here, never guessed from a folder or a branch.\n"
        f'if [ -n "${{{MUSE_SEAT_ACTIVE}-}}" ]; then\n'
        "  echo 'muse-seat: muse-seat is already running in this chain of processes.  Either the muse found on PATH "
        "is a script that starts muse-seat again (put the real muse first in PATH), or this is a session muse-seat "
        f"started, where AGENT_SEAT is already {MUSE_SEAT_TAG}: run muse itself.' >&2\n"
        "  exit 127\n"
        "fi\n"
        f"{MUSE_SEAT_ACTIVE}=1\n"
        f"export {MUSE_SEAT_ACTIVE}\n"
        f"AGENT_SEAT={MUSE_SEAT_TAG}\n"
        f"AGENT_TAG={MUSE_SEAT_TAG}\n"
        "export AGENT_SEAT AGENT_TAG\n"
        "case $0 in\n"
        "  */*) _ms_self=$0 ;;\n"
        '  *) _ms_self=$(command -v "$0") || _ms_self=$0 ;;\n'
        "esac\n"
        "_ms_real=$(command -v muse) || _ms_real=\n"
        'if [ -z "$_ms_real" ] || [ ! -f "$_ms_real" ] || [ ! -x "$_ms_real" ]; then\n'
        "  echo 'muse-seat: cannot find the real muse on PATH; install Muse Code or add it to PATH.' >&2\n"
        "  exit 127\n"
        "fi\n"
        'if [ "$_ms_real" -ef "$_ms_self" ]; then\n'
        "  printf 'muse-seat: the muse found on PATH (%s) is this wrapper itself; put the real muse first "
        'in PATH, or call it by its full path.\\n\' "$_ms_real" >&2\n'
        "  exit 127\n"
        "fi\n"
        'exec "$_ms_real" "$@"\n'
    )


def render_opencode_plugin() -> str:
    """opencode-plugin/fleet-lane-guard.ts in the stable dir.  OpenCode has no config hooks key; it loads a local
    plugin listed by absolute path in opencode.json's plugin array.  The plugin inspects tool.execute.before (keyed
    on the presence of a command in the tool args, because OpenCode's shell tool name is not confirmed) and denies
    through permission.ask, the one explicit deny channel in the plugin SDK.  It runs the stable fleet-guard-hook
    with a Claude-shaped payload and reads the Claude deny shape back.  The guard path is derived from the plugin's
    own file, so the text holds no home path and no credential, and this function takes no home to embed: the same
    text is right in every stable dir.  A permission prompt the plugin cannot rule on is a denial,  not a
    throw:  OpenCode's default handling of a throw is to allow.  Whether permission.ask is reached for a
    call that tool.execute.before judged, and whether a throw from tool.execute.before denies, are
    UNVERIFIED: permission.ask also judges whatever command text its own input carries,  and the throw
    is only a second belt."""
    return """// Generated by fleet_lanes.install_tools.  Do not edit; run `python3 -m fleet_lanes.install_tools apply tools`.
// OpenCode guard plugin: denies fleet-repo checkouts in temp directories and the commands the secret guard blocks.
// It inspects tool.execute.before and denies through permission.ask, the explicit deny channel.  Both are
// UNVERIFIED live: no OpenCode session on this Mac has yet shown the deny reaching the tool.
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const guardCommand = join(dirname(fileURLToPath(import.meta.url)), "..", "fleet-guard-hook");
const deniedCalls = new Map<string, string>();

type AnyRecord = Record<string, unknown>;

// Tool-call data and permission prompts are untrusted and walked recursively, so the walk
// carries bounds:  the depth, width and cycle ceilings apply to every payload, and the byte
// ceiling applies to the extracted command and to the whole permission prompt.  Anything
// over a ceiling is refused before it can exhaust the stack or the CPU ahead of the guard.
const MAX_COMMAND_INPUT_BYTES = 64 * 1024;
const MAX_COMMAND_INPUT_DEPTH = 16;
const MAX_COMMAND_INPUT_NODES = 1000;

// checkInputStructure refuses an untrusted document that is too deep, too wide or cyclic:
// the walk counts the nodes, measures the depth and finds a cycle, and it runs before any
// field of the document is read.
function checkInputStructure(value: unknown, label: string): void {
  const seen = new WeakSet<object>();
  let nodes = 0;
  const walk = (current: unknown, depth: number): void => {
    if (depth > MAX_COMMAND_INPUT_DEPTH) {
      throw new TypeError(`${label} exceeds the maximum depth`);
    }
    if (++nodes > MAX_COMMAND_INPUT_NODES) {
      throw new TypeError(`${label} exceeds the maximum element count`);
    }
    if (!current || typeof current !== "object") return;
    if (seen.has(current)) {
      throw new TypeError(`${label} contains a cycle`);
    }
    seen.add(current);
    if (Array.isArray(current)) {
      for (const item of current) walk(item, depth + 1);
    } else {
      const record = current as AnyRecord;
      for (const key of Object.keys(record)) walk(record[key], depth + 1);
    }
    seen.delete(current);
  };
  walk(value, 0);
}

// checkInputBounds adds the byte ceiling on the whole payload for callers whose input is a
// bounded document (the permission prompt).  Every walk of untrusted input goes through one
// of the two, so a second caller cannot forget the bounds.
function checkInputBounds(value: unknown, label: string): void {
  checkInputStructure(value, label);
  let serialized: string | undefined;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw new TypeError(`${label} is not serializable`);
  }
  if (serialized !== undefined && Buffer.byteLength(serialized, "utf8") > MAX_COMMAND_INPUT_BYTES) {
    throw new TypeError(`${label} exceeds 64 KiB`);
  }
}

function commandFrom(value: unknown): string | null {
  // Structural bounds guard the walk below;  the byte ceiling is applied to the extracted
  // command, not the whole payload.  The hook keys on the presence of a command rather
  // than a tool name because OpenCode's shell tool name is not confirmed (v2 research
  // recorded `shell`, current builds `bash`), and a file write or edit can legitimately
  // carry more than 64 KiB of content:  a whole-payload ceiling would deny a call the
  // guard never ruled on (sentry #757).
  checkInputStructure(value, "OpenCode tool input");
  const walk = (current: unknown): string | null => {
    if (typeof current === "string") return current;
    if (Array.isArray(current)) {
      return current.every((v) => typeof v === "string") ? current.join(" ") : null;
    }
    if (!current || typeof current !== "object") return null;
    const record = current as AnyRecord;
    for (const key of ["command", "cmd", "CommandLine", "commandLine", "script"]) {
      const found = walk(record[key]);
      if (found) return found;
    }
    for (const key of ["tool_input", "toolInput", "input", "args"]) {
      const found = walk(record[key]);
      if (found) return found;
    }
    return null;
  };
  const command = walk(value);
  if (command !== null && Buffer.byteLength(command, "utf8") > MAX_COMMAND_INPUT_BYTES) {
    throw new TypeError("OpenCode command input exceeds 64 KiB");
  }
  return command;
}

function cwdFrom(value: unknown, fallback: string): string {
  if (!value || typeof value !== "object") return fallback;
  const record = value as AnyRecord;
  for (const key of ["workdir", "cwd", "Cwd", "working_directory", "directory"]) {
    if (typeof record[key] === "string" && record[key]) return record[key] as string;
  }
  return fallback;
}

// The guard is the authorization source, so a guard that cannot answer is a denial, not an
// allow:  a timeout, a guard that will not start, and output we cannot read all reject, so a
// hung or missing fleet-guard-hook never reads as authorization.  Nothing here resolves null
// except a guard that answered and did not deny.
const GUARD_TIMEOUT_MS = 5000;

// A child that ignores SIGTERM must not outlive the runner:  after the grace period the
// child is SIGKILLed,  and the runner waits for its close so the child is reaped on the
// interruption paths as well as on the timeout path.
const GUARD_KILL_GRACE_MS = 500;

class GuardFailure extends Error {
  readonly exitCode: number;
  constructor(message: string, exitCode: number) {
    super(message);
    this.name = "GuardFailure";
    this.exitCode = exitCode;
  }
}

function runGuard(command: string, cwd: string): Promise<string | null> {
  return new Promise((resolve, reject) => {
    let terminateChild = () => {};
    try {
      const child = spawn(guardCommand, ["--format", "claude"], { stdio: ["pipe", "pipe", "ignore"] });
      let out = "";
      let settled = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      let killTimer: ReturnType<typeof setTimeout> | undefined;
      // `child.killed` only says a signal was SENT,  not that the child died,  so liveness
      //  is read off the exit state:  a child still running has neither code nor signal.
      const alive = () => child.exitCode === null && child.signalCode === null;
      const cleanup = () => {
        if (timer !== undefined) clearTimeout(timer);
        process.off("exit", onParentExit);
        process.off("SIGINT", onParentSignal as () => void);
        process.off("SIGTERM", onParentSignal as () => void);
      };
      // SIGTERM first,  then SIGKILL if the child is still there after the grace period,
      //  so a guard that ignores SIGTERM cannot outlive the runner.
      const terminate = () => {
        if (alive()) child.kill("SIGTERM");
        if (killTimer === undefined) {
          killTimer = setTimeout(() => {
            killTimer = undefined;
            if (alive()) child.kill("SIGKILL");
          }, GUARD_KILL_GRACE_MS);
        }
      };
      terminateChild = terminate;
      const onParentExit = () => terminate();
      // An interruption is not a reason to leave the guard running:  terminate the child
      //  first,  drop our own handlers so the signal takes its normal course,  then re-raise.
      const onParentSignal = (signal: NodeJS.Signals) => {
        terminate();
        cleanup();
        process.kill(process.pid, signal);
      };
      const fail = (error: GuardFailure) => {
        if (settled) return;
        settled = true;
        terminate();
        cleanup();
        reject(error);
      };
      const succeed = (reason: string | null) => {
        if (settled) return;
        settled = true;
        if (killTimer !== undefined) clearTimeout(killTimer);
        killTimer = undefined;
        cleanup();
        resolve(reason);
      };
      process.once("exit", onParentExit);
      process.once("SIGINT", onParentSignal as () => void);
      process.once("SIGTERM", onParentSignal as () => void);
      timer = setTimeout(() => {
        fail(new GuardFailure(`fleet guard at ${guardCommand} timed out after ${GUARD_TIMEOUT_MS}ms`, 124));
      }, GUARD_TIMEOUT_MS);
      child.on("error", (error: Error) => {
        const code = (error as NodeJS.ErrnoException).code === "ENOENT" ? 127 : 1;
        fail(new GuardFailure(`fleet guard at ${guardCommand} failed to start: ${error.message}`, code));
      });
      child.stdout.on("data", (chunk: Buffer) => {
        out += chunk.toString();
      });
      child.stdin.on("error", (error: Error) => {
        fail(new GuardFailure(`fleet guard at ${guardCommand} input failed: ${error.message}`, 1));
      });
      child.on("close", () => {
        if (settled) return;
        try {
          const body = JSON.parse(out) as AnyRecord;
          if (!body || typeof body !== "object" || Array.isArray(body)) {
            throw new Error("response is not an object");
          }
          const hook = body.hookSpecificOutput as AnyRecord | undefined;
          const reason = hook?.permissionDecisionReason;
          succeed(
            hook?.permissionDecision === "deny" && typeof reason === "string" ? reason : null,
          );
        } catch (error) {
          fail(
            new GuardFailure(
              `fleet guard at ${guardCommand} returned invalid output: ${
                error instanceof Error ? error.message : String(error)
              }`,
              1,
            ),
          );
        }
      });
      child.stdin.end(JSON.stringify({ tool_name: "Bash", tool_input: { command }, cwd }));
    } catch (error) {
      terminateChild();
      reject(
        new GuardFailure(
          `fleet guard at ${guardCommand} failed to start: ${
            error instanceof Error ? error.message : String(error)
          }`,
          127,
        ),
      );
    }
  });
}

// Only command-shaped values are probed.  Free text carried in permission metadata (a title,
// a path, an ID) is not a shell command, and sending it to the guard both risks denying a
// call that is not a fleet clone and costs one child process per string.
const MAX_GUARD_CANDIDATES = 8;

// A permission pattern is a glob that names WHAT may be called,  and a title is prose.
// Neither is a command,  so neither is probed.  A value is a command candidate when it has
// something a shell would run in it:  a space,  a slash,  or a shell metacharacter.  A bare
// word is not probed,  because no guard rule can match on one and the spawn is pure cost.
const COMMAND_SHAPE = " /;&|<>`$(){}[]'";

function isCommandShaped(value: string): boolean {
  // A space, a tab, a path separator or a shell metacharacter all mean the value
  // could be a command.  A tab is tested by its character code because the class
  // cannot be spelled with a backslash here.
  if (value.indexOf("\t") >= 0) return true;
  for (const ch of COMMAND_SHAPE) {
    if (value.indexOf(ch) >= 0) return true;
  }
  return false;
}

// The shape checks the plugin API documents,  enforced at runtime because a TypeScript
// annotation is not one.  A hookInput that is not an object,  a pattern that is neither a
// string nor an array of strings,  or a title that is not a string,  is a malformed
// permission prompt:  the plugin cannot rule on it,  so it must not be read as if it were
// well formed.
function permissionInput(hookInput: unknown): AnyRecord {
  if (!hookInput || typeof hookInput !== "object" || Array.isArray(hookInput)) {
    throw new TypeError("the permission hookInput must be an object");
  }
  const hook = hookInput as AnyRecord;
  const pattern = hook.pattern;
  if (pattern !== undefined && pattern !== null) {
    if (typeof pattern === "string") {
      // a string is fine
    } else if (Array.isArray(pattern)) {
      for (const item of pattern) {
        if (typeof item !== "string") {
          throw new TypeError("every entry of a permission pattern array must be a string");
        }
      }
    } else {
      throw new TypeError("a permission pattern must be a string or an array of strings");
    }
  }
  if (hook.title !== undefined && hook.title !== null && typeof hook.title !== "string") {
    throw new TypeError("a permission title must be a string");
  }
  if (hook.title === "") {
    throw new TypeError("a permission title must not be empty");
  }
  // The identifiers are the correlation key between a denial and the call that caused
  // it,  so an identifier that is not a non-empty string is a malformed prompt too:  a
  // null key would let the call through instead of refusing it.
  if (typeof hook.sessionID !== "string" || !hook.sessionID) {
    throw new TypeError("a permission sessionID must be a non-empty string");
  }
  if (hook.callID !== undefined && hook.callID !== null) {
    if (typeof hook.callID !== "string" || !hook.callID) {
      throw new TypeError("a permission callID must be a non-empty string");
    }
  }
  return hook;
}

function guardCandidates(hook: AnyRecord): string[] {
  const candidates: string[] = [];
  const pattern = hook.pattern;
  if (typeof pattern === "string") {
    if (isCommandShaped(pattern)) candidates.push(pattern);
  } else if (Array.isArray(pattern)) {
    for (const item of pattern) {
      if (typeof item === "string" && isCommandShaped(item)) candidates.push(item);
    }
  }
  const metadata = hook.metadata;
  if (metadata && typeof metadata === "object" && !Array.isArray(metadata)) {
    for (const key of ["command", "cmd", "script"]) {
      const value = (metadata as AnyRecord)[key];
      if (typeof value === "string" && isCommandShaped(value)) candidates.push(value);
    }
  }
  return candidates;
}

function denialKey(sessionID: unknown, callID: unknown): string | null {
  if (typeof sessionID !== "string" || !sessionID) return null;
  if (typeof callID !== "string" || !callID) return null;
  return JSON.stringify([sessionID, callID]);
}

export const server = async (input: { directory?: string; worktree?: string }) => {
  const fallbackCwd = input.directory ?? input.worktree ?? "";
  return {
    "tool.execute.before": async (
      hookInput: { tool: string; sessionID: string; callID: string },
      output: { args: unknown },
    ) => {
      let command: string | null;
      try {
        command = commandFrom(output.args);
      } catch {
        // Malformed or oversized tool input is not something the guard can rule on, and it
        // is not something to allow either:  no command means no call proceeds.
        const key = denialKey(hookInput.sessionID, hookInput.callID);
        if (key) deniedCalls.set(key, "OpenCode command input could not be read");
        throw new Error("OpenCode command input could not be read");
      }
      if (!command) return;
      let reason: string | null;
      try {
        reason = await runGuard(command, cwdFrom(output.args, fallbackCwd));
      } catch (error) {
        // The guard could not answer.  Deny: an unavailable guard is not authorization.
        const message = error instanceof Error ? error.message : String(error);
        const key = denialKey(hookInput.sessionID, hookInput.callID);
        if (key) deniedCalls.set(key, message);
        throw new Error(message);
      }
      if (reason) {
        const key = denialKey(hookInput.sessionID, hookInput.callID);
        if (key) deniedCalls.set(key, reason);
        throw new Error(reason);
      }
    },
    "permission.ask": async (
      hookInput: {
        type?: string;
        pattern?: string | string[];
        sessionID: string;
        callID?: string;
        title?: string;
        metadata?: unknown;
      },
      output: { status: "ask" | "deny" | "allow" },
    ) => {
      // A prompt the plugin cannot rule on is a denial,  not an error the caller sees:
      // a malformed,  oversized or cyclic prompt fails closed.  Throwing here would
      // leave the answer to OpenCode's default handling,  which is to allow.
      let hook: AnyRecord;
      try {
        // The bounds are checked before any field is read,  so a pathological payload
        // cannot cost unbounded CPU or memory ahead of the validation.
        checkInputBounds(hookInput, "OpenCode permission input");
        hook = permissionInput(hookInput);
      } catch {
        output.status = "deny";
        return;
      }
      const key = denialKey(hook.sessionID, hook.callID);
      if (key && deniedCalls.has(key)) {
        // Answering a denial consumes it, so a long-lived server does not retain one
        // entry per denied call forever.
        deniedCalls.delete(key);
        output.status = "deny";
        return;
      }
      for (const candidate of guardCandidates(hook).slice(0, MAX_GUARD_CANDIDATES)) {
        let reason: string | null;
        try {
          reason = await runGuard(candidate, fallbackCwd);
        } catch (error) {
          output.status = "deny";
          return;
        }
        if (reason) {
          output.status = "deny";
          return;
        }
      }
    },
  };
};

export const FleetLaneGuardPlugin = server;
export default { id: "fleet-lane-guard", server };
"""


def muse_owner_actions(paths: Paths) -> list:
    """What the OWNER still has to do after `apply tools`.  This tool never runs these commands and never edits
    Muse's config, so it can only say them."""
    return [
        f"muse plugins install {shlex.quote(paths.muse_bundle)} --scope user",
        f"muse plugins approve {MUSE_PLUGIN_ID}",
        "start a new Muse Code session",
    ]


def hook_command(paths: Paths, fmt: str) -> str:
    """The exact string written into a platform config."""
    return f"{shlex.quote(paths.hook_shim)} --format {fmt}"


def render_minimax_hook_shim() -> str:
    """MM owns its adapter.  No shell prefilter: native file paths need Python too."""
    return render_hook_shim(
        GuardFacts(),
        module=f"{PACKAGE}.minimax_guard_hook",
        purpose=("# MiniMax shell and file guards.  No seat is assigned or overwritten here.",),
    )


def fleet_hook_command(paths: Paths, fmt: str) -> str:
    """The fleet hook (lane guard plus secret guard) as a command string."""
    return f"{shlex.quote(paths.fleet_shim)} --format {fmt}"


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
    m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", init_text, re.MULTILINE)
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
    names = sorted(
        n for n in os.listdir(pkg) if n.endswith(".py") and os.path.isfile(os.path.join(pkg, n))
    )
    missing = [m for m in REQUIRED_MODULES if m not in names]
    if missing:
        raise Refused(f"source {PACKAGE}/ lacks required modules: {', '.join(missing)}")
    try:
        reg_bytes = _read_bytes(src.registry)
        reg = json.loads(reg_bytes.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"registry {src.registry} is unreadable: {exc}")
    if (
        not isinstance(reg, dict)
        or not isinstance(reg.get("apps"), list)
        or not isinstance(reg.get("seats"), list)
    ):
        raise Refused(f"registry {src.registry} needs top-level 'apps' and 'seats' lists")
    files: dict[str, bytes] = {f"{PACKAGE}/{n}": _read_bytes(os.path.join(pkg, n)) for n in names}
    files[REGISTRY_NAME] = reg_bytes
    facts = read_guard_facts(src.scripts_dir)
    files[HOOK_NAME] = render_hook_shim(facts).encode("utf-8")
    files[FLEET_HOOK_NAME] = render_fleet_hook_shim(facts).encode("utf-8")
    files[MINIMAX_HOOK_NAME] = render_minimax_hook_shim().encode("utf-8")
    secret_src = os.path.join(src.scripts_dir, *SECRET_GUARD_SOURCE)
    try:
        files[f"{PACKAGE}/{SECRET_GUARD_MODULE}"] = _read_bytes(secret_src)
    except OSError as exc:
        raise Refused(f"source lacks the secret guard {secret_src}: {exc.strerror or exc}")
    files[CLUTCH_HOOKS_FILE] = render_clutch_hooks(paths.stable).encode("utf-8")
    files[MUSE_SEAT_NAME] = render_muse_seat().encode("utf-8")
    files[MUSE_MANIFEST] = render_muse_manifest(paths.stable).encode("utf-8")
    files[MUSE_WRAPPER] = render_muse_wrapper(paths.stable, facts).encode("utf-8")
    files[OPENCODE_PLUGIN_FILE] = render_opencode_plugin().encode("utf-8")
    init_text = files[f"{PACKAGE}/__init__.py"].decode("utf-8", "replace")
    version = (
        f"sha={src.sha}\npackage={_package_version(init_text)}\nfiles={len(files)}\n"
        f"tree={tree_digest(files)}\n"
    )
    files[VERSION_NAME] = version.encode("utf-8")
    return files


def file_mode(rel: str) -> int:
    return (
        0o755
        if rel in (HOOK_NAME, FLEET_HOOK_NAME, MINIMAX_HOOK_NAME, MUSE_SEAT_NAME, MUSE_WRAPPER)
        else 0o644
    )


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
_TOP_FILES = (
    VERSION_NAME,
    REGISTRY_NAME,
    HOOK_NAME,
    FLEET_HOOK_NAME,
    MINIMAX_HOOK_NAME,
    CLUTCH_HOOKS_FILE,
    OPENCODE_PLUGIN_FILE,
) + _MUSE_FILES


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
        if (
            reld not in ("", PACKAGE)
            and not walk
            and not [f for f in filenames if f != ".DS_Store"]
        ):
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
    status: str  # new, changed, same, stale


@dataclass
class ToolsPlan:
    stable: str
    state: str  # missing, current, stale, foreign
    files: list[FileStatus]
    version_text: str
    hook_shim_text: str
    lane_shim_text: str | None  # None when the source has no lane.py
    lane_shim_state: str  # new, changed, same, skipped, stale, foreign
    sha: str
    problems: list[str] = field(default_factory=list)
    lane_detail: str = ""  # for a foreign lane shim: what is at the path instead
    warnings: list[str] = field(
        default_factory=list
    )  # a prefilter or tool filter that could not be generated
    facts: GuardFacts = field(default_factory=GuardFacts)
    seat_shim_text: str = ""
    muse_manifest_text: str = ""
    muse_wrapper_text: str = ""


def _stable_location_problems(src: Source, paths: Paths) -> list[str]:
    """The stable dir is replaced wholesale, so it must not hold anything the run still needs."""
    stable = os.path.realpath(paths.stable)
    out: list[str] = []
    for label, p in (("source", src.scripts_dir), ("registry", src.registry), ("home", paths.home)):
        if _is_within(os.path.realpath(p), stable):
            out.append(
                f"{paths.stable} would hold the {label} ({p}); replacing it would delete the {label}"
            )
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
            problems.append(
                f"{paths.stable} contains a .git entry: it is a checkout, never a lane-tools dir"
            )
        existing, extras = scan_stable(paths.stable)
        if existing or extras:
            why = not_ours(existing)
            if why:
                problems.append(
                    f"{paths.stable} is not empty and is not a lane-tools dir this tool wrote ({why})"
                )
            elif extras and not replace_extra:
                shown = ", ".join(extras[:3]) + (
                    f" and {len(extras) - 3} more" if len(extras) > 3 else ""
                )
                problems.append(
                    f"{paths.stable} holds {len(extras)} entr{'y' if len(extras) == 1 else 'ies'} "
                    f"this tool never writes ({shown}); pass --replace-extra to have them deleted, "
                    "or choose another --stable-dir"
                )
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
        elif (
            os.path.isfile(paths.lane_shim)
            and not os.path.islink(paths.lane_shim)
            and _is_generated(paths.lane_shim)
        ):
            same = _read_bytes(paths.lane_shim) == lane_text.encode("utf-8")
            lane_state = "same" if same and os.access(paths.lane_shim, os.X_OK) else "changed"
        else:
            # Another owner (for example a symlink to a checkout's bin/lane).  Left alone: it must not
            # stop the guard from installing.
            lane_state = "foreign"
            lane_detail = describe_foreign(paths.lane_shim)
    elif os.path.lexists(paths.lane_shim) and _is_generated(paths.lane_shim):
        lane_state = "stale"
    facts = read_guard_facts(src.scripts_dir)
    return ToolsPlan(
        stable=paths.stable,
        state=state,
        files=statuses,
        version_text=files[VERSION_NAME].decode("utf-8"),
        hook_shim_text=files[HOOK_NAME].decode("utf-8"),
        lane_shim_text=lane_text,
        lane_shim_state=lane_state,
        sha=src.sha,
        problems=problems,
        lane_detail=lane_detail,
        warnings=list(facts.problems),
        facts=facts,
        seat_shim_text=files[MUSE_SEAT_NAME].decode("utf-8"),
        muse_manifest_text=files[MUSE_MANIFEST].decode("utf-8"),
        muse_wrapper_text=files[MUSE_WRAPPER].decode("utf-8"),
    )


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
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            start_new_session=True,
        )
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


def run_argv(
    argv: Sequence[str],
    stdin: bytes,
    env: Mapping[str, str],
    timeout: float,
    cwd: str | None = None,
) -> RunResult:
    """`argv` run directly (no shell) with exactly `env`, the way a hook runner that clears the environment starts a
    plugin hook.  The process group is killed on a timeout."""
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        return RunResult(None, b"", b"", error=f"cannot start {argv[0] if argv else '?'}: {exc}")
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
    if shape == "antigravity":  # UNVERIFIED: run_command with tool_input.CommandLine and Cwd
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": "run_command",
            "tool_input": {"CommandLine": command, "Cwd": cwd},
        }
    if shape == "cursor":  # beforeShellExecution: command, cwd and workspace_roots at the top level
        return {
            "hook_event_name": "beforeShellExecution",
            "command": command,
            "cwd": cwd,
            "workspace_roots": [cwd],
        }
    if shape in (
        "muse",
        "dsh",
    ):  # a payload whose shell tool is named "bash", in lower case (Muse Code, the DSH engine)
        return {
            "hook_event_name": "PreToolUse",
            "session_id": "lane-verify",
            "cwd": cwd,
            "permission_mode": "default",
            "tool_name": "bash",
            "tool_input": {"command": command, "description": "lane-guard verify"},
        }
    return {
        "session_id": "lane-verify",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "lane-guard verify"},
        "cwd": cwd,
    }


def check_deny_output(fmt: str, out: bytes, marker: str = DENY_MARKER) -> str | None:
    """None when `out` is the platform's deny for a probe command (DENY_COMMAND by default), else the reason it is not.
    `marker` is the text the deny reason must hold."""
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
    if not isinstance(reason, str) or marker not in reason:
        return (
            "deny reason does not name the probe destination"
            if marker == DENY_MARKER
            else f"deny reason does not name {marker}"
        )
    return None


def check_exit2_output(res: RunResult, marker: str = DENY_MARKER) -> str | None:
    """None when `res` is the exit-2 contract's deny (exit code 2, reason on stderr, nothing on stdout)."""
    if res.returncode != 2:
        return f"exit {res.returncode}, expected 2 (the exit-2 contract)"
    if res.stdout.strip():
        return "stdout is not empty (the exit-2 contract keeps the reason on stderr)"
    if marker not in res.stderr.decode("utf-8", "replace"):
        return f"stderr does not name {marker}"
    return None


def _brief(data: bytes, limit: int = 300) -> str:
    text = repr(data[:limit])
    return text + (f"...(+{len(data) - limit} bytes)" if len(data) > limit else "")


@dataclass
class Check:
    target: str
    name: str
    status: str  # PASS, FAIL, WARN, SKIP
    detail: str = ""

    def line(self) -> str:
        return f"{self.status:<5} {self.target:<12} {self.name:<24} {self.detail}".rstrip()


def _variants(minimal_path: bool) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = [("login-PATH", None)]
    if minimal_path:
        out.append(("minimal-PATH", MINIMAL_PATH))
    return out


def probe_command(
    command: str,
    *,
    target: str,
    fmt: str,
    shape: str,
    home: str,
    timeout: float,
    minimal_path: bool = True,
    secret: bool = False,
) -> list[Check]:
    """Run `command` (a hook command string) against the deny and the allow payload, once per PATH.  With `secret`
    (the fleet hook) a third payload, a `ps`, must be denied too, in the same contract."""
    checks: list[Check] = []
    cases = [("deny", DENY_COMMAND, True, DENY_MARKER), ("allow", ALLOW_COMMAND, False, "")]
    if secret:
        cases.append(("secret", SECRET_DENY_COMMAND, True, SECRET_DENY_MARKER))
    for vname, vpath in _variants(minimal_path):
        env = probe_env(home, vpath)
        for kind, cmd, must_deny, marker in cases:
            res = run_shell(
                command, json.dumps(probe_payload(shape, cmd, home)).encode("utf-8"), env, timeout
            )
            problem: str | None = None
            if res.error:
                problem = res.error
            elif res.timed_out:
                problem = f"timed out after {timeout:g}s"
            elif must_deny and fmt in EXIT2_FORMATS:
                problem = check_exit2_output(res, marker)
            elif res.returncode != 0:
                problem = f"exit {res.returncode}, expected 0"
            elif must_deny:
                problem = check_deny_output(fmt, res.stdout, marker)
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
    fmt: str  # the --format value
    shape: str  # payload shape the probes use
    style: str  # claude (nested groups), cursor (flat list), antigravity (named groups)
    event: str  # the config event that gates a tool call
    matcher: str | None  # tool name the entry must match
    platform_dir: str  # must exist, relative to home
    config_rel: str  # config file, relative to home
    supported: bool = True  # a hook entry in a config file that this tool writes
    kind: str = (
        "config"  # "plugin": no config file is edited (Muse Code takes hooks from a plugin);
    )
    # "block": a marked block in a file we do not own; "bundle": a plugin directory
    # this tool owns outright, outside the stable dir (MiniMax Code)
    in_all: bool = (
        False  # reached by `all` although it is not a config-file platform (kind "bundle")
    )
    new_mode: int = 0o644
    hook_extra: tuple = ()  # extra keys of the hook object, as (key, value) pairs
    notes: tuple = ()
    unverified: tuple = ()


PLATFORMS: dict[str, Platform] = {
    p.key: p
    for p in (
        Platform(
            "claude",
            "Claude Code (CLI and desktop app)",
            "claude",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".claude",
            ".claude/settings.json",
            new_mode=0o600,
            hook_extra=(
                ("timeout", HOOK_TIMEOUT_S),
                ("statusMessage", "Checking checkout location"),
            ),
            notes=(
                "New last group under hooks.PreToolUse, matcher Bash.  Existing groups, order and formatting stay.",
                "Keeps the file's mode (settings.json is 0600 and its env block can hold secrets).",
                "Start a new session after applying: a running session may keep the hooks it started with.",
            ),
            unverified=("Whether a running session reloads an edited settings.json.",),
        ),
        Platform(
            "codex",
            "Codex (CLI and desktop app)",
            "codex",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".codex",
            ".codex/hooks.json",
            hook_extra=(
                ("timeout", HOOK_TIMEOUT_S),
                ("statusMessage", "Checking checkout location"),
            ),
            notes=(
                "Same output format as Claude.  The entry is appended as the LAST group, so the trust Codex "
                "recorded for the existing hooks (config.toml hooks.state, keyed by position) stays valid.",
                "Codex re-trusts hooks by hash after an edit.  Review and trust the new hook with /hooks in "
                "Codex.  This tool never edits config.toml; verify reports whether a trust record exists and "
                "whether [features] hooks is on.",
            ),
            unverified=(
                "That a hook with no trust record is inactive.",
                "How Codex computes trusted_hash.  The record is found by its header only, so it is reported as "
                "`hash UNCHECKED` (a WARN, never a PASS) and --strict always fails for codex.",
                "What Codex does when [features] hooks is absent from config.toml (verify fails only when the key "
                "is present and not true).",
            ),
        ),
        Platform(
            "grok",
            "Grok and Grok Build",
            "grok",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".grok",
            ".grok/hooks/" + GROK_FILE,
            hook_extra=(("timeout", HOOK_TIMEOUT_S),),
            notes=(
                "Its own file, hooks/lane-guard.json.  imported-from-claude.json looks generated and is left alone.",
                "Output is {decision, reason}; do not rely on Grok reading the Claude settings file.",
            ),
            unverified=(
                "Grok also loads ~/.claude/settings.json hooks when compat.claude.hooks is on, so it may "
                "run both entries (harmless: a deny from either wins).",
                "That a hooks/*.json file written by this tool is picked up without a Grok restart.",
            ),
        ),
        Platform(
            "antigravity",
            "Antigravity (agy CLI)",
            "antigravity",
            "antigravity",
            "antigravity",
            "PreToolUse",
            "run_command",
            ".gemini",
            ".gemini/config/hooks.json",
            hook_extra=(("timeout", HOOK_TIMEOUT_S), ("type", "command")),
            notes=(
                "Own named group 'lane-guard' in ~/.gemini/config/hooks.json; the other groups are untouched.",
            ),
            unverified=(
                "Where the matcher key goes in an Antigravity PreToolUse entry (placed flat in the entry; "
                "the hook allows every non-shell tool, so a misplaced matcher is harmless).",
                "That the IDE builds fire PreToolUse at all (CLI documented, IDE reported not to).",
                "The payload field names run_command sends (the guard reads CommandLine and Cwd).",
            ),
        ),
        Platform(
            "cursor",
            "Cursor",
            "cursor",
            "cursor",
            "cursor",
            "beforeShellExecution",
            None,
            ".cursor",
            ".cursor/hooks.json",
            hook_extra=(("timeout", HOOK_TIMEOUT_S), ("failClosed", False)),
            notes=(
                "beforeShellExecution with failClosed false: a broken hook must not block ALL shell use.  The "
                "cost is that a broken install fails open, so only `verify` proves it works.",
                "Worktrees Cursor creates itself never go through a shell hook.",
            ),
            unverified=("That Cursor re-reads hooks.json without a restart.",),
        ),
        Platform(
            "muse",
            "Muse Code (plugin)",
            "muse",
            "muse",
            "claude",
            "PreToolUse",
            None,
            ".config/muse",
            ".config/muse/settings.json",
            supported=False,
            kind="plugin",
            notes=(
                "Muse takes hooks from a PLUGIN, not from settings.json.  `apply tools` writes the plugin bundle "
                f"({MUSE_PLUGIN_DIR}/) into the stable dir with everything else (`apply muse` runs that install); this "
                "tool never edits Muse's config, and the only muse it runs is `muse plugins validate` in the muse "
                "verify, confined to a throwaway HOME with auto-update off.  The owner installs and approves the "
                "plugin (OWNER ACTIONS).",
                "A plugin hook cannot carry a matcher, so Muse runs the wrapper on every tool call; the wrapper exits "
                "0 at once only when it can prove the call is not a shell call, and hands everything else to the "
                "guard.",
                "Deny output is the Claude shape (hookSpecificOutput, permissionDecision deny); an allow is empty "
                "stdout and exit 0.  Hooks start outside Muse's sandbox with a cleared environment, so every path "
                "in the bundle is absolute.",
            ),
            unverified=(
                f"Whether {MUSE_EXPERIMENTAL_ENV}=1 must be set for plugins to load.",
                "Whether a running Muse session picks up a newly approved plugin (start a new session).",
                "Whether `muse plugins install` keeps the absolute hook command (it copies the bundle into its "
                "cache), and whether an edited wrapper in the stable dir needs a re-approval.",
                "That Muse names its only shell tool bash (lower case); the wrapper also accepts the other shell "
                "tool words the guard knows.",
                "What Muse does when the hook times out or crashes (`timeoutMs` is 15000 here): fail-closed would "
                "block shell calls under load, fail-open would switch the guards off exactly then.  The binary's "
                "strings did not settle it.  The wrapper and fleet-guard-hook answer an ordinary call in `sh` "
                "without starting Python, so a timeout is rare.",
            ),
        ),
        Platform(
            "clutch",
            "Clutch (DSH engine, cordis patch)",
            "claude",
            "dsh",
            "claude",
            "PreToolUse",
            "bash",
            ".clutch/dsh",
            CLUTCH_PATCH_REL,
            supported=False,
            kind="block",
            notes=(
                "ONE marked block in the home-level cordis patch, ~/.clutch/dsh/cordis.patch.yml, which applies to every "
                "profile.  It mounts @deepseek-ai/dsh-hooks-claude-code with a fleet-only hooks.json in the stable dir "
                "(matcher `bash`, the engine's shell tool).  It never points the bridge at ~/.claude/settings.json: that "
                "file carries Claude-only hooks, such as the session-start seat hook.",
                "Before it writes, `apply clutch` refuses unless the plugin package resolves under the real "
                "~/.clutch/dsh/profiles/node_modules (the engine repairs a dangling link only when it boots, so restart "
                "clutch-web first), then runs the pinned `dsh --profile P --dump-config` for every profile on a scratch "
                "copy of the profiles: a merge-and-parse check, not a plugin load.  An empty or comment-only patch makes "
                "the engine exit, so the file this tool creates always holds a block, and removing the last block removes "
                "the file.",
                "A profile with `patchReload: live` (clutch-web's `web`) watches this file, so the write reaches the "
                "running engine at once;  a `startup` profile reads it at its next start.  `apply` prints each profile's "
                "mode.",
            ),
            unverified=(
                "That a tool call made through the web UI or the ACP bridge reaches the hook with `command` in "
                "tool_input (the README says the payload is Claude's; the live proof is a `ps` in a new Clutch "
                "session).",
                "That a hook that cannot start is only logged: the bridge README says so.  A hook that exits 0 with "
                "no output allows.",
            ),
        ),
        Platform(
            "kimi",
            "Kimi Code (config.toml hooks block)",
            "kimi",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".kimi-code",
            KIMI_CONFIG_REL,
            supported=False,
            kind="block",
            notes=(
                "ONE marked [[hooks]] block appended to ~/.kimi-code/config.toml.  Kimi Code documents that exit code 2 "
                "blocks a PreToolUse call and that stderr is the reason, so the command is the fleet hook with `--format "
                "kimi` (exit 2, reason on stderr, nothing on stdout).  Any other exit, a crash or a timeout allows "
                "(fail-open).",
                "The block is checked before it is written: the new file must parse as TOML when tomllib exists, and "
                "`kimi doctor` (on PATH, else ~/.kimi-code/bin/kimi) must accept a scratch copy of it (the real config is "
                "never passed to the real home); with no kimi binary the write is refused unless --no-kimi-check.",
                "Kimi Code rewrites its own config.toml without comments, which deletes the markers.  The entry is "
                "therefore found by its content (a [[hooks]] table whose command is the fleet hook), never by the "
                "markers: a current entry that lost them is left as it is, a stale one is replaced where it stands, "
                "duplicates collapse into one, and the hook named in any other form (an inline hooks array) is refused.",
            ),
            unverified=(
                "That Kimi names its shell tool Bash (its hooks page uses Bash in the example, and the matcher is a "
                "regex on the tool name).",
                "That a running session picks up the new block: start a new session.",
            ),
        ),
        Platform(
            "minimax",
            "MiniMax Code (local plugin bundle)",
            "claude",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".minimax",
            MINIMAX_PLUGIN_REL + "/" + MINIMAX_HOOKS_FILE,
            supported=False,
            kind="bundle",
            in_all=True,
            notes=(
                f"MiniMax Code reads no settings.json for hooks.  It loads them from a LOCAL PLUGIN: this tool writes the "
                f"directory ~/{MINIMAX_PLUGIN_REL}/ (a real directory, copied in, never a symlink: MiniMax skips symlinks) "
                f"with {MINIMAX_MANIFEST_FILE} and {MINIMAX_HOOKS_FILE}, in the Claude-compatible plugin flavour, so the "
                "matchers are the compatible tool names `Bash|Read|Write|Edit|bash|read|write|edit`.  Local plugins are enabled by default: there is no install or "
                "approve step.  Nothing else under ~/.minimax is touched.",
                "The hook is MM-owned minimax-guard-hook (shared shell policy plus file guards), by absolute path into "
                "the stable dir.  Deny is the Claude shape; MiniMax honours it, and a timeout, a crash or invalid output "
                "FAILS OPEN.  `verify` probes policy; native dispatch remains a separate proof step.  MiniMax gives a hook only "
                "a small allowlist of environment variables, so no seat or switch reaches it and none is passed.",
                "It is part of `all` (the owner runs MiniMax Code), and a missing ~/.minimax makes it a skip.  An existing "
                "directory that holds anything but our two files, or is a symlink, is refused and left alone.",
                "No SessionStart seat hook is wired.  MiniMax omits seat variables from hook subprocesses; the adapter never "
                "assigns MM or overwrites a launcher seat.  Other-seat lane protection requires trusted identity.",
            ),
            unverified=(
                "That MiniMax Code fires the hook for its shell tool (verify runs the exact command, not MiniMax): the "
                "live proof is a new MiniMax session asked to run `ps`, which must be refused.",
                "Whether MiniMax runs the command through a shell (the path is written unquoted when it has no special "
                "characters, as it does for the other platforms).",
                "Whether an already-running MiniMax session picks up a newly written plugin: start a new session.",
            ),
        ),
        Platform(
            "copilot",
            "Copilot CLI (fleet-owned hooks file)",
            "claude",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".copilot",
            COPILOT_HOOKS_REL,
            supported=False,
            kind="bundle",
            in_all=True,
            notes=(
                "A NEW fleet-owned file, ~/.copilot/hooks/fleet-lane-guard.json (a documented user-level load path for "
                "*.json under ~/.copilot/hooks).  This tool never edits ~/.copilot/settings.json, which the fleet does "
                "not own, and never touches the existing agent-hook-otlp.json in the same directory.",
                "The file is version 1 with one hooks.PreToolUse group, matcher Bash (the PascalCase event gets Claude "
                "matcher semantics and reports the Claude tool name, which is exactly the payload fleet-guard-hook "
                "--format claude parses), and one type command hook running the fleet hook by absolute path with "
                "timeoutSec 5.  A command preToolUse hook is fail-closed on exit 2, a crash or any non-zero exit, and "
                "fails open only on a timeout.",
                "OWNER-GATED: there is no live preToolUse record on this Mac, so the first install needs Jay to start a "
                "new Copilot session and ask for a process listing, which must be refused.  `verify copilot` proves the "
                "command in the written file denies, and ends with a SKIP for that live proof once Copilot is installed "
                "again;  it never claims it.  "
                "Copilot CLI itself was retired on 2026-10-20 because it was not being used,  so the target has no "
                "config to write until it is installed again, and nothing recreates the directory.  Named explicitly, "
                "the platform is a FAIL (an uninstalled platform reached through `all` is a SKIP):  asking to verify "
                "a platform that is not there is a mistake worth reporting,  not a healthy state.",
            ),
            unverified=(
                "That Copilot CLI fires a PreToolUse hook from ~/.copilot/hooks/fleet-lane-guard.json at all (only a "
                "postToolUse firing, from agent-hook-otlp.json, is recorded on this Mac).",
                "That a running Copilot session picks up the new file: start a new session.",
            ),
        ),
        Platform(
            "opencode",
            "OpenCode (local plugin in the stable dir)",
            "claude",
            "claude",
            "claude",
            "PreToolUse",
            None,
            ".config/opencode",
            OPENCODE_CONFIG_REL,
            supported=False,
            kind="plugin",
            in_all=True,
            new_mode=0o600,
            notes=(
                "OpenCode's config schema has no hooks key.  `apply tools` writes the guard plugin into the stable dir "
                f"({OPENCODE_PLUGIN_FILE}); `apply opencode` lists that file by ABSOLUTE path as a string entry in the "
                "plugin array of ~/.config/opencode/opencode.json (the plugin array is the proven load path; "
                "~/.config/opencode/plugin directory auto-discovery is UNCONFIRMED and is not relied on).  Nothing else "
                "in opencode.json is changed: its other keys are never printed, a backup is owner-only (0600) whatever "
                "the file's mode, and the plugin text holds no credential, so no second copy of any token in that file "
                "is made.",
                "The plugin inspects tool.execute.before, keyed on the presence of a command in the tool args rather "
                "than a tool name (OpenCode's shell tool name is not confirmed), runs the stable fleet-guard-hook with a "
                "Claude-shaped payload, remembers a deny by session and call id, and denies through permission.ask, "
                "the one explicit deny channel in the plugin SDK (output.status deny).  A throw from "
                "tool.execute.before is a second belt only.  A permission prompt the plugin cannot rule on "
                "(malformed, oversized or cyclic) is a deny and not a throw,  because OpenCode's default handling "
                "of a throw is to allow.",
                "OpenCode is the OPENCODE seat (branch prefix opencode/), so this guard is for a real seat.",
            ),
            unverified=(
                "Whether permission.ask is reached for a shell call that tool.execute.before judged, and whether it "
                "sees the command text (its input is a Permission with type, pattern, title and metadata; the plugin "
                "judges whatever command text those carry, and a deny remembered from tool.execute.before).",
                "Whether a throw from tool.execute.before denies or only errors the tool call, which is why "
                "permission.ask is the primary channel and the throw is not relied on.",
                "That OpenCode loads an absolute-path local plugin entry in a live session: the live proof is a new "
                "OpenCode session asked for a process listing, which must be refused.  Only Jay can run that proof.",
            ),
        ),
        Platform(
            "fx",
            "fx (no hook surface: UNSUPPORTED)",
            "claude",
            "claude",
            "claude",
            "PreToolUse",
            "Bash",
            ".fx",
            ".fx/settings.json",
            supported=False,
            kind="config",
            in_all=True,
            notes=(
                "Compensating control (prepared, NOT applied): merge the JSON that `plan fx` prints into "
                "~/.fx/settings.json by hand, after Jay approves it.  It moves permission_mode off yolo to ask and "
                "adds a static permission.bash deny list over the provable temp-destination shapes (a clone, a gh "
                "clone, a worktree add or an archive extraction into /tmp, /private/tmp, /var/tmp or "
                "/private/var/tmp), with a top-level `*` rule that asks for everything else.  This tool never "
                "writes ~/.fx/settings.json.",
                "The live ~/.fx/settings.json has permission_mode yolo with no permission block (key names only were "
                "looked at; no value was printed).",
            ),
            unverified=(
                "The exact wildcard matching fx applies to a permission.bash pattern (the shape is taken from the "
                "published permissions documentation example).",
                "That the compensating control, once Jay applies it, is ever exercised: fx stays RED until then.",
            ),
        ),
    )
}
# What the owner still has to do after a platform was written.  Printed by `apply`.
NEXT_STEPS: dict[str, str] = {
    "codex": "review and trust the new hook with /hooks in Codex; until then it is probably inactive",
    "claude": "start a new Claude Code session (a running one may keep the hooks it started with)",
    "clutch": "no restart for a `patchReload: live` profile (clutch-web's web: it watches the file);  a `startup` "
    "profile reads it at its next start.  Ask a new Clutch session to run `ps` and expect a refusal",
    "kimi": "start a new Kimi Code session, ask it to run `ps`, and expect a refusal",
    "minimax": "start a new MiniMax Code session, ask it to run `ps`, and expect a refusal (a local plugin is enabled by "
    "default: nothing to install or approve)",
    "copilot": "OWNER STEP (first install): start a new Copilot CLI session, ask it to run a process listing, and "
    "expect a refusal.  No live preToolUse firing is recorded on this Mac, so until that proof exists the guard is "
    "written but UNVERIFIED",
    "opencode": "OWNER STEP: start a new OpenCode session, ask it to run a process listing, and expect a refusal.  "
    "Whether permission.ask denies live is UNVERIFIED until that proof exists",
    "fx": "no installer step exists: fx stays RED (UNSUPPORTED) until Jay merges the prepared compensating control "
    "from `plan fx` into ~/.fx/settings.json by hand, or approves an exception",
}
PLATFORM_ORDER: tuple[str, ...] = tuple(PLATFORMS)
SUPPORTED_KEYS: tuple[str, ...] = tuple(k for k in PLATFORM_ORDER if PLATFORMS[k].supported)
# What `all` reaches: the config-file platforms, then the ones that are not config files but belong to a tool the owner
# runs and need no owner step afterwards (muse needs an install and an approval, clutch and kimi edit a file we do not own).
ALL_KEYS: tuple[str, ...] = SUPPORTED_KEYS + tuple(
    k for k in PLATFORM_ORDER if PLATFORMS[k].in_all and k not in SUPPORTED_KEYS
)


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
    holder: dict  # the dict that holds "command"
    group: dict | None  # the dict that holds "matcher" (None for cursor)
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
                    problems.append(
                        f"matcher {group.get('matcher')!r} never matches {plat.matcher}"
                    )
                if hook.get("type", "command") != "command":
                    problems.append(f"hook type is {hook.get('type')!r}, not 'command'")
                out.append(
                    Entry(
                        hook["command"],
                        f"hooks.{plat.event}[{gi}].hooks[{hi}]",
                        problems,
                        hook,
                        group,
                        gi,
                        hi,
                    )
                )
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
    return [
        Entry(h["command"], f"hooks.{plat.event}[{i}]", [], h, None, None, i)
        for i, h in enumerate(lst)
        if isinstance(h, dict) and is_ours(h.get("command"))
    ]


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
                out.append(
                    Entry(h["command"], f"{name}.{plat.event}[{i}]", problems, h, h, name, i)
                )  # type: ignore[arg-type]
    return out


_LOCATE: dict[str, Callable[[dict, Platform], list[Entry]]] = {
    "claude": _locate_claude,
    "cursor": _locate_cursor,
    "antigravity": _locate_antigravity,
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
        if (
            plat.style == "claude"
            and first.group is not None
            and not _matcher_gates(first.group.get("matcher"), plat.matcher)
        ):
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
            notes.append(
                f"{len(entries) - 1} duplicate {HOOK_NAME} entr{'y' if len(entries) == 2 else 'ies'} left in place"
            )
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
            new["version"] = 1  # Cursor's hooks.json schema version; only a fresh file needs it
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
        return isinstance(new, dict) and all(
            k in new and _is_extension(v, new[k]) for k, v in old.items()
        )
    if isinstance(old, list):
        return (
            isinstance(new, list)
            and len(new) >= len(old)
            and all(_is_extension(a, b) for a, b in zip(old, new))
        )
    return old == new


def subtree(doc: dict, plat: Platform) -> tuple[str, object]:
    """The part of a config this tool edits, for diffs.  Nothing else of the file is ever printed."""
    if plat.style == "antigravity":
        picked = {
            k: v for k, v in doc.items() if k == AG_GROUP or (isinstance(v, dict) and count_ours(v))
        }
        return AG_GROUP, picked
    hooks = doc.get("hooks")
    return f"hooks.{plat.event}", (hooks.get(plat.event) if isinstance(hooks, dict) else None)


# Defense in depth.  `plan` no longer prints any line of the owner's config that is not our own entry
# (see plan_view), so these patterns only see our entry and the old command of an entry being updated.
_KEYWORDS = r"(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key|credential|authorization)"
_SECRET_PAIR = re.compile(r'(?i)("[^"]*' + _KEYWORDS + r'[^"]*"\s*:\s*)"(?:[^"\\]|\\.)*"')
_SECRET_KV = re.compile(
    r"(?i)(\b[\w-]*" + _KEYWORDS + r"s?[\w-]*)(['\"]?\s*[:=]\s*)"
    r"((?:(?:bearer|basic|digest|token)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&'\"]+))"
)
_SECRET_FLAG = re.compile(
    r"(?i)(?<![\w-])(--?[\w-]*(?:passw(?:or)?d|token|secret|api[_-]?key|credential)[\w-]*)(=|\s+)"
    r"(?!-)(\"[^\"]*\"|'[^']*'|\S+)"
)
_USER_PASS = re.compile(r"(?<![\w-])(-u|--user)(\s+|=)(?!-)(\S*:\S+)")
_SECRET_INLINE = re.compile(
    r"(?i)((?:token|secret|passw\w*|api[_-]?key|authorization|bearer)\W{1,4})[A-Za-z0-9_\-./+=]{8,}"
)
_TOKEN_SHAPES = re.compile(
    r"\b(?:(?:sk-|pk-|gh[pousr]_|xox[abprs]-)[A-Za-z0-9_\-]{10,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{8,}|pk_(?:live|test)_[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|A[CK][0-9a-f]{32}\b|AIza[0-9A-Za-z_-]{30,}|AKIA[A-Z0-9]{12,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,})"
)
_LONG_SECRET = re.compile(r"(?<![\w/.~+%-])[A-Za-z0-9/+]{40}(?![\w/.~+%-])")


def _mask_kv(m: re.Match[str]) -> str:
    if "<redacted>" in m.group(3):
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}<redacted>"


def _mask_flag(m: re.Match[str]) -> str:
    return m.group(0) if "<redacted>" in m.group(3) else f"{m.group(1)}{m.group(2)}<redacted>"


def _mask_long(m: re.Match[str]) -> str:
    """40 characters of base64 alphabet that mix digits and both cases: an AWS secret access key.  Ordinary
    paths and words fail one of the tests."""
    t = m.group(0)
    secret = (
        re.search(r"\d", t)
        and re.search(r"[a-z]", t)
        and re.search(r"[A-Z]", t)
        and t.count("/") <= 3
    )
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
    for name, grp in node.items() if isinstance(node, dict) else []:
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
    path: str  # as named
    real: str  # symlinks resolved
    exists: bool
    raw: bytes
    doc: dict
    indent: object
    ensure_ascii: bool
    trailing_newline: bool
    exact: bool  # the original text equals our re-serialization of it
    mode: int | None
    note: str = ""
    duplicate_keys: list = field(
        default_factory=list
    )  # keys repeated in one object: a rewrite keeps only the last
    lossy_numbers: list = field(default_factory=list)  # number literals a rewrite would change
    unparseable_numbers: list = field(
        default_factory=list
    )  # NaN, Infinity, 1e999: strict JSON parsers reject the file


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
        raise Refused(
            f"{path} is a dangling symlink (-> {os.readlink(path)}); refusing to create files through it"
        )
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
        return Loaded(
            path,
            real,
            True,
            raw,
            {},
            2,
            True,
            True,
            False,
            mode,
            note="file is empty; treated as {}",
        )
    scan = _Scan()
    try:
        doc = json.loads(
            text,
            object_pairs_hook=scan.pairs,
            parse_constant=scan.constant,
            parse_float=scan.number,
        )
    except (ValueError, RecursionError) as exc:
        raise Refused(f"{path} is not valid JSON ({exc}); refusing to touch it")
    if not isinstance(doc, dict):
        raise Refused(f"{path} is JSON but not an object at the top level; refusing to touch it")
    flags = dict(
        duplicate_keys=scan.duplicates,
        lossy_numbers=scan.lossy,
        unparseable_numbers=scan.unparseable,
    )
    indent = _detect_indent(text)
    trailing = text.endswith("\n")
    if not scan.unparseable:
        for ea in (True, False):
            if json.dumps(doc, indent=indent, ensure_ascii=ea) + ("\n" if trailing else "") == text:
                return Loaded(path, real, True, raw, doc, indent, ea, trailing, True, mode, **flags)
    ea = False
    try:  # a lone surrogate cannot be written as raw text: escape instead
        json.dumps(doc, indent=indent, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        ea = True
    return Loaded(
        path,
        real,
        True,
        raw,
        doc,
        indent,
        ea,
        trailing,
        False,
        mode,
        note="original formatting does not round-trip; apply re-indents the whole file (content unchanged)",
        **flags,
    )


def dump_config(doc: dict, cfg: Loaded) -> str:
    return json.dumps(doc, indent=cfg.indent, ensure_ascii=cfg.ensure_ascii, allow_nan=False) + (
        "\n" if cfg.trailing_newline else ""
    )


@dataclass
class PlatformPlan:
    platform: Platform
    path: str
    action: str  # add, update, unchanged, skip, refuse, unsupported
    command: str
    reason: str = ""
    diff: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    new_text: str | None = None
    cfg: Loaded | None = None
    link: str | None = None  # "symlink -> <real path>" when the config is reached through a symlink
    hidden: int = 0  # hooks of other owners under the edited event; counted, never printed
    label: str = ""


def _json_lines(obj: object) -> list[str]:
    if obj is None:
        return []
    return json.dumps(obj, indent=2, ensure_ascii=False).splitlines()


def _link_check(
    path: str, home: str, rel: str, follow_symlinks: bool
) -> tuple[str | None, str | None, str | None]:
    """(refusal, write refusal, note) for a config that may be reached through symlinks.  A dangling link
    anywhere between the home and the file is always a refusal: writing would create its target's
    directories.  A path that resolves outside the home is a refusal only for a WRITE, and only without
    `follow_symlinks`; inside the home it is written through."""
    cur = home
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur) and not os.path.exists(cur):
            return (
                f"{cur} is a dangling symlink (-> {os.readlink(cur)}); refusing to create files through it",
                None,
                None,
            )
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
        out.append(
            "the file has disableAllHooks: true, so the platform runs NO hooks and the guard is inert"
        )
    env = doc.get("env")
    if isinstance(env, dict) and name in env and _is_off(env[name]):
        out.append(
            f"the env block sets {name}={env[name]!r}, the guard's kill switch, so the guard is off for every command"
        )
    return out


def plan_platform(plat: Platform, paths: Paths, *, follow_symlinks: bool = False) -> PlatformPlan:
    """Read-only: what `apply PLATFORM` would do, with a unified diff of the hooks this tool edits.  The diff
    shows only our own entry; other hooks are counted and elided."""
    path = config_path(paths, plat)
    cmd = hook_command(paths, plat.fmt)
    if plat.kind == "plugin":
        if plat.key == "opencode":
            op = plan_opencode(paths, follow_symlinks=follow_symlinks)
            act = {
                "new": "add",
                "stale": "update",
                "current": "unchanged",
                "skip": "skip",
                "refuse": "refuse",
            }[op.state]
            return PlatformPlan(plat, op.config, act, op.entry, op.reason, notes=list(op.notes))
        return PlatformPlan(
            plat,
            paths.muse_bundle,
            "unsupported",
            cmd,
            "takes hooks from a plugin bundle, not from a config file; `plan muse` shows it and "
            "`apply tools` writes it.  No config file is edited",
        )
    if plat.kind == "bundle":
        if plat.key == "copilot":
            cp = plan_copilot(paths, follow_symlinks=follow_symlinks)
            act = {
                "new": "add",
                "stale": "update",
                "current": "unchanged",
                "skip": "skip",
                "refuse": "refuse",
            }[cp.state]
            return PlatformPlan(plat, cp.path, act, cp.command, cp.reason, notes=list(cp.notes))
        mp = plan_minimax(paths, follow_symlinks=follow_symlinks)
        act = {
            "new": "add",
            "stale": "update",
            "current": "unchanged",
            "skip": "skip",
            "refuse": "refuse",
        }[mp.state]
        return PlatformPlan(plat, mp.bundle, act, mp.command, mp.reason, notes=list(mp.notes))
    if not plat.supported:
        reason = (
            FX_BLOCKER + "; nothing is written"
            if plat.key == "fx"
            else "no verified hook surface; nothing is written"
        )
        return PlatformPlan(plat, path, "unsupported", cmd, reason)
    if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
        return PlatformPlan(
            plat,
            path,
            "skip",
            cmd,
            f"~/{plat.platform_dir} does not exist (platform not installed)",
        )
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
            return PlatformPlan(
                plat,
                path,
                "refuse",
                cmd,
                f"{path} holds numbers JSON cannot represent ({_first(cfg.unparseable_numbers)}); "
                "strict parsers reject the file and a rewrite would change them; fix the file first",
            )
        if cfg.duplicate_keys:
            return PlatformPlan(
                plat,
                path,
                "refuse",
                cmd,
                f"{path} has duplicate key(s) {_first(cfg.duplicate_keys)} in one object; a rewrite "
                "would silently keep only the last value; fix the file first",
            )
        if cfg.lossy_numbers:
            return PlatformPlan(
                plat,
                path,
                "refuse",
                cmd,
                f"{path} holds number(s) a rewrite would change ({_first(cfg.lossy_numbers)}); "
                "refusing to touch it",
            )
        if cfg.exists and cfg.mode is not None and not cfg.mode & 0o200:
            return PlatformPlan(
                plat,
                path,
                "refuse",
                cmd,
                f"{path} is read-only (mode {cfg.mode:04o}); not overriding the owner's choice",
            )
        parent = os.path.dirname(cfg.real)
        if os.path.isdir(parent) and not os.access(parent, os.W_OK | os.X_OK):
            return PlatformPlan(
                plat, path, "refuse", cmd, f"the directory {parent} is not writable"
            )
    new_text = dump_config(new_doc, cfg)
    try:
        new_text.encode("utf-8")
    except UnicodeEncodeError:
        return PlatformPlan(
            plat, path, "refuse", cmd, f"the merged document for {path} cannot be encoded as UTF-8"
        )
    if cfg.note:
        notes.append(cfg.note)
    notes.extend("WARNING: " + w for w in _switch_warnings(new_doc))
    if plat.key == "codex" and action == "updated":
        notes.append(
            "the existing Codex trust record for this hook is stale after this change; review and "
            "re-trust it with /hooks in Codex"
        )
    label, before, hidden = plan_view(cfg.doc, plat)
    _label, after, _h = plan_view(new_doc, plat)
    diff: list[str] = []
    if action != "unchanged":
        diff = [
            redact(ln)
            for ln in difflib.unified_diff(
                _json_lines(before),
                _json_lines(after),
                fromfile=f"{path} {label} (current)",
                tofile=f"{path} {label} (after apply)",
                n=3,
                lineterm="",
            )
        ]
    act = {"added": "add", "updated": "update", "unchanged": "unchanged"}[action]
    return PlatformPlan(
        plat,
        path,
        act,
        cmd,
        diff=diff,
        notes=notes,
        new_text=new_text,
        cfg=cfg,
        link=link,
        hidden=hidden,
        label=label,
    )


# --------------------------------------------------------------------------- apply: tools


@dataclass
class Result:
    target: str
    status: str  # added, updated, unchanged, installed, skipped, refused, failed, unsupported
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
        if (plat.supported or plat.kind in ("plugin", "block", "bundle")) and (
            plat.fmt,
            plat.shape,
        ) not in out:
            out.append((plat.fmt, plat.shape))
    return out


def fleet_probe_formats() -> list[tuple[str, str]]:
    """(--format, payload shape) of every harness that runs fleet-guard-hook."""
    out: list[tuple[str, str]] = []
    for plat in PLATFORMS.values():
        if plat.kind in ("plugin", "block", "bundle") and (plat.fmt, plat.shape) not in out:
            out.append((plat.fmt, plat.shape))
    return out


def apply_tools(
    src: Source,
    paths: Paths,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    probes: bool = True,
    emit: Callable[[str], None] = lambda s: None,
    replace_extra: bool = False,
) -> Result:
    """Copy or refresh the stable dir atomically.  The new copy is staged beside the final dir and
    probed first (every supported platform's format); it is swapped in only if the probes pass.
    Between the two renames of the swap the dir is briefly absent, and a hook call that lands there
    fails open.  A non-empty dir that this tool did not write is refused (see `plan_tools`)."""
    try:
        if os.path.realpath(src.scripts_dir) == os.path.realpath(paths.stable):
            raise Refused(
                "the source is the stable dir itself; run this from a checkout of origin/main"
            )
        plan = plan_tools(src, paths, replace_extra=replace_extra)
        if plan.problems:
            raise Refused("; ".join(plan.problems))
        files = build_files(src, paths)
    except Refused as exc:
        return Result("tools", "refused", str(exc))
    lane_text = plan.lane_shim_text
    runnable = all(
        os.access(p, os.X_OK)
        for p in (
            paths.hook_shim,
            paths.fleet_shim,
            os.path.join(paths.stable, MINIMAX_HOOK_NAME),
            paths.muse_seat,
            paths.muse_wrapper,
        )
    )
    if (
        plan.state == "current"
        and plan.lane_shim_state in ("same", "skipped", "foreign")
        and runnable
    ):
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
                got = probe_command(
                    f"{staged_cmd} --format {fmt}",
                    target="staged",
                    fmt=fmt,
                    shape=shape,
                    home=paths.home,
                    timeout=timeout,
                    minimal_path=minimal_path,
                )
                bad.extend(c for c in got if c.status == "FAIL")
            staged_fleet = shlex.quote(os.path.join(stage, FLEET_HOOK_NAME))
            for fmt, shape in fleet_probe_formats():
                got = probe_command(
                    f"{staged_fleet} --format {fmt}",
                    target="staged-fleet",
                    fmt=fmt,
                    shape=shape,
                    home=paths.home,
                    timeout=timeout,
                    minimal_path=minimal_path,
                    secret=True,
                )
                bad.extend(c for c in got if c.status == "FAIL")
            staged_mm = shlex.quote(os.path.join(stage, MINIMAX_HOOK_NAME))
            bad.extend(
                c
                for c in probe_minimax_files(
                    staged_mm,
                    paths.home,
                    os.path.join(stage, REGISTRY_NAME),
                    timeout,
                    minimal_path,
                    source_registry=src.registry,
                )
                if c.status == "FAIL"
            )
            # MM has its own adapter, so the shared shell probes above prove nothing for it.  Probe
            # the staged copy with compatible (Bash) and native (bash) shell payloads, including
            # secret denials, or a broken decide() shell branch could be swapped in and fail open.
            for shape in ("claude", "muse"):
                bad.extend(
                    c
                    for c in probe_command(
                        staged_mm,
                        target="staged-mm",
                        fmt="claude",
                        shape=shape,
                        home=paths.home,
                        timeout=timeout,
                        minimal_path=minimal_path,
                        secret=True,
                    )
                    if c.status == "FAIL"
                )
            if bad:
                for c in bad:
                    emit(c.line())
                raise Refused(
                    "the staged copy failed its own probes; the installed copy was not touched"
                )
        shutil.rmtree(
            os.path.join(stage, PACKAGE, "__pycache__"), ignore_errors=True
        )  # probes wrote it with the stage path
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
    for w in plan.warnings:
        extra += f"; WARNING {w}"
    return Result(
        "tools", "installed", f"{paths.stable} sha {plan.sha[:12]}, {len(files)} files{extra}"
    )


# --------------------------------------------------------------------------- apply: platforms


def _reread_matches(real: str, raw: bytes, existed: bool) -> bool:
    try:
        now = _read_bytes(real) if os.path.exists(real) else None
    except OSError:
        return False
    return now == raw if existed else now is None


def _makedirs_private(dirpath: str) -> None:
    """Create `dirpath` (and parents) owner-only, refusing a symlinked final component.

    A config directory the fleet writes credentials into must not be created with the
    process umask's permissions,  and `os.makedirs` follows a symlink at the target,  so
    the link is refused here rather than written through.  Every level this call creates
    is 0700,  because `os.makedirs` gives its intermediate levels the umask's default.
    `exist_ok=True` keeps an existing directory's own permissions:  only a directory this
    call creates is 0700.
    """
    if os.path.islink(dirpath):
        raise OSError(f"{dirpath} is a symlink; not creating a directory through it")
    missing = []
    current = dirpath
    while current and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for level in reversed(missing):
        try:
            os.mkdir(level, 0o700)
        except FileExistsError:
            pass
    if os.path.islink(dirpath):
        raise OSError(f"{dirpath} is a symlink; not creating a directory through it")


# The lock files live in the fleet's own state directory,  never beside the file they
# guard,  so a lock never appears in a platform's home as a file this tool does not own.
LOCK_DIR_REL = ".local/state/fleet-lanes/locks"


@contextlib.contextmanager
def _target_lock(home: str, *targets: str):
    """Hold an exclusive lock over the targets for the whole check-and-write.

    The re-read that guards against a concurrent edit runs outside any lock,  so two
    installers can both pass the check and the second write then erases the first.
    The lock is taken before the re-read and released after the last write,  its
    validation and its rollback,  so the check and the write are one unit.
    """
    directory = os.path.join(home, *LOCK_DIR_REL.split("/"))
    missing = []
    current = directory
    while current and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for level in reversed(missing):
        try:
            os.mkdir(level, 0o700)
        except FileExistsError:
            pass
    handles = []
    try:
        for target in sorted(set(targets)):
            name = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16] + ".lock"
            handle = open(os.path.join(directory, name), "a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handles.append(handle)
        yield
    finally:
        for handle in handles:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


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
            os.chmod(
                name, BACKUP_MODE
            )  # owner-only whatever the original's mode (a settings file may hold a token)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
        return name
    raise OSError("could not find a free backup name")


def apply_platform(
    plat: Platform,
    paths: Paths,
    *,
    now: Callable[[], float] = time.time,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    shim_proven: bool = False,
    follow_symlinks: bool = False,
    dsh_bin: str | None = None,
    check_dsh: bool = True,
    check_kimi: bool = True,
) -> Result:
    """Merge the hook into the platform's config, but only if the installed shim passes the probes for this
    platform's own format right now.  The `apply` command never sets `shim_proven` (it skips those probes):
    `apply tools` proves a staged copy, and an `unchanged` tools result proves nothing about this run.  Only
    callers that have just proven the shim themselves (the self-test, the tests) pass it."""
    if plat.key == "fx":
        return Result(
            plat.key,
            "skipped",
            FX_BLOCKER
            + "; no file was written.  Merge the compensating control from `plan fx` by hand "
            "only after Jay approves it",
        )
    if plat.kind == "plugin":
        if plat.key == "opencode":
            return apply_opencode(
                paths,
                now=now,
                timeout=timeout,
                minimal_path=minimal_path,
                shim_proven=shim_proven,
                follow_symlinks=follow_symlinks,
            )
        return apply_muse(paths)
    if plat.kind == "block":
        return apply_block(
            plat,
            paths,
            now=now,
            timeout=timeout,
            minimal_path=minimal_path,
            dsh_bin=dsh_bin,
            check_dsh=check_dsh,
            check_kimi=check_kimi,
        )
    if plat.kind == "bundle":
        if plat.key == "copilot":
            return apply_copilot(
                paths,
                now=now,
                timeout=timeout,
                minimal_path=minimal_path,
                shim_proven=shim_proven,
                follow_symlinks=follow_symlinks,
            )
        return apply_minimax(
            paths,
            timeout=timeout,
            minimal_path=minimal_path,
            shim_proven=shim_proven,
            follow_symlinks=follow_symlinks,
        )
    plan = plan_platform(plat, paths, follow_symlinks=follow_symlinks)
    if plan.action == "unsupported":
        return Result(plat.key, "unsupported", plan.reason)
    if plan.action == "skip":
        return Result(plat.key, "skipped", plan.reason)
    if plan.action == "refuse":
        return Result(plat.key, "refused", plan.reason)
    if not shim_proven:
        if not (os.path.isfile(paths.hook_shim) and os.access(paths.hook_shim, os.X_OK)):
            return Result(
                plat.key,
                "refused",
                f"the hook shim {paths.hook_shim} is missing or not executable; run `apply tools` first",
            )
        checks = probe_command(
            plan.command,
            target=plat.key,
            fmt=plat.fmt,
            shape=plat.shape,
            home=paths.home,
            timeout=timeout,
            minimal_path=minimal_path,
        )
        bad = [c for c in checks if c.status == "FAIL"]
        if bad:
            return Result(
                plat.key,
                "refused",
                "the installed hook shim fails its probes, so it will not be wired in: "
                + bad[0].detail,
            )
    if plan.action == "unchanged":
        return Result(plat.key, "unchanged", f"{plan.path} already has the hook")
    cfg = plan.cfg
    assert cfg is not None and plan.new_text is not None
    # Self-checks on the document we are about to write.
    try:
        reparsed = json.loads(plan.new_text)
    except ValueError as exc:
        return Result(
            plat.key, "refused", f"the merged document does not serialize to valid JSON: {exc}"
        )
    found = locate(reparsed, plat)
    if len(found) < 1 or found[0].command != plan.command or found[0].problems:
        return Result(
            plat.key,
            "refused",
            "the merged document does not hold the hook where the platform reads it",
        )
    if plan.action == "add" and not _is_extension(cfg.doc, reparsed):
        return Result(
            plat.key, "refused", "internal check failed: the merge would change existing content"
        )
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
            return Result(
                plat.key,
                "failed",
                f"{plan.path} changed while it was being merged; nothing written, retry",
            )
        atomic_write(
            real, plan.new_text.encode("utf-8"), cfg.mode if cfg.mode is not None else plat.new_mode
        )
    except OSError as exc:
        return Result(
            plat.key, "failed", f"{plan.path} not changed: {type(exc).__name__}: {exc}", backup
        )
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
        return Result(
            plat.key,
            "failed",
            f"{plan.path} failed validation after the write; the original was restored",
            backup,
        )
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
        out.append(
            Check(
                t,
                "VERSION",
                "PASS" if ver.get("tree") else "FAIL",
                f"sha={ver.get('sha', '?')[:12]} package={ver.get('package', '?')} files={ver.get('files', '?')}",
            )
        )
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
            out.append(
                Check(
                    t,
                    "files",
                    "FAIL",
                    "the files no longer match VERSION's tree digest (modified, added or removed)",
                )
            )
        else:
            out.append(Check(t, "files", "PASS", f"{len(body)} files match the VERSION digest"))
    try:
        reg = json.loads(_read_bytes(paths.registry).decode("utf-8"))
        good = isinstance(reg, dict) and bool(reg.get("apps")) and bool(reg.get("seats"))
        out.append(
            Check(
                t,
                REGISTRY_NAME,
                "PASS" if good else "FAIL",
                "parses and has apps and seats" if good else "parses but has no apps or seats",
            )
        )
    except (OSError, ValueError) as exc:
        out.append(Check(t, REGISTRY_NAME, "FAIL", f"{paths.registry}: {exc}"))
    facts = read_guard_facts(paths.stable)
    out.append(_check_shim(t, HOOK_NAME, paths.hook_shim, render_hook_shim(facts)))
    if facts.words:
        out.append(
            Check(
                t,
                "prefilter",
                "PASS",
                f"{len(facts.words)} trigger words, {len(facts.lookalikes)} non-ASCII "
                "lookalikes and the \\u escape, all from the installed guard.py",
            )
        )
    else:
        out.append(
            Check(
                t,
                "prefilter",
                "WARN",
                "; ".join(facts.problems) or "the installed guard has no trigger words",
            )
        )
    out.append(_check_shim(t, FLEET_HOOK_NAME, paths.fleet_shim, render_fleet_hook_shim(facts)))
    out.append(
        _check_shim(
            t,
            MINIMAX_HOOK_NAME,
            os.path.join(paths.stable, MINIMAX_HOOK_NAME),
            render_minimax_hook_shim(),
        )
    )
    if f"{PACKAGE}/{SECRET_GUARD_MODULE}" in have:
        out.append(
            Check(
                t, "secret guard", "PASS", f"{PACKAGE}/{SECRET_GUARD_MODULE} is in the stable copy"
            )
        )
    else:
        out.append(
            Check(
                t,
                "secret guard",
                "FAIL",
                f"{PACKAGE}/{SECRET_GUARD_MODULE} is missing, so fleet-guard-hook "
                "would run the lane guard alone; re-run `apply tools`",
            )
        )
    if (
        f"{PACKAGE}/{LANE_MODULE}" in have
        and os.path.lexists(paths.lane_shim)
        and not (
            os.path.isfile(paths.lane_shim)
            and not os.path.islink(paths.lane_shim)
            and _is_generated(paths.lane_shim)
        )
    ):
        out.append(
            Check(
                t,
                LANE_NAME,
                "WARN",
                f"{paths.lane_shim} is {describe_foreign(paths.lane_shim)}; left alone, "
                "so `lane` there is NOT the stable copy",
            )
        )
    elif f"{PACKAGE}/{LANE_MODULE}" in have:
        c = _check_shim(t, LANE_NAME, paths.lane_shim, render_lane_shim(paths.stable))
        out.append(c)
        if c.status == "PASS":
            res = run_shell(
                f"{shlex.quote(paths.lane_shim)} --help", b"", probe_env(paths.home, None), timeout
            )
            ok = res.returncode == 0 and not res.timed_out
            out.append(
                Check(
                    t,
                    "lane --help",
                    "PASS" if ok else "FAIL",
                    f"exit={res.returncode} stdout={_brief(res.stdout, 120)}"
                    + (f" stderr={_brief(res.stderr, 200)}" if res.stderr else "")
                    + ("; timed out" if res.timed_out else ""),
                )
            )
    else:
        out.append(
            Check(
                t,
                LANE_NAME,
                "WARN",
                f"no {LANE_MODULE} in the stable copy yet, so there is no lane shim",
            )
        )
    return out


def _check_shim(target: str, name: str, path: str, expected: str) -> Check:
    if not os.path.isfile(path):
        return Check(target, f"shim {name}", "FAIL", f"{path} is missing")
    mode = os.stat(path).st_mode
    if not (mode & 0o111) or not os.access(path, os.X_OK):
        return Check(
            target, f"shim {name}", "FAIL", f"{path} is not executable (mode {mode & 0o777:o})"
        )
    try:
        if _read_bytes(path) != expected.encode("utf-8"):
            return Check(
                target,
                f"shim {name}",
                "FAIL",
                f"{path} differs from what this install_tools writes; re-run `apply tools`",
            )
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
        return [
            Check(
                "codex",
                "trust",
                "WARN",
                f"cannot read {toml}; Codex trust is UNVERIFIED (review the hook with /hooks)",
            )
        ]
    out: list[Check] = []
    flag = toml_features_hooks(text)
    if flag is not None and flag != "true":
        out.append(
            Check(
                "codex",
                "features",
                "FAIL",
                f"{toml} sets [features] hooks = {flag}, so Codex ignores hooks.json and the hook never runs",
            )
        )
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == header:
            for body in lines[i + 1 :]:
                if body.lstrip().startswith("["):
                    break
                if re.match(r'\s*trusted_hash\s*=\s*"sha256:[0-9a-f]+"', body):
                    out.append(
                        Check(
                            "codex",
                            "trust",
                            "WARN",
                            "config.toml has a hooks.state record for this hook, but its hash is UNCHECKED: "
                            "an edited or stale hook would show the same record.  Confirm with /hooks in Codex",
                        )
                    )
                    return out
            break
    out.append(
        Check(
            "codex",
            "trust",
            "WARN",
            f"NOT TRUSTED YET: no hooks.state record for {key}.  Codex re-trusts by hash after an edit, so review "
            "the hook with /hooks in Codex.  Until then it is probably inactive (UNVERIFIED)",
        )
    )
    return out


def verify_platform(
    plat: Platform, paths: Paths, *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True
) -> list[Check]:
    """Take the hook command back out of the platform's config file and run it against the deny and
    allow payloads, then check that the platform will actually run it: no disableAllHooks, no kill switch
    in the config's env block (or, as a warning, in the caller's environment), and for Codex the feature
    flag and the trust record.  Finds the entry through the same event and matcher accessors the merge uses."""
    t = plat.key
    if plat.key == "fx":
        if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
            return [
                Check(
                    t,
                    "platform",
                    "SKIP",
                    f"~/{plat.platform_dir} does not exist (platform not installed)",
                )
            ]
        return [Check(t, "platform", "FAIL", FX_BLOCKER)]
    if plat.kind == "plugin":
        if plat.key == "opencode":
            return verify_opencode(paths, timeout=timeout, minimal_path=minimal_path)
        return verify_muse(paths, timeout=timeout, minimal_path=minimal_path)
    if plat.kind == "block":
        return verify_block(plat, paths, timeout=timeout, minimal_path=minimal_path)
    if plat.kind == "bundle":
        if plat.key == "copilot":
            # Copilot CLI is retired on this Mac,  so the platform dir may be absent.  The
            # per-platform verifier SKIPs that case,  and the dispatch must reach it
            # rather than answer FAIL for a platform that is simply not installed.
            return verify_copilot(paths, timeout=timeout, minimal_path=minimal_path)
        return verify_minimax(paths, timeout=timeout, minimal_path=minimal_path)
    if not plat.supported:
        return [
            Check(
                t,
                "platform",
                "FAIL",
                "UNSUPPORTED: no verified hook surface, so there is nothing to verify",
            )
        ]
    path = config_path(paths, plat)
    if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
        return [
            Check(
                t,
                "platform",
                "SKIP",
                f"~/{plat.platform_dir} does not exist (platform not installed)",
            )
        ]
    if not os.path.exists(os.path.realpath(path)):
        return [Check(t, "config", "FAIL", f"{path} does not exist; no hook installed")]
    try:
        cfg = load_config(path)
        entries = locate(cfg.doc, plat)
    except Refused as exc:
        return [Check(t, "config", "FAIL", str(exc))]
    if cfg.unparseable_numbers:
        return [
            Check(
                t,
                "config",
                "FAIL",
                f"{path} holds numbers JSON cannot represent ({_first(cfg.unparseable_numbers)}); "
                "the platform's JSON parser rejects the whole file, so no hook runs",
            )
        ]
    elsewhere = count_ours(cfg.doc) - len(entries)
    if not entries:
        hint = (
            f"; {elsewhere} {HOOK_NAME} command(s) sit elsewhere in the file, where the platform does not gate "
            f"tool calls on them"
            if elsewhere > 0
            else ""
        )
        return [
            Check(t, "config", "FAIL", f"no {HOOK_NAME} hook under {plat.event} in {path}{hint}")
        ]
    out: list[Check] = []
    for idx, e in enumerate(entries):
        label = "entry" if len(entries) == 1 else f"entry#{idx + 1}"
        if e.problems:
            out.append(Check(t, label, "FAIL", f"{e.where}: " + "; ".join(e.problems)))
            continue
        out.append(Check(t, label, "PASS", f"{e.where} command={e.command}"))
        out.extend(
            probe_command(
                e.command,
                target=t,
                fmt=plat.fmt,
                shape=plat.shape,
                home=paths.home,
                timeout=timeout,
                minimal_path=minimal_path,
            )
        )
    if elsewhere > 0:
        out.append(
            Check(
                t,
                "stray",
                "WARN",
                f"{elsewhere} more {HOOK_NAME} command(s) in the file are outside {plat.event} and do nothing",
            )
        )
    for problem in _switch_warnings(cfg.doc):
        out.append(
            Check(
                t,
                "hook-switch",
                "FAIL",
                f"{path}: {problem}; the probes above run the command directly, "
                "so they cannot see this",
            )
        )
    name, _off = guard_switch()
    if _is_off(os.environ.get(name, "")):
        out.append(
            Check(
                t,
                "guard-switch-env",
                "WARN",
                f"{name}={os.environ.get(name)!r} is set in the environment this command runs in: the probes "
                "scrub it, but an agent started from this environment gets a guard that is off",
            )
        )
    if plat.key == "codex" and entries and entries[0].group_index is not None:
        out.extend(
            _codex_checks(paths, os.path.join(paths.home, ".codex", "hooks.json"), entries[0])
        )
    return out


# --------------------------------------------------------------------------- Muse Code: plan, apply, verify


@dataclass
class MusePlan:
    stable: str
    bundle: str
    state: str  # new (no file yet), current, stale
    files: list[FileStatus]
    manifest_text: str
    wrapper_text: str
    seat_text: str
    actions: list[str]
    warnings: list[str]


def _muse_expected(stable: str, facts: GuardFacts) -> dict[str, str]:
    return {
        MUSE_SEAT_NAME: render_muse_seat(),
        MUSE_MANIFEST: render_muse_manifest(stable),
        MUSE_WRAPPER: render_muse_wrapper(stable, facts),
    }


def _read_regular(path: str) -> bytes | None:
    """The bytes of a regular, non-symlink file, or None."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        return _read_bytes(path)
    except OSError:
        return None


def plan_muse(src: Source, paths: Paths) -> MusePlan:
    """Read-only: what `apply tools` writes for Muse Code (the plugin bundle and the seat shim), compared with what
    the stable dir holds now, plus the OWNER ACTIONS that come after.  Nothing here runs muse or reads its config."""
    facts = read_guard_facts(src.scripts_dir)
    want = _muse_expected(paths.stable, facts)
    statuses: list[FileStatus] = []
    for rel, text in want.items():
        have = _read_regular(os.path.join(paths.stable, *rel.split("/")))
        statuses.append(
            FileStatus(
                rel,
                "new" if have is None else ("same" if have == text.encode("utf-8") else "changed"),
            )
        )
    if all(s.status == "new" for s in statuses):
        state = "new"
    else:
        state = "current" if all(s.status == "same" for s in statuses) else "stale"
    return MusePlan(
        stable=paths.stable,
        bundle=paths.muse_bundle,
        state=state,
        files=statuses,
        manifest_text=want[MUSE_MANIFEST],
        wrapper_text=want[MUSE_WRAPPER],
        seat_text=want[MUSE_SEAT_NAME],
        actions=muse_owner_actions(paths),
        warnings=list(facts.problems),
    )


def apply_muse(paths: Paths) -> Result:
    """The library half of `apply muse`: the bundle and the seat shim are part of the stable copy that
    `apply_tools` swaps in atomically (the command line runs that first), so this writes nothing.  It checks that
    the installed ones are the current ones and returns the verdict the owner actions hang on.  It never runs muse
    and never edits Muse's config.  (The command line's `apply muse` then runs the muse verify unless --no-verify,
    and that runs `muse plugins validate`; see verify_muse.)"""
    facts = read_guard_facts(paths.stable)
    for rel, text in _muse_expected(paths.stable, facts).items():
        full = os.path.join(paths.stable, *rel.split("/"))
        have = _read_regular(full)
        if have is None:
            return Result(
                "muse",
                "refused",
                f"{full} is missing; run `apply tools` first (it writes the plugin bundle)",
            )
        if have != text.encode("utf-8"):
            return Result(
                "muse",
                "refused",
                f"{full} is not what this install_tools writes; run `apply tools` to refresh the stable copy",
            )
        if rel in (MUSE_SEAT_NAME, MUSE_WRAPPER) and not os.access(full, os.X_OK):
            return Result("muse", "refused", f"{full} is not executable; run `apply tools`")
    return Result(
        "muse",
        "unchanged",
        f"plugin bundle {paths.muse_bundle} is current; the stable copy holds everything "
        "this tool writes for Muse.  OWNER ACTIONS follow (yours to run; this tool "
        "never installs or approves the plugin)",
    )


def _muse_cases() -> list[tuple[str, dict, bool, str]]:
    """(check name, hook payload, must deny, text the deny reason must hold).  The payloads a Muse hook runner could
    send: its shell tool in lower case, the same tool spelled Bash, a `ps` (the secret guard's rule), and two tools that
    are not shell calls (one of them carrying the denied command as plain text, which must never be judged as a
    command)."""
    cwd = "/"
    return [
        ("deny/hook-env", probe_payload("muse", DENY_COMMAND, cwd), True, DENY_MARKER),
        (
            "deny-Bash/hook-env",
            dict(probe_payload("muse", DENY_COMMAND, cwd), tool_name="Bash"),
            True,
            DENY_MARKER,
        ),
        (
            "secret/hook-env",
            probe_payload("muse", SECRET_DENY_COMMAND, cwd),
            True,
            SECRET_DENY_MARKER,
        ),
        ("allow/hook-env", probe_payload("muse", ALLOW_COMMAND, cwd), False, ""),
        (
            "other-tool/hook-env",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "lane-verify",
                "cwd": cwd,
                "tool_name": "write",
                "tool_input": {"file_path": "/tmp/notes.md", "content": DENY_COMMAND},
            },
            False,
            "",
        ),
        (
            "other-tool-ps/hook-env",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "lane-verify",
                "cwd": cwd,
                "tool_name": "write",
                "tool_input": {"file_path": "/tmp/notes.md", "content": SECRET_DENY_COMMAND},
            },
            False,
            "",
        ),
    ]


# `muse plugins validate` runs the muse found on PATH.  That muse is a launcher that finds its install dir from its own
# path, not from HOME, and unless auto-update is off it stamps that dir and starts a background self-update (curl to
# its download host) on a run more than an hour after the last check; MUSE_LAUNCHER_INSTALL=1 installs before
# anything else; XDG_CONFIG_HOME and a credential-path variable decide where its config, lock file and sign-in file
# are.  So validate gets the caller's environment minus every XDG_ and MUSE_ variable, then HOME, the XDG dirs,
# TMPDIR and the credential path inside a throwaway dir, auto-update off and sign-in off.  The one MUSE_ variable
# kept is the plugin feature switch, which is not a path.  (Read from launcher version 3, October 2026, and seen in a
# run of a sandboxed copy; what the Muse binary itself writes during validate is UNVERIFIED.)
_MUSE_VALIDATE_DIRS = (
    ("HOME", ""),
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
    ("XDG_STATE_HOME", "state"),
    ("XDG_RUNTIME_DIR", "runtime"),
    ("TMPDIR", "tmp"),
)
_MUSE_VALIDATE_FILES = (("MUSE_AUTH_PATH", ("config", "muse", "auth.json")),)
_MUSE_VALIDATE_SET = (("MUSE_NO_AUTO_UPDATE", "1"), ("MUSE_LOGIN", "0"))
_MUSE_VALIDATE_KEEP = (MUSE_EXPERIMENTAL_ENV,)


def muse_validate_env(scratch: str, base: Mapping[str, str] | None = None) -> dict:
    """The environment `verify` runs `muse plugins validate` with (see _MUSE_VALIDATE_DIRS).  Creates the dirs."""
    base = os.environ if base is None else base
    env = {
        k: v
        for k, v in base.items()
        if not (k.startswith("XDG_") or k.startswith("MUSE_")) or k in _MUSE_VALIDATE_KEEP
    }
    for name, rel in _MUSE_VALIDATE_DIRS:
        path = os.path.join(scratch, rel) if rel else scratch
        os.makedirs(path, mode=0o700, exist_ok=True)
        env[name] = path
    for name, parts in _MUSE_VALIDATE_FILES:
        env[name] = os.path.join(scratch, *parts)
    env.update(_MUSE_VALIDATE_SET)
    return env


def _check_text(target: str, name: str, path: str, expected: str, executable: bool) -> Check:
    have = _read_regular(path)
    if have is None:
        return Check(
            target, name, "FAIL", f"{path} is missing or not a regular file; run `apply tools`"
        )
    if executable and not (os.stat(path).st_mode & 0o111 and os.access(path, os.X_OK)):
        return Check(
            target,
            name,
            "FAIL",
            f"{path} is not executable (mode {os.stat(path).st_mode & 0o777:o})",
        )
    if have != expected.encode("utf-8"):
        return Check(
            target,
            name,
            "FAIL",
            f"{path} differs from what this install_tools writes; re-run `apply tools`",
        )
    return Check(target, name, "PASS", f"{path} current" + (", executable" if executable else ""))


def verify_muse(
    paths: Paths, *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True
) -> list[Check]:
    """Prove the Muse plugin bundle works, without Muse.  The hook command is taken back out of the installed
    manifest and run the way Muse runs a hook: directly (no shell), with an EMPTY environment, from `/`, with
    absolute paths only.  (`minimal_path=False`, the CLI's --no-minimal-path, gives it the caller's PATH instead,
    for a machine where python3 is not where an empty environment looks.)  A known deny payload must print the
    deny shape, an allow payload and a payload for a tool that is not a shell tool must print nothing, and all
    must exit 0.  When `muse` is on PATH, `muse plugins validate` also runs and its answer is reported; an absent
    muse is a SKIP, never a failure.  That runs the real muse, so it is confined (muse_validate_env): HOME, the XDG
    dirs, TMPDIR and Muse's credential path inside a throwaway directory (muse creates a lock file under its config
    dir even to validate, observed), none of the caller's XDG_ or MUSE_ variables except the plugin switch, and
    MUSE_NO_AUTO_UPDATE=1 and MUSE_LOGIN=0, so the launcher neither stamps its install dir nor updates itself.  It
    is confined, not proven read-only: the launcher still runs from its own install dir.  What this cannot see is
    whether the owner has installed and approved the plugin in Muse."""
    t = "muse"
    out: list[Check] = []
    facts = read_guard_facts(paths.stable)
    out.append(
        _check_text(
            t, "plugin.json", paths.muse_manifest, render_muse_manifest(paths.stable), False
        )
    )
    out.append(
        _check_text(
            t,
            "hooks/lane-guard.sh",
            paths.muse_wrapper,
            render_muse_wrapper(paths.stable, facts),
            True,
        )
    )
    out.append(_check_shim(t, FLEET_HOOK_NAME, paths.fleet_shim, render_fleet_hook_shim(facts)))
    out.append(_check_text(t, "shim muse-seat", paths.muse_seat, render_muse_seat(), True))
    argv: list | None = None
    try:
        doc = json.loads((_read_regular(paths.muse_manifest) or b"").decode("utf-8"))
        hooks = doc["capabilities"]["hooks"]
        pre = [h for h in hooks if isinstance(h, dict) and h.get("event") == "PreToolUse"]
        if len(pre) != 1:
            out.append(
                Check(
                    t,
                    "hook command",
                    "FAIL",
                    f"the manifest has {len(pre)} PreToolUse hooks, expected exactly 1",
                )
            )
        else:
            argv = pre[0].get("command")
            extra = sorted(set(pre[0]) & {"matcher"})
            if extra:
                out.append(
                    Check(
                        t,
                        "hook command",
                        "FAIL",
                        "the manifest hook has a matcher, which Muse rejects",
                    )
                )
                argv = None
            elif argv != muse_hook_argv(paths.stable):
                out.append(
                    Check(
                        t,
                        "hook command",
                        "FAIL",
                        f"manifest command {argv!r} is not {muse_hook_argv(paths.stable)!r}",
                    )
                )
                argv = None
            else:
                out.append(Check(t, "hook command", "PASS", " ".join(shlex.quote(a) for a in argv)))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        out.append(
            Check(
                t,
                "hook command",
                "FAIL",
                f"cannot read the hook command from {paths.muse_manifest}: {exc}",
            )
        )
    hook_env: dict = {} if minimal_path else {"PATH": os.environ.get("PATH", "")}
    if argv is None:
        out.append(
            Check(t, "hook-env probes", "FAIL", "not run: no usable hook command in the manifest")
        )
    else:
        for name, payload, must_deny, marker in _muse_cases():
            res = run_argv(argv, json.dumps(payload).encode("utf-8"), hook_env, timeout, cwd="/")
            problem: str | None = None
            if res.error:
                problem = res.error
            elif res.timed_out:
                problem = f"timed out after {timeout:g}s"
            elif res.returncode != 0:
                problem = f"exit {res.returncode}, expected 0"
            elif must_deny:
                problem = check_deny_output("muse", res.stdout, marker)
            elif res.stdout:
                problem = "produced output, expected none"
            observed = f"exit={res.returncode} stdout={_brief(res.stdout)}"
            if res.stderr:
                observed += f" stderr={_brief(res.stderr, 200)}"
            out.append(
                Check(
                    t,
                    name,
                    "FAIL" if problem else "PASS",
                    (f"{problem}; " if problem else "") + observed,
                )
            )
    muse_bin = shutil.which("muse")
    if not os.path.isdir(paths.muse_bundle):
        out.append(
            Check(
                t, "muse validate", "SKIP", f"there is no bundle at {paths.muse_bundle} to validate"
            )
        )
    elif not muse_bin:
        out.append(
            Check(
                t,
                "muse validate",
                "SKIP",
                "muse is not on PATH, so `muse plugins validate` was not run",
            )
        )
    else:
        with tempfile.TemporaryDirectory(prefix="lane-muse-validate-") as scratch_home:
            res = run_argv(
                [muse_bin, "plugins", "validate", paths.muse_bundle, "--json"],
                b"",
                muse_validate_env(scratch_home),
                max(timeout, 30.0),
            )
        verdict: object = None
        try:
            verdict = json.loads(res.stdout.decode("utf-8")).get("valid")
        except (ValueError, AttributeError):
            pass
        if res.timed_out or res.error:
            out.append(
                Check(
                    t,
                    "muse validate",
                    "WARN",
                    res.error
                    or f"timed out after {max(timeout, 30.0):g}s; the bundle was not validated",
                )
            )
        elif verdict is True and res.returncode == 0:
            out.append(Check(t, "muse validate", "PASS", f"{muse_bin} accepts {paths.muse_bundle}"))
        elif verdict is False or res.returncode not in (0, None):
            out.append(
                Check(
                    t,
                    "muse validate",
                    "FAIL",
                    f"{muse_bin} rejects the bundle (exit {res.returncode}): "
                    f"{_brief(res.stdout or res.stderr, 300)}",
                )
            )
        else:
            out.append(
                Check(
                    t,
                    "muse validate",
                    "WARN",
                    f"could not read a verdict from `muse plugins validate` (exit {res.returncode})",
                )
            )
    out.append(
        Check(
            t,
            "owner actions",
            "SKIP",
            "whether the owner has run `muse plugins install ... --scope user` and `muse plugins approve "
            f"{MUSE_PLUGIN_ID}` is not visible from here (this tool does not read Muse's config)",
        )
    )
    return out


# --------------------------------------------------------------------------- Clutch and Kimi: a marked block in a file we do not own


def block_path(plat: Platform, paths: Paths) -> str:
    if plat.key == "clutch":
        return paths.clutch_patch
    return paths.kimi_config


def block_command(plat: Platform, paths: Paths) -> str:
    """The hook command string the block (or, for Clutch, the stable hooks.json) holds."""
    return fleet_hook_command(paths, plat.fmt)


def block_body(plat: Platform, paths: Paths) -> list[str]:
    """The lines between the markers."""
    if plat.key == "clutch":
        return [
            "- insert:",
            "    - id: fleet-hooks-guards",
            "      name: '@deepseek-ai/dsh-hooks-claude-code'",
            "      config:",
            f"        configPath: {json.dumps(paths.clutch_hooks)}",
        ]
    return [
        "[[hooks]]",
        'event = "PreToolUse"',
        'matcher = "Bash"',
        f"command = {json.dumps(block_command(plat, paths))}",
        f"timeout = {HOOK_TIMEOUT_S}",
    ]


def kimi_fields(plat: Platform, paths: Paths) -> dict:
    """The four fields of the Kimi `[[hooks]]` entry this tool writes, as values (what a parse of the file gives)."""
    return {
        "event": "PreToolUse",
        "matcher": "Bash",
        "command": block_command(plat, paths),
        "timeout": HOOK_TIMEOUT_S,
    }


def block_fields(plat: Platform, paths: Paths) -> dict:
    """The `[[hooks]]` fields this tool writes for the TOML block platform (Kimi)."""
    return kimi_fields(plat, paths)


_TOML_TABLE_RE = re.compile(r"^[ \t]*\[")

# A top-level TOML key, as the structural check sees it:  a bare name, a quoted name or
# a single-quoted name, then an equals sign.  It is the line shape a well-formed
# top-level key/value pair has.
_TOML_KEY_RE = re.compile(
    r"^[ \t]*(?:[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*')"
    r"(?:[ \t]*\.[ \t]*(?:[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*'))*[ \t]*="
)


def _toml_scan(line: str, depth: int, quote: str | None) -> tuple[int, str | None]:
    """The bracket depth and the open string delimiter after reading `line`, both carried across lines.  A value may
    span lines, so `depth` and `quote` are the whole state of a TOML value: inside them the lines are the value's
    text and not keys or tables of their own."""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is not None:
            if ch == "\\" and quote == '"':
                i += 2  # an escaped character, whatever it is
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if line.startswith('"""', i) or line.startswith("'''", i):
            quote = line[i : i + 3]
            i += 3
        elif ch == "#":
            break  # a comment runs to the end of the line
        elif ch == '"' or ch == "'":
            quote = ch
            i += 1
        elif ch in "[{":
            depth += 1
            i += 1
        elif ch in "]}":
            depth -= 1
            i += 1
        else:
            i += 1
    return depth, quote


def _toml_value_end(lines: list[str], start: int) -> int:
    """The index just past the value that begins on `lines[start]`: a multi-line array, inline table or
    triple-quoted string ends on the line that closes it, not on the line its key is on."""
    depth = 0
    quote: str | None = None
    for i in range(start, len(lines)):
        depth, quote = _toml_scan(lines[i], depth, quote)
        if quote is None and depth <= 0:
            return i + 1
    return len(lines)




@dataclass
class BlockPlan:
    key: str
    path: str
    state: str  # ok, skip or refuse
    reason: str = ""
    action: str = "none"  # create, append, replace or none
    new_text: str | None = None
    raw: bytes | None = None
    mode: int | None = None
    exists: bool = False
    command: str = ""
    body: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _toml_structure_problem(text: str) -> str | None:
    """Why `text` is not well-formed TOML, as far as a scanner can tell, or None.

    There is no ``tomllib`` on the minimum Python,  so this is the check that stands in
    for it.  A scanner cannot check every rule of TOML,  but it catches the corruption a
    writer like this one can do:  a value or a string that is never closed,  and a line
    left over from a value that was replaced.

    It reuses ``_toml_scan``,  which already carries the bracket depth and the open
    string across lines,  so a line inside a value is that value's text and not a key.
    """
    depth = 0
    quote: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        at_top = quote is None and depth <= 0
        depth, quote = _toml_scan(line, depth, quote)
        if not at_top:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _TOML_TABLE_RE.match(line):
            continue
        if not _TOML_KEY_RE.match(line):
            return "config.toml line %d is neither a table header nor a key" % lineno
    if quote is not None:
        return "config.toml has a string that is never closed"
    if depth != 0:
        return "config.toml has a bracket or a brace that is not closed"
    return None


def _has_top_level_key(text: str, name: str) -> bool:
    """True when `name` is a key at the top level of `text`, not inside a table.

    A key under a table belongs to that table,  and a regex over the whole file cannot
    tell the two apart:  a table-scoped `hooks` key is perfectly valid,  and refusing it
    would refuse a good file.  A table header therefore ENDS the top level,  and every
    key after one belongs to that table until the next header.
    """
    depth = 0
    quote: str | None = None
    in_table = False
    pattern = re.compile(r"^[ \t]*%s[ \t]*=" % re.escape(name))
    for line in text.splitlines():
        at_top = quote is None and depth <= 0
        depth, quote = _toml_scan(line, depth, quote)
        if at_top and _TOML_TABLE_RE.match(line):
            # A table header ends the top level:  the keys under it are that table's.
            in_table = True
            continue
        if at_top and pattern.match(line) and not in_table:
            return True
    return False


def _toml_problem(text: str) -> str | None:
    """Why `text` would not load as Kimi's config.toml, or None.  With tomllib (Python 3.11+) the new file has to
    parse; without it the structure is scanned,  which catches a value that was replaced
    and left a line behind,  and a value or a string that is never closed.  The one
    thing checked either way is that no top-level `hooks` key exists beside our array
    of tables."""
    try:
        import tomllib
    except ImportError:
        problem = _toml_structure_problem(text)
        if problem:
            return problem
    else:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return f"the new config.toml would not parse as TOML ({exc})"
    # The one thing checked either way:  a top-level `hooks` key would be redefined by
    # the [[hooks]] table this tool writes.
    if _has_top_level_key(text, "hooks"):
        return "config.toml has a top-level `hooks =` key, which a [[hooks]] table would redefine"
    return None


def plan_block(plat: Platform, paths: Paths) -> BlockPlan:
    """Read-only: what `apply` would do to the file that holds this platform's block."""
    path = block_path(plat, paths)
    bp = BlockPlan(
        plat.key, path, "ok", command=block_command(plat, paths), body=block_body(plat, paths)
    )
    if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
        bp.state, bp.reason = (
            "skip",
            f"~/{plat.platform_dir} does not exist (platform not installed)",
        )
        return bp
    text: str | None = None
    if os.path.islink(path):
        bp.state, bp.reason = "refuse", f"{path} is a symlink; not writing through it"
        return bp
    if os.path.lexists(path):
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                bp.state, bp.reason = "refuse", f"{path} is not a regular file"
                return bp
            bp.raw = _read_bytes(path)
            bp.mode, bp.exists = stat.S_IMODE(st.st_mode), True
            text = bp.raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            bp.state, bp.reason = "refuse", f"{path} cannot be read as UTF-8 text ({exc})"
            return bp
        if bp.mode is not None and not bp.mode & 0o200:
            bp.state, bp.reason = (
                "refuse",
                f"{path} is read-only (mode {bp.mode:04o}); not overriding the owner's choice",
            )
            return bp
    try:
        if plat.key == "clutch":
            if text is not None and text.strip() and not CP.is_list_patch(text):
                bp.state, bp.reason = (
                    "refuse",
                    f"{path} is not a top-level YAML list, so it is not a cordis patch; left alone",
                )
                return bp
            new, bp.action = CP.upsert(text, BLOCK_NAME, BLOCK_TOOL, bp.body)
        else:
            # Kimi rewrites its config.toml without comments, so the entry is found by what it says, not by markers
            new, bp.action, extra = TH.plan(
                text, BLOCK_NAME, BLOCK_TOOL, bp.body, block_fields(plat, paths), is_ours
            )
            bp.notes.extend(extra)
    except MB.BlockError as exc:
        bp.state, bp.reason = "refuse", f"{path}: {exc}"
        return bp
    bp.new_text = new
    if plat.key == "kimi":
        problem = _toml_problem(new)
        if problem:
            bp.state, bp.reason = "refuse", problem
    if bp.state == "ok" and bp.exists and not os.access(os.path.dirname(path), os.W_OK | os.X_OK):
        bp.state, bp.reason = "refuse", f"the directory {os.path.dirname(path)} is not writable"
    return bp


def find_kimi(home: str) -> str | None:
    """The `kimi` binary: the one on PATH, else the one Kimi Code installs under its own home (a clean shell or a
    launchd job has no ~/.local/bin on PATH, and ~/.local/bin/kimi is only a link to this file).  The home under
    test, never the real $KIMI_CODE_HOME."""
    found = shutil.which("kimi")
    if found:
        return found
    cand = os.path.join(home, ".kimi-code", "bin", "kimi")
    return cand if os.path.isfile(cand) and os.access(cand, os.X_OK) else None


def _kimi_doctor(text: str, timeout: float, home: str) -> tuple[str, str]:
    """(PASS | FAIL | WARN | SKIP, detail): `kimi doctor` on a scratch copy of config.toml, in a scratch home, with
    auto-install off, so the real config and the real binary are never touched."""
    kimi = find_kimi(home)
    if not kimi:
        return (
            "SKIP",
            "kimi was not found (not on PATH, not at ~/.kimi-code/bin/kimi), so `kimi doctor` was not run",
        )
    with tempfile.TemporaryDirectory(prefix="fleet-kimi-doctor-") as scratch:
        kh = os.path.join(scratch, "kh")
        os.makedirs(kh, mode=0o700)
        os.makedirs(os.path.join(scratch, "home"))
        with open(os.path.join(kh, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(os.path.join(kh, "tui.toml"), "w", encoding="utf-8") as fh:
            fh.write("[upgrade]\nauto_install = false\n")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.path.join(scratch, "home"),
            "KIMI_CODE_HOME": kh,
            "NO_COLOR": "1",
        }
        res = run_argv([kimi, "doctor"], b"", env, max(timeout, 60.0))
    if res.error or res.timed_out:
        return (
            "WARN",
            res.error
            or f"`kimi doctor` timed out after {max(timeout, 60.0):g}s; the file was not validated",
        )
    out = res.stdout.decode("utf-8", "replace")
    if res.returncode == 0 and "All checked config files are valid" in out:
        return "PASS", f"{kimi} doctor accepts the config.toml"
    return (
        "FAIL",
        f"{kimi} doctor rejects the config.toml (exit {res.returncode}): {_brief(res.stdout or res.stderr, 300)}",
    )


def _clutch_dsh(
    new_text: str, paths: Paths, dsh_bin: str | None, timeout: float
) -> tuple[str, str]:
    """(PASS | FAIL | SKIP, detail): the pinned engine's --dump-config on a scratch copy of the profiles."""
    binary = dsh_bin or DC.find_dsh(paths.home)
    if not binary:
        return "SKIP", (
            "the pinned dsh was not found at ~/apps/clutch-runtime/node_modules/.bin/dsh (pass --dsh PATH)"
        )
    ok, detail = DC.check_patch(
        binary,
        new_text,
        os.path.join(paths.home, ".clutch", "dsh", "profiles"),
        ("@deepseek-ai/dsh-hooks-claude-code",),
        timeout=max(timeout, DC.DEFAULT_TIMEOUT),
    )
    return ("PASS" if ok else "FAIL"), detail


def apply_block(
    plat: Platform,
    paths: Paths,
    *,
    now: Callable[[], float] = time.time,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    dsh_bin: str | None = None,
    check_dsh: bool = True,
    check_kimi: bool = True,
) -> Result:
    """Write this platform's marked block.  The fleet shim is probed right now (deny, allow and a `ps`), the new text
    is validated by the tool's own checker, a backup comes first, and the write is atomic."""
    bp = plan_block(plat, paths)
    if bp.state == "skip":
        return Result(plat.key, "skipped", bp.reason)
    if bp.state == "refuse":
        return Result(plat.key, "refused", bp.reason)
    if not (os.path.isfile(paths.fleet_shim) and os.access(paths.fleet_shim, os.X_OK)):
        return Result(
            plat.key,
            "refused",
            f"the fleet hook shim {paths.fleet_shim} is missing or not executable; "
            "run `apply tools` first",
        )
    if plat.key == "clutch":
        want = render_clutch_hooks(paths.stable).encode("utf-8")
        if _read_regular(paths.clutch_hooks) != want:
            return Result(
                plat.key,
                "refused",
                f"{paths.clutch_hooks} is missing or not current; run `apply tools` first",
            )
    checks = probe_command(
        bp.command,
        target=plat.key,
        fmt=plat.fmt,
        shape=plat.shape,
        home=paths.home,
        timeout=timeout,
        minimal_path=minimal_path,
        secret=True,
    )
    bad = [c for c in checks if c.status == "FAIL"]
    if bad:
        return Result(
            plat.key,
            "refused",
            "the installed fleet hook shim fails its probes, so it will not be wired in: "
            + bad[0].detail,
        )
    if bp.action == "none":
        return Result(
            plat.key,
            "unchanged",
            f"{bp.path} already has the "
            + ("entry" if bp.notes else "block")
            + "".join("; " + n for n in bp.notes),
        )
    assert bp.new_text is not None
    notes: list[str] = []
    if plat.key == "clutch":
        if check_dsh:
            status, detail = _clutch_dsh(bp.new_text, paths, dsh_bin, timeout)
            if status == "FAIL":
                return Result(
                    plat.key, "refused", f"the Clutch check does not accept the new patch: {detail}"
                )
            if status == "SKIP":
                return Result(
                    plat.key,
                    "refused",
                    f"{detail}; or pass --no-dsh-check to write the patch unproven",
                )
            notes.append(detail)
        else:
            notes.append("the patch was NOT checked with the engine (--no-dsh-check)")
    else:
        if check_kimi:
            status, detail = _kimi_doctor(bp.new_text, timeout, paths.home)
            if status == "FAIL":
                return Result(plat.key, "refused", detail)
            if status == "SKIP":
                return Result(
                    plat.key,
                    "refused",
                    f"{detail}; or pass --no-kimi-check to write the entry unproven",
                )
            notes.append(detail)
        else:
            notes.append("config.toml was NOT checked with `kimi doctor` (--no-kimi-check)")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    backup: str | None = None
    try:
        os.makedirs(os.path.dirname(bp.path), exist_ok=True)
        if bp.exists:
            assert bp.raw is not None
            backup = _make_backup(bp.path, bp.raw, bp.mode, stamp)
        if not _reread_matches(bp.path, bp.raw or b"", bp.exists):
            if backup:
                os.unlink(backup)
            return Result(
                plat.key,
                "failed",
                f"{bp.path} changed while it was being edited; nothing written, retry",
            )
        atomic_write(
            bp.path, bp.new_text.encode("utf-8"), bp.mode if bp.mode is not None else 0o600
        )
    except OSError as exc:
        return Result(
            plat.key, "failed", f"{bp.path} not changed: {type(exc).__name__}: {exc}", backup
        )
    again = plan_block(plat, paths)
    if again.state != "ok" or again.action != "none":
        try:
            if bp.exists and bp.raw is not None:
                atomic_write(bp.path, bp.raw, bp.mode if bp.mode is not None else 0o600)
            else:
                os.unlink(bp.path)
        except OSError:
            pass
        return Result(
            plat.key,
            "failed",
            f"{bp.path} failed validation after the write; the original was restored",
            backup,
        )
    detail = bp.path + (f" (backup {backup})" if backup else " (new file)")
    notes = bp.notes + notes
    if notes:
        detail += "; " + "; ".join(notes)
    return Result(
        plat.key, "added" if bp.action in ("create", "append") else "updated", detail, backup
    )


def _block_command_from_file(plat: Platform, paths: Paths, text: str) -> tuple[str | None, str]:
    """(the hook command string, a problem).  Kimi: the `command = "..."` line inside our block.  Clutch: the command
    in the stable hooks.json."""
    if plat.key == "clutch":
        try:
            doc = json.loads(_read_bytes(paths.clutch_hooks).decode("utf-8"))
            cmd = doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            return None, f"cannot read the hook command from {paths.clutch_hooks}: {exc}"
        return (
            (cmd, "")
            if isinstance(cmd, str)
            else (None, f"{paths.clutch_hooks} has no command string")
        )
    try:
        info = TH.inspect(text, BLOCK_NAME, block_fields(plat, paths), is_ours)
    except MB.BlockError as exc:
        return None, str(exc)
    if not info.ours:
        return None, "no [[hooks]] entry of ours has a command line"
    cmd = info.ours[0].command
    return (cmd, "") if cmd else (None, "the fleet [[hooks]] entry has no readable command string")


def verify_block(
    plat: Platform,
    paths: Paths,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    dsh_bin: str | None = None,
) -> list[Check]:
    """Take the command back out of what is on disk and run it, as for every platform; then ask the tool's own checker
    (the pinned dsh for Clutch, `kimi doctor` for Kimi) about the file as it is now."""
    t = plat.key
    if not os.path.isdir(os.path.join(paths.home, *plat.platform_dir.split("/"))):
        return [
            Check(
                t,
                "platform",
                "SKIP",
                f"~/{plat.platform_dir} does not exist (platform not installed)",
            )
        ]
    path = block_path(plat, paths)
    try:
        text = _read_bytes(path).decode("utf-8")
    except FileNotFoundError:
        return [
            Check(
                t,
                "block",
                "SKIP",
                f"not wired: {path} does not exist, so there is no `{BLOCK_NAME}` block; run `apply {t}`",
            )
        ]
    except (OSError, UnicodeDecodeError) as exc:
        return [
            Check(
                t,
                "block",
                "FAIL",
                f"{path} cannot be read ({exc}); no `{BLOCK_NAME}` block installed",
            )
        ]
    info: TH.Inspection | None = None
    try:
        if plat.key == "kimi":
            # Kimi rewrites config.toml without comments, so the markers can be gone while the hook is still wired:
            # the entry is found by what it says (see toml_hooks)
            info = TH.inspect(text, BLOCK_NAME, block_fields(plat, paths), is_ours)
            body = MB.block_body(text, BLOCK_NAME) if info.state == "block" else None
            not_wired = info.state == "none"
        else:
            body = MB.block_body(text, BLOCK_NAME)
            not_wired = body is None
    except MB.BlockError as exc:
        return [Check(t, "block", "FAIL", f"{path}: {exc}")]
    if not_wired:
        # Opt-in, like muse: the tool is installed but nobody ran `apply {t}`, which is not a broken install.  A bare
        # verify reads it as a skip; naming the platform turns that lone SKIP into a FAIL (see _verify_targets).
        return [
            Check(
                t, "block", "SKIP", f"not wired: no `{BLOCK_NAME}` block in {path}; run `apply {t}`"
            )
        ]
    out: list[Check] = []
    if info is not None and info.state == "stripped":
        if info.current:
            out.append(
                Check(
                    t,
                    "block",
                    "WARN",
                    f"{path} holds the current fleet [[hooks]] entry, but its `{BLOCK_NAME}` "
                    "markers are gone (Kimi Code rewrites config.toml without comments); "
                    "the hook is still wired",
                )
            )
        else:
            out.append(
                Check(
                    t,
                    "block",
                    "FAIL",
                    f"the fleet [[hooks]] entr{'y' if len(info.ours) == 1 else 'ies'} in {path} "
                    f"({len(info.ours)}) is not what this install_tools writes; re-run `apply {t}`",
                )
            )
    elif tuple(body or ()) != tuple(block_body(plat, paths)):
        out.append(
            Check(
                t,
                "block",
                "FAIL",
                f"the block in {path} is not what this install_tools writes; re-run `apply {t}`",
            )
        )
    else:
        out.append(Check(t, "block", "PASS", f"{path} holds the current `{BLOCK_NAME}` block"))
    if info is not None and info.state == "block" and info.outside:
        out.append(
            Check(
                t,
                "duplicate",
                "WARN",
                f"{len(info.outside)} more fleet [[hooks]] entr"
                f"{'y' if len(info.outside) == 1 else 'ies'} outside the block in {path}; "
                f"re-run `apply {t}` to remove them",
            )
        )
    if info is not None and info.other_refs:
        out.append(
            Check(
                t,
                "duplicate",
                "WARN",
                f"{path} names the fleet hook again on line "
                + ", ".join(str(n) for n in info.other_refs)
                + " outside any [[hooks]] entry",
            )
        )
    if plat.key == "clutch":
        want = render_clutch_hooks(paths.stable).encode("utf-8")
        have = _read_regular(paths.clutch_hooks)
        out.append(
            Check(
                t,
                "hooks.json",
                "PASS" if have == want else "FAIL",
                f"{paths.clutch_hooks} "
                + (
                    "is current"
                    if have == want
                    else "is missing or not what this install_tools writes; re-run `apply tools`"
                ),
            )
        )
    cmd, problem = _block_command_from_file(plat, paths, text)
    if cmd is None:
        out.append(Check(t, "hook command", "FAIL", problem))
    else:
        out.append(Check(t, "hook command", "PASS", cmd))
        out.extend(
            probe_command(
                cmd,
                target=t,
                fmt=plat.fmt,
                shape=plat.shape,
                home=paths.home,
                timeout=timeout,
                minimal_path=minimal_path,
                secret=True,
            )
        )
    if plat.key == "clutch":
        status, detail = _clutch_dsh(text, paths, dsh_bin, timeout)
        out.append(Check(t, "dsh --dump-config", status if status != "SKIP" else "SKIP", detail))
        out.append(
            Check(
                t,
                "patch reload",
                "SKIP",
                DC.reload_note(os.path.join(paths.home, ".clutch", "dsh", "profiles"))
                + "; whether the running engine has loaded the plugin is not visible from here",
            )
        )
    else:
        problem = _toml_problem(text)
        out.append(
            Check(
                t,
                "config.toml",
                "FAIL" if problem else "PASS",
                problem or "parses (or no tomllib to ask)",
            )
        )
        status, detail = _kimi_doctor(text, timeout, paths.home)
        out.append(Check(t, "kimi doctor", status, detail))
        out.append(
            Check(
                t,
                "fires",
                "SKIP",
                "whether Kimi Code fires the hook is not visible from here; ask a new "
                "session to run `ps` and expect a refusal",
            )
        )
    return out


# --------------------------------------------------------------------------- MiniMax Code: a plugin bundle in ~/.minimax/plugins


def render_minimax_manifest() -> str:
    """.claude-plugin/plugin.json.  MiniMax needs at least a name; the other two keys are the Claude plugin manifest's."""
    doc = {
        "name": MINIMAX_PLUGIN_ID,
        "version": "2.0.0",
        "description": "MiniMax shell and file guards (written by fleet_lanes.install_tools)",
    }
    return json.dumps(doc, indent=2) + "\n"


def minimax_hook_command(paths: Paths) -> str:
    """MM-owned adapter, compatible deny shape, absolute stable path."""
    return shlex.quote(os.path.join(paths.stable, MINIMAX_HOOK_NAME))


def render_minimax_hooks(paths: Paths) -> str:
    """hooks/hooks.json.  Only `type: command` exists; `timeout` is whole seconds, 1 to 10."""
    doc = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Read|Write|Edit|bash|read|write|edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": minimax_hook_command(paths),
                            "timeout": MINIMAX_HOOK_TIMEOUT_S,
                        }
                    ],
                }
            ]
        }
    }
    return json.dumps(doc, indent=2) + "\n"


def minimax_expected(paths: Paths) -> dict[str, str]:
    """Relative path (under the bundle) to text."""
    return {
        MINIMAX_MANIFEST_FILE: render_minimax_manifest(),
        MINIMAX_HOOKS_FILE: render_minimax_hooks(paths),
    }


def registry_tree(registry: str) -> str:
    """The first registered ~/Code folder name.  Probes follow the registry, not a literal.

    The registry is required input to a security probe, so an unreadable, unparseable, or
    misshapen one is refused by name instead of degrading to no tree: silently returning ""
    would drop the integration-tree deny probes and let the probe pass without ever running them.
    """
    try:
        payload = json.loads(_read_bytes(registry).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(
            f"required integration registry is unavailable or invalid: {registry}"
        ) from exc
    if not isinstance(payload, dict):
        raise Refused(f"required integration registry {registry}: top level must be an object")
    apps = payload.get("apps")
    if not isinstance(apps, list):
        raise Refused(f"required integration registry {registry}: 'apps' must be a list")
    for app in apps:
        if not isinstance(app, dict):
            raise Refused(f"required integration registry {registry}: app entries must be objects")
        for candidate in (app.get("codeDir"), app.get("repo")):
            if candidate is None:
                continue
            if not isinstance(candidate, str):
                raise Refused(
                    f"required integration registry {registry}: app paths must be strings"
                )
            # Only the folder name under ~/Code identifies the tree: a registry value
            # spelled as a path or `owner/name` would otherwise probe outside ~/Code.
            name = os.path.basename(candidate.strip().rstrip("/"))
            if name and name not in (".", ".."):
                return name
    raise Refused(f"required integration registry {registry} contains no registered tree")


def probe_minimax_files(
    command: str,
    home: str,
    registry: str,
    timeout: float,
    minimal_path: bool,
    source_registry: str | None = None,
) -> list[Check]:
    """Run MM's compatible file payloads in its restricted subprocess environment.

    `source_registry` names the registry the operator has to repair when `registry` is a staged
    copy that is deleted with the stage; it is reported with the refusal.
    """
    env = {k: os.environ[k] for k in _MINIMAX_HOOK_ENV_KEYS if k in os.environ and k != "HOME"}
    env.update(HOME=home, PATH=MINIMAL_PATH if minimal_path else os.environ.get("PATH", ""))
    try:
        tree = registry_tree(registry)
    except Refused as exc:
        # An unreadable/invalid registry is a failed check, not an abort: letting
        # Refused escape here unwinds through apply_minimax and skips the
        # remaining platforms in `apply all`.
        detail = str(exc)
        if source_registry and source_registry != registry:
            # The staged copy is removed with the stage, so its path is not actionable:
            # name the registry the operator must repair.
            detail = f"{detail}; source registry: {source_registry}"
        return [Check("minimax", "registry-tree", "FAIL", detail)]
    integration = os.path.join("Code", tree, "probe-placeholder")
    out = []
    for tool, path, deny in (
        ("Read", ".secrets/probe-placeholder", True),
        ("Read", ".env", True),
        ("Write", integration, True),
        ("Edit", ".secrets/probe-placeholder", True),
        ("Read", "public-placeholder", False),
        ("Write", "public-placeholder", False),
        ("read", ".secrets/probe-placeholder", True),
        ("write", integration, True),
        ("edit", ".secrets/probe-placeholder", True),
    ):
        field = "path" if tool in ("read", "write") else "file_path"
        payload = {"tool_name": tool, "tool_input": {field: os.path.join(home, path)}, "cwd": home}
        res = run_shell(command, json.dumps(payload).encode("utf-8"), env, timeout)
        problem = res.error or ("timed out" if res.timed_out else None)
        if not problem and res.returncode != 0:
            problem = f"exit {res.returncode}, expected 0"
        if not problem:
            problem = (
                check_deny_output("claude", res.stdout, "MiniMax")
                if deny
                else ("unexpected output" if res.stdout.strip() or res.stderr.strip() else None)
            )
        missing_sources = [
            source.decode("ascii")
            for source in (b"SECRET_FILE_PATTERNS", b"FLEET_APPS_JSON")
            if source in res.stdout
        ]
        if not problem and missing_sources:
            problem = "required file policy is unavailable or invalid: " + ", ".join(missing_sources)
        out.append(
            Check(
                "minimax",
                f"{tool}/{os.path.basename(path)}/{'deny' if deny else 'allow'}/hook-env",
                "FAIL" if problem else "PASS",
                problem or f"compatible file payload checked ({path})",
            )
        )
    return out


@dataclass
class MinimaxPlan:
    bundle: str
    state: str  # new, stale, current, skip, refuse
    reason: str
    command: str
    files: list[FileStatus]
    manifest_text: str
    hooks_text: str
    notes: list[str]


def _minimax_foreign(bundle: str) -> str | None:
    """None when `bundle` is a directory this tool could have written (only our two files, no symlink, a manifest that
    names our plugin when there is one), else why it is not."""
    if os.path.islink(bundle):
        return f"{bundle} is a symlink; MiniMax Code skips a symlinked plugin and this tool will not write through one"
    if not os.path.isdir(bundle):
        return f"{bundle} exists and is not a directory"
    allowed_files = {MINIMAX_MANIFEST_FILE, MINIMAX_HOOKS_FILE}
    allowed_dirs = {rel.split("/")[0] for rel in allowed_files}
    for dirpath, dirnames, filenames in os.walk(bundle):
        here = os.path.relpath(dirpath, bundle)
        for d in dirnames:
            rel = d if here == "." else here + "/" + d
            if os.path.islink(os.path.join(dirpath, d)):
                return f"{bundle} holds a symlink ({rel}); it is not a directory this tool wrote"
            if rel not in allowed_dirs:
                return f"{bundle} holds {rel}/, which this tool never writes"
        for f in filenames:
            rel = f if here == "." else here + "/" + f
            full = os.path.join(dirpath, f)
            if f == ".DS_Store" and not os.path.islink(full):
                continue  # Finder litter, as in the stable-copy scan; the swap removes it with the old directory
            if rel not in allowed_files or os.path.islink(full):
                return f"{bundle} holds {rel}, which this tool never writes"
    # The manifest is the ownership marker: an empty directory, or one without it, is somebody else's.
    manifest = _read_regular(os.path.join(bundle, *MINIMAX_MANIFEST_FILE.split("/")))
    if manifest is None:
        return (
            f"{bundle}/{MINIMAX_MANIFEST_FILE} is missing or not a regular file, "
            "so this tool cannot tell the directory is its own"
        )
    try:
        doc = json.loads(manifest.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return f"{bundle}/{MINIMAX_MANIFEST_FILE} is not JSON"
    if not isinstance(doc, dict) or doc.get("name") != MINIMAX_PLUGIN_ID:
        return f"{bundle}/{MINIMAX_MANIFEST_FILE} does not name the plugin {MINIMAX_PLUGIN_ID!r}"
    return None


def _sweep_stale_minimax_leftovers(plugins_real: str, stage_parent: str, bundle: str) -> None:
    """Remove `<bundle>.old-<pid>` and `.fleet-guard-stage-*` directories a killed run left behind.  Only ones older
    than a minute, so a run that is still going is never touched."""
    cutoff = time.time() - 60
    candidates: list[str] = []
    try:
        base = os.path.basename(bundle) + ".old-"
        candidates += [
            os.path.join(plugins_real, n) for n in os.listdir(plugins_real) if n.startswith(base)
        ]
        candidates += [
            os.path.join(stage_parent, n)
            for n in os.listdir(stage_parent)
            if n.startswith(".fleet-guard-stage-")
        ]
    except OSError:
        return
    for c in candidates:
        try:
            if os.path.isdir(c) and not os.path.islink(c) and os.lstat(c).st_mtime < cutoff:
                shutil.rmtree(c, ignore_errors=True)
        except OSError:
            pass


def plan_minimax(paths: Paths, *, follow_symlinks: bool = False) -> MinimaxPlan:
    """Read-only: what `apply minimax` writes into ~/.minimax/plugins/fleet-guard, compared with what is there."""
    bundle = paths.minimax_bundle
    want = minimax_expected(paths)
    cmd = minimax_hook_command(paths)
    files = [FileStatus(rel, "new") for rel in want]

    def plan(state: str, reason: str = "", notes: Sequence[str] = ()) -> MinimaxPlan:
        return MinimaxPlan(
            bundle,
            state,
            reason,
            cmd,
            files,
            want[MINIMAX_MANIFEST_FILE],
            want[MINIMAX_HOOKS_FILE],
            list(notes),
        )

    if not os.path.isdir(os.path.join(paths.home, MINIMAX_PLUGIN_REL.split("/")[0])):
        return plan("skip", "~/.minimax does not exist (platform not installed)")
    refusal, outside, link = _link_check(bundle, paths.home, MINIMAX_PLUGIN_REL, follow_symlinks)
    if refusal:
        return plan("refuse", refusal)
    notes = [link] if link else []
    if os.path.lexists(bundle):
        foreign = _minimax_foreign(bundle)
        if foreign:
            return plan("refuse", foreign)
        files = []
        for rel, text in want.items():
            have = _read_regular(os.path.join(bundle, *rel.split("/")))
            files.append(
                FileStatus(
                    rel,
                    "same"
                    if have == text.encode("utf-8")
                    else ("new" if have is None else "changed"),
                )
            )
        state = "current" if all(f.status == "same" for f in files) else "stale"
    else:
        state = "new"
    if state != "current" and outside:
        return plan("refuse", outside, notes)
    return plan(state, "", notes)


def apply_minimax(
    paths: Paths,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    shim_proven: bool = False,
    follow_symlinks: bool = False,
) -> Result:
    """Write the plugin directory.  It is built in a stage directory beside `plugins/` (so MiniMax never scans a half-written
    plugin) and renamed into place; an existing directory is replaced only when this tool wrote it.  Like every platform,
    it is wired in only if the installed fleet hook passes its own probes right now."""
    mp = plan_minimax(paths, follow_symlinks=follow_symlinks)
    if mp.state == "skip":
        return Result("minimax", "skipped", mp.reason)
    if mp.state == "refuse":
        return Result("minimax", "refused", mp.reason)
    if not shim_proven:
        if not (
            os.path.isfile(os.path.join(paths.stable, MINIMAX_HOOK_NAME))
            and os.access(os.path.join(paths.stable, MINIMAX_HOOK_NAME), os.X_OK)
        ):
            return Result(
                "minimax",
                "refused",
                f"the MiniMax hook {minimax_hook_command(paths)} is missing or not executable; run `apply tools` first",
            )
        bad = [
            c
            for c in probe_command(
                mp.command,
                target="minimax",
                fmt="claude",
                shape="claude",
                home=paths.home,
                timeout=timeout,
                minimal_path=minimal_path,
                secret=True,
            )
            if c.status == "FAIL"
        ]
        bad.extend(
            c
            for c in probe_minimax_files(
                mp.command, paths.home, paths.registry, timeout, minimal_path
            )
            if c.status == "FAIL"
        )
        if bad:
            return Result(
                "minimax",
                "refused",
                "the installed fleet hook fails its probes, so it will not be wired in: "
                + bad[0].detail,
            )
    if mp.state == "current":
        return Result("minimax", "unchanged", f"{mp.bundle} already holds the plugin")
    want = minimax_expected(paths)
    stage: str | None = None
    try:
        os.makedirs(os.path.dirname(mp.bundle), exist_ok=True)
        # Stage beside the REAL plugins/ directory: when plugins/ is a symlink to another filesystem, a stage under
        # ~/.minimax would make the final rename fail with EXDEV.
        plugins_real = os.path.realpath(os.path.dirname(mp.bundle))
        stage_parent = os.path.dirname(plugins_real)
        _sweep_stale_minimax_leftovers(plugins_real, stage_parent, mp.bundle)
        stage = tempfile.mkdtemp(prefix=".fleet-guard-stage-", dir=stage_parent)
        _write_tree(stage, {rel: text.encode("utf-8") for rel, text in want.items()})
        _swap_dir(stage, mp.bundle)
        stage = None
    except OSError as exc:
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
        return Result("minimax", "failed", f"{mp.bundle} not written: {type(exc).__name__}: {exc}")
    for rel, text in want.items():
        if _read_regular(os.path.join(mp.bundle, *rel.split("/"))) != text.encode(
            "utf-8"
        ) or os.path.islink(mp.bundle):
            return Result(
                "minimax",
                "failed",
                f"{mp.bundle}/{rel} is not what was written; check it and re-run `apply minimax`",
            )
    return Result(
        "minimax",
        "added" if mp.state == "new" else "updated",
        f"{mp.bundle} (a real directory: {MINIMAX_MANIFEST_FILE}, {MINIMAX_HOOKS_FILE})",
    )


# What MiniMax gives a hook process: a small allowlist, and nothing of the caller's shell (so no AGENT_SEAT and no kill switch).
_MINIMAX_HOOK_ENV_KEYS = ("HOME", "LANG", "TERM", "SHELL", "USER", "TMPDIR")


def _minimax_hook_from_file(path: str, paths: Paths) -> tuple[str | None, list[Check]]:
    """(the hook command, checks) read back out of the written hooks.json, with every shape rule MiniMax applies."""
    t = "minimax"
    raw = _read_regular(path)
    if raw is None:
        return None, [
            Check(
                t,
                "hooks.json",
                "FAIL",
                f"{path} is missing or not a regular file; run `apply minimax`",
            )
        ]
    try:
        doc = json.loads(raw.decode("utf-8"))
        groups = doc["hooks"]["PreToolUse"]
        if not isinstance(groups, list) or len(groups) != 1:
            raise ValueError(
                f"expected exactly 1 PreToolUse group, found {len(groups) if isinstance(groups, list) else type(groups).__name__}"
            )
        group = groups[0]
        if group.get("matcher") != "Bash|Read|Write|Edit|bash|read|write|edit":
            raise ValueError(
                f"matcher is {group.get('matcher')!r}, expected 'Bash|Read|Write|Edit|bash|read|write|edit'"
            )
        hooks = group.get("hooks")
        if not isinstance(hooks, list) or len(hooks) != 1:
            raise ValueError("expected exactly 1 hook in the group")
        hook = hooks[0]
        if hook.get("type") != "command":
            raise ValueError(f"hook type is {hook.get('type')!r}; MiniMax only runs 'command'")
        timeout = hook.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 10:
            raise ValueError(f"timeout is {timeout!r}; MiniMax wants whole seconds, 1 to 10")
        cmd = hook.get("command")
        if not isinstance(cmd, str) or not is_ours(cmd):
            raise ValueError("the hook command is not the fleet hook")
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
        return None, [Check(t, "hooks.json", "FAIL", f"{path}: {exc}")]
    out = [
        Check(
            t,
            "hooks.json",
            "PASS",
            f"PreToolUse, matcher Bash|Read|Write|Edit|bash|read|write|edit, type command, timeout {timeout}s",
        )
    ]
    if raw != render_minimax_hooks(paths).encode("utf-8"):
        out.append(
            Check(
                t,
                "hooks.json current",
                "FAIL",
                f"{path} differs from what this install_tools writes; re-run `apply minimax`",
            )
        )
        return None, out
    return cmd, out


def verify_minimax(
    paths: Paths, *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True
) -> list[Check]:
    """Prove the plugin denies, without MiniMax.  The bundle must be a REAL directory (MiniMax skips symlinks) whose plugin.json
    has a name; the hook command is taken back out of the written hooks.json and run the way a shell runs it, with a lane
    deny, a secret deny and an allow, and once more in the small environment MiniMax gives a hook.  What this cannot see
    is whether MiniMax Code fires the hook."""
    t = "minimax"
    if not os.path.isdir(os.path.join(paths.home, ".minimax")):
        return [Check(t, "platform", "SKIP", "~/.minimax does not exist (platform not installed)")]
    bundle = paths.minimax_bundle
    if not os.path.lexists(bundle):
        return [
            Check(t, "bundle", "SKIP", f"not wired: {bundle} does not exist; run `apply minimax`")
        ]
    if os.path.islink(bundle) or not os.path.isdir(bundle):
        return [
            Check(
                t,
                "bundle",
                "FAIL",
                f"{bundle} is not a real directory (MiniMax Code skips a symlinked plugin); "
                "move it aside and run `apply minimax`",
            )
        ]
    out: list[Check] = [Check(t, "bundle", "PASS", f"{bundle} is a real directory")]
    raw = _read_regular(paths.minimax_manifest)
    if raw is None:
        out.append(
            Check(
                t,
                "plugin.json",
                "FAIL",
                f"{paths.minimax_manifest} is missing or not a regular file; run `apply minimax`",
            )
        )
    else:
        try:
            doc = json.loads(raw.decode("utf-8"))
            name = doc.get("name") if isinstance(doc, dict) else None
        except (ValueError, UnicodeDecodeError):
            name = None
        if not isinstance(name, str) or not name:
            out.append(
                Check(
                    t,
                    "plugin.json",
                    "FAIL",
                    f"{paths.minimax_manifest} has no `name`; MiniMax cannot load the plugin",
                )
            )
        elif raw != render_minimax_manifest().encode("utf-8"):
            out.append(
                Check(
                    t,
                    "plugin.json",
                    "FAIL",
                    f"{paths.minimax_manifest} differs from what this install_tools writes; re-run `apply minimax`",
                )
            )
        else:
            out.append(Check(t, "plugin.json", "PASS", f"name={name!r}"))
    out.append(
        _check_shim(
            t,
            MINIMAX_HOOK_NAME,
            os.path.join(paths.stable, MINIMAX_HOOK_NAME),
            render_minimax_hook_shim(),
        )
    )
    cmd, got = _minimax_hook_from_file(paths.minimax_hooks, paths)
    out.extend(got)
    if cmd is None:
        out.append(
            Check(t, "hook-env probes", "FAIL", "not run: no usable hook command in hooks.json")
        )
        return out
    out.append(Check(t, "hook command", "PASS", cmd))
    out.extend(
        probe_command(
            cmd,
            target=t,
            fmt="claude",
            shape="claude",
            home=paths.home,
            timeout=timeout,
            minimal_path=minimal_path,
            secret=True,
        )
    )
    env = {k: os.environ[k] for k in _MINIMAX_HOOK_ENV_KEYS if k in os.environ and k != "HOME"}
    env["HOME"] = paths.home
    env["PATH"] = MINIMAL_PATH if minimal_path else os.environ.get("PATH", "")
    for name, command, marker in (
        ("deny/hook-env", DENY_COMMAND, DENY_MARKER),
        ("secret/hook-env", SECRET_DENY_COMMAND, SECRET_DENY_MARKER),
    ):
        res = run_shell(
            cmd,
            json.dumps(probe_payload("claude", command, paths.home)).encode("utf-8"),
            env,
            timeout,
        )
        if res.error:
            problem: str | None = res.error
        elif res.timed_out:
            problem = f"timed out after {timeout:g}s"
        elif res.returncode != 0:
            problem = f"exit {res.returncode}, expected 0"
        else:
            problem = check_deny_output("claude", res.stdout, marker)
        out.append(
            Check(
                t,
                name,
                "FAIL" if problem else "PASS",
                (f"{problem}; " if problem else "")
                + f"exit={res.returncode} stdout={_brief(res.stdout)}",
            )
        )
    # The verify CLI surfaces an invalid registry as a structured refusal
    # (main prints `REFUSED ...`), not a downgraded check FAIL: verify runs a
    # single platform, so the `apply all` unwind the downgrade inside
    # probe_minimax_files protects against does not apply here.
    registry_tree(paths.registry)
    out.extend(probe_minimax_files(cmd, paths.home, paths.registry, timeout, minimal_path))
    switch, _off = guard_switch()
    if _is_off(os.environ.get(switch, "")):
        out.append(
            Check(
                t,
                "guard-switch-env",
                "WARN",
                f"{switch}={os.environ.get(switch)!r} is set in the environment this command runs in: the probes scrub it, "
                "and MiniMax gives a hook no such variable",
            )
        )
    out.append(
        Check(
            t,
            "fires",
            "SKIP",
            "whether MiniMax Code fires the hook is not visible from here; ask a new session to "
            "run a process listing and expect a refusal",
        )
    )
    return out


# --------------------------------------------------------------------------- Copilot CLI: a fleet-owned hooks file


def render_copilot_hooks(paths: Paths) -> str:
    """~/.copilot/hooks/fleet-lane-guard.json.  Version 1, one PreToolUse group with matcher Bash and one command
    hook (timeoutSec 5), running the fleet hook in the Claude contract."""
    doc = {
        "version": 1,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": fleet_hook_command(paths, "claude"),
                            "timeoutSec": HOOK_TIMEOUT_S,
                        }
                    ],
                }
            ]
        },
    }
    return json.dumps(doc, indent=2) + "\n"


def copilot_expected(paths: Paths) -> dict:
    return json.loads(render_copilot_hooks(paths))


def _copilot_foreign(doc: object, paths: Paths) -> str | None:
    """Why a file at our own hooks path is not one this tool wrote, or None.  A file that holds any hook that is not
    ours, any other event, or any other top-level key is somebody else's and is left alone."""
    if not isinstance(doc, dict):
        return "it is JSON but not an object"
    if doc == copilot_expected(paths) or doc == {}:
        return None
    if set(doc) - {"version", "hooks"}:
        return (
            "it holds top-level keys this tool never writes ("
            + ", ".join(sorted(set(doc) - {"version", "hooks"}))
            + ")"
        )
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return "its hooks value is not an object"
    if set(hooks) - {"PreToolUse"}:
        return (
            "it holds hook events this tool never writes ("
            + ", ".join(sorted(set(hooks) - {"PreToolUse"}))
            + ")"
        )
    total = 0
    for group in hooks.get("PreToolUse") or []:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list):
            return "a PreToolUse group holds no hook list"
        for hook in inner:
            total += 1
            if not isinstance(hook, dict) or not is_ours(hook.get("command")):
                return "it holds a hook this tool did not write"
    if total == 0:
        return "it holds no fleet hook and is not a file this tool wrote"
    return None


@dataclass
class CopilotPlan:
    path: str
    state: str  # new, stale, current, skip, refuse
    reason: str
    command: str
    text: str
    notes: list[str] = field(default_factory=list)
    raw: bytes | None = None
    mode: int | None = None
    exists: bool = False


def plan_copilot(paths: Paths, *, follow_symlinks: bool = False) -> CopilotPlan:
    """Read-only: what `apply copilot` writes into ~/.copilot/hooks/fleet-lane-guard.json."""
    path = paths.copilot_hooks
    cmd = fleet_hook_command(paths, "claude")
    text = render_copilot_hooks(paths)

    def plan(state: str, reason: str = "", notes: Sequence[str] = ()) -> CopilotPlan:
        return CopilotPlan(path, state, reason, cmd, text, list(notes))

    if not os.path.isdir(os.path.join(paths.home, ".copilot")):
        return plan("skip", "~/.copilot does not exist (platform not installed)")
    refusal, outside, link = _link_check(path, paths.home, COPILOT_HOOKS_REL, follow_symlinks)
    if refusal:
        return plan("refuse", refusal)
    notes = [link] if link else []
    if os.path.islink(path):
        return plan(
            "refuse", f"{path} is a symlink; this tool writes a real file there, not through a link"
        )
    if os.path.lexists(path):
        if not os.path.isfile(path):
            return plan("refuse", f"{path} exists and is not a regular file")
        try:
            cfg = load_config(path)
        except Refused as exc:
            return plan("refuse", str(exc))
        foreign = _copilot_foreign(cfg.doc, paths)
        if foreign:
            return plan("refuse", f"{path}: {foreign}; left alone")
        cp = plan("current" if cfg.doc == copilot_expected(paths) else "stale", "", notes)
        cp.raw, cp.mode, cp.exists = cfg.raw, cfg.mode, cfg.exists
        if cp.state != "current" and outside:
            return plan("refuse", outside, notes)
        return cp
    if outside:
        return plan("refuse", outside, notes)
    return plan("new", "", notes)


def apply_copilot(
    paths: Paths,
    *,
    now: Callable[[], float] = time.time,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    shim_proven: bool = False,
    follow_symlinks: bool = False,
) -> Result:
    """Write the fleet-owned Copilot hooks file, with a backup first, but only if the fleet hook denies right now."""
    cp = plan_copilot(paths, follow_symlinks=follow_symlinks)
    if cp.state == "skip":
        return Result("copilot", "skipped", cp.reason)
    if cp.state == "refuse":
        return Result("copilot", "refused", cp.reason)
    if not shim_proven:
        if not (os.path.isfile(paths.fleet_shim) and os.access(paths.fleet_shim, os.X_OK)):
            return Result(
                "copilot",
                "refused",
                f"the fleet hook {paths.fleet_shim} is missing or not executable; run `apply tools` first",
            )
        bad = [
            c
            for c in probe_command(
                cp.command,
                target="copilot",
                fmt="claude",
                shape="claude",
                home=paths.home,
                timeout=timeout,
                minimal_path=minimal_path,
                secret=True,
            )
            if c.status == "FAIL"
        ]
        if bad:
            return Result(
                "copilot",
                "refused",
                "the installed fleet hook fails its probes, so it will not be wired in: "
                + bad[0].detail,
            )
    if cp.state == "current":
        return Result("copilot", "unchanged", f"{cp.path} already holds the hook")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    backup: str | None = None
    # The lock covers the re-read,  the write,  the validation and the rollback,  so a
    # second installer cannot write between this check and this write.
    with _target_lock(paths.home, cp.path):
        try:
            _makedirs_private(os.path.dirname(cp.path))
            if cp.exists:
                assert cp.raw is not None
                backup = _make_backup(cp.path, cp.raw, cp.mode, stamp)
            if not _reread_matches(cp.path, cp.raw or b"", cp.exists):
                if backup:
                    os.unlink(backup)
                return Result(
                    "copilot",
                    "failed",
                    f"{cp.path} changed while it was being written; nothing written, retry",
                )
            atomic_write(cp.path, cp.text.encode("utf-8"), cp.mode if cp.mode is not None else 0o644)
        except OSError as exc:
            return Result(
                "copilot", "failed", f"{cp.path} not changed: {type(exc).__name__}: {exc}", backup
            )
        try:
            after = json.loads(_read_bytes(cp.path).decode("utf-8"))
            ok = after == copilot_expected(paths) and bool(locate(after, PLATFORMS["copilot"]))
        except (OSError, ValueError):
            ok = False
        if not ok:
            try:
                if cp.exists and cp.raw is not None:
                    atomic_write(cp.path, cp.raw, cp.mode if cp.mode is not None else 0o644)
                else:
                    os.unlink(cp.path)
            except OSError:
                pass
            return Result(
                "copilot",
                "failed",
                f"{cp.path} failed validation after the write; the original was restored",
                backup,
            )
    detail = cp.path + (f" (backup {backup})" if backup else " (new file)")
    return Result("copilot", "added" if cp.state == "new" else "updated", detail, backup)


def verify_copilot(
    paths: Paths, *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True
) -> list[Check]:
    """Take the command back out of the written hooks file and run it.  What this cannot see is whether Copilot
    fires a preToolUse hook, which is UNVERIFIED on this Mac and stays a SKIP."""
    t = "copilot"
    if not os.path.isdir(os.path.join(paths.home, ".copilot")):
        return [Check(t, "platform", "SKIP", "~/.copilot does not exist (platform not installed)")]
    path = paths.copilot_hooks
    if not os.path.lexists(path):
        return [
            Check(t, "hooks file", "SKIP", f"not wired: {path} does not exist; run `apply copilot`")
        ]
    if os.path.islink(path) or not os.path.isfile(path):
        return [Check(t, "hooks file", "FAIL", f"{path} is not a regular file")]
    out: list[Check] = [Check(t, "hooks file", "PASS", f"{path} is a regular file")]
    out.append(
        _check_shim(
            t,
            FLEET_HOOK_NAME,
            paths.fleet_shim,
            render_fleet_hook_shim(read_guard_facts(paths.stable)),
        )
    )
    try:
        cfg = load_config(path)
        entries = locate(cfg.doc, PLATFORMS["copilot"])
    except Refused as exc:
        out.append(Check(t, "hooks entry", "FAIL", str(exc)))
        return out
    if cfg.doc != copilot_expected(paths):
        out.append(
            Check(
                t,
                "hooks entry",
                "FAIL",
                f"{path} differs from what this install_tools writes; re-run `apply copilot`",
            )
        )
        return out
    if not entries or entries[0].problems:
        out.append(
            Check(
                t,
                "hooks entry",
                "FAIL",
                f"{path}: "
                + ("; ".join(entries[0].problems) if entries else "no fleet hook under PreToolUse"),
            )
        )
        return out
    out.append(Check(t, "hooks entry", "PASS", f"{entries[0].where} command={entries[0].command}"))
    out.extend(
        probe_command(
            entries[0].command,
            target=t,
            fmt="claude",
            shape="claude",
            home=paths.home,
            timeout=timeout,
            minimal_path=minimal_path,
            secret=True,
        )
    )
    switch, _off = guard_switch()
    if _is_off(os.environ.get(switch, "")):
        out.append(
            Check(
                t,
                "guard-switch-env",
                "WARN",
                f"{switch}={os.environ.get(switch)!r} is set in the environment this command runs in; the probes scrub it",
            )
        )
    out.append(
        Check(
            t,
            "fires",
            "SKIP",
            "whether Copilot CLI fires this PreToolUse hook is UNVERIFIED (no live preToolUse record on this "
            "Mac); the owner proof is a new Copilot session asked to run a process listing, which must be refused",
        )
    )
    return out


# --------------------------------------------------------------------------- OpenCode: a local plugin listed in opencode.json


def opencode_entry(paths: Paths) -> str:
    """The plugin array entry: the stable plugin file by absolute path."""
    return paths.opencode_plugin


def _is_opencode_ours(entry: object, paths: Paths) -> bool:
    want = opencode_entry(paths)
    if isinstance(entry, str):
        return entry == want or entry.endswith("/" + OPENCODE_PLUGIN_FILE)
    if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str):
        return entry[0] == want or entry[0].endswith("/" + OPENCODE_PLUGIN_FILE)
    return False


def opencode_merge(doc: dict, paths: Paths) -> tuple[dict, str]:
    """(new document, action): the plugin array holds our absolute path exactly once.  Every other key and entry
    is preserved as it is; a `plugin` value that is not a list raises ShapeError."""
    new = copy.deepcopy(doc)
    plugins = new.get("plugin")
    if plugins is None:
        plugins = new["plugin"] = []
    if not isinstance(plugins, list):
        raise ShapeError("'plugin' in opencode.json is not a list")
    want = opencode_entry(paths)
    ours = [i for i, e in enumerate(plugins) if _is_opencode_ours(e, paths)]
    if len(ours) == 1 and plugins[ours[0]] == want:
        return new, "unchanged"
    if not ours:
        plugins.append(want)
        return new, "added"
    first = ours[0]
    plugins[first] = want
    for i in reversed(ours[1:]):
        del plugins[i]
    return new, "updated"


@dataclass
class OpencodePlan:
    config: str
    entry: str
    state: str  # new, stale, current, skip, refuse
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    cfg: Loaded | None = None
    new_text: str | None = None
    plugin_text: str = ""


def plan_opencode(paths: Paths, *, follow_symlinks: bool = False) -> OpencodePlan:
    """Read-only: what `apply opencode` does to the plugin array of opencode.json.  Only the entry is described;
    no other key of that file (which holds credential-shaped values) is ever read into the output."""
    config = paths.opencode_config
    entry = opencode_entry(paths)
    plugin_text = render_opencode_plugin()

    def plan(state: str, reason: str = "", notes: Sequence[str] = ()) -> OpencodePlan:
        return OpencodePlan(config, entry, state, reason, list(notes), plugin_text=plugin_text)

    if not os.path.isdir(os.path.join(paths.home, ".config", "opencode")):
        return plan("skip", "~/.config/opencode does not exist (platform not installed)")
    refusal, outside, link = _link_check(config, paths.home, OPENCODE_CONFIG_REL, follow_symlinks)
    if refusal:
        return plan("refuse", refusal)
    notes = [link] if link else []
    try:
        cfg = load_config(config)
        new_doc, action = opencode_merge(cfg.doc, paths)
    except Refused as exc:
        return plan("refuse", str(exc))
    have_plugin = _read_regular(paths.opencode_plugin)
    if have_plugin is None:
        notes.append(
            f"{paths.opencode_plugin} is not in the stable dir yet; `apply tools` writes it"
        )
        plugin_state = "new"
    elif have_plugin != plugin_text.encode("utf-8"):
        notes.append(
            f"{paths.opencode_plugin} is not what this install_tools writes; `apply tools` refreshes it"
        )
        plugin_state = "stale"
    else:
        plugin_state = "current"
    state = {
        "added": "new" if not cfg.exists else "stale",
        "updated": "stale",
        "unchanged": "current",
    }[action]
    if state == "current" and plugin_state != "current":
        state = "stale"
    if state != "current":
        if outside:
            return plan("refuse", outside, notes)
        if cfg.unparseable_numbers:
            return plan(
                "refuse",
                f"{config} holds numbers JSON cannot represent ({_first(cfg.unparseable_numbers)}); "
                "strict parsers reject the file and a rewrite would change them; fix the file first",
            )
        if cfg.duplicate_keys:
            return plan(
                "refuse",
                f"{config} has duplicate key(s) {_first(cfg.duplicate_keys)} in one object; a rewrite would "
                "silently keep only the last value; fix the file first",
            )
        if cfg.lossy_numbers:
            return plan(
                "refuse",
                f"{config} holds number(s) a rewrite would change ({_first(cfg.lossy_numbers)}); refusing to touch it",
            )
        if cfg.exists and cfg.mode is not None and not cfg.mode & 0o200:
            return plan(
                "refuse",
                f"{config} is read-only (mode {cfg.mode:04o}); not overriding the owner's choice",
            )
    op = plan(state, "", notes)
    op.cfg = cfg
    op.new_text = dump_config(new_doc, cfg)
    return op


def apply_opencode(
    paths: Paths,
    *,
    now: Callable[[], float] = time.time,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    shim_proven: bool = False,
    follow_symlinks: bool = False,
) -> Result:
    """List the stable plugin by absolute path in opencode.json's plugin array.  A backup is owner-only (0600)
    whatever the file's mode, so a file that holds a token never gains a second readable copy."""
    op = plan_opencode(paths, follow_symlinks=follow_symlinks)
    if op.state == "skip":
        return Result("opencode", "skipped", op.reason)
    if op.state == "refuse":
        return Result("opencode", "refused", op.reason)
    if _read_regular(paths.opencode_plugin) != render_opencode_plugin().encode("utf-8"):
        return Result(
            "opencode",
            "refused",
            f"{paths.opencode_plugin} is missing or not current; run `apply tools` first",
        )
    if not shim_proven:
        if not (os.path.isfile(paths.fleet_shim) and os.access(paths.fleet_shim, os.X_OK)):
            return Result(
                "opencode",
                "refused",
                f"the fleet hook {paths.fleet_shim} is missing or not executable; run `apply tools` first",
            )
        bad = [
            c
            for c in probe_command(
                fleet_hook_command(paths, "claude"),
                target="opencode",
                fmt="claude",
                shape="claude",
                home=paths.home,
                timeout=timeout,
                minimal_path=minimal_path,
                secret=True,
            )
            if c.status == "FAIL"
        ]
        if bad:
            return Result(
                "opencode",
                "refused",
                "the installed fleet hook fails its probes, so it will not be wired in: "
                + bad[0].detail,
            )
    if op.state == "current":
        return Result("opencode", "unchanged", f"{op.config} already lists the plugin")
    cfg = op.cfg
    assert cfg is not None and op.new_text is not None
    try:
        reparsed = json.loads(op.new_text)
        _doc, act = opencode_merge(reparsed, paths)
        if act != "unchanged":
            return Result(
                "opencode", "refused", "the merged document does not hold the plugin entry"
            )
    except (ValueError, Refused) as exc:
        return Result("opencode", "refused", f"the merged document is not valid: {exc}")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    backup: str | None = None
    real = cfg.real
    # The lock covers the re-read,  the write,  the validation and the rollback,  so a
    # second installer cannot write between this check and this write.
    with _target_lock(paths.home, real):
        try:
            _makedirs_private(os.path.dirname(real))
            if cfg.exists:
                backup = _make_backup(real, cfg.raw, cfg.mode, stamp)
            if not _reread_matches(real, cfg.raw, cfg.exists):
                if backup:
                    os.unlink(backup)
                return Result(
                    "opencode",
                    "failed",
                    f"{op.config} changed while it was being merged; nothing written, retry",
                )
            atomic_write(real, op.new_text.encode("utf-8"), cfg.mode if cfg.mode is not None else 0o600)
        except OSError as exc:
            return Result(
                "opencode", "failed", f"{op.config} not changed: {type(exc).__name__}: {exc}", backup
            )
        try:
            after = json.loads(_read_bytes(real).decode("utf-8"))
            _doc, act = opencode_merge(after, paths)
            ok = act == "unchanged"
        except (OSError, ValueError, Refused):
            ok = False
        if not ok:
            try:
                if cfg.exists:
                    atomic_write(real, cfg.raw, cfg.mode if cfg.mode is not None else 0o600)
                else:
                    os.unlink(real)
            except OSError:
                pass
            return Result(
                "opencode",
                "failed",
                f"{op.config} failed validation after the write; the original was restored",
                backup,
            )
    detail = op.config + (f" (backup {backup})" if backup else " (new file)")
    return Result("opencode", "added" if not cfg.exists else "updated", detail, backup)


def verify_opencode(
    paths: Paths, *, timeout: float = PROBE_TIMEOUT_S, minimal_path: bool = True
) -> list[Check]:
    """The plugin entry is read back out of opencode.json and the plugin file is checked against what this tool
    writes; the guard command the plugin runs is then probed directly.  Whether OpenCode loads the plugin and
    whether permission.ask denies live are UNVERIFIED and stay a SKIP."""
    t = "opencode"
    if not os.path.isdir(os.path.join(paths.home, ".config", "opencode")):
        return [
            Check(
                t, "platform", "SKIP", "~/.config/opencode does not exist (platform not installed)"
            )
        ]
    if not os.path.isfile(paths.opencode_plugin):
        return [
            Check(
                t,
                "plugin file",
                "SKIP",
                f"not wired: {paths.opencode_plugin} does not exist; run `apply tools`",
            )
        ]
    out: list[Check] = []
    out.append(
        _check_text(t, "plugin file", paths.opencode_plugin, render_opencode_plugin(), False)
    )
    plugin_text = (_read_regular(paths.opencode_plugin) or b"").decode("utf-8", "replace")
    for needle, label in (
        ('"tool.execute.before"', "tool.execute.before hook"),
        ('"permission.ask"', "permission.ask hook"),
        ('output.status = "deny"', "permission.ask deny"),
        ("fleet-guard-hook", "stable fleet hook"),
    ):
        out.append(
            Check(
                t,
                label,
                "PASS" if needle in plugin_text else "FAIL",
                f"{paths.opencode_plugin} "
                + ("contains it" if needle in plugin_text else f"does not contain {needle}"),
            )
        )
    if "Authorization" in plugin_text or "Bearer" in plugin_text:
        out.append(
            Check(
                t,
                "no credential copy",
                "FAIL",
                "the plugin text names an authorization value; it must hold none",
            )
        )
    else:
        out.append(Check(t, "no credential copy", "PASS", "the plugin text holds no credential"))
    out.append(
        _check_shim(
            t,
            FLEET_HOOK_NAME,
            paths.fleet_shim,
            render_fleet_hook_shim(read_guard_facts(paths.stable)),
        )
    )
    config = paths.opencode_config
    if not os.path.exists(os.path.realpath(config)):
        out.append(Check(t, "config", "FAIL", f"{config} does not exist; no plugin listed"))
        return out
    try:
        cfg = load_config(config)
    except Refused as exc:
        out.append(Check(t, "config", "FAIL", str(exc)))
        return out
    if cfg.unparseable_numbers:
        out.append(
            Check(
                t,
                "config",
                "FAIL",
                f"{config} holds numbers JSON cannot represent ({_first(cfg.unparseable_numbers)}); "
                "the platform's JSON parser rejects the whole file, so no plugin loads",
            )
        )
        return out
    plugins = cfg.doc.get("plugin")
    if not isinstance(plugins, list):
        out.append(
            Check(
                t,
                "plugin entry",
                "FAIL",
                f"{config} has no plugin list; the guard plugin is not listed",
            )
        )
        return out
    ours = [e for e in plugins if _is_opencode_ours(e, paths)]
    want = opencode_entry(paths)
    if ours == [want]:
        out.append(Check(t, "plugin entry", "PASS", f"plugin array lists {want} exactly once"))
    elif not ours:
        out.append(
            Check(
                t,
                "plugin entry",
                "FAIL",
                f"no fleet plugin entry in {config}; run `apply opencode`",
            )
        )
        return out
    else:
        out.append(
            Check(
                t,
                "plugin entry",
                "FAIL",
                f"{config} lists the fleet plugin {len(ours)} times or by a stale path; re-run `apply opencode`",
            )
        )
        return out
    out.extend(
        probe_command(
            fleet_hook_command(paths, "claude"),
            target=t,
            fmt="claude",
            shape="claude",
            home=paths.home,
            timeout=timeout,
            minimal_path=minimal_path,
            secret=True,
        )
    )
    for problem in _switch_warnings(cfg.doc):
        out.append(Check(t, "hook-switch", "FAIL", f"{config}: {problem}"))
    out.append(
        Check(
            t,
            "fires",
            "SKIP",
            "whether OpenCode loads this local plugin and permission.ask denies a live shell call is "
            "UNVERIFIED; the owner proof is a new OpenCode session asked to run a process listing, which "
            "must be refused",
        )
    )
    return out


# --------------------------------------------------------------------------- fx: no hook surface (UNSUPPORTED, with a prepared control)


def render_fx_compensating_control() -> str:
    """The prepared fx compensating control, as JSON for the owner to merge into ~/.fx/settings.json by hand.
    It is a static permission.bash deny list over the temp-destination shapes guard.py proves, plus permission_mode
    off yolo.  The top-level `*` rule asks for every other call, so the deny list is the only thing that blocks
    and nothing else is silently allowed.  It cannot resolve d=$(mktemp -d), cannot read a repo's fleet status, and
    cannot see a bash -c layer."""
    roots = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp")
    phrases = ("git clone", "gh repo clone", "git worktree add", "git worktree move", "git archive")
    bash: dict[str, str] = {}
    for phrase in phrases:
        for root in roots:
            bash[f"*{phrase}* {root}/*"] = "deny"
    doc = {"permission_mode": "ask", "permission": {"*": "ask", "bash": bash}}
    return json.dumps(doc, indent=2) + "\n"


# --------------------------------------------------------------------------- command line


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python3 -m fleet_lanes.install_tools", description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--self-test",
        action="store_true",
        help="build a temp fake home, apply, verify, corrupt the guard, and assert verify FAILS",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=PROBE_TIMEOUT_S,
        help="seconds per probe subprocess (default 5)",
    )
    p.add_argument(
        "--no-minimal-path",
        action="store_true",
        help="skip the second probe run under PATH=/usr/bin:/bin:/usr/sbin:/sbin",
    )
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--home",
            default=None,
            help="home directory to read and write (default $HOME; must be an existing, non-empty path)",
        )
        sp.add_argument(
            "--stable-dir", default=None, help="stable dir (default <home>/apps/lane-tools)"
        )
        sp.add_argument(
            "--timeout", type=float, default=argparse.SUPPRESS, help="seconds per probe subprocess"
        )
        sp.add_argument(
            "--no-minimal-path",
            action="store_true",
            default=argparse.SUPPRESS,
            help="skip the second probe run under PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        )

    def sourcing(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--source",
            default=None,
            help="directory that contains fleet_lanes/ (default: this checkout)",
        )
        sp.add_argument(
            "--registry", default=None, help="fleet-apps.json to install (default: the source's)"
        )
        sp.add_argument(
            "--sha",
            default=None,
            help="source git sha to record in VERSION (default: git rev-parse HEAD)",
        )

    def writing(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--replace-extra",
            action="store_true",
            help="tools: also delete files in an existing lane-tools dir that this tool never writes (the dir must still be one it wrote)",
        )
        sp.add_argument(
            "--follow-symlinks",
            action="store_true",
            help="platforms: write through a config symlink that resolves outside --home",
        )
        sp.add_argument(
            "--dsh",
            default=None,
            metavar="PATH",
            help="clutch: the pinned engine binary for the --dump-config proof (default "
            "~/apps/clutch-runtime/node_modules/.bin/dsh)",
        )
        sp.add_argument(
            "--no-dsh-check",
            action="store_true",
            help="clutch: write the patch without the engine proof (an empty or broken patch stops the "
            "engine at its next start)",
        )
        sp.add_argument(
            "--no-kimi-check",
            action="store_true",
            help="kimi: write the entry without `kimi doctor` (needed only when no kimi binary is found)",
        )

    sp = sub.add_parser(
        "plan", help="read-only: show the files, shims, and the JSON merge as a diff"
    )
    common(sp)
    sourcing(sp)
    writing(sp)
    sp.add_argument(
        "targets",
        nargs="*",
        help="tools, a platform, or all (default: all, plus muse, clutch and kimi listed)",
    )
    sp = sub.add_parser(
        "apply", help="install tools and/or wire the hook into platforms, then verify"
    )
    common(sp)
    sourcing(sp)
    writing(sp)
    sp.add_argument("--no-verify", action="store_true", help="do not run verify after applying")
    sp.add_argument(
        "--strict",
        action="store_true",
        help="treat a WARN (for example Codex trust pending) as a failure",
    )
    sp.add_argument("targets", nargs="*", help="tools, a platform, or all (required)")
    sp = sub.add_parser("verify", help="prove the installed hooks deny and allow correctly")
    common(sp)
    sp.add_argument(
        "--strict",
        action="store_true",
        help="treat a WARN (for example Codex trust pending) as a failure",
    )
    sp.add_argument("targets", nargs="*", help="tools, a platform, or all (default: all)")
    return p


def _expand(raw: Sequence[str], default: Sequence[str]) -> tuple[list[str], set[str]]:
    """(ordered targets, explicitly named targets).  `all` is tools plus every supported platform plus the ones marked
    `in_all` (MiniMax Code).  `mm` is `minimax`."""
    valid = {"tools", "all"} | set(PLATFORM_ORDER) | set(PLATFORM_ALIASES)
    for n in raw:
        if n not in valid:
            raise SystemExit(
                _usage_error(
                    f"unknown target {n!r}; choose from tools, all, {', '.join(PLATFORM_ORDER)} "
                    f"(or {', '.join(PLATFORM_ALIASES)})"
                )
            )
    names = [PLATFORM_ALIASES.get(n, n) for n in (list(raw) or list(default))]
    explicit = {n for n in names if n != "all"} if raw else set()
    want: set[str] = set()
    for n in names:
        if n == "all":
            want.update(("tools",) + ALL_KEYS)
        else:
            want.add(n)
    order = [k for k in ("tools",) + PLATFORM_ORDER if k in want]
    return order, {PLATFORM_ALIASES.get(n, n) for n in explicit}


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
        raise SystemExit(
            _usage_error(f"{what} resolves to /; refusing to use the filesystem root as a home")
        )
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


def _verify_targets(
    targets: Sequence[str],
    explicit: set[str],
    paths: Paths,
    *,
    timeout: float,
    minimal_path: bool,
    strict: bool,
    emit: Callable[[str], None],
) -> int:
    all_checks: list[Check] = []
    for tgt in targets:
        got = (
            verify_tools(paths, timeout=timeout)
            if tgt == "tools"
            else verify_platform(PLATFORMS[tgt], paths, timeout=timeout, minimal_path=minimal_path)
        )
        # A platform that is not installed is a skip when reached through `all`, a FAIL when named.
        if tgt != "tools" and tgt in explicit and len(got) == 1 and got[0].status == "SKIP":
            got = [Check(got[0].target, got[0].name, "FAIL", got[0].detail + " (named explicitly)")]
        _print_checks(got, emit)
        all_checks.extend(got)
    fails = sum(1 for c in all_checks if c.status == "FAIL")
    warns = sum(1 for c in all_checks if c.status == "WARN")
    passes = sum(1 for c in all_checks if c.status == "PASS")
    skipped = sum(1 for c in all_checks if c.status == "SKIP")
    emit(
        f"verify: {passes} PASS, {fails} FAIL, {warns} WARN, {skipped} SKIP"
        + (
            "  -> NOT HEALTHY"
            if fails or (strict and warns)
            else ("  -> healthy" + (" (see WARN)" if warns else ""))
        )
    )
    return EXIT_FAIL if fails or (strict and warns) else EXIT_OK


def _printable(line: str) -> str:
    """A generated line with every non-ASCII character spelled <U+XXXX>, so the plan prints on any terminal."""
    return re.sub(r"[^\x00-\x7f]", lambda m: f"<U+{ord(m.group()):04X}>", line)


def _emit_muse_plan(
    plat: Platform, mp: MusePlan, paths: Paths, emit: Callable[[str], None]
) -> None:
    emit(f"== {plat.key}  {plat.label}  {mp.bundle}  [plugin bundle {mp.state}]")
    emit(
        "   Muse Code takes hooks from a plugin, not from settings.json.  The bundle and the seat shim are part of the "
        "stable copy, written atomically with the rest by `apply tools`; `apply muse` runs that same install (an "
        "unchanged copy is left alone), then prints the OWNER ACTIONS.  Nothing else is written for muse."
    )
    for fs in mp.files:
        emit(f"   {fs.status:<8} {fs.rel}")
    for w in mp.warnings:
        emit(f"   WARNING: {w}")
    emit(
        "   OWNER ACTIONS (yours to run; this tool never edits Muse's config, and the only muse it runs is "
        "`muse plugins validate` in verify, confined to a throwaway HOME):"
    )
    for i, action in enumerate(mp.actions, 1):
        emit(f"     {i}. {action}")
    emit(f"   note: {MUSE_EXPERIMENTAL_ENV}=1 may have to be set for plugins to load (UNVERIFIED).")
    emit(
        f"   muse-seat: run {paths.muse_seat} instead of `muse` to start Muse Code as seat {MUSE_SEAT_TAG} "
        f"(it sets AGENT_SEAT={MUSE_SEAT_TAG} and AGENT_TAG={MUSE_SEAT_TAG}, then runs the real muse found on PATH; "
        "it refuses when there is none, or when that one is itself)."
    )
    emit(f"   manifest {paths.muse_manifest}:")
    for ln in mp.manifest_text.splitlines():
        emit(f"     | {ln}")
    emit(f"   hook wrapper {paths.muse_wrapper}:")
    for ln in mp.wrapper_text.splitlines():
        emit(f"     | {_printable(ln)}")
    emit(f"   shim {paths.muse_seat}:")
    for ln in mp.seat_text.splitlines():
        emit(f"     | {ln}")
    for n in plat.notes:
        emit(f"   note: {n}")
    for u in plat.unverified:
        emit(f"   UNVERIFIED: {u}")


def _emit_minimax_plan(
    plat: Platform, mp: MinimaxPlan, paths: Paths, emit: Callable[[str], None], *, explicit: bool
) -> bool:
    """Print the plan of the MiniMax Code plugin.  True when it is a failure (a refusal, or a skip of a named target)."""
    emit(f"== {plat.key}  {plat.label}  {mp.bundle}  [{mp.state}]")
    if mp.state in ("skip", "refuse"):
        emit(f"   {mp.state.upper()}: {mp.reason}")
        return mp.state == "refuse" or (explicit and mp.state == "skip")
    emit(
        "   MiniMax Code takes hooks from a local plugin: a REAL directory under ~/.minimax/plugins (a symlink is skipped), "
        "enabled by default.  `apply minimax` runs `apply tools` first (the fleet hook lives in the stable dir), then "
        "builds the directory in a stage beside plugins/ and renames it into place.  Nothing else under ~/.minimax is touched."
    )
    emit(f"   command: {mp.command}")
    for fs in mp.files:
        emit(f"   {fs.status:<8} {fs.rel}")
    for n in mp.notes:
        emit(f"   {n}")
    emit(f"   {MINIMAX_MANIFEST_FILE}:")
    for ln in mp.manifest_text.splitlines():
        emit(f"     | {ln}")
    emit(f"   {MINIMAX_HOOKS_FILE}:")
    for ln in mp.hooks_text.splitlines():
        emit(f"     | {ln}")
    for n in plat.notes:
        emit(f"   note: {n}")
    for u in plat.unverified:
        emit(f"   UNVERIFIED: {u}")
    return False


def _emit_copilot_plan(
    plat: Platform, cp: CopilotPlan, emit: Callable[[str], None], *, explicit: bool
) -> bool:
    """Print the plan of the Copilot CLI hooks file.  True when it is a failure."""
    emit(f"== {plat.key}  {plat.label}  {cp.path}  [{cp.state}]")
    if cp.state in ("skip", "refuse"):
        emit(f"   {cp.state.upper()}: {cp.reason}")
        return cp.state == "refuse" or (explicit and cp.state == "skip")
    emit(
        "   Copilot CLI loads *.json under ~/.copilot/hooks.  This is a NEW fleet-owned file; "
        "~/.copilot/settings.json and agent-hook-otlp.json are never touched."
    )
    emit(f"   command: {cp.command}")
    for ln in cp.text.splitlines():
        emit(f"     | {ln}")
    for n in cp.notes:
        emit(f"   {n}")
    for n in plat.notes:
        emit(f"   note: {n}")
    for u in plat.unverified:
        emit(f"   UNVERIFIED: {u}")
    return False


def _emit_opencode_plan(
    plat: Platform, op: OpencodePlan, emit: Callable[[str], None], *, explicit: bool
) -> bool:
    """Print the plan of the OpenCode plugin guard.  True when it is a failure."""
    emit(f"== {plat.key}  {plat.label}  {op.config}  [{op.state}]")
    if op.state in ("skip", "refuse"):
        emit(f"   {op.state.upper()}: {op.reason}")
        return op.state == "refuse" or (explicit and op.state == "skip")
    emit(
        "   OpenCode has no config hooks key.  The plugin array entry below is the only change to opencode.json; "
        "no other key of that file is printed, and a backup is owner-only (0600)."
    )
    emit(f"   plugin entry (absolute path): {op.entry}")
    emit(f"   plugin file in the stable dir: {OPENCODE_PLUGIN_FILE} (written by `apply tools`)")
    for ln in op.plugin_text.splitlines():
        emit(f"     | {ln}")
    for n in op.notes:
        emit(f"   {n}")
    for n in plat.notes:
        emit(f"   note: {n}")
    for u in plat.unverified:
        emit(f"   UNVERIFIED: {u}")
    return False


def _emit_fx_plan(plat: Platform, emit: Callable[[str], None]) -> bool:
    """Print the fx blocker and the prepared compensating control.  Nothing is written, ever."""
    emit(f"== {plat.key}  {plat.label}  ~/.fx/settings.json  [unsupported]")
    emit(f"   {FX_BLOCKER}.")
    emit("   prepared compensating control (reviewable; merge by hand only after Jay approves it):")
    for ln in render_fx_compensating_control().splitlines():
        emit(f"     | {ln}")
    emit(
        "   apply path: Jay merges that JSON into ~/.fx/settings.json (moving permission_mode off yolo), then "
        "runs `install_tools verify fx`, which stays FAIL UNSUPPORTED until Jay also approves an exception or a "
        "real hook surface exists.  This tool never writes ~/.fx."
    )
    for n in plat.notes:
        emit(f"   note: {n}")
    for u in plat.unverified:
        emit(f"   UNVERIFIED: {u}")
    return False


def _emit_block_plan(
    plat: Platform, paths: Paths, emit: Callable[[str], None], *, explicit: bool
) -> bool:
    """Print the plan of a block platform.  True when it is a failure (a refusal, or a skip of a named target)."""
    bp = plan_block(plat, paths)
    emit(f"== {plat.key}  {plat.label}  {bp.path}  [{bp.action if bp.state == 'ok' else bp.state}]")
    if bp.state != "ok":
        emit(f"   {'REFUSE' if bp.state == 'refuse' else 'SKIP'}: {bp.reason}")
    else:
        emit(f"   command: {bp.command}")
        emit(
            f"   the block `{BLOCK_NAME}` ({bp.action}); "
            + ("nothing outside it is touched:" if not bp.notes else "see the notes:")
        )
        for ln in MB.render_block(BLOCK_NAME, BLOCK_TOOL, bp.body):
            emit(f"     + {ln}")
        for n in bp.notes:
            emit(f"   note: {n}")
        if plat.key == "clutch":
            emit(f"   hooks file {paths.clutch_hooks} (written by `apply tools`):")
            for ln in render_clutch_hooks(paths.stable).splitlines():
                emit(f"     | {ln}")
    for n in plat.notes:
        emit(f"   note: {n}")
    for u in plat.unverified:
        emit(f"   UNVERIFIED: {u}")
    return bp.state == "refuse" or (explicit and bp.state == "skip")


def _cmd_plan(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    home = _resolve_home(args)
    paths = make_paths(home, _resolve_stable(args))
    targets, explicit = _expand(args.targets, ("all", "muse", "clutch", "kimi"))
    src = resolve_source(args.source, args.registry, args.sha)
    rc = EXIT_OK
    emit(
        f"plan (read-only)  home={paths.home}  stable={paths.stable}  source={src.scripts_dir}  sha={src.sha}"
    )
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
            emit(
                f"   {same} file(s) already identical; the whole dir is replaced atomically (stage, probe, swap)"
            )
            stale = [fs.rel for fs in tp.files if fs.status == "stale"]
            if stale and not tp.problems:
                emit(
                    f"   WILL DELETE {len(stale)} file{'' if len(stale) == 1 else 's'} the new copy does not contain:"
                )
                for rel in stale[:20]:
                    emit(f"     {rel}")
                if len(stale) > 20:
                    emit(f"     ... and {len(stale) - 20} more")
            emit("   VERSION would read:")
            for ln in tp.version_text.splitlines():
                emit(f"     {ln}")
            emit(
                "   the shims run `/usr/bin/env python3 -I`; the minimum is Python 3.9 (macOS /usr/bin/python3), "
                "and a GUI-launched app may resolve python3 under PATH=/usr/bin:/bin only"
            )
            emit(f"   shim {paths.hook_shim}:")
            for ln in tp.hook_shim_text.splitlines():
                emit(f"     | {_printable(ln)}")
            if tp.facts.words:
                emit(
                    f"   prefilter: derived from guard.py _TRIGGER ({len(tp.facts.words)} words: {', '.join(tp.facts.words)}), "
                    f"the JSON \\u escape and {len(tp.facts.lookalikes)} non-ASCII lookalike letter(s); a payload over "
                    f"{tp.facts.max_chars or 'any size'} characters skips it.  A benign Bash call exits in the shell, "
                    "without starting Python"
                )
            if tp.facts.words:
                emit(
                    f"   fleet-guard-hook prefilter: the same words plus the secret guard's ({len(SECRET_WORDS)} words: "
                    f"{', '.join(SECRET_WORDS)}; and a `ps` followed by white space), so an ordinary Bash call exits in the "
                    "shell for both guards"
                )
            for w in tp.warnings:
                emit(f"   WARNING: {w}")
            emit(
                f"   Muse Code files (written with the rest, swapped in atomically): {MUSE_SEAT_NAME}, "
                f"{MUSE_PLUGIN_DIR}/ (see `plan muse`)"
            )
            if tp.lane_shim_state == "foreign":
                emit(
                    f"   shim {paths.lane_shim}: LEFT ALONE, it is {tp.lane_detail}; the stable dir and hook still install"
                )
            elif tp.lane_shim_text is None:
                emit(
                    f"   shim {paths.lane_shim}: SKIPPED, the source package has no {LANE_MODULE}"
                    + (
                        "; a stale generated lane shim would be removed"
                        if tp.lane_shim_state == "stale"
                        else ""
                    )
                )
            else:
                emit(f"   shim {paths.lane_shim} [{tp.lane_shim_state}]:")
                for ln in tp.lane_shim_text.splitlines():
                    emit(f"     | {ln}")
            for prob in tp.problems:
                emit(f"   REFUSE: {prob}")
                rc = EXIT_FAIL
            continue
        plat = PLATFORMS[tgt]
        if plat.key == "fx":
            _emit_fx_plan(plat, emit)
            continue
        if plat.kind == "plugin":
            if plat.key == "opencode":
                if _emit_opencode_plan(
                    plat,
                    plan_opencode(paths, follow_symlinks=args.follow_symlinks),
                    emit,
                    explicit=tgt in explicit,
                ):
                    rc = EXIT_FAIL
            else:
                _emit_muse_plan(plat, plan_muse(src, paths), paths, emit)
            continue
        if plat.kind == "block":
            if _emit_block_plan(plat, paths, emit, explicit=tgt in explicit):
                rc = EXIT_FAIL
            continue
        if plat.kind == "bundle":
            if plat.key == "copilot":
                if _emit_copilot_plan(
                    plat,
                    plan_copilot(paths, follow_symlinks=args.follow_symlinks),
                    emit,
                    explicit=tgt in explicit,
                ):
                    rc = EXIT_FAIL
            elif _emit_minimax_plan(
                plat,
                plan_minimax(paths, follow_symlinks=args.follow_symlinks),
                paths,
                emit,
                explicit=tgt in explicit,
            ):
                rc = EXIT_FAIL
            continue
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
            emit(
                f"   {pp.hidden} other hook{'' if one else 's'} under {pp.label} {'is' if one else 'are'} "
                "left untouched and not shown"
            )
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
    wired = [
        t
        for t in targets
        if PLATFORMS.get(t) is not None and PLATFORMS[t].kind in ("plugin", "block", "bundle")
    ]
    if wired and "tools" not in targets:
        # The Muse bundle, the fleet hook shim and Clutch's hooks.json are part of the stable copy, so `apply muse`,
        # `apply clutch` and `apply kimi` write them the only way they are written: with the rest of the stable
        # copy, through the same stage, probe and swap.  An unchanged copy stays untouched.  `apply minimax` needs the
        # stable fleet hook the same way: its plugin directory only points at it.
        targets.insert(targets.index(wired[0]), "tools")
    minimal = not args.no_minimal_path
    results: list[Result] = []
    for tgt in targets:
        if tgt == "tools":
            res = apply_tools(
                resolve_source(args.source, args.registry, args.sha),
                paths,
                timeout=args.timeout,
                minimal_path=minimal,
                emit=emit,
                replace_extra=args.replace_extra,
            )
        else:
            # Never `shim_proven`: each platform's own command is probed right now, in every run.
            res = apply_platform(
                PLATFORMS[tgt],
                paths,
                timeout=args.timeout,
                minimal_path=minimal,
                follow_symlinks=args.follow_symlinks,
                dsh_bin=getattr(args, "dsh", None),
                check_dsh=not getattr(args, "no_dsh_check", False),
                check_kimi=not getattr(args, "no_kimi_check", False),
            )
        results.append(res)
        emit(res.line())
    failed = [
        r for r in results if not r.ok and not (r.status == "skipped" and r.target not in explicit)
    ]
    rc = EXIT_FAIL if failed else EXIT_OK
    for r in results:
        if r.status in ("added", "updated") and r.target in NEXT_STEPS:
            emit(f"NEXT       {r.target:<12} {NEXT_STEPS[r.target]}")
        if r.target == "muse" and r.ok:
            for i, action in enumerate(muse_owner_actions(paths), 1):
                emit(f"OWNER ACTION {i} {action}")
            emit(
                f"NEXT       muse         {MUSE_EXPERIMENTAL_ENV}=1 may be required for plugins to load (UNVERIFIED)"
            )
    if not args.no_verify:
        todo = [r.target for r in results if r.ok]
        if todo:
            emit("")
            emit("verifying what was just applied ...")
            vrc = _verify_targets(
                todo,
                explicit,
                paths,
                timeout=args.timeout,
                minimal_path=minimal,
                strict=args.strict,
                emit=emit,
            )
            rc = max(rc, vrc)
    return rc


def _cmd_verify(args: argparse.Namespace, emit: Callable[[str], None]) -> int:
    home = _resolve_home(args)
    paths = make_paths(home, _resolve_stable(args))
    targets, explicit = _expand(args.targets, ("all", "muse", "clutch", "kimi"))
    return _verify_targets(
        targets,
        explicit,
        paths,
        timeout=args.timeout,
        minimal_path=not args.no_minimal_path,
        strict=args.strict,
        emit=emit,
    )


# --------------------------------------------------------------------------- self-test

_PREFILTER_CLAUSE = re.compile(r"^[ \t]*\*[^\n]*\) ;;[ \t]*\n", re.MULTILINE)


def empty_prefilter(shim_text: str) -> str:
    """The hook shim with every prefilter pattern removed, leaving `*) exit 0 ;;` as the only clause: a shim that
    allows everything without starting Python.  For the self-test and the tests, which prove that `verify` fails
    on exactly that.  Text without a prefilter comes back unchanged."""
    return _PREFILTER_CLAUSE.sub("", shim_text)


def _seed_fake_home(home: str) -> None:
    """Platform dirs with the real files' SHAPES and dummy commands."""

    def put(rel: str, doc: object) -> None:
        full = os.path.join(home, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")

    other = {"type": "command", "command": "/bin/true", "timeout": 3}
    put(
        ".claude/settings.json",
        {
            "env": {"EXAMPLE": "x"},
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [dict(other, statusMessage="dummy")]}]
            },
        },
    )
    put(
        ".codex/hooks.json",
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [dict(other)]}]}},
    )
    put(
        ".cursor/hooks.json",
        {
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "/bin/true", "timeout": 8, "failClosed": False}]
            },
        },
    )
    put(".gemini/config/hooks.json", {"other-hook": {"Stop": [dict(other)], "enabled": True}})
    put(
        ".grok/hooks/imported-from-claude.json",
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [dict(other)]}]}},
    )
    os.makedirs(
        os.path.join(home, ".minimax", "plugins"), exist_ok=True
    )  # MiniMax Code: an empty plugins dir


def self_test(
    emit: Callable[[str], None],
    *,
    timeout: float = PROBE_TIMEOUT_S,
    minimal_path: bool = True,
    source: Source | None = None,
) -> int:
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
            r = apply_platform(
                PLATFORMS[key], paths, timeout=timeout, minimal_path=minimal_path, shim_proven=True
            )
            emit(r.line())
            if not r.ok:
                failures.append(f"apply {key}: {r.detail}")
        r = apply_platform(
            PLATFORMS["minimax"],
            paths,
            timeout=timeout,
            minimal_path=minimal_path,
            shim_proven=True,
        )
        emit(r.line())
        if not r.ok:
            failures.append(f"apply minimax: {r.detail}")
        targets = ["tools"] + list(SUPPORTED_KEYS) + ["minimax", "muse"]

        def run_verify(label: str, show: tuple) -> tuple[int, list[Check]]:
            checks: list[Check] = []
            for tgt in targets:
                checks.extend(
                    verify_tools(paths, timeout=timeout)
                    if tgt == "tools"
                    else verify_platform(
                        PLATFORMS[tgt], paths, timeout=timeout, minimal_path=minimal_path
                    )
                )
            counts = {
                st: sum(1 for c in checks if c.status == st)
                for st in ("PASS", "FAIL", "WARN", "SKIP")
            }
            emit(
                f"self-test: verify {label}: " + ", ".join(f"{n} {st}" for st, n in counts.items())
            )
            _print_checks([c for c in checks if c.status in show], emit)
            return counts["FAIL"], checks

        fails, _ = run_verify("after install, expect no FAIL", ("FAIL", "WARN"))
        if fails:
            failures.append(f"verify reported {fails} FAIL on a fresh install")
        guard = os.path.join(paths.package_dir, "guard.py")
        hidden = guard + ".disabled"
        os.rename(guard, hidden)
        try:
            _fails, checks = run_verify(
                "with guard.py renamed, expect FAIL on every platform", ("FAIL",)
            )
            for key in SUPPORTED_KEYS + ("minimax", "muse"):
                if not any(
                    c.target == key and c.name.startswith("deny/") and c.status == "FAIL"
                    for c in checks
                ):
                    failures.append(
                        f"verify did NOT fail {key} with the guard module renamed: a broken install would read as healthy"
                    )
            if not any(c.target == "tools" and c.status == "FAIL" for c in checks):
                failures.append("verify tools did not notice the missing module")
        finally:
            os.rename(hidden, guard)
        fails, _ = run_verify("after repair, expect no FAIL", ("FAIL",))
        if fails:
            failures.append(f"verify reported {fails} FAIL after the guard was restored")
        # The prefilter in the hook shim decides before Python starts, so a shim whose patterns match nothing
        # allows everything.  verify has to catch that too.
        shim_bytes = _read_bytes(paths.hook_shim)
        broken = empty_prefilter(shim_bytes.decode("utf-8"))
        if broken.encode("utf-8") == shim_bytes:
            failures.append("the hook shim has no prefilter to empty, so this phase could not run")
        else:
            with open(paths.hook_shim, "wb") as fh:
                fh.write(broken.encode("utf-8"))
            try:
                _fails, checks = run_verify(
                    "with the prefilter patterns emptied, expect FAIL on every platform", ("FAIL",)
                )
                for key in SUPPORTED_KEYS:
                    if not any(
                        c.target == key and c.name.startswith("deny/") and c.status == "FAIL"
                        for c in checks
                    ):
                        failures.append(
                            f"verify did NOT fail {key} with the prefilter emptied: a shim that allows everything would read as healthy"
                        )
                if not any(c.target == "tools" and c.status == "FAIL" for c in checks):
                    failures.append("verify tools did not notice the edited hook shim")
            finally:
                with open(paths.hook_shim, "wb") as fh:
                    fh.write(shim_bytes)
        fails, _ = run_verify("after the second repair, expect no FAIL", ("FAIL",))
        if fails:
            failures.append(f"verify reported {fails} FAIL after the hook shim was restored")
        # The fleet hook (Muse Code's plugin) is a second path to the same proof: an allow-everything fleet shim must
        # make verify muse FAIL, and so must a stable copy that lost the secret guard.
        fleet_bytes = _read_bytes(paths.fleet_shim)
        with open(paths.fleet_shim, "wb") as fh:
            fh.write(b"#!/bin/sh\nexit 0\n")
        try:
            _fails, checks = run_verify(
                "with the fleet hook shim allowing everything, expect FAIL on muse", ("FAIL",)
            )
            for key in ("muse",):
                if not any(
                    c.target == key and c.name.startswith("deny/") and c.status == "FAIL"
                    for c in checks
                ):
                    failures.append(
                        f"verify did NOT fail {key} with an allow-everything fleet hook: a broken plugin path would read as healthy"
                    )
            if not any(c.target == "tools" and c.status == "FAIL" for c in checks):
                failures.append("verify tools did not notice the edited fleet hook shim")
        finally:
            with open(paths.fleet_shim, "wb") as fh:
                fh.write(fleet_bytes)
        # ...and so must a fleet shim whose prefilter patterns were emptied: it would allow every call in `sh`
        emptied = empty_prefilter(fleet_bytes.decode("utf-8"))
        if emptied.encode("utf-8") == fleet_bytes:
            failures.append(
                "the fleet hook shim has no prefilter to empty, so that phase could not run"
            )
        else:
            with open(paths.fleet_shim, "wb") as fh:
                fh.write(emptied.encode("utf-8"))
            try:
                _fails, checks = run_verify(
                    "with the fleet hook prefilter emptied, expect FAIL on muse", ("FAIL",)
                )
                for key in ("muse",):
                    if not any(
                        c.target == key and c.name.startswith("deny/") and c.status == "FAIL"
                        for c in checks
                    ):
                        failures.append(
                            f"verify did NOT fail {key} with the fleet hook prefilter emptied: a shim that allows "
                            "everything would read as healthy"
                        )
            finally:
                with open(paths.fleet_shim, "wb") as fh:
                    fh.write(fleet_bytes)

        # MiniMax owns its shim and adapter.  Damage those paths independently:
        # changing Muse's shared fleet shim must not be treated as MM coverage.
        def require_minimax_damage(checks: list[Check], damage: str) -> None:
            for name in (
                "deny/login-PATH",
                "Read/probe-placeholder/deny/hook-env",
                "Write/probe-placeholder/deny/hook-env",
            ):
                if not any(
                    c.target == "minimax" and c.name == name and c.status == "FAIL" for c in checks
                ):
                    failures.append(f"verify did NOT fail minimax {name} {damage}")
            if not any(c.target == "tools" and c.status == "FAIL" for c in checks):
                failures.append(f"verify tools did not notice MiniMax damage {damage}")

        mm_shim = os.path.join(paths.stable, MINIMAX_HOOK_NAME)
        mm_bytes = _read_bytes(mm_shim)
        with open(mm_shim, "wb") as fh:
            fh.write(b"#!/bin/sh\nexit 0\n")
        try:
            _fails, checks = run_verify(
                "with the MiniMax hook shim allowing everything, expect FAIL on minimax", ("FAIL",)
            )
            require_minimax_damage(checks, "with an allow-everything MiniMax hook")
        finally:
            with open(mm_shim, "wb") as fh:
                fh.write(mm_bytes)
        mm_adapter = os.path.join(paths.package_dir, "minimax_guard_hook.py")
        os.rename(mm_adapter, mm_adapter + ".disabled")
        try:
            _fails, checks = run_verify(
                "with the MiniMax adapter renamed, expect FAIL on minimax", ("FAIL",)
            )
            require_minimax_damage(checks, "with the MiniMax adapter missing")
        finally:
            os.rename(mm_adapter + ".disabled", mm_adapter)
        secret = os.path.join(paths.package_dir, SECRET_GUARD_MODULE)
        os.rename(secret, secret + ".disabled")
        try:
            _fails, checks = run_verify(
                "with the secret guard module renamed, expect FAIL on muse and minimax", ("FAIL",)
            )
            for key in ("muse", "minimax"):
                if not any(
                    c.target == key and c.name.startswith("secret/") and c.status == "FAIL"
                    for c in checks
                ):
                    failures.append(
                        f"verify did NOT fail {key} with the secret guard missing: the fleet hook would be the lane guard alone"
                    )
        finally:
            os.rename(secret + ".disabled", secret)
        fails, _ = run_verify("after the third repair, expect no FAIL", ("FAIL",))
        if fails:
            failures.append(f"verify reported {fails} FAIL after the fleet hook was restored")
    if failures:
        for f in failures:
            emit(f"self-test FAIL: {f}")
        return EXIT_FAIL
    emit(
        "self-test PASS: the install verifies, a renamed guard module, an emptied prefilter, an allow-everything fleet hook "
        "and independent MiniMax shim/adapter damage and a missing secret guard each make verify FAIL, "
        "and each repair verifies again"
    )
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
