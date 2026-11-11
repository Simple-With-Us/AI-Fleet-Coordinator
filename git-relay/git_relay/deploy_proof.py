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
import urllib.parse
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


def validate_operator_url(url: str) -> str:
    """The supported operator endpoint shape, enforced at both edges.

    The prover (BootstrapProof(url=...)) and the installer
    (GIT_RELAY_OPERATOR_URL) validate through this one helper so the exact
    same URL string is bound in the proof record and the receipt verifier.
    Only loopback http(s) with an explicit port and the exact /v1/ path is
    supported;  userinfo, query and fragment are refused, so a bearer is
    never sent to an arbitrary external or malformed endpoint.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("operator url: empty")
    # urlsplit silently strips ASCII newlines/tabs and treats an empty
    # query/fragment delimiter as absent:  validate the RAW string so the
    # exact bytes validated are the exact bytes used.
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise ValueError("operator url: control or whitespace bytes are not allowed")
    if "?" in url or "#" in url:
        raise ValueError("operator url: query and fragment are not allowed")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("operator url: scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("operator url: userinfo is not allowed")
    if parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("operator url: host must be loopback")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("operator url: invalid port") from None
    if port is None:
        raise ValueError("operator url: explicit port required")
    if port == 0:
        raise ValueError("operator url: port 0 is not a real endpoint")
    if parsed.path != "/v1/":
        raise ValueError("operator url: path must be exactly /v1/")
    return url


class RuntimeProof:
    URL = "http://127.0.0.1:8471/v1/"
    REPO = "Simple-With-Us/AI-Fleet-Coordinator"
    # The in-host proof against localhost needs no User-Agent; the external
    # proof names itself explicitly (the front-end filter rejects anonymous
    # Python agents).  See ExternalProof.USER_AGENT.
    USER_AGENT = None

    def __init__(self, bearer, opener=None, url=None):
        self.bearer = bearer
        self.opener = opener or build_opener(NoRedirect())
        if url is not None:
            # Operator-listener override:  validated to the supported
            # loopback endpoint shape before any bearer is sent to it.
            self.URL = validate_operator_url(url)

    def call(self, action, args):
        raw = json.dumps(args, separators=(",", ":")).encode()
        headers = {"Authorization": "Bearer " + self.bearer, "Content-Type": "application/json"}
        if self.USER_AGENT:
            headers["User-Agent"] = self.USER_AGENT
        request = Request(self.URL + action, data=raw, headers=headers)
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
        repo_info = repo.get("repository") if isinstance(repo, dict) else None
        if (
            status != 200
            or not isinstance(repo_info, dict)
            or repo_info.get("full_name") != self.REPO
            or repo_info.get("default_branch") != "main"
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
# without assuming the deployed image's manifest digest was already logged. The
# old-generation refusal is reason-only: install-seat's receipt bind requires
# the updated image's audited scalars and fails closed against a pre-rollout
# relay with deployment-order guidance (see _audit_row_binds).
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
    VERSION = 2
    USER_AGENT = "swu-git-relay-external-proof/2"

    def __call__(self, seat, proof_id, policy_before, policy_after):
        # The record and its audit correlation are only meaningful for an
        # actual seat and proof id:  refuse empty or non-string inputs before
        # anything is built or called.  probe_path validates the id's shape.
        if not isinstance(seat, str) or not seat:
            raise ValueError("invalid seat")
        path = probe_path(proof_id)
        record = {
            "v": self.VERSION,
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
        repo_info = repo.get("repository") if isinstance(repo, dict) else None
        if (
            status != 200
            or not isinstance(repo_info, dict)
            or repo_info.get("full_name") != self.REPO
            or repo_info.get("default_branch") != "main"
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


BOOTSTRAP_CAUSE = "seat_not_in_policy"


class BootstrapProof(RuntimeProof):
    """First-seat, no-write runtime proof for a seat the policy does not yet name.

    The ordinary ExternalProof is unrunable for a first seat:  its read must
    answer 200, and an unconfigured seat is refused before that.  The
    bootstrap proof instead certifies the exact pre-activation state:  BOTH
    the read and the NULL_OID negative probe are refused with exactly
    seat_not_in_policy, and the relay audits each denial against the
    verifier-confirmed identity.  PROVEN means "the exact seat to add is
    currently denied, the denial is the audited pre-policy cause, and no
    mutation was possible" (NULL_OID preconditions, no successful read
    claimed);  any other answer - including an unexpected 200, which would
    mean the identity already resolves - is REFUSED.

    PROVEN requires the relay's operator-admission marker on BOTH refusals:
    only the operator listener (make_handler(operator_bootstrap=True)) emits
    it, so a run against the public listener is REFUSED with
    no_operator_admission instead of producing a proven-but-uninstallable
    receipt.  The operator URL override is validated (loopback http(s) only,
    explicit port, exact /v1/ path, no userinfo/query/fragment) and
    install-seat binds the same URL string through verify_bootstrap_receipt.
    """

    URL = "https://agents.simplewithus.com/v1/"
    VERSION = 3
    USER_AGENT = "swu-git-relay-bootstrap-proof/3"

    def __call__(self, seat, identity, proof_id, policy_before, policy_after):
        if not isinstance(seat, str) or not seat:
            raise ValueError("invalid seat")
        if not isinstance(identity, str) or not identity:
            raise ValueError("invalid identity")
        path = probe_path(proof_id)
        record = {
            "v": self.VERSION,
            "seat": seat,
            # The prover's claimed identity;  verify_bootstrap_receipt binds
            # it to the relay's verifier-confirmed identity and to the seat
            # row's configured claim, so claim == verified == configured.
            "identity": identity,
            "proof_id": proof_id,
            "ts": int(time.time()),
            "url": self.URL,
            "repo": self.REPO,
            "probe_path": path,
            "policy_before": policy_before,
            "policy_after": policy_after,
        }
        try:
            status, result = self.call("wf_repo_get", {"repo": self.REPO})
        except OSError:
            return dict(record, outcome=INCONCLUSIVE, stage="read")
        if status != 401 or not isinstance(result, dict) or result.get("error") != BOOTSTRAP_CAUSE:
            return dict(record, outcome=REFUSED, stage="read", status=status)
        if result.get("bootstrap_admission") != "operator":
            # The refusal carries no operator-admission marker:  this run
            # reached the public listener, whose generic bounded row binds
            # no verifier-confirmed identity, so a receipt from it would be
            # uninstallable.  Refuse instead of claiming proof.
            return dict(record, outcome=REFUSED, stage="read", status=status,
                        reason="no_operator_admission")
        # Byte-identical to ExternalProof's negative probe for the same proof
        # id:  the flat wf_file_patch request whose NULL_OID preconditions
        # (expected_head_sha, base_blob_sha) make a mutation impossible, so
        # only the refusal differs between the two proofs.
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
        if (
            status == 401
            and isinstance(result, dict)
            and result.get("error") == BOOTSTRAP_CAUSE
            and result.get("bootstrap_admission") == "operator"
        ):
            return dict(record, outcome=PROVEN, refusal=BOOTSTRAP_CAUSE)
        if status == 401 and isinstance(result, dict) and result.get("error") == BOOTSTRAP_CAUSE:
            return dict(record, outcome=REFUSED, stage="probe", status=status,
                        reason="no_operator_admission")
        return dict(record, outcome=REFUSED, stage="probe", status=status)


def _row_binds_path(row, probe_path):
    """The probe path, or its exact redacted stand-in.

    The relay's audit hygiene (audit.py) hashes any detail value that looks
    credential-shaped: a long mixed-class probe path is logged as
    path_sha256 + path_len with the path itself withheld. Binding the hash
    and length of the expected probe path is the same string equality under
    the relay's privacy rule; the path never has to be readable to bind.
    """
    if "path" in row:
        return row.get("path") == probe_path
    return row.get("path_sha256") == hashlib.sha256(probe_path.encode()).hexdigest() and row.get(
        "path_len"
    ) == len(probe_path)


def _audit_row_binds(record, rows):
    """Exactly one actual relay audit row must name this proof run.

    The row the relay wrote for the denied probe carries the allowlisted
    scalars: the same seat, action and repo, a denial outcome, the probe path
    (which names the one-time proof id) and exactly the refusal the proof
    recorded. Only the updated image writes that row: a pre-rollout relay
    refuses wf_file_patch pre-dispatch as an unknown action and audits the
    refusal reason alone, with no scalars to bind. The receipt is the
    operator's signed claim; this is the authoritative half, from the relay's
    own log.
    """
    matches = 0
    unbound_refusal = False
    for row in rows or ():
        if (
            isinstance(row, dict)
            and row.get("seat") == record["seat"]
            and row.get("action") == "wf_file_patch"
            and row.get("repo") == record["repo"]
            and row.get("result") in ("bad_request", "denied")
            and row.get("reason") == record.get("refusal")
        ):
            if _row_binds_path(row, record["probe_path"]):
                matches += 1
            elif "path" not in row and "path_sha256" not in row:
                # The probe reached a relay that predates workflow-file
                # dispatch: its pre-dispatch refusal row carries the reason
                # alone, so no proof id can ever bind to it. Deploy the
                # updated relay image (whose denials audit the allowlisted
                # scalars), then rerun the install.
                unbound_refusal = True
    if matches != 1:
        if unbound_refusal:
            raise ValueError(
                "relay predates workflow-file dispatch: its refusal audit row "
                "carries no bound scalars; deploy the updated relay image, "
                "then rerun the install"
            )
        raise ValueError("proof not bound to the relay audit")


def read_audit_rows(path, max_bytes=4 * 1024 * 1024):
    """Parse recent relay audit rows; any malformed line refuses closed.

    The log is append-only and a verification needs its most recent rows, so
    at most the last max_bytes are read: a log that grows past the cap never
    blocks a proof. The window can open mid-line, so the first partial line
    is dropped; a malformed complete line still refuses.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(-max_bytes, os.SEEK_END)
        raw = fh.read(max_bytes)
    text = raw.decode("utf-8")
    if size > max_bytes:
        newline = text.find("\n")
        text = "" if newline == -1 else text[newline + 1 :]
    rows = []
    for line in text.splitlines():
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
    url=None,
    repo=None,
):
    """Return the proven record, or refuse it.

    Gates, in order: shape, HMAC (forged or tampered), outcome (only PROVEN is
    a proof), proof target (record version, endpoint and repository must be
    exactly the expected ExternalProof values, never another environment's),
    staleness (outside the window), seat (names another seat),
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
    # The receipt must name THIS proof target: the record version, the exact
    # endpoint and the exact repository, not merely any HMAC-valid record for
    # the seat (a proof against another environment must never verify here).
    if url is None:
        url = ExternalProof.URL
    if repo is None:
        repo = ExternalProof.REPO
    if (
        record.get("v") != ExternalProof.VERSION
        or record.get("url") != url
        or record.get("repo") != repo
    ):
        raise ValueError("receipt names another proof target")
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


def _bootstrap_row_binds(record, rows, identity):
    """Exactly one audited bootstrap denial row must name this proof run.

    The bootstrap row is written only after the Worker confirms the bearer,
    so its identity field is the verifier's answer.  The receipt binds when
    exactly one row matches the closed cause, that CONFIRMED identity equal
    to the receipt's claimed identity, the workflow-file action, the proof
    repository, the denial result and the proof-id path.  More than one
    match is as much a refusal as none:  the binding must name one run.
    """
    matches = 0
    identity_sha256 = hashlib.sha256(identity.encode()).hexdigest()
    for row in rows or ():
        if (
            isinstance(row, dict)
            and row.get("cause") == BOOTSTRAP_CAUSE
            and (row.get("identity") == identity or row.get("identity_sha256") == identity_sha256)
            and row.get("action") == "wf_file_patch"
            and row.get("repo") == record["repo"]
            and row.get("result") == "unauthenticated"
            and _row_binds_path(row, record["probe_path"])
        ):
            matches += 1
    if matches != 1:
        raise ValueError("bootstrap proof not bound to the relay audit")


def verify_bootstrap_receipt(
    key,
    receipt,
    *,
    seat,
    identity,
    now,
    store,
    policy_before,
    policy_after,
    audit_rows,
    window_s=RECEIPT_WINDOW_S,
    store_id=True,
    url=None,
    repo=None,
):
    """The bootstrap analogue of verify_receipt;  same gates, bootstrap target.

    Every gate of the external path remains:  shape, HMAC, outcome (only
    PROVEN), staleness, the named seat, both policy digests, probe-path
    consistency, replay, audit binding and replay-store consumption.  Two
    differences:  the record must be exactly a BootstrapProof record (never
    another proof type's), and the record's claimed identity must equal the
    seat row's configured identity - which _bootstrap_row_binds then equates
    with the relay's verifier-confirmed answer.
    """
    url = BootstrapProof.URL if url is None else url
    repo = BootstrapProof.REPO if repo is None else repo
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
    if record.get("refusal") != BOOTSTRAP_CAUSE:
        raise ValueError("receipt is not a proof")
    if (
        record.get("v") != BootstrapProof.VERSION
        or record.get("url") != url
        or record.get("repo") != repo
    ):
        raise ValueError("receipt names another proof target")
    ts = record.get("ts")
    if not isinstance(ts, int) or not 0 <= now - ts <= window_s:
        raise ValueError("receipt stale")
    if record.get("seat") != seat:
        raise ValueError("receipt names another seat")
    if record.get("identity") != identity:
        raise ValueError("receipt names another identity")
    if record.get("policy_before") != policy_before or record.get("policy_after") != policy_after:
        raise ValueError("receipt names another transaction")
    proof_id = record.get("proof_id")
    if record.get("probe_path") != probe_path(proof_id):
        raise ValueError("receipt probe path inconsistent")
    if store.seen(proof_id):
        raise ValueError("receipt replayed")
    _bootstrap_row_binds(record, audit_rows, identity)
    if store_id:
        store.add(record)
    return record
