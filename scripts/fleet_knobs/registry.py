"""The knob registry:  the one list of every Infisical knob the open-loops jobs may read.

A name that is not here cannot be fetched, so a job cannot ask Infisical for an arbitrary
secret by name.  Each entry carries what the loader and the narrowing rules need:

  kind      str, int, bool, enum, list or caps (a mapping of names to counts)
  default   what a job gets when Infisical has no value, or cannot be reached and no
            last-known-good copy exists.  It is the value the component's first live run uses.
  secret    a secret is never written to disk and never defaulted:  an unreachable or unset
            secret fails closed (SecretUnavailable)
  choices   enum only, MOST CONSERVATIVE FIRST.  The order is what "narrower" means
  safe      bool only:  the conservative value (a local file can force it, never undo it)
  narrower  int only:  "min" when a lower number is narrower (a cap), "max" when a higher
            number is (idle hours before a commit)
  local     "narrow" (a local file or STOP file may make the knob narrower) or "deny" (no
            local override at all:  an owner id or a launch timestamp)

INFISICAL.md carries one row per entry.  ``python3 -m fleet_knobs inventory --check
../INFISICAL.md`` (from ``scripts/``) fails when the two drift, and the unit tests run the same check.

To add a knob:  add an entry here, then run ``python3 -m fleet_knobs inventory --write
../INFISICAL.md`` from ``scripts/`` and commit both.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

KINDS = ("str", "int", "bool", "enum", "list", "caps")

# Linear user ids are UUIDs.  The launch timestamp is ISO 8601 with an offset or Z.
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
ISO_PATTERN = (r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
# 32 random bytes as hex, base64 or urlsafe base64.
SIGNING_KEY_PATTERN = r"^[A-Za-z0-9+/=_-]{32,256}$"


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str
    owner: str
    default: Any = None
    secret: bool = False
    choices: Tuple[str, ...] = ()
    safe: Optional[bool] = None
    narrower: str = "min"
    local: str = "narrow"
    minimum: int = 0
    maximum: Optional[int] = None
    pattern: Optional[str] = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: unknown kind {self.kind!r}")
        if self.kind == "enum" and (not self.choices or self.default not in self.choices):
            raise ValueError(f"{self.name}: an enum needs choices and a default among them")
        if self.kind == "bool" and (not isinstance(self.default, bool)
                                    or not isinstance(self.safe, bool)):
            raise ValueError(f"{self.name}: a bool needs a bool default and a bool safe value")
        if self.narrower not in ("min", "max"):
            raise ValueError(f"{self.name}: narrower must be min or max")
        if self.local not in ("narrow", "deny"):
            raise ValueError(f"{self.name}: local must be narrow or deny")
        if self.secret and (self.default not in (None, "") or self.kind != "str"):
            raise ValueError(f"{self.name}: a secret is a str with no default")


_S = Spec

REGISTRY: Tuple[Spec, ...] = (
    # C1 fleet-autopush
    _S("FLEET_AUTOPUSH_MODE", "enum", "C1", default="off",
       choices=("off", "report", "snapshot", "full"),
       note="what the autopush hooks may do;  full also needs FLEET_AUTOPUSH_IDLE_COMMIT"),
    _S("FLEET_AUTOPUSH_REPOS", "list", "C1", default=(),
       note="allowlist of owner/repo names the hooks may touch"),
    _S("FLEET_AUTOPUSH_IDLE_COMMIT", "bool", "C1", default=False, safe=False,
       note="true lets the idle backstop commit on its own (mode full only)"),
    _S("FLEET_AUTOPUSH_IDLE_HOURS", "int", "C1", default=2, narrower="max", minimum=1,
       maximum=168, note="hours a lane sits unchanged before the backstop sweep acts"),
    _S("FLEET_AUTOPUSH_PUBLIC_DRAFTS", "list", "C1", default=(),
       note="public repos where a draft PR may open;  empty means none"),
    # F7 fleet-facts publisher
    _S("FLEET_FACTS_SIGNING_KEY", "str", "F7", secret=True, pattern=SIGNING_KEY_PATTERN,
       note="HMAC key for the signed facts files;  never cached, a job without it publishes nothing"),
    _S("FLEET_FACTS_PUBLISH_FULL_COPY", "bool", "F7", default=False, safe=False,
       note="true also publishes the full mac-facts.json, which keeps lane paths and is readable by everything that can "
            "read fleet-loops-data;  false (the default) publishes only the path-free gb/ copy and the full list"),
    _S("FLEET_FACTS_PUBLISH_CAP_MINUTES", "int", "F7", default=20, narrower="min", minimum=1, maximum=60,
       note="minutes a publish run may take before it ends itself and its child processes"),
    _S("FLEET_FACTS_PUBLISH_MAX_LOAD1", "int", "F7", default=250, narrower="min", minimum=1, maximum=1000,
       note="a publish run is skipped when the one-minute load average is over this"),
    _S("FLEET_LOOPS_DATA_ALLOWED_APPS", "list", "F7", default=(), local="deny",
       note="app slugs the owner accepts as able to read the private fleet-loops-data repository;  "
            "empty means the publisher refuses while any installed app can read it"),
    # C6 needs-owner
    _S("NEEDS_OWNER_NOTIFY_PAUSED", "bool", "C6", default=False, safe=True,
       note="true pauses the fallback notifier;  an Infisical outage past the 48 hour copy "
            "limit reads false and unpauses it"),
    _S("NEEDS_OWNER_CAPS", "caps", "C6",
       default={"urgent_per_hour": 3, "per_hour": 6, "per_day": 30},
       note="notifier post caps"),
    _S("OWNER_LINEAR_USER_ID", "str", "C6", default="", local="deny", pattern=UUID_PATTERN,
       note="the owner's Linear user id;  empty means no Linear reply is accepted as the owner's"),
    # C4 claim-expiry
    _S("CLAIM_EXPIRY_MODE", "enum", "C4", default="dry-run",
       choices=("dry-run", "ping-only", "act"),
       note="act also needs CLAIM_EXPIRY_LAUNCH_AT"),
    _S("CLAIM_EXPIRY_CAPS", "caps", "C4",
       default={"releases_per_run": 20, "releases_per_day": 40, "pings_per_run": 40},
       note="code ceilings are the same numbers;  Infisical can lower them"),
    _S("CLAIM_EXPIRY_LAUNCH_AT", "str", "C4", default="", local="deny", pattern=ISO_PATTERN,
       note="ISO 8601 time the claim clock starts, set once;  empty means the clock has not started"),
    # C3 ask-capture
    _S("ASK_CAPTURE_ENABLED", "bool", "C3", default=False, safe=False,
       note="false turns the capture hooks and the drain off"),
    _S("ASK_CAPTURE_SHADOW", "bool", "C3", default=True, safe=True,
       note="true appends would-file rows for review and never calls board"),
    _S("ASK_CAPTURE_CAPS", "caps", "C3",
       default={"per_session": 5, "per_seat_day": 30, "model_calls_day": 80},
       note="extractor and filing caps"),
    # C5 lane sweep
    _S("LANE_SWEEP_MODE", "enum", "C5", default="report",
       choices=("report", "act", "retire"),
       note="act arms merges and retires merged-PR lanes;  retire adds the Phase 3 deletions"),
    _S("LANE_SWEEP_CAPS", "caps", "C5",
       default={"lane_retires_per_run": 10, "report_posts_per_run": 12},
       note="sweep action caps"),
    _S("LANE_SWEEP_OPTIN_REPOS", "list", "C5", default=(),
       note="money-path repos the sweep may act on;  empty means none"),
    # C7 consult
    _S("CONSULT_ENABLED", "bool", "C7", default=False, safe=False,
       note="false refuses every consult verb"),
)

BY_NAME: Dict[str, Spec] = {s.name: s for s in REGISTRY}
assert len(BY_NAME) == len(REGISTRY), "duplicate knob name in REGISTRY"


def spec_for(name: str) -> Spec:
    """The registry entry for `name`, or KeyError.  Callers wrap it into KnobError."""
    return BY_NAME[name]
