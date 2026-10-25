"""The shared vocabulary of ``fleet_config``:  states, proof levels and the result types.

Every other module in this package speaks in these types and in no others.  The design
contract lives in ``docs/plans/2026-10-19-fleet-config-design.md``;  this module is the part
of it that code imports.  Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.

The names here are fixed.  A check state, a proof level or a config kind is never renamed
to fit one platform, because the SwiftUI app, the contract tests and four parallel
implementers all key off these spellings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "CHECK_STATES",
    "COMPLIANCE_SCOPES",
    "CONFIG_KINDS",
    "SEAT_CONFIG_KINDS",
    "CheckResult",
    "CheckSpec",
    "CheckState",
    "CheckType",
    "Collector",
    "ConfigKind",
    "ExpectedState",
    "Manifest",
    "NotApplicable",
    "NotImplementedRule",
    "ObservedState",
    "OverallVerdict",
    "ProofLevel",
    "ReasonCode",
    "Rule",
    "SeatManifest",
    "StatusReport",
    "StatusSummary",
    "Threshold",
    "UncoveredSurface",
]


class CheckState(str, Enum):
    """The outcome of one check.  These six values are the whole vocabulary.

    ``UNKNOWN`` is a real outcome and never reads as healthy.  A stale, errored, timed out
    or unparseable check is reported as ``UNKNOWN`` with a failing reason code, never as
    ``PASS``;  see ``ReasonCode`` and contract.py ``is_failing``.
    """

    PASS = "PASS"
    DRIFT = "DRIFT"
    MISSING = "MISSING"
    UNPROVEN = "UNPROVEN"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


CHECK_STATES: tuple[CheckState, ...] = tuple(CheckState)


class ProofLevel(str, Enum):
    """How strongly a passing check was proved.  Ordered ``L0`` < ``L1`` < ``L2``.

    L0 is config present and matching by hash.  L1 pipes a synthetic PreToolUse payload
    that attempts a write into a sentinel lane into the installed hook, and the hook must
    deny it;  it runs on every status run.  L2 runs a headless platform canary in a
    throwaway lane to show the platform actually loads the hook;  it costs a model turn,
    so it runs only on demand or daily.
    """

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"

    @property
    def rank(self) -> int:
        """The ordering rank of this level, so levels compare without string tricks."""
        return _PROOF_RANK[self]

    def at_least(self, other: ProofLevel) -> bool:
        """True when this level proves at least as much as `other`."""
        return self.rank >= other.rank


_PROOF_RANK: dict[ProofLevel, int] = {ProofLevel.L0: 0, ProofLevel.L1: 1, ProofLevel.L2: 2}


class ConfigKind(str, Enum):
    """The config kinds the engine checks.

    Six of them are per-seat config surfaces:  the instruction file,  the hook
    registrations,  the MCP registrations,  the skills,  the sub-agents and the
    permissions.  The seventh, ``compliance``, is a fleet-wide rule:  it is about the
    fleet rather than about one seat's config surface (a certificate that expires,  a
    backup that stopped,  a secret that ended up in a flat key file).  A compliance rule
    lives in its own list in the manifest,  so it never takes part in a seat's coverage
    tuple.

    A new kind is a design change to the plan document and to this enum together, never
    a string invented inside one check module.
    """

    INSTRUCTIONS = "instructions"
    HOOKS = "hooks"
    MCP = "mcp"
    SKILLS = "skills"
    SUBAGENTS = "subagents"
    PERMISSIONS = "permissions"
    COMPLIANCE = "compliance"


CONFIG_KINDS: tuple[ConfigKind, ...] = tuple(ConfigKind)

# The kinds a seat's own config surface is checked on.  The compliance kind is
# fleet-wide,  so a seat that has no compliance rule is not uncovered:  coverage is a
# property of a seat's config surface,  and the compliance family carries its own list.
SEAT_CONFIG_KINDS: tuple[ConfigKind, ...] = tuple(
    kind for kind in ConfigKind if kind is not ConfigKind.COMPLIANCE
)

# The host scopes a fleet-wide compliance rule can be about.  A scope says which host
# the rule describes;  the collector says where the check runs.  They are usually the
# same word today,  but they are not the same fact.  ``fleet`` is the scope for a rule
# that is about the fleet wherever it runs,  such as a per-repo rule.
COMPLIANCE_SCOPES: tuple[str, ...] = ("mac", "vm", "hetzner", "cloud", "fleet")


class Collector(str, Enum):
    """Where a check runs.  Only ``mac`` is implemented in Phase 2.

    A rule whose collector is ``vm``, ``hetzner`` or ``api`` yields an ``UNSUPPORTED``
    result in Phase 2, with the reason saying so.  The values are reserved now so a later
    phase adds collectors without a schema change.
    """

    MAC = "mac"
    VM = "vm"
    HETZNER = "hetzner"
    API = "api"


class CheckType(str, Enum):
    """The check a rule asks for.  The kind-to-type table is pinned in the design doc.

    ``credential_stat`` is the only check that touches a credential file, and it reads
    ``stat`` data only (existence, mode, size, mtime).  It never opens the file.

    ``credential_key_present`` never touches a file at all:  it asks the fleet's
    ``fleet-secrets`` CLI for the KEY NAMES in one secret-store folder and reports which
    of the rule's names are there.  A key name is an identifier,  never a value,  so the
    check cannot carry a secret into a result even by accident.

    The three compliance check types serve the fleet-wide family.  ``command`` runs one
    allowlisted program and reads its exit code.  ``secrets_hygiene`` looks for a
    credential written as a flat key file and for an internal identifier inside a
    published file.  ``certificate_expiry`` reads the expiry of the Apple signing
    certificates the fleet uses.  None of them ever prints a secret value.
    """

    MARKED_BLOCK_HASH = "marked_block_hash"
    FILE_HASH = "file_hash"
    HOOK_REGISTERED = "hook_registered"
    MCP_SERVERS_REGISTERED = "mcp_servers_registered"
    SKILLS_INSTALLED = "skills_installed"
    SUBAGENTS_INSTALLED = "subagents_installed"
    PERMISSIONS_MATCH = "permissions_match"
    CREDENTIAL_STAT = "credential_stat"
    CREDENTIAL_KEY_PRESENT = "credential_key_present"
    COMMAND = "command"
    SECRETS_HYGIENE = "secrets_hygiene"
    CERTIFICATE_EXPIRY = "certificate_expiry"


class ReasonCode(str, Enum):
    """A short machine token that says why a result has its state.

    The human sentence lives in ``CheckResult.reason``;  this token is what the verdict
    math and the app key off.  ``STALE``, ``ERROR``, ``TIMEOUT`` and ``UNPARSEABLE`` make
    an ``UNKNOWN`` result failing (fail closed), while a bare ``UNKNOWN`` does not by
    itself prove drift.
    """

    OK = "ok"
    DRIFT = "drift"
    MISSING = "missing"
    STALE = "stale"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNPARSEABLE = "unparseable"
    PROOF_NOT_RUN = "proof-not-run"
    PROOF_FAILED = "proof-failed"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not-applicable"
    UNKNOWN = "unknown"


class OverallVerdict(str, Enum):
    """The top-level verdict of one status run.  ``PASS`` is the only healthy value."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExpectedState:
    """What a rule wants to find.  It never holds a secret value.

    ``names`` carries identifiers only (MCP server names, skill names, hook ids, secret
    store key names), and a credential expectation is a mode and a presence flag, never
    contents or a hash of contents.
    """

    description: str
    present: bool | None = None
    sha256: str | None = None
    names: tuple[str, ...] = ()
    mode: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class ObservedState:
    """What a check actually found.  It never holds a secret value.

    For a credential file this is ``stat`` output only:  ``present``, ``mode``,
    ``size_bytes`` and ``mtime``.  There is deliberately no field that could hold file
    contents, so a result cannot leak one by construction.
    """

    present: bool | None = None
    sha256: str | None = None
    names: tuple[str, ...] = ()
    mode: str | None = None
    size_bytes: int | None = None
    mtime: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class CheckSpec:
    """The check to run:  a type, a target and an optional detail.

    ``target`` is a path for file checks, a name (never a value) for a credential
    reference, and a secret-store folder path for a credential key check.  ``detail``
    names the part inside the target, such as a marked block name or a hook event.
    Per-kind meanings are pinned in the design doc, section 5.
    """

    type: CheckType
    target: str
    detail: str = ""


@dataclass(frozen=True)
class Threshold:
    """A number a compliance rule reads from a fleet knob,  never from a literal.

    ``knob`` is the name of a knob in ``scripts/fleet_knobs/registry.py``,  and the value
    comes from Infisical through that registry.  A threshold in the manifest is a pointer
    to a knob,  so the owner changes a number in one place and every rule that reads it
    moves together.

    ``minimum`` and ``maximum`` are the values the rule accepts.  A value outside them is
    not a threshold,  so the rule reads ``UNKNOWN`` rather than deciding on a number
    nobody meant.  ``unit`` names what the number counts,  such as days or findings.
    """

    knob: str
    minimum: int
    maximum: int
    unit: str = ""


@dataclass(frozen=True)
class Rule:
    """One desired-state rule, as loaded from the manifest.

    ``id`` is unique across the whole manifest.  ``owner_seat`` is the seat that owns the
    rule (it answers for drift), which may differ from the seat being checked;
    ``seat`` is the seat whose config is being checked, and the two are different
    fields on purpose.  ``autofix_allowed`` is the only route by which the apply side
    may change anything for this rule.  ``ttl_seconds`` bounds how long a result stays
    fresh.  ``source`` is the rule's provenance:  a standing directive plus date, or a
    Zulip topic message id that cites the seat reply the rule came from (AFC-384
    requires the review to cite MM's answers and every other seat's reply).  It is
    free text and never affects the verdict.

    A seat rule names the harness config surfaces it checks in ``platforms``.  A
    compliance rule names the hosts it is about in ``scope`` instead,  because a
    fleet-wide rule is not about one seat's config surface;  its ``seat`` is empty and
    its ``kind`` is ``compliance``.

    ``threshold`` is the knob a compliance rule reads its number from,  and is None for
    a rule with no threshold.  ``implemented`` says whether the engine has a check for
    this rule's check type:  a rule that is not implemented is a registry row that says
    so,  and it reads ``UNSUPPORTED`` rather than a pass.  ``implementation_reason``
    explains that,  and is required when ``implemented`` is false.
    """

    id: str
    kind: ConfigKind
    platforms: tuple[str, ...]
    check: CheckSpec
    expected: ExpectedState
    proof_level: ProofLevel
    owner_seat: str
    autofix_allowed: bool
    seat: str = ""
    collector: Collector = Collector.MAC
    ttl_seconds: int = 900
    description: str = ""
    source: str = ""
    scope: tuple[str, ...] = ()
    threshold: Threshold | None = None
    implemented: bool = True
    implementation_reason: str = ""


@dataclass(frozen=True)
class NotApplicable:
    """An explicit cover for a kind a seat or platform does not have, with its reason.

    An empty ``platforms`` tuple means every platform the seat entry lists.  Silence is
    never a cover:  the loader rejects a (seat, platform, kind) triple that neither a
    rule nor one of these entries covers.
    """

    kind: ConfigKind
    reason: str
    platforms: tuple[str, ...] = ()


@dataclass(frozen=True)
class UncoveredSurface:
    """A config surface that fits none of the six kinds,  so the app does not check it.

    A seat's channel configuration,  its identity file,  or any other file that is not
    instructions,  hooks,  MCP,  skills,  sub-agents or permissions has no kind,  so it
    has no rule and no cover.  Naming it here makes that blindness visible:  the
    envelope carries the surface and the reason,  so the app can say "not checked"
    instead of leaving a hole where a check would have been.

    ``seat`` is empty in a manifest entry,  because the manifest already knows the seat
    the entry hangs off,  and it is filled in by the envelope,  which is one flat list.
    """

    surface: str
    reason: str
    seat: str = ""


@dataclass(frozen=True)
class SeatManifest:
    """One seat's desired state:  its platforms, its rules and its explicit covers."""

    seat: str
    platforms: tuple[str, ...]
    rules: tuple[Rule, ...] = ()
    not_applicable: tuple[NotApplicable, ...] = ()
    uncovered: tuple[UncoveredSurface, ...] = ()


@dataclass(frozen=True)
class Manifest:
    """A whole loaded manifest.  ``seats`` is in manifest order, keyed by registry tag.

    ``compliance_rules`` holds the fleet-wide family:  the rules that are about the
    fleet rather than about one seat's config surface.  The list is empty for a manifest
    that declares none,  which is what every manifest before this family looked like.
    """

    schema_version: int
    seats: tuple[SeatManifest, ...]
    source_path: str | None = None
    compliance_rules: tuple[Rule, ...] = ()

    def seat(self, tag: str) -> SeatManifest | None:
        """The entry for registry tag `tag`, or None.  Tags are compared exactly."""
        for entry in self.seats:
            if entry.seat == tag:
                return entry
        return None


@dataclass(frozen=True)
class CheckResult:
    """One checked rule on one platform, ready for the JSON envelope.

    ``checked_at`` and ``expires_at`` are ISO 8601 timezone.utc stamps with a ``Z`` suffix.
    ``stale`` is true when the result is past ``ttl_seconds`` at read time;  a stale
    result is re-labelled ``UNKNOWN`` with reason code ``STALE`` by the aggregator
    before it reaches the envelope, so this field is evidence, not the verdict.
    """

    rule_id: str
    seat: str
    platform: str
    kind: ConfigKind
    state: CheckState
    reason: str
    reason_code: ReasonCode
    expected: ExpectedState
    observed: ObservedState
    proof_level_required: ProofLevel
    proof_level_achieved: ProofLevel | None
    collector: Collector
    check_type: CheckType
    checked_at: str
    expires_at: str
    ttl_seconds: int
    stale: bool = False


@dataclass(frozen=True)
class StatusSummary:
    """Counts for one status run.  ``by_state`` always carries all six states."""

    total: int
    by_state: dict[CheckState, int]
    overall_verdict: OverallVerdict


@dataclass(frozen=True)
class NotImplementedRule:
    """A rule the registry declares and the engine cannot run yet.

    It is named here so the report can say what is missing without producing a result for
    it.  A rule that cannot run must not make the run's verdict unknown forever,  and it
    must not disappear either:  the fleet has to see the gap.

    ``owner_seat`` is the seat that answers for the gap,  and ``reason`` says why the
    rule cannot run yet.
    """

    rule_id: str
    owner_seat: str
    reason: str


@dataclass(frozen=True)
class StatusReport:
    """One whole status run, before the envelope builder renders it as JSON."""

    schema_version: int
    generated_at: str
    results: tuple[CheckResult, ...]
    summary: StatusSummary
    manifest_source: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    uncovered: tuple[UncoveredSurface, ...] = field(default_factory=tuple)
    not_implemented: tuple[NotImplementedRule, ...] = field(default_factory=tuple)
