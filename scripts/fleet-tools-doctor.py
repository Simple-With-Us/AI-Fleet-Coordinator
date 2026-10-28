#!/usr/bin/env python3
"""fleet-tools-doctor:  is this host's fleet tooling current and complete?

One OK / WARN / FAIL line per check, for the Mac and the Hetzner server alike
(docs/protocols/fleet-tool-parity.md says what each check guards).  Names,
commits, versions and booleans only:  never a credential value.

  python3 scripts/fleet-tools-doctor.py [--seats CLAUDE,CODEX] [--json] [--no-network]
                                        [--remote ALIAS] [--write PATH|-] [--post]

Every row also carries a `state` (AFC-461), the answer Muster shows:
  current   observed equal to the expected revision or version in this run
  stale     installed, but behind (or different from) what is expected
  missing   a required command, file, wrapper or timer is absent
  unknown   the doctor could not tell (no network, a failed probe, an unreachable host)
  absent    an optional tool that is not installed (informational)
A fleet tool is `current` only when it was compared with the tip of refs/heads/main read
from GitHub in this run (`git ls-remote`):  a local origin/main ref is never enough, and
`--no-network` turns every such row `unknown`.

The tool registry (TOOL_REGISTRY below) lists the platform CLIs and dev tools per host,
with whether each is required or optional there and where its latest version is read
(npm, brew on the Mac, or GitHub releases).  Add a row there to cover a new tool.

`--remote ALIAS` reads the server's doctor JSON over a read-only SSH key whose forced command
runs this doctor with `--json`;  `--write PATH` writes the combined Mac + server report that
Muster reads (`-` prints it).  docs/protocols/fleet-tool-parity.md has both owner steps.

Checks:
  runtime     the installed fleet-cli release (simple-chat, simple-notes, simple-board, recall, lane,
              fleet-mode) was built from AI-Fleet-Coordinator origin/main
  checkout    each canonical Code checkout that ships a CLI is on
              origin/main:  AI-Fleet-Coordinator, FleetLink
  commands    the fleet tools (simple-chat, simple-notes, simple-board, recall, lane, fleet-mode)
              and fleetlink are on PATH (the Mac also keeps lane and fleet-mode
              in ~/apps, which counts)
  catalogs    the shared contracts ship with the runtime:  tools.json and,
              once AFC-329 lands, the board Linear catalog
  tool-file   the files the recall, lane and fleet-mode tools run from ship with the runtime
  fleet-mode-doc  the Mac's ~/apps/FLEET-MODE.md equals the repo's docs/FLEET-MODE.md (WARN if not)
  seat:<S>    per seat:  Zulip bot, Linear app and FleetLink token key NAMES in Infisical
              (Fleet-OPS /zulip and /linear, AI Fleet Coordinator /fleetlink;  AFC-359:  no credential file is read)
  mcp:codex   Codex registers simple-chat (and FleetLink when a token exists)
  mcp:claude  Claude Code registers simple-chat (and FleetLink likewise)
  tool:<T>    each fleet tool's currency (simple-chat, simple-notes, simple-board, recall, lane, fleet-mode,
              fleet-secrets, fleetlink), then each registry tool's install and version
  updater:<U> the job that keeps the runtime on main (the Mac's runtime-sync LaunchAgent,
              the server's fleet-cli-git-update and fleet-tools-doctor timers)

Exit 1 on any FAIL on this host, 0 otherwise.  Standard library only, and Python 3.9 compatible
(the Mac's LaunchAgent runs /usr/bin/python3).
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
RUNTIME = HOME / ".local/share/fleet-cli"
# The server paths that carry the integration trees today.  The owner removed the
# /root aliases on 2026-10-25, so a host resolves its trees from the environment
# and keeps these only as the last resort.  The volume a host keeps its trees on
# is named by FLEET_SERVER_VOLUME, never written here.
SERVER_CODE_ROOTS = (Path("/root/Code"), Path("/home/jay/Code"))
REPOS = ("AI-Fleet-Coordinator", "FleetLink")
# The tools both hosts carry (docs/protocols/fleet-tool-parity.md), then FleetLink's CLI.
FLEET_TOOLS = ("simple-chat", "simple-notes", "simple-board", "recall", "lane", "fleet-mode")
COMMANDS = (*FLEET_TOOLS, "fleetlink")
# The Mac keeps lane and fleet-mode in ~/apps rather than on PATH.
COMMAND_FALLBACK_DIRS = (HOME / "apps",)
# Where a Mac finds the published runtime (the server reads RUNTIME/current).
MAC_RUNTIME = HOME / "apps/lanes/_managed/fleet/agent-sync-runtime"
# The simple-tools commands the Mac links into the managed runtime:  command name -> entry script.
MAC_SIMPLE_TOOLS = (
    ("simple-chat", "simple-chat"),
    ("simple-notes", "simple-notes"),
    ("simple-board", "simple-board"),
)
MAC_FLEET_MODE_DOC = HOME / "apps/FLEET-MODE.md"
AFC_REMOTE_URL = "https://github.com/Simple-With-Us/AI-Fleet-Coordinator"
# Coarse git pathspecs that bound the diff;  is_shipped() then applies the exact rules.
SHIPPED = (
    "scripts/simple-chat",
    "scripts/simple-notes",
    "scripts/simple-board",
    "scripts/agent_sync/",
    "scripts/linear-board/",
    "fleet-apps.json",
    "docs/protocols/agent-sync-partition.toml",
    "scripts/fleet-cli-release.py",
    "scripts/recall",
    "scripts/fleet_rag/",
    "scripts/fleet_lanes/",
    "scripts/fleet_knobs/",
    "scripts/fleet-mode",
    "docs/FLEET-MODE.md",
    "docs/protocols/simple-tools-guide.md",
)
# What a fleet-cli release ships:  mirrors EXACT and allowed_path in scripts/fleet-cli-release.py, so a
# commit that changes only tests or docs under those folders needs no push and is not drift.
# The fallback copy of what a release ships, for a host with no checkout.  With a checkout,
# is_shipped asks scripts/fleet-cli-release.py itself, so the two cannot drift;  a test pins this
# copy to the release's EXACT.
SHIPPED_EXACT = frozenset(
    {
        "scripts/simple-chat",
        "scripts/simple-notes",
        "scripts/simple-board",
        "scripts/linear-board/board",
        "scripts/linear-board/board-ids.txt",
        "fleet-apps.json",
        "docs/protocols/agent-sync-partition.toml",
        "scripts/fleet-cli-release.py",
        "scripts/agent_sync/mcp/tools.json",
        "scripts/agent_sync/mcp/fixtures.jsonl",
        "scripts/agent_sync/mcp/eavesdropper-tools.json",
        "scripts/agent_sync/mcp/renamed-tools.json",
        "scripts/agent_sync/mcp/gb-identities.json",
        "scripts/agent_sync/mcp/notes-tools.json",
        "scripts/agent_sync/mcp/recall-tools.json",
        "scripts/linear-board/catalog/core-tools.json",
        "scripts/agent_sync/mcp/fleetlink-tools.json",
        # AFC-412 PR2:  mirrors the release EXACT entry for the operator registry outbox exporter.
        "scripts/agent_sync/operator_sheets_export.mjs",
        "docs/protocols/simple-tools-guide.md",
        "scripts/agent_sync/wake/wake-schema.json",
        "scripts/linear-board/catalog/named-commands.json",
        "scripts/linear-board/catalog/official-mcp-tools.json",
        "scripts/linear-board/catalog/public-inputs.json",
        "scripts/recall",
        "scripts/fleet_rag/__init__.py",
        "scripts/fleet_rag/contribute_guard.py",
        "scripts/fleet_rag/core.py",
        "scripts/fleet_rag/doctor.py",
        "scripts/fleet_rag/health.py",
        "scripts/fleet_rag/public_fallback.py",
        "scripts/fleet_rag/recall_api.py",
        "scripts/fleet_rag/scrub.py",
        "scripts/fleet_rag/search_log.py",
        "scripts/fleet_rag/telemetry.py",
        "scripts/fleet_lanes/bin/lane",
        "scripts/fleet_lanes/__init__.py",
        "scripts/fleet_lanes/doctor.py",
        "scripts/fleet_lanes/guard.py",
        "scripts/fleet_lanes/lane.py",
        "scripts/fleet_lanes/layout.py",
        "scripts/fleet_knobs/__init__.py",
        "scripts/fleet_knobs/knobs.py",
        "scripts/fleet_knobs/lkg.py",
        "scripts/fleet_knobs/registry.py",
        "scripts/fleet_knobs/source.py",
        "scripts/fleet_knobs/values.py",
        "scripts/fleet-mode",
        "docs/FLEET-MODE.md",
    }
)
STATE = HOME / ".local/state/fleet-tools-doctor.json"
CATALOGS = (
    ("scripts/agent_sync/mcp/tools.json", True),
    ("scripts/linear-board/catalog/named-commands.json", False),
    ("scripts/linear-board/catalog/official-mcp-tools.json", False),
)
_RELEASE_POLICY: list = []

# --- AFC-461:  states, the report Muster reads, and the server route -------------------------
SCHEMA_VERSION = 1  # the doctor's --json document
REPORT_SCHEMA_VERSION = 1  # the combined report file (--write)
REPORT_RELATIVE = ".local/state/fleet-tools/report.json"  # under HOME;  the LaunchAgent's --write
NO_ROUTE = "no read-only route to the server"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes")
REMOTE_TIMEOUT = 150  # the server's doctor reads GitHub and npm, so allow it time
MAX_REMOTE_BYTES = 1024 * 1024  # the same bound fleet_config puts on a doctor's answer
PROBE_TIMEOUT = 5  # each `<tool> --version`
LATEST_TIMEOUT = 20  # each npm view / brew info
GITHUB_TIMEOUT = 8
# Probes run four at a time:  a burst of a dozen Node and Bun CLIs on a loaded Mac starved each
# other past the 5 second cap under launchd (AFC-461).  A probe or lookup that timed out gets one
# more try afterwards, on its own.
REGISTRY_WORKERS = 4
GRACE_MINUTES = 70  # an hourly timer that fired within this window is still on schedule
MAC_UPDATER_LABEL = "com.jay.agent-sync-runtime-sync"
SERVER_TIMERS = ("fleet-cli-git-update.timer", "fleet-tools-doctor.timer")
STATUS_FOR_STATE = {
    "current": "OK",
    "absent": "OK",
    "stale": "FAIL",
    "missing": "FAIL",
    "unknown": "WARN",
}
STATE_FOR_STATUS = {"OK": "current", "WARN": "unknown", "FAIL": "missing"}
# Rows a reader leaves out of its verdict:  the seat harness, ~/Code (the human tree) on the Mac, and
# presence checks (`command:`, `tool-file:`, and `catalog:` on the server), whose `current` means only
# "present";  each fleet tool's currency, a missing one included, is on its `tool:` row.
VERDICT_EXCLUDED = ("harness", "human-tree", "presence")
ROW_FIELDS = ("state", "category", "tool", "installed", "expectedRev", "behindCommits", "reason")
# Owner decision pending (AFC-461):  what "current" means for a vendor CLI.  "latest":  the
# package manager's latest version is the bar, and a tool behind it is stale with the gap shown.
# "skew":  only a Mac/server difference makes it stale;  the gap to latest is still reported.
VENDOR_CURRENCY_POLICY = "latest"

# The tool registry.  One row per command, per host:  "mac" / "server" say whether the host must
# have it (REQUIRED:  absent is `missing`, a FAIL) or may (OPTIONAL:  absent is `absent`,
# informational).  A host that is not named does not check the tool at all.  Latest version, read
# only with the network on:  brew when the command resolves into a Homebrew Cellar, else "npm"
# (`npm view PKG version`), else "github" (the latest release tag, unauthenticated).  Without any,
# latest stays null and the row is current on presence and host skew alone.  "skew": False leaves a
# tool out of the Mac/server comparison.  "version_file" (under HOME) is read instead of running
# the command, for a launcher that updates itself when started.  "version_args" replaces --version.
REQUIRED = "required"
OPTIONAL = "optional"
TOOL_REGISTRY: tuple = (
    # Platform CLIs, one per seat.  CLAUDE and CODEX also work on the server
    # (docs/protocols/fleet-tool-parity.md, Hosts), so only those two are required there.
    {
        "name": "claude",
        "kind": "platform-cli",
        "mac": REQUIRED,
        "server": REQUIRED,
        "npm": "@anthropic-ai/claude-code",
    },
    {
        "name": "codex",
        "kind": "platform-cli",
        "mac": REQUIRED,
        "server": REQUIRED,
        "npm": "@openai/codex",
    },
    {
        "name": "opencode",
        "kind": "platform-cli",
        "mac": REQUIRED,
        "server": OPTIONAL,
        "npm": "@opencode/cli",
    },
    {
        "name": "muse",
        "kind": "platform-cli",
        "mac": REQUIRED,
        "server": OPTIONAL,
        "version_file": ".local/bin/.muse-version",
        "note": "the launcher updates itself when run, so its version file is read instead",
    },
    {"name": "cursor-agent", "kind": "platform-cli", "mac": REQUIRED, "server": OPTIONAL},
    {"name": "grok", "kind": "platform-cli", "mac": REQUIRED, "server": OPTIONAL},
    {"name": "agy", "kind": "platform-cli", "mac": REQUIRED, "server": OPTIONAL},
    {"name": "mcode", "kind": "platform-cli", "mac": REQUIRED, "server": OPTIONAL},
    {"name": "mmx", "kind": "platform-cli", "mac": OPTIONAL, "server": OPTIONAL, "npm": "mmx-cli"},
    {
        "name": "copilot",
        "kind": "platform-cli",
        "mac": OPTIONAL,
        "server": OPTIONAL,
        "npm": "@github/copilot",
    },
    {"name": "fx", "kind": "platform-cli", "mac": REQUIRED, "server": OPTIONAL},
    {"name": "clutch", "kind": "platform-cli", "mac": REQUIRED, "server": OPTIONAL},
    {
        "name": "kimi",
        "kind": "platform-cli",
        "mac": OPTIONAL,
        "server": OPTIONAL,
        "note": "retired seat:  informational only",
    },
    {
        "name": "droid",
        "kind": "platform-cli",
        "mac": OPTIONAL,
        "server": OPTIONAL,
        "note": "no fleet seat:  informational only",
    },
    {
        "name": "gemini",
        "kind": "platform-cli",
        "mac": OPTIONAL,
        "server": OPTIONAL,
        "note": "no fleet seat:  informational only",
    },
    # Dev tools the fleet tools and seats lean on.
    {"name": "gh", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED, "github": "cli/cli"},
    {"name": "git", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED, "skew": False},
    {"name": "node", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED},
    {"name": "npm", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED, "npm": "npm"},
    {"name": "pnpm", "kind": "dev-tool", "mac": OPTIONAL, "server": OPTIONAL, "npm": "pnpm"},
    {"name": "python3", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED, "skew": False},
    {
        "name": "uv",
        "kind": "dev-tool",
        "mac": OPTIONAL,
        "server": OPTIONAL,
        "github": "astral-sh/uv",
    },
    {"name": "infisical", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED},
    {"name": "docker", "kind": "dev-tool", "server": REQUIRED},
    {"name": "jq", "kind": "dev-tool", "mac": REQUIRED, "server": OPTIONAL},
    {"name": "tailscale", "kind": "dev-tool", "mac": REQUIRED, "server": REQUIRED},
    {"name": "wrangler", "kind": "dev-tool", "mac": OPTIONAL, "npm": "wrangler"},
    {"name": "cloudflared", "kind": "dev-tool", "mac": OPTIONAL},
    {
        "name": "minisign",
        "kind": "dev-tool",
        "mac": REQUIRED,
        "server": OPTIONAL,
        "github": "jedisct1/minisign",
        "note": "verifies signed fleet helpers (docs/protocols/signed-helpers.md)",
    },
    {
        "name": "gitleaks",
        "kind": "dev-tool",
        "mac": REQUIRED,
        "server": REQUIRED,
        "github": "gitleaks/gitleaks",
        "note": "recall contribute scans with it",
    },
    {"name": "xcodegen", "kind": "dev-tool", "mac": OPTIONAL},
)
# Where the registry looks, whatever PATH the caller has (launchd and a forced SSH command both
# start with a short one).  The caller's own PATH is searched after these.
TOOL_PATH_DIRS = (
    "{home}/.local/bin",
    "/root/.local/bin",
    "/home/jay/.local/bin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    "/snap/bin",
    "{home}/.npm-global/bin",
    "{home}/.bun/bin",
    "{home}/.cargo/bin",
)
CELLAR_RE = re.compile(r"/(?:opt/homebrew|usr/local|home/linuxbrew/\.linuxbrew)/Cellar/([^/]+)/")
VERSION_RE = re.compile(
    r"(?<![\w.])v?(\d+(?:\.\d+)+)(?:-((?:alpha|beta|rc|pre|preview|dev)[0-9A-Za-z.]*))?",
    re.IGNORECASE,
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# What each Mac installer copies out of the runtime, so a copy can be compared blob by blob with
# main.  `files` are (installed path, repo path);  `trees` are (installed dir, repo dir, recursive):
# recursive mirrors rsync --delete without tests/ and __pycache__/ (install-fleet-rag.sh), and
# not recursive is the top-level *.py files (install-fleet-secrets.sh).
COPY_SETS = {
    "recall": {
        "root": "apps/fleet-rag",
        "entry": "recall",
        "files": (
            ("recall", "scripts/recall"),
            ("recall-tunnel", "scripts/recall-tunnel"),
            ("fleet-recall-mcp.py", "scripts/fleet-recall-mcp.py"),
            ("fleet-rag.py", "scripts/fleet-rag.py"),
        ),
        "trees": (("fleet_rag", "scripts/fleet_rag", True),),
        "fix": "bash scripts/install-fleet-rag.sh",
    },
    "fleet-mode": {
        "root": "apps",
        "entry": "fleet-mode",
        "files": (("fleet-mode", "scripts/fleet-mode"), ("FLEET-MODE.md", "docs/FLEET-MODE.md")),
        "trees": (),
        "fix": "copy scripts/fleet-mode and docs/FLEET-MODE.md to ~/apps",
    },
    "fleet-secrets": {
        "root": ".local/share/fleet-secrets",
        "entry": "fleet-secrets",
        "files": (("fleet-secrets", "scripts/fleet-secrets"),),
        "trees": (("fleet_secrets", "scripts/fleet_secrets", False),),
        "fix": "bash scripts/install-fleet-secrets.sh",
    },
}
# The server's wrappers exec current/<script> (fleet-cli-release.py wrapper_files).
SERVER_WRAPPERS = {
    "simple-chat": "scripts/simple-chat",
    "simple-notes": "scripts/simple-notes",
    "simple-board": "scripts/simple-board",
    "recall": "scripts/recall",
    "lane": "scripts/fleet_lanes/bin/lane",
    "fleet-mode": "scripts/fleet-mode",
}
RUNPY = (
    "import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); "
    "runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
)


def release_policy():
    """scripts/fleet-cli-release.py from the AI-Fleet-Coordinator checkout, or None."""
    if not _RELEASE_POLICY:
        module = None
        repo = find_repo("AI-Fleet-Coordinator")
        path = repo / "scripts/fleet-cli-release.py" if repo else None
        if path and path.exists():
            try:
                import importlib.util

                spec = importlib.util.spec_from_file_location("fleet_cli_release_policy", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                module = None
        _RELEASE_POLICY.append(module)
    return _RELEASE_POLICY[0]


def is_shipped(path: str) -> bool:
    policy = release_policy()
    if policy is not None and callable(getattr(policy, "allowed_path", None)):
        return bool(policy.allowed_path(path))
    return path in SHIPPED_EXACT or bool(
        re.fullmatch(r"scripts/agent_sync/(?:mcp/)?[a-z][a-z0-9_]*\.py", path)
        or path in ("scripts/agent_sync/__init__.py", "scripts/agent_sync/mcp/__init__.py")
    )


# The entry file of each tool the release added with AFC-331, and the modules it cannot run without.
TOOL_FILES = (
    "scripts/recall",
    "scripts/fleet_rag/core.py",
    "scripts/fleet_rag/recall_api.py",
    "scripts/fleet_lanes/bin/lane",
    "scripts/fleet_lanes/lane.py",
    "scripts/fleet_lanes/layout.py",
    "scripts/fleet-mode",
    "docs/FLEET-MODE.md",
)


def seat_file(seat: str) -> str:
    return seat.replace("-", "_").upper()


def title_case(seat: str) -> str:
    return "-".join(part.capitalize() for part in seat.split("-"))


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, check: str, status: str, detail: str, **fields) -> None:
        """One row.  `check`, `status` and `detail` are the original contract (fleet_config and the
        server's --post read only these);  `fields` adds the AFC-461 ones (state, category, ...)."""
        row = {"check": check, "status": status, "detail": detail}
        row.update(fields)
        self.rows.append(row)

    def row(self, check: str) -> dict | None:
        for row in self.rows:
            if row["check"] == check:
                return row
        return None

    @property
    def failed(self) -> bool:
        return any(r["status"] == "FAIL" for r in self.rows)


def git_try(repo: Path, *args: str, runner: Callable = subprocess.run) -> str | None:
    """Stdout of a git command, or None when it fails (so a failure is never read as an empty answer)."""
    try:
        out = runner(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git(repo: Path, *args: str, runner: Callable = subprocess.run) -> str:
    return git_try(repo, *args, runner=runner) or ""


def remote_ls_remote(url: str, runner: Callable = subprocess.run) -> str:
    try:
        out = runner(
            ["git", "ls-remote", url, "refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else ""


def remote_main_with_source(
    repo: Path, network: bool, runner: Callable = subprocess.run
) -> tuple[str, str]:
    """(sha, source):  "remote" when GitHub answered in this run, "local-ref" for the checkout's own
    origin/main ref, which is never enough to call anything current (AFC-461)."""
    if network:
        line = git(repo, "ls-remote", "origin", "refs/heads/main", runner=runner)
        if line:
            return line.split()[0], "remote"
    sha = git(repo, "rev-parse", "origin/main", runner=runner)
    return sha, ("local-ref" if sha else "")


def remote_main(repo: Path, network: bool, runner: Callable = subprocess.run) -> str:
    return remote_main_with_source(repo, network, runner)[0]


def read_tip(network: bool, runner: Callable = subprocess.run) -> dict:
    """The tip of AFC refs/heads/main on GitHub, read once per run:  the expected revision for every
    fleet tool.  `rev` is None (with `error`) when it was not read, and then nothing is current."""
    expected = {
        "repo": AFC_REMOTE_URL,
        "ref": "refs/heads/main",
        "source": "git ls-remote",
        "rev": None,
        "readAt": None,
        "error": None,
    }
    if not network:
        expected["error"] = "not read (--no-network)"
        return expected
    try:
        out = runner(
            ["git", "ls-remote", AFC_REMOTE_URL, "refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        expected["error"] = f"git ls-remote failed ({type(exc).__name__})"
        return expected
    first = (out.stdout or "").split()
    if out.returncode != 0 or not first or not SHA_RE.match(first[0]):
        expected["error"] = f"git ls-remote failed (exit {out.returncode})"
        return expected
    expected["rev"] = first[0]
    expected["readAt"] = utc_now()
    return expected


def utc_now(clock: Callable = time.time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def host_kind() -> str:
    """ "mac" or "server":  which half of the fleet this doctor is reporting for."""
    return "mac" if sys.platform == "darwin" else "server"


def short(sha: str | None) -> str:
    return sha[:7] if sha else "?"


def runtime_fields(rt: dict, reason: str) -> dict:
    """A tool that runs straight from the runtime is exactly as current as the runtime."""
    return {
        "state": rt["state"],
        "installed": rt.get("head"),
        "expectedRev": rt.get("tip"),
        "behindCommits": rt.get("behind"),
        "grace": rt.get("grace"),
        "reason": reason + ("" if rt["state"] == "current" else ";  " + (rt.get("reason") or "")),
    }


def git_has_commit(repo: Path, sha: str, runner: Callable = subprocess.run) -> bool:
    return git_try(repo, "cat-file", "-e", sha + "^{commit}", runner=runner) is not None


def commits_between(
    repo: Path, old: str, new: str, runner: Callable = subprocess.run
) -> int | None:
    """How many commits `old` is behind `new`, or None when either is not in the object store."""
    if not (git_has_commit(repo, old, runner) and git_has_commit(repo, new, runner)):
        return None
    out = git_try(repo, "rev-list", "--count", f"{old}..{new}", runner=runner)
    return int(out) if out and out.isdigit() else None


def _resolve_root(candidate: str) -> Path:
    """A root named in the environment, read the way the fleet reads one.

    `~` is expanded against the doctor's home and a relative path is joined onto
    it, never onto the working directory, so a host that names a root resolves it
    the same way `fleet_lanes/server_working_dirs.py` and `fleet_lanes/layout.py`
    resolve one.
    """
    raw = candidate.strip()
    if raw == "~":
        return HOME
    if raw.startswith("~/"):
        return HOME / raw[2:]
    path = Path(raw).expanduser()
    return path if path.is_absolute() else HOME / path


def code_roots() -> list[Path]:
    """Where the integration checkouts live, most specific first.

    A root the caller configured wins, because that is the root the rest of the
    fleet resolves.  Then the volume the host names, then the server paths that
    carry the trees today, and the copy under the home last.  The owner removed
    the /root aliases on 2026-10-25, so a host that names no root and keeps no
    alias has no tree, and the doctor says so instead of reporting a checkout
    it never found.

    Every candidate is returned, safe or not, because the "no checkout" warning
    has to name the roots that were searched.  `find_repo` is where a root
    another user can write is dropped before anything is run from it.
    """
    roots: list[Path] = []
    configured = (os.environ.get("FLEET_CODE_ROOT") or "").strip()
    if configured:
        roots.append(_resolve_root(configured))
    volume = (os.environ.get("FLEET_SERVER_VOLUME") or "").strip()
    if volume:
        roots.append(_resolve_root(volume) / "Code")
    roots.extend(SERVER_CODE_ROOTS)
    roots.append(HOME / "Code")
    ordered: list[Path] = []
    for root in roots:
        if root not in ordered:
            ordered.append(root)
    return ordered


def find_repo(name: str, euid: int | None = None) -> Path | None:
    """The checkout of `name` under the first root that holds one.

    Run as root, a root another user can write is skipped (see `dir_is_root_safe`),
    so the doctor never runs a git command or a checkout's code as root out of a
    tree it does not trust.  Every checkout the doctor reads reaches it through
    here, so the check covers `release_policy` and `check_checkouts` alike.
    """
    as_root = (os.geteuid() if euid is None else euid) == 0
    for root in code_roots():
        if as_root and not dir_is_root_safe(str(root)):
            continue
        if (root / name / ".git").exists():
            return root / name
    return None


def read_json_file(path: Path, limit: int = 2 * 1024 * 1024) -> dict | None:
    """A JSON object from a small local file, or None when it is absent, too big or malformed."""
    try:
        if path.stat().st_size > limit:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_utc(text) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError, OverflowError):
        return None


def systemd_timer(
    unit: str, runner: Callable = subprocess.run, clock: Callable = time.time
) -> dict | None:
    """What systemd says about a timer, or None when systemctl could not answer.

    A timer's LastTriggerUSec only moves when the timer itself fires.  The service is also
    started by hand and by GitHub on each merge, and those runs never touch it, so a service
    that ran a minute ago can sit behind a timer that "last fired" hours back.  The last run is
    the later of the timer's trigger and the paired service's start.
    """
    service = unit[: -len(".timer")] + ".service" if unit.endswith(".timer") else None
    try:
        out = runner(
            [
                "systemctl",
                "show",
                "--timestamp=unix",
                "-p",
                "LoadState,ActiveState,LastTriggerUSec,NextElapseUSecRealtime,ExecMainStartTimestamp",
                *([unit, service] if service else [unit]),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # The timer's block comes first, so its LoadState and ActiveState are the first seen.
    props: dict = {}
    stamps = []
    for line in (out.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in ("LastTriggerUSec", "ExecMainStartTimestamp"):
            match = re.match(r"@(\d+)", value)
            if match:
                stamps.append(int(match.group(1)))
        else:
            props.setdefault(key, value)
    if "LoadState" not in props:
        return None
    last = max(stamps) if stamps else None
    return {
        "unit": unit,
        "loadState": props.get("LoadState"),
        "activeState": props.get("ActiveState"),
        "lastTrigger": last,
        "minutesSince": int((clock() - last) // 60) if last else None,
    }


def timer_on_schedule(timer: dict | None) -> tuple[bool, str]:
    """(on schedule, why not):  loaded, active, and fired within GRACE_MINUTES."""
    if timer is None:
        return False, "systemctl could not report the timer"
    unit = timer["unit"]
    if timer.get("loadState") == "not-found":
        return False, f"{unit} is not installed"
    if timer.get("activeState") != "active":
        return False, f"{unit} is {timer.get('activeState') or 'not active'}"
    minutes = timer.get("minutesSince")
    if minutes is None:
        return False, f"{unit} has not fired yet"
    if minutes > GRACE_MINUTES:
        return False, f"{unit} last fired {minutes} minutes ago"
    return True, f"{unit} fired {minutes} minutes ago"


def check_server_timers(report: Report, runner: Callable = subprocess.run) -> dict:
    """The jobs that keep the server current and run this doctor there (`updater:<unit>`)."""
    timers = {}
    for unit in SERVER_TIMERS:
        timer = systemd_timer(unit, runner)
        timers[unit] = timer
        ok, why = timer_on_schedule(timer)
        check = f"updater:{unit}"
        fields = {"category": "fleet-tool", "tool": None, "reason": why}
        if timer is None:
            report.add(check, "WARN", why, state="unknown", **fields)
        elif timer.get("loadState") == "not-found":
            report.add(
                check, "FAIL", f"{why}:  the server has no such timer", state="missing", **fields
            )
        elif ok:
            report.add(check, "OK", f"active;  {why}", state="current", **fields)
        elif timer.get("minutesSince") is None and timer.get("activeState") == "active":
            report.add(check, "WARN", why, state="unknown", **fields)
        else:
            report.add(check, "FAIL", why, state="stale", **fields)
    return timers


def check_git_runtime(
    report: Report,
    checkout: Path,
    network: bool,
    runner: Callable = subprocess.run,
    expected: dict | None = None,
) -> dict:
    """The server runs from a git checkout (fleet-cli-git-update.py, 2026-10-19):  is it at the tip of
    main on GitHub, and does `current` (which the wrappers exec) resolve to it?"""
    expected = expected if expected is not None else read_tip(network, runner)
    tip = expected.get("rev")
    head = git_try(checkout, "rev-parse", "HEAD", runner=runner) or ""
    state = read_json_file(RUNTIME / "git-state.json") or {}
    try:
        points = (RUNTIME / "current").resolve() == checkout.resolve()
        target = str((RUNTIME / "current").resolve())
    except (OSError, RuntimeError):
        points, target = False, "nothing"
    rt = {"head": head or None, "tip": tip, "behind": None, "grace": None}
    fields = {"category": "fleet-tool", "tool": None, "installed": head or None, "expectedRev": tip}

    def finish(status: str, detail: str, row_state: str, reason: str) -> dict:
        rt.update(state=row_state, status=status, reason=reason)
        report.add(
            "runtime",
            status,
            detail,
            state=row_state,
            behindCommits=rt["behind"],
            grace=rt["grace"],
            reason=reason,
            **fields,
        )
        return rt

    if not head:
        return finish("WARN", f"cannot read HEAD in {checkout}", "unknown", "git rev-parse failed")
    if not points:
        why = f"{RUNTIME / 'current'} resolves to {target}, not {checkout}"
        return finish("FAIL", f"{why}:  the wrappers run that", "stale", why)
    if not tip:
        why = f"the tip of main was not read ({expected.get('error')})"
        return finish("WARN", f"running from git at {short(head)};  {why}", "unknown", why)
    if head == tip:
        return finish(
            "OK", f"running from git at the tip of main {short(tip)}", "current", "at the tip"
        )
    rt["behind"] = commits_between(checkout, head, tip, runner)
    count = "count unknown" if rt["behind"] is None else f"{rt['behind']} commits behind"
    on_schedule, timer_why = timer_on_schedule(systemd_timer(SERVER_TIMERS[0], runner))
    bad = state.get("bad") == tip
    reasons = []
    if bad:
        reasons.append(
            f"main {short(tip)} failed the smoke test ({state.get('bad_reason')});  staying on {short(head)} until main moves"
        )
    if not on_schedule:
        reasons.append(timer_why)
    rt["grace"] = on_schedule and not bad
    if rt["grace"]:
        why = f"behind within grace:  {timer_why}, so the next update moves it"
        return finish(
            "WARN",
            f"running from git at {short(head)}, main {short(tip)} ({count}):  {why}",
            "stale",
            why,
        )
    why = ";  ".join(reasons)
    return finish(
        "FAIL",
        f"running from git at {short(head)}, main {short(tip)} ({count}):  {why}:  run systemctl start fleet-cli-git-update",
        "stale",
        why,
    )


def launchd_job(label: str, runner: Callable = subprocess.run) -> dict | None:
    """launchctl's view of a LaunchAgent in this user's GUI domain, or None when launchctl failed."""
    try:
        out = runner(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return {"label": label, "loaded": False}
    top = {}
    for line in (out.stdout or "").splitlines():
        match = re.match(r"^\t([a-z][a-z ]*?) = (.*)$", line)
        if match and match.group(1) not in top:
            top[match.group(1)] = match.group(2).strip()
    runs = top.get("runs", "")
    return {
        "label": label,
        "loaded": True,
        "runs": int(runs) if runs.isdigit() else None,
        "lastExit": top.get("last exit code"),
        "state": top.get("state"),
    }


def previous_host(previous: dict | None, host: str) -> dict:
    """This host's document from the previous combined report, or {}."""
    for doc in (previous or {}).get("hosts") or []:
        if isinstance(doc, dict) and doc.get("host") == host:
            return doc
    return {}


def check_mac_updater(
    report: Report,
    runner: Callable = subprocess.run,
    previous: dict | None = None,
    clock: Callable = time.time,
) -> dict:
    """com.jay.agent-sync-runtime-sync:  loaded, not paused, exiting cleanly, and still being started
    (its launchd run counter moved since the previous report)."""
    job = launchd_job(MAC_UPDATER_LABEL, runner) or {"label": MAC_UPDATER_LABEL, "loaded": None}
    prev = previous_host(previous, "mac")
    prev_runs = (prev.get("updater") or {}).get("runs")
    prev_at = parse_utc((previous or {}).get("generatedAt"))
    # Two runs of the 120-second job, and a margin, must fit between the reports to judge the counter.
    judged = prev_at is not None and clock() - prev_at >= 270 and isinstance(prev_runs, int)
    runs = job.get("runs")
    advanced = (runs != prev_runs) if (judged and runs is not None) else None
    paused = (HOME / ".agent-sync/runtime-sync.pause").exists()
    info = dict(job, paused=paused, previousRuns=prev_runs, advanced=advanced)
    check = f"updater:{MAC_UPDATER_LABEL}"
    fields = {"category": "fleet-tool", "tool": None}
    clean = job.get("lastExit") in (None, "0", "(never exited)")
    if job.get("loaded") is None:
        why = "launchctl print failed"
        report.add(check, "WARN", why, state="unknown", reason=why, **fields)
    elif not job["loaded"]:
        why = "the LaunchAgent is not loaded"
        report.add(
            check,
            "FAIL",
            f"{why}:  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{MAC_UPDATER_LABEL}.plist",
            state="missing",
            reason=why,
            **fields,
        )
    elif paused:
        why = "paused by ~/.agent-sync/runtime-sync.pause"
        report.add(
            check, "FAIL", f"{why} (remove it to resume)", state="stale", reason=why, **fields
        )
    elif not clean:
        why = f"its last run exited {job.get('lastExit')}"
        report.add(
            check,
            "FAIL",
            f"{why}:  see ~/apps/logs/agent-sync-runtime-sync.log",
            state="stale",
            reason=why,
            **fields,
        )
    elif advanced is False:
        why = f"its run counter is still {runs} since the previous report:  launchd is not starting it"
        report.add(check, "FAIL", why, state="stale", reason=why, **fields)
    elif advanced is None:
        why = f"{runs} runs;  no previous report old enough to show the counter moving"
        report.add(check, "WARN", why, state="unknown", reason=why, **fields)
    else:
        why = f"loaded, {runs} runs (was {prev_runs}), last exit 0"
        report.add(check, "OK", why, state="current", reason=why, **fields)
    return info


def check_mac_runtime(
    report: Report,
    runner: Callable = subprocess.run,
    expected: dict | None = None,
    updater: dict | None = None,
    previous: dict | None = None,
) -> dict:
    """The Mac's managed runtime (simple-chat, board, and the source of every Mac copy) against the tip.

    Behind is amber only while the updater is healthy and has not yet had its chance:  it is loaded,
    not paused, exits cleanly, its run counter moved, the worktree has no tracked changes, and this
    runtime was not already behind the same tip in the previous report.  A tip that failed the
    updater's smoke test is amber too:  the updater waits for main to move.  The runtime shares its
    object store with ~/Code and every lane, so "the tip is already fetched" cannot tell whether
    the updater has seen it, and is not used."""
    expected = expected or {}
    updater = updater or {}
    tip = expected.get("rev")
    head = git_try(MAC_RUNTIME, "rev-parse", "HEAD", runner=runner) or ""
    rt = {"head": head or None, "tip": tip, "behind": None, "grace": None}
    fields = {"category": "fleet-tool", "tool": None, "installed": head or None, "expectedRev": tip}

    def finish(status: str, detail: str, row_state: str, reason: str) -> dict:
        rt.update(state=row_state, status=status, reason=reason)
        report.add(
            "runtime",
            status,
            detail,
            state=row_state,
            behindCommits=rt["behind"],
            grace=rt["grace"],
            reason=reason,
            **fields,
        )
        return rt

    if not head:
        return finish(
            "WARN", f"cannot read HEAD in {MAC_RUNTIME}", "unknown", "git rev-parse failed"
        )
    if not tip:
        why = f"the tip of main was not read ({expected.get('error') or 'no expected revision'})"
        return finish("WARN", f"runtime at {short(head)};  {why}", "unknown", why)
    if head == tip:
        return finish("OK", f"runtime at the tip of main {short(tip)}", "current", "at the tip")
    rt["behind"] = commits_between(MAC_RUNTIME, head, tip, runner)
    count = "count unknown" if rt["behind"] is None else f"{rt['behind']} commits behind"
    blocking = []
    if updater.get("paused"):
        blocking.append("the updater is paused (~/.agent-sync/runtime-sync.pause)")
    tracked = git_try(MAC_RUNTIME, "status", "--porcelain", "--untracked-files=no", runner=runner)
    if tracked:
        blocking.append("tracked changes in the runtime block the updater")
    if updater.get("loaded") is False:
        blocking.append(f"{MAC_UPDATER_LABEL} is not loaded")
    elif updater.get("lastExit") not in (None, "0", "(never exited)"):
        blocking.append(f"the updater's last run exited {updater.get('lastExit')}")
    if updater.get("advanced") is False:
        blocking.append("the updater's run counter has not moved since the previous report")
    try:
        bad = (HOME / ".agent-sync/runtime-sync.bad-sha").read_text().strip()
    except OSError:
        bad = ""
    prev_rt = next(
        (
            r
            for r in previous_host(previous, "mac").get("checks") or []
            if isinstance(r, dict) and r.get("check") == "runtime"
        ),
        {},
    )
    same_as_before = (
        prev_rt.get("state") == "stale"
        and prev_rt.get("installed") == head
        and prev_rt.get("expectedRev") == tip
    )
    # `grace` is True only on evidence that the updater will catch up, False on evidence that it
    # will not, and None when this run cannot tell (no previous report old enough to judge the
    # run counter, as on a first run):  that is amber, never red, and never a claimed grace.
    if blocking:
        rt["grace"], why = False, ";  ".join(blocking)
    elif bad and bad == tip:
        rt["grace"], why = (
            True,
            f"main {short(tip)} failed the runtime smoke test;  the updater waits for main to move",
        )
    elif updater.get("advanced") is None:
        # Not judged yet, so a "same tip as before" from a report minutes old is no evidence either.
        rt["grace"], why = (
            None,
            "behind;  no previous report old enough to show whether the updater is moving",
        )
    elif same_as_before:
        rt["grace"], why = False, "still behind the same tip as in the previous report"
    elif updater.get("advanced"):
        rt["grace"], why = (
            True,
            "behind within grace:  the updater is running and moves it within 2 minutes",
        )
    else:
        rt["grace"], why = False, "no previous report shows the updater moving"
    status = "FAIL" if rt["grace"] is False else "WARN"
    return finish(
        status, f"runtime at {short(head)}, main {short(tip)} ({count}):  {why}", "stale", why
    )


def check_runtime(
    report: Report,
    network: bool,
    runner: Callable = subprocess.run,
    expected: dict | None = None,
    updater: dict | None = None,
    previous: dict | None = None,
) -> dict:
    checkout = RUNTIME / "repo"
    if (checkout / ".git").exists():
        return check_git_runtime(report, checkout, network, runner, expected)
    # The live release is what `current` points at;  status.json is only the last receive's
    # receipt and goes stale after a rollback.
    live = RUNTIME / "current" / "release.json"
    status_file = live if live.exists() else RUNTIME / "status.json"
    if not status_file.exists():
        if host_kind() == "mac" and (MAC_RUNTIME / ".git").exists():
            return check_mac_runtime(
                report, runner, expected or read_tip(network, runner), updater, previous
            )
        why = f"no {status_file} (a Mac reads the published runtime instead)"
        report.add("runtime", "WARN", why, state="unknown", category="fleet-tool", reason=why)
        return {"state": "unknown", "status": "WARN", "reason": why, "head": None, "tip": None}
    return check_release_runtime(report, status_file, network, runner)


def check_release_runtime(
    report: Report, status_file: Path, network: bool, runner: Callable = subprocess.run
) -> dict:
    """A host still on Mac-pushed release copies (before 2026-10-19)."""
    afc = find_repo("AI-Fleet-Coordinator")
    status = json.loads(status_file.read_text())
    status.setdefault("status", "paused" if (RUNTIME / "paused").exists() else "ready")
    commit = str(status.get("source_commit", ""))
    if (RUNTIME / "paused").exists():
        report.add(
            "runtime:updates",
            "WARN",
            "paused (fleet-cli-release resume turns the Mac's pushes back on)",
            state="unknown",
            category="fleet-tool",
        )
    # A host with no checkout (the Hetzner server may have none) asks GitHub directly.
    if afc:
        main, source = remote_main_with_source(afc, network, runner)
    else:
        main, source = (remote_ls_remote(AFC_REMOTE_URL, runner), "remote") if network else ("", "")
    fields = {"category": "fleet-tool", "installed": commit or None, "expectedRev": main or None}
    rt = {"head": commit or None, "tip": main or None}

    def finish(status_word: str, detail: str, row_state: str) -> dict:
        report.add("runtime", status_word, detail, state=row_state, reason=detail, **fields)
        rt.update(state=row_state, status=status_word, reason=detail)
        return rt

    if not main:
        return finish(
            "WARN", f"release {commit[:7]} ({status.get('status')}); origin/main unknown", "unknown"
        )
    if commit == main:
        if source != "remote":
            return finish(
                "WARN",
                f"release built from the local origin/main ref {main[:7]};  GitHub was not read",
                "unknown",
            )
        return finish("OK", f"release built from origin/main {main[:7]}", "current")
    raw = (
        git_try(afc, "diff", "--name-only", commit, main, "--", *SHIPPED, runner=runner)
        if afc
        else None
    )
    if raw is None:
        # No checkout, or a commit not fetched here:  the doctor cannot tell, so it says so.
        return finish(
            "WARN",
            f"release built from {commit[:7]}, origin/main {main[:7]}:  cannot compare (no checkout, or a commit not fetched here)",
            "unknown",
        )
    changed = [line for line in raw.splitlines() if is_shipped(line)]
    if not changed:
        detail = f"release built from {commit[:7]};  no shipped file changed up to origin/main {main[:7]}"
        if source != "remote":
            return finish("WARN", detail + " (a local ref;  GitHub was not read)", "unknown")
        return finish("OK", detail, "current")
    return finish(
        "FAIL",
        f"release built from {commit[:7]};  {len(changed)} shipped files changed up to origin/main {main[:7]}:  the Mac's fleet-cli-sync has not pushed them",
        "stale",
    )


def check_checkouts(
    report: Report, network: bool, runner: Callable = subprocess.run, host: str | None = None
) -> dict:
    """Each CLI checkout against its GitHub main.  On the Mac these read ~/Code, the human tree,
    so they are tagged `human-tree` and Muster leaves them out of its verdict."""
    category = "human-tree" if (host or host_kind()) == "mac" else "fleet-tool"
    states = {}
    for name in REPOS:
        check = f"checkout:{name}"
        repo = find_repo(name)
        if not repo:
            why = "no checkout under " + ", ".join(str(root) for root in code_roots())
            report.add(check, "WARN", why, state="unknown", category=category, reason=why)
            states[name] = "unknown"
            continue
        head = git(repo, "rev-parse", "HEAD", runner=runner)
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", runner=runner)
        main, source = remote_main_with_source(repo, network, runner)
        fields = {"category": category, "installed": head or None, "expectedRev": main or None}
        if branch != "main":
            row_state, status, detail = "stale", "WARN", f"on {branch}, not main"
        elif main and head != main:
            row_state, status, detail = (
                "stale",
                "FAIL",
                f"main at {head[:7]}, origin/main {main[:7]}:  run code-main-sync",
            )
        elif main and source == "remote":
            row_state, status, detail = "current", "OK", f"main {head[:7]}"
        else:
            row_state, status, detail = (
                "unknown",
                "WARN",
                f"main {head[:7]};  GitHub was not read, so only the local origin/main ref was compared",
            )
        report.add(check, status, detail, state=row_state, reason=detail, **fields)
        states[name] = row_state
    return states


def dir_is_root_safe(directory: str) -> bool:
    """Whether root may run binaries from `directory`:  owned by root and not group- or world-writable.

    A directory another user can write (``/home/jay/.local/bin``) can hold a binary that user
    planted, and the doctor runs what it finds (``probe_version`` runs ``<tool> --version``), so
    as root such a directory is never searched.  A missing directory is harmless and kept.
    """
    try:
        info = os.stat(directory)
    except OSError:
        return True
    return info.st_uid == 0 and not (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def tool_path_dirs(euid: int | None = None) -> list[str]:
    """The registry's search path:  TOOL_PATH_DIRS, then the caller's PATH.

    Run as root, a directory another user can write is left out (see ``dir_is_root_safe``), so
    the doctor never executes a binary as root from one.  The ``jay`` login's own tools are
    checked by running the doctor as ``jay``, where ``{home}/.local/bin`` is that directory.
    """
    as_root = (os.geteuid() if euid is None else euid) == 0
    dirs: list[str] = []
    for entry in [d.format(home=HOME) for d in TOOL_PATH_DIRS] + os.environ.get("PATH", "").split(
        os.pathsep
    ):
        if not entry or entry in dirs:
            continue
        if as_root and not dir_is_root_safe(entry):
            continue
        dirs.append(entry)
    return dirs


def command_path(name: str) -> str | None:
    found = shutil.which(name, path=os.pathsep.join(tool_path_dirs()))
    if found:
        return found
    for directory in COMMAND_FALLBACK_DIRS:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return f"{candidate} (not on PATH)"
    return None


def check_commands(report: Report) -> None:
    """Presence only:  the `tool:<name>` rows carry each command's currency."""
    for name in COMMANDS:
        path = command_path(name)
        report.add(
            f"command:{name}",
            "OK" if path else "FAIL",
            path or "not on PATH",
            state="current" if path else "missing",
            category="presence",
            tool=name,
            reason="present (presence only;  currency is on tool:" + name + ")"
            if path
            else "not found",
        )


def check_catalogs(report: Report, host: str | None = None) -> None:
    base = RUNTIME / "current"
    if not base.exists():
        repo = find_repo("AI-Fleet-Coordinator")
        base = repo if repo else base
    human = (host or host_kind()) == "mac" and not str(base).startswith(str(RUNTIME))
    for rel, required in CATALOGS:
        present = (base / rel).exists()
        status = "OK" if present else ("FAIL" if required else "WARN")
        report.add(
            f"catalog:{Path(rel).name}",
            status,
            f"{'present' if present else 'missing'} in {base}",
            state="current" if present else "missing",
            category="human-tree" if human else "presence",
        )


def runtime_base() -> Path | None:
    """The tree whose files this host runs:  the fleet-cli release, else the Mac's published runtime,
    else a checkout."""
    for base in (RUNTIME / "current", MAC_RUNTIME):
        if base.exists():
            return base
    return find_repo("AI-Fleet-Coordinator")


def check_tool_files(report: Report) -> None:
    base = runtime_base()
    for rel in TOOL_FILES:
        present = bool(base and (base / rel).is_file())
        report.add(
            f"tool-file:{rel}",
            "OK" if present else "FAIL",
            f"{'present' if present else 'missing'} in {base}",
            state="current" if present else "missing",
            category="presence",
        )


def check_fleet_mode_doc(report: Report, rt: dict | None = None) -> None:
    """The Mac keeps a copy of the doc at ~/apps/FLEET-MODE.md;  the repo copy is canonical."""
    if not MAC_FLEET_MODE_DOC.is_file():
        return  # the server has no ~/apps copy:  fleet-mode reads the release's docs/FLEET-MODE.md
    base = runtime_base()
    repo_doc = base / "docs/FLEET-MODE.md" if base else None
    rt_state = (rt or {}).get("state", "unknown")
    if not repo_doc or not repo_doc.is_file():
        report.add(
            "fleet-mode-doc",
            "WARN",
            f"no docs/FLEET-MODE.md in {base} to compare with {MAC_FLEET_MODE_DOC}",
            state="unknown",
            category="fleet-tool",
        )
    elif repo_doc.read_bytes() == MAC_FLEET_MODE_DOC.read_bytes():
        report.add(
            "fleet-mode-doc",
            "OK",
            f"{MAC_FLEET_MODE_DOC} equals the repo copy",
            # Equal to the runtime's copy is current only while the runtime is at the tip.
            state="current" if rt_state == "current" else "unknown",
            category="fleet-tool",
            reason=None if rt_state == "current" else "the runtime is not confirmed at the tip",
        )
    else:
        report.add(
            "fleet-mode-doc",
            "WARN",
            f"{MAC_FLEET_MODE_DOC} differs from {repo_doc}:  edit docs/FLEET-MODE.md in the repo and copy it to ~/apps",
            state="stale",
            category="fleet-tool",
        )


# The Zulip key prefix where it differs from the seat (agent_sync/zulip.py SEAT_FILE_OVERRIDES).
ZULIP_PREFIX_OVERRIDES = {"GROK": "GROK_BUILD"}
_names_cache: dict[str, frozenset[str] | None] = {}


def infisical_names(
    folder: str, env: dict[str, str], project: str = "fleet-ops"
) -> frozenset[str] | None:
    """Key NAMES in one Infisical folder (`fleet-secrets names`:  no value reaches this process), or None when
    fleet-secrets is missing or fails.  Fleet-OPS unless `project` says otherwise.  Once per folder per run."""
    cache_key = folder if project == "fleet-ops" else f"{project}:{folder}"
    if cache_key in _names_cache:
        return _names_cache[cache_key]
    binary = (
        env.get("FLEET_SECRETS_BIN")
        or shutil.which("fleet-secrets")
        or str(HOME / ".local/bin/fleet-secrets")
    )
    names: frozenset[str] | None = None
    try:
        proc = subprocess.run(
            [binary, "names", "--path", folder, "--project", project],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0:
            names = frozenset(line.strip() for line in proc.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        names = None
    _names_cache[cache_key] = names
    return names


def check_seat(report: Report, seat: str, env: dict[str, str]) -> None:
    code = seat_file(seat)
    zulip_names = infisical_names("/zulip", env)
    if zulip_names is None:
        report.add(f"seat:{seat}", "WARN", "fleet-secrets could not list Infisical Fleet-OPS names")
        return
    prefix = ZULIP_PREFIX_OVERRIDES.get(code, code)
    zulip = all(f"{prefix}_ZULIP_{part}" in zulip_names for part in ("EMAIL", "KEY", "SITE"))
    linear_names = infisical_names("/linear", env) or frozenset()
    linear_env = bool(
        env.get(f"LINEAR_{code}_CLIENT_ID") and env.get(f"LINEAR_{code}_CLIENT_SECRET")
    )
    linear = (
        linear_env or {f"{code}_LINEAR_CLIENT_ID", f"{code}_LINEAR_CLIENT_SECRET"} <= linear_names
    )
    fleetlink = bool(env.get(f"FLEETLINK_TOKEN_{code}")) or f"FLEETLINK_TOKEN_{code}" in (
        infisical_names("/fleetlink", env, "ai-fleet-coordinator") or frozenset()
    )
    missing = [
        label
        for label, ok in (
            ("zulip bot", zulip),
            ("linear app", linear),
            ("fleetlink token", fleetlink),
        )
        if not ok
    ]
    report.add(
        f"seat:{seat}",
        "OK" if not missing else "FAIL",
        "all credentials present" if not missing else "missing " + ", ".join(missing),
    )


MCP_PROBE = b"".join(
    json.dumps(m).encode() + b"\n"
    for m in (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fleet-tools-doctor", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
)


class ClientConfigError(ValueError):
    """A named client source is invalid; its contents are never included."""


def client_servers(seat: str, home: Path = HOME) -> dict:
    source = "Claude client JSON" if seat == "CLAUDE" else "Codex client TOML"
    try:
        if seat == "CLAUDE":
            data = json.loads((home / ".claude.json").read_text())
            field = "mcpServers"
        elif seat == "CODEX":
            text = (home / ".codex/config.toml").read_text()
            try:
                import tomllib
            except ImportError:
                # Python < 3.11 (a Mac's /usr/bin/python3 is 3.9) has no TOML parser:  fall back to the
                # table headers for the server names.  Their env cannot be read, so none is forwarded.
                return {name: {} for name in codex_mcp_names(text)}
            data = tomllib.loads(text)
            field = "mcp_servers"
        else:
            return {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, ImportError):
        raise ClientConfigError(source + " unavailable or malformed") from None
    if not isinstance(data, dict) or not isinstance(data.get(field, {}), dict):
        raise ClientConfigError(source + " has invalid server registry")
    servers = data.get(field, {})
    for name, server in servers.items():
        if not isinstance(name, str) or not isinstance(server, dict):
            raise ClientConfigError(source + " has invalid server entry")
        env = server.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
        ):
            raise ClientConfigError(source + " has invalid server environment")
    return servers


def registered_env(seat: str, home: Path = HOME) -> dict[str, str]:
    """Client files supply only nonsecret locale settings, never credentials or identity.

    Approved credentials come from the process environment or fleet-secrets.  Client-file
    values cannot override either source or redirect credential lookup.
    """
    server = client_servers(seat, home).get("simple-chat", {})
    env = server.get("env", {})
    return {key: value for key, value in env.items() if key in {"LANG", "LC_ALL"}}


def check_mcp_startup(report: Report, seat: str, runner: Callable = subprocess.run) -> None:
    """Start the seat's stdio MCP the way a client does and ask for its tools.  A release that is
    missing a file the server reads fails here, before any agent notices (2026-10-16)."""
    if not shutil.which("simple-chat"):
        report.add(f"mcp-start:{seat}", "FAIL", "simple-chat is not on PATH")
        return
    try:
        result = runner(
            ["simple-chat", "mcp", "--default-seat=%s" % seat],
            input=MCP_PROBE,
            env={**registered_env(seat), **os.environ},
            capture_output=True,
            timeout=60,
            check=False,
        )
    except ClientConfigError as exc:
        report.add(f"mcp-start:{seat}", "FAIL", str(exc))
        return
    except subprocess.TimeoutExpired:
        report.add(f"mcp-start:{seat}", "FAIL", "simple-chat mcp did not answer within 60 seconds")
        return
    count = None
    for line in (result.stdout or b"").splitlines():
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if reply.get("id") == 2:
            count = len(((reply.get("result") or {}).get("tools")) or [])
    if count:
        report.add(f"mcp-start:{seat}", "OK", f"simple-chat mcp serves {count} tools")
    else:
        first = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()[:1]
        why = first[0][:160] if first else f"exit {result.returncode}"
        report.add(f"mcp-start:{seat}", "FAIL", f"simple-chat mcp did not list its tools:  {why}")


def codex_mcp_names(text: str) -> list[str]:
    """Server names in a Codex config.toml:  quoted keys, sub-tables and inline tables all count."""
    try:
        import tomllib
    except ImportError:  # Python < 3.11:  fall back to the table headers
        names: list[str] = []
        for line in text.splitlines():
            m = re.match(r'\s*\[mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))', line)
            if m and (m.group(1) or m.group(2)) not in names:
                names.append(m.group(1) or m.group(2))
        return names
    try:
        return list((tomllib.loads(text).get("mcp_servers") or {}).keys())
    except tomllib.TOMLDecodeError:
        return []


def check_mcp(report: Report) -> None:
    for seat in ("CODEX", "CLAUDE"):
        label = "mcp:" + seat.lower()
        try:
            names = sorted(client_servers(seat, HOME))
        except ClientConfigError as exc:
            report.add(label, "FAIL", str(exc))
            continue
        lacking = [name for name in ("simple-chat", "fleetlink") if name not in names]
        report.add(
            label,
            "WARN" if lacking else "OK",
            "registered: "
            + (", ".join(names) or "none")
            + (";  missing " + ", ".join(lacking) if lacking else ""),
        )


# --- AFC-461:  fleet tool currency ------------------------------------------------------------


def git_blob(data: bytes) -> str:
    """The git blob id of these bytes (what `git hash-object --no-filters` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_blobs(repo: Path, rev: str, pathspecs, runner: Callable = subprocess.run) -> dict | None:
    """{repo path: blob id} at `rev` for these pathspecs, or None when git cannot answer."""
    out = git_try(repo, "ls-tree", "-r", "-z", rev, "--", *pathspecs, runner=runner)
    if out is None:
        return None
    blobs = {}
    for entry in out.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and path:
            blobs[path] = parts[2]
    return blobs


def file_history(repo: Path, rev: str, path: str, runner: Callable = subprocess.run) -> list | None:
    """[(commit, blob after it)] newest first for one path on `rev`'s history, or None."""
    out = git_try(
        repo,
        "log",
        "--format=%x00%H",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        "-n",
        "300",
        rev,
        "--",
        path,
        runner=runner,
    )
    if out is None:
        return None
    history = []
    for chunk in out.split("\0"):
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        blob = None
        for line in lines[1:]:
            if line.startswith(":"):
                fields = line.split("\t")[0].split()
                if len(fields) >= 4:
                    blob = fields[3]
        if blob:
            history.append((lines[0].strip(), blob))
    return history


def copy_reference(
    repo: Path, rt: dict, runner: Callable = subprocess.run
) -> tuple[str | None, str]:
    """(revision, what it is) that installed copies are compared with:  the tip of main when it was
    read and is in the object store, else the runtime's HEAD (then only a difference is conclusive)."""
    tip, head = rt.get("tip"), rt.get("head")
    if tip and git_has_commit(repo, tip, runner):
        return tip, "tip"
    return (head, "head") if head else (None, "none")


def compare_copy(
    name: str, spec: dict, repo: Path, rt: dict, runner: Callable = subprocess.run, extra_pairs=()
) -> dict:
    """Compare one installed copy with main, file by file.  Returns the row's fields."""
    root = HOME / spec["root"]
    entry = root / spec["entry"]
    tool_fields = {"category": "fleet-tool", "tool": name, "expectedRev": rt.get("tip")}
    if not entry.is_file():
        why = f"{entry} is not installed"
        return dict(
            tool_fields, status="FAIL", state="missing", detail=f"{why}:  {spec['fix']}", reason=why
        )
    ref, kind = copy_reference(repo, rt, runner)
    pathspecs = [repo_path for _, repo_path in spec["files"]] + [d for _, d, _ in spec["trees"]]
    pathspecs += [repo_path for _, repo_path in extra_pairs]
    blobs = tree_blobs(repo, ref, pathspecs, runner) if ref else None
    if blobs is None:
        why = "git could not list main's files to compare with"
        return dict(tool_fields, status="WARN", state="unknown", detail=why, reason=why)
    pairs = list(spec["files"]) + list(extra_pairs)
    for inst_dir, repo_dir, recursive in spec["trees"]:
        for repo_path in sorted(blobs):
            if not repo_path.startswith(repo_dir + "/"):
                continue
            rel = repo_path[len(repo_dir) + 1 :]
            parts = rel.split("/")
            if recursive:
                if any(p in ("tests", "__pycache__") for p in parts) or rel.endswith(".pyc"):
                    continue
            elif len(parts) != 1 or not rel.endswith(".py"):
                continue
            pairs.append((f"{inst_dir}/{rel}", repo_path))
    expected_installed = {inst for inst, _ in pairs}
    extra = []
    for inst_dir, _, recursive in spec["trees"]:
        base = root / inst_dir
        if not base.is_dir():
            continue
        if recursive:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__")]
                for fname in filenames:
                    if fname.endswith(".pyc") or fname == "tests":
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, fname), root)
                    if rel not in expected_installed:
                        extra.append(rel)
        else:
            for child in sorted(base.iterdir()):
                rel = f"{inst_dir}/{child.name}"
                if child.is_file() and child.name.endswith(".py") and rel not in expected_installed:
                    extra.append(rel)
    differ, absent = [], []
    for inst_rel, repo_path in pairs:
        path = Path(inst_rel) if os.path.isabs(inst_rel) else root / inst_rel
        if repo_path not in blobs:
            continue  # a pair whose source is not on main is not this tool's business
        if not path.is_file():
            absent.append(repo_path)
            continue
        try:
            blob = git_blob(path.read_bytes())
        except OSError:
            absent.append(repo_path)
            continue
        if blob != blobs[repo_path]:
            differ.append((repo_path, blob))
    if not (differ or absent or extra):
        if kind == "tip":
            detail = f"every copied file equals main {short(ref)}"
            return dict(
                tool_fields,
                status="OK",
                state="current",
                installed=short(ref),
                detail=detail,
                reason=detail,
            )
        why = f"the copies equal the runtime HEAD {short(ref)}, but the tip of main " + (
            f"{short(rt.get('tip'))} is not in the object store"
            if rt.get("tip")
            else "was not read"
        )
        return dict(
            tool_fields,
            status="WARN",
            state="unknown",
            installed=short(ref),
            detail=why,
            reason=why,
        )
    later: set = set()
    local_edit, oldest_match, oldest_index = [], None, -1
    for repo_path, blob in differ:
        history = file_history(repo, ref, repo_path, runner) or []
        index = next((i for i, (_, b) in enumerate(history) if b == blob), None)
        if index is None:
            local_edit.append(repo_path)
            continue
        later.update(commit for commit, _ in history[:index])
        if index > oldest_index:
            oldest_index, oldest_match = index, history[index][0]
    parts = []
    if differ:
        parts.append(f"{len(differ)} differ ({_names(p for p, _ in differ)})")
    if absent:
        parts.append(f"{len(absent)} not installed ({_names(absent)})")
    if extra:
        parts.append(f"{len(extra)} extra ({_names(extra)})")
    if local_edit:
        installed, behind = "local edit", None
        parts.append(
            f"no commit on main ever held {_names(local_edit)} as installed (a local edit, or a copy from an unmerged branch)"
        )
    else:
        installed = short(oldest_match) if oldest_match else None
        behind = len(later) if differ else None
    against = f"main {short(ref)}" if kind == "tip" else f"the runtime HEAD {short(ref)}"
    count = "" if behind is None else f";  {behind} later commits"
    why = f"files from {against}:  " + ", ".join(parts)
    return dict(
        tool_fields,
        status="FAIL",
        state="stale",
        installed=installed,
        behindCommits=behind,
        detail=f"{root} differs from {against}{count}:  {', '.join(parts)}:  {spec['fix']}",
        reason=why,
    )


def _names(items, limit: int = 4) -> str:
    names = [Path(str(i)).name for i in items]
    return ", ".join(names[:limit]) + (
        f" and {len(names) - limit} more" if len(names) > limit else ""
    )


def add_tool_row(report: Report, name: str, fields: dict) -> None:
    fields = dict(fields)
    report.add(f"tool:{name}", fields.pop("status"), fields.pop("detail"), **fields)


def inherit_row(name: str, rt: dict, how: str) -> dict:
    status = rt.get("status") or STATUS_FOR_STATE.get(rt.get("state", "unknown"), "WARN")
    detail = f"{how};  runtime {rt.get('state')}"
    return dict(
        runtime_fields(rt, how), status=status, detail=detail, category="fleet-tool", tool=name
    )


def check_lane_tools(report: Report, rt: dict, runner: Callable = subprocess.run) -> None:
    """The Mac's ~/apps/lane-tools, through `install_tools plan tools` run from the runtime."""
    scripts = MAC_RUNTIME / "scripts"
    stable = HOME / "apps/lane-tools"
    base = {"category": "fleet-tool", "tool": "lane", "expectedRev": rt.get("tip")}
    if not (scripts / "fleet_lanes/install_tools.py").is_file():
        why = f"no fleet_lanes/install_tools.py in {scripts}"
        add_tool_row(
            report, "lane", dict(base, status="WARN", state="unknown", detail=why, reason=why)
        )
        return
    try:
        out = runner(
            [
                sys.executable,
                "-I",
                "-c",
                RUNPY,
                str(scripts),
                "fleet_lanes.install_tools",
                "plan",
                "tools",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        text, code = out.stdout or "", out.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        text, code = "", f"({type(exc).__name__})"
    header = re.search(r"^== tools\s+stable dir (.+?)\s+\[([a-z]+)\]\s*$", text, re.MULTILINE)
    version = ""
    try:
        version = re.search(
            r"^sha=([0-9a-f]{7,40})$", (stable / "VERSION").read_text(), re.MULTILINE
        ).group(1)
    except (OSError, AttributeError):
        version = ""
    base["installed"] = version[:7] or None
    if code != 0 or not header:
        why = f"install_tools plan answered with text the doctor does not recognise (exit {code})"
        add_tool_row(
            report, "lane", dict(base, status="WARN", state="unknown", detail=why, reason=why)
        )
        return
    plan_state = header.group(2)
    section = text[header.end() :].split("\n== ", 1)[0]
    listed = section.split("already identical", 1)[0]
    changed = re.findall(r"^ {3}(\S+) +(\S.*?)\s*$", listed, re.MULTILINE)
    changed = [(word, rel) for word, rel in changed if not word[0].isdigit()]
    deletes = "WILL DELETE" in section
    shim_changed = re.search(r"^ {3}shim \S+/lane \[(?!same\])", section, re.MULTILINE) is not None
    only_version = (
        plan_state == "stale"
        and [rel for _, rel in changed] == ["VERSION"]
        and not deletes
        and not shim_changed
    )
    if plan_state == "missing":
        why = f"{stable} is not installed"
        add_tool_row(
            report,
            "lane",
            dict(
                base,
                status="FAIL",
                state="missing",
                detail=f"{why}:  python3 -m fleet_lanes.install_tools apply tools",
                reason=why,
            ),
        )
    elif plan_state == "current" or only_version:
        how = (
            f"{stable} equals the runtime's files"
            if plan_state == "current"
            else f"{stable} has every file of the runtime;  only VERSION still records {version[:7]}"
        )
        fields = inherit_row("lane", rt, how)
        fields["installed"] = version[:7] or None
        add_tool_row(report, "lane", fields)
    elif plan_state == "stale":
        names = [rel for _, rel in changed]
        behind = (
            commits_between(MAC_RUNTIME, version, rt["tip"], runner)
            if version and rt.get("tip")
            else None
        )
        count = "" if behind is None else f", {behind} commits behind main"
        why = (
            f"{stable} at {version[:7] or '?'}{count}:  {len(names)} files differ ({_names(names)})"
        )
        add_tool_row(
            report,
            "lane",
            dict(
                base,
                status="FAIL",
                state="stale",
                behindCommits=behind,
                detail=f"{why}:  python3 -m fleet_lanes.install_tools apply tools",
                reason=why,
            ),
        )
    else:
        why = f"install_tools plan says [{plan_state}], which the doctor does not treat as current"
        add_tool_row(
            report, "lane", dict(base, status="WARN", state="unknown", detail=why, reason=why)
        )


def check_mac_tools(report: Report, rt: dict, runner: Callable = subprocess.run) -> None:
    """Mac:  where each fleet tool runs from, and whether that is the tip of main."""
    for command, script in MAC_SIMPLE_TOOLS:
        link = HOME / ".local/bin" / command
        target = MAC_RUNTIME / "scripts" / script
        if not link.exists():
            why = f"{link} is not installed"
            add_tool_row(
                report,
                command,
                dict(
                    status="FAIL",
                    state="missing",
                    detail=why,
                    reason=why,
                    category="fleet-tool",
                    tool=command,
                ),
            )
        elif os.path.realpath(link) == os.path.realpath(target):
            add_tool_row(
                report, command, inherit_row(command, rt, f"{link} links into the runtime")
            )
        else:
            why = f"{link} resolves to {os.path.realpath(link)}, not the managed runtime"
            add_tool_row(
                report,
                command,
                dict(
                    status="FAIL",
                    state="stale",
                    detail=why,
                    reason=why,
                    category="fleet-tool",
                    tool=command,
                ),
            )
    # The `board` wrapper is retired (2026-10-28):  `simple-board` is the entry point and is already
    # checked through FLEET_TOOLS and MAC_SIMPLE_TOOLS, so no `board` row is emitted.  A row for a
    # deliberately retired wrapper would be a permanent false failure in the published report.
    for name in ("recall", "fleet-mode", "fleet-secrets"):
        add_tool_row(report, name, compare_copy(name, COPY_SETS[name], MAC_RUNTIME, rt, runner))
    check_lane_tools(report, rt, runner)
    share = command_path("fleetlink")
    why = "presence only:  FleetLink has no managed runtime on the Mac, so currency is not checked"
    add_tool_row(
        report,
        "fleetlink",
        dict(
            status="WARN" if share else "FAIL",
            state="unknown" if share else "missing",
            detail=f"{share};  {why}" if share else "fleetlink is not on PATH",
            reason=why if share else "not found",
            category="fleet-tool",
            tool="fleetlink",
        ),
    )


def check_server_tools(
    report: Report, rt: dict, checkouts: dict, runner: Callable = subprocess.run
) -> None:
    """Server:  each wrapper execs current/<script>, fleet-secrets points at its Linux entry point,
    and fleetlink runs from a FleetLink checkout on main."""
    for name, script in SERVER_WRAPPERS.items():
        path = HOME / ".local/bin" / name
        try:
            text = path.read_text(errors="replace")[:65536]
        except OSError:
            text = None
        common = {"category": "fleet-tool", "tool": name}
        if text is None:
            why = f"no wrapper at {path}"
            add_tool_row(
                report, name, dict(common, status="FAIL", state="missing", detail=why, reason=why)
            )
        elif ".local/share/fleet-cli/current" in text and script in text:
            add_tool_row(report, name, inherit_row(name, rt, f"{path} runs current/{script}"))
        else:
            why = f"{path} does not run current/{script}"
            add_tool_row(
                report, name, dict(common, status="FAIL", state="missing", detail=why, reason=why)
            )
    link = HOME / ".local/bin/fleet-secrets"
    common = {"category": "fleet-tool", "tool": "fleet-secrets"}
    target = os.path.realpath(link) if link.exists() else ""
    if not target:
        why = f"{link} is not installed"
        add_tool_row(
            report,
            "fleet-secrets",
            dict(common, status="FAIL", state="missing", detail=why, reason=why),
        )
    elif os.path.basename(target) != "linux-entrypoint.py":
        why = f"{link} points at {target}, not linux-entrypoint.py (docs/protocols/fleet-cli-from-git.md)"
        add_tool_row(
            report,
            "fleet-secrets",
            dict(common, status="FAIL", state="stale", detail=why, reason=why),
        )
    else:
        extra = ((target, "scripts/fleet_secrets/linux-entrypoint.py"),)
        add_tool_row(
            report,
            "fleet-secrets",
            compare_copy(
                "fleet-secrets", COPY_SETS["fleet-secrets"], RUNTIME / "repo", rt, runner, extra
            ),
        )
    share = HOME / ".local/bin/fleetlink"
    common = {"category": "fleet-tool", "tool": "fleetlink"}
    fleetlink = checkouts.get("FleetLink", "unknown")
    if not share.exists():
        why = f"{share} is not installed"
        add_tool_row(
            report,
            "fleetlink",
            dict(common, status="FAIL", state="missing", detail=why, reason=why),
        )
    else:
        how = f"{share} runs the FleetLink checkout, which is {fleetlink} (see checkout:FleetLink)"
        status = {"current": "OK", "stale": "FAIL", "missing": "FAIL"}.get(fleetlink, "WARN")
        add_tool_row(
            report,
            "fleetlink",
            dict(common, status=status, state=fleetlink, detail=how, reason=how),
        )


# --- AFC-461:  the tool registry ----------------------------------------------------------------


def parse_version(text) -> dict | None:
    """The first version-looking token in the first lines of `text`:  {"text", "numbers", "pre"}."""
    if not text:
        return None
    for line in str(text).splitlines()[:5]:
        match = VERSION_RE.search(line)
        if match:
            pre = (match.group(2) or "").lower()
            return {
                "text": match.group(1) + (f"-{pre}" if pre else ""),
                "numbers": tuple(int(p) for p in match.group(1).split(".")),
                "pre": pre,
            }
    return None


def compare_versions(a: dict, b: dict) -> int:
    """-1, 0 or 1.  Numbers first;  then a pre-release (alpha, beta, rc, ...) sorts before its release.
    Other suffixes (build hashes, vendor tags) are ignored."""
    width = max(len(a["numbers"]), len(b["numbers"]))
    x = a["numbers"] + (0,) * (width - len(a["numbers"]))
    y = b["numbers"] + (0,) * (width - len(b["numbers"]))
    if x != y:
        return -1 if x < y else 1
    if a["pre"] and not b["pre"]:
        return -1
    if b["pre"] and not a["pre"]:
        return 1
    return 0


def tool_env() -> dict:
    env = dict(os.environ)
    env.update(
        PATH=os.pathsep.join(tool_path_dirs()),
        NO_COLOR="1",
        NO_UPDATE_NOTIFIER="1",
        npm_config_update_notifier="false",
        HOMEBREW_NO_AUTO_UPDATE="1",
        HOMEBREW_NO_ANALYTICS="1",
        HOMEBREW_NO_ENV_HINTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def probe_version(entry: dict, path: str, runner: Callable = subprocess.run) -> dict:
    """{"version": parsed or None, "raw": first line, "error": why the probe failed, or None}."""
    if entry.get("version_file"):
        try:
            raw = (HOME / entry["version_file"]).read_text(errors="replace")[:256]
        except OSError as exc:
            return {
                "version": None,
                "raw": None,
                "error": f"cannot read ~/{entry['version_file']} ({type(exc).__name__})",
            }
        return {"version": parse_version(raw), "raw": raw.strip()[:80], "error": None}
    try:
        out = runner(
            [path, *entry.get("version_args", ("--version",))],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env=tool_env(),
        )
    except subprocess.TimeoutExpired:
        return {
            "version": None,
            "raw": None,
            "error": f"--version did not answer within {PROBE_TIMEOUT} seconds",
            "timedOut": True,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "version": None,
            "raw": None,
            "error": f"--version could not run ({type(exc).__name__})",
        }
    text = ((out.stdout or "") + "\n" + (out.stderr or "")).strip()
    version = parse_version(text)
    raw = text.splitlines()[0][:80] if text else ""
    if version is None and out.returncode != 0:
        return {"version": None, "raw": raw, "error": f"--version exited {out.returncode}"}
    return {"version": version, "raw": raw, "error": None}


def latest_source(entry: dict, real: str | None) -> tuple[str | None, str | None]:
    """Where this install's latest version is read:  ("brew", formula), ("npm", package),
    ("github", owner/repo) or (None, None)."""
    match = CELLAR_RE.search(real or "")
    if match:
        return "brew", match.group(1)
    if entry.get("npm"):
        return "npm", entry["npm"]
    if entry.get("github"):
        return "github", entry["github"]
    return None, None


def http_get(url: str, timeout: float = GITHUB_TIMEOUT) -> bytes:
    """GET a GitHub API URL without credentials (the doctor never sends a token)."""
    if not url.startswith("https://api.github.com/"):
        raise ValueError("only api.github.com is read")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "fleet-tools-doctor"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(256 * 1024)


def npm_latest(package: str, runner: Callable = subprocess.run) -> dict:
    npm = shutil.which("npm", path=os.pathsep.join(tool_path_dirs()))
    if not npm:
        return {"version": None, "error": "npm is not installed"}
    try:
        out = runner(
            [npm, "view", package, "version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LATEST_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env=tool_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "version": None,
            "error": f"npm view failed ({type(exc).__name__})",
            "timedOut": isinstance(exc, subprocess.TimeoutExpired),
        }
    version = parse_version(out.stdout) if out.returncode == 0 else None
    return {"version": version, "error": None if version else f"npm view exited {out.returncode}"}


def brew_latest(formulae, runner: Callable = subprocess.run) -> dict:
    """{formula: {"version", "error"}} from one `brew info --json=v2` (Homebrew's own metadata)."""
    names = sorted(set(formulae))
    brew = shutil.which("brew", path=os.pathsep.join(tool_path_dirs()))
    if not names:
        return {}
    if not brew:
        return {n: {"version": None, "error": "brew is not installed"} for n in names}
    try:
        out = runner(
            [brew, "info", "--json=v2", "--formula", *names],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LATEST_TIMEOUT * 3,
            check=False,
            stdin=subprocess.DEVNULL,
            env=tool_env(),
        )
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {
            n: {"version": None, "error": f"brew info failed ({type(exc).__name__})"} for n in names
        }
    found = {}
    for formula in (data or {}).get("formulae") or []:
        if isinstance(formula, dict):
            stable = ((formula.get("versions") or {}).get("stable")) or ""
            found[formula.get("name")] = parse_version(stable)
    return {
        n: {"version": found.get(n), "error": None if found.get(n) else "brew info did not list it"}
        for n in names
    }


def github_latest(repo: str, fetch: Callable = http_get) -> dict:
    try:
        data = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
    except Exception as exc:  # noqa: BLE001 - any failure is "not read", never a crash
        return {"version": None, "error": f"GitHub releases not read ({type(exc).__name__})"}
    version = (
        parse_version(str((data or {}).get("tag_name") or "")) if isinstance(data, dict) else None
    )
    return {"version": version, "error": None if version else "GitHub release tag not recognised"}


def registry_row(
    entry: dict, host: str, path: str | None, probe: dict | None, latest: dict | None
) -> dict:
    """One registry tool's row fields.  `latest` is None when the tool has no latest source, else
    {"source", "version" (parsed or None), "error"}."""
    name = entry["name"]
    required = entry.get(host) == REQUIRED
    note = f"  ({entry['note']})" if entry.get("note") else ""
    row = {
        "category": entry["kind"],
        "tool": name,
        "required": required,
        "path": path,
        "version": None,
        "latest": None,
        "latestSource": (latest or {}).get("source"),
        "basis": None,
        "installed": None,
        "expectedRev": None,
        "behindCommits": None,
    }
    if not path:
        if required:
            why = f"not installed;  required on the {host}"
            return dict(row, status="FAIL", state="missing", detail=why + note, reason=why)
        why = "not installed (optional here)"
        return dict(row, status="OK", state="absent", detail=why + note, reason=why)
    probe = probe or {}
    if probe.get("error"):
        why = f"version probe failed:  {probe['error']}"
        return dict(row, status="WARN", state="unknown", detail=f"{path};  {why}{note}", reason=why)
    version = probe.get("version")
    if version is None:
        why = f"version output not recognised ({probe.get('raw') or 'empty'!r})"
        return dict(row, status="WARN", state="unknown", detail=f"{path};  {why}{note}", reason=why)
    row.update(version=version["text"], installed=version["text"])
    where = f"{version['text']} at {path}"
    if latest is None:
        why = "no latest source, so presence and host skew only"
        return dict(
            row,
            status="OK",
            state="current",
            basis="presence",
            detail=f"{where};  {why}{note}",
            reason=why,
        )
    source = latest["source"]
    if latest.get("version") is None:
        why = f"latest from {source} not read:  {latest.get('error')}"
        return dict(
            row, status="WARN", state="unknown", detail=f"{where};  {why}{note}", reason=why
        )
    row["latest"] = latest["version"]["text"]
    order = compare_versions(version, latest["version"])
    if order >= 0:
        why = f"{'at' if order == 0 else 'ahead of'} {source}'s latest {row['latest']}"
        return dict(
            row,
            status="OK",
            state="current",
            basis="latest",
            detail=f"{where};  {why}{note}",
            reason=why,
        )
    gap = f"behind {source}'s latest {row['latest']}"
    if VENDOR_CURRENCY_POLICY == "skew":
        return dict(
            row,
            status="OK",
            state="current",
            basis="skew",
            detail=f"{where};  {gap} (policy:  skew only){note}",
            reason=gap,
        )
    return dict(
        row,
        status="WARN",
        state="stale",
        basis="latest",
        detail=f"{where};  {gap}{note}",
        reason=gap,
    )


def safe_result(job, fallback):
    """One item's answer;  an unexpected error in it becomes `fallback`, never the whole batch's."""
    try:
        return job.result()
    except Exception as exc:  # noqa: BLE001 - isolate each item
        if isinstance(fallback, dict) and "error" in fallback:
            return dict(fallback, error=f"{fallback['error']} ({type(exc).__name__})")
        return fallback


def check_registry(
    report: Report,
    host: str,
    network: bool,
    runner: Callable = subprocess.run,
    fetch: Callable = http_get,
) -> None:
    """Every TOOL_REGISTRY row for this host:  installed, its version, and the latest one."""
    entries = [entry for entry in TOOL_REGISTRY if entry.get(host)]
    search = os.pathsep.join(tool_path_dirs())
    paths = {e["name"]: shutil.which(e["name"], path=search) for e in entries}
    reals = {n: (os.path.realpath(p) if p else None) for n, p in paths.items()}
    sources = {}
    for entry in entries:
        if paths[entry["name"]]:
            kind, ident = latest_source(entry, reals[entry["name"]])
            if kind:
                sources[entry["name"]] = (kind, ident)
    latest: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=REGISTRY_WORKERS) as pool:
        probes = {
            e["name"]: pool.submit(probe_version, e, paths[e["name"]], runner)
            for e in entries
            if paths[e["name"]]
        }
        lookups = {}
        if network:
            brew = [ident for kind, ident in sources.values() if kind == "brew"]
            brew_job = pool.submit(brew_latest, brew, runner) if brew else None
            for name, (kind, ident) in sources.items():
                if kind == "npm":
                    lookups[name] = pool.submit(npm_latest, ident, runner)
                elif kind == "github":
                    lookups[name] = pool.submit(github_latest, ident, fetch)
            brew_found = safe_result(brew_job, {}) if brew_job else {}
            for name, (kind, ident) in sources.items():
                if kind == "brew":
                    latest[name] = dict(
                        brew_found.get(ident) or {"version": None, "error": "brew info failed"},
                        source="brew",
                    )
        for name, job in lookups.items():
            failed = {"version": None, "error": "latest lookup failed"}
            latest[name] = dict(safe_result(job, failed), source=sources[name][0])
        for name, (kind, _) in sources.items():
            latest.setdefault(
                name, {"source": kind, "version": None, "error": "not read (--no-network)"}
            )
        failed_probe = {"version": None, "raw": None, "error": "version probe failed"}
        results = {name: safe_result(job, failed_probe) for name, job in probes.items()}
    # One more try, alone, for whatever timed out while it shared the machine with the others.
    by_name = {e["name"]: e for e in entries}
    for name, result in results.items():
        if result.get("timedOut"):
            results[name] = probe_version(by_name[name], paths[name], runner)
    for name, found in latest.items():
        if found.get("timedOut") and sources.get(name, (None,))[0] == "npm":
            latest[name] = dict(npm_latest(sources[name][1], runner), source="npm")
    for entry in entries:
        name = entry["name"]
        fields = registry_row(entry, host, paths[name], results.get(name), latest.get(name))
        add_tool_row(report, name, fields)


def apply_skew(docs: list) -> None:
    """Mac/server skew:  a registry tool older than the same tool on the other host is stale.  Only
    when both hosts were reached, and never for an entry with "skew": False."""
    by_host = {d.get("host"): d for d in docs if isinstance(d, dict) and d.get("reachable")}
    if not ("mac" in by_host and "server" in by_host):
        return
    skewable = {e["name"] for e in TOOL_REGISTRY if e.get("skew", True)}

    def rows(doc):
        return {
            r.get("tool"): r
            for r in doc.get("checks") or []
            if isinstance(r, dict)
            and r.get("category") in ("platform-cli", "dev-tool")
            and r.get("tool") in skewable
            and r.get("version")
        }

    mac, server = rows(by_host["mac"]), rows(by_host["server"])
    for name in sorted(set(mac) & set(server)):
        a, b = parse_version(mac[name]["version"]), parse_version(server[name]["version"])
        if not (a and b):
            continue
        order = compare_versions(a, b)
        if order == 0:
            continue
        older, other, newer = (
            (mac[name], "server", server[name]) if order < 0 else (server[name], "mac", mac[name])
        )
        older["skew"] = {"host": other, "version": newer["version"]}
        if older.get("state") == "current":
            why = f"older than the {other} ({older['version']} < {newer['version']})"
            older.update(state="stale", status="WARN", basis="skew", reason=why)
            older["detail"] = f"{older.get('detail')};  {why}"


# --- AFC-486:  the Apple Notes service (the Operator's queue, the Mac runner, the action log) --------

NOTES_RUNNER_LABEL = "com.jay.operator-notes-runner"
NOTES_RUNNER_STATE = HOME / ".local/state/operator/notes-runner.json"
NOTES_ACTION_LOG = HOME / "Library/Logs/fleet/apple-notes-actions.jsonl"
NOTES_POLL_STALE_S = (
    180  # the runner long-polls every 25 s, so three minutes of silence means it is not polling
)
NOTES_QUEUE_STALE_S = 600  # a request waiting ten minutes means the runner is not taking work
NOTES_LOG_STALE_S = 1800  # unsent log lines older than this mean the runner stopped sending them
NOTES_LOG_ROTATIONS = (
    5  # apple-notes-coding.sh keeps .1 to .5 (agent_sync.notes.ACTION_LOG_ROTATIONS)
)


def _whole_lines(path: Path, size: int) -> int:
    """The bytes of `path` up to the end of its last whole line:  a torn tail (a write that failed part way) is never
    sent, so it is never counted as waiting to be."""
    try:
        with open(path, "rb") as fh:
            end = size
            while end > 0:  # back a window at a time, so a torn tail of any length is left out
                start = max(0, end - 64 * 1024)
                fh.seek(start)
                newline = fh.read(end - start).rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                end = start
            return 0
    except OSError:
        return size


def _notes_unsent(cursor: dict) -> tuple[int, float | None]:
    """Bytes of the action log the runner has not read yet, and when the file that holds the first of them was last
    written:  the rest of the file its cursor names (by inode:  the current log or a rotated copy), then every newer
    file whole;  every kept file when it names none of them, or when that file is now shorter than the cursor (cut
    short), since the runner then sends them all again.  Only whole lines count."""
    files: list[tuple[int, int, float]] = []  # (inode, bytes of whole lines, mtime), oldest first
    for n in range(NOTES_LOG_ROTATIONS, -1, -1):
        path = (
            NOTES_ACTION_LOG
            if n == 0
            else NOTES_ACTION_LOG.with_name(f"{NOTES_ACTION_LOG.name}.{n}")
        )
        try:
            info = path.stat()
        except OSError:
            continue
        files.append((info.st_ino, _whole_lines(path, info.st_size), info.st_mtime))
    offset = cursor.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        offset = 0
    first = 0
    for index, (inode, size, _) in enumerate(files):
        if inode == cursor.get("inode") and offset <= size:
            first = index
            break
    else:
        offset = 0
    waiting = [
        (size - (offset if index == first else 0), mtime)
        for index, (_, size, mtime) in enumerate(files)
        if index >= first
    ]
    unsent = sum(count for count, _ in waiting)
    written = next((mtime for count, mtime in waiting if count > 0), None)
    return unsent, written


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def _age(text, now: float) -> float | None:
    moment = parse_utc(text)
    return None if moment is None else now - moment


def notes_health(env: dict[str, str]) -> dict:
    """The Operator queue's /notes/v1/health, signed as the SERVER principal.  Raises the client's NotesError."""
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from agent_sync import (
        notes_client as NC,
    )  # the shipped runtime carries it;  imported only on the server

    return NC.operator_client(env).health()


def check_notes(
    report: Report,
    host: str,
    env: dict[str, str],
    runner: Callable = subprocess.run,
    *,
    network: bool = True,
    health: Callable[[dict[str, str]], dict] = notes_health,
    clock: Callable[[], float] = time.time,
) -> None:
    """The Notes service rows Muster shows (category `service`):  on the Mac the runner's LaunchAgent and its last poll,
    and the action log;  on the server the Operator queue's health, read with the SERVER key."""
    now = clock()
    base = {"category": "service"}
    if host == "mac":
        job = launchd_job(NOTES_RUNNER_LABEL, runner)
        state = read_json_file(NOTES_RUNNER_STATE) or {}
        polled = _age(state.get("last_poll_at"), now)
        error = str(state.get("last_error") or "")[:160]
        fields = dict(base, tool="notes-runner")
        if job is None:
            report.add(
                "notes:runner",
                "WARN",
                "launchctl could not be read",
                state="unknown",
                reason="launchctl",
                **fields,
            )
        elif not job.get("loaded"):
            report.add(
                "notes:runner",
                "FAIL",
                f"{NOTES_RUNNER_LABEL} is not loaded, so cloud and server seats cannot write Apple Notes",
                state="missing",
                reason="not loaded",
                **fields,
            )
        elif polled is None or polled > NOTES_POLL_STALE_S:
            detail = f"loaded ({job.get('state') or 'unknown state'}) but last polled the Operator {_ago(polled)}"
            report.add(
                "notes:runner",
                "WARN",
                detail + (f";  last error:  {error}" if error else ""),
                state="stale",
                reason="not polling",
                **fields,
            )
        else:
            handled = int(state.get("handled") or 0)
            report.add(
                "notes:runner",
                "OK",
                f"polling (last {_ago(polled)}), {handled} requests run",
                state="current",
                **fields,
            )
        fields = dict(base, tool="notes-log")
        try:
            size = NOTES_ACTION_LOG.stat().st_size
        except OSError:
            report.add(
                "notes:action-log",
                "WARN",
                "no action log yet (apple-notes-coding.sh has not run since it gained one)",
                state="unknown",
                reason="absent",
                **fields,
            )
            return
        cursor = state.get("log") if isinstance(state.get("log"), dict) else {}
        unsent, written = _notes_unsent(cursor)
        shipped = _age(state.get("log_shipped_at"), now)
        # The runner sends new lines every pass (25 s at most), so lines written in the last few minutes are not late.
        waited = None if written is None else now - written
        # Only an error from shipping the log ("log:  ..."), and only one newer than the last send, says why it stalled.
        erred = _age(state.get("last_error_at"), now)
        log_error = (
            error
            if error.startswith("log:")
            and erred is not None
            and (shipped is None or erred < shipped)
            else ""
        )
        if (
            unsent
            and (shipped is None or shipped > NOTES_LOG_STALE_S)
            and (waited is None or waited > NOTES_POLL_STALE_S)
            and job
            and job.get("loaded")
        ):
            report.add(
                "notes:action-log",
                "WARN",
                f"{unsent} bytes not yet sent to the Operator (last sent {_ago(shipped)})"
                + (f";  last error ({_ago(erred)}):  {log_error}" if log_error else ""),
                state="stale",
                reason="unsent",
                **fields,
            )
        else:
            report.add(
                "notes:action-log",
                "OK",
                f"{size} bytes;  sent to the Operator {_ago(shipped)}",
                state="current",
                **fields,
            )
        return
    if host != "server":
        return
    fields = dict(base, tool="notes-queue")
    if not network:
        report.add(
            "notes:queue",
            "WARN",
            "not checked (--no-network)",
            state="unknown",
            reason="no network",
            **fields,
        )
        return
    try:
        data = health(env)
    except Exception as exc:  # noqa: BLE001 - a failed probe is a row, never a crash
        code = getattr(exc, "code", type(exc).__name__)
        message = str(getattr(exc, "message", "") or "")[:200]
        status, state_name = (
            ("WARN", "unknown") if code == "not_configured" else ("FAIL", "missing")
        )
        report.add(
            "notes:queue",
            status,
            f"the Operator notes queue:  {code}{(':  ' + message) if message else ''}",
            state=state_name,
            reason=str(code),
            **fields,
        )
        return
    polled = _age(data.get("runner_polled_at"), now)
    oldest = data.get("oldest_queued_seconds")
    counts = f"{data.get('queued', 0)} queued, {data.get('running', 0)} running"
    if polled is None or polled > NOTES_POLL_STALE_S:
        report.add(
            "notes:queue",
            "WARN",
            f"{counts};  the Mac runner last polled {_ago(polled)}",
            state="stale",
            reason="runner silent",
            **fields,
        )
    elif isinstance(oldest, int) and oldest > NOTES_QUEUE_STALE_S:
        report.add(
            "notes:queue",
            "WARN",
            f"{counts};  the oldest has waited {_ago(oldest).replace(' ago', '')}",
            state="stale",
            reason="backlog",
            **fields,
        )
    else:
        report.add(
            "notes:queue",
            "OK",
            f"{counts};  the Mac runner polled {_ago(polled)}",
            state="current",
            **fields,
        )


# --- AFC-461:  the document, the server route and the report file -------------------------------


def finalize_rows(report: Report, host: str) -> None:
    """Give every row the AFC-461 fields, so a reader never has to guess.  A row that set no state
    takes it from its status;  seat and MCP rows are the harness, which Muster leaves out."""
    for row in report.rows:
        check = row["check"]
        if "state" not in row:
            row["state"] = STATE_FOR_STATUS.get(row["status"], "unknown")
        if "category" not in row:
            if check.startswith(("seat:", "mcp:", "mcp-start:")):
                row["category"] = "harness"
            else:
                row["category"] = "fleet-tool"
        for field in ROW_FIELDS:
            row.setdefault(field, None)


def doctor_commit() -> str | None:
    return git_try(Path(__file__).resolve().parent, "rev-parse", "HEAD")


def host_document(
    report: Report, host: str, expected: dict, network: bool, updater: dict | None
) -> dict:
    """The --json document:  `ok` and `checks` first (the original contract), then the AFC-461 fields."""
    doc = {
        "ok": not report.failed,
        "checks": report.rows,
        "schemaVersion": SCHEMA_VERSION,
        "host": host,
        "hostname": os.uname().nodename,
        "generatedAt": utc_now(),
        "doctorCommit": doctor_commit(),
        "network": network,
        "expected": expected,
        "policy": {"vendorCurrency": VENDOR_CURRENCY_POLICY},
    }
    if updater is not None:
        doc["updater"] = updater
    return doc


def expected_server_checks() -> list[str]:
    checks = ["runtime"] + [f"updater:{unit}" for unit in SERVER_TIMERS]
    checks += [f"tool:{name}" for name in (*SERVER_WRAPPERS, "fleet-secrets", "fleetlink")]
    return checks


def unreachable_server(error: str) -> dict:
    """The server's half when it could not be read:  every row it would have is `unknown`, with why."""
    rows = [
        {
            "check": check,
            "status": "WARN",
            "detail": error,
            "state": "unknown",
            "category": "fleet-tool",
            "tool": check.split(":", 1)[1] if check.startswith("tool:") else None,
            "installed": None,
            "expectedRev": None,
            "behindCommits": None,
            "reason": error,
        }
        for check in expected_server_checks()
    ]
    for entry in TOOL_REGISTRY:
        if entry.get("server"):
            rows.append(
                {
                    "check": f"tool:{entry['name']}",
                    "status": "WARN",
                    "detail": error,
                    "state": "unknown",
                    "category": entry["kind"],
                    "tool": entry["name"],
                    "required": entry["server"] == REQUIRED,
                    "path": None,
                    "version": None,
                    "latest": None,
                    "latestSource": None,
                    "basis": None,
                    "installed": None,
                    "expectedRev": None,
                    "behindCommits": None,
                    "reason": error,
                }
            )
    return {
        "host": "server",
        "reachable": False,
        "error": error,
        "schemaVersion": SCHEMA_VERSION,
        "ok": None,
        "checks": rows,
    }


# What an ssh failure may say in the report:  a fixed phrase per known class, never ssh's own text
# (which can carry host names and addresses).
SSH_ERRORS = (
    ("permission denied", "the server refused the key"),
    ("host key verification failed", "the server's host key is not trusted"),
    ("timed out", "the connection timed out"),
    ("connection refused", "the connection was refused"),
    ("could not resolve hostname", "the host name did not resolve"),
    ("no route to host", "there is no route to the host"),
    ("connection closed", "the server closed the connection"),
)
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*")
MAX_REMOTE_DEPTH = 32
MAX_REMOTE_NODES = 100000
MAX_REMOTE_ROWS = 5000
TOO_DEEP = "the server's answer is nested too deeply or has too many nodes"


def json_within_limits(
    value, max_depth: int = MAX_REMOTE_DEPTH, max_nodes: int = MAX_REMOTE_NODES
) -> bool:
    """True when a parsed JSON value is at most `max_depth` deep and has at most `max_nodes` nodes."""
    stack, nodes = [(value, 1)], 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            return False
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return True


def remote_rows_valid(rows: list) -> bool:
    """Every row a reader will touch has the types it expects."""
    if len(rows) > MAX_REMOTE_ROWS:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not all(isinstance(row.get(key), str) for key in ("check", "status", "detail")):
            return False
        for key in ("state", "category", "tool", "reason", "version"):
            if row.get(key) is not None and not isinstance(row.get(key), str):
                return False
    return True


def fetch_remote(alias: str, runner: Callable = subprocess.run) -> dict:
    """The server's doctor document over the read-only key:  the forced command runs the doctor with
    --json whatever is asked, so nothing is sent.  Any failure is `reachable: false` with the reason."""
    if not ALIAS_RE.fullmatch(alias or ""):
        return unreachable_server(f"{NO_ROUTE} (the ssh alias is not a plain host alias)")
    try:
        config = runner(
            ["ssh", "-G", alias],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return unreachable_server(f"{NO_ROUTE} (ssh could not run:  {type(exc).__name__})")
    hostname = ""
    for line in (config.stdout or "").splitlines():
        if line.lower().startswith("hostname "):
            hostname = line.split(None, 1)[1].strip()
            break
    if config.returncode != 0 or not hostname or hostname.lower() == alias.lower():
        return unreachable_server(f"{NO_ROUTE} (the ssh alias {alias} is not configured)")
    try:
        out = runner(
            ["ssh", *SSH_OPTIONS, alias],
            capture_output=True,
            timeout=REMOTE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return unreachable_server(f"ssh {alias} did not answer within {REMOTE_TIMEOUT} seconds")
    except (OSError, subprocess.SubprocessError) as exc:
        return unreachable_server(f"ssh {alias} could not run ({type(exc).__name__})")
    stdout = out.stdout or b""
    if isinstance(stdout, str):
        stdout = stdout.encode()
    # The doctor exits 1 when the server has a FAIL row;  that is still an answer.  255 is ssh's own.
    if out.returncode not in (0, 1):
        err = out.stderr or b""
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        err = err.lower()
        phrase = next((says for marker, says in SSH_ERRORS if marker in err), None)
        return unreachable_server(
            f"ssh {alias} exited {out.returncode}" + (f" ({phrase})" if phrase else "")
        )
    if len(stdout) > MAX_REMOTE_BYTES:
        return unreachable_server(f"the server's answer is larger than {MAX_REMOTE_BYTES} bytes")
    try:
        doc = json.loads(stdout.decode("utf-8", "replace"))
    except RecursionError:
        return unreachable_server(TOO_DEEP)
    except ValueError:
        return unreachable_server("the server answered with something that is not JSON")
    if not json_within_limits(doc):
        return unreachable_server(TOO_DEEP)
    if not isinstance(doc, dict) or not isinstance(doc.get("checks"), list):
        return unreachable_server("the server answered without a doctor document")
    if "schemaVersion" not in doc:
        return unreachable_server(
            "the server's doctor predates schemaVersion 1 (an old output shape)"
        )
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        return unreachable_server(
            f"the server's doctor reports schemaVersion {doc.get('schemaVersion')!r}, not 1"
        )
    if doc.get("host") != "server":
        return unreachable_server(f"{alias} answered as host {doc.get('host')!r}, not the server")
    if not remote_rows_valid(doc["checks"]):
        return unreachable_server("the server's doctor answered with a malformed check row")
    doc["reachable"] = True
    doc["error"] = None
    return doc


def combined_report(docs: list) -> dict:
    apply_skew(docs)
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "tool": "fleet-tools-doctor",
        "generatedAt": utc_now(),
        "hosts": docs,
    }


def write_report(path: Path, report_doc: dict) -> None:
    """Atomic and private:  a 0600 temporary file in the same folder, fsynced, then renamed over.
    The folder must be a real directory (a symlink is refused), created 0700 when absent."""
    if path.parent.is_symlink():
        raise ValueError(f"refusing to write the report through a symlinked folder:  {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".report.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(report_doc, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # The rename has already replaced the report atomically;  syncing the folder only makes that
    # rename survive a power cut.  Some file systems refuse a directory fsync, and a lost rename only
    # leaves the previous report, which a reader ages out to unknown, so this step is best effort.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def state_counts(doc: dict) -> str:
    counts: dict = {}
    for row in doc.get("checks") or []:
        if isinstance(row, dict) and row.get("category") not in VERDICT_EXCLUDED:
            counts[row.get("state")] = counts.get(row.get("state"), 0) + 1
    order = ("current", "stale", "missing", "unknown", "absent")
    return ", ".join(f"{counts[s]} {s}" for s in order if counts.get(s)) or "no rows"


def stable_failing(line: str) -> str:
    """Dedup key for a failing line:  elapsed-minute counters tick every run."""
    return re.sub(r"\b\d+ minutes ago\b", "<N> minutes ago", line)


def post_changes(report: Report, runner: Callable = subprocess.run) -> bool:
    """Post once per change in the failing set, so a standing problem is not repeated every run."""
    failing = sorted(f"{r['check']}: {r['detail']}" for r in report.rows if r["status"] == "FAIL")
    try:
        previous = json.loads(STATE.read_text()).get("failing", [])
    except (OSError, ValueError):
        previous = []
    if [stable_failing(f) for f in failing] == [stable_failing(p) for p in previous]:
        return False
    host = os.uname().nodename
    if failing:
        body = (
            f"fleet-tools-doctor on {host}:  {len(failing)} failing check{'s' if len(failing) != 1 else ''}.\n"
            + "\n".join(f"- {line}" for line in failing)
        )
    else:
        body = f"fleet-tools-doctor on {host}:  every check passes again."
    try:
        result = runner(
            [
                "simple-chat",
                "post",
                "--channel",
                "Work-In-Progress",
                "--topic",
                "fleet tools doctor",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ):  # simple-chat missing from PATH (check_commands reports it) or hung
        return False
    if result.returncode == 0:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"failing": failing}))
        return True
    return False


def run(
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
    runner: Callable = subprocess.run,
    fetch: Callable = http_get,
) -> tuple[Report, int]:
    parser = argparse.ArgumentParser(
        prog="fleet-tools-doctor", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--seats", default="", help="comma-separated seats whose credentials this host must hold"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="read nothing from GitHub, npm or brew:  fleet tool rows turn unknown, never current",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="when the set of failing checks changes, post it with simple-chat to #Work-In-Progress, topic 'fleet tools doctor' (the seat comes from AGENT_SEAT in the environment, as simple-chat resolves it)",
    )
    parser.add_argument(
        "--no-registry",
        action="store_true",
        help="skip the tool registry (no `<tool> --version` probes), for a caller with a short time budget",
    )
    parser.add_argument(
        "--remote",
        metavar="ALIAS",
        help="also read the server's doctor over this read-only ssh alias (fleet-tools-ro;  docs/protocols/fleet-tool-parity.md)",
    )
    parser.add_argument(
        "--write",
        metavar="PATH",
        help="write the combined report (this host plus the server) to PATH, 0600 and atomically;  '-' prints it instead",
    )
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    report = Report()
    network = not args.no_network
    host = host_kind()
    report_path = Path(args.write).expanduser() if args.write and args.write != "-" else None
    previous = read_json_file(report_path or (HOME / REPORT_RELATIVE))
    expected = read_tip(network, runner)
    updater = None
    on_mac_runtime = (
        host == "mac" and (MAC_RUNTIME / ".git").exists() and not (RUNTIME / "repo/.git").exists()
    )
    if on_mac_runtime:
        updater = check_mac_updater(report, runner, previous)
    if on_mac_runtime:
        # simple-chat, board and every Mac copy come from the managed runtime, so the Mac's rows are
        # always judged against it, even if an old release receipt is still on disk.
        rt = check_mac_runtime(report, runner, expected, updater, previous)
    else:
        rt = check_runtime(report, network, runner, expected, updater, previous)
    checkouts = check_checkouts(report, network, runner, host)
    check_commands(report)
    check_catalogs(report, host)
    check_tool_files(report)
    check_fleet_mode_doc(report, rt)
    if on_mac_runtime:
        check_mac_tools(report, rt, runner)
    elif host == "server" and (RUNTIME / "repo/.git").exists():
        check_server_timers(report, runner)
        check_server_tools(report, rt, checkouts, runner)
    if not args.no_registry:
        check_registry(report, host, network, runner, fetch)
    for seat in [s.strip().upper() for s in args.seats.split(",") if s.strip()]:
        check_seat(report, seat, env)
        check_mcp_startup(report, seat)
    check_mcp(report)
    check_notes(report, host, env, runner, network=network)
    finalize_rows(report, host)
    doc = host_document(report, host, expected, network, updater)
    if args.post:
        post_changes(report, runner)
    server = None
    if args.remote:
        server = fetch_remote(args.remote, runner)
    elif host == "mac" and args.write:
        server = unreachable_server(f"{NO_ROUTE} (no --remote given)")
    combined = None
    if args.write:
        docs = json.loads(
            json.dumps([dict(doc, reachable=True, error=None)] + ([server] if server else []))
        )
        combined = combined_report(docs)
        if report_path:
            write_report(report_path, combined)
    if args.write == "-":
        print(json.dumps(combined, indent=2))
    elif args.json:
        print(json.dumps(doc, indent=2))
    elif report_path:
        far = ""
        if server is not None:
            far = ";  server " + (
                state_counts(server)
                if server.get("reachable")
                else f"unreachable:  {server.get('error')}"
            )
        print(f"fleet-tools-doctor:  wrote {report_path}:  {host} {state_counts(doc)}{far}")
    else:
        for row in report.rows:
            print(f"{row['status']:<4}  {row['check']:<28} {row['detail']}")
        if server is not None:
            if server.get("reachable"):
                print(f"-- server ({server.get('hostname')}):")
                for row in server.get("checks") or []:
                    print(
                        f"{row.get('status', ''):<4}  {row.get('check', ''):<28} {row.get('detail', '')}"
                    )
            else:
                print(f"-- server unreachable:  {server.get('error')}")
    return report, 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(run()[1])
