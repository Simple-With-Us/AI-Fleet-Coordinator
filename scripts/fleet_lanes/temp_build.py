"""Where a build command puts its output, read from its argv alone (AFC-618, the temp-build rule class).

Agents' builds drained about 43 GB of system temp in one week of November 2026:  Xcode DerivedData through
`-derivedDataPath /private/tmp/...`, SwiftPM and cargo build trees, and `node_modules` in throwaway trees.  Nothing tracks
those folders, so they outlive the session that made them.  The guard (hook), the build shim and the watcher share this
module so the three agree on what counts.

    build_outputs(tool, args, env)  ->  [Output(what, path, explicit), ...]

`path` is the text as given, relative or absolute;  the caller joins it with the working directory and decides whether it
is in temp.  An implicit output (`explicit` False) is the tool's default inside the project folder, which is the working
directory unless an option names another:  `.build` for SwiftPM, `target` for cargo, and the folder itself for npm, pnpm,
Yarn and Bun.  Xcode's default is DerivedData under ~/Library, so xcodebuild has no implicit output.

Pure:  no file reads, no subprocess, Python 3.9 safe.  Tests: fleet_lanes/tests/test_temp_build.py.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

TOOLS = ("xcodebuild", "swift", "cargo", "npm", "pnpm", "yarn", "bun")

# xcodebuild options that take a folder, and the build settings that move output.
_XCODE_PATH_OPTS = {
    "-derivedDataPath": "Xcode DerivedData",
    "-archivePath": "an Xcode archive",
    "-resultBundlePath": "an Xcode result bundle",
    "-clonedSourcePackagesDirPath": "cloned Swift packages",
    "-packageCachePath": "the Swift package cache",
}
_XCODE_SETTINGS = ("SYMROOT", "OBJROOT", "CONFIGURATION_BUILD_DIR", "DSTROOT", "SHARED_PRECOMPS_DIR")

_SWIFT_BUILDS = frozenset({"build", "test", "run"})
_SWIFT_SCRATCH = ("--scratch-path", "--build-path")
_SWIFT_VALUE_OPTS = frozenset({"--package-path", "--scratch-path", "--build-path", "--cache-path", "--config-path",
                               "--security-path", "-c", "--configuration", "--product", "--target", "--filter", "--skip",
                               "--triple", "--sdk", "--toolchain", "--swift-sdk", "-Xcc", "-Xswiftc", "-Xlinker", "-Xcxx",
                               "--arch", "-j", "--jobs"})

_CARGO_BUILDS = frozenset({"build", "b", "test", "t", "run", "r", "check", "c", "bench", "doc", "d", "clippy", "rustc",
                           "rustdoc", "fix"})
_CARGO_VALUE_OPTS = frozenset({"--target-dir", "--manifest-path", "-p", "--package", "--bin", "--example", "--test",
                               "--bench", "--features", "-F", "--target", "--profile", "-j", "--jobs", "--color", "-Z",
                               "--config", "--message-format", "--exclude", "--lib", "-C"})

_NODE_INSTALLS = frozenset({"install", "i", "ci", "add", "rebuild", "isntall", "in", "ins", "inst", "insta", "instal"})
_NODE_RUNS = frozenset({"run", "run-script", "rum", "urn"})
_NODE_DIR_OPTS = {"npm": ("--prefix", "-C"), "pnpm": ("--dir", "-C"), "yarn": ("--cwd",), "bun": ("--cwd",)}
_NODE_VALUE_OPTS = frozenset({"--prefix", "-C", "--dir", "--cwd", "--workspace", "-w", "--filter", "-F", "--registry",
                              "--cache", "--userconfig", "--loglevel", "--tag"})


@dataclass(frozen=True)
class Output:
    what: str  # a phrase for the deny reason, "Xcode DerivedData"
    path: str  # as given;  the caller joins a relative path with the working directory
    explicit: bool  # named by an option, rather than the tool's default inside the project folder


def tool_name(word: str) -> str | None:
    """The build tool a command word names, or None."""
    name = os.path.basename(word)
    return name if name in TOOLS else None


def _value(args: Sequence[str], i: int, opt: str) -> tuple[str | None, int]:
    """The value of `opt` at args[i], spelled `opt VALUE` or `opt=VALUE`, and how many words it took."""
    a = args[i]
    if a == opt:
        return (args[i + 1], 2) if i + 1 < len(args) else (None, 1)
    if a.startswith(opt + "="):
        return a[len(opt) + 1:], 1
    return None, 0


def _xcodebuild(args: Sequence[str]) -> list[Output]:
    out: list[Output] = []
    i = 0
    while i < len(args):
        a = args[i]
        took = 1
        for opt, what in _XCODE_PATH_OPTS.items():
            value, used = _value(args, i, opt)
            if used:
                took = used
                if value:
                    out.append(Output(what, value, True))
                break
        else:
            name, eq, value = a.partition("=")
            if eq and name in _XCODE_SETTINGS and value:
                out.append(Output(f"Xcode build products ({name})", value, True))
        i += took
    return out


def _positional(args: Sequence[str], value_opts: frozenset[str]) -> tuple[list[str], dict[str, str]]:
    """(the words that are not options, the last value of each value option)."""
    words: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("-") and len(a) > 1:
            name, eq, value = a.partition("=")
            if eq:
                opts[name] = value
            elif a in value_opts and i + 1 < len(args):
                opts[a] = args[i + 1]
                i += 1
            i += 1
            continue
        words.append(a)
        i += 1
    return words, opts


def _swift(args: Sequence[str]) -> list[Output]:
    words, opts = _positional(args, _SWIFT_VALUE_OPTS)
    if not words or words[0] not in (*_SWIFT_BUILDS, "package"):
        return []
    for opt in _SWIFT_SCRATCH:
        if opts.get(opt):
            return [Output("a SwiftPM build folder", opts[opt], True)]
    if words[0] not in _SWIFT_BUILDS:
        return []
    return [Output("a SwiftPM build folder", os.path.normpath(os.path.join(opts.get("--package-path") or ".", ".build")), False)]


def _cargo(args: Sequence[str], env: Mapping[str, str]) -> list[Output]:
    words, opts = _positional(args, _CARGO_VALUE_OPTS)
    if not words or words[0] not in _CARGO_BUILDS:
        return []
    if opts.get("--target-dir"):
        return [Output("a cargo target folder", opts["--target-dir"], True)]
    if (env.get("CARGO_TARGET_DIR") or "").strip():
        return [Output("a cargo target folder (CARGO_TARGET_DIR)", env["CARGO_TARGET_DIR"].strip(), True)]
    manifest = opts.get("--manifest-path")
    base = os.path.dirname(manifest) if manifest else (opts.get("-C") or ".")
    return [Output("a cargo target folder", os.path.normpath(os.path.join(base or ".", "target")), False)]


def _node(tool: str, args: Sequence[str]) -> list[Output]:
    words, opts = _positional(args, _NODE_VALUE_OPTS)
    if "-g" in args or "--global" in args or opts.get("--location") == "global":
        return []  # a global install goes to the tool's own prefix, never the project
    if words:
        sub = words[0]
        if sub in _NODE_RUNS:
            if len(words) < 2 or "build" not in words[1]:
                return []
        elif sub not in _NODE_INSTALLS and "build" not in sub:
            return []
    elif tool != "yarn":
        return []  # bare `yarn` installs;  bare npm, pnpm or bun prints help
    folder = next((opts[o] for o in _NODE_DIR_OPTS.get(tool, ()) if opts.get(o)), None)
    if folder:
        return [Output("node_modules and build output", folder, True)]
    return [Output("node_modules and build output", ".", False)]


def build_outputs(tool: str, args: Sequence[str], env: Mapping[str, str] | None = None) -> list[Output]:
    """Every folder this build would write, best first.  `tool` is a word from TOOLS (`tool_name` gives it), `args` the words
    after it, already expanded, and `env` the variables the command sets or inherits."""
    env = {} if env is None else env
    if tool == "xcodebuild":
        return _xcodebuild(args)
    if tool == "swift":
        return _swift(args)
    if tool == "cargo":
        return _cargo(args, env)
    if tool in ("npm", "pnpm", "yarn", "bun"):
        return _node(tool, args)
    return []


def unwrap_xcrun(args: Sequence[str]) -> tuple[str | None, list[str]]:
    """`xcrun [--sdk X] [--toolchain Y] TOOL ARGS...`:  the build tool it runs and that tool's words, or (None, [])."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--sdk", "-sdk", "--toolchain", "-toolchain", "--log", "-l"):
            i += 2 if a in ("--sdk", "-sdk", "--toolchain", "-toolchain") else 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        tool = tool_name(a)
        return (tool, list(args[i + 1:])) if tool else (None, [])
    return None, []


def alternative(tool: str, host: str) -> str:
    """The deny reason's way out:  the lane-local or cache folder this tool should write instead."""
    caches = "~/Library/Caches" if host == "mac" else "~/.cache"
    if tool == "xcodebuild":
        return (f"Drop -derivedDataPath to use Xcode's own DerivedData, or point it at {caches}/<App>DerivedData "
                "(HogHunter's scripts/xcodegen-post.sh does this) or at a folder inside your lane.")
    if tool == "swift":
        return f"Build inside your lane (its .build stays there), or pass --scratch-path {caches}/<App>-swiftpm."
    if tool == "cargo":
        return f"Build inside your lane (its target/ stays there), or pass --target-dir {caches}/cargo-target/<App>."
    if tool in ("npm", "pnpm", "yarn", "bun"):
        return "Run it in your lane under ~/apps/lanes, where node_modules is cleaned with the lane."
    return "Build inside your lane under ~/apps/lanes instead."
