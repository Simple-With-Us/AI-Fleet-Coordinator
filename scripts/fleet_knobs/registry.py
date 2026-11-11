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

What the owner reads in Muster's Controls tab comes from the same entry:

  group     the plain section the knob sits in (Autopush, Lane Sweep, ...)
  label     the knob's plain name, in Title Case
  hosts     which machines run a job that reads it:  mac, server, both, or unknown when
            nothing in this repository reads it
  consumers (plain job name, repository path that names the knob) for each reader found
            here;  empty exactly when hosts is unknown, and a test checks every path
  requires  (value, other knob):  the value does less than its name says until the other
            knob is set
  consequences  (value, sentence):  what a risky value makes the fleet do
  unit, step    int only:  what the number counts, and the stepper's step
  format, example  str with a pattern:  the format in words, and one value it accepts
  once      the value is meant to be set once (a launch time)
  requires_explicit  the reader treats the default as "not set" (a compliance threshold)

INFISICAL.md carries one row per entry, and ``registry.json`` beside this file carries every
field for Muster.  ``python3 -m fleet_knobs inventory --check ../INFISICAL.md`` and
``--check-json fleet_knobs/registry.json`` (from ``scripts/``) fail when either drifts, and the
unit tests run the same checks.

To add a knob:  add an entry here, then run ``python3 -m fleet_knobs inventory --write
../INFISICAL.md`` and ``python3 -m fleet_knobs inventory --write-json fleet_knobs/registry.json``
from ``scripts/`` and commit all three.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KINDS = ("str", "int", "bool", "enum", "list", "caps")
HOSTS = ("mac", "server", "both", "unknown")

# Linear user ids are UUIDs.  The launch timestamp is ISO 8601 with an offset or Z.
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
ISO_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$"
# A seat tag:  CLAUDE, GROK-WEB, BF-DEPLOYER.
SEAT_PATTERN = r"^[A-Z0-9][A-Z0-9-]{0,31}$"
# 32 random bytes as hex, base64 or urlsafe base64.
SIGNING_KEY_PATTERN = r"^[A-Za-z0-9+/=_-]{32,256}$"


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str
    owner: str
    default: Any = None
    secret: bool = False
    choices: tuple[str, ...] = ()
    safe: bool | None = None
    narrower: str = "min"
    local: str = "narrow"
    minimum: int = 0
    maximum: int | None = None
    pattern: str | None = None
    note: str = ""
    group: str = ""
    label: str = ""
    hosts: str = "unknown"
    consumers: tuple[tuple[str, str], ...] = ()
    requires: tuple[tuple[str, str], ...] = ()
    consequences: tuple[tuple[str, str], ...] = ()
    unit: str = ""
    step: int = 1
    format: str = ""
    example: str = ""
    once: bool = False
    requires_explicit: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: unknown kind {self.kind!r}")
        if self.kind == "enum" and (not self.choices or self.default not in self.choices):
            raise ValueError(f"{self.name}: an enum needs choices and a default among them")
        if self.kind == "bool" and (
            not isinstance(self.default, bool) or not isinstance(self.safe, bool)
        ):
            raise ValueError(f"{self.name}: a bool needs a bool default and a bool safe value")
        if self.narrower not in ("min", "max"):
            raise ValueError(f"{self.name}: narrower must be min or max")
        if self.local not in ("narrow", "deny"):
            raise ValueError(f"{self.name}: local must be narrow or deny")
        if self.secret and (self.default not in (None, "") or self.kind != "str"):
            raise ValueError(f"{self.name}: a secret is a str with no default")
        if self.hosts not in HOSTS:
            raise ValueError(f"{self.name}: hosts must be one of {', '.join(HOSTS)}")
        if (self.hosts == "unknown") != (not self.consumers):
            raise ValueError(
                f"{self.name}: a knob names its readers exactly when its hosts are known"
            )
        if self.step < 1:
            raise ValueError(f"{self.name}: step must be at least 1")
        for value, _ in self.requires + self.consequences:
            if self.kind == "enum" and value not in self.choices:
                raise ValueError(f"{self.name}: {value!r} is not one of its choices")
            if self.kind == "bool" and value not in ("true", "false"):
                raise ValueError(f"{self.name}: a bool's value is true or false")


_S = Spec

# The readers this repository holds, named once so every knob of a job says the same thing.
_NOTIFIER = (("the owner notifier", "scripts/needs_owner/notifier.py"),)
_BAR = (("the needs-owner filing command", "scripts/needs_owner/bar.py"),)
_PUBLISHER = (("the facts publisher", "scripts/fleet_facts/publish.py"),)
_SIGNER = (("the facts publisher", "scripts/fleet_facts/signing.py"),)
_NOTES = (
    ("simple-notes, the stdio MCP and the notes runner", "scripts/simple_tools/notes_client.py"),
)
_PIN = (("the Apple Notes helper", "scripts/apple-notes-coding.sh"),)
_SHEET = (("the reference sheet sync", "scripts/fleet-apps-sheet-sync.mjs"),)
_COMPLIANCE = (("the fleet-config compliance rules", "scripts/fleet_config/seed_manifest.py"),)
_WATCHDOG = (("the Cloudflare usage watchdog", "scripts/cf-usage-watchdog/watchdog.py"),)
_WAKE = (("the wake router", "scripts/simple_tools/wake_settings.py"),)

REGISTRY: tuple[Spec, ...] = (
    # C1 fleet-autopush
    _S(
        "FLEET_AUTOPUSH_MODE",
        "enum",
        "C1",
        default="off",
        choices=("off", "report", "snapshot", "full"),
        note="what the autopush hooks may do;  full also needs FLEET_AUTOPUSH_IDLE_COMMIT",
        group="Autopush",
        label="Autopush Mode",
        requires=(("full", "FLEET_AUTOPUSH_IDLE_COMMIT"),),
    ),
    _S(
        "FLEET_AUTOPUSH_REPOS",
        "list",
        "C1",
        default=(),
        note="allowlist of owner/repo names the hooks may touch",
        group="Autopush",
        label="Repositories Autopush May Touch",
    ),
    _S(
        "FLEET_AUTOPUSH_IDLE_COMMIT",
        "bool",
        "C1",
        default=False,
        safe=False,
        note="true lets the idle backstop commit on its own (mode full only)",
        group="Autopush",
        label="Idle Backstop May Commit",
        consequences=(("true", "The idle backstop commits on its own when the mode is full."),),
    ),
    _S(
        "FLEET_AUTOPUSH_IDLE_HOURS",
        "int",
        "C1",
        default=2,
        narrower="max",
        minimum=1,
        maximum=168,
        note="hours a lane sits unchanged before the backstop sweep acts",
        group="Autopush",
        label="Idle Hours Before The Backstop",
        unit="hours",
    ),
    _S(
        "FLEET_AUTOPUSH_PUBLIC_DRAFTS",
        "list",
        "C1",
        default=(),
        note="public repos where a draft PR may open;  empty means none",
        group="Autopush",
        label="Public Repositories For Draft PRs",
    ),
    # F7 fleet-facts publisher
    _S(
        "FLEET_FACTS_SIGNING_KEY",
        "str",
        "F7",
        secret=True,
        pattern=SIGNING_KEY_PATTERN,
        note="HMAC key for the signed facts files;  never cached, a job without it publishes nothing",
        group="Facts Publisher",
        label="Facts Signing Key",
        hosts="mac",
        consumers=_SIGNER,
    ),
    _S(
        "FLEET_FACTS_PUBLISH_FULL_COPY",
        "bool",
        "F7",
        default=False,
        safe=False,
        note="true also publishes the full mac-facts.json, which keeps lane paths and is readable by everything that can "
        "read fleet-loops-data;  false (the default) publishes only the path-free gb/ copy and the full list",
        group="Facts Publisher",
        label="Publish The Full Facts Copy",
        hosts="mac",
        consumers=_PUBLISHER,
        consequences=(
            (
                "true",
                "The full mac-facts.json is published, with lane paths, to everything that can read "
                "fleet-loops-data.",
            ),
        ),
    ),
    _S(
        "FLEET_FACTS_PUBLISH_CAP_MINUTES",
        "int",
        "F7",
        default=20,
        narrower="min",
        minimum=10,
        maximum=60,
        note="minutes a publish run may take before it ends itself and its child processes;  10 is the least that leaves "
        "room for the gatherers (5 minutes are reserved) and the writes",
        group="Facts Publisher",
        label="Publish Time Cap",
        hosts="mac",
        consumers=_PUBLISHER,
        unit="minutes",
        step=5,
    ),
    _S(
        "FLEET_FACTS_PUBLISH_MAX_LOAD1",
        "int",
        "F7",
        default=250,
        narrower="min",
        minimum=1,
        maximum=1000,
        note="a publish run is skipped when the one-minute load average is over this",
        group="Facts Publisher",
        label="Publish Load Limit",
        hosts="mac",
        consumers=_PUBLISHER,
        unit="load average",
        step=10,
    ),
    _S(
        "FLEET_LOOPS_DATA_ALLOWED_APPS",
        "list",
        "F7",
        default=(),
        local="deny",
        note="app slugs the owner accepts as able to read the private fleet-loops-data repository;  "
        "empty means the publisher refuses while any installed app can read it",
        group="Facts Publisher",
        label="Apps Allowed To Read Fleet-Loops-Data",
        hosts="mac",
        consumers=_PUBLISHER,
    ),
    # C6 needs-owner
    _S(
        "NEEDS_OWNER_NOTIFY_PAUSED",
        "bool",
        "C6",
        default=False,
        safe=True,
        note="true pauses the fallback notifier;  an Infisical outage past the 48 hour copy "
        "limit reads false and unpauses it",
        group="Owner Notifier",
        label="Pause The Owner Notifier",
        hosts="mac",
        consumers=_NOTIFIER,
        consequences=(
            ("false", "The fallback notifier posts needs-owner notices again."),
            ("true", "The fallback notifier stops posting until this is set back to false."),
        ),
    ),
    _S(
        "NEEDS_OWNER_CAPS",
        "caps",
        "C6",
        default={"urgent_per_hour": 3, "per_hour": 6, "per_day": 30},
        note="notifier post caps",
        group="Owner Notifier",
        label="Owner Notifier Post Caps",
        hosts="mac",
        consumers=_NOTIFIER,
    ),
    _S(
        "OWNER_LINEAR_USER_ID",
        "str",
        "C6",
        default="",
        local="deny",
        pattern=UUID_PATTERN,
        note="the owner's Linear user id;  empty means no Linear reply is accepted as the owner's",
        group="Owner Notifier",
        label="Owner's Linear User Id",
        hosts="mac",
        consumers=_NOTIFIER,
        format="a Linear user id, which is a UUID",
        example="00000000-0000-4000-8000-000000000000",
    ),
    # C6 the owner bar:  what may reach Jay through `needs-owner file` (docs/protocols/needs-owner.md, What May Reach Jay)
    _S(
        "NEEDS_OWNER_BAR",
        "enum",
        "C6",
        default="off",
        choices=("off", "warn", "enforce"),
        local="deny",
        note="how hard the bar is on `needs-owner file`;  off changes nothing, warn files the item and warns when "
        "--why-owner is missing, enforce refuses a filing with no reason class;  an unreadable or unknown value is off",
        group="Owner Bar",
        label="Owner Bar Mode",
        hosts="both",
        consumers=_BAR,
        consequences=(
            (
                "warn",
                "A needs-owner filing with no reason class still goes through,  with one warning line to the seat.",
            ),
            (
                "enforce",
                "A needs-owner filing with no reason class is refused,  so the seat decides it,  asks a peer in "
                "Side-Chats or posts to Pending-for-Jay.",
            ),
        ),
    ),
    _S(
        "NEEDS_OWNER_SEAT_DAILY_MAX",
        "int",
        "C6",
        default=0,
        narrower="min",
        local="deny",
        minimum=0,
        maximum=50,
        note="most needs-owner items one seat may file in 24 hours, counted on the host that files them;  0 means no cap;  "
        "applies in warn (a warning) and enforce (a refusal) and never in off",
        group="Owner Bar",
        label="Owner Bar Daily Filing Cap",
        hosts="both",
        consumers=_BAR,
        unit="filings a day",
    ),
    # C4 claim-expiry
    _S(
        "CLAIM_EXPIRY_MODE",
        "enum",
        "C4",
        default="dry-run",
        choices=("dry-run", "ping-only", "act"),
        note="act also needs CLAIM_EXPIRY_LAUNCH_AT",
        group="Claim Expiry",
        label="Claim Expiry Mode",
        requires=(("act", "CLAIM_EXPIRY_LAUNCH_AT"),),
    ),
    _S(
        "CLAIM_EXPIRY_CAPS",
        "caps",
        "C4",
        default={"releases_per_run": 20, "releases_per_day": 40, "pings_per_run": 40},
        note="code ceilings are the same numbers;  Infisical can lower them",
        group="Claim Expiry",
        label="Claim Expiry Caps",
    ),
    _S(
        "CLAIM_EXPIRY_LAUNCH_AT",
        "str",
        "C4",
        default="",
        local="deny",
        pattern=ISO_PATTERN,
        note="ISO 8601 time the claim clock starts, set once;  empty means the clock has not started",
        group="Claim Expiry",
        label="Claim Clock Start",
        format="an ISO 8601 time with its offset",
        example="2026-11-05T09:00:00-06:00",
        once=True,
    ),
    # C3 ask-capture
    _S(
        "ASK_CAPTURE_ENABLED",
        "bool",
        "C3",
        default=False,
        safe=False,
        note="false turns the capture hooks and the drain off",
        group="Ask Capture",
        label="Ask Capture On",
    ),
    _S(
        "ASK_CAPTURE_SHADOW",
        "bool",
        "C3",
        default=True,
        safe=True,
        note="true appends would-file rows for review and never calls board",
        group="Ask Capture",
        label="Ask Capture Shadow Mode",
        consequences=(
            ("false", "Captured asks may be filed with board instead of only listed for review."),
        ),
    ),
    _S(
        "ASK_CAPTURE_CAPS",
        "caps",
        "C3",
        default={"per_session": 5, "per_seat_day": 30, "model_calls_day": 80},
        note="extractor and filing caps",
        group="Ask Capture",
        label="Ask Capture Caps",
    ),
    # C5 lane sweep
    _S(
        "LANE_SWEEP_MODE",
        "enum",
        "C5",
        default="report",
        choices=("report", "act", "retire"),
        note="act arms merges and retires merged-PR lanes;  retire adds the Phase 3 deletions",
        group="Lane Sweep",
        label="Lane Sweep Mode",
        consequences=(
            ("act", "The sweep may merge and retire merged-PR lanes."),
            (
                "retire",
                "The sweep may merge, retire merged-PR lanes and make the Phase 3 deletions.",
            ),
        ),
    ),
    _S(
        "LANE_SWEEP_CAPS",
        "caps",
        "C5",
        default={"lane_retires_per_run": 10, "report_posts_per_run": 12},
        note="sweep action caps",
        group="Lane Sweep",
        label="Lane Sweep Caps",
    ),
    _S(
        "LANE_SWEEP_OPTIN_REPOS",
        "list",
        "C5",
        default=(),
        note="money-path repos the sweep may act on;  empty means none",
        group="Lane Sweep",
        label="Money-Path Repositories The Sweep May Touch",
    ),
    # N1 Apple Notes (AFC-331;  the Operator notes queue, AFC-486)
    _S(
        "OPERATOR_API_URL",
        "str",
        "N1",
        default="",
        local="deny",
        pattern=r"^(https://[A-Za-z0-9.-]+(:[0-9]{1,5})?)?$",
        note="the Operator API's https origin (no path):  where simple-notes, the stdio MCP and the Mac runner send "
        "Apple Notes requests off a Mac (docs/protocols/operator-notes.md);  empty means not configured;  the Worker "
        "has the same origin as its LISTENER_API_URL var",
        group="Apple Notes",
        label="Operator API Address",
        hosts="both",
        consumers=_NOTES,
        format="an https origin with no path",
        example="https://operator.example.com",
    ),
    _S(
        "APPLE_NOTES_PIN_RETRY",
        "bool",
        "N1",
        default=True,
        safe=False,
        note="true lets apple-notes-coding.sh run the pin or unpin Shortcut a second time when the first run misses the note "
        "index;  false keeps the single attempt;  the APPLE_NOTES_PIN_RETRY environment variable is only a "
        "local narrowing override and can turn the retry off, never on",
        group="Apple Notes",
        label="Retry The Pin Shortcut",
        hosts="mac",
        consumers=_PIN,
    ),
    # C7 consult
    _S(
        "CONSULT_ENABLED",
        "bool",
        "C7",
        default=False,
        safe=False,
        note="false refuses every consult verb",
        group="Consult",
        label="Consult On",
    ),
    # S1 Fleet Reference Sheet sync's Fleet RAG ingest (fleet-apps-sheet-sync.mjs)
    _S(
        "FLEET_SHEET_INGEST_ENABLED",
        "bool",
        "S1",
        default=True,
        safe=False,
        note="false writes the registry mirror and skips the `recall ingest --source doc` that carries it;  "
        "the sheet itself is still written",
        group="Reference Sheet",
        label="Recall Ingest After A Sheet Sync",
        hosts="mac",
        consumers=_SHEET,
    ),
    _S(
        "FLEET_SHEET_INGEST_TIMEOUT_MS",
        "int",
        "S1",
        default=5400000,
        narrower="min",
        minimum=60000,
        maximum=21600000,
        note="milliseconds the Fleet RAG ingest may run before fleet-apps-sheet-sync.mjs gives up on it;  "
        "a full corpus walk took about 43 minutes, so the default is 90;  a local file may only lower it",
        group="Reference Sheet",
        label="Recall Ingest Time Limit",
        hosts="mac",
        consumers=_SHEET,
        unit="milliseconds",
        step=300000,
    ),
    # C8 fleet compliance rules (AFC-400):  the thresholds the fleet-config compliance
    # rules read.  The default is the value the owner is expected to set,  and a
    # compliance rule reads UNKNOWN while the knob still only holds it:  a threshold
    # nobody set is not a threshold,  so the rule cannot decide on one.
    _S(
        "COMPLIANCE_CERT_EXPIRY_DAYS",
        "int",
        "C8",
        default=30,
        narrower="min",
        minimum=1,
        maximum=365,
        note="days before an Apple signing certificate expires that the fleet-config compliance rule "
        "calls a finding;  the rule reads the value only once it is set in Infisical,  so the default "
        "is the value to set rather than one the rule may use",
        group="Compliance",
        label="Certificate Expiry Warning",
        hosts="mac",
        consumers=_COMPLIANCE,
        unit="days",
        requires_explicit=True,
    ),
    _S(
        "COMPLIANCE_SECRETS_FINDINGS_MAX",
        "int",
        "C8",
        default=0,
        narrower="min",
        minimum=0,
        maximum=10,
        note="how many secrets-hygiene findings the fleet-config compliance rule tolerates;  the rule "
        "reads the value only once it is set in Infisical,  so the default is the value to set rather "
        "than one the rule may use",
        group="Compliance",
        label="Secrets Findings Tolerated",
        hosts="mac",
        consumers=_COMPLIANCE,
        unit="findings",
        requires_explicit=True,
    ),
    # W1 Cloudflare account usage watchdog (scripts/cf-usage-watchdog, AFC-617).  Its thresholds
    # are in that folder's config.json so the design stays in git;  these two choose how it acts.
    _S(
        "CF_USAGE_WATCHDOG_MODE",
        "enum",
        "W1",
        default="observe",
        choices=("observe", "enforce"),
        note="observe only alerts;  enforce also stops the Worker behind a breach.  "
        "A STOP file in the watchdog's state directory forces observe",
        group="Cloudflare Watchdog",
        label="Cloudflare Watchdog Mode",
        hosts="server",
        consumers=_WATCHDOG,
        consequences=(
            ("enforce", "The watchdog stops the Worker behind a breach, not only alerts."),
        ),
    ),
    _S(
        "CF_USAGE_WATCHDOG_PROFILE",
        "enum",
        "W1",
        default="paid",
        choices=("paid", "free"),
        local="deny",
        note="which threshold profile in config.json applies:  paid catches runaways, free is 80% of the Free caps",
        group="Cloudflare Watchdog",
        label="Cloudflare Watchdog Profile",
        hosts="server",
        consumers=_WATCHDOG,
    ),
    # W2 the wake router (scripts/simple_tools/wake_settings.py, OPS-1048).  The one long-running
    # reader:  it loads these at startup and refreshes them in the background.  A test keeps the
    # defaults equal to wake_router.ROUTER_DEFAULTS and claude_wake.DEFAULTS.
    _S(
        "WAKE_ROUTER_DELIVERY_ENABLED",
        "bool",
        "W2",
        default=False,
        safe=False,
        note="false makes the wake router pick and write receipts and deliver nothing;  true also needs the seat "
        "named in WAKE_ROUTER_DELIVERY_SEATS",
        group="Wake Router",
        label="Wake Delivery On",
        hosts="server",
        consumers=_WAKE,
        requires=(("true", "WAKE_ROUTER_DELIVERY_SEATS"),),
        consequences=(("true", "The wake router delivers wakes for the seats listed."),),
    ),
    _S(
        "WAKE_ROUTER_DELIVERY_SEATS",
        "list",
        "W2",
        default=(),
        local="deny",
        note="the seats the wake router may deliver for;  empty means none, whatever WAKE_ROUTER_DELIVERY_ENABLED says",
        group="Wake Router",
        label="Seats Wake Delivery Covers",
        hosts="server",
        consumers=_WAKE,
    ),
    _S(
        "WAKE_ROUTER_WORKERS",
        "int",
        "W2",
        default=4,
        narrower="min",
        minimum=1,
        maximum=64,
        note="wake router worker threads;  read when the router starts, so a new value needs a restart",
        group="Wake Router",
        label="Wake Router Workers",
        hosts="server",
        consumers=_WAKE,
        unit="threads",
    ),
    _S(
        "WAKE_ROUTER_MAX_DELIVERIES",
        "int",
        "W2",
        default=16,
        narrower="min",
        minimum=1,
        maximum=256,
        note="deliveries that may run at once, each of which may be a whole headless turn",
        group="Wake Router",
        label="Most Wake Deliveries At Once",
        hosts="server",
        consumers=_WAKE,
        unit="deliveries",
    ),
    _S(
        "WAKE_ROUTER_MAX_ATTEMPTS",
        "int",
        "W2",
        default=5,
        narrower="min",
        minimum=1,
        maximum=50,
        note="failed tries before a wake ends in a dead receipt;  a busy or unreachable chat uses up none",
        group="Wake Router",
        label="Most Wake Delivery Tries",
        hosts="server",
        consumers=_WAKE,
        unit="tries",
    ),
    _S(
        "WAKE_ROUTER_RETRY_BACKOFF",
        "str",
        "W2",
        default="30,120,300,900",
        local="deny",
        pattern=r"^[1-9][0-9]{0,5}(,[1-9][0-9]{0,5}){0,15}$",
        note="seconds to wait after each failed try, in order;  the last repeats",
        group="Wake Router",
        label="Wake Retry Backoff",
        hosts="server",
        consumers=_WAKE,
        format="whole seconds separated by commas",
        example="30,120,300,900",
    ),
    _S(
        "WAKE_ROUTER_BUSY_RETRY_SECONDS",
        "int",
        "W2",
        default=30,
        narrower="max",
        minimum=1,
        maximum=3600,
        note="seconds before asking a busy chat again",
        group="Wake Router",
        label="Busy Chat Retry Wait",
        hosts="server",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_UNREACHABLE_RETRY_SECONDS",
        "int",
        "W2",
        default=300,
        narrower="max",
        minimum=1,
        maximum=86400,
        note="seconds before trying a host that did not answer again",
        group="Wake Router",
        label="Unreachable Host Retry Wait",
        hosts="server",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_MAX_AGE_SECONDS",
        "int",
        "W2",
        default=86400,
        narrower="min",
        minimum=60,
        maximum=604800,
        note="how long a wake may wait before it ends in a dead receipt",
        group="Wake Router",
        label="Longest A Wake May Wait",
        hosts="server",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_SHUTDOWN_WAIT_SECONDS",
        "int",
        "W2",
        default=1000,
        local="deny",
        minimum=1,
        maximum=3600,
        note="what a SIGTERM waits for running deliveries;  keep it above the resume limit plus 60 and below the "
        "service's stop timeout, or a restart closes a delivery as interrupted",
        group="Wake Router",
        label="Wake Router Shutdown Wait",
        hosts="server",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_RESUME_TIMEOUT_SECONDS",
        "int",
        "W2",
        default=900,
        narrower="min",
        minimum=60,
        maximum=3600,
        note="how long one headless resume of a chat may run before it is stopped and refused",
        group="Wake Router",
        label="Wake Resume Time Limit",
        hosts="both",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_CONFIRM_SECONDS",
        "int",
        "W2",
        default=20,
        narrower="min",
        minimum=1,
        maximum=300,
        note="how long a socket delivery watches the chat's transcript for proof it arrived",
        group="Wake Router",
        label="Wake Confirmation Wait",
        hosts="both",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_SETTINGS_REFRESH_SECONDS",
        "int",
        "W2",
        default=300,
        local="deny",
        minimum=30,
        maximum=86400,
        note="how often the wake router reads these knobs again;  SIGHUP reads them at once",
        group="Wake Router",
        label="Wake Router Settings Refresh",
        hosts="server",
        consumers=_WAKE,
        unit="seconds",
    ),
    _S(
        "WAKE_ROUTER_MAC_SSH",
        "str",
        "W2",
        default="",
        local="deny",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]*$",
        note="the ssh destination of the Mac for wakes to its chats;  empty falls back to CLAUDE_LETTER_REMOTE",
        group="Wake Router",
        label="Wake Router Mac Destination",
        hosts="server",
        consumers=_WAKE,
        format="an ssh destination, with no options",
    ),
)

BY_NAME: dict[str, Spec] = {s.name: s for s in REGISTRY}
assert len(BY_NAME) == len(REGISTRY), "duplicate knob name in REGISTRY"


def spec_for(name: str) -> Spec:
    """The registry entry for `name`, or KeyError.  Callers wrap it into KnobError."""
    return BY_NAME[name]
