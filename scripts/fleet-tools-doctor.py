#!/usr/bin/env python3
"""fleet-tools-doctor:  is this host's fleet tooling current and complete?

One OK / WARN / FAIL line per check, for the Mac and the Hetzner server alike
(docs/protocols/fleet-tool-parity.md says what each check guards).  Names,
commits and booleans only:  never a credential value.

  python3 scripts/fleet-tools-doctor.py [--seats CLAUDE,CODEX] [--json] [--no-network]

Checks:
  runtime     the installed fleet-cli release (agent-sync, board, recall, lane,
              fleet-mode) was built from AI-Fleet-Coordinator origin/main
  checkout    each /root/Code (or ~/Code) checkout that ships a CLI is on
              origin/main:  AI-Fleet-Coordinator, FleetLink
  commands    the five fleet tools (agent-sync, board, recall, lane, fleet-mode)
              and fleet-share are on PATH (the Mac also keeps lane and fleet-mode
              in ~/apps, which counts)
  catalogs    the shared contracts ship with the runtime:  tools.json and,
              once AFC-329 lands, the board Linear catalog
  tool-file   the files the recall, lane and fleet-mode tools run from ship with the runtime
  fleet-mode-doc  the Mac's ~/apps/FLEET-MODE.md equals the repo's docs/FLEET-MODE.md (WARN if not)
  seat:<S>    per seat:  Zulip bot, Linear app and FleetLink token key NAMES in Infisical Fleet-OPS
              (/zulip, /linear, /fleetlink;  AFC-359:  no credential file is read)
  mcp:codex   Codex registers agent-sync (and FleetLink when a token exists)
  mcp:claude  Claude Code registers agent-sync (and FleetLink likewise)

Exit 1 on any FAIL, 0 otherwise.  Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
RUNTIME = HOME / ".local/share/fleet-cli"
CODE_ROOTS = [Path("/root/Code"), HOME / "Code"]
REPOS = ("AI-Fleet-Coordinator", "FleetLink")
# The five tools both hosts carry (docs/protocols/fleet-tool-parity.md), then FleetLink's CLI.
FLEET_TOOLS = ("agent-sync", "board", "recall", "lane", "fleet-mode")
COMMANDS = (*FLEET_TOOLS, "fleet-share")
# The Mac keeps lane and fleet-mode in ~/apps rather than on PATH.
COMMAND_FALLBACK_DIRS = (HOME / "apps",)
# Where a Mac finds the published runtime (the server reads RUNTIME/current).
MAC_RUNTIME = HOME / "apps/lanes/_managed/fleet/agent-sync-runtime"
MAC_FLEET_MODE_DOC = HOME / "apps/FLEET-MODE.md"
AFC_REMOTE_URL = "https://github.com/Simple-With-Us/AI-Fleet-Coordinator"
# Coarse git pathspecs that bound the diff;  is_shipped() then applies the exact rules.
SHIPPED = (
    "scripts/agent-sync",
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
    "docs/protocols/agent-sync-tools-guide.md",
)
# What a fleet-cli release ships:  mirrors EXACT and allowed_path in scripts/fleet-cli-release.py, so a
# commit that changes only tests or docs under those folders needs no push and is not drift.
# The fallback copy of what a release ships, for a host with no checkout.  With a checkout,
# is_shipped asks scripts/fleet-cli-release.py itself, so the two cannot drift;  a test pins this
# copy to the release's EXACT.
SHIPPED_EXACT = frozenset(
    {
        "scripts/agent-sync",
        "scripts/linear-board/board",
        "scripts/linear-board/board-ids.txt",
        "fleet-apps.json",
        "docs/protocols/agent-sync-partition.toml",
        "scripts/fleet-cli-release.py",
        "scripts/agent_sync/mcp/tools.json",
        "scripts/agent_sync/mcp/fixtures.jsonl",
        "scripts/agent_sync/mcp/listener-tools.json",
        "scripts/agent_sync/mcp/renamed-tools.json",
        "scripts/agent_sync/mcp/gb-identities.json",
        "scripts/agent_sync/mcp/notes-tools.json",
        "scripts/agent_sync/mcp/recall-tools.json",
        "scripts/linear-board/catalog/core-tools.json",
        "scripts/agent_sync/mcp/fleetlink-tools.json",
        "docs/protocols/agent-sync-tools-guide.md",
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
        self.rows: list[dict[str, str]] = []

    def add(self, check: str, status: str, detail: str) -> None:
        self.rows.append({"check": check, "status": status, "detail": detail})

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


def remote_main(repo: Path, network: bool, runner: Callable = subprocess.run) -> str:
    if network:
        line = git(repo, "ls-remote", "origin", "refs/heads/main", runner=runner)
        if line:
            return line.split()[0]
    return git(repo, "rev-parse", "origin/main", runner=runner)


def find_repo(name: str) -> Path | None:
    for root in CODE_ROOTS:
        if (root / name / ".git").exists():
            return root / name
    return None


def check_git_runtime(
    report: Report, checkout: Path, network: bool, runner: Callable = subprocess.run
) -> None:
    """The server runs from a git checkout (fleet-cli-git-update.py, 2026-10-19):  is it at origin/main?"""
    commit = git(checkout, "rev-parse", "HEAD", runner=runner)
    main = remote_main(checkout, network, runner)
    try:
        state = json.loads((RUNTIME / "git-state.json").read_text())
    except (OSError, ValueError):
        state = {}
    if not main:
        report.add("runtime", "WARN", f"running from git at {commit[:7]};  origin/main unknown")
    elif commit == main:
        report.add("runtime", "OK", f"running from git at origin/main {main[:7]}")
    elif state.get("bad") == main:
        report.add(
            "runtime",
            "WARN",
            f"origin/main {main[:7]} failed the smoke test ({state.get('bad_reason')});  staying on {commit[:7]} until main moves",
        )
    else:
        report.add(
            "runtime",
            "FAIL",
            f"running from git at {commit[:7]}, origin/main {main[:7]}:  run systemctl start fleet-cli-git-update",
        )


def check_runtime(report: Report, network: bool, runner: Callable = subprocess.run) -> None:
    checkout = RUNTIME / "repo"
    if (checkout / ".git").exists() and (RUNTIME / "current").resolve() == checkout.resolve():
        check_git_runtime(report, checkout, network, runner)
        return
    # The live release is what `current` points at;  status.json is only the last receive's
    # receipt and goes stale after a rollback.
    live = RUNTIME / "current" / "release.json"
    status_file = live if live.exists() else RUNTIME / "status.json"
    afc = find_repo("AI-Fleet-Coordinator")
    if not status_file.exists():
        report.add(
            "runtime", "WARN", f"no {status_file} (a Mac reads the published runtime instead)"
        )
        return
    status = json.loads(status_file.read_text())
    status.setdefault("status", "paused" if (RUNTIME / "paused").exists() else "ready")
    commit = str(status.get("source_commit", ""))
    if (RUNTIME / "paused").exists():
        report.add(
            "runtime:updates",
            "WARN",
            "paused (fleet-cli-release resume turns the Mac's pushes back on)",
        )
    # A host with no checkout (the Hetzner server may have none) asks GitHub directly.
    main = (
        remote_main(afc, network, runner)
        if afc
        else (remote_ls_remote(AFC_REMOTE_URL, runner) if network else "")
    )
    if not main:
        report.add(
            "runtime", "WARN", f"release {commit[:7]} ({status.get('status')}); origin/main unknown"
        )
    elif commit == main:
        report.add("runtime", "OK", f"release built from origin/main {main[:7]}")
    else:
        raw = (
            git_try(afc, "diff", "--name-only", commit, main, "--", *SHIPPED, runner=runner)
            if afc
            else None
        )
        if raw is None:
            # No checkout, or a commit not fetched here:  the doctor cannot tell, so it says so.
            report.add(
                "runtime",
                "WARN",
                f"release built from {commit[:7]}, origin/main {main[:7]}:  cannot compare (no checkout, or a commit not fetched here)",
            )
            return
        changed = [line for line in raw.splitlines() if is_shipped(line)]
        if not changed:
            report.add(
                "runtime",
                "OK",
                f"release built from {commit[:7]};  no shipped file changed up to origin/main {main[:7]}",
            )
        else:
            report.add(
                "runtime",
                "FAIL",
                f"release built from {commit[:7]};  {len(changed)} shipped files changed up to origin/main {main[:7]}:  the Mac's fleet-cli-sync has not pushed them",
            )


def check_checkouts(report: Report, network: bool, runner: Callable = subprocess.run) -> None:
    for name in REPOS:
        repo = find_repo(name)
        if not repo:
            report.add(f"checkout:{name}", "WARN", "no checkout under /root/Code or ~/Code")
            continue
        head = git(repo, "rev-parse", "HEAD", runner=runner)
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", runner=runner)
        main = remote_main(repo, network, runner)
        if branch != "main":
            report.add(f"checkout:{name}", "WARN", f"on {branch}, not main")
        elif main and head != main:
            report.add(
                f"checkout:{name}",
                "FAIL",
                f"main at {head[:7]}, origin/main {main[:7]}:  run code-main-sync",
            )
        else:
            report.add(f"checkout:{name}", "OK", f"main {head[:7]}")


def command_path(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for directory in COMMAND_FALLBACK_DIRS:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return f"{candidate} (not on PATH)"
    return None


def check_commands(report: Report) -> None:
    for name in COMMANDS:
        path = command_path(name)
        report.add(f"command:{name}", "OK" if path else "FAIL", path or "not on PATH")


def check_catalogs(report: Report) -> None:
    base = RUNTIME / "current"
    if not base.exists():
        repo = find_repo("AI-Fleet-Coordinator")
        base = repo if repo else base
    for rel, required in CATALOGS:
        present = (base / rel).exists()
        status = "OK" if present else ("FAIL" if required else "WARN")
        report.add(
            f"catalog:{Path(rel).name}", status, f"{'present' if present else 'missing'} in {base}"
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
        )


def check_fleet_mode_doc(report: Report) -> None:
    """The Mac keeps a copy of the doc at ~/apps/FLEET-MODE.md;  the repo copy is canonical."""
    if not MAC_FLEET_MODE_DOC.is_file():
        return  # the server has no ~/apps copy:  fleet-mode reads the release's docs/FLEET-MODE.md
    base = runtime_base()
    repo_doc = base / "docs/FLEET-MODE.md" if base else None
    if not repo_doc or not repo_doc.is_file():
        report.add(
            "fleet-mode-doc",
            "WARN",
            f"no docs/FLEET-MODE.md in {base} to compare with {MAC_FLEET_MODE_DOC}",
        )
    elif repo_doc.read_bytes() == MAC_FLEET_MODE_DOC.read_bytes():
        report.add("fleet-mode-doc", "OK", f"{MAC_FLEET_MODE_DOC} equals the repo copy")
    else:
        report.add(
            "fleet-mode-doc",
            "WARN",
            f"{MAC_FLEET_MODE_DOC} differs from {repo_doc}:  edit docs/FLEET-MODE.md in the repo and copy it to ~/apps",
        )


# The Zulip key prefix where it differs from the seat (agent_sync/zulip.py SEAT_FILE_OVERRIDES).
ZULIP_PREFIX_OVERRIDES = {"GROK": "GROK_BUILD"}
_names_cache: dict[str, frozenset[str] | None] = {}


def infisical_names(folder: str, env: dict[str, str]) -> frozenset[str] | None:
    """Key NAMES in one Fleet-OPS folder (`fleet-secrets names`:  no value reaches this process), or None when
    fleet-secrets is missing or fails.  Once per folder per run."""
    if folder in _names_cache:
        return _names_cache[folder]
    binary = (
        env.get("FLEET_SECRETS_BIN")
        or shutil.which("fleet-secrets")
        or str(HOME / ".local/bin/fleet-secrets")
    )
    names: frozenset[str] | None = None
    try:
        proc = subprocess.run(
            [binary, "names", "--path", folder],
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
    _names_cache[folder] = names
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
        infisical_names("/fleetlink", env) or frozenset()
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


def check_mcp_startup(report: Report, seat: str, runner: Callable = subprocess.run) -> None:
    """Start the seat's stdio MCP the way a client does and ask for its tools.  A release that is
    missing a file the server reads fails here, before any agent notices (2026-10-16)."""
    if not shutil.which("agent-sync"):
        report.add(f"mcp-start:{seat}", "FAIL", "agent-sync is not on PATH")
        return
    try:
        result = runner(
            ["agent-sync", "mcp", "--default-seat", seat],
            input=MCP_PROBE,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        report.add(f"mcp-start:{seat}", "FAIL", "agent-sync mcp did not answer within 60 seconds")
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
        report.add(f"mcp-start:{seat}", "OK", f"agent-sync mcp serves {count} tools")
    else:
        first = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()[:1]
        why = first[0][:160] if first else f"exit {result.returncode}"
        report.add(f"mcp-start:{seat}", "FAIL", f"agent-sync mcp did not list its tools:  {why}")


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
    codex = HOME / ".codex/config.toml"
    if codex.exists():
        names = codex_mcp_names(codex.read_text())
        lacking = [n for n in ("agent-sync", "fleetlink") if n not in names]
        report.add(
            "mcp:codex",
            "OK" if not lacking else "WARN",
            "registered: "
            + ", ".join(names)
            + (f";  missing {', '.join(lacking)}" if lacking else ""),
        )
    claude = HOME / ".claude.json"
    if claude.exists():
        servers = sorted((json.loads(claude.read_text()).get("mcpServers") or {}).keys())
        lacking = [n for n in ("agent-sync", "fleetlink") if n not in servers]
        report.add(
            "mcp:claude",
            "OK" if not lacking else "WARN",
            "registered: "
            + (", ".join(servers) or "none")
            + (f";  missing {', '.join(lacking)}" if lacking else ""),
        )


def post_changes(report: Report, runner: Callable = subprocess.run) -> bool:
    """Post once per change in the failing set, so a standing problem is not repeated every run."""
    failing = sorted(f"{r['check']}: {r['detail']}" for r in report.rows if r["status"] == "FAIL")
    try:
        previous = json.loads(STATE.read_text()).get("failing", [])
    except (OSError, ValueError):
        previous = []
    if failing == previous:
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
                "agent-sync",
                "post",
                "--channel",
                "agent-sync",
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
    ):  # agent-sync missing from PATH (check_commands reports it) or hung
        return False
    if result.returncode == 0:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"failing": failing}))
        return True
    return False


def run(argv: list[str] | None = None, env: dict[str, str] | None = None) -> tuple[Report, int]:
    parser = argparse.ArgumentParser(
        prog="fleet-tools-doctor", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--seats", default="", help="comma-separated seats whose credentials this host must hold"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--no-network", action="store_true", help="compare with the local origin/main refs only"
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="when the set of failing checks changes, post it with agent-sync to #agent-sync, topic 'fleet tools doctor' (the seat comes from AGENT_SEAT in the environment, as agent-sync resolves it)",
    )
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    report = Report()
    network = not args.no_network
    check_runtime(report, network)
    check_checkouts(report, network)
    check_commands(report)
    check_catalogs(report)
    check_tool_files(report)
    check_fleet_mode_doc(report)
    for seat in [s.strip().upper() for s in args.seats.split(",") if s.strip()]:
        check_seat(report, seat, env)
        check_mcp_startup(report, seat)
    check_mcp(report)
    if args.post:
        post_changes(report)
    if args.json:
        print(json.dumps({"ok": not report.failed, "checks": report.rows}, indent=2))
    else:
        for row in report.rows:
            print(f"{row['status']:<4}  {row['check']:<28} {row['detail']}")
    return report, 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(run()[1])
