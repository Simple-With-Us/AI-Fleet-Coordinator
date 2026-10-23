"""The Operator notes API's wire format (AFC-486, docs/protocols/operator-notes.md, "The Wire Protocol").

Shared by the Operator (notes_queue.py), the Python clients (notes_client.py) and the Mac runner
(notes_runner.py);  the hosted Worker's src/notes.js signs the same string.  Three headers:

    X-Agent-Sync-Principal:  WORKER, SERVER or RUNNER
    X-Agent-Sync-Timestamp:  unix seconds
    X-Agent-Sync-Signature:  lowercase hex HMAC-SHA256, keyed with the principal's key, over
        "agent-sync-notes/1\\n<METHOD>\\n<path>\\n<raw query>\\n<timestamp>\\n<principal>\\n<sha256 hex of the body>"

A signature names one principal, one method, one path, one exact query, one body and one moment;  it is good
for five minutes either side of the Operator's clock.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import hmac

CONTRACT = "agent-sync-notes/1"
PREFIX = "/notes/v1/"
PRINCIPAL_HEADER = "X-Agent-Sync-Principal"
TIMESTAMP_HEADER = "X-Agent-Sync-Timestamp"
SIGNATURE_HEADER = "X-Agent-Sync-Signature"
MAX_SKEW_S = 300
MIN_KEY_LEN = 32
MAX_BODY = 4 * 1024 * 1024
WORKER, SERVER, RUNNER = "WORKER", "SERVER", "RUNNER"
PRINCIPALS = (WORKER, SERVER, RUNNER)
# Where each principal's key lives:  Infisical, project AI Fleet Coordinator, prod, this folder.
KEY_NAMES = {
    WORKER: "OPERATOR_NOTES_WORKER_KEY",
    SERVER: "OPERATOR_NOTES_SERVER_KEY",
    RUNNER: "OPERATOR_NOTES_RUNNER_KEY",
}
KEY_PATH = "/operator"
KEY_PROJECT = "ai-fleet-coordinator"
CLAIM_WAIT_MAX = 25
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def signing_string(
    method: str, path: str, query: str, timestamp: str, principal: str, body: bytes
) -> bytes:
    return (
        "%s\n%s\n%s\n%s\n%s\n%s\n%s"
        % (CONTRACT, method.upper(), path, query, timestamp, principal, body_hash(body))
    ).encode("utf-8")


def sign(
    key: str, method: str, path: str, query: str, timestamp: str, principal: str, body: bytes
) -> str:
    return hmac.new(
        key.encode("utf-8"),
        signing_string(method, path, query, timestamp, principal, body),
        hashlib.sha256,
    ).hexdigest()


def headers(
    key: str, method: str, path: str, query: str, principal: str, body: bytes, now: float
) -> dict[str, str]:
    """The three signed headers (and the content type) for one request."""
    timestamp = str(int(now))
    out = {
        PRINCIPAL_HEADER: principal,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(key, method, path, query, timestamp, principal, body),
    }
    if body:
        out["Content-Type"] = "application/json"
    return out
