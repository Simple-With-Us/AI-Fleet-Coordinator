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
    receipt the deployment transaction can audit: the negative probe's message
    carries the one-time proof id, so the denied call the relay audits (the
    current API records the canonical arguments of a denied call in its audit
    detail) is findable by exactly that id. The proof never assumes the
    manifest digest of the deployed image was already logged: it accepts the
    documented refusal set and records which refusal answered.

    The negative probe is mutation-free by construction, not merely denied
    first: it pins NULL_OID as expected head and base blob, which no ref,
    commit or blob can equal, so even a service that unexpectedly ALLOWS the
    call cannot complete the provider write. An unexpected answer is recorded
    REFUSED and halts the transaction regardless.

    Timeout recovery contract: a transport error or timeout yields an
    INCONCLUSIVE record, never a signed success. An inconclusive probe left the
    relay's state unknown to the caller, so a retry must use a fresh proof id;
    sign_receipt refuses any record whose outcome is not PROVEN.
    """

    URL = "https://agents.simplewithus.com/v1/"

    def __call__(self, seat, proof_id):
        record = {
            "v": 1,
            "seat": seat,
            "proof_id": proof_id,
            "ts": int(time.time()),
            "url": self.URL,
            "repo": self.REPO,
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
            "path": "README.md",
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


def verify_receipt(key, receipt, *, seat, now, seen_proof_ids, window_s=RECEIPT_WINDOW_S):
    """Return the proven record, or refuse: forged, stale, replayed or wrong-seat.

    seen_proof_ids is the caller's accepted-id store; a proof id is added only
    after this accepts, so a replayed receipt is refused on every later use.
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
    if record.get("proof_id") in seen_proof_ids:
        raise ValueError("receipt replayed")
    if record.get("seat") != seat:
        raise ValueError("receipt names another seat")
    return record
