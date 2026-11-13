#!/usr/bin/env python3
"""Write the Worker's Linear token health to a local file for Muster (AFC-478 follow-up).

POSTs a signed request to https://agents.simplewithus.com/internal/health/linear
(src/health.js) and writes ~/.local/state/fleet-tools/worker-health.json, mode 0600,
atomically.  The answer holds ages, counts and fixed codes only.  The signing key is
HEALTH_HMAC_KEY from Fleet-OPS through the shared direct reader, held in memory and never printed
or written.  A reader treats a file older than 45 minutes, missing, malformed or with
another schemaVersion as unknown.  Python 3.9, standard library only.

  fleet-worker-health.py [--url URL] [--write PATH] [--dry-run]

--dry-run signs and calls the route, prints the verdict summary (never the key), and writes
nothing.  Exit 0 on a written or dry-run answer, 1 when the Worker answered badly or not at all
(the file then records state "unknown" with a fixed error code, so Muster shows it).
"""

import argparse
import errno
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

CONTRACT = "simple-tools-health/1"
SIGNATURE_HEADER = "x-simple-tools-health-signature"
USER_AGENT = "fleet-worker-health/1 (AI-Fleet-Coordinator)"
DEFAULT_URL = "https://agents.simplewithus.com/internal/health/linear"
DEFAULT_PATH = os.path.expanduser("~/.local/state/fleet-tools/worker-health.json")
SCHEMA_VERSION = 1
READ_BINDING = "FLEET_WORKER_HEALTH_INFISICAL_READ_TOKEN"
PROJECT_BINDING = "FLEET_WORKER_HEALTH_INFISICAL_PROJECT_ID"
SECRET_NAME = "HEALTH_HMAC_KEY"
READ_TIMEOUT = 20.0


def sign(key, body):
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def request_body(now=None):
    return json.dumps(
        {"contract": CONTRACT, "sent_at": int(now if now is not None else time.time())},
        separators=(",", ":"),
    ).encode("utf-8")


def _shared_client():
    """Load only the reviewed sibling client, including under the LaunchAgent's -I."""
    directory = os.path.dirname(os.path.realpath(__file__))
    path = os.path.join(directory, "fleet_infisical.py")
    client = sys.modules.get("fleet_infisical")
    if client is not None:
        if os.path.realpath(getattr(client, "__file__", "")) != path:
            raise ImportError("shared reader origin refused")
        return client
    spec = importlib.util.spec_from_file_location("fleet_infisical", path)
    if spec is None or spec.loader is None:
        raise ImportError("shared reader unavailable")
    client = importlib.util.module_from_spec(spec)
    sys.modules["fleet_infisical"] = client
    try:
        spec.loader.exec_module(client)
    except Exception:
        if sys.modules.get("fleet_infisical") is client:
            sys.modules.pop("fleet_infisical")
        raise
    return client


def read_key(env=None, reader=None):
    source = dict(os.environ if env is None else env)
    # Keep the existing Infisical-synced value transport; never hand-set it here.
    key = source.get(SECRET_NAME, "").strip()
    if key:
        return key
    token = (source.get(READ_BINDING) or "").strip()
    project = (source.get(PROJECT_BINDING) or "").strip()
    if not token or not project:
        return ""
    try:
        fetch = reader if reader is not None else _shared_client().fetch_project
        values = fetch(
            project, "/", {}, token=token, names=(SECRET_NAME,), timeout=READ_TIMEOUT
        )
        value = values.get(SECRET_NAME)
        return value.strip() if isinstance(value, str) else ""
    except Exception:
        # Fixed no_key health state below; no provider body, credential or fallback.
        return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a 3xx:  the signed request must not be forwarded to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return 0, None


STATES = ("ok", "warn", "fail")


def valid_answer(answer):
    """The full top-level and per-seat shape, checked before anything is persisted."""
    if not isinstance(answer, dict) or answer.get("contract") != CONTRACT:
        return False
    if answer.get("worst") not in STATES or answer.get("bundle") not in ("ok", "invalid"):
        return False
    seats = answer.get("seats")
    if not isinstance(seats, list):
        return False
    for seat in seats:
        if not isinstance(seat, dict) or not isinstance(seat.get("seat"), str):
            return False
        if seat.get("state") not in STATES:
            return False
    return True


def document(status, answer, generated_at=None):
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(generated_at if generated_at is not None else time.time())
    )
    if status == 200 and valid_answer(answer):
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
    """Atomic and private, through one O_NOFOLLOW descriptor for the state folder:  the temporary
    file, the rename and the folder fsync all use it, so a folder swapped for a symlink after the
    check cannot redirect the write (Kody, PR 1477)."""
    directory = os.path.dirname(path)
    if os.path.islink(directory):
        raise OSError("state directory is a symlink; refusing to write")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise OSError("state directory is a symlink; refusing to write") from error
        raise
    try:
        tmp = ".worker-health-%d-%s" % (os.getpid(), secrets.token_hex(8))
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(doc, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, os.path.basename(path), src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
