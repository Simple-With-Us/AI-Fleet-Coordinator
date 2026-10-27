"""The read-only plan side:  desired versus observed, and the installer behind each change.

``build_plan`` reads the manifest and the results of a status run and says what an apply
would do, without touching anything.  The plan is strictly read-only:  it writes no file,
starts no process and changes no state, so ``fleet-config apply --plan`` is safe to run at
any time.

Every entry names three things, because a plan that says "something is wrong" is not
actionable:

  * the rule id, the seat, the platform and the kind, so the row can be found again
  * what would change, in one sentence
  * which existing installer would make that change, and which file it would write

The installer tables below mirror the engines under ``scripts/fleet_lanes/``.  This package
never imports ``fleet_lanes`` at runtime, because a stable copy of it runs under
``python3 -I`` carrying just this package;  the mirror is pinned by a test that does
import the engines.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from . import touchid
from .checks import iso_utc, utc_now
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    ConfigKind,
    Manifest,
    ProofLevel,
    Rule,
)

__all__ = [
    "KNOWN_PLATFORMS",
    "MCP_FILES",
    "MCP_TARGETS",
    "RULES_FILES",
    "RULES_NEEDS_OWN_FLAG",
    "TOOLS_FILES",
    "Installer",
    "Plan",
    "PlanEntry",
    "build_plan",
    "installer_for",
    "known_platforms",
]

# The rules file each platform's instructions live in.  Mirrors
# ``fleet_lanes.install_rules.PLATFORMS`` (``rel_path``).  ``home-agents`` is not a seat
# platform and is deliberately absent;  ``conductor`` and ``muse-code`` have no rules file
# and are absent too.
RULES_FILES: dict[str, str] = {
    "claude": ".claude/CLAUDE.md",
    "codex": ".codex/AGENTS.md",
    "fx": ".fx/AGENTS.md",
    "grok": ".grok/GROK.md",
    "antigravity": ".gemini/config/AGENTS.md",
    "minimax": ".minimax/memory/user.md",
    "cursor": ".cursor/rules/fleet-lane-map.mdc",
    "clutch": ".clutch/dsh/AGENTS.md",
    "kimi": ".kimi-code/AGENTS.md",
    "opencode": ".config/opencode/AGENTS.md",
}

# The platforms whose rules file belongs to the owner, so applying to it needs the
# installer's ``--i-own-this-file`` flag.  Mirrors ``install_rules.Platform.owner_file``.
RULES_NEEDS_OWN_FLAG: tuple[str, ...] = ("claude",)

# The file each platform's hook registration lives in.  Mirrors
# ``fleet_lanes.install_tools.PLATFORMS`` (``config_rel``).
TOOLS_FILES: dict[str, str] = {
    "claude": ".claude/settings.json",
    "codex": ".codex/hooks.json",
    "grok": ".grok/hooks/lane-guard.json",
    "antigravity": ".gemini/config/hooks.json",
    "cursor": ".cursor/hooks.json",
    "muse": ".config/muse/settings.json",
    "clutch": ".clutch/dsh/cordis.patch.yml",
    "kimi": ".kimi-code/config.toml",
    "minimax": ".minimax/plugins/fleet-guard/hooks/hooks.json",
}

# The MCP config target each platform uses.  Mirrors ``fleet_lanes.install_mcp.TARGET_KEYS``
# and the platform ids that reach them.
MCP_TARGETS: dict[str, str] = {
    "opencode": "opencode",
    "kimi": "kimi",
    "muse": "muse",
    "muse-code": "muse",
    "minimax": "minimax",
    "fx": "fx",
    "clutch": "clutch",
}

# The file each MCP target writes.  Mirrors ``fleet_lanes.install_mcp.JSON_TARGETS``.
MCP_FILES: dict[str, str] = {
    "opencode": ".config/opencode/opencode.json",
    "kimi": ".kimi-code/mcp.json",
    "muse": ".config/muse/settings.json",
    "minimax": ".minimax/mcp.json",
    "fx": ".fx/mcp.json",
    "clutch": ".clutch/dsh/cordis.patch.yml",
}

# Every platform id this package knows about.  A platform the manifest names that is not
# here is a usage error, never an empty pass.
KNOWN_PLATFORMS: tuple[str, ...] = tuple(
    sorted(set(RULES_FILES) | set(TOOLS_FILES) | set(MCP_TARGETS))
)

# The skills installer always writes and has no read-only mode,  so it is never an apply
# target.  The plan says so instead of naming a command that would write when asked.
SKILLS_INSTALLER_NOTE = (
    "scripts/install-fleet-skills.py always writes and has no read-only mode,  so it is "
    "never used as an apply target"
)


def known_platforms() -> tuple[str, ...]:
    """The platform ids this package knows about."""
    return KNOWN_PLATFORMS


@dataclass(frozen=True)
class Installer:
    """One existing installer engine, named the way the plan shows it.

    ``argv`` is the command without the home, because the home is the caller's to name.
    ``files`` are the paths under the home the installer touches, relative to it.
    """

    key: str
    argv: tuple[str, ...]
    files: tuple[str, ...]
    note: str = ""

    def command(self, home: str = "<HOME>") -> str:
        """The command line as a human would run it."""
        return " ".join(self.argv) + " --home " + home


def _rules_installer(platform: str) -> Installer | None:
    """The installer that writes a platform's rules file, or None."""
    path = RULES_FILES.get(platform)
    if path is None:
        return None
    argv = ["python3", "-m", "fleet_lanes.install_rules", "apply", platform, "--create"]
    if platform in RULES_NEEDS_OWN_FLAG:
        argv.append("--i-own-this-file")
    return Installer(
        key="install_rules",
        argv=tuple(argv),
        files=(path,),
        note="writes the fleet rules block into the platform's own file",
    )


def _tools_installer(platform: str) -> Installer | None:
    """The installer that writes a platform's hook registration, or None."""
    path = TOOLS_FILES.get(platform)
    if path is None:
        return None
    return Installer(
        key="install_tools",
        argv=("python3", "-m", "fleet_lanes.install_tools", "apply", platform),
        files=(path,),
        note="installs the fleet guard hook and verifies that it denies",
    )


def _mcp_installer(platform: str, expected_names: tuple[str, ...] = ()) -> Installer | None:
    """The installer that registers a platform's MCP servers, or None."""
    target = MCP_TARGETS.get(platform)
    if target is None:
        return None
    path = MCP_FILES.get(target)
    argv = ["python3", "-m", "fleet_lanes.install_mcp", "apply", target]
    if "agent-sync" in expected_names and target != "opencode":
        # install_mcp registers fleet-recall only unless --with-agent-sync is given, so
        # without it a rule that expects the local ST-MCP server can never reach PASS.  The
        # installer's own note forbids the flag for opencode,  which registers its own.
        argv.append("--with-agent-sync")
    return Installer(
        key="install_mcp",
        argv=tuple(argv),
        files=(path,) if path else (),
        note="registers the fleet MCP servers in the platform's MCP config",
    )


def installer_for(rule: Rule, platform: str | None = None) -> Installer | None:
    """The installer that would make `rule`'s change, or None when there is none.

    `platform` names the platform the change is about;  when it is absent the rule's
    first platform is used.  A rule with no installer is still shown in the plan;  it is
    marked as not auto-fixable,  with the reason,  rather than dropped.
    """
    target = platform if platform is not None else (rule.platforms[0] if rule.platforms else "")
    if rule.expected.present is False:
        # The desired state is that the target is ABSENT,  so no installer can make this
        # change:  every installer here creates the thing the rule forbids.  Handing one
        # of them the rule would let apply write exactly what the manifest forbids.
        return None
    if rule.kind is ConfigKind.INSTRUCTIONS:
        if rule.check.type is not CheckType.MARKED_BLOCK_HASH:
            return None
        return _rules_installer(target)
    if rule.kind is ConfigKind.HOOKS:
        return _tools_installer(target)
    if rule.kind is ConfigKind.MCP:
        return _mcp_installer(target, tuple(rule.expected.names))
    return None


@dataclass(frozen=True)
class PlanEntry:
    """One rule that differs from its desired state, and what would change.

    ``auto_fixable`` is the whole permission the apply side has:  the manifest must allow
    an automatic fix,  an installer must exist,  and the change must be one an installer
    can make (a missing or drifted file,  not an unknown state).
    """

    rule_id: str
    seat: str
    platform: str
    kind: ConfigKind
    state: CheckState
    reason: str
    change: str
    installer: Installer | None
    files: tuple[str, ...]
    autofix_allowed: bool
    persistence_class: bool
    owner_seat: str
    proof_level_required: ProofLevel
    auto_fixable: bool
    blocked_reason: str

    def command(self, home: str = "<HOME>") -> str:
        """The installer command line, or an empty string when there is no installer."""
        if self.installer is None:
            return ""
        return self.installer.command(home)


@dataclass(frozen=True)
class Plan:
    """The whole plan for one seat:  what an apply would change, and what it would not.

    The plan holds no state and writes nothing.  ``autofixable`` and ``blocked`` split the
    entries into the ones an apply may do and the ones it must not.
    """

    seat: str | None
    kind: ConfigKind | None
    platform: str | None
    generated_at: str
    entries: tuple[PlanEntry, ...]
    notes: tuple[str, ...] = ()

    def autofixable(self) -> tuple[PlanEntry, ...]:
        """The entries an apply may act on."""
        return tuple(entry for entry in self.entries if entry.auto_fixable)

    def blocked(self) -> tuple[PlanEntry, ...]:
        """The entries an apply must not act on, with the reason on each entry."""
        return tuple(entry for entry in self.entries if not entry.auto_fixable)

    def render(self, home: str = "<HOME>") -> str:
        """The plan as text for a human."""
        lines: list[str] = []
        scope = self.seat or "every seat"
        if self.kind is not None:
            scope = "%s (%s)" % (scope, self.kind.value)
        lines.append(
            "Plan for %s:  %d entr%s,  %d auto-fixable."
            % (
                scope,
                len(self.entries),
                "y" if len(self.entries) == 1 else "ies",
                len(self.autofixable()),
            )
        )
        if not self.entries:
            lines.append("  nothing to change:  every checked rule matches the manifest")
        for entry in self.entries:
            lines.append(
                "  %-11s %s  %s  %s"
                % (
                    entry.state.value,
                    entry.rule_id,
                    entry.kind.value,
                    entry.platform,
                )
            )
            lines.append("      %s" % entry.change)
            if entry.installer is not None:
                lines.append("      installer:  %s" % entry.command(home))
                for path in entry.files:
                    lines.append("      file:  %s" % path)
            if entry.auto_fixable:
                if entry.persistence_class:
                    lines.append(
                        "      autofix:  yes,  but this is a persistence-class write and needs "
                        "a Touch ID confirmation"
                    )
                else:
                    lines.append("      autofix:  yes")
            else:
                lines.append("      autofix:  no  (%s)" % entry.blocked_reason)
        for note in self.notes:
            lines.append("  note:  %s" % note)
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, object]:
        """The plan as plain data, for a caller that renders it as JSON."""
        return {
            "seat": self.seat,
            "kind": self.kind.value if self.kind is not None else None,
            "platform": self.platform,
            "generatedAt": self.generated_at,
            "entries": [
                {
                    "ruleId": entry.rule_id,
                    "seat": entry.seat,
                    "platform": entry.platform,
                    "kind": entry.kind.value,
                    "state": entry.state.value,
                    "reason": entry.reason,
                    "change": entry.change,
                    "installer": entry.installer.key if entry.installer else None,
                    "command": entry.command(),
                    "files": list(entry.files),
                    "autofixAllowed": entry.autofix_allowed,
                    "persistenceClass": entry.persistence_class,
                    "ownerSeat": entry.owner_seat,
                    "proofLevelRequired": entry.proof_level_required.value,
                    "autoFixable": entry.auto_fixable,
                    "blockedReason": entry.blocked_reason,
                }
                for entry in self.entries
            ],
            "notes": list(self.notes),
        }


def _change_text(rule: Rule, result: CheckResult, installer: Installer | None) -> str:
    """One sentence saying what would change for this rule."""
    kind = rule.kind.value
    platform = result.platform
    subject = (
        "the fleet-wide compliance rule for %s" % platform
        if rule.kind is ConfigKind.COMPLIANCE
        else "the %s config for %s" % (kind, platform)
    )
    if result.state is CheckState.MISSING:
        if installer is not None:
            return "%s is missing;  %s would write %s" % (
                subject,
                installer.key,
                ", ".join(installer.files) or "its file",
            )
        return "%s is missing;  no installer can create it" % subject
    if result.state is CheckState.DRIFT:
        if installer is not None:
            return "%s does not match the manifest;  %s would rewrite %s" % (
                subject,
                installer.key,
                ", ".join(installer.files) or "its file",
            )
        return "%s does not match the manifest;  no installer can repair it" % subject
    return "%s could not be verified (%s:  %s);  no change is identified" % (
        subject,
        result.state.value,
        result.reason,
    )


def _blocked_reason(
    rule: Rule,
    result: CheckResult,
    installer: Installer | None,
) -> str:
    """Why the apply side must not touch this entry."""
    if not rule.autofix_allowed:
        return "the manifest does not allow an automatic fix for this rule"
    if installer is None:
        if rule.kind is ConfigKind.SKILLS:
            return SKILLS_INSTALLER_NOTE
        if (
            rule.kind is ConfigKind.INSTRUCTIONS
            and rule.check.type is not CheckType.MARKED_BLOCK_HASH
        ):
            return "no installer writes this file;  only the marked rules block can be installed"
        return "no installer can make this change"
    if result.state not in (CheckState.MISSING, CheckState.DRIFT):
        return "the state is %s,  which names no change to make" % result.state.value
    return ""


def build_plan(
    manifest: Manifest,
    observed_results: Sequence[CheckResult],
    *,
    seat: str | None = None,
    kind: ConfigKind | None = None,
    platform: str | None = None,
    now: datetime | None = None,
) -> Plan:
    """Build the read-only plan for the rules that differ from their desired state.

    ``observed_results`` are the results of a status run;  a result that is ``PASS``
    matches the desired state and produces no entry.  ``seat``, ``kind`` and ``platform``
    narrow the plan the same way they narrow a status run.

    Nothing is written and nothing is run:  the plan is a description of what an apply
    would do.
    """
    rules: dict[str, Rule] = {}
    for seat_manifest in manifest.seats:
        for rule in seat_manifest.rules:
            rules[rule.id] = rule
    for rule in manifest.compliance_rules:
        rules[rule.id] = rule
    entries: list[PlanEntry] = []
    notes: list[str] = []
    for result in observed_results:
        if seat is not None and result.seat != seat:
            continue
        if kind is not None and result.kind is not kind:
            continue
        if platform is not None and result.platform != platform:
            continue
        if result.state is CheckState.PASS:
            continue
        rule = rules.get(result.rule_id)
        if rule is None:
            notes.append(
                "the result for %s has no rule in the manifest;  it is listed but cannot be acted on"
                % result.rule_id
            )
            continue
        installer = installer_for(rule, result.platform)
        persistence = touchid.is_persistence_class(rule.kind)
        blocked = _blocked_reason(rule, result, installer)
        entries.append(
            PlanEntry(
                rule_id=rule.id,
                seat=rule.seat or result.seat,
                platform=result.platform,
                kind=rule.kind,
                state=result.state,
                reason=result.reason,
                change=_change_text(rule, result, installer),
                installer=installer,
                files=installer.files if installer is not None else (),
                autofix_allowed=rule.autofix_allowed,
                persistence_class=persistence,
                owner_seat=rule.owner_seat,
                proof_level_required=rule.proof_level,
                auto_fixable=bool(installer) and not blocked,
                blocked_reason=blocked,
            )
        )
    return Plan(
        seat=seat,
        kind=kind,
        platform=platform,
        generated_at=iso_utc(now if now is not None else utc_now()),
        entries=tuple(entries),
        notes=tuple(notes),
    )
