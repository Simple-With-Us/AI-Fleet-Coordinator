"""The seed generator for ``fleet-seats.json``:  the fleet's desired state, built from
the installer sources.

The manifest is not hand-written.  Every rule in it is derived from something the
fleet's own installers own:

  * the rules file of a platform, its path and the marker block body, from
    ``fleet_lanes.install_rules`` and the template files under
    ``scripts/fleet_lanes/rules/``;
  * the hook config file of a platform, its event and its timeout, from
    ``fleet_lanes.install_tools``;
  * the MCP config file of a platform and the servers the fleet registers in it, from
    ``fleet_lanes.install_mcp`` and ``scripts/install-fleet-rag.sh``;
  * the skills home of a seat, from ``scripts/fleet_skill_identity.py``;
  * the seat roster, the platform a seat runs on and the Zulip credential file of a
    seat, from ``fleet-apps.json``.

The expected hash of an ``instructions`` rule is the SHA-256 of the marked block body
as the installer writes it, taken from the template source and never from the file
installed on this Mac.  A hash snapshotted from the installed file would be circular:
the drift check would compare the installed file with itself and report PASS forever.
When a template changes, the seeded hash goes stale and the check reports DRIFT until
the manifest is re-seeded, which is the point of pinning a desired state.  The test
``tests/test_manifest.py`` recomputes every hash from the installer source, so a
template change is caught by the test suite and not by a silent green app.

Run it from ``scripts/``:

    python3 -m fleet_config.seed_manifest            # write the manifest
    python3 -m fleet_config.seed_manifest --check    # verify the file is current
    python3 -m fleet_config.seed_manifest --print    # write it to stdout, write nothing

The output is deterministic:  the same repository state always produces the same
bytes, so re-running the generator never produces a diff of its own.

This module is a development and build tool.  It imports ``fleet_lanes`` inside its
functions, which the runtime package never does (design doc, section 12).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "MARKER_BLOCK",
    "PACK_SKILLS_DIR",
    "SEAT_PLATFORMS",
    "build_manifest",
    "instruction_hash",
    "main",
    "platform_rules_path",
    "render_manifest",
    "write_manifest",
]

# The marker block the rules installer writes into every platform's rules file.
MARKER_BLOCK = "fleet-lane-map"

# The credential mode the fleet holds every Zulip credential at.
CREDENTIAL_MODE = "0600"

# The secret-store folder the Zulip credentials live in:  Infisical, project
# Fleet-OPS, prod, path /zulip.  The fleet-secrets CLI takes the same path.
SECRET_STORE_PATH = "/zulip"

# The fleet skills pack, one directory per skill, at the repo root.
PACK_SKILLS_DIR = "docs/fleet-skills"

# The hook ids the check derives from an installed config.  A hook id is the entry's
# own name, else its group name, else the script its command runs, and a trailing
# "-hook" is ignored when two ids are compared.
LANE_GUARD_HOOK = "lane-guard-hook"  # the five config-file platforms:  from the command
FLEET_GUARD_HOOK = "fleet-guard-hook"  # the clutch block and the minimax bundle
NAMED_GROUP_HOOK = "lane-guard"  # the antigravity group, and the muse plugin hook

# The seat to platform mapping.  Nothing in the registry maps a seat tag to a
# platform id:  fleet-apps.json seatApps.bySeat uses app slugs (claude-code,
# grok-terminal), which are not platform ids, so the mapping is pinned here, once per
# seat.  A seat with no Mac harness surface lists no platform at all and carries one
# seat-wide not-applicable entry per kind.
SEAT_PLATFORMS: dict[str, tuple[str, ...]] = {
    "CLAUDE": ("claude",),
    "CODEX": ("codex",),
    "AG": ("antigravity",),
    "CURSOR": ("cursor",),
    "GROK": ("grok",),
    "CLUTCH": ("clutch",),
    "FX": ("fx",),
    "MM": ("minimax",),
    "MA": ("muse",),
    "MC": ("muse-code",),
    "OPENCODE": ("opencode",),
    "GROK-WEB": (),
    "GROK-BOT": (),
    "ECHO": (),
    "WISE": (),
}


# --------------------------------------------------------------------------- helpers


def _repo_root() -> Path:
    """The repository root, two levels above this file."""
    return Path(__file__).resolve().parents[2]


def _scripts_dir() -> Path:
    """The ``scripts/`` directory of this checkout."""
    return Path(__file__).resolve().parents[1]


def _install_rules():
    """The ``fleet_lanes.install_rules`` module, imported on demand.

    The import is inside the function because this module is a development tool:  the
    runtime package must run with only itself present.
    """
    if str(_scripts_dir()) not in sys.path:
        sys.path.insert(0, str(_scripts_dir()))
    from fleet_lanes import install_rules

    return install_rules


def _install_tools():
    """The ``fleet_lanes.install_tools`` module, imported on demand."""
    if str(_scripts_dir()) not in sys.path:
        sys.path.insert(0, str(_scripts_dir()))
    from fleet_lanes import install_tools

    return install_tools


def platform_rules_path(platform: str) -> str:
    """The rules file of a platform, as a home-relative path, from the installer table.

    The path comes from ``install_rules.PLATFORMS``, so a path the installer changes
    is a path the manifest follows.  A platform with no rules file has no path, and
    the caller covers that kind as not applicable instead.
    """
    install_rules = _install_rules()
    row = install_rules.PLATFORM_BY_NAME.get(platform)
    if row is None or not row.rel_path:
        raise ValueError("the platform %r has no rules file" % platform)
    return "~/" + row.rel_path


def instruction_hash(platform: str) -> str:
    """The SHA-256 of the marked block body the installer writes for a platform.

    The body is the template's text with the marker lines excluded and the carriage
    returns stripped, joined with newlines and one trailing newline, encoded as UTF-8
    (design doc, section 5).  It is read from the installer source, never from the
    file installed on this Mac.
    """
    install_rules = _install_rules()
    row = install_rules.PLATFORM_BY_NAME.get(platform)
    if row is None or not row.variant:
        raise ValueError("the platform %r has no rules template" % platform)
    lines = install_rules.block_lines(row.variant)
    body = "\n".join(lines[1:-1]) + "\n"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def pack_skill_names() -> tuple[str, ...]:
    """The fleet skill names, from the canonical pack, sorted.

    A directory counts as a skill when it holds a SKILL.md, which is the rule the
    skill installer itself uses.  The names are the desired state:  the skills check
    is a subset comparison, so a seat that also holds its own skills still passes.
    """
    pack = _repo_root() / PACK_SKILLS_DIR
    names = []
    for entry in sorted(os.listdir(pack)):
        path = pack / entry
        if not path.is_dir() or entry.startswith(".") or entry == "by-seat":
            continue
        if (path / "SKILL.md").is_file():
            names.append(entry)
    if not names:
        raise ValueError("the fleet skill pack at %s holds no skill" % pack)
    return tuple(names)


def _registry() -> dict[str, dict]:
    """The live seat rows of ``fleet-apps.json``, keyed by tag.

    The registry is the roster's source of truth:  the manifest never invents a seat.
    """
    data = json.loads((_repo_root() / "fleet-apps.json").read_text(encoding="utf-8"))
    rows = {}
    for row in data.get("seats", []):
        tag = row.get("tag")
        if isinstance(tag, str) and tag and not row.get("retired") and not row.get("aliasOf"):
            rows[tag] = row
    return rows


def _credential(seat: str) -> tuple[str, str]:
    """The (path, reference name) of a seat's retired Zulip credential file.

    The path comes from the registry's ``zulipFileCode``, and the reference name is
    the name the credential is known by in the fleet's secret store.  The reference
    name is what a check reason carries, so the path never reaches a reason string.

    The flat file is on its way out:  ``~/.secrets`` is being retired by AFC-359, and
    every reader now goes through Infisical.  This helper still names the file,
    because a rule that says the retired file must be GONE needs its path.
    """
    rows = _registry()
    row = rows.get(seat)
    code = (row or {}).get("zulipFileCode")
    if not code:
        raise ValueError("the seat %r has no Zulip credential file code" % seat)
    return "~/.secrets/Zulip/%s-zuliprc" % code, "ZULIP_%s" % code.upper().replace("-", "_")


def _secret_store_keys(seat: str) -> tuple[str, ...]:
    """The secret-store key names a seat's Zulip credential is held under.

    The prefix is the seat's registry file code, uppercased with dashes turned into
    underscores, which is how the fleet names the key:  the ``Claude`` file code is
    ``CLAUDE_ZULIP_KEY``, and the ``Grok-Build`` code is ``GROK_BUILD_ZULIP_KEY``.

    The names are the names of the CREDENTIAL, never its value, so they are safe to
    put in a manifest, a result and a reason string.
    """
    rows = _registry()
    row = rows.get(seat)
    code = (row or {}).get("zulipFileCode")
    if not code:
        raise ValueError("the seat %r has no Zulip credential file code" % seat)
    prefix = code.upper().replace("-", "_")
    return ("%s_ZULIP_EMAIL" % prefix, "%s_ZULIP_KEY" % prefix, "%s_ZULIP_SITE" % prefix)


# --------------------------------------------------------------------------- rule shapes


def _rule(
    rule_id: str,
    kind: str,
    platforms: Sequence[str],
    check: dict[str, str],
    expected: dict[str, object],
    owner_seat: str,
    description: str,
    proof_level: str = "L0",
    autofix_allowed: bool = False,
    ttl_seconds: int = 900,
    source: str = "",
) -> dict[str, object]:
    """One manifest rule, in the shape the design doc pins (section 6.1)."""
    return {
        "id": rule_id,
        "kind": kind,
        "platforms": list(platforms),
        "check": check,
        "expected": expected,
        "proofLevel": proof_level,
        "ownerSeat": owner_seat,
        "autofixAllowed": autofix_allowed,
        "collector": "mac",
        "ttlSeconds": ttl_seconds,
        "description": description,
        "source": source,
    }


def _cover(kind: str, reason: str, platforms: Sequence[str] = ()) -> dict[str, object]:
    """One not-applicable entry:  a kind this seat or platform does not have."""
    return {"kind": kind, "reason": reason, "platforms": list(platforms)}


def _instructions_rule(seat: str, platform: str) -> dict[str, object]:
    """The rule that pins the marker block of a platform's rules file."""
    path = platform_rules_path(platform)
    return _rule(
        "%s-instructions-lane-map" % seat.lower(),
        "instructions",
        [platform],
        {"type": "marked_block_hash", "target": path, "detail": MARKER_BLOCK},
        {
            "description": "The %s block in %s matches the installer template"
            % (MARKER_BLOCK, path),
            "present": True,
            "sha256": instruction_hash(platform),
        },
        seat,
        "The expected hash is the body the rules installer writes, read from the "
        "installer source, so a template change shows as drift instead of hiding.",
    )


def _hooks_rule(
    seat: str,
    platform: str,
    target: str,
    event: str,
    names: Sequence[str],
    timeout_seconds: int,
    description: str,
    proof_level: str = "L1",
) -> dict[str, object]:
    """The rule that pins the guard hook of a platform."""
    return _rule(
        "%s-hooks-lane-guard" % seat.lower(),
        "hooks",
        [platform],
        {"type": "hook_registered", "target": target, "detail": event},
        {
            "description": "The fleet guard hook is registered for %s in %s" % (event, target),
            "present": True,
            "names": list(names),
            "timeoutSeconds": timeout_seconds,
        },
        seat,
        description,
        proof_level=proof_level,
    )


def _mcp_rule(seat: str, platform: str, target: str, names: Sequence[str]) -> dict[str, object]:
    """The rule that pins the fleet MCP servers of a platform."""
    return _rule(
        "%s-mcp-fleet-servers" % seat.lower(),
        "mcp",
        [platform],
        {"type": "mcp_servers_registered", "target": target, "detail": ""},
        {
            "description": "The fleet MCP servers are registered in %s" % target,
            "present": True,
            "names": list(names),
        },
        seat,
        "The comparison is a subset:  the harness may hold servers of its own, and "
        "only a missing fleet server is drift.",
    )


def _skills_rule(seat: str, platform: str, target: str, names: Sequence[str]) -> dict[str, object]:
    """The rule that pins the fleet skills of a seat."""
    return _rule(
        "%s-skills-fleet" % seat.lower(),
        "skills",
        [platform],
        {"type": "skills_installed", "target": target, "detail": ""},
        {
            "description": "The fleet skill pack is installed in %s" % target,
            "present": True,
            "names": list(names),
        },
        seat,
        "The comparison is a subset:  a seat may hold skills of its own, and only a "
        "missing fleet skill is drift.",
    )


def _subagents_rule(
    seat: str, platform: str, target: str, names: Sequence[str], description: str = ""
) -> dict[str, object]:
    """The rule that pins the sub-agent definitions of a seat."""
    return _rule(
        "%s-subagents-fleet" % seat.lower(),
        "subagents",
        [platform],
        {"type": "subagents_installed", "target": target, "detail": ""},
        {
            "description": "The fleet sub-agent definitions are installed in %s" % target,
            "present": True,
            "names": list(names),
        },
        seat,
        description or "The comparison is a subset, and an empty list makes this a presence check.",
    )


def _credential_rule(seat: str, platform: str) -> dict[str, object]:
    """The rule that pins a seat's Zulip credential in the secret store.

    The credential lives in Infisical, project Fleet-OPS, path ``/zulip``, under the
    seat's key names.  The check asks the ``fleet-secrets`` CLI whether those names
    are there, and the ``names`` verb prints names only, so no value can reach a
    result.
    """
    keys = _secret_store_keys(seat)
    reference = "the %s Zulip credential" % seat
    return _rule(
        "%s-permissions-zulip-key" % seat.lower(),
        "permissions",
        [platform],
        {"type": "credential_key_present", "target": SECRET_STORE_PATH, "detail": reference},
        {
            "description": "The Zulip credential is reachable in the secret store at %s"
            % SECRET_STORE_PATH,
            "present": True,
            "names": list(keys),
        },
        seat,
        "The check asks the fleet-secrets CLI for key NAMES and never for a value, so "
        "a secret cannot reach a result, a reason or a log.  A secret store that "
        "cannot be reached is a failing UNKNOWN, never a pass.",
    )


def _retired_credential_rule(seat: str, platform: str) -> dict[str, object]:
    """The rule that the RETIRED flat credential file must be gone.

    ``~/.secrets`` is being retired by AFC-359.  The file may still exist while that
    work is in flight, so the rule says it must be absent:  a file that is there is
    DRIFT, which is the finding AFC-359 wants to see, and it goes green on its own
    when the folder is deleted.
    """
    path, reference = _credential(seat)
    return _rule(
        "%s-permissions-zulip-retired-file" % seat.lower(),
        "permissions",
        [platform],
        {"type": "credential_stat", "target": path, "detail": reference},
        {
            "description": "The retired flat Zulip credential file is gone, stat only",
            "present": False,
        },
        seat,
        "AFC-359 retires ~/.secrets, so the flat credential file must be absent.  The "
        "check reads stat data only and never opens the file.",
    )


def _permissions_mode_rule(seat: str, platform: str, target: str) -> dict[str, object]:
    """The rule that pins the mode of a settings file that holds permission rules."""
    return _rule(
        "%s-permissions-settings" % seat.lower(),
        "permissions",
        [platform],
        {"type": "permissions_match", "target": target, "detail": ""},
        {
            "description": "The permission settings file %s is owner-only" % target,
            "present": True,
            "mode": CREDENTIAL_MODE,
        },
        seat,
        "The permission identifiers of this file belong to the harness, not to the "
        "fleet, so the rule pins the mode alone and lists no identifier.",
    )


# --------------------------------------------------------------------------- the seats


def _hook_target(platform: str) -> tuple[str, str, int, str]:
    """The (target, event, timeout, hook id) of a platform's guard hook.

    The five platforms with a hook config file are read from the installer table, so a
    path or an event the installer changes is a path the manifest follows.  Clutch and
    MiniMax Code hold their hook somewhere else, and those two are pinned here.
    """
    install_tools = _install_tools()
    row = install_tools.PLATFORMS.get(platform)
    if row is None:
        raise ValueError("the platform %r has no hook surface" % platform)
    if platform in ("claude", "codex", "grok", "antigravity", "cursor"):
        timeout = install_tools.HOOK_TIMEOUT_S
        # Antigravity keeps one named group per hook, so its id is the group name;
        # the others name the hook after the script their command runs.
        hook_id = NAMED_GROUP_HOOK if platform == "antigravity" else LANE_GUARD_HOOK
        return "~/" + row.config_rel, row.event, timeout, hook_id
    if platform == "clutch":
        # The Clutch cordis patch is YAML, which the standard library cannot read.
        # The hook loads a JSON file in the stable dir instead, and that JSON is what
        # this rule reads.
        return (
            "~/apps/lane-tools/" + install_tools.CLUTCH_HOOKS_FILE,
            "PreToolUse",
            install_tools.HOOK_TIMEOUT_S,
            FLEET_GUARD_HOOK,
        )
    if platform == "minimax":
        # MiniMax Code reads no settings file for hooks:  it loads them from a local
        # plugin bundle, whose hooks.json holds the fleet hook.
        return (
            "~/" + install_tools.MINIMAX_PLUGIN_REL + "/" + install_tools.MINIMAX_HOOKS_FILE,
            "PreToolUse",
            install_tools.MINIMAX_HOOK_TIMEOUT_S,
            FLEET_GUARD_HOOK,
        )
    raise ValueError("the platform %r has no hook config file" % platform)


def _mcp_target(platform: str) -> str:
    """The MCP config file of a platform, from the installer tables.

    Six platforms are registered by ``install_mcp``, and the five harnesses it does
    not touch are registered by ``scripts/install-fleet-rag.sh``, which is a shell
    script and so is pinned here.
    """
    if str(_scripts_dir()) not in sys.path:
        sys.path.insert(0, str(_scripts_dir()))
    from fleet_lanes import install_mcp

    target = install_mcp.JSON_BY_KEY.get(platform)
    if target is None:
        alias = install_mcp.ALIASES.get(platform)
        target = install_mcp.JSON_BY_KEY.get(alias) if alias else None
    if target is not None:
        return "~/" + target.rel_path
    shell_targets = {
        "claude": "~/.claude.json",
        "cursor": "~/.cursor/mcp.json",
        "antigravity": "~/.gemini/config/mcp_config.json",
        "codex": "~/.codex/config.toml",
        "grok": "~/.grok/config.toml",
    }
    if platform in shell_targets:
        return shell_targets[platform]
    raise ValueError("the platform %r has no MCP config file" % platform)


def _mcp_servers(platform: str) -> tuple[str, ...]:
    """The MCP servers the fleet registers for a platform.

    ``fleet-recall`` is registered everywhere.  ``agent-sync`` is registered only for
    the platforms with a fixed seat, and the installer's own note holds it back for
    OpenCode, whose seat is shared with a Conductor session that has no default.
    """
    fixed_seat = ("muse-code", "minimax", "fx")
    if platform == "muse":
        # The muse target of install_mcp carries the fixed seat MC, so the agent-sync
        # entry is registered as MC and never as MA.
        return ("fleet-recall",)
    if platform in fixed_seat:
        return ("fleet-recall", "agent-sync")
    return ("fleet-recall",)


def _skills_target(seat: str) -> str:
    """The skills home of a seat, from the skill installer's seat table."""
    if str(_scripts_dir()) not in sys.path:
        sys.path.insert(0, str(_scripts_dir()))
    from fleet_skill_identity import SEATS

    for row in SEATS.values():
        if row.tag == seat and row.dest.startswith("~/"):
            return row.dest
    raise ValueError("the seat %r has no skills home" % seat)


def _no_hooks_rule(seat: str, platform: str, target: str, description: str) -> dict[str, object]:
    """The rule that pins the absence of a hook, for a harness with no hook mechanism.

    ``expected.present`` false inverts the check:  the target must hold no hook for
    the event.  The rule passes while the harness has no hook surface, and reports
    drift the moment one is registered there.
    """
    return _rule(
        "%s-hooks-none" % seat.lower(),
        "hooks",
        [platform],
        {"type": "hook_registered", "target": target, "detail": "PreToolUse"},
        {
            "description": "No hook is registered for PreToolUse in %s" % target,
            "present": False,
        },
        seat,
        description,
        proof_level="L0",
    )


def _harness_seat(
    seat: str,
    platform: str,
    *,
    subagents: tuple[str, Sequence[str]] | None,
    skip_mcp_reason: str | None = None,
    skip_instructions_reason: str | None = None,
    skip_skills_reason: str | None = None,
    hook_absent_target: str | None = None,
    hook_absent_description: str = "",
    subagents_description: str = "",
    extra_covers: Sequence[dict[str, object]] = (),
) -> dict[str, object]:
    """One seat entry for a harness that has a Mac surface.

    The six kinds are the same for every such seat:  the rules file, the guard hook,
    the MCP servers, the skills, the sub-agent definitions and the credential.  A
    platform whose MCP registration cannot be read by the standard-library reader
    names the reason instead, and is covered as not applicable.  A harness with no
    rules file and one with no hook mechanism are covered the same way.
    """
    skills = pack_skill_names()
    rules: list[dict[str, object]] = []
    covers: list[dict[str, object]] = list(extra_covers)
    if skip_instructions_reason is None:
        rules.append(_instructions_rule(seat, platform))
    else:
        covers.append(_cover("instructions", skip_instructions_reason, [platform]))
    if hook_absent_target is None:
        target, event, timeout, hook_id = _hook_target(platform)
        rules.append(
            _hooks_rule(
                seat,
                platform,
                target,
                event,
                [hook_id],
                timeout,
                "The hook id is the one the check derives from the installed config, "
                "and the timeout is the one the installer writes.",
            )
        )
    else:
        rules.append(_no_hooks_rule(seat, platform, hook_absent_target, hook_absent_description))
    if skip_mcp_reason is None:
        rules.append(_mcp_rule(seat, platform, _mcp_target(platform), _mcp_servers(platform)))
    else:
        covers.append(_cover("mcp", skip_mcp_reason, [platform]))
    if skip_skills_reason is None:
        rules.append(_skills_rule(seat, platform, _skills_target(seat), skills))
    else:
        covers.append(_cover("skills", skip_skills_reason, [platform]))
    if subagents is None:
        covers.append(
            _cover(
                "subagents",
                "The fleet installs no sub-agent definitions for the %s harness." % platform,
                [platform],
            )
        )
    else:
        rules.append(
            _subagents_rule(seat, platform, subagents[0], subagents[1], subagents_description)
        )
    rules.append(_credential_rule(seat, platform))
    rules.append(_retired_credential_rule(seat, platform))
    return {"platforms": [platform], "rules": rules, "notApplicable": covers}


def _hosted_seat(seat: str, subject: str, credential_reason: str) -> dict[str, object]:
    """One seat entry for a seat with no Mac config surface.

    Such a seat lists no platform and carries one seat-wide not-applicable entry per
    kind, each with a reason:  silence is never a cover (design doc, section 6.2).
    """
    return {
        "platforms": [],
        "rules": [],
        "notApplicable": [
            _cover("instructions", "%s, so this Mac holds no rules file for it." % subject),
            _cover("hooks", "%s, so this Mac holds no hook config for it." % subject),
            _cover("mcp", "%s, so this Mac holds no MCP config for it." % subject),
            _cover("skills", "%s, so this Mac holds no skills home for it." % subject),
            _cover("subagents", "%s, so this Mac holds no sub-agent definitions for it." % subject),
            _cover("permissions", credential_reason),
        ],
    }


def _build_seats() -> dict[str, dict[str, object]]:
    """Every live seat's manifest entry, keyed by registry tag."""
    seats: dict[str, dict[str, object]] = {}

    # The five harnesses with a rules file, a hook config file and an MCP config file.
    seats["CLAUDE"] = _harness_seat(
        "CLAUDE", "claude", subagents=("~/.claude/agents", ("code-scout", "docs-scribe"))
    )
    seats["CODEX"] = _harness_seat(
        "CODEX",
        "codex",
        subagents=("~/.codex/agents", ("code-scout.toml", "docs-scribe.toml")),
        subagents_description=(
            "Codex keeps its sub-agent definitions as TOML files, and the check names a "
            "definition by its file name, so the expected names carry the .toml suffix."
        ),
    )
    seats["AG"] = _harness_seat("AG", "antigravity", subagents=None)
    seats["CURSOR"] = _harness_seat("CURSOR", "cursor", subagents=None)
    seats["GROK"] = _harness_seat("GROK", "grok", subagents=None)

    # Clutch:  the hook lives in a JSON file of the stable dir, because the cordis
    # patch that mounts it is YAML.  The MCP registration is a marked block in that
    # same YAML patch, which the standard library cannot read.
    seats["CLUTCH"] = _harness_seat(
        "CLUTCH",
        "clutch",
        subagents=None,
        skip_mcp_reason=(
            "The Clutch MCP registration is a marked block in the YAML cordis patch "
            "(~/.clutch/dsh/cordis.patch.yml), which the standard-library MCP reader "
            "cannot parse;  Clutch carries fleet-recall as a native agent preset."
        ),
    )

    # Fx:  no hook mechanism at all.  The rules file, the MCP servers and the skills
    # are there, and the permission settings file is its only guard surface.
    seats["FX"] = _harness_seat(
        "FX",
        "fx",
        subagents=None,
        hook_absent_target="~/.fx/settings.json",
        hook_absent_description=(
            "Fx has no hook mechanism at all:  fx --help lists no hook, plugin or "
            "extension command and the published docs have no hooks page, so no guard "
            "hook can be installed for this seat.  The rule pins the absence of one."
        ),
        extra_covers=[
            _cover(
                "hooks",
                "Fx has no hook mechanism:  fx --help lists no hook, plugin or extension "
                "command and the published docs have no hooks page, so no guard hook can "
                "be installed for this seat.",
                ["fx"],
            )
        ],
    )
    seats["FX"]["rules"].append(_permissions_mode_rule("FX", "fx", "~/.fx/settings.json"))

    # MiniMax Code:  the hook is a local plugin bundle, and the sub-agent definitions
    # are the Mavis ones the seat's identity names.
    seats["MM"] = _harness_seat("MM", "minimax", subagents=("~/.minimax/agents", ()))

    # Muse Code and Muse Assist.  Muse Code takes its hooks from a plugin, and the
    # plugin manifest holds its hook table in a shape the Phase 2 hook reader does not
    # parse, so the hooks kind is covered as not applicable rather than by a rule that
    # could only ever report a false miss.
    muse_covers = [
        _cover(
            "hooks",
            "Muse takes its hooks from a plugin bundle, whose manifest holds the hook "
            "table under capabilities.hooks with an argv command;  the Phase 2 hook "
            "reader reads neither, so there is no hook file this check can read.",
        ),
    ]
    seats["MC"] = _harness_seat(
        "MC",
        "muse-code",
        subagents=None,
        skip_instructions_reason=(
            "Muse Code has no user-level rules file of its own:  install_rules lists the "
            "muse-code platform as unsupported, and Muse reads ~/.claude/CLAUDE.md as its "
            "fallback, which the claude platform covers."
        ),
        hook_absent_target="~/.config/muse/settings.json",
        hook_absent_description=(
            "Muse takes its hooks from a plugin bundle, not from a settings file, so no "
            "hook is registered in the settings file.  The plugin manifest holds the "
            "hook table in a shape the Phase 2 hook reader does not parse."
        ),
        extra_covers=muse_covers,
    )
    seats["MA"] = _harness_seat(
        "MA",
        "muse",
        subagents=None,
        skip_instructions_reason=(
            "Muse has no user-level rules file of its own:  install_rules lists the "
            "muse-code platform as unsupported, and Muse reads ~/.claude/CLAUDE.md as its "
            "fallback, which the claude platform covers."
        ),
        skip_skills_reason=(
            "Muse Assist runs on a cloud VM and takes its skills from the by-seat pack "
            "(docs/fleet-skills/by-seat/muse-assist), so this Mac holds no skills home "
            "for it."
        ),
        hook_absent_target="~/.config/muse/settings.json",
        hook_absent_description=(
            "Muse takes its hooks from a plugin bundle, not from a settings file, so no "
            "hook is registered in the settings file.  The plugin manifest holds the "
            "hook table in a shape the Phase 2 hook reader does not parse."
        ),
        extra_covers=muse_covers,
    )

    # OpenCode:  a rules file, an MCP config file and a credential.  Its guard hook
    # will be a local plugin, because OpenCode has no hooks key in its config but its
    # plugin API does have tool.execute.before and permission.ask hooks.
    seats["OPENCODE"] = _harness_seat(
        "OPENCODE",
        "opencode",
        subagents=None,
        hook_absent_target="~/.config/opencode/opencode.json",
        hook_absent_description=(
            "OpenCode has no hooks key in its config file, so the desired state is that "
            "none is registered there.  Its guard will be a local plugin, because the "
            "plugin API has tool.execute.before and permission.ask hooks;  L1 becomes "
            "available once that plugin lands."
        ),
        skip_skills_reason=(
            "OpenCode loads the fleet skills from the project tree (skills/), not from a "
            "user-level skills home, so this Mac holds no skills directory to check."
        ),
        extra_covers=[
            _cover(
                "hooks",
                "OpenCode has no hooks key in its config:  its guard will be a local "
                "plugin, because the plugin API has tool.execute.before and "
                "permission.ask hooks.  The plugin is not installed yet, so the hooks "
                "rule asserts that no config hook is registered and L1 arrives with it.",
                ["opencode"],
            ),
        ],
    )

    # The seats with no Mac config surface.
    seats["GROK-WEB"] = _hosted_seat(
        "GROK-WEB",
        "Grok Web runs in the web app and the iOS app",
        "Grok Web's credential is held by the server listener's secret store, not by "
        "a Mac file of its own.",
    )
    seats["GROK-BOT"] = _hosted_seat(
        "GROK-BOT",
        "The Grok Bot personas run as Cursor cloud agents",
        "Grok Bot has no Zulip credential of its own:  its GB-<ROLE> personas are "
        "owner-managed and hold no zuliprc.",
    )
    seats["ECHO"] = _hosted_seat(
        "ECHO",
        "Echo runs on the Instinct app's own computer and reaches the fleet through "
        "the hosted agent-sync bridge",
        "Echo's credential is held by the hosted listener's secret store, not by a Mac "
        "file of its own.",
    )
    seats["WISE"] = _hosted_seat(
        "WISE",
        "Wise runs on the Instinct app's own computer and reaches the fleet through "
        "the hosted agent-sync bridge",
        "Wise's credential is held by the hosted listener's secret store, not by a Mac "
        "file of its own.",
    )
    return seats


# --------------------------------------------------------------------------- the manifest


def build_manifest() -> dict[str, object]:
    """The whole manifest as a plain dict, ready to render as JSON.

    The seats come from the registry roster, so a seat that is added to
    ``fleet-apps.json`` and not to this seed is caught by the loader, which refuses a
    live seat with no manifest entry.
    """
    seats = _build_seats()
    live = set(_registry())
    missing = sorted(live - set(seats))
    if missing:
        raise ValueError("the seed has no entry for the live seat(s): %s" % ", ".join(missing))
    unknown = sorted(set(seats) - live)
    if unknown:
        raise ValueError(
            "the seed has an entry for a seat the registry does not hold live: %s"
            % ", ".join(unknown)
        )
    return {"schemaVersion": 2, "seats": {tag: seats[tag] for tag in sorted(seats)}}


def render_manifest(manifest: dict[str, object]) -> str:
    """The manifest as the text of ``fleet-seats.json``.

    Two-space indent, sorted keys and one trailing newline, so the file is stable in
    a diff and running the generator twice produces identical bytes.
    """
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_manifest(path: Path | None = None) -> Path:
    """Write the manifest and return the path it was written to."""
    target = path if path is not None else _repo_root() / "fleet-seats.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_manifest(build_manifest()), encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    """Write the manifest, or check that the file on disk is the one this seed builds."""
    parser = argparse.ArgumentParser(
        prog="python3 -m fleet_config.seed_manifest",
        description="Build fleet-seats.json from the installer sources.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when the file on disk differs from the seed, writing nothing",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="write the manifest to stdout and write no file",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    text = render_manifest(build_manifest())
    target = _repo_root() / "fleet-seats.json"
    if args.print_only:
        sys.stdout.write(text)
        return 0
    if args.check:
        if not target.is_file():
            print("fleet-seats.json is missing;  run python3 -m fleet_config.seed_manifest")
            return 1
        current = target.read_text(encoding="utf-8")
        if current != text:
            print("fleet-seats.json is stale;  run python3 -m fleet_config.seed_manifest")
            return 1
        print("fleet-seats.json is current")
        return 0
    target.write_text(text, encoding="utf-8")
    print("wrote %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
