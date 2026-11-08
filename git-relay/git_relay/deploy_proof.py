"""Own-seat, no-write runtime proof for this exact existing-service rollout.

The bearer is supplied from a private local file by the fixed root entry point;
never persisted in the manifest, printed, transmitted elsewhere or returned.
Mutation-free by construction: the negative probe pins the git null object id
(see NULL_OID) as both expected head and base blob, preconditions no provider
write can satisfy, so no answer - expected or not - can produce a mutation.
"""

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

# The git null object id. It never names a real object: a branch head is always
# an existing commit (the null id marks an unborn or deleted ref), and a blob
# whose SHA-1 is all zero would require a preimage. Pinning it as the probe's
# expected_head_sha and base_blob_sha makes every write precondition
# unsatisfiable, at every layer, for any service that could answer the probe:
# the relay's ref/commit/blob comparisons cannot match it, and the provider's
# createCommitOnBranch enforces expectedHeadOid server-side.
NULL_OID = "0" * 40

# One-time proof ids: a bounded git-path-safe charset, because the id rides the
# negative probe's PATH (the structured, non-secret field the relay audits for
# a denied workflow call - never the message or edits, which can carry a
# private payload).
PROOF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def probe_path(proof_id):
    """The negative probe's path, carrying the one-time proof id into the audit row."""
    if not isinstance(proof_id, str) or not PROOF_ID.fullmatch(proof_id):
        raise ValueError("invalid proof id")
    return "deploy-proof/%s.probe" % proof_id


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RuntimeProof:
    URL = "http://127.0.0.1:8471/v1/"
    REPO = "Simple-With-Us/AI-Fleet-Coordinator"

    def __init__(self, bearer, opener=None):
        self.bearer = bearer
        self.opener = opener or build_opener(NoRedirect())

    def call(self, action, args):
        raw = json.dumps(args, separators=(",", ":")).encode()
        request = Request(
            self.URL + action,
            data=raw,
            headers={"Authorization": "Bearer " + self.bearer, "Content-Type": "application/json"},
        )
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as response:
            status = response.code
            with response:
                raw = response.read(65537)
        else:
            with response:
                status, raw = response.status, response.read(65537)
        if len(raw) > 65536:
            return None, None
        try:
            return status, json.loads(raw)
        except (ValueError, UnicodeError):
            return None, None

    def __call__(self, identity, hashes):
        # Real own-App authenticated read proves normal policy/minter path.
        status, repo = self.call("wf_repo_get", {"repo": self.REPO})
        if (
            status != 200
            or not isinstance(repo, dict)
            or repo.get("repository", {}).get("full_name") != self.REPO
            or repo.get("repository", {}).get("default_branch") != "main"
        ):
            return False
        status, result = self.call(
            "wf_file_patch",
            {
                "repo": self.REPO,
                "branch": "main",
                "path": "README.md",
                "message": "Bounded runtime negative probe: default branch must refuse",
                "expected_head_sha": NULL_OID,
                "base_blob_sha": NULL_OID,
                "edits": [{"start": 0, "end": 0, "text": ""}],
            },
        )
        if hashes.get("code/provenance_patch.py") is None:
            # Original image does not know the new action. Its refusal precedes
            # policy/outcome/provider effects and proves the old dispatch table.
            return status == 400 and result == {"error": "unknown action 'wf_file_patch'"}
        return status == 400 and result == {"error": "default branch is not writable"}


# Every refusal the guarded write chain may answer the negative probe with, by
# image generation: the old dispatch table refuses the action itself, the newer
# one refuses the default branch before any provider mutation. Accepting the
# set, and recording which one answered, is what lets the external proof run
# without assuming the deployed image's manifest digest was already logged.
REFUSALS = frozenset({"unknown action 'wf_file_patch'", "default branch is not writable"})

PROVEN = "proven"
REFUSED = "refused"
INCONCLUSIVE = "inconclusive"

RECEIPT_WINDOW_S = 600


class ExternalProof(RuntimeProof):
    """Own-seat, no-write runtime proof from outside the relay host.

    Same probes as the local proof, against the relay's external URL, plus a
    receipt the deployment transaction can audit. The negative probe's PATH
    carries the one-time proof id: the relay audits a denied workflow call's
    allowlisted scalars (path among them), never the message or edits, so the
    actual audit row is findable by exactly that id. The proof never assumes
    the manifest digest of the deployed image was already logged: it accepts
    the documented refusal set and records which refusal answered.

    The negative probe is mutation-free by construction, not merely denied
    first: it pins NULL_OID as expected head and base blob, which no ref,
    commit or blob can equal, so even a service that unexpectedly ALLOWS the
    call cannot complete the provider write. An unexpected answer is recorded
    REFUSED and halts the transaction regardless.

    The record binds the exact policy transaction it proves for: policy_before
    and policy_after are the full canonical digests (seat_add_policy.digest) of
    the live policy and of the live policy plus the one added seat.

    Timeout recovery contract: a transport error or timeout yields an
    INCONCLUSIVE record, never a signed success. An inconclusive probe left the
    relay's state unknown to the caller, so a retry must use a fresh proof id;
    sign_receipt refuses any record whose outcome is not PROVEN.
    """

    URL = "https://agents.simplewithus.com/v1/"

    def __call__(self, seat, proof_id, policy_before, policy_after):
        path = probe_path(proof_id)
        record = {
            "v": 2,
            "seat": seat,
            "proof_id": proof_id,
            "ts": int(time.time()),
            "url": self.URL,
            "repo": self.REPO,
            "probe_path": path,
            "policy_before": policy_before,
            "policy_after": policy_after,
        }
        try:
            status, repo = self.call("wf_repo_get", {"repo": self.REPO})
        except OSError:
            # Timeout or transport failure: the relay's state is unknown to the
            # caller, so this is never a proof. Retry with a fresh proof id.
            return dict(record, outcome=INCONCLUSIVE, stage="read")
        if (
            status != 200
            or not isinstance(repo, dict)
            or repo.get("repository", {}).get("full_name") != self.REPO
            or repo.get("repository", {}).get("default_branch") != "main"
        ):
            return dict(record, outcome=REFUSED, stage="read", status=status)
        probe = {
            "repo": self.REPO,
            "branch": "main",
            "path": path,
            "message": "Bounded external negative probe %s: default branch must refuse" % proof_id,
            "expected_head_sha": NULL_OID,
            "base_blob_sha": NULL_OID,
            "edits": [{"start": 0, "end": 0, "text": ""}],
        }
        try:
            status, result = self.call("wf_file_patch", probe)
        except OSError:
            return dict(record, outcome=INCONCLUSIVE, stage="probe")
        refusal = isinstance(result, dict) and result.get("error")
        if status == 400 and refusal in REFUSALS:
            return dict(record, outcome=PROVEN, refusal=refusal)
        return dict(record, outcome=REFUSED, stage="probe", status=status)


def _canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


def sign_receipt(key, record):
    """Bind a PROVEN record to the operator's proof key; never sign another outcome."""
    if not isinstance(record, dict) or record.get("outcome") != PROVEN:
        raise ValueError("only a proven record is signed")
    detached = json.loads(_canonical(record))
    return {
        "record": detached,
        "hmac": hmac.new(key, _canonical(record), hashlib.sha256).hexdigest(),
    }


class ReplayStore:
    """Durable accepted-proof store: survives a process restart, fails closed.

    One small JSON document, rewritten atomically (same-directory temporary
    file, fsync, os.replace, directory fsync). A missing file is an empty
    store; a malformed one refuses everything (it is never silently reset). An
    id is recorded only after every receipt gate has passed, and once recorded
    it is never dropped: a replayed receipt is refused on every later use, by
    any process reading this file.
    """

    MAX_ENTRIES = 1024
    _FIELDS = ("proof_id", "ts", "seat", "policy_before", "policy_after")

    def __init__(self, path):
        self._path = path
        try:
            with open(path, "rb") as fh:
                raw = fh.read(1 << 20)
        except FileNotFoundError:
            self._entries = []
            return
        except OSError as exc:
            raise ValueError("proof replay store unreadable") from exc
        try:
            doc = json.loads(raw.decode("utf-8"))
            entries = doc["accepted"]
            if not isinstance(entries, list):
                raise TypeError()
            self._entries = [
                {field: e[field] for field in self._FIELDS} for e in entries if isinstance(e, dict)
            ]
            for entry in self._entries:
                if (
                    not isinstance(entry["proof_id"], str)
                    or not isinstance(entry["ts"], int)
                    or any(not isinstance(entry[f], str) for f in self._FIELDS if f != "ts")
                ):
                    raise TypeError()
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError("proof replay store malformed") from exc

    def seen(self, proof_id):
        return any(entry["proof_id"] == proof_id for entry in self._entries)

    def add(self, record):
        if len(self._entries) >= self.MAX_ENTRIES:
            raise ValueError("proof replay store at capacity; reconcile it")
        self._entries.append({field: record[field] for field in self._FIELDS})
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".proof-store-", suffix=".tmp")
        try:
            data = json.dumps({"v": 1, "accepted": self._entries}, sort_keys=True).encode()
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write to proof replay store")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp, self._path)
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)


def _audit_row_binds(record, rows):
    """Exactly one actual relay audit row must name this proof run.

    The row the relay wrote for the denied probe carries the allowlisted
    scalars: the same seat, action and repo, a denial outcome, the probe path
    (which names the one-time proof id) and exactly the refusal the proof
    recorded. The receipt is the operator's signed claim; this is the
    authoritative half, from the relay's own log.
    """
    matches = 0
    for row in rows or ():
        if (
            isinstance(row, dict)
            and row.get("seat") == record["seat"]
            and row.get("action") == "wf_file_patch"
            and row.get("repo") == record["repo"]
            and row.get("result") in ("bad_request", "denied")
            and row.get("path") == record["probe_path"]
            and row.get("reason") == record.get("refusal")
        ):
            matches += 1
    if matches != 1:
        raise ValueError("proof not bound to the relay audit")


def read_audit_rows(path, max_bytes=4 * 1024 * 1024):
    """Parse the relay's append-only audit log into rows; any malformed line refuses."""
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("audit log too large to verify against")
    rows = []
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def verify_receipt(
    key,
    receipt,
    *,
    seat,
    now,
    store,
    policy_before,
    policy_after,
    audit_rows,
    window_s=RECEIPT_WINDOW_S,
    store_id=True,
):
    """Return the proven record, or refuse it.

    Gates, in order: shape, HMAC (forged or tampered), outcome (only PROVEN is
    a proof), staleness (outside the window), seat (names another seat),
    transaction binding (names another policy before/after pair), probe-path
    consistency (path must be probe_path(proof_id)), replay (the durable store
    has already accepted this id), and audit binding (the relay's own log must
    hold exactly one matching denial row). The id enters the durable store only
    after every gate passes, so a crash before the store write can never accept
    a receipt it later replays - the retry simply runs the gates again.

    store_id=False is the guarded transaction's dry-run check: every gate runs,
    nothing is stored, and the same receipt still verifies at apply time, which
    runs the gates again and records. Nothing but the dry-run path may pass False.
    """
    if not isinstance(receipt, dict) or set(receipt) != {"record", "hmac"}:
        raise ValueError("receipt shape")
    record, signature = receipt["record"], receipt["hmac"]
    if not isinstance(record, dict) or not isinstance(signature, str):
        raise ValueError("receipt shape")
    expected = hmac.new(key, _canonical(record), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("receipt forged")
    if record.get("outcome") != PROVEN:
        raise ValueError("receipt is not a proof")
    ts = record.get("ts")
    if not isinstance(ts, int) or not 0 <= now - ts <= window_s:
        raise ValueError("receipt stale")
    if record.get("seat") != seat:
        raise ValueError("receipt names another seat")
    if record.get("policy_before") != policy_before or record.get("policy_after") != policy_after:
        raise ValueError("receipt names another transaction")
    proof_id = record.get("proof_id")
    if record.get("probe_path") != probe_path(proof_id):
        raise ValueError("receipt probe path inconsistent")
    if store.seen(proof_id):
        raise ValueError("receipt replayed")
    _audit_row_binds(record, audit_rows)
    if store_id:
        store.add(record)
    return record
