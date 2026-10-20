"""Loader skeleton for the per-seat desired-state manifest, ``fleet-seats.json``.

The manifest is a sibling of ``fleet-apps.json`` at the repo root, never an edit to it.
Its seat keys are registry tags, and the loader refuses any key the registry does not
hold as a live seat, so a seat name is never hand-typed into code or trusted from the
file alone.  The full shape and the coverage rule are pinned in
``docs/plans/2026-10-19-fleet-config-design.md``, sections 6 and 7.

This skeleton parses the document into types.py dataclasses and checks the top-level
shape.  Coverage validation (every seat, platform and kind covered by a rule or an
explicit not-applicable entry) and platform validation are separate functions with
pinned signatures, so the manifest implementer fills them in without moving callers.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.  This package never imports
``fleet_lanes`` at runtime:  a stable copy run with ``python3 -I`` carries just this
package, so the registry file is read as JSON here and ``fleet_lanes.layout`` remains
the canonical parser whose semantics this loader mirrors.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .types import (
    CONFIG_KINDS,
    CheckSpec,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    Manifest,
    NotApplicable,
    ProofLevel,
    Rule,
    SeatManifest,
)

__all__ = [
    "CHECK_TYPES_BY_KIND",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "REGISTRY_FILENAME",
    "ManifestError",
    "default_manifest_path",
    "default_registry_path",
    "known_platforms",
    "load_manifest",
    "registry_seat_tags",
    "validate_coverage",
    "validate_platforms",
    "validate_rules",
]

# The pinned manifest version, checked exactly.  A breaking manifest change raises it.
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "fleet-seats.json"
REGISTRY_FILENAME = "fleet-apps.json"

# The check types each kind supports, from design doc section 5.  A rule whose check
# type is not listed for its kind is rejected:  the pairing is fixed, so a rule can
# never ask for a check its kind has no meaning for.
CHECK_TYPES_BY_KIND: dict[ConfigKind, tuple[CheckType, ...]] = {
    ConfigKind.INSTRUCTIONS: (CheckType.MARKED_BLOCK_HASH, CheckType.FILE_HASH),
    ConfigKind.HOOKS: (CheckType.HOOK_REGISTERED,),
    ConfigKind.MCP: (CheckType.MCP_SERVERS_REGISTERED,),
    ConfigKind.SKILLS: (CheckType.SKILLS_INSTALLED,),
    ConfigKind.SUBAGENTS: (CheckType.SUBAGENTS_INSTALLED,),
    ConfigKind.PERMISSIONS: (
        CheckType.PERMISSIONS_MATCH,
        CheckType.CREDENTIAL_STAT,
        CheckType.CREDENTIAL_KEY_PRESENT,
    ),
}


class ManifestError(ValueError):
    """The manifest or the registry cannot be trusted, so the caller must refuse."""


def default_manifest_path() -> Path:
    """``fleet-seats.json`` at the repo root (two levels above ``scripts/``)."""
    return Path(__file__).resolve().parents[2] / MANIFEST_FILENAME


def default_registry_path() -> Path:
    """``fleet-apps.json`` at the repo root, unless ``FLEET_APPS_JSON`` names another."""
    override = (os.environ.get("FLEET_APPS_JSON") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / REGISTRY_FILENAME


def registry_seat_tags(registry_path: Path | None = None) -> tuple[str, ...]:
    """The live roster:  ``seats[].tag`` from fleet-apps.json, in registry order.

    A row counts as live when it is not marked retired and carries no ``aliasOf``.  This
    is the one source of truth for seat names;  code never hardcodes a tag, and the
    manifest may only use keys returned here.  Raises ManifestError when the registry is
    missing or is not the expected shape, because a run without a roster proves nothing.
    """
    target = registry_path if registry_path is not None else default_registry_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError("cannot read the fleet registry at %s" % target) from exc
    except ValueError as exc:
        raise ManifestError("the fleet registry at %s is not valid JSON" % target) from exc
    if not isinstance(data, dict) or not isinstance(data.get("seats"), list):
        raise ManifestError("the fleet registry at %s has no seats list" % target)
    tags: list[str] = []
    for row in data["seats"]:
        if not isinstance(row, dict):
            continue
        tag = row.get("tag")
        if isinstance(tag, str) and tag and not row.get("retired") and not row.get("aliasOf"):
            tags.append(tag)
    if not tags:
        raise ManifestError("the fleet registry at %s holds no live seats" % target)
    return tuple(tags)


def _enum(enum_type, value: object, what: str):
    """One enum value from the manifest, or ManifestError naming the field."""
    try:
        return enum_type(value)
    except ValueError:
        raise ManifestError("%s has an unknown value %r" % (what, value)) from None


def _str_tuple(value: object, what: str) -> tuple[str, ...]:
    """A list of strings from the manifest, or ManifestError naming the field."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError("%s must be a list of strings" % what)
    return tuple(value)


def _parse_rule(raw: object, seat: str) -> Rule:
    """One rule object into a ``Rule``.  Unknown enums and shapes raise ManifestError."""
    if not isinstance(raw, dict):
        raise ManifestError("seat %s holds a rule that is not an object" % seat)
    check_raw = raw.get("check")
    expected_raw = raw.get("expected")
    if not isinstance(check_raw, dict) or not isinstance(expected_raw, dict):
        raise ManifestError("seat %s holds a rule without a check and expected object" % seat)
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise ManifestError("seat %s holds a rule without an id" % seat)
    return Rule(
        id=rule_id,
        kind=_enum(ConfigKind, raw.get("kind"), "rule %s kind" % rule_id),
        platforms=_str_tuple(raw.get("platforms"), "rule %s platforms" % rule_id),
        check=CheckSpec(
            type=_enum(CheckType, check_raw.get("type"), "rule %s check type" % rule_id),
            target=str(check_raw.get("target") or ""),
            detail=str(check_raw.get("detail") or ""),
        ),
        expected=ExpectedState(
            description=str(expected_raw.get("description") or ""),
            present=expected_raw.get("present"),
            sha256=expected_raw.get("sha256"),
            names=_str_tuple(expected_raw.get("names"), "rule %s expected names" % rule_id),
            mode=expected_raw.get("mode"),
            timeout_seconds=expected_raw.get("timeoutSeconds"),
        ),
        proof_level=_enum(ProofLevel, raw.get("proofLevel"), "rule %s proofLevel" % rule_id),
        owner_seat=str(raw.get("ownerSeat") or ""),
        autofix_allowed=bool(raw.get("autofixAllowed")),
        seat=seat,
        collector=_enum(Collector, raw.get("collector") or "mac", "rule %s collector" % rule_id),
        ttl_seconds=int(raw.get("ttlSeconds") or 900),
        description=str(raw.get("description") or ""),
    )


def _parse_seat(tag: str, raw: object) -> SeatManifest:
    """One seat entry into a ``SeatManifest``.  The key is the registry tag."""
    if not isinstance(raw, dict):
        raise ManifestError("seat %s entry is not an object" % tag)
    rules = tuple(_parse_rule(item, tag) for item in raw.get("rules") or [])
    covers: list[NotApplicable] = []
    for item in raw.get("notApplicable") or []:
        if not isinstance(item, dict):
            raise ManifestError("seat %s holds a not-applicable entry that is not an object" % tag)
        covers.append(
            NotApplicable(
                kind=_enum(ConfigKind, item.get("kind"), "seat %s not-applicable kind" % tag),
                reason=str(item.get("reason") or ""),
                platforms=_str_tuple(
                    item.get("platforms"), "seat %s not-applicable platforms" % tag
                ),
            )
        )
    return SeatManifest(
        seat=tag,
        platforms=_str_tuple(raw.get("platforms"), "seat %s platforms" % tag),
        rules=rules,
        not_applicable=tuple(covers),
    )


def load_manifest(
    path: Path | None = None,
    registry_path: Path | None = None,
) -> Manifest:
    """Load and parse the manifest, checking it against the live registry roster.

    Raises ManifestError for a missing or malformed file, a wrong ``schemaVersion``, a
    seat key the registry does not hold as live, a live seat with no entry, or a
    duplicate rule id.  Coverage and platform validation run separately (below), so a
    caller can report parse errors and coverage errors in distinct passes.
    """
    target = path if path is not None else default_manifest_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError("cannot read the manifest at %s" % target) from exc
    except ValueError as exc:
        raise ManifestError("the manifest at %s is not valid JSON" % target) from exc
    if not isinstance(data, dict):
        raise ManifestError("the manifest at %s is not a JSON object" % target)
    if data.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("the manifest schemaVersion must be %d" % MANIFEST_SCHEMA_VERSION)
    raw_seats = data.get("seats")
    if not isinstance(raw_seats, dict):
        raise ManifestError("the manifest seats must be an object keyed by registry tag")
    live = registry_seat_tags(registry_path)
    unknown = sorted(set(raw_seats) - set(live))
    if unknown:
        raise ManifestError(
            "manifest seats not in the live registry roster: %s" % ", ".join(unknown)
        )
    missing = sorted(set(live) - set(raw_seats))
    if missing:
        raise ManifestError("live registry seats with no manifest entry: %s" % ", ".join(missing))
    seats = tuple(_parse_seat(tag, raw_seats[tag]) for tag in live if tag in raw_seats)
    seen: dict[str, str] = {}
    for seat in seats:
        for rule in seat.rules:
            if rule.id in seen:
                raise ManifestError(
                    "duplicate rule id %s (seats %s and %s)" % (rule.id, seen[rule.id], seat.seat)
                )
            seen[rule.id] = seat.seat
    return Manifest(schema_version=MANIFEST_SCHEMA_VERSION, seats=seats, source_path=str(target))


def known_platforms() -> tuple[str, ...]:
    """The platform ids the installer tables define, sorted.

    This helper is for development and tests only:  it imports ``fleet_lanes`` inside
    the function body, which the runtime package never does (design doc, section 12).
    A runtime caller passes the known set in as data, to ``validate_platforms``.

    The set is the union of the two installer tables:  ``install_rules.PLATFORMS``
    (the harness config surfaces that carry a rules file) and
    ``install_tools.PLATFORMS`` (the harness config surfaces that carry a hook).  The
    two tables do not name the same platforms:  ``install_rules`` has no ``muse`` row
    and ``install_tools`` has no ``home-agents`` row, and a platform id a manifest
    names must be known to one of them.
    """
    from fleet_lanes import install_rules, install_tools

    names = {platform.name for platform in install_rules.PLATFORMS}
    names.update(install_tools.PLATFORMS.keys())
    return tuple(sorted(names))


def _covered(seat: SeatManifest, platform: str, kind: ConfigKind) -> bool:
    """True when a rule or an explicit cover accounts for one (platform, kind) pair."""
    for rule in seat.rules:
        if rule.kind is kind and platform in rule.platforms:
            return True
    for cover in seat.not_applicable:
        if cover.kind is kind and (not cover.platforms or platform in cover.platforms):
            return True
    return False


def validate_coverage(manifest: Manifest) -> None:
    """Every (seat, platform, kind) triple is covered, or ManifestError is raised.

    A triple is covered when a rule for that seat names the kind and the platform, or a
    not-applicable entry covers it (an entry with empty platforms covers every platform
    the seat lists).  A seat with an empty platforms list needs one seat-wide
    not-applicable entry per kind, each with a reason.  Silence is never a cover.

    Every not-applicable entry needs a non-empty reason:  a cover without a reason is
    a gap dressed as a decision, so it is rejected here rather than rendered as one.
    """
    problems: list[str] = []
    for seat in manifest.seats:
        for cover in seat.not_applicable:
            if not cover.reason.strip():
                problems.append(
                    "seat %s has a not-applicable entry for %s with no reason"
                    % (seat.seat, cover.kind.value)
                )
        if not seat.platforms:
            for kind in CONFIG_KINDS:
                if not any(
                    cover.kind is kind and not cover.platforms for cover in seat.not_applicable
                ):
                    problems.append(
                        "seat %s lists no platform and has no seat-wide not-applicable entry for %s"
                        % (seat.seat, kind.value)
                    )
            continue
        for platform in seat.platforms:
            for kind in CONFIG_KINDS:
                if not _covered(seat, platform, kind):
                    problems.append(
                        "seat %s platform %s kind %s is covered by no rule and by no "
                        "not-applicable entry" % (seat.seat, platform, kind.value)
                    )
    if problems:
        raise ManifestError(
            "the manifest does not cover every (seat, platform, kind) triple:  %s"
            % ";  ".join(problems)
        )


def validate_platforms(manifest: Manifest, known_platforms: tuple[str, ...]) -> None:
    """Every platform named anywhere in the manifest is known, or ManifestError.

    ``known_platforms`` comes from ``fleet_lanes.install_rules.PLATFORMS`` at the call
    site, passed in as data so this package never imports ``fleet_lanes`` at runtime.
    ``known_platforms()`` above supplies that set to a development or test caller.
    """
    known = set(known_platforms)
    named = set()
    for seat in manifest.seats:
        named.update(seat.platforms)
        for rule in seat.rules:
            named.update(rule.platforms)
        for cover in seat.not_applicable:
            named.update(cover.platforms)
    unknown = sorted(named - known)
    if unknown:
        raise ManifestError(
            "the manifest names platform ids the installer tables do not define:  %s"
            % ", ".join(unknown)
        )


def validate_rules(manifest: Manifest) -> None:
    """Every rule is internally consistent, or ManifestError is raised.

    A rule must name the seat it checks, an owner seat, at least one platform, and a
    kind its check type actually supports.  A rule whose proof level is above L0 for a
    kind other than hooks is rejected, because no proof machinery exists for the other
    kinds and ``UNPROVEN`` would become their permanent state.

    A rule may only name platforms the seat entry lists:  a rule for a platform the
    seat does not have is a rule that would never run, and a silent one at that.
    An L2 rule needs a TTL of at least 86400 seconds, so a daily canary stays fresh
    between runs.
    """
    problems: list[str] = []
    for seat in manifest.seats:
        for rule in seat.rules:
            if not rule.seat.strip():
                problems.append("rule %s names no seat" % rule.id)
            if not rule.owner_seat.strip():
                problems.append("rule %s names no owner seat" % rule.id)
            if not rule.platforms:
                problems.append("rule %s names no platform" % rule.id)
            else:
                for platform in rule.platforms:
                    if platform not in seat.platforms:
                        problems.append(
                            "rule %s names platform %s, which seat %s does not list"
                            % (rule.id, platform, seat.seat)
                        )
            supported = CHECK_TYPES_BY_KIND[rule.kind]
            if rule.check.type not in supported:
                problems.append(
                    "rule %s is a %s rule, which the %s check type does not support"
                    % (rule.id, rule.kind.value, rule.check.type.value)
                )
            if rule.kind is not ConfigKind.HOOKS and rule.proof_level is not ProofLevel.L0:
                problems.append(
                    "rule %s asks for proof %s, but only a hooks rule may ask for more "
                    "than L0" % (rule.id, rule.proof_level.value)
                )
            if rule.proof_level is ProofLevel.L2 and rule.ttl_seconds < 86400:
                problems.append(
                    "rule %s asks for L2 proof, so its ttlSeconds must be at least 86400" % rule.id
                )
    if problems:
        raise ManifestError("the manifest holds inconsistent rules:  %s" % ";  ".join(problems))
