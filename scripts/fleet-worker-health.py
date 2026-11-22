#!/usr/bin/env python3
"""Write the Worker's Linear token health to a local file for Muster (AFC-478 follow-up).

POSTs a signed request to https://agents.simplewithus.com/internal/health/linear
(src/health.js) and writes ~/.local/state/fleet-tools/worker-health.json, mode 0600,
atomically.  The answer holds ages, counts and fixed codes only.  The signing key is
HEALTH_HMAC_KEY from Fleet-OPS, read into memory through this script's own scoped read-only
service token and never printed or written.  A reader treats a file older than 45 minutes, missing, malformed or with
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
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CONTRACT = "simple-tools-health/1"
SIGNATURE_HEADER = "x-simple-tools-health-signature"
USER_AGENT = "fleet-worker-health/1 (AI-Fleet-Coordinator)"
DEFAULT_URL = "https://agents.simplewithus.com/internal/health/linear"
DEFAULT_PATH = os.path.expanduser("~/.local/state/fleet-tools/worker-health.json")
SCHEMA_VERSION = 1
TOKEN_ACCOUNT = "worker-health-ro"
SECRET_FOLDER = "/"
# The canonical Infisical command source beside this script.  It is the
# reviewed reader this host's fleet tools share,  so its absence means this
# host has no reader at all.
INFISICAL_CLI = os.path.join(os.path.dirname(os.path.realpath(__file__)), "fleet_infisical_cli.py")


def sign(key, body):
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def request_body(now=None):
    return json.dumps(
        {"contract": CONTRACT, "sent_at": int(now if now is not None else time.time())},
        separators=(",", ":"),
    ).encode("utf-8")


def read_key():
    """The signing key:  the environment value first, else this script's own scoped token
    against Infisical Fleet-OPS prod root.  "" when the folder holds no such name.

    A source that failed is not a missing value:  the shared client,  the token store and
    the read itself each raise,  naming the reader and the account,  so the source that
    must be repaired stays visible.  The key is never logged.
    """
    key = os.environ.get("HEALTH_HMAC_KEY", "").strip()
    if key:
        return key
    if not os.path.isfile(INFISICAL_CLI):
        # A reader that is not there is a broken reader,  not a missing value:  the
        # source that must be repaired is named,  so it stays visible.
        raise RuntimeError(
            "the Infisical reader (fleet_infisical_cli.py) is missing beside this script,  "
            "so the " + TOKEN_ACCOUNT + " scoped service token cannot be read"
        )
    try:
        import fleet_infisical as FI
    except ImportError:
        raise RuntimeError(
            "the shared Infisical client (fleet_infisical.py) is not importable,  so the "
            + TOKEN_ACCOUNT
            + " scoped service token cannot be read"
        ) from None
    env = dict(os.environ)
    env[FI.ENV_READ_PATHS] = SECRET_FOLDER
    try:
        value, status = FI.fetch_scoped(
            "HEALTH_HMAC_KEY",
            SECRET_FOLDER,
            TOKEN_ACCOUNT,
            env,
            require_finite_expiry=True,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            "the Infisical read for the "
            + TOKEN_ACCOUNT
            + " scoped service token timed out ("
            + type(exc).__name__
            + ")"
        ) from None
    except Exception as exc:
        raise RuntimeError(
            "the Infisical read for the "
            + TOKEN_ACCOUNT
            + " scoped service token could not run ("
            + type(exc).__name__
            + ")"
        ) from None
    if status == "absent":
        # The folder holds no such name:  that is a missing value,  and the caller
        # sees it as one.
        return ""
    if status != "ok":
        raise RuntimeError(
            "the "
            + TOKEN_ACCOUNT
            + " scoped service token store is unavailable or refused ("
            + status
            + ")"
        )
    return value


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
    try:
        key = read_key()
    except RuntimeError as exc:
        # The source that must be repaired is named,  and the run stops:  a report
        # written without the key would say nothing about why.
        sys.stderr.write("fleet-worker-health: %s\n" % exc)
        return 2
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
