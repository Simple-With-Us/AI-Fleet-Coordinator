"""Pure reviewed prep-to-final transform. No host I/O or activation."""

import hashlib
import json

from .deploy_transaction import TransactionError
from .patch_policy import digest, prepare


def finalize(prep, code_bytes, policy_bytes):
    keys = {
        "config",
        "files",
        "source_commit",
        "policy_raw_before",
        "policy_semantic_before",
        "policy_semantic_after",
    }
    if set(prep) != keys or prep["source_commit"] != "23acbe1cb3a04737caece3602c022f0a534a2aca":
        raise TransactionError("unreviewed preparation source")
    names = [
        "workflow_verbs.py",
        "policy.py",
        "patch_policy.py",
        "provenance_patch.py",
        "patch_secretscan.py",
    ]
    rows = prep["files"]
    if len(rows) != 5 or [r.get("name") for r in rows] != names or set(code_bytes) != set(names):
        raise TransactionError("exact five-file inventory required")
    candidates = []
    for row in rows:
        if (
            set(row) != {"root", "name", "before", "after", "meta"}
            or row["root"] != "code"
            or row["meta"] != {"uid": 0, "gid": 0, "mode": 0o644}
            or row["before"] == row["after"]
            or hashlib.sha256(code_bytes[row["name"]]).hexdigest() != row["after"]
        ):
            raise TransactionError("code candidate or metadata drift")
        candidates.append(code_bytes[row["name"]])
    if hashlib.sha256(policy_bytes).hexdigest() != prep["policy_raw_before"]:
        raise TransactionError("live policy byte drift")
    before = json.loads(policy_bytes)
    after = prepare(before, prep["policy_semantic_before"])
    if len(before) != 41 or digest(after) != prep["policy_semantic_after"]:
        raise TransactionError("live policy semantic or inventory drift")
    raw = (json.dumps(after, sort_keys=True, separators=(",", ":")) + "\n").encode()
    policy_row = {
        "root": "state",
        "name": "seats.json",
        "before": prep["policy_raw_before"],
        "after": hashlib.sha256(raw).hexdigest(),
        "meta": {"uid": 65534, "gid": 65534, "mode": 0o600},
    }
    doc = {
        "config": prep["config"],
        "files": rows + [policy_row],
        "runner": "/opt/git-relay/transactions/patch-1429/runner.py",
        "unit": "git-relay-patch-1429.service",
    }
    encoded = (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # Digest is the exact bytes compiled into the fixed private entry only after
    # independent manifest/source boundary qualification. Never caller input.
    return encoded, hashlib.sha256(encoded).hexdigest(), candidates + [raw]
