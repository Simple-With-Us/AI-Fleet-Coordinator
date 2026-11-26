"""The alert service:  one poll cycle and the daemon loop.

Per cycle, per confirmed ownership row on the allowlist:  read the PR, its
reviews, its inline comments and its head's check runs through the relay's
service_read lane;  seed a first-sight baseline instead of alerting on
history;  derive events;  submit one wake per new event to the ATTESTED
origin session;  then settle pending receipts.  Every event lands in the
store exactly once;  anything that cannot be delivered is held with a named
reason and counted, never dropped.

Besides review activity, two states of the PR itself are events:  a check run
on the current head that failed, and a current head in merge conflict with
the base branch.  Both take the same road as a review event (one Operator
status line, then one exact-origin wake), so the chat that owns the PR hears
what blocks it.  Mergeability is read from the PR body;  a body without it
(the relay's reduced read) derives no conflict event, in either direction.

Only claimed-ownership rows drive alerts:  session, letter AND generation
claims come from the open_pr caller;  a row missing any of them is held
("no-origin-claim"), never delivered.  The claims are untrusted hints:  the
wake-router checks all three against the live registry row, and require_pr
pins the wake to the session's own bound_prs entry, before delivering.

Configuration (environment;  no secrets in argv or the environment itself):
  RA_OWNERS_PATH         the relay pr_owners JSONL (read-only)
  RA_STATE_DIR           this service's own state folder (event store, lock,
                         status.json)
  RA_RELAY_URL           default http://127.0.0.1:8471
  RA_SERVICE_TOKEN_FILE  this service's dedicated relay service_read bearer
  RA_ROUTER_URL          default http://127.0.0.1:8794
  RA_WAKE_KEY_FILE       this service's dedicated wake-router credential
  RA_REPOS               comma allowlist of qualified repos (REQUIRED)
  RA_SEAT_MAP_JSON       relay slug -> router seat mapping (explicit; no default)
  RA_SELFTEST_SEAT       router seat with at least one live chat, for the
                         startup require-session support probe (required)
  RA_INTERVAL_S          poll cadence, default 900
"""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime

from . import (
    comment_evidence,
    detect,
    events,
    github_read,
    issue_events,
    messages,
    stamps,
    wakes,
)

ISSUE_COMMENT_KINDS = issue_events.ISSUE_COMMENT_KINDS


# Relay seat slug -> wake-router seat tag.  There is NO default and NO
# derivation:  the mapping is deploy-time config (RA_SEAT_MAP_JSON), because a
# derived tag is a guess about identity, and an unmapped seat is held, never
# guessed.
def seat_map_from(env) -> dict:
    raw = str(env.get("RA_SEAT_MAP_JSON") or "").strip()
    if not raw:
        return {}
    table = json.loads(raw)
    if not isinstance(table, dict):
        raise ValueError("RA_SEAT_MAP_JSON must be a JSON object")
    return {str(k): str(v).upper() for k, v in table.items()}


def load_owner_rows(path: str) -> list[dict]:
    rows: dict[tuple, dict] = {}
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "repo" in row and "number" in row:
                rows[(row["repo"], int(row["number"]))] = row
    return [r for r in rows.values() if r.get("confirmed")]


def _edit_key(c: dict):
    """The exact updated_at key when c is a well-formed EDITED comment
    (valid strict stamps, updated later than created), else None.  Unknown
    or unedited revisions are never baselined and never alerted as edits."""
    c_key = stamps.stamp_key(str(c.get("created_at") or ""))
    u_key = stamps.stamp_key(str(c.get("updated_at") or ""))
    if c_key is None or u_key is None or u_key <= c_key:
        return None
    return u_key


def _instant(text) -> datetime | None:
    """Parse an ISO-8601 timestamp as an INSTANT, not a string:  trailing Z,
    fractional seconds and numeric offsets all normalise to an aware UTC
    datetime.  Returns None when missing or unparseable, so callers can pick
    a conservative fallback instead of comparing lexical strings (where
    '2026-01-01T00:00:00+05:30' sorts against '2025-12-31T23:59:59Z')."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # A naive timestamp is ambiguous:  treat it as UTC rather than guess
        # a local zone, and never mix naive with aware in comparisons.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def seat_for(slug: str, seat_map: dict) -> str:
    return seat_map.get(slug, "")


def _credential_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


#: A wake whose receipt cannot be read at all (404 past the router's own
#: retention, or unreachable through every retry) is bounded:  after this
#: many seconds unsettled the event is held "receipt-lost" so it surfaces
#: instead of pending forever.
RECEIPT_DEADLINE_S = 3600
#: Upper bound on re-alert epochs for recoverable holds (not-live /
#: ambiguous):  each epoch mints a fresh wake id only after the prior wake
#: record is certainly undelivered.
MAX_EPOCHS = 5


def _findings_members(value):
    """A findings batch's exact constituent membership, or None:  1..LIMIT
    exact-key records, ids int>0 strictly increasing, an ISO timestamp string
    and one lowercase 40-hex head per member.  Anything else is no membership
    at all, so the caller delivers ordinarily instead of qualifying an
    aggregate."""
    if not isinstance(value, list) or not 1 <= len(value) <= events.FINDINGS_MEMBER_LIMIT:
        return None
    members = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "at", "head"}:
            return None
        if type(item["id"]) is not int or item["id"] < 1:
            return None
        if not isinstance(item["at"], str) or not isinstance(item["head"], str):
            return None
        if len(item["head"]) != 40 or any(c not in "0123456789abcdef" for c in item["head"]):
            return None
        members.append({"id": item["id"], "at": item["at"], "head": item["head"]})
    if any(b["id"] <= a["id"] for a, b in zip(members, members[1:])):
        return None
    return members


def _valid_intent_members(value, high):
    """A migration intent's WHOLE member list, or None when any element is
    off the recorded shape:  a list of exact {id, digest, updated, actor}
    records with unique bounded ids (1..high), one lowercase 64-hex digest,
    a valid stamp and a string actor per member.  The intent is validated as
    a whole BEFORE any baseline or completion write:  a partial or malformed
    member list is never replayed piecemeal (silently skipping bad members
    and completing anyway would re-alert the very history the boundary was
    recorded to adopt)."""
    if not isinstance(value, list):
        return None
    members = []
    seen_ids = set()
    for member in value:
        if not isinstance(member, dict) or set(member) != {"id", "digest", "updated", "actor"}:
            return None
        cid = member["id"]
        digest = member["digest"]
        updated = member["updated"]
        if type(cid) is not int or cid < 1 or cid > high or cid in seen_ids:
            return None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return None
        if not isinstance(updated, str) or stamps.stamp_key(updated) is None:
            return None
        if not isinstance(member["actor"], str):
            return None
        seen_ids.add(cid)
        members.append(member)
    return members


#: The qualified router's source-event grammar for the canonical-id kinds:
#: i<id> (adds, issue_opened) and i<id>r<64-hex digest> (edits), the id a
#: whole canonical numeric GitHub id bounded by 2**53-1, never a node id.
_EVENT_CANON_ID_RE = re.compile(r"i([1-9][0-9]{0,15})$")
_EVENT_CANON_EDIT_RE = re.compile(r"i([1-9][0-9]{0,15})r[0-9a-f]{64}$")
_MAX_GITHUB_ID = 2**53 - 1


def _canonical_id_event(row, pattern):
    """A new-kind source identity (pr_comment_*/issue_*), or None when the
    row cannot prove the qualified grammar:  exact event id shape, grounded
    canonical source instant, integer epoch, and (issue subject only) the
    router's 1..99,999,999 number bound.  The subject is derived from the
    kind - the PR ledger drops the field on append, and the kinds are
    disjoint between subjects."""
    kind = row.get("kind")
    subject = "pr" if kind.startswith("pr_") else "issue"
    event_id = row.get("event_id")
    match = pattern.fullmatch(event_id) if isinstance(event_id, str) else None
    if match is None or int(match.group(1)) > _MAX_GITHUB_ID:
        return None
    number = row.get("number")
    if subject == "issue" and (type(number) is not int or not 1 <= number <= 99_999_999):
        return None
    instant = _instant(row.get("source_time"))
    if instant is None:
        return None
    epoch = row.get("epoch")
    if type(epoch) is not int or epoch < 0:
        return None
    return {
        "repo": row.get("repo"),
        "number": number,
        "kind": kind,
        "event_id": event_id,
        "epoch": epoch,
        "source_epoch": instant.timestamp(),
        "subject": subject,
    }


def source_identity(row):
    # Old ledger rows have no source_time and use ordinary delivery; no
    # backfill is inferred from detection timestamps.
    kind = row.get("kind")
    if kind in ("pr_comment_added", "issue_comment_added", "issue_opened"):
        return _canonical_id_event(row, _EVENT_CANON_ID_RE)
    if kind in ("pr_comment_edited", "issue_comment_edited"):
        return _canonical_id_event(row, _EVENT_CANON_EDIT_RE)
    if kind in ("review_submitted", "review_completed"):
        instant = _instant(row.get("source_time"))
        if instant is None:
            return None
        return {
            **{k: row.get(k) for k in ("repo", "number", "kind", "event_id", "epoch")},
            "source_epoch": instant.timestamp(),
        }
    if kind == "findings_added":
        # A findings batch is a SET of source comments, not one source:  its
        # identity is the exact membership (id, creation time, head per
        # comment).  source_epoch is the newest member's own creation time;
        # the event id must name that same newest member.
        members = _findings_members(row.get("members"))
        if members is None:
            return None
        instants = [_instant(m["at"]) for m in members]
        if any(i is None for i in instants):
            return None
        if row.get("event_id") != "c%d" % members[-1]["id"]:
            return None
        return {
            **{k: row.get(k) for k in ("repo", "number", "kind", "event_id", "epoch")},
            "source_epoch": max(instants).timestamp(),
            "members": members,
        }
    return None


#: Reasons the bounded recovery pass may revisit:  the delivery-time store
#: miss is the one hold that accumulates terminal rows below the epoch cap
#: (it was never redrive-eligible, so such rows exist).  The pick-time
#: holds in wakes.REDRIVE_REASONS are already redriven while pending by
#: every settle pass;  a below-cap terminal row carrying one of them is a
#: state current code cannot produce, and recovering it is outside this
#: gap.  Membership here is only context for which rows a selection may
#: name:  it never substitutes for the row's authoritative delivered:false
#: receipt, which the pass reads live.  REDRIVE_REASONS itself is NOT
#: expanded:  the settle path's reasons are unchanged.
RECOVERY_REASONS = ("session-not-found",)

#: The marker a recovery next-attempt carries on its ledger row in the
#: dedicated `recovery` field:  set when the pass persists the selected
#: attempt and NEVER cleared by any status transition, so reconciliation
#: can keep the attempt bounded to its selection in every state.  A marked
#: row whose receipt is missing (or expired past retention) is NEVER
#: resubmitted - after an uncertain acceptance the absence of a record
#: cannot prove the wake did not run, and same-id dedupe is not
#: permanent.  It fails closed instead.
RECOVERY_ATTEMPT = "recovery-attempt"

#: Upper bound on redispatches in ONE recovery pass, whatever the explicit
#: selection names:  recovery is a bounded post-repair act, not a scan.
MAX_RECOVERY_SENDS = 8

#: Selector fields, exactly:  the event, the prior wake that must still be
#: its ledger row, the epoch that row must sit at, and the posted Operator
#: message the redispatch will reference.
SELECTION_FIELDS = ("repo", "number", "kind", "event_id", "wake_id", "epoch", "message_id")


def _recovery_selection(env) -> list:
    """The explicit recovery selection (RA_RECOVER_HELD):  a JSON array of
    objects, one per exact stored attempt::

        [{"repo": "owner/name", "number": 2196, "kind": "review_completed",
          "event_id": "51", "wake_id": "ra-...", "epoch": 0,
          "message_id": 91}]

    Structured JSON because event ids legitimately contain separators
    (merge_conflict ids are head:base commit pairs) - a delimited string
    cannot carry them.  Empty, unset, "[]", "0", "false" or "no" means
    recovery is OFF:  there is no default-on startup scan.  A malformed
    document or entry is a configuration refusal, never a guess.  Kinds
    validate against events.KINDS as adopted NOW.
    """
    raw = str(env.get("RA_RECOVER_HELD") or "").strip()
    if not raw or raw.lower() in ("0", "false", "no"):
        return []
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ValueError("RA_RECOVER_HELD is not a JSON array of selectors: %s" % exc)
    if not isinstance(doc, list):
        raise ValueError("RA_RECOVER_HELD must be a JSON array of selectors")
    selected = []
    for item in doc:
        if not isinstance(item, dict) or set(item) != set(SELECTION_FIELDS):
            raise ValueError(
                "RA_RECOVER_HELD entry must have exactly the fields %s: %r"
                % (", ".join(SELECTION_FIELDS), item)
            )
        repo = item["repo"]
        number = item["number"]
        kind = item["kind"]
        event_id = item["event_id"]
        wake_id = item["wake_id"]
        epoch = item["epoch"]
        message_id = item["message_id"]
        ok = (
            type(repo) is str
            and type(number) is int
            and wakes.PR_RE.fullmatch("%s#%d" % (repo, number)) is not None
            and kind in events.KINDS
            and type(event_id) is str
            and bool(event_id)
            and type(wake_id) is str
            and wakes.WAKE_ID_RE.fullmatch(wake_id) is not None
            and type(epoch) is int
            and 0 <= epoch
            and epoch + 1 < MAX_EPOCHS
            and type(message_id) is int
            and message_id > 0
        )
        if not ok:
            raise ValueError("RA_RECOVER_HELD entry is not a valid selector: %r" % (item,))
        selected.append((repo, number, kind, event_id, wake_id, epoch, message_id))
    return selected


#: The settled cross-layer compatibility bound for CONSUMER routes (relay
#: narrow reader / router / association grammar):  repo object numbers stay
#: within 1..99,999,999.  The pure hint mapper accepts a wider range as a
#: parser;  a hint beyond this bound is durably HELD with a typed reason
#: before any read or bind - a documented boundary refusal, never a silent
#: truncation and never a bind an downstream contract cannot represent.
CONSUMER_NUMBER_MAX = 99999999


class RetryHint(Exception):
    """A hint's reconcile hit a TRANSIENT failure (a read error, an
    uncertain page, a refused capability probe, a binder/provider fault).
    The worker requeues the row with bounded backoff instead of consuming
    it:  a transient failure is never a permanent loss, and a poison hint
    backs off rather than hot-looping."""


class AlertService:
    def __init__(
        self,
        store: events.EventStore,
        gh,
        router: wakes.WakeClient,
        owners_path: str,
        seat_map: dict,
        repos: set[str],
        clock=time.time,
        *,
        gate_seat: str,
        owner_provider=None,
        coverage_provider=None,
        coverage_reset=None,
        origin_binder=None,
        cancelled=None,
        message_poster=None,
        issue_store=None,
        issue_owner_provider=None,
        issue_origin_binder=None,
        issue_probe_target=None,
        pr_comment_alerts=False,
    ):
        if not repos:
            raise ValueError("an explicit repo allowlist is required (no fleet-wide default)")
        if not gate_seat:
            # The per-cycle require_pr probe is mandatory:  without a gate
            # seat the service cannot detect a router rollback before
            # dispatching, so there is no supported silent no-gate mode.
            raise ValueError("a gate seat is required for the per-cycle capability probe")
        self.cancelled = cancelled or (lambda: False)
        self.message_poster = message_poster
        self.binding_holds = {}
        self.bound_this_cycle = set()
        self.origins_this_cycle = {}
        self.bound_issues_this_cycle = set()
        self.issue_origins_this_cycle = {}
        self.owner_provider = owner_provider
        # The receipt census' own coverage word ("complete"/"partial"), set
        # by the provider after each enumeration.  Owners absent from a
        # partial census are never iterated, so per-PR holds cannot speak
        # for them:  coverage is reported here, at scan level, and it is
        # never derived from the rows that did arrive.
        self.coverage_provider = coverage_provider
        # Opens a fresh scan each cycle (the provider accumulates coverage
        # conservatively across every census read within one scan).
        self.coverage_reset = coverage_reset
        self.origin_binder = origin_binder
        self.store = store
        self.gh = gh
        self.router = router
        self.owners_path = owners_path
        self.seat_map = dict(seat_map)
        self.repos = set(repos)
        self.clock = clock
        # When set, every cycle re-probes the router's require_pr validation
        # before any wake goes out:  a rollback between cycles holds the
        # cycle (nothing dispatches, no cursor advances) instead of sending
        # wakes whose PR binding would go unenforced.
        self.gate_seat = gate_seat
        # Standalone-issue held evidence (separate ledger, separate file:
        # never the PR store's seeds/cursors), or None when unwired.
        self.issue_store = issue_store
        # The standalone-issue lane:  the daemon-side bound_issues census
        # provider, the registry binder that re-proves ownership at
        # execution, and the operator-supplied REAL probe target (an exact
        # live session plus an unbound issue) for the per-cycle require_issue
        # capability gate.  All three are mandatory for issue dispatch:  a
        # configured census without a probe target holds the issue lane
        # (never dispatches against an unproven router) while the PR lane
        # runs unaffected.
        self.issue_owner_provider = issue_owner_provider
        self.issue_origin_binder = issue_origin_binder
        self.issue_probe_target = issue_probe_target
        # Generic PR conversation-comment (issue_comment on a PR) add/edit
        # alerting.  DEFAULT OFF:  the released scope is PR review/finding/
        # conflict only, so without an explicit opt-in this service is the
        # legacy system exactly - no conversation-comment canonical reads,
        # no comment baselines/cursor/migration writes, no comment effects,
        # and pr_comment hints hold durably instead of consuming.  Enabling
        # later runs the reviewed fixed-boundary migration on legacy seeds,
        # so nothing alerts retroactively.
        self.pr_comment_alerts = bool(pr_comment_alerts)
        # Targeted-reconcile holds, visible in the status snapshot until the
        # next successful reconcile of that owner clears them.  Additive
        # only:  a reconcile never touches cycle-owned binding_holds.
        self.targeted_holds = {}

    def cycle(self) -> dict:
        counts = {
            "prs": 0,
            "seeded": 0,
            "events": 0,
            "submitted": 0,
            "delivered": 0,
            "held": 0,
            "unknown": 0,
            "read_errors": 0,
            "redriven": 0,
        }
        if self.gate_seat:
            self.router.check_require_pr_support(self.gate_seat, probe_id=str(int(self.clock())))
        self.bound_this_cycle = set()
        self.origins_this_cycle = {}
        self.bound_issues_this_cycle = set()
        self.issue_origins_this_cycle = {}
        self.binding_holds = {}
        if self.coverage_reset is not None:
            self.coverage_reset()
        owners = self.owner_provider() if self.owner_provider else load_owner_rows(self.owners_path)
        for row in owners:
            repo, number = row["repo"], int(row["number"])
            if repo not in self.repos:
                continue
            counts["prs"] += 1
            if self.origin_binder is not None:
                try:
                    if self.cancelled():
                        continue
                    origin = self.origin_binder(repo, number)
                    self.origins_this_cycle[(repo, number)] = origin
                    self.bound_this_cycle.add((repo, number))
                except Exception as exc:
                    if len(self.binding_holds) < 1000:
                        self.binding_holds[repo + "#" + str(number)] = type(exc).__name__
                    # No cursor advances while binding is unavailable. A later
                    # cycle resumes from the original event watermark.
                    counts["held"] += 1
                    continue
            self._scan_owner(row, counts)
        issue_owners = None
        if self.issue_owner_provider is not None:
            issue_owners = self._issue_cycle(counts)
        self._settle(counts, owners)
        if issue_owners is not None and self.issue_store is not None:
            for pending in self.issue_store.pending():
                self._settle_row(counts, issue_owners, pending)
        # Read coverage at the END of the scan, after every census read the
        # cycle made (enumeration and binder lookups alike), so the cycle's
        # own counts and the status snapshot describe the same scan.
        if self.coverage_provider is not None:
            counts["coverage"] = self.coverage_provider()
        return counts

    def _scan_owner(self, row: dict, counts: dict) -> None:
        """The per-owner poll body:  read, first-sight seed, derive, dispatch.
        Shared by the full cycle and a targeted reconcile alike;  it owns no
        global census, coverage or settlement state, and it never resets
        anything outside its owner."""
        repo, number = row["repo"], int(row["number"])
        try:
            pr = self.gh.pull(repo, number)
            if str(pr.get("state") or "") != "open":
                return
            current_head = str((pr.get("head") or {}).get("sha") or "")
            cursor = self.store.cursor(repo, number)
            reviews = self.gh.reviews(repo, number)
            comments = self.gh.review_comments(repo, number)
            check_runs = self.gh.check_runs(repo, current_head) if current_head else []
            conv = (
                tuple(self.gh.issue_comments(repo, number))
                if self.pr_comment_alerts
                # Lane disabled (the default):  no conversation-comment
                # read at all - the cycle is the legacy system exactly.
                else ()
            )
        except github_read.GithubReadError:
            # A failed or uncertain read advances no cursor and creates no
            # event:  next cycle retries from the same watermark.
            counts["read_errors"] += 1
            return
        if not self.store.has_seed(repo, number):
            # First sight:  baseline only what predates the PR's
            # REGISTRATION row (the relay's creation record).  Activity
            # landing between registration and this first poll is after
            # the baseline and alerts normally - first sight is not a
            # silent discard.
            # A row with no registration time baselines EVERYTHING
            # (conservative:  no replay), never discards silently.
            owner_instant = _instant(row.get("ts"))
            if owner_instant is None:
                # Registration time missing or unparseable:  baseline
                # EVERYTHING (no replay), same as the old "9999" sentinel.
                predates = lambda ts, _missing: True
            else:

                def predates(ts, missing_seeded):
                    item = _instant(ts)
                    if item is None:
                        # Unparseable item time keeps the old per-type
                        # posture:  reviews/comments suppressed, checks
                        # still alerted (the old "" vs "9" sentinels).
                        return missing_seeded
                    return item <= owner_instant

            max_review = max(
                [
                    int(r["id"])
                    for r in reviews
                    if isinstance(r.get("id"), int) and predates(r.get("submitted_at"), True)
                ]
                or [0]
            )
            max_comment = max(
                [
                    int(c["id"])
                    for c in comments
                    if isinstance(c.get("id"), int) and predates(c.get("created_at"), True)
                ]
                or [0]
            )
            max_check = max(
                [
                    int(r["id"])
                    for r in check_runs
                    if isinstance(r.get("id"), int) and predates(r.get("completed_at"), False)
                ]
                or [0]
            )
            max_issue_comment = max(
                [
                    int(c["id"])
                    for c in conv
                    if type(c.get("id")) is int and predates(c.get("created_at"), True)
                ]
                or [0]
            )
            # Crash-safe ordering:  the baseline revision rows are durable
            # BEFORE the seed (the completion marker).  A crash between them
            # re-runs this whole block on the next cycle (has_seed is still
            # false):  baseline rows dedupe by ledger key, and the seed is
            # written only after every baseline row of THIS scan is durable.
            # A canonical read that changed across the retry moves no
            # boundary backward:  the retry's own complete ascending scan is
            # the grounded rollout evidence for the rows it baselines.
            owner_key = stamps.stamp_key(str(row.get("ts") or ""))
            for c in conv:
                # Baseline pre-registration EDITS only:  a comment that
                # existed at seed and whose current revision was already
                # there at REGISTRATION is known state.  A revision edited
                # AFTER registration but before this first scan is real
                # post-registration activity and must alert, so it is never
                # baselined.  Unknown registration time baselines everything
                # (conservative, no replay).
                u_key = _edit_key(c)
                if u_key is None:
                    continue
                if owner_key is not None and u_key > owner_key:
                    continue
                self._baseline_comment_edit(repo, number, c, max_issue_comment)
            if self.pr_comment_alerts:
                self.store.seed(
                    repo, number, max_review, max_comment, max_check, max_issue_comment
                )
            else:
                # Lane disabled (the default):  the seed is the LEGACY
                # three-part shape - no conversation-comment watermark is
                # established and no migration intent is recorded while the
                # lane is off.  Enabling the lane later runs the reviewed
                # fixed-boundary migration on this same seed.
                self.store.append(
                    {
                        "repo": repo,
                        "number": number,
                        "kind": "seed",
                        "event_id": "%d:%d:%d" % (max_review, max_comment, max_check),
                        "status": "seed",
                        "summary": "baseline; nothing before this is alerted",
                    }
                )
            counts["seeded"] += 1
        else:
            # A legacy three-part seed predates conversation-comment
            # coverage.  Baseline its fourth component ONCE, anchored to the
            # current canonical scan (the rollout moment):  the legacy row's
            # bytes stay untouched, a new seed row preserves its three
            # components, and every existing comment (edited ones by digest)
            # becomes known state instead of a flood of new alerts.
            seed_row = self.store.seed_row(repo, number)
            parts = str((seed_row or {}).get("event_id") or "").split(":")
            if len(parts) < 4 and self.pr_comment_alerts:
                # Fixed-boundary migration:  the FIRST attempt records a
                # durable migration-intent row BEFORE any baseline write -
                # the intended four-part completion value (its event_id) and
                # the exact intended baseline revisions (its members).  A
                # crash at any later point resumes THIS SAME boundary:  the
                # baseline is never recomputed from a changed retry scan, so
                # activity landing after the intent was recorded is never
                # silently absorbed into known state - it alerts after the
                # migration completes.
                intent = self.store.migration_intent(repo, number)
                if intent is None:
                    mig_high = max(
                        [int(c["id"]) for c in conv if type(c.get("id")) is int] or [0]
                    )
                    intended = []
                    seen_cids = set()
                    for c in conv:
                        cid = c.get("id")
                        if type(cid) is not int or cid < 1 or cid in seen_cids or _edit_key(c) is None:
                            continue
                        seen_cids.add(cid)
                        intended.append(
                            {
                                "id": cid,
                                "digest": comment_evidence.comment_revision_digest(c),
                                "updated": str(c.get("updated_at") or ""),
                                "actor": str((c.get("user") or {}).get("login") or ""),
                            }
                        )
                    intent = self.store.append(
                        {
                            "repo": repo,
                            "number": number,
                            "kind": "migration-intent",
                            "event_id": "%s:%s:%s:%d"
                            % (
                                parts[0] if len(parts) > 0 else "0",
                                parts[1] if len(parts) > 1 else "0",
                                parts[2] if len(parts) > 2 else "0",
                                mig_high,
                            ),
                            "status": "seed",
                            "members": intended,
                            "summary": "migration intent: the fixed adoption boundary, durable before any baseline write",
                        }
                    )
                intent_parts = str(intent.get("event_id") or "").split(":")
                if len(intent_parts) != 4 or not all(p.isdigit() for p in intent_parts):
                    # A corrupt intent row cannot be replayed and the
                    # boundary must never silently move:  hold the migration
                    # visibly instead of completing on a guess.
                    counts["held"] += 1
                    return
                mig_high = int(intent_parts[3])
                members = _valid_intent_members(intent.get("members"), mig_high)
                if members is None:
                    # The WHOLE intent is validated before any write:  a
                    # missing, malformed, duplicate or out-of-bound member
                    # holds the migration visibly.  Completing anyway (with
                    # the bad members silently skipped) would re-alert the
                    # adopted history - never a partial completion.
                    counts["held"] += 1
                    return
                for member in members:
                    self._baseline_from_member(repo, number, member, mig_high)
                self.store.append(
                    {
                        "repo": repo,
                        "number": number,
                        "kind": "seed",
                        "event_id": str(intent["event_id"]),
                        "status": "seed",
                        "summary": "migration baseline: conversation comments existing at rollout are known state",
                    }
                )
                counts["migrated"] = counts.get("migrated", 0) + 1
        cur = self.store.cursor(repo, number)
        found = detect.derive(
            repo,
            number,
            current_head,
            reviews,
            comments,
            check_runs,
            cur,
            pull=pr,
        )
        if self.pr_comment_alerts:
            watermark = int(cur.get("issue_comment_id") or 0)
            prior, seen = detect.comment_revision_state(self.store.comment_rows(repo, number))
            for c in conv:
                event = detect.reconcile_comment(
                    repo, number, c, watermark, prior.get(c.get("id")), current_head,
                    seen=seen.get(c.get("id")),
                )
                if event is not None:
                    found.append(event)
        link = str(pr.get("html_url") or "")
        for event in found:
            if self.store.get(repo, number, event["kind"], event["event_id"]):
                continue
            counts["events"] += 1
            self._dispatch(row, event, link, counts)


    def _baseline_comment_edit(self, repo: str, number: int, c: dict, high: int) -> None:
        """Record the current revision digest of an already-edited comment as
        baseline (first-sight seed):  known state, never an alert.  Only a
        LATER revision (a different digest) alerts."""
        cid = c.get("id")
        if type(cid) is not int or cid < 1 or _edit_key(c) is None:
            return
        self._baseline_from_member(
            repo,
            number,
            {
                "id": cid,
                "digest": comment_evidence.comment_revision_digest(c),
                "updated": str(c.get("updated_at") or ""),
                "actor": str((c.get("user") or {}).get("login") or ""),
            },
            high,
        )

    def _baseline_from_member(self, repo: str, number: int, member: dict, high: int) -> None:
        """Append ONE baseline revision row from an exact intended record (a
        migration-intent member, or the live-comment equivalent built by
        _baseline_comment_edit):  deduped by ledger key on resume, never an
        alert.  The row carries the digest, the actor and the revision's own
        updated_at as source_time so the shared reconciliation reads it as
        an observation.  A malformed member is skipped, never coerced."""
        if not isinstance(member, dict):
            return
        cid = member.get("id")
        digest = member.get("digest")
        updated = str(member.get("updated") or "")
        if (
            type(cid) is not int
            or cid < 1
            or cid > high
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or stamps.stamp_key(updated) is None
        ):
            return
        event_id = "i%dr%s" % (cid, digest)
        if self.store.get(repo, number, "pr_comment_edited", event_id) is not None:
            # A resumed baseline attempt (crash before the completion seed)
            # finds its own rows already durable:  never duplicate them.
            return
        self.store.append(
            {
                "repo": repo,
                "number": number,
                "kind": "pr_comment_edited",
                "event_id": event_id,
                "status": "seed",
                "source_time": updated,
                "observed_updated": updated,
                "actor": str(member.get("actor") or ""),
                "revision": digest,
                "summary": "baseline; nothing before this is alerted",
            }
        )

    def _issue_cycle(self, counts: dict) -> list:
        """The standalone-issue part of one poll cycle:  the require_issue
        capability gate (fail-closed for the ISSUE lane only), the one-time
        rollout boundary, then the complete protected bound_issues census,
        each row canonically read and reconciled.  Returns the census rows
        for settlement, or None when the lane is unwired."""
        if self.issue_store is None:
            return None
        try:
            rows = [
                row
                for row in (self.issue_owner_provider() or [])
                if isinstance(row, dict)
            ]
        except Exception:  # noqa: BLE001 - a census failure is contained to the issue lane
            # The provider owns a COMPLETE protected enumeration and fails
            # loud (Held) when it cannot vouch.  That failure never escapes
            # into PR scanning or settlement:  the issue lane has no
            # coverage this cycle, pending issue rows settle with an empty
            # owner list (every new effect fenced by _effects_allowed), and
            # the next cycle retries the enumeration.
            counts["issue_census_failed"] = counts.get("issue_census_failed", 0) + 1
            return []
        target = self.issue_probe_target
        if callable(target):
            # A callable target is resolved FRESH each cycle (the daemon
            # derives session/letter/CURRENT generation from the same
            # protected census, so a generation change never goes stale).
            # Any failure fails the issue lane closed this cycle.
            try:
                target = target()
            except Exception:  # noqa: BLE001 - a target failure is a gate failure
                target = None
        try:
            if not isinstance(target, dict):
                raise wakes.WakeError("issue probe target unconfigured")
            self.router.check_required_issue_support(
                str(target.get("seat") or ""),
                probe_id=str(int(self.clock())),
                session=str(target.get("session") or ""),
                letter=str(target.get("letter") or ""),
                generation=target.get("generation"),
                require_issue=str(target.get("require_issue") or ""),
            )
        except wakes.WakeError:
            # The router's require_issue support is unproven:  NO issue wake
            # goes out this cycle (nothing dispatches, no cursor advances).
            # The PR lane is unaffected;  the lane retries next cycle.
            counts["issue_gate_failed"] = counts.get("issue_gate_failed", 0) + 1
            return rows
        self.issue_store.record_rollout()
        for row in rows:
            repo = str(row.get("repo") or "")
            number = row.get("number")
            if repo not in self.repos or type(number) is not int or number < 1:
                continue
            counts["issues"] = counts.get("issues", 0) + 1
            if self.issue_origin_binder is not None:
                try:
                    if self.cancelled():
                        continue
                    origin = self.issue_origin_binder.bind(repo, number)
                    self.issue_origins_this_cycle[(repo, number)] = origin
                    self.bound_issues_this_cycle.add((repo, number))
                except Exception as exc:  # noqa: BLE001 - a bind failure holds, never guesses
                    if len(self.binding_holds) < 1000:
                        self.binding_holds[repo + "#" + str(number)] = type(exc).__name__
                    counts["held"] += 1
                    continue
            self._scan_issue(row, counts)
        return rows

    def _scan_issue(self, row: dict, counts: dict) -> None:
        """The per-issue poll body:  canonical type/state read, first-sight
        baseline, shared comment reconciliation, dispatch.  Mirrors
        _scan_owner's disciplines in the separate issue ledger."""
        repo, number = row["repo"], int(row["number"])
        try:
            issue = self.gh.issue(repo, number)
            if issue.get("kind") != "issue" or str(issue.get("state") or "") != "open":
                # A PR number in the issue census is owned by the PR lane;
                # a closed issue is settled.  Neither derives here.
                return
            conv = tuple(self.gh.issue_comments(repo, number))
        except github_read.GithubReadError:
            counts["read_errors"] += 1
            return
        reg_key = stamps.stamp_key(str(row.get("ts") or ""))
        if not self.issue_store.has_seed(repo, number):

            def predates_binding(c):
                # The comment baseline boundary is the census row's binding
                # instant:  only what postdates it alerts.  Unknown binding
                # time baselines everything (conservative, no replay), and
                # an unparseable comment stamp is baselined, never alerted.
                if reg_key is None:
                    return True
                c_key = stamps.stamp_key(str(c.get("created_at") or ""))
                return c_key is None or c_key <= reg_key

            high = max(
                [
                    int(c["id"])
                    for c in conv
                    if type(c.get("id")) is int and predates_binding(c)
                ]
                or [0]
            )
            # Crash-safe ordering:  baseline revision rows durable BEFORE
            # the seed (completion marker), deduped by key on a resume.
            for c in conv:
                u_key = _edit_key(c)
                if u_key is None:
                    continue
                if reg_key is not None and u_key > reg_key:
                    continue
                self._baseline_issue_comment_edit(repo, number, c, high)
            self.issue_store.seed(repo, number, high)
            counts["seeded"] += 1
        link = str(issue.get("html_url") or "")
        # issue_opened:  the canonical created_at establishes the opening
        # relative to the GROUNDED census-rollout boundary.  An opening
        # after rollout is current-state notification (a missed real-time
        # hint is covered);  an opening at/before rollout (every
        # pre-adoption binding and the first-scan window) is retrospective
        # history and is baselined, never replayed.  A binding timestamp is
        # never used as opening evidence.  Intermediate closed/deleted
        # history is not claimed from current-state reads.
        rollout_key = stamps.stamp_key(self.issue_store.rollout_ts() or "")
        created_key = stamps.stamp_key(str(issue.get("created_at") or ""))
        opened_event_id = "i%d" % issue["id"]
        if (
            rollout_key is not None
            and created_key is not None
            and created_key > rollout_key
            and self.issue_store.get(repo, number, "issue_opened", opened_event_id) is None
        ):
            event = {
                "repo": repo,
                "number": number,
                "kind": "issue_opened",
                # The qualified router's i<id> grammar:  the issue's own
                # canonical numeric GitHub id (validated 1..2**53-1 by the
                # read), never the number, never a node id, never "open".
                "event_id": opened_event_id,
                "subject": "issue",
                "source_time": str(issue.get("created_at") or ""),
                "actor": "",
                "head": "",
                "current_head": "",
                "head_matches": True,
                "revision": None,
                "observed_updated": None,
                "summary": "issue opened",
            }
            counts["events"] += 1
            self._dispatch(row, event, link, counts)
        watermark = int(self.issue_store.cursor(repo, number).get("comment_id") or 0)
        prior, seen = detect.comment_revision_state(
            self.issue_store.comment_rows(repo, number), kinds=ISSUE_COMMENT_KINDS
        )
        for c in conv:
            event = detect.reconcile_comment(
                repo, number, c, watermark, prior.get(c.get("id")), "", kinds=ISSUE_COMMENT_KINDS,
                seen=seen.get(c.get("id")),
            )
            if event is None:
                continue
            event["subject"] = "issue"
            if self.issue_store.get(repo, number, event["kind"], event["event_id"]):
                continue
            counts["events"] += 1
            self._dispatch(row, event, link, counts)

    def _baseline_issue_comment_edit(self, repo: str, number: int, c: dict, high: int) -> None:
        """The issue-ledger twin of _baseline_comment_edit:  record the
        current revision digest of an already-edited issue comment as known
        state (seed or crash-resumed attempt), never an alert."""
        cid = c.get("id")
        if type(cid) is not int or cid < 1 or cid > high or _edit_key(c) is None:
            return
        updated = str(c.get("updated_at") or "")
        digest = comment_evidence.comment_revision_digest(c)
        event_id = "i%dr%s" % (cid, digest)
        if self.issue_store.get(repo, number, "issue_comment_edited", event_id) is not None:
            return
        self.issue_store.append(
            {
                "repo": repo,
                "number": number,
                "kind": "issue_comment_edited",
                "event_id": event_id,
                "subject": "issue",
                "status": "seed",
                "source_time": updated,
                "observed_updated": updated,
                "actor": str((c.get("user") or {}).get("login") or ""),
                "revision": digest,
                "summary": "baseline; nothing before this is alerted",
            }
        )

    def _ledger_for(self, row: dict):
        """The ledger a row belongs to:  issue-subject rows live ONLY in the
        separate issue store, everything else in the PR store."""
        if row.get("subject") == "issue":
            if self.issue_store is None:
                raise ValueError("issue ledger unconfigured")
            return self.issue_store
        return self.store

    def _dispatch(self, owner: dict, event: dict, link: str, counts: dict, epoch: int = 0) -> None:
        if self._comment_lane_fenced(event):
            return
        repo, number = event["repo"], event["number"]
        is_issue = event.get("subject") == "issue"
        if is_issue and self.issue_store is None:
            # An issue event with no issue ledger can be neither dispatched
            # nor held durably:  count it and leave the hint held upstream.
            counts["held"] += 1
            return
        if not self._effects_allowed(
            repo, number, subject="issue" if is_issue else "pr"
        ):
            return
        if is_issue:
            # The wake target and the message destination are ONE identity:
            # both come from this cycle's exact binder result, never from
            # provider origin_* claims read separately.
            origin = self.issue_origins_this_cycle.get((repo, number))
            if not isinstance(origin, dict):
                origin = {}
            session = str(origin.get("session") or "")
            letter = str(origin.get("letter") or "")
            generation = origin.get("generation")
            seat = str(origin.get("seat") or "")
        else:
            session = str(owner.get("origin_session_claim") or "")
            letter = str(owner.get("origin_letter_claim") or "")
            generation = owner.get("origin_generation_claim")
            seat = seat_for(str(owner.get("seat") or ""), self.seat_map)
        head_note = "" if event["head_matches"] else " (against an older head)"
        topic = "%s#%s: %s%s" % (repo, number, event["summary"], head_note)
        base = dict(
            event,
            epoch=epoch,
            status="held",
            wake_id=None,
            reason=None,
            message_id=event.get("message_id"),
            link=link or event.get("link") or "",
        )
        if not session or not letter or type(generation) is not int or generation < 1:
            # Without all three claims there is nothing for the router to
            # check against the live row:  hold visibly, never guess.
            base["reason"] = "no-origin-claim"
        elif not seat:
            base["reason"] = "no-seat-mapping"
        else:
            if type(base.get("message_id")) is not int or base["message_id"] <= 0:
                if self.message_poster is None:
                    base["reason"] = "operator-message-unconfigured"
                    self._ledger_for(base).append(base)
                    counts["held"] += 1
                    return
                base.update(status="message-pending")
                self._ledger_for(base).append(base)
                self._post_message(owner, base, counts)
                return
            wake_id = (
                wakes.wake_id_for_issue(repo, number, event["kind"], event["event_id"], epoch)
                if is_issue
                else wakes.wake_id_for(repo, number, event["kind"], event["event_id"], epoch)
            )
            try:
                if is_issue:
                    self.router.submit_issue(
                        seat,
                        wake_id,
                        session,
                        letter,
                        generation,
                        "%s#%s" % (repo, number),
                        topic,
                        link,
                        message_id=base["message_id"],
                        source_event=source_identity(base),
                    )
                else:
                    self.router.submit(
                        seat,
                        wake_id,
                        session,
                        letter,
                        generation,
                        "%s#%s" % (repo, number),
                        topic,
                        link,
                        message_id=base["message_id"],
                        source_event=source_identity(base),
                    )
            except wakes.WakeRetryable:
                # The wake may or may not have landed:  reconcile by receipt
                # next cycle (same wake id), never a new one.
                base.update(status="submit-pending", wake_id=wake_id, reason=None)
            except wakes.WakeError as exc:
                base["reason"] = "wake-submit: %s" % exc
            else:
                base.update(status="submitted", wake_id=wake_id, reason=None)
                counts["submitted"] += 1
        if base["status"] == "held":
            counts["held"] += 1
        self._ledger_for(base).append(base)

    def _settle(self, counts: dict, owners: list) -> None:
        for pending in self.store.pending():
            self._settle_row(counts, owners, pending)

    def _settle_scoped(self, counts: dict, owners: list, repo: str, number: int) -> None:
        """Settlement bounded to ONE owner's pending rows.  A targeted
        reconcile settles exactly what it owns:  every other owner's rows,
        holds, watermarks and seeds are untouched, and nothing here claims
        or resets full-scan coverage."""
        for pending in self.store.pending():
            if pending["repo"] == repo and int(pending["number"]) == int(number):
                self._settle_row(counts, owners, pending)

    def _settle_row(self, counts: dict, owners: list, pending: dict) -> None:
        """One pending row's receipt reconciliation, exactly as the full
        settle pass performs it;  extracted so a scoped settle reuses the
        same per-row semantics instead of a parallel implementation."""
        now = self.clock()
        row = self._ledger_for(pending).get(
            pending["repo"], pending["number"], pending["kind"], pending["event_id"]
        )
        if row is None or (
            not row.get("wake_id") and row.get("status") not in events.MESSAGE_PENDING
        ):
            return
        owner = self._owner_row(owners, pending["repo"], pending["number"])
        # A recovery attempt's receipt is always read from the seat the
        # wake was actually submitted to (frozen on its row):  owner
        # rotation or disappearance never reroutes the lookup, and a
        # gone owner still permits safe frozen reconciliation.
        owner_seat = str(row.get("target_seat") or "") or seat_for(
            str((owner or {}).get("seat") or ""), self.seat_map
        )
        if not owner_seat:
            return
        age = now - self._row_epoch_seconds(row)
        if row["status"] in events.MESSAGE_PENDING:
            if row["status"] == "message-posting":
                # A crash after durable intent can have happened after send.
                self._finish(row, "held", "operator-message-outcome-unknown")
                counts["held"] += 1
            elif self._effects_allowed(
                row["repo"],
                row["number"],
                subject="issue" if row.get("subject") == "issue" else "pr",
            ):
                if row["status"] == "message-pending":
                    self._post_message(owner, row, counts)
                else:
                    self._dispatch(
                        owner or {},
                        row,
                        row.get("link") or "",
                        counts,
                        epoch=int(row.get("epoch") or 0),
                    )
            return
        if type(row.get("message_id")) is not int or row["message_id"] <= 0:
            self._finish(row, "held", "legacy-wake-without-message")
            counts["held"] += 1
            return
        try:
            receipt = self.router.receipt(owner_seat, row["wake_id"])
        except wakes.WakeRetryable:
            self._maybe_expire(row, age, counts)
            return
        except wakes.WakeError:
            return
        if receipt is None:
            # 404:  the router has no record.  For a submit-pending row
            # the submit was lost:  resubmit the SAME wake id (idempotent
            # if it did land).  For a submitted row the router's record
            # is gone past retention:  bounded by the deadline.
            if row["status"] == "submit-pending":
                if row.get("recovery") == RECOVERY_ATTEMPT:
                    # A recovery's next attempt is never resubmitted
                    # blind:  after an uncertain acceptance a missing
                    # (expired or removed) record cannot prove the wake
                    # did not run, and same-id dedupe is not permanent.
                    # Fail closed;  only a fresh explicit selection
                    # against proven evidence may retry, next epoch.
                    self._finish(row, "held", "recovery-receipt-uncertain")
                    counts["held"] += 1
                else:
                    self._resubmit(row, owner, counts)
            else:
                self._maybe_expire(row, age, counts)
            return
        terminal, outcome = self.router.receipt_terminal(receipt)
        if not terminal:
            self._maybe_expire(row, age, counts)
            return
        if outcome == "delivered":
            self._finish(row, "delivered", None)
            counts["delivered"] += 1
        elif outcome == "unknown":
            self._finish(row, "unknown", "transport-unproven")
            counts["unknown"] += 1
        else:
            reason = outcome[5:]
            if (
                reason in wakes.REDRIVE_REASONS
                and int(row.get("epoch") or 0) + 1 < MAX_EPOCHS
                # A recovery attempt (its marker rides the row through
                # every status) is bounded to the explicitly selected
                # attempt in every reconciliation state:  no automatic
                # fresh epoch, and never a target change from rotated
                # current claims.  It settles held with the receipt's
                # reason and waits for a fresh explicit selection.
                and row.get("recovery") != RECOVERY_ATTEMPT
            ):
                if self._comment_lane_fenced(row):
                    return
                if not self._effects_allowed(
                    row["repo"],
                    row["number"],
                    subject="issue" if row.get("subject") == "issue" else "pr",
                ):
                    return
                # The wake record is certainly undelivered;  a fresh wake
                # id (next epoch) may retry while the origin chat is away.
                event = {
                    k: row.get(k)
                    for k in (
                        "repo",
                        "number",
                        "kind",
                        "event_id",
                        "subject",
                        "actor",
                        "head",
                        "current_head",
                        "head_matches",
                        "summary",
                        "message_id",
                        "link",
                        "revision",
                        "observed_updated",
                    )
                }
                self._dispatch(
                    owner or {}, event, "", counts, epoch=int(row.get("epoch") or 0) + 1
                )
                counts["redriven"] += 1
            else:
                self._finish(row, "held", reason)
                counts["held"] += 1

    def _targeted_hold(self, repo: str, number: int, reason: str) -> None:
        """A targeted reconcile's visible hold:  additive, bounded, cleared
        by the next successful reconcile of the same owner.  Never written
        into the cycle-owned binding_holds."""
        if len(self.targeted_holds) < 1000:
            self.targeted_holds[repo + "#" + str(number)] = reason

    def reconcile_hint(self, hint: dict) -> dict:
        """Execute ONE queued hint.  Runs ONLY on the single review-alerts
        worker thread, serialized with the poll cycle by construction.

        A hint carries a target, never authority:  protected ownership and
        origin binding are re-read fresh AT EXECUTION, the per-cycle
        capability probe gates nudges exactly as it gates polls, and
        settlement is scoped to the hinted owner.  No global census,
        coverage, binding-hold or watermark state is reset, cleared or
        claimed.  A hint whose census/binding reads fail is raised back to
        the caller unconsumed, so it is retried, never dropped.
        """
        counts = {
            "hint": str(hint.get("hint_kind") or ""),
            "subject": str(hint.get("subject") or ""),
            "events": 0,
            "seeded": 0,
            "submitted": 0,
            "delivered": 0,
            "held": 0,
            "unknown": 0,
            "read_errors": 0,
            "redriven": 0,
            "outcome": "",
            "disposition": "consumed",
        }
        subject = str(hint.get("subject") or "")
        repo = str(hint.get("repo") or "")
        number = hint.get("number")
        kind = str(hint.get("hint_kind") or "")
        if subject == "issue":
            if type(number) is int and number > CONSUMER_NUMBER_MAX:
                counts["held"] += 1
                counts["disposition"] = "held"
                counts["outcome"] = "held:number-out-of-contract"
                return counts
            return self._issue_hint(hint, counts)
        if subject != "pr" or repo not in self.repos or type(number) is not int or number <= 0:
            counts["outcome"] = "refused"
            return counts
        if number > CONSUMER_NUMBER_MAX:
            # Typed boundary refusal:  parseable by the mapper, but outside
            # every downstream consumer contract.  Durable, visible, held -
            # retried only by a NEW hint under a revised contract.
            self._targeted_hold(repo, number, "number-out-of-contract")
            counts["held"] += 1
            counts["disposition"] = "held"
            counts["outcome"] = "held:number-out-of-contract"
            return counts
        if self.cancelled():
            counts["outcome"] = "cancelled"
            return counts
        # The capability probe gates nudge-driven dispatch exactly as it
        # gates the poll:  a router rollback between cycles must stop both.
        # A refusal raises:  the hint stays queued and is retried, never
        # silently dropped and never dispatched against an unproven router.
        if self.gate_seat:
            self.router.check_require_pr_support(self.gate_seat, probe_id=str(int(self.clock())))
        owners = self.owner_provider() if self.owner_provider else load_owner_rows(self.owners_path)
        owner = self._owner_row(owners, repo, number)
        if owner is None:
            # Durable, visible, per-hint:  the inbox row itself becomes the
            # held evidence (not only the volatile targeted_holds view), and
            # a later poll cycle that now sees the owner requeues it
            # (retry-on-ownership-change) - a new hint for the same target
            # reconciles immediately and independently.
            self._targeted_hold(repo, number, "owner-unknown")
            counts["held"] += 1
            counts["disposition"] = "held"
            counts["outcome"] = "held:owner-unknown"
            return counts
        if self.origin_binder is not None:
            try:
                origin = self.origin_binder(repo, number)
                self.origins_this_cycle[(repo, number)] = origin
                self.bound_this_cycle.add((repo, number))
            except Exception as exc:  # noqa: BLE001 - a bind failure holds, never guesses
                self._targeted_hold(repo, number, type(exc).__name__)
                counts["held"] += 1
                counts["disposition"] = "held"
                counts["outcome"] = "held:bind:%s" % type(exc).__name__
                return counts
        self.targeted_holds.pop(repo + "#" + str(number), None)
        if kind == "pr_comment":
            if not self.pr_comment_alerts:
                # Lane disabled (the default):  hold the hint DURABLY,
                # never consume it - the inbox row carries the full
                # evidence until the lane is explicitly enabled;  no
                # canonical read, no ledger write, no dispatch.
                self._targeted_hold(repo, number, "pr-comment-alerts-disabled")
                counts["held"] += 1
                counts["disposition"] = "held"
                counts["outcome"] = "held:pr-comment-alerts-disabled"
                return counts
            self._comment_event(owner, hint, counts)
        elif kind in ("pr", "pr_review", "pr_review_comment", "check_run", "check_suite", "merge_group"):
            self._scan_owner(owner, counts)
        else:
            counts["outcome"] = "refused"
            return counts
        if counts["read_errors"]:
            # A transient read failure is NEVER consumed:  with no poll
            # conversation-comment read, consuming here would lose a
            # requested comment for good.  The worker requeues with
            # bounded backoff;  the row settles only when its canonical
            # reads and settlement succeed, or it becomes a durable hold.
            raise RetryHint("GithubReadError")
        if counts["disposition"] == "held":
            return counts
        self._settle_scoped(counts, owners, repo, number)
        counts["outcome"] = "reconciled"
        return counts

    def _comment_event(self, owner: dict, hint: dict, counts: dict) -> None:
        """Complete a PR conversation-comment hint from canonical evidence.

        The hint is only a nudge:  it proves an authenticated source
        observed something about a comment, never the comment's canonical
        existence, content or current state.  A numeric source_ref names
        the candidate comment id;  the canonical single read proves the
        comment, its exact bytes and its target membership (its issue_url
        must BE this PR's number - a numeric id alone never proves
        membership).  Added vs edited derives from canonical state
        (created vs updated stamps), never from the hint's action field:
        an "edited" or "deleted" action on a comment that exists and is
        new to us is an add, and a "created" action on a comment already
        recorded reconciles by ledger key.  A 404 is held as uncertainty
        (missing OR transferred/deleted without access - it never proves
        deletion), an evidence gap is held, a transport failure retries.
        The hint's action stays in the inbox row's evidence only."""
        repo, number = owner["repo"], int(owner["number"])
        try:
            pr = self.gh.pull(repo, number)
        except github_read.GithubReadError:
            counts["read_errors"] += 1
            return
        if str(pr.get("state") or "") != "open":
            # The PR itself is settled:  nothing to alert on, and nothing
            # to hold - the poll's closed-state discipline owns this row.
            return
        comment_ref = str(hint.get("source_ref") or "")
        if (
            not comment_ref.isdigit()
            or not 1 <= int(comment_ref) <= github_read.COMMENT_ID_MAX
        ):
            # A non-numeric or out-of-range ref names no canonical comment:
            # held with the hint's evidence, never guessed from.
            self._targeted_hold(repo, number, "comment-ref-invalid")
            counts["held"] += 1
            counts["disposition"] = "held"
            counts["outcome"] = "held:comment-ref-invalid"
            return
        comment_id = int(comment_ref)
        try:
            evidence = self.gh.issue_comment(repo, comment_id, expected_number=number)
        except comment_evidence.CommentUnverified:
            # Malformed, mismatched or target-foreign canonical evidence:
            # held, never laundered into an event.
            self._targeted_hold(repo, number, "comment-evidence-unverified")
            counts["held"] += 1
            counts["disposition"] = "held"
            counts["outcome"] = "held:comment-evidence-unverified"
            return
        except github_read.GithubReadError as exc:
            if exc.github_status == 404:
                # 404 = missing OR transferred/deleted without access:  held
                # uncertainty, never deletion, never an add/edit guess.
                self._targeted_hold(repo, number, "comment-not-found-uncertain")
                counts["held"] += 1
                counts["disposition"] = "held"
                counts["outcome"] = "held:comment-not-found-uncertain"
                return
            counts["read_errors"] += 1
            return
        current_head = str((pr.get("head") or {}).get("sha") or "")
        # The SAME shared reconciliation the poll scan runs:  identical
        # canonical state yields an identical outcome in either arrival
        # order, and one observed revision alerts exactly once.
        row = {
            "id": evidence["id"],
            "user": {"login": evidence["login"]},
            "created_at": evidence["created_at"],
            "updated_at": evidence["updated_at"],
            "body": evidence["body"],
        }
        watermark = int(self.store.cursor(repo, number).get("issue_comment_id") or 0)
        prior, seen = detect.comment_revision_state(self.store.comment_rows(repo, number))
        event = detect.reconcile_comment(
            repo, number, row, watermark, prior.get(evidence["id"]), current_head,
            seen=seen.get(evidence["id"]),
        )
        if event is None:
            return
        counts["events"] += 1
        self._dispatch(owner, event, str(pr.get("html_url") or ""), counts)

    def _hold_issue_hint(self, hint: dict, counts: dict) -> dict:
        """A standalone-issue hint becomes a VISIBLE held row in the separate
        issue ledger, with its full evidence (source event id, ref, time,
        action, delivery id).  This is the path when the issue lane is
        unwired (no census/binder/ledger), the hint names an issue outside
        the current census, or binding fails:  no owner is inferred from
        assignees, labels, titles or letters, and nothing is dispatched."""
        counts["held"] += 1
        # The INBOX row carries the unresolved evidence (durable, complete,
        # never compacted while held);  the issue ledger keeps only a
        # diagnostic copy.  The hint stays HELD, not consumed:  unresolved
        # evidence must not age out under consumed-row retention.
        counts["disposition"] = "held"
        counts["outcome"] = "held:issue-owner-unknown"
        if self.issue_store is None:
            return counts
        repo = str(hint.get("repo") or "")
        number = hint.get("number")
        kind = {
            "issue": "issue_opened",
            "issue_comment_issue": "issue_comment_added",
        }.get(str(hint.get("hint_kind") or ""))
        source_id = str(hint.get("source_ref") or "") or str(hint.get("source_event_id") or "")
        if kind is None or not source_id or repo not in self.repos:
            return counts
        if type(number) is not int or number <= 0:
            return counts
        # Diagnostic identity ONLY:  a hint's source_ref is evidence, never
        # a canonical comment id, so no "i<...>" event identity is minted
        # from it (and nothing is truncated into one).  The digest of the
        # full delivery/event/ref triple is deterministic (dedupe) and
        # collision-safe;  the row is labeled diagnostic so no future
        # reader mistakes it for a canonical event.
        event_id = "diag:%s" % hashlib.sha256(
            (
                str(hint.get("delivery_id") or "")
                + "\x00"
                + str(hint.get("source_event_id") or "")
                + "\x00"
                + str(hint.get("source_ref") or "")
            ).encode()
        ).hexdigest()[:32]
        if self.issue_store.get(repo, number, kind, event_id):
            return counts
        self.issue_store.append(
            {
                "repo": repo,
                "number": number,
                "kind": kind,
                "event_id": event_id,
                "diagnostic": True,
                "subject": "issue",
                "source_time": str(hint.get("source_time") or "") or None,
                "source_ref": str(hint.get("source_ref") or "") or None,
                "source_action": str(hint.get("source_action") or "") or None,
                "delivery_id": str(hint.get("delivery_id") or "") or None,
                "summary": "issue activity held: no ownership contract",
                "status": "held",
                "reason": "issue-owner-unknown",
            }
        )
        return counts

    def _issue_hint(self, hint: dict, counts: dict) -> dict:
        """Execute ONE standalone-issue hint through the canonical path.

        Same disciplines as the PR branch:  the hint is only a nudge;
        census membership and origin binding are re-read fresh AT
        EXECUTION, the require_issue capability probe gates dispatch, and
        every canonical decision (opening vs baseline, add vs edit) comes
        from the canonical reads through the SAME shared reconciliation the
        poll uses, so hint-first and poll-first converge.  Anything outside
        the wired lane (no census, no ledger, unknown or unbindable issue)
        falls back to the durable held-evidence row, never a guess."""
        if self.issue_owner_provider is None or self.issue_store is None:
            return self._hold_issue_hint(hint, counts)
        repo = str(hint.get("repo") or "")
        number = hint.get("number")
        kind = str(hint.get("hint_kind") or "")
        if (
            repo not in self.repos
            or type(number) is not int
            or number <= 0
            or kind not in ("issue", "issue_comment_issue")
        ):
            return self._hold_issue_hint(hint, counts)
        if self.cancelled():
            counts["outcome"] = "cancelled"
            return counts
        # The require_issue probe gates hint-driven issue dispatch exactly
        # as it gates the poll:  a refusal RAISES, so the hint stays queued
        # and is retried, never dispatched against an unproven router.
        target = self.issue_probe_target
        if callable(target):
            # Same fresh-read contract as the poll gate:  a callable target
            # is resolved at execution;  a failure is a WakeError so the
            # hint stays queued and retries, never dispatched unproven.
            try:
                target = target()
            except Exception as exc:  # noqa: BLE001
                raise wakes.WakeError("issue probe target unavailable") from exc
        if not isinstance(target, dict):
            raise wakes.WakeError("issue probe target unconfigured")
        self.router.check_required_issue_support(
            str(target.get("seat") or ""),
            probe_id=str(int(self.clock())),
            session=str(target.get("session") or ""),
            letter=str(target.get("letter") or ""),
            generation=target.get("generation"),
            require_issue=str(target.get("require_issue") or ""),
        )
        self.issue_store.record_rollout()
        try:
            rows = [row for row in (self.issue_owner_provider() or []) if isinstance(row, dict)]
        except Exception as exc:  # noqa: BLE001
            # A census failure raises back to the worker UNCONSUMED (the
            # hint retries with backoff), as a typed wake-layer error -
            # never a raw provider exception escaping the worker loop.
            raise wakes.WakeError("issue census unavailable") from exc
        row = None
        for candidate in rows:
            if (
                str(candidate.get("repo") or "") == repo
                and type(candidate.get("number")) is int
                and candidate["number"] == number
            ):
                row = candidate
                break
        if row is None:
            return self._hold_issue_hint(hint, counts)
        if self.issue_origin_binder is not None:
            try:
                origin = self.issue_origin_binder.bind(repo, number)
                self.issue_origins_this_cycle[(repo, number)] = origin
                self.bound_issues_this_cycle.add((repo, number))
            except Exception:  # noqa: BLE001 - a bind failure holds, never guesses
                return self._hold_issue_hint(hint, counts)
        self.targeted_holds.pop(repo + "#" + str(number), None)
        if kind == "issue":
            # The opening:  a full canonical scan (the issue_opened decision
            # is grounded in the canonical created_at vs the rollout
            # boundary, shared with the poll).
            self._scan_issue(row, counts)
        else:
            self._issue_comment_event(row, hint, counts)
        if counts["read_errors"]:
            # A transient read failure is NEVER consumed:  requeue with
            # bounded backoff, same as the PR branch.
            raise RetryHint("GithubReadError")
        if counts["disposition"] == "held":
            return counts
        for pending in self.issue_store.pending():
            if pending["repo"] == repo and int(pending["number"]) == number:
                self._settle_row(counts, rows, pending)
        counts["outcome"] = "reconciled"
        return counts

    def _issue_comment_event(self, row: dict, hint: dict, counts: dict) -> None:
        """Complete a standalone-issue comment hint from canonical evidence:
        the issue-ledger twin of _comment_event.  A numeric source_ref names
        the candidate comment id;  the canonical single read proves the
        comment and its target membership (its issue_url must BE this
        issue's number).  Added vs edited derives from canonical state
        through the shared reconciliation, never from the hint's action."""
        repo, number = row["repo"], int(row["number"])
        try:
            issue = self.gh.issue(repo, number)
        except github_read.GithubReadError:
            counts["read_errors"] += 1
            return
        if issue.get("kind") != "issue" or str(issue.get("state") or "") != "open":
            return
        comment_ref = str(hint.get("source_ref") or "")
        if (
            not comment_ref.isdigit()
            or not 1 <= int(comment_ref) <= github_read.COMMENT_ID_MAX
        ):
            self._targeted_hold(repo, number, "comment-ref-invalid")
            counts["held"] += 1
            counts["disposition"] = "held"
            counts["outcome"] = "held:comment-ref-invalid"
            return
        comment_id = int(comment_ref)
        try:
            evidence = self.gh.issue_comment(repo, comment_id, expected_number=number)
        except comment_evidence.CommentUnverified:
            self._targeted_hold(repo, number, "comment-evidence-unverified")
            counts["held"] += 1
            counts["disposition"] = "held"
            counts["outcome"] = "held:comment-evidence-unverified"
            return
        except github_read.GithubReadError as exc:
            if exc.github_status == 404:
                self._targeted_hold(repo, number, "comment-not-found-uncertain")
                counts["held"] += 1
                counts["disposition"] = "held"
                counts["outcome"] = "held:comment-not-found-uncertain"
                return
            counts["read_errors"] += 1
            return
        evidence_row = {
            "id": evidence["id"],
            "user": {"login": evidence["login"]},
            "created_at": evidence["created_at"],
            "updated_at": evidence["updated_at"],
            "body": evidence["body"],
        }
        watermark = int(self.issue_store.cursor(repo, number).get("comment_id") or 0)
        prior, seen = detect.comment_revision_state(
            self.issue_store.comment_rows(repo, number), kinds=ISSUE_COMMENT_KINDS
        )
        event = detect.reconcile_comment(
            repo, number, evidence_row, watermark, prior.get(evidence["id"]), "",
            kinds=ISSUE_COMMENT_KINDS, seen=seen.get(evidence["id"]),
        )
        if event is None:
            return
        event["subject"] = "issue"
        if self.issue_store.get(repo, number, event["kind"], event["event_id"]):
            return
        counts["events"] += 1
        self._dispatch(row, event, str(issue.get("html_url") or ""), counts)

    def recover_held(self, selected: list) -> dict:
        """One bounded post-repair pass over terminal-held events, only for
        the exact attempts the selection names.  Requires the protected
        owner provider and the live origin binder:  without fresh binding
        there is no protected identity to act on, so the pass refuses
        outright rather than skipping the check.

        A held row is redispatched only when ALL of these hold:
          - the selection names THIS exact event (repo, number, kind,
            event_id) AND the row still carries the selection's expected
            prior wake id, epoch and posted message id - the ledger row is
            the original envelope of what was submitted, and a row that
            moved on is never recovered on guesswork;
          - the pass has not reached MAX_RECOVERY_SENDS;
          - the hold reason is in RECOVERY_REASONS (the store-miss gap
            this pass exists for), and the next epoch stays below
            MAX_EPOCHS;
          - the AUTHORITATIVE prior receipt, fetched now from the router
            by the row's exact wake id, exists and is readable (an absent
            or expired-past-retention record, a read failure, or an
            unknown or delivered outcome all fail closed), proves explicit
            delivered IS false with the same failure code, and its pick
            identity (seat, session, letter, generation) matches what the
            redispatch would use NOW from the current owner claims.  The
            receipt carries no require_pr or message_id, so PR and message
            provenance come from the validated ledger row alone;  the
            receipt proves outcome and chat target, nothing more;
          - the origin still binds NOW (fresh resolve + bind-pr against
            the live registry, exactly as a cycle does) and effects are
            allowed.
        The exact next attempt (deterministic wake id at the next epoch,
        same posted message id) is persisted as submit-pending BEFORE the
        router is called, marked RECOVERY_ATTEMPT with its dispatch target
        frozen on the row (target_seat/session/letter/generation - current
        owner claims may rotate later;  provenance never does).  Ordinary
        reconciliation then owns it:  a receipt settles it as usual, but a
        MISSING receipt is never answered with a blind resubmit of the
        same id - after an uncertain acceptance that can re-execute a wake
        that already ran - and fails closed to held
        recovery-receipt-uncertain, retriable only by a fresh explicit
        selection against proven evidence.  The settle pass's own
        delivered-is-False semantics are untouched, and REDRIVE_REASONS is
        not expanded.  Append-only:  no ledger row is rewritten."""
        if self.owner_provider is None or self.origin_binder is None:
            raise ValueError(
                "held recovery requires the protected owner provider"
                " and the live origin binder"
            )
        counts = {
            "candidates": 0,
            "recovered": 0,
            "skipped": 0,
            "mismatched": 0,
            "unmatched": 0,
            "submitted": 0,
            "submit-pending": 0,
            "held": 0,
        }
        owners = self.owner_provider()
        wanted = {(e[0], e[1], e[2], e[3]): e for e in selected}
        seen = set()
        rows = sorted(
            self.store.held(),
            key=lambda r: (r["repo"], int(r["number"]), r["kind"], str(r["event_id"])),
        )
        for row in rows:
            repo, number = row["repo"], int(row["number"])
            key = (repo, number, row["kind"], str(row.get("event_id")))
            expect = wanted.get(key)
            if expect is None:
                continue
            seen.add(key)
            reason = str(row.get("reason") or "")
            old_wake_id = str(row.get("wake_id") or "")
            epoch = int(row.get("epoch") or 0)
            message_id = row.get("message_id")
            if (
                repo not in self.repos
                or reason not in RECOVERY_REASONS
                or old_wake_id != expect[4]
                or epoch != expect[5]
                or epoch + 1 >= MAX_EPOCHS
                or message_id != expect[6]
                or type(message_id) is not int
                or message_id <= 0
            ):
                # The ledger no longer shows the exact attempt the
                # selection names:  refuse rather than recover a different
                # attempt on guesswork.
                counts["mismatched"] += 1
                continue
            if counts["recovered"] >= MAX_RECOVERY_SENDS:
                break
            counts["candidates"] += 1
            owner = self._owner_row(owners, repo, number)
            if owner is None:
                counts["skipped"] += 1
                continue
            owner_seat = seat_for(str(owner.get("seat") or ""), self.seat_map)
            if not owner_seat:
                counts["skipped"] += 1
                continue
            # The authoritative outcome evidence:  the router's own record
            # of THIS wake, read now.  Only an explicit delivered:false
            # with the same code proves certain-undelivered;  a missing,
            # expired, unreadable, unknown or delivered record fails
            # closed.
            try:
                receipt = self.router.receipt(owner_seat, old_wake_id)
            except wakes.WakeError:
                counts["skipped"] += 1
                continue
            if not isinstance(receipt, dict):
                counts["skipped"] += 1
                continue
            session = str(owner.get("origin_session_claim") or "")
            letter = str(owner.get("origin_letter_claim") or "")
            generation = owner.get("origin_generation_claim")
            if (
                receipt.get("delivered") is not False
                or str(receipt.get("wake_id") or "") != old_wake_id
                or str(receipt.get("code") or receipt.get("reason") or "") != reason
                or str(receipt.get("seat") or "") != owner_seat
                or not session
                or not letter
                or type(generation) is not int
                or generation < 1
                or str(receipt.get("session_id") or "") != session
                or str(receipt.get("picked_letter") or "") != letter
                or receipt.get("picked_generation") != generation
            ):
                counts["skipped"] += 1
                continue
            try:
                if self.cancelled():
                    counts["skipped"] += 1
                    continue
                origin = self.origin_binder(repo, number)
                self.origins_this_cycle[(repo, number)] = origin
                self.bound_this_cycle.add((repo, number))
            except Exception:  # noqa: BLE001 - a bind failure holds, never guesses
                counts["skipped"] += 1
                continue
            if not self._effects_allowed(repo, number):
                counts["skipped"] += 1
                continue
            # Persist the exact next attempt BEFORE crossing the router,
            # marked, with its dispatch target frozen on the row.
            next_epoch = epoch + 1
            next_wake = wakes.wake_id_for(
                repo, number, row["kind"], str(row.get("event_id")), next_epoch
            )
            intent = {
                k: row.get(k)
                for k in (
                    "repo",
                    "number",
                    "kind",
                    "event_id",
                    "actor",
                    "head",
                    "current_head",
                    "head_matches",
                    "summary",
                )
            }
            intent.update(
                ts=None,
                epoch=next_epoch,
                status="submit-pending",
                wake_id=next_wake,
                reason=None,
                recovery=RECOVERY_ATTEMPT,
                message_id=message_id,
                link=row.get("link") or "",
                target_seat=owner_seat,
                target_session=session,
                target_letter=letter,
                target_generation=generation,
            )
            self.store.append(intent)
            head_note = "" if row.get("head_matches") else " (against an older head)"
            topic = "%s#%s: %s%s" % (repo, number, row.get("summary") or "", head_note)
            try:
                self.router.submit(
                    owner_seat,
                    next_wake,
                    session,
                    letter,
                    generation,
                    "%s#%s" % (repo, number),
                    topic,
                    row.get("link") or "",
                    message_id=message_id,
                )
            except wakes.WakeRetryable:
                # Unsettled:  the marked intent row stands;  ordinary
                # reconciliation reads the receipt and never blind-
                # resubmits this attempt.
                counts["submit-pending"] += 1
            except wakes.WakeError as exc:
                self._finish(intent, "held", "wake-submit: %s" % exc)
                counts["held"] += 1
            else:
                self._finish(intent, "submitted", None)
                counts["submitted"] += 1
            counts["recovered"] += 1
        counts["unmatched"] = len(wanted) - len(seen)
        return counts

    def _post_message(self, owner: dict, row: dict, counts: dict) -> None:
        if self._comment_lane_fenced(row):
            return
        is_issue = row.get("subject") == "issue"
        if not self._effects_allowed(
            row["repo"], row["number"], subject="issue" if is_issue else "pr"
        ):
            return
        if self.message_poster is None:
            self._finish(row, "held", "operator-message-unconfigured")
            counts["held"] += 1
            return
        origin = (
            self.issue_origins_this_cycle if is_issue else self.origins_this_cycle
        ).get((row["repo"], row["number"]))
        if not isinstance(origin, dict) or not origin:
            self._finish(row, "held", "no-origin-claim")
            counts["held"] += 1
            return
        # Persist intent before crossing the existing Operator post boundary.
        posting = dict(row, status="message-posting", reason=None)
        self._ledger_for(row).append(posting)
        try:
            posting_owner = dict(
                owner or {},
                _review_origin=origin,
            )
            message_id = self.message_poster(posting_owner, row, row.get("link") or "")
            if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
                raise ValueError("invalid-message-id")
        except messages.MessageDestinationUnresolved:
            self._finish(posting, "message-pending", "operator-message-destination-unresolved")
            return
        except messages.MessageRetryable:
            self._finish(posting, "message-pending", "operator-message-unavailable")
            return
        except Exception:
            self._finish(posting, "held", "operator-message-outcome-unknown")
            counts["held"] += 1
            return
        posted = dict(posting, status="message-posted", message_id=message_id)
        self._ledger_for(row).append(posted)
        self._dispatch(
            owner or {},
            posted,
            posted.get("link") or "",
            counts,
            epoch=int(posted.get("epoch") or 0),
        )

    def _comment_lane_fenced(self, row: dict) -> bool:
        """Default-off gate, effect side:  while the generic PR
        conversation-comment lane is disabled (the default), an EXISTING
        generic-comment ledger row produces no new effect - no operator
        message post, no wake submit, resubmit or redrive.  The row keeps
        its durable state and resumes when the lane is explicitly enabled.
        Read-only receipt reconciliation (delivered/unknown/expiry and
        unknown-outcome finalization) is never fenced.  Issue-lane kinds
        are disjoint, so the kind check alone identifies the lane."""
        return not self.pr_comment_alerts and row.get("kind") in (
            "pr_comment_added",
            "pr_comment_edited",
        )

    def _effects_allowed(self, repo, number, subject="pr"):
        if self.cancelled():
            return False
        if subject == "issue":
            # Fail CLOSED:  without a binder there is no protected
            # association proof at all, and without THIS cycle's bound
            # coverage (probe gate passed, bind succeeded) no new issue
            # effect fires - dispatch, pending message posting, resubmits
            # and redrives are all fenced.  Read-only receipt
            # reconciliation continues ungated.
            return (
                self.issue_origin_binder is not None
                and (repo, number) in self.bound_issues_this_cycle
            )
        return self.origin_binder is None or (repo, number) in self.bound_this_cycle

    def _resubmit(self, row: dict, owner: dict | None, counts: dict) -> None:
        """A submit-pending wake with no router record:  resubmit the SAME
        wake id.  If the original did land, the router answers its existing
        record (idempotent), so no duplicate delivery results."""
        if self._comment_lane_fenced(row):
            return
        is_issue = row.get("subject") == "issue"
        if owner is None or not self._effects_allowed(
            row["repo"], row["number"], subject="issue" if is_issue else "pr"
        ):
            return
        if is_issue:
            origin = self.issue_origins_this_cycle.get((row["repo"], row["number"]))
            if not isinstance(origin, dict):
                origin = {}
            session = str(origin.get("session") or "")
            letter = str(origin.get("letter") or "")
            generation = origin.get("generation")
            seat = str(origin.get("seat") or "")
        else:
            session = str(owner.get("origin_session_claim") or "")
            letter = str(owner.get("origin_letter_claim") or "")
            generation = owner.get("origin_generation_claim")
            seat = seat_for(str(owner.get("seat") or ""), self.seat_map)
        if not session or not letter or type(generation) is not int or generation < 1:
            self._finish(row, "held", "no-origin-claim")
            return
        head_note = "" if row.get("head_matches") else " (against an older head)"
        topic = "%s#%s: %s%s" % (row["repo"], row["number"], row.get("summary") or "", head_note)
        try:
            if is_issue:
                self.router.submit_issue(
                    seat,
                    row["wake_id"],
                    session,
                    letter,
                    generation,
                    "%s#%s" % (row["repo"], row["number"]),
                    topic,
                    row.get("link") or "",
                    message_id=row["message_id"],
                    source_event=source_identity(row),
                )
            else:
                self.router.submit(
                    seat,
                    row["wake_id"],
                    session,
                    letter,
                    generation,
                    "%s#%s" % (row["repo"], row["number"]),
                    topic,
                    row.get("link") or "",
                    message_id=row["message_id"],
                    source_event=source_identity(row),
                )
        except wakes.WakeRetryable:
            return  # still unsettled;  next cycle reconciles
        except wakes.WakeError as exc:
            self._finish(row, "held", "wake-submit: %s" % exc)
            return
        self._finish(row, "submitted", None)

    def _maybe_expire(self, row: dict, age: float, counts: dict) -> None:
        if age > RECEIPT_DEADLINE_S:
            self._finish(row, "held", "receipt-lost")
            counts["held"] += 1

    def _finish(self, row: dict, status: str, reason) -> None:
        self._ledger_for(row).append(dict(row, status=status, reason=reason))

    @staticmethod
    def _row_epoch_seconds(row: dict) -> float:
        try:
            return calendar.timegm(time.strptime(str(row.get("ts") or ""), "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, OverflowError):
            return time.time()

    def owned_targets(self) -> set:
        """The (repo, number) set the CURRENT protected owner census names
        for this service's repos.  Read fresh at call time;  the worker uses
        it after a poll cycle to requeue owner-unknown/bind held rows whose
        ownership has since appeared.  A provider failure propagates:  no
        census, no requeue - held evidence is never revived on a guess."""
        owners = self.owner_provider() if self.owner_provider else load_owner_rows(self.owners_path)
        return {
            (str(row["repo"]), int(row["number"]))
            for row in owners
            if str(row.get("repo") or "") in self.repos and type(row.get("number")) is int
        }

    @staticmethod
    def _owner_row(owners: list, repo: str, number: int) -> dict | None:
        for row in owners:
            if row["repo"] == repo and int(row["number"]) == int(number):
                return row
        return None

    def status_snapshot(self) -> dict:
        """The operationally visible surface:  counts, every held event with
        its reason, and every pending event.  Written to status.json each
        cycle so a stuck alert is found here, not by reading the log."""
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": self.store.counts(),
            "coverage": (
                self.coverage_provider() if self.coverage_provider is not None else "unknown"
            ),
            "binding_holds": dict(self.binding_holds),
            "targeted_holds": dict(self.targeted_holds),
            "issue_held": [
                {
                    k: r.get(k)
                    for k in (
                        "repo",
                        "number",
                        "kind",
                        "event_id",
                        "reason",
                        "source_time",
                        "source_ref",
                        "delivery_id",
                        "ts",
                    )
                }
                for r in (self.issue_store.held() if self.issue_store is not None else [])
            ],
            "held": [
                {
                    k: r.get(k)
                    for k in (
                        "repo",
                        "number",
                        "kind",
                        "event_id",
                        "epoch",
                        "reason",
                        "ts",
                        "message_id",
                    )
                }
                for r in self.store.held()
            ],
            "pending": [
                {
                    k: r.get(k)
                    for k in ("repo", "number", "kind", "event_id", "epoch", "status", "ts")
                }
                for r in self.store.pending()
            ],
        }


def _write_status(state_dir: str, snapshot: dict) -> None:
    tmp = os.path.join(state_dir, ".status.json.tmp")
    final = os.path.join(state_dir, "status.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ
    owners_path = str(env.get("RA_OWNERS_PATH") or "").strip()
    state_dir = str(env.get("RA_STATE_DIR") or "").strip()
    selftest_seat = str(env.get("RA_SELFTEST_SEAT") or "").strip()
    token_file = str(env.get("RA_SERVICE_TOKEN_FILE") or "").strip()
    wake_key_file = str(env.get("RA_WAKE_KEY_FILE") or "").strip()
    missing = [
        name
        for name, value in (
            ("RA_OWNERS_PATH", owners_path),
            ("RA_STATE_DIR", state_dir),
            ("RA_SELFTEST_SEAT", selftest_seat),
            ("RA_SERVICE_TOKEN_FILE", token_file),
            ("RA_WAKE_KEY_FILE", wake_key_file),
        )
        if not value
    ]
    if missing:
        print("required: %s" % ", ".join(missing), file=sys.stderr)
        return 2
    os.makedirs(state_dir, mode=0o700, exist_ok=True)

    # Single writer:  one instance per state dir, or none.
    lock_fd = os.open(os.path.join(state_dir, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another review-alerts instance holds this state dir", file=sys.stderr)
        return 0

    router = wakes.WakeClient(
        str(env.get("RA_ROUTER_URL") or "http://127.0.0.1:8794"),
        lambda: _credential_from_file(wake_key_file),
    )
    # Rollout gate:  never send a real wake to a router without hold semantics.
    router.check_required_session_support(selftest_seat, probe_id=str(int(time.time())))
    gh = github_read.ServiceReadClient(
        str(env.get("RA_RELAY_URL") or "http://127.0.0.1:8471"),
        lambda: _credential_from_file(token_file),
    )
    repos = {r.strip() for r in str(env.get("RA_REPOS") or "").split(",") if r.strip()}
    if not repos:
        print(
            "RA_REPOS must name the qualified repo allowlist (no fleet-wide default)",
            file=sys.stderr,
        )
        return 2
    store = events.EventStore(os.path.join(state_dir, "events.jsonl"))
    service = AlertService(
        store, gh, router, owners_path, seat_map_from(env), repos=repos, gate_seat=selftest_seat
    )
    try:
        recovery_selection = _recovery_selection(env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if recovery_selection:
        # Post-repair recovery:  one explicitly selected, bounded pass at
        # process start, before the first cycle, under the single-writer
        # lock.  With no selection there is no pass at all, and without
        # the protected owner provider and live origin binder the pass
        # refuses (this standalone wiring has neither).
        try:
            recovery = service.recover_held(recovery_selection)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"recovery": recovery}, sort_keys=True), flush=True)
    if "--once" in argv:
        counts = service.cycle()
        _write_status(state_dir, service.status_snapshot())
        print(json.dumps(counts, sort_keys=True))
        return 0
    interval = float(env.get("RA_INTERVAL_S") or 900)
    while True:
        try:
            counts = service.cycle()
            _write_status(state_dir, service.status_snapshot())
            print(json.dumps(counts, sort_keys=True), flush=True)
        except Exception as exc:  # a bad cycle must not kill the daemon
            print(json.dumps({"cycle_error": type(exc).__name__}), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
