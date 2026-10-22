#!/usr/bin/env python3
"""Write the Worker's Linear token health to a local file for Muster (AFC-478 follow-up).

POSTs a signed request to https://agents.simplewithus.com/internal/health/linear
(src/health.js) and writes ~/.local/state/fleet-tools/worker-health.json, mode 0600,
atomically.  The answer holds ages, counts and fixed codes only.  The signing key is
HEALTH_HMAC_KEY from Fleet-OPS (`fleet-secrets get`), read into memory and never printed
or written.  A reader treats a file older than 45 minutes, missing, malformed or with
another schemaVersion as unknown.  Python 3.9, standard library only.

  fleet-worker-health.py [--url URL] [--write PATH] [--dry-run]

--dry-run signs and calls the route, prints the verdict summary (never the key), and writes
nothing.  Exit 0 on a written or dry-run answer, 1 when the Worker answered badly or not at all
(the file then records state "unknown" with a fixed error code, so Muster shows it).
"""

import argparse
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

CONTRACT = "agent-sync-health/1"
SIGNATURE_HEADER = "x-agent-sync-health-signature"
USER_AGENT = "fleet-worker-health/1 (AI-Fleet-Coordinator)"
DEFAULT_URL = "https://agents.simplewithus.com/internal/health/linear"
DEFAULT_PATH = os.path.expanduser("~/.local/state/fleet-tools/worker-health.json")
SCHEMA_VERSION = 1


def sign(key, body):
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def request_body(now=None):
    return json.dumps(
        {"contract": CONTRACT, "sent_at": int(now if now is not None else time.time())},
        separators=(",", ":"),
    ).encode("utf-8")


def read_key():
    key = os.environ.get("HEALTH_HMAC_KEY", "").strip()
    if key:
        return key
    binary = shutil.which("fleet-secrets") or os.path.expanduser("~/.local/bin/fleet-secrets")
    try:
        out = subprocess.run(
            [binary, "get", "HEALTH_HMAC_KEY"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def fetch(url, key, now=None):
    body = request_body(now)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            SIGNATURE_HEADER: sign(key, body),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, None


def document(status, answer, generated_at=None):
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(generated_at if generated_at is not None else time.time())
    )
    if status == 200 and isinstance(answer, dict) and answer.get("contract") == CONTRACT:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "tool": "fleet-worker-health",
            "generatedAt": stamp,
            "reachable": True,
            "worst": answer.get("worst"),
            "bundle": answer.get("bundle"),
            "seats": answer.get("seats", []),
        }
    code = (
        "no_answer"
        if status == 0
        else "not_configured_or_refused"
        if status in (401, 404)
        else "http_%d" % status
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": "fleet-worker-health",
        "generatedAt": stamp,
        "reachable": False,
        "worst": "unknown",
        "error": code,
        "seats": [],
    }


def write_atomic(path, doc):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".worker-health-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--write", default=DEFAULT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    key = read_key()
    if not key:
        status, answer = 0, None
        doc = document(status, answer)
        doc["error"] = "no_key"
    else:
        status, answer = fetch(args.url, key)
        doc = document(status, answer)
    if args.dry_run:
        counts = {}
        for seat in doc["seats"]:
            counts[seat.get("state")] = counts.get(seat.get("state"), 0) + 1
        print(
            json.dumps(
                {
                    "reachable": doc["reachable"],
                    "worst": doc["worst"],
                    "seats": len(doc["seats"]),
                    "by_state": counts,
                    "error": doc.get("error"),
                }
            )
        )
    else:
        write_atomic(args.write, doc)
    return 0 if doc["reachable"] else 1


if __name__ == "__main__":
    sys.exit(main())
