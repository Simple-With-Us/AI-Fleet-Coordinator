#!/usr/bin/env python3
"""Flag non-prod Infisical environment selection in fleet repos.

Owner decision 2026-10-10:  prod is the only Infisical environment, and dev and staging
are being deleted from every project.  Anything that still selects one of them would read
a deleted (or stale) environment, so this lint fails on:

  * env files (``.cursor/infisical.env``, ``.env``, ``.env.*``, ``*.env``, ``*.env.example``):
    ``INFISICAL_ENV`` or ``INFISICAL_ENVIRONMENT`` (also with a prefix, such as
    ``CLUTCH_INFISICAL_ENV``) set to anything but ``prod``, and a ``:-dev`` / ``:=dev``
    (or staging) shell default;
  * ``cursor-cloud-start.sh``:  the same, including literal assignments such as
    ``export INFISICAL_ENV="dev"``, plus ``infisical ... --env=dev``;
  * ``.infisical.json`` whose ``defaultEnvironment`` is not ``prod``.

Whole-line comments are skipped.  A line that really needs a non-prod word can carry the
marker ``infisical-env: allow`` (with a reason) and is skipped.

Findings print as ``path:line: message``.  The offending line is never echoed, only the
environment slug (and only when it looks like one), because env files can hold secrets.

Usage (from a checkout of the repo to lint; exit 0 clean, 1 findings, 2 usage error):

    python3 scripts/check-infisical-env.py                 # the repo this script lives in
    python3 scripts/check-infisical-env.py PATH [PATH...]  # other checkouts or folders
    python3 scripts/check-infisical-env.py --fleet [--fetch]

``--fleet`` lints every git repo under ``~/Code`` as it is on ``origin/main`` (read with
``git show``, never from the working trees, which lag main).  Add ``--fetch`` to refresh
``origin`` first.  CI runs the default form on this repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROD = "prod"
ALLOW_MARKER = "infisical-env: allow"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
             "DerivedData", "Pods", ".build", "vendor"}
MAX_BYTES = 512 * 1024          # a config file bigger than this is not what this lint is for

# NAME=value, optionally "export NAME=value"; NAME ends in INFISICAL_ENV or INFISICAL_ENVIRONMENT.
ENV_ASSIGN_RE = re.compile(
    r"""^\s*(?:export\s+)?(?P<name>[A-Za-z0-9_]*INFISICAL_ENV(?:IRONMENT)?)\s*=\s*(?P<value>.*)$""")
# ${NAME:-dev}  ${NAME:=staging}  ${NAME-dev}  (any variable: the owner's rule is "no dev fallbacks").
SHELL_DEFAULT_RE = re.compile(r"""\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=]\s*["']?(?P<slug>dev|staging)["']?\s*\}""")
# infisical run --env=dev   infisical export --env dev   --env="staging"
CLI_ENV_RE = re.compile(r"""--env(?:=|\s+)["']?(?P<slug>[A-Za-z0-9_-]+)["']?""")
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def render(self, prefix: str = "") -> str:
        return f"{prefix}{self.path}:{self.line}: {self.message}"


# --------------------------------------------------------------------------- classification

def kinds_for(path: str) -> set[str]:
    """Which rule sets apply to a repo-relative path (posix separators)."""
    name = path.rsplit("/", 1)[-1]
    kinds: set[str] = set()
    if (name == ".env" or name.startswith(".env.") or name.endswith(".env")
            or name.endswith(".env.example")):
        kinds.add("env-file")
    if name == "cursor-cloud-start.sh":
        kinds.add("start-script")
    if name == ".infisical.json":
        kinds.add("infisical-json")
    return kinds


# --------------------------------------------------------------------------- rules

def _clean_value(raw: str) -> str:
    """The assigned value without quotes or a trailing ' # comment'."""
    value = raw.strip()
    if value and value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split("#", 1)[0].strip()


def _show_slug(slug: str) -> str:
    """Name the slug only when it looks like one;  never echo anything else from a line."""
    return repr(slug) if SLUG_RE.match(slug) else "a non-slug value"


def _scan_lines(path: str, text: str, kinds: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    script = "start-script" in kinds
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ALLOW_MARKER in line:
            continue
        m = ENV_ASSIGN_RE.match(line)
        if m:
            value = _clean_value(m.group("value"))
            # A "$VAR" / "${VAR...}" value is a reference;  the shell-default rule judges it.
            if value and not value.startswith("$") and value.lower() != PROD:
                findings.append(Finding(path, lineno, (
                    f"{m.group('name')} is {_show_slug(value)}, but prod is the only Infisical "
                    "environment;  set it to prod")))
        for d in SHELL_DEFAULT_RE.finditer(line):
            findings.append(Finding(path, lineno, (
                f"shell fallback to {d.group('slug')!r}:  prod is the only Infisical "
                "environment, so the fallback must be prod (or fail when unset)")))
        if script:
            for c in CLI_ENV_RE.finditer(line):
                if c.group("slug").lower() != PROD and not c.group("slug").startswith("$"):
                    findings.append(Finding(path, lineno, (
                        f"--env {_show_slug(c.group('slug'))}:  prod is the only Infisical "
                        "environment")))
    return findings


def _scan_infisical_json(path: str, text: str) -> list[Finding]:
    try:
        data = json.loads(text)
    except ValueError:
        return [Finding(path, 1, "cannot parse .infisical.json, so defaultEnvironment is unchecked")]
    if not isinstance(data, dict) or "defaultEnvironment" not in data:
        return []
    env = data.get("defaultEnvironment")
    if isinstance(env, str) and env.strip().lower() == PROD:
        return []
    shown = _show_slug(env) if isinstance(env, str) else "a non-string value"
    line = 1
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "defaultEnvironment" in raw:
            line = lineno
            break
    return [Finding(path, line, f"defaultEnvironment is {shown}, but prod is the only Infisical "
                                "environment;  set it to \"prod\"")]


def scan_text(path: str, text: str) -> list[Finding]:
    """All findings for one file's text.  `path` is repo-relative with '/' separators."""
    kinds = kinds_for(path)
    if not kinds:
        return []
    findings = _scan_lines(path, text, kinds) if kinds & {"env-file", "start-script"} else []
    if "infisical-json" in kinds:
        findings += _scan_infisical_json(path, text)
    return findings


# --------------------------------------------------------------------------- sources

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=check)


class WorkingTree:
    """Files of a checkout or plain folder:  git's view when it is a repo, else a walk."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def paths(self) -> list[str]:
        try:
            out = _git(self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
            return sorted(p for p in out.stdout.decode("utf-8", "replace").split("\0") if p)
        except (OSError, subprocess.CalledProcessError):
            pass
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                found.append((Path(dirpath) / fn).relative_to(self.root).as_posix())
        return sorted(found)

    def read(self, path: str) -> str | None:
        full = self.root / path
        try:
            if not full.is_file() or full.stat().st_size > MAX_BYTES:
                return None
            return full.read_text(errors="replace")
        except OSError:
            return None


class GitRef:
    """Files of a repo as they are at a ref (default origin/main), read with git show."""

    def __init__(self, repo: Path, ref: str) -> None:
        self.repo, self.ref = repo, ref

    def paths(self) -> list[str]:
        out = _git(self.repo, "ls-tree", "-r", "-z", "--name-only", self.ref)
        return sorted(p for p in out.stdout.decode("utf-8", "replace").split("\0") if p)

    def read(self, path: str) -> str | None:
        got = _git(self.repo, "show", f"{self.ref}:{path}", check=False)
        if got.returncode != 0 or len(got.stdout) > MAX_BYTES:
            return None
        return got.stdout.decode("utf-8", "replace")


def scan_source(source) -> list[Finding]:
    findings: list[Finding] = []
    for path in source.paths():
        if not kinds_for(path) or set(path.split("/")) & SKIP_DIRS:
            continue
        text = source.read(path)
        if text is not None:
            findings += scan_text(path, text)
    return findings


# --------------------------------------------------------------------------- CLI

def fleet_repos(code_dir: Path) -> list[Path]:
    return sorted(p for p in code_dir.iterdir() if (p / ".git").exists())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Flag non-prod Infisical environment selection.")
    ap.add_argument("paths", nargs="*", help="checkouts or folders to lint (default: this repo)")
    ap.add_argument("--fleet", action="store_true",
                    help="lint every git repo under --code-dir as it is on --ref")
    ap.add_argument("--code-dir", default="~/Code", help="where --fleet looks (default ~/Code)")
    ap.add_argument("--ref", default="origin/main", help="git ref --fleet reads (default origin/main)")
    ap.add_argument("--fetch", action="store_true", help="with --fleet: git fetch origin first")
    args = ap.parse_args(argv)

    total = 0
    scanned = 0
    if args.fleet:
        if args.paths:
            ap.error("--fleet takes no PATH arguments")
        code_dir = Path(args.code_dir).expanduser()
        if not code_dir.is_dir():
            print(f"check-infisical-env: {code_dir} is not a directory", file=sys.stderr)
            return 2
        for repo in fleet_repos(code_dir):
            if args.fetch:
                _git(repo, "fetch", "-q", "origin", check=False)
            try:
                findings = scan_source(GitRef(repo, args.ref))
            except (OSError, subprocess.CalledProcessError):
                print(f"check-infisical-env: {repo.name}: no {args.ref} to read, skipped",
                      file=sys.stderr)
                continue
            scanned += 1
            for f in findings:
                print(f.render(f"{repo.name}:"))
            total += len(findings)
    else:
        roots = [Path(p).expanduser() for p in args.paths] or [Path(__file__).resolve().parents[1]]
        for root in roots:
            if not root.is_dir():
                print(f"check-infisical-env: {root} is not a directory", file=sys.stderr)
                return 2
            scanned += 1
            prefix = f"{root.name}:" if len(roots) > 1 else ""
            findings = scan_source(WorkingTree(root))
            for f in findings:
                print(f.render(prefix))
            total += len(findings)

    if total:
        print(f"check-infisical-env: {total} finding(s) in {scanned} repo(s);  prod is the only "
              "Infisical environment", file=sys.stderr)
        return 1
    print(f"check-infisical-env: ok ({scanned} repo(s), prod only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
