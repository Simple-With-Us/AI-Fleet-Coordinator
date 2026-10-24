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
    COMPLIANCE_SCOPES,
    SEAT_CONFIG_KINDS,
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
    Threshold,
    UncoveredSurface,
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
    "validate_compliance_rules",
    "validate_coverage",
    "validate_platforms",
    "validate_rules",
]

# The pinned manifest version, checked exactly.  A breaking manifest change raises it.
#  Version 2 adds the optional ``source`` provenance field on every rule (AFC-416):
#  a standing directive plus date, or a Zulip topic message id.  Version 1 documents
#  without ``source`` are refused, so a stale manifest cannot silently lose provenance.
#
#  The fleet-wide compliance family (AFC-400) did NOT raise it:  it arrives as an
#  ADDITIVE top-level key,  ``complianceRules``,  and a reader that does not know the key
#  ignores it,  so the app keeps reading version 2.  A version moves when a reader has to
#  be told;  an ignored key does not need one.
MANIFEST_SCHEMA_VERSION = 2
MANIFEST_FILENAME = "fleet-seats.json"
REGISTRY_FILENAME = "fleet-apps.json"

# The top-level key the fleet-wide compliance family lives under.
COMPLIANCE_RULES_KEY = "complianceRules"

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
    ConfigKind.COMPLIANCE: (
        CheckType.COMMAND,
        CheckType.SECRETS_HYGIENE,
        CheckType.CERTIFICATE_EXPIRY,
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


def _parse_threshold(raw: object, rule_id: str) -> Threshold | None:
    """The threshold object of a rule, or None when the rule has no threshold.

    A threshold names a knob and the range of values the rule accepts.  The number itself
    is never in the manifest:  it comes from Infisical through the knob registry.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("rule %s holds a threshold that is not an object" % rule_id)
    knob = raw.get("knob")
    if not isinstance(knob, str) or not knob.strip():
        raise ManifestError("rule %s holds a threshold without a knob name" % rule_id)
    minimum = _int_field(raw.get("minimum"), None, "rule %s threshold minimum" % rule_id)
    maximum = _int_field(raw.get("maximum"), None, "rule %s threshold maximum" % rule_id)
    if minimum is None or maximum is None:
        raise ManifestError(
            "rule %s holds a threshold without a minimum and a maximum" % rule_id
        )
    return Threshold(
        knob=knob.strip(),
        minimum=minimum,
        maximum=maximum,
        unit=str(raw.get("unit") or ""),
    )


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
    implemented_raw = raw.get("implemented")
    if implemented_raw is not None and not isinstance(implemented_raw, bool):
        raise ManifestError("rule %s holds an implemented flag that is not true or false" % rule_id)
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
            timeout_seconds=_int_field(
                expected_raw.get("timeoutSeconds"),
                None,
                "rule %s expected timeoutSeconds" % rule_id,
            ),
        ),
        proof_level=_enum(ProofLevel, raw.get("proofLevel"), "rule %s proofLevel" % rule_id),
        owner_seat=str(raw.get("ownerSeat") or ""),
        autofix_allowed=bool(raw.get("autofixAllowed")),
        seat=seat,
        collector=_enum(Collector, raw.get("collector") or "mac", "rule %s collector" % rule_id),
        ttl_seconds=_int_field(raw.get("ttlSeconds"), 900, "rule %s ttlSeconds" % rule_id),
        description=str(raw.get("description") or ""),
        source=str(raw.get("source") or ""),
        scope=_str_tuple(raw.get("scope"), "rule %s scope" % rule_id),
        threshold=_parse_threshold(raw.get("threshold"), rule_id),
        implemented=True if implemented_raw is None else implemented_raw,
        implementation_reason=str(raw.get("implementationReason") or ""),
    )


def _int_field(value: object, default: int | None, what: str) -> int | None:
    """An integer field from the manifest, or ManifestError naming the field.

    ``int()`` on a non-numeric string raises a bare ``ValueError``, and
    ``ManifestError`` is a SUBCLASS of ``ValueError``, so a caller that catches
    ``ManifestError`` never catches it.  The CLI would then die with a traceback on a
    manifest with ``"ttlSeconds": "abc"``, and a consumer reads the exit code as a
    failing verdict rather than the refused run a bad manifest is.  Every integer field
    goes through here.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ManifestError("%s must be a number, not %r" % (what, value))
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            raise ManifestError("%s must be a number, not %r" % (what, value)) from None
    raise ManifestError("%s must be a number, not %r" % (what, value))


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
    uncovered: list[UncoveredSurface] = []
    for item in raw.get("uncovered") or []:
        if not isinstance(item, dict):
            raise ManifestError("seat %s holds an uncovered entry that is not an object" % tag)
        surface = item.get("surface")
        if not isinstance(surface, str) or not surface.strip():
            raise ManifestError("seat %s holds an uncovered entry without a surface" % tag)
        uncovered.append(
            UncoveredSurface(
                surface=surface.strip(),
                reason=str(item.get("reason") or ""),
            )
        )
    return SeatManifest(
        seat=tag,
        platforms=_str_tuple(raw.get("platforms"), "seat %s platforms" % tag),
        rules=rules,
        not_applicable=tuple(covers),
        uncovered=tuple(uncovered),
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
    compliance = _parse_compliance_rules(data.get(COMPLIANCE_RULES_KEY))
    seen: dict[str, str] = {}
    for seat in seats:
        for rule in seat.rules:
            if rule.id in seen:
                raise ManifestError(
                    "duplicate rule id %s (seats %s and %s)" % (rule.id, seen[rule.id], seat.seat)
                )
            seen[rule.id] = seat.seat
    for rule in compliance:
        if rule.id in seen:
            raise ManifestError(
                "duplicate rule id %s (seats %s and %s)"
                % (rule.id, seen[rule.id], "the compliance family")
            )
        seen[rule.id] = "the compliance family"
        if rule.owner_seat and rule.owner_seat not in live:
            raise ManifestError(
                "compliance rule %s names the owner seat %s, which the registry does not hold "
                "live" % (rule.id, rule.owner_seat)
            )
    return Manifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        seats=seats,
        source_path=str(target),
        compliance_rules=compliance,
    )


def _parse_compliance_rules(raw: object) -> tuple[Rule, ...]:
    """The fleet-wide compliance family, parsed into rules.

    The key is optional:  a manifest that declares none is a manifest from before this
    family existed,  and it loads with an empty list.  The rules carry no seat,  because
    a compliance rule is about the fleet and not about one seat's config surface.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("the manifest %s must be a list of rules" % COMPLIANCE_RULES_KEY)
    rules: list[Rule] = []
    for item in raw:
        rule = _parse_rule(item, "")
        if rule.kind is not ConfigKind.COMPLIANCE:
            raise ManifestError(
                "compliance rule %s has kind %s, and only %s is a compliance kind"
                % (rule.id, rule.kind.value, ConfigKind.COMPLIANCE.value)
            )
        rules.append(rule)
    return tuple(rules)


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
            for kind in SEAT_CONFIG_KINDS:
                if not any(
                    cover.kind is kind and not cover.platforms for cover in seat.not_applicable
                ):
                    problems.append(
                        "seat %s lists no platform and has no seat-wide not-applicable entry for %s"
                        % (seat.seat, kind.value)
                    )
            continue
        for platform in seat.platforms:
            for kind in SEAT_CONFIG_KINDS:
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


def validate_compliance_rules(manifest: Manifest) -> None:
    """Every fleet-wide rule is internally consistent, or ManifestError is raised.

    A compliance rule must name an owner seat,  at least one scope,  a check,  and a check
    type its kind supports.  It must ask for no proof above L0,  because the proof
    machinery is about a seat's hook and a fleet rule has no hook.  It must not allow an
    automatic fix,  because no installer writes a fleet rule and a permission nobody can
    use is a promise the apply side cannot keep.

    A rule that is not implemented must say why.  Silence there would be a gap dressed
    as a rule,  and the registry is where the fleet looks to see what is still missing.

    A rule with a threshold must name a knob and a real range.  The knob's existence in
    the fleet's registry is checked by the test suite,  which may read that registry;  the
    engine itself never imports it,  so the isolated copy keeps working.
    """
    problems: list[str] = []
    for rule in manifest.compliance_rules:
        if not rule.owner_seat.strip():
            problems.append("compliance rule %s names no owner seat" % rule.id)
        if not rule.scope:
            problems.append("compliance rule %s names no scope" % rule.id)
        else:
            for scope in rule.scope:
                if scope not in COMPLIANCE_SCOPES:
                    problems.append(
                        "compliance rule %s names the scope %s, which is not a known host scope"
                        % (rule.id, scope)
                    )
        if rule.platforms:
            problems.append(
                "compliance rule %s names platforms,  and a fleet rule is about a host,  not "
                "about a harness config surface" % rule.id
            )
        if rule.seat.strip():
            problems.append(
                "compliance rule %s names a seat,  and a fleet rule is about no one seat"
                % rule.id
            )
        supported = CHECK_TYPES_BY_KIND[ConfigKind.COMPLIANCE]
        if rule.check.type not in supported:
            problems.append(
                "compliance rule %s asks for the %s check,  which a compliance rule does not "
                "support" % (rule.id, rule.check.type.value)
            )
        if rule.proof_level is not ProofLevel.L0:
            problems.append(
                "compliance rule %s asks for proof %s,  but the proof machinery is about a "
                "seat's hook" % (rule.id, rule.proof_level.value)
            )
        if rule.autofix_allowed:
            problems.append(
                "compliance rule %s allows an automatic fix,  and no installer writes a fleet "
                "rule" % rule.id
            )
        if rule.ttl_seconds <= 0:
            problems.append("compliance rule %s has a ttlSeconds of zero" % rule.id)
        if not rule.implemented and not rule.implementation_reason.strip():
            problems.append(
                "compliance rule %s is not implemented,  and says no reason why" % rule.id
            )
        if rule.threshold is not None:
            if not rule.threshold.knob.strip():
                problems.append("compliance rule %s holds a threshold without a knob" % rule.id)
            if rule.threshold.minimum > rule.threshold.maximum:
                problems.append(
                    "compliance rule %s holds a threshold whose minimum is above its maximum"
                    % rule.id
                )
    if problems:
        raise ManifestError(
            "the manifest holds inconsistent compliance rules:  %s" % ";  ".join(problems)
        )


def validate_rules(manifest: Manifest) -> None:
    """Every seat rule is internally consistent, or ManifestError is raised.

    A rule must name the seat it checks, an owner seat, at least one platform, and a
    kind its check type actually supports.  A rule whose proof level is above L0 for a
    kind other than hooks is rejected, because no proof machinery exists for the other
    kinds and ``UNPROVEN`` would become their permanent state.

    A rule may only name platforms the seat entry lists:  a rule for a platform the
    seat does not have is a rule that would never run, and a silent one at that.
    An L2 rule needs a TTL of at least 86400 seconds, so a daily canary stays fresh
    between runs.

    The fleet-wide compliance family has its own validator,  ``validate_compliance_rules``,
    because a fleet rule is about a host and not about a seat's config surface.
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
            if rule.scope:
                problems.append(
                    "rule %s names a scope, and only a fleet-wide compliance rule has one"
                    % rule.id
                )
            if rule.threshold is not None:
                problems.append(
                    "rule %s holds a threshold, and only a fleet-wide compliance rule reads a knob"
                    % rule.id
                )
    if problems:
        raise ManifestError("the manifest holds inconsistent rules:  %s" % ";  ".join(problems))
