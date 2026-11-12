"""Reconcile one host's agent config with this checkout.

Surfaces, each checked by the installer that already owns it:

* skills — ``install-fleet-skills.py`` (apply uses ``--home-only``)
* rules blocks — ``fleet_lanes.install_rules``
* guard hooks — ``fleet_lanes.install_tools``
* MCP registrations — ``fleet_lanes.install_mcp`` and the fleet-recall entries
  ``install-fleet-rag.sh`` writes, including its Claude hook files

``~/.claude/CLAUDE.md`` and ``~/AGENTS.md`` are report-only.  Apply never passes
``--i-own-this-file`` and never creates a tool home that is not already there.

Check is the default.  A managed drift, a refusal or an error exits 1.
``--apply`` runs only after that check, and only for managed drift.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_SCRIPTS)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import fleet_lanes.install_mcp as mcp
import fleet_lanes.install_rules as rules
import fleet_lanes.install_tools as tools
from fleet_skill_identity import (
    catalog_skill_names,
    fold_yaml_description,
    platform_installs,
    skill_allowed_for_seat,
    specialize_from_monet,
    tool_home_exists,
)

_SKILLS_PATH = os.path.join(_SCRIPTS, "install-fleet-skills.py")
_skills_spec = importlib.util.spec_from_file_location("install_fleet_skills", _SKILLS_PATH)
if _skills_spec is None or _skills_spec.loader is None:
    raise ImportError(f"cannot load {_SKILLS_PATH}")
skills_installer = importlib.util.module_from_spec(_skills_spec)
_skills_spec.loader.exec_module(skills_installer)

_BLOCKING = ("drift", "refused", "error")
_OUTSIDE_KEYS = ("FLEET_APPS_ROOT", "FLEET_CODE_ROOT", "FLEET_SERVER_VOLUME")
_RECALL_NAME = "fleet-recall"
_HOOK_FILES = (
    "fleet-recall-session-start.sh",
    "fleet-recall-stop.py",
    "fleet-recall-prompt-gate.py",
)


class UsageError(Exception):
    """The home or the command line cannot be used."""


@dataclass
class Finding:
    surface: str
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "surface": self.surface,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Result:
    home: str
    mode: str
    findings: list[Finding] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.status in _BLOCKING for item in self.findings)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for item in self.findings:
            counts[item.status] = counts.get(item.status, 0) + 1
        order = ("drift", "refused", "error", "report-only", "current", "skip")
        parts = [f"{counts[name]} {name}" for name in order if counts.get(name)]
        return ", ".join(parts) if parts else "no findings"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": 1,
            "mode": self.mode,
            "ok": self.ok,
            "summary": self.summary(),
            "applied": list(self.applied),
            "findings": [item.as_dict() for item in self.findings],
        }


def render(result: Result) -> str:
    lines = [f"fleet-agent-config reconcile  ({result.mode})", f"home: {result.home}", ""]
    for item in result.findings:
        lines.append(f"{item.status:<12} {item.surface:<12} {item.name:<16} {item.detail}")
    lines.append("")
    lines.append(f"summary: {result.summary()}")
    if result.applied:
        lines.append("applied: " + ", ".join(result.applied))
    lines.append("")
    return "\n".join(lines)


def _detail(text: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > 240:
        return flat[:237] + "..."
    return flat or "no detail"


def _within(path: str, home: str) -> bool:
    root = os.path.realpath(home)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _resolve_home(raw: str) -> str:
    if raw is None or not str(raw).strip():
        raise UsageError("home must not be empty")
    home = os.path.abspath(os.path.expanduser(str(raw)))
    if not os.path.isdir(home):
        raise UsageError(f"home {home} is not a directory")
    if os.path.realpath(home) == os.sep:
        raise UsageError("home resolves to /; refusing")
    return home


def _env_for(home: str, base: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["HOME"] = home
    real_home = os.path.realpath(home)
    own = os.path.realpath(os.path.expanduser("~"))
    if real_home == own:
        return env
    for key in _OUTSIDE_KEYS:
        raw = (env.get(key) or "").strip()
        if not raw:
            continue
        path = os.path.realpath(os.path.expanduser(raw))
        if path != real_home and not path.startswith(real_home + os.sep):
            env.pop(key, None)
    return env


def _run_in_env(env: Mapping[str, str], fn: Callable[[], object]) -> object:
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(env)
    try:
        return fn()
    finally:
        os.environ.clear()
        os.environ.update(old)


def _skill_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for name in catalog_skill_names(skills_installer.DOCS_SKILLS):
        path = os.path.join(skills_installer.DOCS_SKILLS, name, "SKILL.md")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as handle:
            sources[name] = handle.read()
    return sources


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return None


def check_skills(home: str, env: Mapping[str, str]) -> list[Finding]:
    """Compare rendered skills with tool homes that already exist.  Read-only."""

    def run() -> list[Finding]:
        sources = _skill_sources()
        names = catalog_skill_names(skills_installer.DOCS_SKILLS)
        findings: list[Finding] = []
        for dest, seat in platform_installs():
            label = seat.tag.lower()
            if not tool_home_exists(dest):
                findings.append(Finding("skills", label, "skip", "no tool home"))
                continue
            if not _within(dest, home):
                findings.append(
                    Finding("skills", label, "refused", "skill dir resolves outside home")
                )
                continue
            missing: list[str] = []
            differ: list[str] = []
            for name in names:
                if name not in sources or not skill_allowed_for_seat(name, seat):
                    continue
                path = os.path.join(dest, name, "SKILL.md")
                rendered = fold_yaml_description(
                    specialize_from_monet(sources[name], seat, skill_name=name)
                )
                current = _read_text(path)
                if current is None:
                    missing.append(name)
                elif current != rendered:
                    differ.append(name)
            leftover = [
                name
                for name in sorted(skills_installer.NEVER_INSTALL)
                if os.path.isdir(os.path.join(dest, name))
            ]
            if missing or differ or leftover:
                bits = []
                if missing:
                    bits.append("missing " + ", ".join(missing[:8]))
                if differ:
                    bits.append("differs " + ", ".join(differ[:8]))
                if leftover:
                    bits.append("retired " + ", ".join(leftover[:8]))
                findings.append(Finding("skills", label, "drift", _detail("; ".join(bits))))
            else:
                findings.append(Finding("skills", label, "current", f"{len(names)} skills match"))
        rule_bits: list[str] = []
        seen_home = False
        for tool_home, rule_files in skills_installer.ALWAYS_ON_RULE_FILES.items():
            parent = os.path.join(home, tool_home[2:]) if tool_home.startswith("~/") else tool_home
            if not os.path.isdir(parent):
                continue
            if not _within(parent, home):
                findings.append(
                    Finding(
                        "skills", "always-on-rules", "refused", "tool home resolves outside home"
                    )
                )
                return findings
            seen_home = True
            for src_rel, dest_rel in rule_files.items():
                dest = os.path.join(home, dest_rel[2:]) if dest_rel.startswith("~/") else dest_rel
                if not _within(os.path.dirname(dest), home):
                    findings.append(
                        Finding(
                            "skills",
                            "always-on-rules",
                            "refused",
                            "rule path resolves outside home",
                        )
                    )
                    return findings
                src = os.path.join(_REPO, src_rel)
                if not os.path.isfile(src):
                    continue
                with open(src, "rb") as handle:
                    want = handle.read()
                try:
                    with open(dest, "rb") as handle:
                        have = handle.read()
                except OSError:
                    have = None
                if have != want:
                    rule_bits.append(os.path.basename(dest_rel))
        if not seen_home:
            findings.append(Finding("skills", "always-on-rules", "skip", "no tool home"))
        elif rule_bits:
            findings.append(
                Finding(
                    "skills", "always-on-rules", "drift", _detail("differs " + ", ".join(rule_bits))
                )
            )
        else:
            findings.append(Finding("skills", "always-on-rules", "current", "rule files match"))
        return findings

    return _run_in_env(env, run)  # type: ignore[return-value]


def _rules_status(ok: bool, reasons: Sequence[str]) -> str:
    if ok:
        return "current"
    text = " ".join(reasons).lower()
    if any(word in text for word in ("malformed", "read-only", "symlink", "beyond", "unsupported")):
        return "refused"
    return "drift"


def check_rules(home: str) -> list[Finding]:
    findings: list[Finding] = []
    for platform in rules.PLATFORMS:
        if platform.rel_path is None:
            findings.append(
                Finding("rules", platform.name, "skip", _detail(platform.note or "no rules file"))
            )
            continue
        root = platform.root_dir
        if root and not os.path.isdir(os.path.join(home, root)):
            status = "report-only" if platform.owner_file else "skip"
            detail = "owner file absent" if platform.owner_file else "platform not installed"
            findings.append(Finding("rules", platform.name, status, detail))
            continue
        result = rules.verify_platform(platform, home)
        status = _rules_status(result.ok, result.reasons)
        detail = _detail(
            rules.redact("; ".join(result.reasons)) if result.reasons else "block current"
        )
        if platform.owner_file:
            if status == "current":
                findings.append(
                    Finding(
                        "rules", platform.name, "report-only", "current; owner file, not applied"
                    )
                )
            else:
                findings.append(Finding("rules", platform.name, "report-only", detail))
            continue
        findings.append(Finding("rules", platform.name, status, detail))
    return findings


def check_hooks(home: str, env: Mapping[str, str]) -> list[Finding]:
    paths = tools.host_paths(home, env=dict(env))
    findings: list[Finding] = []
    for key in tools.PLATFORM_ORDER:
        platform = tools.PLATFORMS[key]
        installed = not platform.platform_dir or os.path.isdir(
            os.path.join(home, *platform.platform_dir.split("/"))
        )
        if platform.kind == "block":
            plan = tools.plan_block(platform, paths)
            if plan.state == "skip" or not installed:
                findings.append(
                    Finding("hooks", key, "skip", _detail(plan.reason or "platform not installed"))
                )
            elif plan.state == "refuse":
                findings.append(
                    Finding("hooks", key, "refused", _detail(tools.redact(plan.reason)))
                )
            elif plan.action in ("create", "append", "replace"):
                findings.append(Finding("hooks", key, "drift", _detail(plan.action)))
            else:
                findings.append(Finding("hooks", key, "current", "block current"))
            continue
        if (platform.kind == "plugin" and key != "opencode") or not platform.supported:
            if not installed:
                findings.append(Finding("hooks", key, "skip", "platform not installed"))
            else:
                findings.append(
                    Finding(
                        "hooks", key, "report-only", "no automatic hook write for this platform"
                    )
                )
            continue
        plan = tools.plan_platform(platform, paths)
        action = plan.action
        reason = _detail(tools.redact(plan.reason or action))
        if action == "skip":
            findings.append(Finding("hooks", key, "skip", reason))
        elif action == "refuse":
            findings.append(Finding("hooks", key, "refused", reason))
        elif action == "unsupported":
            findings.append(Finding("hooks", key, "report-only", reason))
        elif action in ("add", "update"):
            findings.append(Finding("hooks", key, "drift", reason or action))
        elif action == "unchanged":
            findings.append(Finding("hooks", key, "current", "hook registration matches"))
        else:
            findings.append(
                Finding("hooks", key, "error", _detail(f"unknown plan action {action}"))
            )
    return findings


def check_mcp(home: str, env: Mapping[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    for key in mcp.TARGET_KEYS:
        checks = mcp.verify_target(key, home, False, probe=False, env=dict(env))
        if checks and all(item.status == "SKIP" for item in checks):
            findings.append(Finding("mcp", key, "skip", _detail(checks[0].detail)))
            continue
        failed = [item for item in checks if item.status == "FAIL"]
        if failed:
            findings.append(
                Finding("mcp", key, "drift", _detail("; ".join(item.detail for item in failed[:4])))
            )
        else:
            findings.append(Finding("mcp", key, "current", "registered"))
    return findings


def _rag_server(home: str, env: Mapping[str, str]) -> str:
    raw = (env.get("FLEET_RAG_HOME") or "").strip()
    root = raw if raw else os.path.join(home, "apps", "fleet-rag")
    return os.path.join(root, "fleet-recall-mcp.py")


def _json_recall(path: str, server: str, create: bool, tool_dir: str, home: str) -> Finding:
    name = os.path.relpath(path, home)
    entry = {"command": "python3", "args": [server]}
    if not os.path.isfile(path):
        if create and tool_dir and os.path.isdir(os.path.join(home, tool_dir)):
            return Finding(
                "recall-mcp", name, "drift", "file absent; installer would add fleet-recall"
            )
        return Finding("recall-mcp", name, "skip", "file absent")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return Finding("recall-mcp", name, "refused", "not valid JSON; left alone")
    if not isinstance(data, dict):
        return Finding("recall-mcp", name, "refused", "top level is not an object")
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        return Finding("recall-mcp", name, "refused", "mcpServers is not an object")
    current = servers.get(_RECALL_NAME)
    if current == entry:
        return Finding("recall-mcp", name, "current", "fleet-recall matches")
    if current is not None:
        return Finding("recall-mcp", name, "report-only", "foreign fleet-recall entry left alone")
    return Finding("recall-mcp", name, "drift", "fleet-recall is not registered")


def _toml_parses(text: str) -> bool:
    try:
        import tomllib
    except ModuleNotFoundError:
        return True
    try:
        tomllib.loads(text)
    except Exception:
        return False
    return True


def _toml_recall(path: str, home: str) -> Finding:
    name = os.path.relpath(path, home)
    header = f"[mcp_servers.{_RECALL_NAME}]"
    if not os.path.isfile(path):
        return Finding("recall-mcp", name, "skip", "file absent")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        return Finding("recall-mcp", name, "refused", "cannot read the file")
    if not _toml_parses(text):
        return Finding("recall-mcp", name, "refused", "not valid TOML; left alone")
    if any(line.strip() == header for line in text.splitlines()):
        return Finding("recall-mcp", name, "current", "fleet-recall table is present")
    return Finding("recall-mcp", name, "drift", "fleet-recall table is absent")


def check_recall(home: str, env: Mapping[str, str]) -> list[Finding]:
    server = _rag_server(home, env)
    findings = [
        _json_recall(os.path.join(home, ".claude.json"), server, False, "", home),
        _json_recall(os.path.join(home, ".cursor", "mcp.json"), server, False, ".cursor", home),
        _json_recall(
            os.path.join(home, ".gemini", "config", "mcp_config.json"),
            server,
            True,
            ".gemini",
            home,
        ),
        _toml_recall(os.path.join(home, ".codex", "config.toml"), home),
        _toml_recall(os.path.join(home, ".grok", "config.toml"), home),
        _toml_recall(os.path.join(home, "apps", "grok-acp-runtime", "acp-home-config.toml"), home),
    ]
    claude = os.path.join(home, ".claude")
    if not os.path.isdir(claude):
        findings.append(Finding("recall-hooks", "claude", "skip", "platform not installed"))
        return findings
    hooks_dir = os.path.join(claude, "hooks")
    missing: list[str] = []
    for name in _HOOK_FILES:
        src = os.path.join(_SCRIPTS, "hooks", name)
        dest = os.path.join(hooks_dir, name)
        try:
            with open(src, "rb") as handle:
                want = handle.read()
            with open(dest, "rb") as handle:
                have = handle.read()
        except OSError:
            missing.append(name)
            continue
        if have != want:
            missing.append(name)
    settings = os.path.join(claude, "settings.json")
    exact = {
        "SessionStart": os.path.join(hooks_dir, _HOOK_FILES[0]),
        "Stop": "python3 " + os.path.join(hooks_dir, _HOOK_FILES[1]),
        "UserPromptSubmit": "python3 " + os.path.join(hooks_dir, _HOOK_FILES[2]),
    }
    settings_problem = ""
    if not os.path.isfile(settings):
        settings_problem = "settings.json absent"
    else:
        try:
            with open(settings, encoding="utf-8") as handle:
                raw = handle.read()
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            findings.append(
                Finding("recall-hooks", "claude", "refused", "settings.json is not valid JSON")
            )
            if missing:
                findings.append(
                    Finding(
                        "recall-hooks", "files", "drift", _detail("differs " + ", ".join(missing))
                    )
                )
            return findings
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(data, dict) or (hooks is not None and not isinstance(hooks, dict)):
            findings.append(
                Finding(
                    "recall-hooks", "claude", "refused", "settings.json hooks are not an object"
                )
            )
            return findings
        hooks = hooks or {}
        absent = []
        for event, command in exact.items():
            arr = hooks.get(event) or []
            commands: list[str] = []
            if isinstance(arr, list):
                for entry in arr:
                    if isinstance(entry, dict):
                        for hook in entry.get("hooks") or []:
                            if isinstance(hook, dict):
                                commands.append(str(hook.get("command", "")))
            if command not in commands:
                absent.append(event)
        if absent:
            settings_problem = "missing " + ", ".join(absent)
    if missing or settings_problem:
        bits = []
        if missing:
            bits.append("files " + ", ".join(missing))
        if settings_problem:
            bits.append(settings_problem)
        findings.append(Finding("recall-hooks", "claude", "drift", _detail("; ".join(bits))))
    else:
        findings.append(
            Finding("recall-hooks", "claude", "current", "hook files and settings match")
        )
    return findings


def check(home: str, env: Mapping[str, str] | None = None) -> Result:
    """Read-only drift report for ``home``."""
    root = _resolve_home(home)
    scoped = _env_for(root, env)
    result = Result(root, "check")
    steps = (
        ("skills", lambda: check_skills(root, scoped)),
        ("rules", lambda: check_rules(root)),
        ("hooks", lambda: check_hooks(root, scoped)),
        ("mcp", lambda: check_mcp(root, scoped)),
        ("recall", lambda: check_recall(root, scoped)),
    )
    for name, fn in steps:
        try:
            result.findings.extend(fn())
        except Exception as exc:  # a broken surface must not hide the others
            result.findings.append(
                Finding(name, "*", "error", _detail(f"{type(exc).__name__}: {exc}"))
            )
    return result


def _default_skills(home: str, env: Mapping[str, str]) -> int:
    script = os.path.join(_SCRIPTS, "install-fleet-skills.py")
    proc = subprocess.run(
        [sys.executable, script, "--home-only"],
        cwd=_REPO,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode


def _default_rules(home: str, platform: str, env: Mapping[str, str]) -> int:
    def run() -> int:
        return rules.main(
            ["apply", platform, "--home", home, "--create"],
            out=io.StringIO(),
            err=io.StringIO(),
        )

    return int(_run_in_env(env, run))


def _default_hooks(home: str, platform: str, env: Mapping[str, str]) -> int:
    def run() -> int:
        return tools.main(
            ["apply", platform, "--home", home, "--no-verify"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    return int(_run_in_env(env, run))


def _default_mcp(home: str, target: str, env: Mapping[str, str]) -> int:
    def run() -> int:
        return mcp.main(
            ["apply", target, "--home", home], out=io.StringIO(), err=io.StringIO(), env=dict(env)
        )

    return int(_run_in_env(env, run))


def _default_recall(home: str, env: Mapping[str, str]) -> int:
    script = os.path.join(_SCRIPTS, "install-fleet-rag.sh")
    argv = ["bash", script]
    if env.get("FLEET_AGENT_CONFIG_RECALL_HOOKS", "1") != "0":
        argv.append("--hooks")
    run_env = dict(env)
    # The installer creates ~/.gemini on its own.  A reconcile must not.
    run_env["FLEET_RAG_SKIP_ABSENT_GEMINI"] = "1"
    proc = subprocess.run(
        argv,
        cwd=_REPO,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode


def reconcile(
    home: str,
    *,
    apply: bool = False,
    env: Mapping[str, str] | None = None,
    runners: Mapping[str, Callable] | None = None,
) -> Result:
    """Check ``home``.  With ``apply``, repair managed drift through the installers."""
    root = _resolve_home(home)
    scoped = _env_for(root, env)
    report = check(root, scoped)
    if not apply:
        return report
    calls = {
        "skills": _default_skills,
        "rules": _default_rules,
        "hooks": _default_hooks,
        "mcp": _default_mcp,
        "recall": _default_recall,
    }
    if runners:
        calls.update(runners)
    drifted = [item for item in report.findings if item.status == "drift"]
    if any(item.surface == "skills" for item in drifted):
        code = calls["skills"](root, scoped)
        report.applied.append("skills")
        if code != 0:
            report.findings.append(Finding("skills", "*", "error", f"installer exited {code}"))
    for item in drifted:
        if item.surface == "rules":
            code = calls["rules"](root, item.name, scoped)
            report.applied.append(f"rules:{item.name}")
            if code != 0:
                report.findings.append(
                    Finding("rules", item.name, "error", f"installer exited {code}")
                )
        elif item.surface == "hooks":
            code = calls["hooks"](root, item.name, scoped)
            report.applied.append(f"hooks:{item.name}")
            if code != 0:
                report.findings.append(
                    Finding("hooks", item.name, "error", f"installer exited {code}")
                )
        elif item.surface == "mcp":
            code = calls["mcp"](root, item.name, scoped)
            report.applied.append(f"mcp:{item.name}")
            if code != 0:
                report.findings.append(
                    Finding("mcp", item.name, "error", f"installer exited {code}")
                )
    if any(item.surface in ("recall-mcp", "recall-hooks") for item in drifted):
        recall_env = dict(scoped)
        recall_env["FLEET_RAG_SKIP_ABSENT_GEMINI"] = "1"
        recall_env["FLEET_AGENT_CONFIG_RECALL_HOOKS"] = (
            "1" if any(item.surface == "recall-hooks" for item in drifted) else "0"
        )
        code = calls["recall"](root, recall_env)
        report.applied.append("recall")
        if code != 0:
            report.findings.append(Finding("recall", "*", "error", f"installer exited {code}"))
    if not report.applied:
        report.mode = "apply"
        return report
    again = check(root, scoped)
    again.mode = "apply"
    again.applied = report.applied
    for item in report.findings:
        if item.status == "error" and item.detail.startswith("installer exited"):
            again.findings.append(item)
    return again
