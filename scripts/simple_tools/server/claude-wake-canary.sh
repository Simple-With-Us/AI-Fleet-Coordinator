#!/usr/bin/env bash
# One correlated canary wake to one named Claude letter (OPS-1048, AFC-694).
#
#   bash scripts/simple_tools/server/claude-wake-canary.sh --letter AA --message-id 639559626 --dry-run
#   bash scripts/simple_tools/server/claude-wake-canary.sh --letter AA --message-id 639559626 --test
#   bash scripts/simple_tools/server/claude-wake-canary.sh --letter AA --message-id 639559626
#
# It sends exactly one simple-tools-wake/1 body to the Claude wake router, addressed to one letter
# by the explicit `letter` field, then prints the wake id, the pick and the receipt, and stops.  It
# never sends a second wake:  a later run makes a new wake id.  Run it on the server as the unit's
# user, against the router's loopback port, after the owner has installed and armed the router.
#
#   --letter XX        the letter to wake (required;  one or two capital letters)
#   --message-id N     the Zulip message the woken chat is told to read (required).  Post the canary
#                      message in the work topic first, so the chat has something real to answer.
#   --dry-run          print the body and the target, send nothing
#   --test             send it with "test": true:  the router answers at once with its pick and
#                      delivers nothing (wake_router, The test flag)
#   --url URL          the router (default http://127.0.0.1:${CLAUDE_WAKE_PORT:-8794})
#   --key-file F       the routine key (default $CLAUDE_WAKE_KEY_FILE, else
#                      ~/.local/state/claude-wake/routine-key).  Read in-process, sent only in the
#                      Authorization header, never printed and never on a command line
#   --wait SECONDS     how long to poll GET /receipt/<id> for a final receipt (default 1100, longer
#                      than a headless resume or a Mac delivery can take);  0 does not poll
#   --wake-id ID       use this id instead of a new canary-<utc>-<hex> one (a resend of the same id
#                      is answered from the router's record and starts nothing new)
#   --topic T, --channel C   carried as metadata (default AFC-694 Operator cross-platform
#                      coordination, Work-In-Progress)
#
# Exit:  0 delivered (or a test answer), 3 held (delivery is off), 4 dead, 5 still pending when
# --wait ran out, 1 a transport or router error, 2 bad arguments.
#
# The evidence for AFC-694 is the same wake id in three places:  this output, the router's receipt
# (also in ~/.local/state/claude-wake/done/<wake_id>.json) and the woken chat, whose prompt names the
# wake id.  This script shows the first two;  the third is read in that chat.
set -euo pipefail

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { echo "claude-wake-canary: python3 is missing" >&2; exit 1; }

exec "$PYTHON" -I - "$@" <<'PY'
import argparse
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

CONTRACT = "simple-tools-wake/1"
SEAT = "CLAUDE"
WAKE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FINAL = {"delivered": 0, "test": 0, "held": 3, "dead": 4}
ZULIP = "https://simplewithus.zulipchat.com/#narrow/near/%d"
# Offline tests shorten the poll;  nothing else sets this.
POLL_SECONDS = float(os.environ.get("CLAUDE_WAKE_CANARY_POLL_SECONDS") or 5)
HTTP_TIMEOUT = 30


def fail(message, code=1):
    print("claude-wake-canary: " + message, file=sys.stderr)
    raise SystemExit(code)


def read_key(path):
    """The routine key, under the router's own rule:  a regular file, no group or other access,
    at least 16 characters.  Only its presence is ever reported."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        fail("cannot open the routine key file (missing, or a symlink)")
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            fail("the routine key file must be a regular file with no group or other access")
        key = handle.read(4096).decode("utf-8", "replace").strip()
    if len(key) < 16:
        fail("the routine key is shorter than 16 characters")
    return key


def request(method, url, key, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, OSError, ValueError) as exc:
        fail("the router did not answer at %s (%s)" % (url, type(exc).__name__))


def show(label, record):
    print("%s: %s" % (label, json.dumps(record, sort_keys=True)))


def summary(record):
    keys = ("status", "picked", "picked_letter", "picked_generation", "session_id", "reason",
            "delivered", "route", "ack", "code", "attempts")
    return {k: record.get(k) for k in keys if k in record}


def main(argv):
    parser = argparse.ArgumentParser(prog="claude-wake-canary.sh")
    parser.add_argument("--letter", required=True)
    parser.add_argument("--message-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--url", default="")
    parser.add_argument("--key-file", default="")
    parser.add_argument("--wait", type=int, default=1100)
    parser.add_argument("--wake-id", default="")
    parser.add_argument("--topic", default="AFC-694 Operator cross-platform coordination")
    parser.add_argument("--channel", default="Work-In-Progress")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        raise SystemExit(2 if exc.code else 0)

    if not re.fullmatch(r"[A-Z]{1,2}", args.letter):
        fail("--letter must be one or two capital letters", 2)
    if args.message_id <= 0:
        fail("--message-id must be a Zulip message id", 2)
    if args.wait < 0:
        fail("--wait must be 0 or more", 2)
    wake_id = args.wake_id or "canary-%s-%s" % (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(4))
    if not WAKE_ID_RE.fullmatch(wake_id):
        fail("--wake-id is malformed", 2)
    url = (args.url or "http://127.0.0.1:%s" % (os.environ.get("CLAUDE_WAKE_PORT") or "8794"))
    url = url.rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+", url):
        fail("--url must be a scheme, a host and an optional port, with no path", 2)
    key_file = args.key_file or os.environ.get("CLAUDE_WAKE_KEY_FILE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "claude-wake", "routine-key")

    body = {
        "contract": CONTRACT,
        "seat": SEAT,
        "wake_id": wake_id,
        "message_id": args.message_id,
        "channel": args.channel,
        "topic": args.topic,
        "zulip_link": ZULIP % args.message_id,
        "sender_full_name": "wake canary",
        "letter": args.letter,
        "excerpt": "",
    }
    if args.test:
        body["test"] = True
    print("wake_id: " + wake_id)
    print("target: %s/wake/%s letter %s" % (url, SEAT, args.letter))
    if args.dry_run:
        show("body", body)
        print("dry run: nothing sent")
        return 0

    key = read_key(key_file)
    status, answer = request("POST", "%s/wake/%s" % (url, SEAT), key, body)
    if status not in (200, 202):
        show("refused", {"http": status, **answer})
        return 1
    if answer.get("wake_id") not in (None, wake_id):
        fail("the router answered for another wake id")
    if args.test or answer.get("test"):
        show("pick", summary(answer))
        show("receipt", answer)
        return 0
    show("accepted", {"http": status, **answer})

    deadline = time.monotonic() + args.wait
    record = answer
    while record.get("status") not in FINAL and time.monotonic() < deadline:
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        code, record = request("GET", "%s/receipt/%s" % (url, wake_id), key)
        if code != 200:
            show("refused", {"http": code, **record})
            return 1
    if record.get("status") not in FINAL:
        print("pending: no final receipt within %d s; read it later at GET /receipt/%s"
              % (args.wait, wake_id))
        return 5
    show("pick", summary(record))
    show("receipt", record)
    return FINAL[record["status"]]


raise SystemExit(main(sys.argv[1:]))
PY
