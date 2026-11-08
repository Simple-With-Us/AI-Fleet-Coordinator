"""muster_approval: the approval engine of the helper's `--from-muster` entry.

This module is the security core of `fleet-ops-control.py --from-muster`.  It implements
`server-route-spec.md` version 5.3 for the approval-bearing request, the plan request, the
acknowledgement request and the trust-list chain request: the standard-input protocol, the check order
P0 to P3, Y1 to Y10, R1 to R3, K1 to K3 and C1 to C5, the canonical bytes, the eleven approval field
patterns, the ECDSA P-256 / SHA-256 DER signature check, the kernel clock check, the two-minute window,
the nonce ledger, the rate limits, signed plans, results, refusals and acknowledgements, and the verb
table.

THE CHAIN VERB.  `muster trust-list chain --yes` (standard input `{"protocol":"muster-helper/1",
"chain":[...]}`, 1 to 32 entries, at most 65,536 bytes) is the only thing that writes `muster-trust.json`.
It verifies a chain of owner-signed trust lists against the owner pin and history (`/etc/muster`, read at
P1) and the list the helper holds, all or none, and stores the newest entry as its canonical bytes, so the
file's SHA-256 is that entry's `entrySHA256` (spec 5.3, sections 8 and 12, C1 to C5).  `chain --plan`
stays `mode_not_allowed`.  Until a chain is accepted the route stays inert: with nothing stored every
approval answers `no_trust_list`.

NOT BUILT, in the chain verb: rule C (reopen), the use of the reopen record, the final-refusal store and
rotation.  A rule C entry is refused `chain_refused` with reason `reopen_unavailable` once checks 1 to 7
have passed, nothing is recorded as final, and `finalRefusals` is always `[]` (spec sections 8 and 17A).

What it does not do, by design:

  * It has no network access and starts no process.  Everything that touches the machine's services
    (the plan, the read, the run, the audit row, the one `ops-control.lock`) is handed in by the
    caller as a `backend` object, so this file can be tested without a service.
  * It never reads an environment variable and never opens a path that a request or the environment
    chose.  Where its state lives, where the pin lives, what time it is and what the kernel says of
    the clock come from a `MusterConfig`.  `production_config()` builds the production one from
    constants and the account database, never from the environment, and the forced command never
    passes any other.  A test builds its own `MusterConfig`.  The caller also empties the process
    environment before this file is loaded, because the libraries it imports read some variables of
    their own.

Dependencies: the standard library and `cryptography` (the system package, imported here and so only
in the `--from-muster` branch).  The `/etc/muster` checks are the sibling module `muster_trust_files`,
which the caller loads and hands in (this file never imports a sibling by name).

Every choice that the spec's section 0 settled (the five points first marked OPEN-1 to OPEN-5 in the
first slice, and the points BB's build raised) is one named constant or function below, in the block
`Choices of spec version 5`:

  OPEN-1  P1 reads `/etc/muster` once, the reopen record included, and keeps the bytes: `pin_unsafe`
          arises only there (`check_pin`).
  OPEN-2  Any fault in the helper key file or the state directory is the unsigned `helper_key_missing`
          at P0 (`UNSAFE_HELPER_KEY_CODE`).
  OPEN-3  A final ledger fragment that parses as a whole line gets its line feed and stays spent, and
          only an unparseable one is truncated (`classify_fragment`).
  OPEN-4  Signature and window are checked before `ops-control.lock`; under it the stored list is read
          again and `trust_list_changed` refuses if it moved; an approval gets an audit row only once
          Y8 has spent its nonce (`AUDIT_FROM_STEP`).
  OPEN-5  `muster-trust.json` is never created by a read: absent means nothing is held, and an empty,
          unparseable or unsafe file is `state_unsafe` (`load_stored_list`).  Only the chain verb
          writes it, as one whole file by temporary file and rename (`_Flow._store_chain`).

Behaviours that the spec leaves to the build, stated here so that no reader has to find them in code:

  * The rate step.  The `plan` and `current` classes carry nothing that can be authenticated, so they
    are counted at R1 and K1, before anything else, and anyone who can reach the entry can use their
    budgets.  The `yes` class is NOT counted at Y1: it is counted at Y8, under `ops-control.lock`,
    after the ledger has been read and the nonce found unused and before the nonce is appended
    (`Ledger.record`).  A bad signature, an unknown device, a field mismatch, an unsynchronised clock,
    an expired or early approval, a replay, a changed list and a busy lock therefore never use any
    budget, and a request over the cap or the spacing is refused `rate_limited` without spending its
    nonce.  Lock order is `ops-control.lock` first and `muster-rate.lock` second, everywhere.
  * The rate file.  An event dated after `now` (the clock was stepped back) is counted as made at `now`
    and the clamp is written back, so it cannot hold a key until the clock catches up.  An empty rate
    file is a fresh one.  The `yes` event is fsynced before the nonce is appended.
  * The ledger keeps its last 4,096 lines and every line whose approval expired less than seven days
    ago, whichever keeps more.  The clock alone never makes a nonce forgettable, so a clock that
    steps forward a day, or a year, and back cannot make an approval replayable.  The key is the
    `approvalNonce` alone: the same nonce from another device or another job is a replay.
  * Signatures.  A high-S ECDSA signature is accepted (it is as valid as its low-S twin); it cannot
    be used twice, because the nonce is spent.
  * A fault.  Before the nonce is appended (Y8c) it is `internal_error`, the append itself included
    (a write or an fsync that fails there is `internal_error`, not `state_unsafe`; a failed fsync can
    leave the nonce on disk, and the same approval is then `replayed_nonce`, so a phone re-plans and
    signs a new approval after any `internal_error` or `state_unsafe` from Y8).  After the append it is
    a signed result: `unknown` when the action had begun or may have (a command killed at its limit or
    by a signal, a Coolify GET sent and not answered, an exception once the run began), `failed` only
    when it provably never did (the planner refused, or the shared function refused before it ran
    anything, or the command exited with a definite failure).
  * Two things after the append are not faults.  A failed close of the ledger after its fsync loses
    nothing, because the append is durable, and a failed prune rewrite (of any kind) leaves the ledger
    as it was, because the rewrite is a temporary file and a rename, and the next approval tries it
    again.  Both are swallowed and the run proceeds exactly as if they had not failed.  Any other
    unexpected exception after the append, before or during the run, is a signed `failed` or `unknown`
    result with exactly one audit row; one before the append is a refusal.
  * The ledger's size limit is derived once, from the `yes` cap and the retention, with twice the room
    (`LEDGER_MAX_BYTES`, 4 MiB); a ledger over it is `ledger_corrupt`.
  * The environment.  OpenSSL reads its configuration when `hashlib` is imported, which happens at the
    top of the helper, before it empties the process environment; the lazy `urllib.request` import and
    the `os.environ.clear()` protect what loads afterwards (`cryptography`, `ssl`, the proxy and locale
    variables), not that.  So the forced command line must start the interpreter clean: `/usr/bin/env -i
    SSH_ORIGINAL_COMMAND="$SSH_ORIGINAL_COMMAND" /usr/bin/python3 -I ... --from-muster` (`env -i` alone
    would drop the one variable the route needs).
"""

from __future__ import annotations

import base64
import calendar
import contextlib
import ctypes
import datetime
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import select
import stat
import time

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

# MARK: - Constants

PROTOCOL = "muster-helper/1"
ROUTE = "from-muster"
VIA = "muster-ios"

STDIN_LIMIT = 4096
# Section 5: `muster trust-list chain --yes` alone may send up to this many bytes, in 1 to 32 entries.
CHAIN_STDIN_LIMIT = 65536
CHAIN_MIN_ENTRIES = 1
CHAIN_MAX_ENTRIES = 32
STDIN_TIMEOUT = 10.0
SSH_COMMAND_MAX = 256
# The same class as the helper's SSH_SAFE_RE (a test keeps the two equal).
SSH_SAFE_RE = re.compile(rf"[A-Za-z0-9 ._@-]{{1,{SSH_COMMAND_MAX}}}")
SAFE_WORD_RE = re.compile(r"[A-Za-z0-9._@-]{1,128}")

JOB_ID_RE = re.compile(r"[a-z0-9]{1,26}")
NONCE_RE = re.compile(r"[0-9a-f]{32}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DEVICE_ID_RE = re.compile(r"d-[0-9a-f]{24}")
TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SIGNATURE_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")
# The eleven fields of an approval document and the pattern each must match in full (section 6).
APPROVAL_PATTERNS = {
    "jobId": JOB_ID_RE,
    "jobNonce": NONCE_RE,
    "approvalNonce": NONCE_RE,
    "host": SAFE_WORD_RE,
    "action": SAFE_WORD_RE,
    "target": SAFE_WORD_RE,
    "planSHA256": SHA256_RE,
    "deviceId": DEVICE_ID_RE,
    "issuedAt": TIME_RE,
    "expiresAt": TIME_RE,
    "signature": SIGNATURE_RE,
}
JOB_PATTERNS = {"jobId": JOB_ID_RE, "jobNonce": NONCE_RE, "deviceId": DEVICE_ID_RE}

# What a refusal that has verified the approval names (section 13).
VERIFIED_FIELDS = (
    "jobId",
    "jobNonce",
    "approvalNonce",
    "deviceId",
    "host",
    "action",
    "target",
    "planSHA256",
)

APPROVAL_HEAD = "muster-approve/1"
TRUST_LIST_PREFIX = b"muster-trust-list/1\n"
COUNTERSIGN_PREFIX = b"muster-trust-countersign/1\n"
SIGNED_PREFIX = {
    "plan": b"muster-signed-plan/1\n",
    "result": b"muster-signed-result/1\n",
    "refusal": b"muster-signed-refusal/1\n",
    "ack": b"muster-signed-ack/1\n",
}
CONTRACT = {
    "plan": "muster-signed-plan/1",
    "result": "muster-signed-result/1",
    "refusal": "muster-signed-refusal/1",
    "ack": "muster-signed-ack/1",
}

MAX_INT = 2**53 - 1
CANON_MAX_DEPTH = 64
KEY_MAX_BYTES = 4096

STATE_KEY_NAME = "muster-helper.key"
STATE_TRUST_NAME = "muster-trust.json"
STATE_LEDGER_NAME = "muster-nonces.jsonl"
STATE_RATE_NAME = "muster-rate.json"
STATE_RATE_LOCK_NAME = "muster-rate.lock"
PIN_DIR = "/etc/muster"
STATE_SUBPATH = (".local", "state", "fleet-tools")

# ---- Choices of spec version 5.  Each one is here and nowhere else, so that a change in the spec is a
# change in one place.

# Section 7: the window, in seconds.  There is no grace.
WINDOW_MAX = 120
SKEW_MAX = 60
# Section 11: the ledger keeps its last LEDGER_KEEP_LINES lines and every line whose approval expired
# less than LEDGER_KEEP_SECONDS ago, whichever keeps more.  The clock alone never forgets a nonce.
LEDGER_KEEP_LINES = 4096
LEDGER_KEEP_SECONDS = 7 * 24 * 3600
# Section 11: `muster-trust.json` is 1 to 65,536 bytes, or it is `state_unsafe`.
TRUST_FILE_MAX_BYTES = 65536
# Section 8: a list names at most this many devices and carries at most this many countersignatures.
LIST_MAX_DEVICES = 64
LIST_MAX_COUNTERSIGNATURES = 64
# Section 12: the rate limits, per class of request.  Each class is (the spacing for one key in
# milliseconds, the hourly cap for the whole class).  The key of `plan` and `yes` is `verb target` with
# one space between, that of `chain` is `chain` and that of `current` is `current`.  The `plan` and
# `current` requests carry nothing that can be authenticated, so they are counted first, before
# anything else; the `yes` class is counted last, inside the action lock, after the signature, the
# window and the ledger have passed, so that nothing unauthenticated can use its budget.  The `chain`
# class is counted at C1, before anything else but the pin and the input, like `plan` and `current`.
RATE_LIMITS = {
    "plan": (5000, 120),
    "yes": (2000, 60),
    "chain": (10000, 30),
    "current": (2000, 600),
}
RATE_CONTRACT = "muster-rate/1"
# The hour is sliding and inclusive, in milliseconds: the cap counts the class's requests with
# `now - at <= RATE_HOUR`, so a request exactly 3,600 seconds old still counts.
RATE_HOUR = 3_600_000
# Section 11: the ledger's size limit, derived here and nowhere else.  The most lines it can hold is the
# larger of the floor (LEDGER_KEEP_LINES) and what the `yes` cap lets in over the retention, with one
# more hour for the inclusive boundary.  The longest canonical line has a 26-character jobId and every
# other field at its full width (201 bytes with its line feed).  The limit is that product with twice
# the room, rounded up to a whole MiB (4 MiB today), so a ledger that is merely long is never
# `ledger_corrupt`: one that size is a fault.  A change to the cap, the retention or a field must
# move this, and a test checks that it did.
LEDGER_LINE_MAX_BYTES = 201
LEDGER_MAX_RETAINED_LINES = max(
    LEDGER_KEEP_LINES, RATE_LIMITS["yes"][1] * (LEDGER_KEEP_SECONDS // 3600 + 1)
)
LEDGER_MAX_BYTES = -(-(2 * LEDGER_MAX_RETAINED_LINES * LEDGER_LINE_MAX_BYTES) // 2**20) * 2**20
# The file holds at most the sum of the hourly caps (810).
RATE_MAX_EVENTS = sum(cap for _, cap in RATE_LIMITS.values())
RATE_KEY_MAX = 300
RATE_MAX_BYTES = 128 * 1024
# The rate lock is waited for this long, then the request is `rate_limited` and not counted.
RATE_LOCK_WAIT = 2.0
# Section 7: the kernel's clock state, read with `adjtimex(modes = 0)`.  An approval is refused
# `clock_unsynced` when the call fails, returns TIME_ERROR, reports STA_UNSYNC, or reports a
# `maxerror` above this many microseconds (one second).
CLOCK_MAX_ERROR_US = 1_000_000
TIME_ERROR = 5
STA_UNSYNC = 0x0040
# `struct timex` of Linux on a 64-bit machine, in bytes.
TIMEX_SIZE = 208
# OPEN-2 (sections 11 and 12, P0): any fault in the helper key file or the state directory answers the
# unsigned `helper_key_missing`.  Never `state_unsafe`: a refusal cannot be signed with the key that
# failed.
UNSAFE_HELPER_KEY_CODE = "helper_key_missing"
# Section 14: an approval gets an audit row only once Y8 has spent its nonce, so each approval nonce has
# at most one row, ever, and a replayed or expired approval adds none.
AUDIT_FROM_STEP = "Y8"

# The error codes of section 15 that this slice answers, and nothing else.
ERR_HELPER_KEY_MISSING = UNSAFE_HELPER_KEY_CODE
ERR_PIN_UNSAFE = "pin_unsafe"
ERR_BAD_COMMAND = "bad_command"
ERR_MODE_NOT_ALLOWED = "mode_not_allowed"
ERR_INPUT_TOO_LARGE = "input_too_large"
ERR_BAD_INPUT = "bad_input"
ERR_ACTION_BUSY = "action_busy"
ERR_TRUST_LIST_CHANGED = "trust_list_changed"
ERR_STATE_UNSAFE = "state_unsafe"
ERR_NO_TRUST_LIST = "no_trust_list"
ERR_FIELD_MISMATCH = "field_mismatch"
ERR_UNKNOWN_DEVICE = "unknown_device"
ERR_BAD_SIGNATURE = "bad_signature"
ERR_CLOCK_UNSYNCED = "clock_unsynced"
ERR_WINDOW_TOO_LONG = "window_too_long"
ERR_NOT_YET_VALID = "not_yet_valid"
ERR_EXPIRED = "expired"
ERR_LEDGER_CORRUPT = "ledger_corrupt"
ERR_REPLAYED_NONCE = "replayed_nonce"
ERR_PLAN_MISMATCH = "plan_mismatch"
ERR_RATE_LIMITED = "rate_limited"
ERR_PLAN_REFUSED = "plan_refused"
ERR_INTERNAL_ERROR = "internal_error"
ERR_CHAIN_REFUSED = "chain_refused"
# The `detail` of a result for a fault after Y8 that has no better name (section 12).
INTERNAL_ERROR_DETAIL = "internal_error"
# Section 4: the reasons a plan refusal may carry: the planner's own `error_code` when it gives one
# (the first eight), else `allowlist_miss` or `report_stale`, and `revision_stale` for a resource policy
# whose saved revision moved.  The last three planner words (`update_route_changed` and the two
# `receipt_context_*`) are the helper's own refusals of the same kind that section 4's list does not
# name yet.
PLAN_REFUSED_REASONS = (
    "update_busy",
    "authorization_unavailable",
    "update_route_unavailable",
    "update_executable_unavailable",
    "unit_status_unavailable",
    "unit_status_invalid",
    "unit_unavailable",
    "unit_type_unsupported",
    "update_route_changed",
    "receipt_context_mismatch",
    "receipt_context_unavailable",
    "allowlist_miss",
    "report_stale",
    "revision_stale",
)
# Section 15 (v5.3): the reasons of a `chain_refused`, in the order of section 8's checks.  The three
# reasons of the OPEN reopen proposal (`reopen_unrecorded`, `reopen_tag`, `reopen_device`) are not
# answered: a rule C entry is `reopen_unavailable` here, whatever the record holds.
CHAIN_REFUSED_REASONS = (
    "version_gap",
    "first_rule",
    "previous_hash",
    "owner_key",
    "history_hash",
    "owner_signature",
    "duplicate_device",
    "countersignature",
    "closed_rule",
    "services_changed",
    "server_keys_removed",
    "reopen_unavailable",
)
# Section 14 (v5.3): the audit row of an accepted chain.
CHAIN_AUDIT_RESULT = "accepted"
# Every code the flow may answer.  A test keeps this equal to the codes of section 15.
ANSWERED_CODES = frozenset(
    {
        ERR_HELPER_KEY_MISSING,
        ERR_PIN_UNSAFE,
        ERR_BAD_COMMAND,
        ERR_MODE_NOT_ALLOWED,
        ERR_INPUT_TOO_LARGE,
        ERR_BAD_INPUT,
        ERR_ACTION_BUSY,
        ERR_TRUST_LIST_CHANGED,
        ERR_STATE_UNSAFE,
        ERR_NO_TRUST_LIST,
        ERR_FIELD_MISMATCH,
        ERR_UNKNOWN_DEVICE,
        ERR_BAD_SIGNATURE,
        ERR_CLOCK_UNSYNCED,
        ERR_WINDOW_TOO_LONG,
        ERR_NOT_YET_VALID,
        ERR_EXPIRED,
        ERR_LEDGER_CORRUPT,
        ERR_REPLAYED_NONCE,
        ERR_PLAN_MISMATCH,
        ERR_RATE_LIMITED,
        ERR_PLAN_REFUSED,
        ERR_INTERNAL_ERROR,
        ERR_CHAIN_REFUSED,
    }
)

# Section 13: what a result's `outcome` may be.  `refused` is never one: a refusal is its own answer.
OUTCOMES = ("done", "submitted", "failed", "unknown", "planned")

# The verb table of section 4.  The `server` scope has the change verbs and the read verbs; the
# `muster` scope has `trust-list current` (`--plan`) and `trust-list chain` (`--yes`).  The `mac` and
# `pm2` scopes and `server eavesdropper-status` are not offered.
CHANGE_VERBS = ("start", "stop", "restart", "logs", "update", "redeploy", "resource-policy")
READ_VERBS = ("resource-status", "seat-inventory", "update-status")
SERVER_SCOPE = "server"
MUSTER_SCOPE = "muster"
TRUST_LIST_VERB = "trust-list"
CURRENT_TARGET = "current"
CHAIN_TARGET = "chain"
# The value of the `host` field of an approval and of every answer.  The name does not end in HOST: the
# repository's private-inventory guard reads a variable named that way, given a one-word literal, as a
# host (test_fleet_skill_identity.py).
HOST_FIELD_VALUE = "server"
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
# DER SubjectPublicKeyInfo of an uncompressed P-256 key: this fixed prefix, then 04 || X || Y.
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
P256_SPKI_LENGTH = 91


# MARK: - Exceptions


class NotCanonical(ValueError):
    """A value that section 2's canonical form cannot hold."""


class StrictJSONError(ValueError):
    """Standard input or a state file is not strict JSON of the allowed kind."""


class Refusal(Exception):
    """A refusal with one of section 15's codes."""

    def __init__(self, code, detail="", reason=None, chain=None):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.reason = reason
        # The refused entry of a `chain_refused` (section 13): {"version", "entrySHA256", "final"}.
        self.chain = chain


class StateUnsafe(Exception):
    """A state file or directory failed its open-time checks."""


class LedgerCorrupt(Exception):
    pass


class ReplayedNonce(Exception):
    pass


class LedgerAppendFault(Exception):
    """Y8c: the new nonce line could not be written or made durable.  The nonce may or may not be on
    disk (an fsync that failed leaves the line there, and the same approval is then a replay), so the
    answer is `internal_error`, never a signed result."""


class RateLimited(Exception):
    pass


class ChainRefused(Exception):
    """C3: `entry` of the chain fails one of section 8's checks, for `reason` (the first that fails)."""

    def __init__(self, reason, entry):
        super().__init__(reason)
        self.reason = reason
        self.entry = entry


class PinUnsafe(Exception):
    pass


class KeyUnusable(Exception):
    pass


class BackendRefused(Exception):
    """The backend refused to plan or read: a fixed phrase, never a path or a secret."""

    def __init__(self, detail="", code=None):
        super().__init__(detail)
        self.detail = detail
        self.code = code


class RunFault(Exception):
    """The backend's run faulted.  `started` says whether the action had begun, or may have.

    A run that began and has no observed end is `unknown`; one that provably never began is `failed`
    (section 13).  Any other exception out of `run` is taken to have begun."""

    def __init__(self, started=True):
        super().__init__("the run faulted")
        self.started = started


class LockBusy(Exception):
    """`ops-control.lock` is held by another action."""


class LockUnsafe(Exception):
    """`ops-control.lock` or its directory failed its checks."""


# MARK: - Canonical bytes (section 2), written without any JSON library


def _ascii_string(text):
    for char in text:
        if not 0x20 <= ord(char) <= 0x7E:
            raise NotCanonical("a string holds a character that is not printable ASCII")
    return text


def _canon_string(text):
    _ascii_string(text)
    # Only the quote and the backslash are escaped.  A slash is never escaped.
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _canon(value, out, depth):
    if depth > CANON_MAX_DEPTH:
        raise NotCanonical("nested too deeply")
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif type(value) is int:
        if abs(value) > MAX_INT:
            raise NotCanonical("an integer is out of range")
        out.append(str(value))
    elif type(value) is str:
        out.append(_canon_string(value))
    elif type(value) is list:
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _canon(item, out, depth + 1)
        out.append("]")
    elif type(value) is dict:
        for key in value:
            if type(key) is not str:
                raise NotCanonical("a key is not a string")
            _ascii_string(key)
        out.append("{")
        for index, key in enumerate(sorted(value, key=lambda k: k.encode("ascii"))):
            if index:
                out.append(",")
            out.append(_canon_string(key))
            out.append(":")
            _canon(value[key], out, depth + 1)
        out.append("}")
    else:
        raise NotCanonical("a value of a type that has no canonical form")


def canonical_bytes(value):
    """The canonical bytes of `value` (section 2): ASCII, sorted keys, no whitespace."""
    out = []
    _canon(value, out, 0)
    return "".join(out).encode("ascii")


def encode_answer(answer):
    """One answer as a line of text for standard output."""
    return canonical_bytes(answer).decode("ascii") + "\n"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


# MARK: - Strict JSON input (section 5)


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _refuse_constant(_name):
    raise ValueError("NaN and infinities are not allowed")


def _refuse_float(_text):
    raise ValueError("floating-point numbers are not allowed")


def _bounded_int(text):
    if len(text) > 17:
        raise ValueError("number too large")
    value = int(text)
    if abs(value) > MAX_INT:
        raise ValueError("number too large")
    return value


def _check_printable_ascii(value):
    """Every string, keys included, must be printable ASCII.  Iterative, so depth cannot hurt it."""
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is str:
            _ascii_string(item)
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is dict:
            for key, member in item.items():
                _ascii_string(key)
                stack.append(member)


def parse_strict_json(raw):
    """The one JSON value in `raw` (bytes), or StrictJSONError.

    Strict UTF-8, one value, no duplicate keys, no NaN or infinity, no floating-point number, bounded
    integers, printable ASCII in every string."""
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_refuse_constant,
            parse_float=_refuse_float,
            parse_int=_bounded_int,
        )
        _check_printable_ascii(value)
    except (ValueError, RecursionError, NotCanonical):
        raise StrictJSONError("not strict JSON") from None
    return value


# MARK: - Times (section 2)


def parse_time(text):
    """Epoch seconds of a `YYYY-MM-DDTHH:MM:SSZ` string, or None when it is not exactly that."""
    if type(text) is not str or not TIME_RE.fullmatch(text):
        return None
    year, month, day = int(text[0:4]), int(text[5:7]), int(text[8:10])
    hour, minute, second = int(text[11:13]), int(text[14:16]), int(text[17:19])
    try:
        moment = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    return calendar.timegm(moment.timetuple())


def format_time(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


# MARK: - Keys and signatures (section 2)

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")


def b64u_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text):
    """Strict base64url without padding: the alphabet only, a possible length, canonical bits."""
    if type(text) is not str or not _B64URL_RE.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u_encode(data) != text:
        raise ValueError("base64url with stray trailing bits")
    return data


def load_p256_public(text):
    """The P-256 public key of a base64url SubjectPublicKeyInfo, or ValueError."""
    try:
        der = b64u_decode(text)
        if (
            len(der) != P256_SPKI_LENGTH
            or not der.startswith(P256_SPKI_PREFIX)
            or der[len(P256_SPKI_PREFIX)] != 0x04
        ):
            raise ValueError("not an uncompressed P-256 key")
        key = serialization.load_der_public_key(der)
    except (UnsupportedAlgorithm, TypeError):
        raise ValueError("not a P-256 key") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a P-256 key")  # noqa: TRY004 - callers catch ValueError for every fault
    return key


def parse_der_signature(sig):
    """(r, s) of a strictly canonical DER ECDSA-Sig-Value, or None.

    Minimal short-form lengths, positive minimal integers, both in [1, n - 1], no trailing byte.  The
    check does not rely on the crypto library's own leniency."""
    if not 8 <= len(sig) <= 72 or sig[0] != 0x30 or sig[1] != len(sig) - 2 or sig[1] >= 0x80:
        return None
    position = 2
    numbers = []
    for _ in range(2):
        if position + 2 > len(sig) or sig[position] != 0x02:
            return None
        length = sig[position + 1]
        if length == 0 or length >= 0x80:
            return None
        body = sig[position + 2 : position + 2 + length]
        if len(body) != length or body[0] & 0x80:
            return None
        if length > 1 and body[0] == 0x00 and not body[1] & 0x80:
            return None
        numbers.append(int.from_bytes(body, "big"))
        position += 2 + length
    if position != len(sig):
        return None
    r, s = numbers
    if not (1 <= r < P256_ORDER and 1 <= s < P256_ORDER):
        return None
    return r, s


def verify_signature(public_text, signature_text, message):
    """True only for a canonical DER signature by that P-256 key over exactly `message`."""
    try:
        key = load_p256_public(public_text)
        signature = b64u_decode(signature_text)
        if parse_der_signature(signature) is None:
            return False
        key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, UnsupportedAlgorithm):
        return False


def key_fingerprint_id(public_der):
    return sha256_hex(public_der)[:16]


class HelperKey:
    """The helper's own signing key (section 13)."""

    def __init__(self, private_key):
        self._key = private_key
        der = private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.public_b64 = b64u_encode(der)
        self.key_id = key_fingerprint_id(der)

    def sign(self, message):
        try:
            return self._key.sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
        except (TypeError, UnsupportedAlgorithm):
            return self._key.sign(message, ec.ECDSA(hashes.SHA256()))


def signed_answer(key, kind, body):
    """One signed answer: the body's canonical bytes under the kind's own prefix (section 13)."""
    body = dict(body, contract=CONTRACT[kind])
    signature = key.sign(SIGNED_PREFIX[kind] + canonical_bytes(body))
    return {
        "protocol": PROTOCOL,
        "kind": kind,
        "body": body,
        "keyId": key.key_id,
        "signature": b64u_encode(signature),
    }


def verify_signed_answer(answer, public_text):
    """True when `answer` is a well-formed signed answer by that helper key (for a verifier)."""
    try:
        kind = answer["kind"]
        body = answer["body"]
        if answer["protocol"] != PROTOCOL or kind not in SIGNED_PREFIX:
            return False
        if body.get("contract") != CONTRACT[kind]:
            return False
        key_id = key_fingerprint_id(b64u_decode(public_text))
        if answer["keyId"] != key_id:
            return False
        message = SIGNED_PREFIX[kind] + canonical_bytes(body)
        return verify_signature(public_text, answer["signature"], message)
    except (KeyError, TypeError, ValueError, AttributeError, NotCanonical):
        return False


def unsigned_helper_key_missing():
    """The single unsigned answer (section 13).  Every verifier treats it as unauthenticated."""
    return {
        "protocol": PROTOCOL,
        "kind": "refusal",
        "body": {"contract": CONTRACT["refusal"], "errorCode": ERR_HELPER_KEY_MISSING},
        "keyId": None,
        "signature": None,
    }


# MARK: - Configuration


class _Timex(ctypes.Structure):
    """`struct timex` of Linux on a 64-bit machine (208 bytes), for `adjtimex`."""

    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("padding", ctypes.c_int * 11),
    ]


def kernel_clock_state():
    """(the call's return value, `status`, `maxerror` in microseconds) from `adjtimex(modes = 0)`.

    Section 7: a read-only call that needs no privilege, made through `ctypes` (no process is started,
    and it works under `python3 -I`).  Raises OSError when the call itself fails."""
    if ctypes.sizeof(_Timex) != TIMEX_SIZE:
        # The layout above is Linux on a 64-bit machine.  Anywhere else it is not known to be right,
        # and the call fails closed: an approval is refused `clock_unsynced`.
        raise OSError(0, "struct timex is not the size this probe was written for")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.adjtimex.argtypes = [ctypes.POINTER(_Timex)]
    libc.adjtimex.restype = ctypes.c_int
    timex = _Timex()
    timex.modes = 0  # modes 0 only reads; nothing can be set
    state = libc.adjtimex(ctypes.byref(timex))
    if state < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return state, timex.status, timex.maxerror


def clock_is_synchronised(reader):
    """True only when `reader()` says the clock is synchronised (section 7).

    False when the call fails or answers anything that is not three integers, when the state is
    TIME_ERROR, when STA_UNSYNC is set, or when `maxerror` is above one second.  It fails closed."""
    try:
        state, status, maxerror = reader()
    except Exception:  # noqa: BLE001 - whatever went wrong, the clock cannot be trusted
        return False
    if any(type(value) is not int for value in (state, status, maxerror)):
        return False
    if state < 0 or state == TIME_ERROR or status & STA_UNSYNC:
        return False
    return maxerror <= CLOCK_MAX_ERROR_US


class MusterConfig:
    """Everything that decides where state lives, what time it is and what the kernel says of it.

    The production value comes from `production_config()` and never from the environment.  Only a
    test builds another.  `clock_state` is `kernel_clock_state` unless a test hands in another."""

    __slots__ = ("clock", "clock_state", "etc_dir", "root_uid", "state_dir", "uid")

    def __init__(self, etc_dir, state_dir, root_uid, uid, clock, clock_state=None):
        self.etc_dir = str(etc_dir)
        self.state_dir = str(state_dir)
        self.root_uid = root_uid
        self.uid = uid
        self.clock = clock
        self.clock_state = clock_state or kernel_clock_state

    def __eq__(self, other):
        return isinstance(other, MusterConfig) and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def __repr__(self):
        return f"MusterConfig(etc_dir={self.etc_dir!r}, state_dir={self.state_dir!r})"


def production_config():
    """The configuration of the forced command: constants and the account database only.

    The home directory is the account database's, not `$HOME`, and no environment variable is read."""
    uid = os.geteuid()
    home = pwd.getpwuid(uid).pw_dir
    return MusterConfig(
        etc_dir=PIN_DIR,
        state_dir=os.path.join(home, *STATE_SUBPATH),
        root_uid=0,
        uid=uid,
        clock=time.time,
        clock_state=kernel_clock_state,
    )


# MARK: - Safe opening of state files (section 11)


def _open_directory(path, uid, mode_mask):
    """A directory descriptor, never following a symbolic link, owned by `uid` with no `mode_mask` bit."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise StateUnsafe("the directory cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & mode_mask:
            raise StateUnsafe("the directory is not private")
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_state_dir(config):
    """The state directory: not a symbolic link, ours, and with no group or other bit."""
    return _open_directory(config.state_dir, config.uid, 0o077)


def _open_state_file(dir_fd, name, uid, flags, create=False):
    """(descriptor, created) for a file in the state directory, or None when it is absent.

    Opened with O_NOFOLLOW and O_CLOEXEC, then fstat-checked: a regular file, owned by `uid`, with no
    group or other bit and exactly one link.  `create` makes an absent file with O_CREAT | O_EXCL and mode 0600."""
    base = flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    created = False
    try:
        fd = os.open(name, base, 0o600, dir_fd=dir_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            fd = os.open(name, base | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            created = True
        except FileExistsError:
            try:
                fd = os.open(name, base, 0o600, dir_fd=dir_fd)
            except OSError:
                raise StateUnsafe("a state file cannot be opened") from None
        except OSError:
            raise StateUnsafe("a state file cannot be created") from None
    except OSError:
        raise StateUnsafe("a state file cannot be opened") from None
    try:
        info = os.fstat(fd)
        # One name only: a second hard link could let another path write what this one reads.  A
        # read-only handle may have no name left (nlink 0): an unlocked reader that opened the stored list
        # just before a chain's rename sees its old inode unlinked, and the file is whole (it changes only
        # by rename), so that is no fault.  A handle that can write to a file nothing links to is refused.
        writable = bool(flags & (os.O_WRONLY | os.O_RDWR))
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_mode & 0o077
            or info.st_nlink > 1
            or (info.st_nlink == 0 and writable)
        ):
            raise StateUnsafe("a state file is not a private regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, created


def _read_all(fd, cap):
    """The whole file, or None when it is larger than `cap`."""
    size = os.fstat(fd).st_size
    if size > cap:
        return None
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(65536, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]


def _replace_file(dir_fd, name, data, uid, durable):
    """Write `data` to a new file beside `name` and rename it over `name` (never in place)."""
    temporary = "." + name + "." + secrets.token_hex(8) + ".tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        try:
            _write_all(fd, data)
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        if durable:
            os.fsync(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=dir_fd)
        raise


# What a crash leaves of a write of the stored list: `_replace_file` names its temporary file this way.
_STORED_LIST_TEMPORARY_RE = re.compile(r"\.muster-trust\.json\.[0-9a-f]{16}\.tmp")


def sweep_stored_list_temporaries(dir_fd, uid):
    """Remove the temporary files that earlier writes of the stored list left behind, and nothing else.

    A kill between the open of the temporary file and the rename leaves it there, and nothing ever
    reads it.  A name must be exactly `.muster-trust.json.`, 16 lowercase hex characters and `.tmp`,
    and the entry must be a regular file (looked at without following a link) owned by `uid` with one
    link.  Every error is ignored: this is housekeeping, and the write that follows does not depend on
    it.  Called under `ops-control.lock`, where the chain verb is the only writer of such files, so
    none of them is live."""
    with contextlib.suppress(Exception):
        for name in os.listdir(dir_fd):
            if not _STORED_LIST_TEMPORARY_RE.fullmatch(name):
                continue
            with contextlib.suppress(OSError):
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1:
                    os.unlink(name, dir_fd=dir_fd)


# MARK: - The helper key (P0)


def load_helper_key(dir_fd, uid):
    """The helper's signing key, or KeyUnusable.  A missing file and an unsafe one are the same."""
    try:
        opened = _open_state_file(dir_fd, STATE_KEY_NAME, uid, os.O_RDONLY)
    except StateUnsafe:
        raise KeyUnusable from None  # OPEN-2: see UNSAFE_HELPER_KEY_CODE
    if opened is None:
        raise KeyUnusable
    fd = opened[0]
    try:
        raw = _read_all(fd, KEY_MAX_BYTES)
    finally:
        os.close(fd)
    if raw is None:
        raise KeyUnusable
    try:
        private = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise KeyUnusable from None
    if not isinstance(private, ec.EllipticCurvePrivateKey) or not isinstance(
        private.curve, ec.SECP256R1
    ):
        raise KeyUnusable
    return HelperKey(private)


# MARK: - The pin and the owner history (P1, section 10)


class Pin:
    """What P1 read: the owner key, the history, and the reopen record's bytes when it exists.

    OPEN-1: the record is read here, once, and kept.  The chain verb of the next slice uses this copy
    and never opens the record again, so `pin_unsafe` arises only at P1."""

    def __init__(self, owner_key, history, reopen_bytes=None, reopen_lines=()):
        self.owner_key = owner_key
        self.history = history
        self.reopen_bytes = reopen_bytes
        self.reopen_lines = list(reopen_lines)

    def key_for_version(self, version):
        return history_key(self.history, version)


def history_key(history, version):
    for entry in history:
        if entry["fromVersion"] <= version and (
            entry["toVersion"] is None or version <= entry["toVersion"]
        ):
            return entry["ownerKey"]
    return None


def check_pin(config, trust_files):
    """The pinned owner key, the owner history and the reopen record (step P1), or PinUnsafe.

    All of it is the shared `muster_trust_files` module's one read of `/etc/muster`; any failure of
    that read, of whatever kind, is `pin_unsafe` (section 10)."""
    try:
        files = trust_files.read_trust_files(config.etc_dir, config.root_uid)
    except Exception:  # noqa: BLE001 - no pin can be established, whatever the reason
        raise PinUnsafe from None
    return Pin(files.owner_key, files.history, files.reopen_bytes, files.reopen_lines)


# MARK: - The stored trust list (Y2, section 8)

_LIST_KEYS = frozenset({"body", "ownerSignature", "countersignatures"})
_BODY_KEYS = frozenset(
    {
        "contract",
        "version",
        "previousSHA256",
        "issuedAt",
        "ownerKey",
        "closed",
        "devices",
        "services",
        "reopenTag",
    }
)
_DEVICE_KEYS = frozenset(
    {"deviceId", "name", "approvalKey", "requestKey", "readingKey", "notificationKey"}
)
_SERVICE_KEYS = frozenset(
    {
        "macPublisher",
        "macExecutor",
        "macAgreement",
        "serverPublisher",
        "serverExecutor",
        "serverExecutorAgreement",
        "serverHelper",
    }
)
TRUST_LIST_CONTRACT = "muster-trust-list/1"


class StoredList:
    def __init__(self, body, owner_signature, devices):
        self.body = body
        self.owner_signature = owner_signature
        self.devices = devices
        self.version = body["version"]
        self.owner_key = body["ownerKey"]
        self.closed = body["closed"]
        self.body_bytes = canonical_bytes(body)
        # The hash Y2 notes and Y7 compares: of the canonical body, so a signature never changes it.
        self.body_sha256 = sha256_hex(self.body_bytes)


class ListEntry(StoredList):
    """One signed trust list as parsed (a chain entry or the stored file): its body, signatures and devices.

    `device_ids` keeps every device in order, duplicates included (check 5 refuses them), and `devices`
    maps each id to its approval key.  `canonical` is the canonical bytes of the whole signed object
    (section 2), the bytes the chain verb stores, and `entry_sha256` is their SHA-256, the entry hash."""

    def __init__(self, document, device_ids):
        body = document["body"]
        devices = {device["deviceId"]: device["approvalKey"] for device in body["devices"]}
        super().__init__(body, document["ownerSignature"], devices)
        self.countersignatures = document["countersignatures"]
        self.device_ids = device_ids
        self.canonical = canonical_bytes(document)
        self.entry_sha256 = sha256_hex(self.canonical)


def parse_list_document(doc):
    """The ListEntry of one signed trust list (a value of strict JSON), or ValueError.

    This is the structure only (section 5, and section 8's field patterns, "checked at P3"): the three
    keys, every body field with its type and pattern, every key a P-256 key (section 2), at most 64
    devices and 64 countersignatures.  The numbered checks of section 8 are the chain walk's and have
    their own reasons: two devices with one id (check 5), two countersignatures naming one device
    (check 6) and `closed` disagreeing with `devices` (check 7) are well-formed here."""
    if type(doc) is not dict or set(doc) != _LIST_KEYS:
        raise ValueError("not a trust list")
    body = doc["body"]
    if type(body) is not dict or set(body) != _BODY_KEYS or body["contract"] != TRUST_LIST_CONTRACT:
        raise ValueError("not a trust list body")
    version = body["version"]
    if type(version) is not int or not 1 <= version <= MAX_INT:
        raise ValueError("bad version")
    if body["previousSHA256"] is not None and not (
        type(body["previousSHA256"]) is str and SHA256_RE.fullmatch(body["previousSHA256"])
    ):
        raise ValueError("bad previous hash")
    if parse_time(body["issuedAt"]) is None or type(body["closed"]) is not bool:
        raise ValueError("bad body")
    load_p256_public(body["ownerKey"])
    if body["reopenTag"] is not None and not (
        type(body["reopenTag"]) is str and SHA256_RE.fullmatch(body["reopenTag"])
    ):
        raise ValueError("bad reopen tag")
    services = body["services"]
    if type(services) is not dict or set(services) != _SERVICE_KEYS:
        raise ValueError("bad services")
    for value in services.values():
        if value is not None:
            load_p256_public(value)
    devices_list = body["devices"]
    if type(devices_list) is not list or len(devices_list) > LIST_MAX_DEVICES:
        raise ValueError("bad devices")
    device_ids = []
    for device in devices_list:
        if type(device) is not dict or set(device) != _DEVICE_KEYS:
            raise ValueError("bad device")
        device_id = device["deviceId"]
        name = device["name"]
        if (
            type(device_id) is not str
            or not DEVICE_ID_RE.fullmatch(device_id)
            or type(name) is not str
            or not 1 <= len(name) <= 40
        ):
            raise ValueError("bad device")
        for role in ("approvalKey", "requestKey", "readingKey", "notificationKey"):
            load_p256_public(device[role])
        device_ids.append(device_id)
    if type(doc["ownerSignature"]) is not str or not SIGNATURE_RE.fullmatch(doc["ownerSignature"]):
        raise ValueError("bad owner signature")
    countersignatures = doc["countersignatures"]
    if type(countersignatures) is not list or len(countersignatures) > LIST_MAX_COUNTERSIGNATURES:
        raise ValueError("bad countersignatures")
    for item in countersignatures:
        if (
            type(item) is not dict
            or set(item) != {"deviceId", "signature"}
            or type(item["deviceId"]) is not str
            or not DEVICE_ID_RE.fullmatch(item["deviceId"])
            or type(item["signature"]) is not str
            or not SIGNATURE_RE.fullmatch(item["signature"])
        ):
            raise ValueError("bad countersignature")
    try:
        entry = ListEntry(doc, device_ids)
    except NotCanonical:
        raise ValueError("a trust list that has no canonical form") from None
    if len(entry.canonical) > TRUST_FILE_MAX_BYTES:
        # The stored file would be unreadable (section 11), so the list is not accepted.  This is defence
        # in depth: a maximal valid entry (64 devices and 64 countersignatures at their longest) is about
        # 55 KB, so no list that passed the checks above reaches it today; it keeps the invariant
        # "an accepted list is readable" if a limit above is ever raised.
        raise ValueError("a trust list larger than the stored file may be")
    return entry


def parse_stored_list(raw):
    """A ListEntry from the stored file's bytes, or ValueError when it is not a well-formed list.

    The reader is strict where the chain's structural parse is not: a stored file satisfies the list-local
    checks of section 8 too (no two devices with one id, no two countersignatures naming one device,
    `closed` exactly when there is no device), because a chain never stores a list that fails them."""
    try:
        doc = parse_strict_json(raw)
    except StrictJSONError:
        raise ValueError("not strict JSON") from None
    entry = parse_list_document(doc)
    if len(set(entry.device_ids)) != len(entry.device_ids):
        raise ValueError("duplicate device")
    named = [item["deviceId"] for item in entry.countersignatures]
    if len(set(named)) != len(named):
        raise ValueError("two countersignatures name one device")
    if entry.closed != (not entry.device_ids):
        raise ValueError("closed does not follow devices")
    return entry


def load_stored_list(dir_fd, uid):
    """The stored list, None when the file is absent, or StateUnsafe.

    OPEN-5: the file is never created here.  Absent is `no list`; empty, unreadable or malformed is
    `state_unsafe`, never `none`, so that damage cannot roll the helper back to a first list."""
    opened = _open_state_file(dir_fd, STATE_TRUST_NAME, uid, os.O_RDONLY)
    if opened is None:
        return None
    fd = opened[0]
    try:
        raw = _read_all(fd, TRUST_FILE_MAX_BYTES)
    finally:
        os.close(fd)
    if raw is None:
        raise StateUnsafe("the stored trust list is too large")
    try:
        return parse_stored_list(raw)
    except ValueError:
        raise StateUnsafe("the stored trust list is malformed") from None


def stored_list_is_current(stored, pin):
    """True when the stored list carries the current owner key and is signed by it (Y2)."""
    if stored.owner_key != pin.owner_key or pin.key_for_version(stored.version) != stored.owner_key:
        return False
    try:
        message = TRUST_LIST_PREFIX + canonical_bytes(stored.body)
    except NotCanonical:
        return False
    return verify_signature(pin.owner_key, stored.owner_signature, message)


# MARK: - The trust-list chain (sections 5 and 8, P3 and C3)


def parse_chain_document(doc):
    """The ListEntry of each entry of a chain request document, in order, or ValueError (P3).

    `{"protocol":"muster-helper/1","chain":[signed list, ...]}`, 1 to 32 entries.  Whether the entries
    ascend is check 1 of the walk (`version_gap`), not a question of structure."""
    if type(doc) is not dict or set(doc) != {"protocol", "chain"} or doc["protocol"] != PROTOCOL:
        raise ValueError("not a chain request")
    chain = doc["chain"]
    if type(chain) is not list or not CHAIN_MIN_ENTRIES <= len(chain) <= CHAIN_MAX_ENTRIES:
        raise ValueError("a chain holds 1 to 32 entries")
    return [parse_list_document(item) for item in chain]


def history_hash_matches(pin, version, body_sha256):
    """Section 10 (v5.3): when `version` is the `toVersion` of a history line, the body hash of the
    list at that version is that line's `toSHA256`.  Any other version has nothing to compare."""
    return all(
        line["toVersion"] != version or line["toSHA256"] == body_sha256 for line in pin.history
    )


def _server_keys_removed(previous, entry):
    """True when the previous list named `serverHelper` or `serverPublisher` and this one names neither."""
    before = previous.body["services"]
    after = entry.body["services"]
    return bool(before["serverHelper"] or before["serverPublisher"]) and not (
        after["serverHelper"] or after["serverPublisher"]
    )


def _services_kept(previous, entry):
    """True when every `services` value is the previous one or null."""
    before = previous.body["services"]
    return all(
        value is None or value == before[name] for name, value in entry.body["services"].items()
    )


def _countersigned_by_previous(previous, entry):
    """True when there is at least one countersignature and each verifies with the `approvalKey` of a
    device on the previous list."""
    if not entry.countersignatures:
        return False
    message = COUNTERSIGN_PREFIX + entry.body_bytes
    for item in entry.countersignatures:
        approval_key = previous.devices.get(item["deviceId"])
        if approval_key is None or not verify_signature(approval_key, item["signature"], message):
            return False
    return True


def _rule_failure(previous, entry):
    """Check 8: the one rule that applies, its conditions in the order written.  The reason or None."""
    body = entry.body
    if previous is None:
        # First: not closed, no countersignatures, no reopen tag.
        if entry.closed or entry.countersignatures or body["reopenTag"] is not None:
            return "first_rule"
        return None
    if entry.closed:
        # B, close: no countersignatures and no reopen tag, services kept or removed, a server key kept.
        if entry.countersignatures or body["reopenTag"] is not None:
            return "closed_rule"
        if not _services_kept(previous, entry):
            return "services_changed"
        if _server_keys_removed(previous, entry):
            return "server_keys_removed"
        return None
    if previous.closed:
        # C, reopen, is OPEN (section 17A): every verifier of Milestones 1 to 4 refuses it.
        return "reopen_unavailable"
    # A, normal change.
    if body["reopenTag"] is not None:
        return "first_rule"
    if not _countersigned_by_previous(previous, entry):
        return "countersignature"
    if _server_keys_removed(previous, entry):
        return "server_keys_removed"
    return None


def entry_failure(previous, entry, pin):
    """Section 8's checks 1 to 8 for one entry, in order: the reason of the first that fails, else None.

    `previous` is the list the verifier holds or the entry before this one (None when nothing is held).
    The owner signature is checked with the key the history assigns to this version, never with the
    entry's own `ownerKey`."""
    # 1
    if entry.version != (1 if previous is None else previous.version + 1):
        return "version_gap"
    # 2
    if previous is None:
        if entry.body["previousSHA256"] is not None:
            return "first_rule"
    elif entry.body["previousSHA256"] != previous.body_sha256:
        return "previous_hash"
    # 3
    assigned = pin.key_for_version(entry.version)
    if assigned is None or entry.owner_key != assigned:
        return "owner_key"
    if not history_hash_matches(pin, entry.version, entry.body_sha256):
        return "history_hash"
    # 4
    if not verify_signature(assigned, entry.owner_signature, TRUST_LIST_PREFIX + entry.body_bytes):
        return "owner_signature"
    # 5
    if len(set(entry.device_ids)) != len(entry.device_ids):
        return "duplicate_device"
    # 6
    named = [item["deviceId"] for item in entry.countersignatures]
    if len(set(named)) != len(named):
        return "countersignature"
    # 7
    if entry.closed != (not entry.device_ids):
        return "closed_rule"
    # 8
    return _rule_failure(previous, entry)


def walk_chain(entries, held, pin):
    """The newest entry of a chain every entry of which passes section 8, or ChainRefused (C3).

    All or none: nothing is kept of an entry before the whole chain has passed, and the first failure
    ends the walk.  `held` is the stored list (None when the helper holds none), `pin` what P1 read."""
    if not entries:
        raise ValueError("an empty chain")
    previous = held
    for entry in entries:
        reason = entry_failure(previous, entry, pin)
        if reason is not None:
            raise ChainRefused(reason, entry)
        previous = entry
    return previous


# MARK: - The approval (section 6) and the plan request (section 5)


def approval_text(approval):
    """The bytes the device signs: the eleven values joined by a line feed, no trailing one."""
    return "\n".join(
        [
            APPROVAL_HEAD,
            approval["jobId"],
            approval["jobNonce"],
            approval["approvalNonce"],
            approval["host"],
            approval["action"],
            approval["target"],
            approval["planSHA256"],
            approval["deviceId"],
            approval["issuedAt"],
            approval["expiresAt"],
        ]
    ).encode("ascii")


def _fields_match(obj, patterns):
    if type(obj) is not dict or set(obj) != set(patterns):
        return False
    return all(type(obj[name]) is str and patterns[name].fullmatch(obj[name]) for name in patterns)


def parse_approval_document(doc):
    """The approval of an approval request document, or ValueError (section 5 and 6)."""
    if type(doc) is not dict or set(doc) != {"protocol", "approval"} or doc["protocol"] != PROTOCOL:
        raise ValueError("not an approval request")
    approval = doc["approval"]
    if not _fields_match(approval, APPROVAL_PATTERNS):
        raise ValueError("bad approval fields")
    if parse_time(approval["issuedAt"]) is None or parse_time(approval["expiresAt"]) is None:
        raise ValueError("bad approval time")
    if approval["jobNonce"] == approval["approvalNonce"]:
        raise ValueError("the two nonces must differ")
    return dict(approval)


def parse_plan_document(doc):
    """The job of a plan request document, or ValueError."""
    if type(doc) is not dict or set(doc) != {"protocol", "job"} or doc["protocol"] != PROTOCOL:
        raise ValueError("not a plan request")
    if not _fields_match(doc["job"], JOB_PATTERNS):
        raise ValueError("bad job fields")
    return dict(doc["job"])


def parse_current_document(doc):
    """Check the document of `muster trust-list current --plan`: only the protocol, or ValueError."""
    if type(doc) is not dict or set(doc) != {"protocol"} or doc["protocol"] != PROTOCOL:
        raise ValueError("not an acknowledgement request")


def check_window(approval, now):
    """Section 7: raises Refusal.  `now` is read once by the caller.  There is no grace."""
    issued = parse_time(approval["issuedAt"])
    expires = parse_time(approval["expiresAt"])
    if expires <= issued or expires - issued > WINDOW_MAX:
        raise Refusal(ERR_WINDOW_TOO_LONG)
    if issued - now > SKEW_MAX:
        raise Refusal(ERR_NOT_YET_VALID)
    if now > expires:
        raise Refusal(ERR_EXPIRED)


# MARK: - The command line (P2, sections 3 and 4)


class Command:
    __slots__ = ("kind", "mode", "scope", "target", "verb")

    def __init__(self, scope, verb, target, mode, kind):
        self.scope = scope
        self.verb = verb
        self.target = target
        self.mode = mode
        self.kind = kind


def parse_command(ssh_command, argv_ok, target_ok):
    """The Command in `SSH_ORIGINAL_COMMAND`, or Refusal(bad_command | mode_not_allowed).

    `target_ok(verb, target)` is the backend's grammar for each verb's target (a unit name, an update
    name, a Coolify uuid, the `field.value.revision` of a resource policy, a platform, a receipt id):
    a target that cannot be one is `bad_command` here, at P2, before the request is counted.  Whether
    an allowed word names something on the allowlist is for the planner (`plan_refused`)."""
    if not argv_ok or type(ssh_command) is not str or not SSH_SAFE_RE.fullmatch(ssh_command):
        raise Refusal(ERR_BAD_COMMAND)
    words = ssh_command.split(" ")
    if len(words) != 4 or any(not word for word in words):
        raise Refusal(ERR_BAD_COMMAND)
    scope, verb, target, mode_word = words
    if mode_word not in ("--plan", "--yes"):
        raise Refusal(ERR_BAD_COMMAND)
    if scope == MUSTER_SCOPE:
        # `trust-list current` is a plan-mode read and `trust-list chain` a `--yes` that needs no
        # device approval (section 4): the other mode of each is `mode_not_allowed`.
        if verb != TRUST_LIST_VERB or target not in (CURRENT_TARGET, CHAIN_TARGET):
            raise Refusal(ERR_BAD_COMMAND)
        if target == CHAIN_TARGET:
            if mode_word == "--plan":
                raise Refusal(ERR_MODE_NOT_ALLOWED)
            return Command(scope, verb, target, "yes", "chain")
        if mode_word != "--plan":
            raise Refusal(ERR_MODE_NOT_ALLOWED)
        return Command(scope, verb, target, "plan", "current")
    if scope != SERVER_SCOPE or verb not in CHANGE_VERBS + READ_VERBS:
        raise Refusal(ERR_BAD_COMMAND)
    if not SAFE_WORD_RE.fullmatch(target):
        raise Refusal(ERR_BAD_COMMAND)
    if verb == "resource-status" and target != "coding":
        raise Refusal(ERR_BAD_COMMAND)
    if not target_ok(verb, target):
        raise Refusal(ERR_BAD_COMMAND)
    mode = "plan" if mode_word == "--plan" else "yes"
    kind = "read" if verb in READ_VERBS else "change"
    if kind == "read" and mode != "plan":
        raise Refusal(ERR_MODE_NOT_ALLOWED)
    return Command(scope, verb, target, mode, kind)


def read_stdin_limited(fd, limit, timeout=STDIN_TIMEOUT):
    """At most `limit` + 1 bytes of `fd`, read to end of file within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    chunks = []
    total = 0
    while total < limit + 1:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("standard input did not end in time")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise TimeoutError("standard input did not end in time")
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


# MARK: - The nonce ledger (Y8a and Y8c, section 11)

_LEDGER_FIELDS = {
    "approvalNonce": NONCE_RE,
    "deviceId": DEVICE_ID_RE,
    "expiresAt": TIME_RE,
    "jobId": JOB_ID_RE,
    "recordedAt": TIME_RE,
}


def _parse_ledger_line(line):
    """The entry of one complete ledger line, or LedgerCorrupt.  Only canonical lines are accepted."""
    try:
        entry = parse_strict_json(line)
        if not _fields_match(entry, _LEDGER_FIELDS):
            raise LedgerCorrupt
        if parse_time(entry["expiresAt"]) is None or parse_time(entry["recordedAt"]) is None:
            raise LedgerCorrupt
        if canonical_bytes(entry) != line:
            raise LedgerCorrupt
    except (StrictJSONError, NotCanonical):
        raise LedgerCorrupt from None
    return entry


def classify_fragment(fragment):
    """OPEN-3: `complete` for a whole canonical ledger line that only lacks its line feed (its nonce
    stays spent, so it is completed and never truncated), `torn` for what an append that never
    finished left (it did not parse, so no action ran on it), `corrupt` for anything that parses as
    JSON and still is not a ledger line we can vouch for."""
    try:
        parse_strict_json(fragment)
    except StrictJSONError:
        return "torn", None
    try:
        return "complete", _parse_ledger_line(fragment)
    except LedgerCorrupt:
        return "corrupt", None


class Ledger:
    def __init__(self, dir_fd, uid):
        self.dir_fd = dir_fd
        self.uid = uid

    def record(self, approval, now, on_repair, admit=None):
        """Spend the approval nonce: ReplayedNonce, LedgerCorrupt or StateUnsafe, else it is on disk.

        The caller holds `ops-control.lock`.  The order is the point: a ledger that cannot be read
        (`ledger_corrupt`) and a nonce that is already on it (`replayed_nonce`) refuse first, and cost
        nobody any rate budget; then `admit()`, the caller's rate step for the `yes` class, runs (it may
        raise RateLimited or StateUnsafe, and then the nonce is not spent); and only then is the new
        line written and fsynced (and the directory too when the file was just created), so an action
        never runs on a nonce that is not durably spent.  The nonce is the key: the same
        `approvalNonce` from another device or another job is a replay all the same."""
        fd, created = _open_state_file(
            self.dir_fd, STATE_LEDGER_NAME, self.uid, os.O_RDWR | os.O_APPEND, create=True
        )
        try:
            data = _read_all(fd, LEDGER_MAX_BYTES)
            if data is None:
                raise LedgerCorrupt
            cut = data.rfind(b"\n") + 1
            complete, fragment = data[:cut], data[cut:]
            entries = []
            if complete:
                for line in complete.split(b"\n")[:-1]:
                    entries.append(_parse_ledger_line(line))
            repair = None
            if fragment:
                state, entry = classify_fragment(fragment)
                if state == "corrupt":
                    raise LedgerCorrupt
                if state == "complete":
                    _write_all(fd, b"\n")
                    os.fsync(fd)
                    entries.append(entry)
                    repair = "completed"
                else:
                    os.ftruncate(fd, cut)
                    os.fsync(fd)
                    repair = "repaired"
            if repair:
                on_repair(repair)
            if any(entry["approvalNonce"] == approval["approvalNonce"] for entry in entries):
                raise ReplayedNonce
            if admit is not None:
                admit()
            row = {
                "approvalNonce": approval["approvalNonce"],
                "deviceId": approval["deviceId"],
                "expiresAt": approval["expiresAt"],
                "jobId": approval["jobId"],
                "recordedAt": format_time(now),
            }
            try:
                _write_all(fd, canonical_bytes(row) + b"\n")
                os.fsync(fd)
                if created:
                    os.fsync(self.dir_fd)
            except OSError:
                raise LedgerAppendFault from None
            entries.append(row)
        finally:
            # A close that fails after the fsynced append loses nothing: the line is durable.  One that
            # fails before it must not hide the refusal that is being raised.
            with contextlib.suppress(OSError):
                os.close(fd)
        # The prune is a best-effort rewrite, atomic by its temporary file and rename, so a failure of
        # any kind leaves the ledger as it was and the next approval tries it again.  The nonce is
        # spent and durable by now, so nothing here may turn the approval into a refusal.
        with contextlib.suppress(Exception):
            self._prune(entries, now)

    def _prune(self, entries, now):
        """Keep the last LEDGER_KEEP_LINES lines, and every line less than LEDGER_KEEP_SECONDS old.

        A line may go only when it is beyond the last 4,096 AND its approval expired more than seven
        days before `now`: the clock alone never forgets a nonce.  A clock that steps forward a day and
        back, or a year and back, cannot make an approval replayable, because the lines that would have
        to push it out are genuine approvals (the rate cap is 60 an hour) and the count needed is far
        beyond any step.  A line whose expiry is after `now` (the clock is behind it) always stays.  The
        rewrite goes through a temporary file, is fsynced, renamed over the ledger and the directory is
        fsynced."""
        excess = len(entries) - LEDGER_KEEP_LINES
        if excess <= 0:
            return
        keep = [
            e
            for index, e in enumerate(entries)
            if index >= excess or not now - parse_time(e["expiresAt"]) > LEDGER_KEEP_SECONDS
        ]
        if len(keep) == len(entries):
            return
        data = b"".join(canonical_bytes(e) + b"\n" for e in keep)
        with contextlib.suppress(OSError):
            _replace_file(self.dir_fd, STATE_LEDGER_NAME, data, self.uid, durable=True)


# MARK: - The rate limits (R1, Y8b, C1, K1, section 12)


def _parse_rate(raw):
    """The events of a rate file's bytes, or StateUnsafe.  An empty file is a fresh one.

    Section 12: at most 810 events, oldest first.  A file with events out of order, a class that is
    not one of the four, a key that is not text, or a time that is not a whole number of
    milliseconds is `state_unsafe`."""
    if not raw:
        return []
    try:
        doc = parse_strict_json(raw)
    except StrictJSONError:
        raise StateUnsafe("the rate file is malformed") from None
    if (
        type(doc) is not dict
        or set(doc) != {"contract", "events"}
        or doc["contract"] != RATE_CONTRACT
        or type(doc["events"]) is not list
        or len(doc["events"]) > RATE_MAX_EVENTS
    ):
        raise StateUnsafe("the rate file is malformed")
    newest = 0
    for event in doc["events"]:
        if (
            type(event) is not list
            or len(event) != 3
            or event[0] not in RATE_LIMITS
            or type(event[1]) is not str
            or len(event[1]) > RATE_KEY_MAX
            or type(event[2]) is not int
            or event[2] < newest
        ):
            raise StateUnsafe("the rate file is malformed")
        newest = event[2]
    return doc["events"]


def _lock_rate(lock_fd):
    """Wait for the rate lock for at most RATE_LOCK_WAIT seconds, or raise RateLimited."""
    deadline = time.monotonic() + RATE_LOCK_WAIT
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RateLimited from None
            time.sleep(0.02)


def check_rate(dir_fd, uid, kind, key, now, durable=False):
    """Count one request of class `kind` (`plan`, `yes`, `chain` or `current`), or raise RateLimited or
    StateUnsafe.

    Per class, at most one request per key within the class's spacing and the class's hourly cap over a
    sliding hour (`RATE_LIMITS`), counted in `muster-rate.json` under its own lock (`muster-rate.lock`).
    Lock order is fixed: `ops-control.lock` first and the rate lock second, never the other way round.
    The `yes` class is counted while the action lock is held (see `Ledger.record`); `plan` and `current`
    take the rate lock alone.  Times are integer milliseconds, `now` (the float clock) rounded down;
    the spacing passes at exactly its length and the hour still counts an event exactly an hour old.
    An event dated after `now` (the clock was stepped back) is counted as made at `now`, so that it
    ages from there (the clamp is written back even when the request is refused), and cannot hold its
    key until the clock catches up.  An empty rate file is a fresh one.  A request that is refused is not
    counted, so a flood of refused requests cannot keep the window shut.  With `durable` the new event
    is fsynced (and the directory) before this returns: the `yes` class needs that, because its event
    is recorded before the nonce is, and the nonce must never be spent without its event."""
    spacing, hourly_max = RATE_LIMITS[kind]
    lock_fd, _ = _open_state_file(dir_fd, STATE_RATE_LOCK_NAME, uid, os.O_RDWR, create=True)
    try:
        _lock_rate(lock_fd)
        fd, _ = _open_state_file(dir_fd, STATE_RATE_NAME, uid, os.O_RDWR, create=True)
        try:
            raw = _read_all(fd, RATE_MAX_BYTES)
        finally:
            os.close(fd)
        if raw is None:
            raise StateUnsafe("the rate file is too large")
        stored = _parse_rate(raw)
        now_ms = int(now * 1000)
        clamped = [[name, other, min(moment, now_ms)] for name, other, moment in stored]
        events = [event for event in clamped if now_ms - event[2] <= RATE_HOUR]
        same_class = [event for event in events if event[0] == kind]
        refused = len(same_class) >= hourly_max or any(
            other == key and now_ms - moment < spacing for _, other, moment in same_class
        )
        if refused:
            if clamped != stored:
                # Keep the clamp on disk, so that a future-dated event cannot hold its key for as long
                # as the clock is behind it, a refused request after a refused request.  The refused
                # request itself is not counted.
                document = {"contract": RATE_CONTRACT, "events": events}
                with contextlib.suppress(OSError):
                    _replace_file(dir_fd, STATE_RATE_NAME, canonical_bytes(document), uid, False)
            raise RateLimited
        events.append([kind, key, now_ms])
        document = {"contract": RATE_CONTRACT, "events": events}
        try:
            _replace_file(dir_fd, STATE_RATE_NAME, canonical_bytes(document), uid, durable)
        except OSError:
            raise StateUnsafe("the rate file cannot be written") from None
    finally:
        os.close(lock_fd)


# MARK: - One request


class RunResult:
    """What the backend's run of an approved plan produced.

    `outcome` is `done`, `submitted`, `failed` or `unknown` (section 13), or `refused` with
    `error_code` `plan_mismatch` when the shared function refused the approved plan.  `audit_id` is
    the id of the row the shared function wrote, when it wrote one."""

    __slots__ = ("audit_id", "detail", "error_code", "outcome", "output")

    def __init__(self, outcome, detail="", output=None, audit_id=None, error_code=None):
        self.outcome = outcome
        self.detail = detail
        self.output = output
        self.audit_id = audit_id
        self.error_code = error_code


class ReadResult:
    __slots__ = ("detail", "output")

    def __init__(self, detail="", output=b""):
        self.detail = detail
        self.output = output


def printable(text):
    """`text` with every character that is not printable ASCII replaced by a question mark."""
    return "".join(c if 0x20 <= ord(c) <= 0x7E else "?" for c in str(text))


class _Flow:
    """The state of one request, so that every refusal says only what was verified."""

    def __init__(self, config, backend, key, dir_fd, trust_files):
        self.config = config
        self.backend = backend
        self.key = key
        self.dir_fd = dir_fd
        self.trust_files = trust_files
        self.pin = None
        self.command = None
        self.claimed = {}
        self.verified = False
        self.fields = None
        # The version of the stored list this request read (section 13), or None while it has not
        # read one, holds none or could not read it.  Only a read of the file sets it.
        self.trust_version = None
        self.spent = False
        self.audited = False
        # True once the backend's run has been called, and the run may have begun the action.
        self.started = False

    # -- answers

    def _now(self):
        return self.config.clock()

    def _read_version(self):
        """Read the stored list for a plan or an acknowledgement and note its version.  Never raises."""
        try:
            stored = load_stored_list(self.dir_fd, self.config.uid)
        except Exception:  # noqa: BLE001 - a plan is still answerable when the list cannot be read
            stored = None
        self.trust_version = stored.version if stored is not None else None
        return stored

    def refusal(self, code, reason=None, chain=None):
        body = {
            "verified": self.verified,
            "errorCode": code,
            "reason": reason if code in (ERR_PLAN_REFUSED, ERR_CHAIN_REFUSED) else None,
            "helperTrustListVersion": self.trust_version,
            "refusedAt": format_time(self._now()),
        }
        if self.verified:
            for name in VERIFIED_FIELDS:
                body[name] = self.fields[name]
        elif self.command is not None and self.command.kind == "chain":
            # Section 13, the chain row: the refused entry when C3 refused one, else null, and nothing
            # `claimed` (a chain carries no job).  A refusal before P2 has parsed the command, or of
            # any other command, has the usual `claimed`.
            body["chain"] = chain
        else:
            body["claimed"] = dict(self.claimed)
        return signed_answer(self.key, "refusal", body), 2

    def _row_fields(self, **extra):
        fields = {"route": ROUTE, "via": VIA}
        for name in ("jobId", "deviceId", "approvalNonce", "planSHA256"):
            fields[name] = self.fields[name]
        fields.update(extra)
        return fields

    def _audit(self, code):
        """One audit row for a refusal, at most once, and only once Y8 has spent the nonce.

        Section 14: a request refused before Y8 writes no row, so that junk, expired approvals and
        replays cannot flood the log (the rate limit bounds the rest).  Called inside the lock."""
        if self.audited or not self.spent:
            return
        self.audited = True
        with contextlib.suppress(Exception):
            self.backend.audit(
                SERVER_SCOPE,
                self.command.verb,
                self.command.target,
                "refused",
                self._row_fields(errorCode=code),
            )

    def _fault_outcome(self):
        """`unknown` when the action had begun or may have, `failed` when it provably never did."""
        return "unknown" if self.started else "failed"

    def _audit_failed(self):
        """The one row of a fault after Y8, unless the shared function already wrote this one's.

        Its result word is the outcome of the answer, `failed` or `unknown`."""
        if self.audited or not self.spent:
            return
        self.audited = True
        with contextlib.suppress(Exception):
            self.backend.audit(
                SERVER_SCOPE,
                self.command.verb,
                self.command.target,
                self._fault_outcome(),
                self._row_fields(),
            )

    def _rate(self, kind, key):
        try:
            check_rate(self.dir_fd, self.config.uid, kind, key, self._now())
        except RateLimited:
            raise Refusal(ERR_RATE_LIMITED) from None
        except StateUnsafe:
            raise Refusal(ERR_STATE_UNSAFE) from None

    def _change_key(self):
        return self.command.verb + " " + self.command.target

    # -- the request

    def run(self, ssh_command, argv_ok, read_stdin):
        # P1
        try:
            self.pin = check_pin(self.config, self.trust_files)
        except PinUnsafe:
            return self.refusal(ERR_PIN_UNSAFE)
        # P2
        try:
            self.command = parse_command(ssh_command, argv_ok, self.backend.target_ok)
        except Refusal as exc:
            self.command = None
            return self.refusal(exc.code)
        command = self.command
        # P3: a chain alone may be up to CHAIN_STDIN_LIMIT bytes (section 5).
        limit = CHAIN_STDIN_LIMIT if command.kind == "chain" else STDIN_LIMIT
        try:
            raw = read_stdin(limit)
        except (OSError, TimeoutError, ValueError):
            return self.refusal(ERR_BAD_INPUT)
        if len(raw) > limit:
            return self.refusal(ERR_INPUT_TOO_LARGE)
        try:
            doc = parse_strict_json(raw)
        except StrictJSONError:
            return self.refusal(ERR_BAD_INPUT)
        try:
            if command.kind == "current":
                parse_current_document(doc)
            elif command.kind == "chain":
                entries = parse_chain_document(doc)
            elif command.mode == "plan":
                job = parse_plan_document(doc)
            else:
                approval = parse_approval_document(doc)
        except ValueError:
            return self.refusal(ERR_BAD_INPUT)
        try:
            if command.kind == "current":
                return self._current()
            if command.kind == "chain":
                return self._chain(entries)
            if command.mode == "plan":
                return self._plan(job)
            return self._approved(approval)
        except Refusal as exc:
            return self.refusal(exc.code, exc.reason, exc.chain)

    # -- K1 to K3: the acknowledgement of what the helper holds

    def _current(self):
        self._rate("current", CURRENT_TARGET)  # K1
        # K2: read without a lock (the file changes only by rename)
        try:
            stored = load_stored_list(self.dir_fd, self.config.uid)
        except StateUnsafe:
            raise Refusal(ERR_STATE_UNSAFE) from None
        self.trust_version = stored.version if stored is not None else None
        # K3
        if stored is None:
            body = {
                "helperTrustListVersion": None,
                "closed": False,
                "signedByCurrentKey": False,
                "trustListSHA256": None,
            }
        else:
            body = {
                "helperTrustListVersion": stored.version,
                "closed": stored.closed,
                "signedByCurrentKey": stored_list_is_current(stored, self.pin),
                "trustListSHA256": stored.body_sha256,
            }
        body["finalRefusals"] = []
        body["at"] = format_time(self._now())
        return signed_answer(self.key, "ack", body), 0

    # -- C1 to C5: a trust-list chain

    def _chain(self, entries):
        """C1 to C5 (section 12, v5.3).  No device approval, and no clock check."""
        self._rate("chain", CHAIN_TARGET)  # C1: the rate lock alone, released before any other lock
        # C2: read without a lock (the file changes only by rename).  A held list whose body hash is
        # not the `toSHA256` of its history line is damaged or forked: `state_unsafe`, with no version
        # reported, since the helper cannot vouch for what it read.
        try:
            held = load_stored_list(self.dir_fd, self.config.uid)
        except StateUnsafe:
            raise Refusal(ERR_STATE_UNSAFE) from None
        if held is not None and not history_hash_matches(self.pin, held.version, held.body_sha256):
            raise Refusal(ERR_STATE_UNSAFE)
        self.trust_version = held.version if held is not None else None
        # C3: the whole chain against C2's list and P1's copy of /etc/muster.  Nothing is recorded,
        # because a refusal here is never final (section 11, 17A).
        try:
            newest = walk_chain(entries, held, self.pin)
        except ChainRefused as refused:
            raise Refusal(
                ERR_CHAIN_REFUSED,
                reason=refused.reason,
                chain={
                    "version": refused.entry.version,
                    "entrySHA256": refused.entry.entry_sha256,
                    "final": False,
                },
            ) from None
        with contextlib.ExitStack() as locked:
            # C4: the action lock, then the stored list again.
            try:
                locked.enter_context(self.backend.lock())
            except LockBusy:
                raise Refusal(ERR_ACTION_BUSY) from None
            except LockUnsafe:
                raise Refusal(ERR_STATE_UNSAFE) from None
            self._check_chain_unchanged(held)
            return self._store_chain(newest, held)

    def _check_chain_unchanged(self, held):
        """C4: the stored list must be the one C2 read, whatever changed it meanwhile.

        Its body hash must be C2's, and a list must be held now exactly when C2 found one: a list stored
        where C2 found none, and one removed, are `trust_list_changed` as well."""
        try:
            again = load_stored_list(self.dir_fd, self.config.uid)
        except StateUnsafe:
            raise Refusal(ERR_STATE_UNSAFE) from None
        if (again is None) != (held is None) or (
            again is not None and again.body_sha256 != held.body_sha256
        ):
            raise Refusal(ERR_TRUST_LIST_CHANGED)

    def _store_chain(self, newest, held):
        """C5, under the action lock: the newest entry written durably, then the audit row, then the
        signed acknowledgement of the list just written.  Temporary files that a crashed write left are
        swept first (`sweep_stored_list_temporaries`).

        A failure before the rename is `internal_error` and nothing is stored (`_replace_file` removes
        its temporary file).  A failure after it (the directory `fsync`, the signing) is `internal_error`
        too, but the list is held: the sender learns that from `trust-list current`, and the same chain
        sent again is `version_gap`.  A failed audit append is not a fault: the list is durable."""
        sweep_stored_list_temporaries(self.dir_fd, self.config.uid)
        try:
            _replace_file(
                self.dir_fd, STATE_TRUST_NAME, newest.canonical, self.config.uid, durable=True
            )
        except OSError:
            raise Refusal(ERR_INTERNAL_ERROR) from None
        with contextlib.suppress(Exception):
            self.backend.audit(
                MUSTER_SCOPE,
                TRUST_LIST_VERB,
                CHAIN_TARGET,
                CHAIN_AUDIT_RESULT,
                {
                    "route": ROUTE,
                    "via": VIA,
                    "trustListVersion": newest.version,
                    "trustListSHA256": newest.body_sha256,
                    "fromVersion": None if held is None else held.version,
                },
            )
        body = {
            "helperTrustListVersion": newest.version,
            "closed": newest.closed,
            "signedByCurrentKey": stored_list_is_current(newest, self.pin),
            "trustListSHA256": newest.body_sha256,
            "finalRefusals": [],
            "at": format_time(self._now()),
        }
        return signed_answer(self.key, "ack", body), 0

    # -- R1 to R3: a plan or a read

    def _plan(self, job):
        command = self.command
        claimed = {"jobId": job["jobId"], "jobNonce": job["jobNonce"], "deviceId": job["deviceId"]}
        self.claimed = dict(claimed)
        self._rate("plan", self._change_key())  # R1
        # R2
        try:
            if command.kind == "read":
                read = self.backend.read(command.verb, command.target)
            else:
                plan = self.backend.plan_bytes(command.verb, command.target)
        except BackendRefused as exc:
            if exc.code in PLAN_REFUSED_REASONS:
                raise Refusal(ERR_PLAN_REFUSED, reason=exc.code) from None
            raise Refusal(ERR_INTERNAL_ERROR) from None
        # R3
        self._read_version()
        version = self.trust_version
        now = self._now()
        if command.kind == "read":
            body = {
                "claimed": claimed,
                "host": HOST_FIELD_VALUE,
                "action": command.verb,
                "target": command.target,
                "planSHA256": None,
                "outcome": "planned",
                "detail": printable(read.detail),
                "outputB64": b64u_encode(read.output) if read.output else None,
                "auditId": None,
                "helperTrustListVersion": version,
                "finishedAt": format_time(now),
            }
            return signed_answer(self.key, "result", body), 0
        body = {
            "claimed": claimed,
            "host": HOST_FIELD_VALUE,
            "action": command.verb,
            "target": command.target,
            "plan": plan.decode("ascii"),
            "planSHA256": sha256_hex(plan),
            "helperTrustListVersion": version,
            "issuedAt": format_time(now),
        }
        return signed_answer(self.key, "plan", body), 0

    # -- Y2 to Y6: everything that needs neither the lock nor the ledger

    def _verify(self, approval):
        """Steps Y2 to Y6.  Raises Refusal.  Returns the stored list the approval was checked against.

        `verified` becomes true only once the signature has checked (Y5)."""
        self.verified = False
        self.fields = None
        command = self.command
        # Y2
        try:
            stored = load_stored_list(self.dir_fd, self.config.uid)
        except StateUnsafe:
            raise Refusal(ERR_STATE_UNSAFE) from None
        if stored is not None:
            self.trust_version = stored.version
        if stored is None or not stored_list_is_current(stored, self.pin):
            raise Refusal(ERR_NO_TRUST_LIST)
        # Y3
        if (approval["host"], approval["action"], approval["target"]) != (
            HOST_FIELD_VALUE,
            command.verb,
            command.target,
        ):
            raise Refusal(ERR_FIELD_MISMATCH)
        # Y4
        approval_key = stored.devices.get(approval["deviceId"])
        if approval_key is None or stored.closed:
            raise Refusal(ERR_UNKNOWN_DEVICE)
        # Y5: only the device's approval key, over exactly the approval text
        if not verify_signature(approval_key, approval["signature"], approval_text(approval)):
            raise Refusal(ERR_BAD_SIGNATURE)
        self.verified = True
        self.fields = dict(approval)
        # Y6: the kernel's clock first, then the window, with `now` read once
        if not clock_is_synchronised(self.config.clock_state):
            raise Refusal(ERR_CLOCK_UNSYNCED)
        now = self._now()
        check_window(approval, now)
        return stored, now

    def _approved(self, approval):
        command = self.command
        self.claimed.update({name: approval[name] for name in VERIFIED_FIELDS})
        # There is no rate step here: the `yes` class is counted at Y8, inside the action lock, after
        # the signature, the window and the ledger have passed, so that nothing a stranger can send
        # spends the owner's budget.
        # OPEN-4: signature, device and window are checked before the one action lock is taken, so
        # that a request that cannot be authentic never holds it.
        stored, _ = self._verify(approval)
        with contextlib.ExitStack() as held:
            # Y7
            try:
                held.enter_context(self.backend.lock())
            except LockBusy:
                raise Refusal(ERR_ACTION_BUSY) from None
            except LockUnsafe:
                raise Refusal(ERR_STATE_UNSAFE) from None
            self._check_unchanged(stored)
            return self._locked(approval, command)

    def _check_unchanged(self, stored):
        """Y7: the stored list must be the one Y2 checked, whatever changed it meanwhile."""
        try:
            again = load_stored_list(self.dir_fd, self.config.uid)
        except StateUnsafe:
            raise Refusal(ERR_STATE_UNSAFE) from None
        if again is None or again.body_sha256 != stored.body_sha256:
            raise Refusal(ERR_TRUST_LIST_CHANGED)

    def _locked(self, approval, command):
        now = self._now()

        def admit():
            # The `yes` class's rate step, in the ledger's order (see Ledger.record): after the
            # ledger has been read and the nonce found unused, before the nonce is appended.  Lock
            # order is the action lock first and the rate lock second.
            check_rate(self.dir_fd, self.config.uid, "yes", self._change_key(), now, durable=True)

        # Y8
        try:
            Ledger(self.dir_fd, self.config.uid).record(approval, now, self._on_repair, admit)
        except ReplayedNonce:
            raise Refusal(ERR_REPLAYED_NONCE) from None
        except LedgerCorrupt:
            raise Refusal(ERR_LEDGER_CORRUPT) from None
        except RateLimited:
            raise Refusal(ERR_RATE_LIMITED) from None
        except LedgerAppendFault:
            # Y8c: a fault at the append is a transient fault of the helper, not a state file to
            # repair in a shell.  The nonce may be on disk; a retry of the same approval then reads
            # `replayed_nonce`.
            raise Refusal(ERR_INTERNAL_ERROR) from None
        except (StateUnsafe, OSError):
            raise Refusal(ERR_STATE_UNSAFE) from None
        self.spent = True
        try:
            return self._after_spent(approval, command)
        except Refusal as exc:
            self._audit(exc.code)  # one row, under the lock, now that the nonce is spent
            raise
        except Exception:  # noqa: BLE001 - after Y8 a fault is a result, never a refusal
            return self.fault(INTERNAL_ERROR_DETAIL)

    def _result_body(self, outcome, detail, output=None, audit_id=None):
        approval = self.fields
        return {
            "jobId": approval["jobId"],
            "jobNonce": approval["jobNonce"],
            "approvalNonce": approval["approvalNonce"],
            "deviceId": approval["deviceId"],
            "host": HOST_FIELD_VALUE,
            "action": self.command.verb,
            "target": self.command.target,
            "planSHA256": approval["planSHA256"],
            "outcome": outcome,
            "detail": printable(detail),
            "outputB64": b64u_encode(output.encode("utf-8", "replace")) if output else None,
            "auditId": audit_id,
            "helperTrustListVersion": self.trust_version,
            "finishedAt": format_time(self._now()),
        }

    def fault(self, detail):
        """Section 12, faults: after Y8 the nonce is spent, so a fault is a signed result and never a
        refusal, with a `detail` naming it and its one audit row.  Its outcome is `unknown` when the
        action had begun, or may have, and `failed` only when it provably never did (section 13)."""
        self._audit_failed()
        body = self._result_body(self._fault_outcome(), detail)
        return signed_answer(self.key, "result", body), 1

    def _after_spent(self, approval, command):
        # Y9: resource-policy compares inside fleet_resource_control, under its own lock.
        if command.verb != "resource-policy":
            try:
                plan = self.backend.plan_bytes(command.verb, command.target)
            except BackendRefused as exc:
                if exc.code in PLAN_REFUSED_REASONS:
                    return self.fault("plan_refused: " + exc.code)
                return self.fault(INTERNAL_ERROR_DETAIL)
            if not hmac.compare_digest(sha256_hex(plan), approval["planSHA256"]):
                raise Refusal(ERR_PLAN_MISMATCH)
        # Y10: from here the action may have begun, so a fault is `unknown` unless the backend says
        # that it provably did not.
        self.started = True
        try:
            result = self.backend.run(command.verb, command.target, dict(approval))
        except RunFault as exc:
            self.started = exc.started
            return self.fault("the helper stopped unexpectedly while running")
        except Exception:  # noqa: BLE001 - a run that died must still be answered
            return self.fault("the helper stopped unexpectedly while running")
        if result.audit_id:
            self.audited = True  # the shared run function wrote its own row
        if result.outcome == "refused":
            if result.error_code == ERR_PLAN_MISMATCH:
                raise Refusal(ERR_PLAN_MISMATCH)
            # Refused inside the run, before it ran anything: the action provably did not begin.
            self.started = False
            return self.fault(result.detail or INTERNAL_ERROR_DETAIL)
        if result.outcome not in OUTCOMES or result.outcome == "planned":
            return self.fault(INTERNAL_ERROR_DETAIL)
        body = self._result_body(result.outcome, result.detail, result.output, result.audit_id)
        code = 1 if result.outcome in ("failed", "unknown") else 0
        return signed_answer(self.key, "result", body), code

    def _on_repair(self, what):
        """Section 11: a ledger repair or completion writes its own audit row."""
        with contextlib.suppress(Exception):
            self.backend.audit(
                MUSTER_SCOPE,
                "ledger-repair",
                STATE_LEDGER_NAME,
                what,
                {"route": ROUTE, "via": VIA},
            )


def handle_request(config, backend, ssh_command, argv_ok, read_stdin, trust_files):
    """(answer, exit status) for one `--from-muster` request.  It never raises.

    `backend` offers `lock()`, `target_ok(verb, target)`, `plan_bytes(verb, target)`,
    `read(verb, target)`, `run(verb, target, approval)` and `audit(scope, verb, target, result,
    fields)`.  `read_stdin(limit)` returns at most limit + 1 bytes.  `trust_files` is the loaded
    `muster_trust_files` module."""
    dir_fd = None
    try:
        # P0
        try:
            dir_fd = open_state_dir(config)
            key = load_helper_key(dir_fd, config.uid)
        except (StateUnsafe, KeyUnusable):
            return unsigned_helper_key_missing(), 2
        flow = _Flow(config, backend, key, dir_fd, trust_files)
        try:
            return flow.run(ssh_command, argv_ok, read_stdin)
        except Exception:  # noqa: BLE001 - the last resort, answered with only what was verified
            if flow.spent:
                # Section 12: after Y8 a fault is a result.  The row is written here when nothing
                # inside the lock could write it.
                with contextlib.suppress(Exception):
                    flow._audit_failed()
                body = flow._result_body(flow._fault_outcome(), INTERNAL_ERROR_DETAIL)
                return signed_answer(key, "result", body), 1
            return flow.refusal(ERR_INTERNAL_ERROR)
    except Exception:  # noqa: BLE001 - nothing may escape as a traceback
        return unsigned_helper_key_missing(), 2
    finally:
        if dir_fd is not None:
            with contextlib.suppress(OSError):
                os.close(dir_fd)
