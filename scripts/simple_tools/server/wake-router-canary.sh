#!/usr/bin/env bash
# One correlated canary wake through the one wake router, for any seat (OPS-1048, AFC-694).
#
#   bash scripts/simple_tools/server/wake-router-canary.sh --seat SEAT --letter XX --message-id N --dry-run
#   bash scripts/simple_tools/server/wake-router-canary.sh --seat SEAT --letter XX --message-id N --test
#   bash scripts/simple_tools/server/wake-router-canary.sh --seat SEAT --letter XX --message-id N
#   bash scripts/simple_tools/server/wake-router-canary.sh --seat SEAT --letter XX --follow WAKE_ID
#
# Run it on the server as the jay user, after install-wake-router.sh.  It first reads
# GET /health/<SEAT> (the overall /health can be 503 while another platform is not installed, so
# only the named seat counts), then sends exactly one simple-tools-wake/1 body to /wake/<SEAT>,
# addressed to one letter, and follows that wake's receipt until it is final.  It never sends a
# second wake:  a later run makes a new wake id.  --follow sends nothing and follows a wake the
# Eavesdropper already sent for a real Zulip tag, so the capture, the receipt and the woken chat
# can be shown to carry the same wake id.
#
#   --seat SEAT        the seat whose router path is used (required)
#   --letter XX        the letter that must be picked (required;  one or two capital letters)
#   --message-id N     the Zulip message the woken chat is told to read (required unless --follow)
#   --dry-run          print the body and the target, send nothing
#   --test             send it with "test": true:  the router answers at once with its pick and
#                      delivers nothing
#   --follow ID        send nothing;  follow the receipt of wake ID
#   --url URL          the router, plain http on loopback only (default http://127.0.0.1:8794)
#   --wait SECONDS     how long to follow the receipt (default 1100);  0 does not follow
#   --poll-seconds S   the pause between receipt reads (default 5, from 0.05 to 60)
#   --wake-id ID       use this id instead of a new canary-<utc>-<hex> one
#   --topic T, --channel C   carried as metadata
#
# The bearer is WAKE_ROUTER_KEY, read in this process through fleet_knobs from the installed
# release, with the token in ~/.config/afc-wake-router/env.  It is sent only in the Authorization
# header, never printed and never on a command line.  Redirects are never followed and no proxy is
# used.
#
# Exit:  0 proven (a test answer, or a delivered receipt with the same wake_id and receipt_id
# that picked --letter);  3 held (delivery is off);  4 dead, or delivered is false or null;
# 5 no final receipt in --wait;  6 another letter was picked;  1 a health, transport or router
# error, or an answer for another wake;  2 bad arguments.
set -euo pipefail

for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 0
  fi
done

PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { echo "wake-router-canary: python3 is missing" >&2; exit 1; }

exec "$PYTHON" -I - "$@" <<'PY'
import argparse
import http.client
import ipaddress
import json
import math
import os
import pwd
import re
import secrets
import stat
import sys
import time
import urllib.parse

NAME = "wake-router-canary"
CONTRACT = "simple-tools-wake/1"
SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")
LETTER_RE = re.compile(r"[A-Z]{1,2}")
WAKE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
NUMBER_RE = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")
FINAL = ("delivered", "held", "dead")
TOKEN = "FLEET_KNOBS_INFISICAL_TOKEN"
KEY_KNOB = "WAKE_ROUTER_KEY"
ZULIP = "https://simplewithus.zulipchat.com/#narrow/near/%d"
HTTP_TIMEOUT = 30
TEST_ROOT_ENV = "WAKE_ROUTER_TEST_ROOT"


class Fail(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def show(label, record):
    print("%s: %s" % (label, json.dumps(record, sort_keys=True)))


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="wake-router-canary.sh", add_help=False)
    parser.add_argument("--seat", required=True)
    parser.add_argument("--letter", required=True)
    parser.add_argument("--message-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--follow", default="")
    parser.add_argument("--url", default="http://127.0.0.1:8794")
    parser.add_argument("--wait", default="1100")
    parser.add_argument("--poll-seconds", default="5")
    parser.add_argument("--wake-id", default="")
    parser.add_argument("--topic", default="AFC-694 Operator cross-platform coordination")
    parser.add_argument("--channel", default="Work-In-Progress")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        raise Fail("bad arguments", 2 if exc.code else 0)
    if not SEAT_RE.fullmatch(args.seat):
        raise Fail("--seat must be a seat label such as CLAUDE or CODEX", 2)
    if not LETTER_RE.fullmatch(args.letter):
        raise Fail("--letter must be one or two capital letters", 2)
    if args.follow:
        if args.test or args.dry_run or args.message_id or args.wake_id:
            raise Fail("--follow sends nothing:  it takes no --test, --dry-run, --message-id or --wake-id", 2)
        if not WAKE_ID_RE.fullmatch(args.follow):
            raise Fail("--follow needs a well-formed wake id", 2)
    elif not re.fullmatch(r"[1-9][0-9]{0,18}", args.message_id or ""):
        raise Fail("--message-id must be a Zulip message id", 2)
    if args.test and args.dry_run:
        raise Fail("--test and --dry-run cannot be combined", 2)
    if not re.fullmatch(r"0|[1-9][0-9]{0,5}", args.wait):
        raise Fail("--wait must be a whole number of seconds", 2)
    args.wait = int(args.wait, 10)
    if not NUMBER_RE.fullmatch(args.poll_seconds):
        raise Fail("--poll-seconds must be a plain decimal number", 2)
    args.poll_seconds = float(args.poll_seconds)
    if not (math.isfinite(args.poll_seconds) and 0.05 <= args.poll_seconds <= 60):
        raise Fail("--poll-seconds must be between 0.05 and 60", 2)
    if args.wake_id and not WAKE_ID_RE.fullmatch(args.wake_id):
        raise Fail("--wake-id is malformed", 2)
    args.host, args.port = check_url(args.url)
    return args


def check_url(url):
    """(host, port) for a plain http URL to a loopback IP literal, with no path, query, fragment
    or user.  Anything else is refused:  the bearer must never leave the host in clear text."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        raise Fail("--url must be plain http to loopback (the TLS front is for the tailnet)", 2)
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise Fail("--url must be a scheme, a loopback address and a port, nothing else", 2)
    host = parts.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise Fail("--url must name a loopback IP address, not a host name", 2)
    if not address.is_loopback or str(address) != host:
        raise Fail("--url must name a loopback address written canonically", 2)
    netloc_port = parts.netloc.rsplit(":", 1)[1] if parts.netloc.rsplit("]", 1)[-1].count(":") else ""
    if not PORT_RE.fullmatch(netloc_port) or not 1 <= int(netloc_port, 10) <= 65535:
        raise Fail("--url needs a plain decimal port with no leading zero", 2)
    return host, int(netloc_port, 10)


def home_dir(uid, environ):
    test_root = "" if uid == 0 else environ.get(TEST_ROOT_ENV, "")
    if test_root:
        if not os.path.isabs(test_root) or not os.path.isdir(test_root):
            raise Fail("%s must be an existing absolute directory" % TEST_ROOT_ENV, 2)
        return os.path.join(test_root, "home")
    return pwd.getpwuid(uid).pw_dir


def read_token(path, uid):
    """The token from the env file, under the installer's rules.  Never printed."""
    try:
        info = os.lstat(path)
    except OSError:
        raise Fail("no env file at %s;  run install-wake-router.sh first" % path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise Fail("%s must be a regular file owned by this user with mode 0600" % path)
    token = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if not sep or name != TOKEN or token is not None or not value:
                raise Fail("%s may hold only one %s line" % (path, TOKEN))
            token = value
    if token is None:
        raise Fail("%s holds no %s" % (path, TOKEN))
    return token


def read_key(home, uid):
    """WAKE_ROUTER_KEY through fleet_knobs, from the installed release, in this process."""
    scripts = os.path.join(home, ".local", "share", "afc-wake-router", "current", "scripts")
    if not os.path.isdir(os.path.join(scripts, "fleet_knobs")):
        raise Fail("no installed release at %s;  run install-wake-router.sh first" % scripts)
    os.environ[TOKEN] = read_token(os.path.join(home, ".config", "afc-wake-router", "env"), uid)
    os.environ["HOME"] = home
    sys.path.insert(0, scripts)
    try:
        from fleet_knobs import knobs
    except Exception as exc:  # noqa: BLE001
        raise Fail("fleet_knobs could not be loaded (%s)" % type(exc).__name__)
    try:
        value = knobs.resolve(KEY_KNOB).value
    except Exception as exc:  # noqa: BLE001 - only the reason is ever shown
        raise Fail("%s is unavailable (%s)" % (KEY_KNOB, getattr(exc, "reason", type(exc).__name__)))
    if not isinstance(value, str) or len(value) < 16:
        raise Fail("%s is shorter than 16 characters" % KEY_KNOB)
    return value


def request(args, method, path, key=None, body=None):
    """(status, JSON object).  http.client follows no redirect and uses no proxy;  a 3xx is a
    failure, never a second request."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    if data is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(args.host, args.port, timeout=HTTP_TIMEOUT)
    try:
        conn.request(method, path, body=data, headers=headers)
        answer = conn.getresponse()
        raw = answer.read(1 << 20)
        status = answer.status
    except (OSError, http.client.HTTPException) as exc:
        raise Fail("the router did not answer at %s (%s)" % (args.url, type(exc).__name__))
    finally:
        conn.close()
    if 300 <= status < 400:
        raise Fail("the router answered %d with a redirect;  redirects are never followed" % status)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise Fail("the router's answer (%d) is not JSON" % status)
    if not isinstance(payload, dict):
        raise Fail("the router's answer (%d) is not a JSON object" % status)
    return status, payload


def summary(record):
    keys = ("status", "picked", "picked_letter", "picked_generation", "session_id", "reason",
            "delivered", "route", "ack", "code", "attempts", "receipt_id", "wake_id")
    return {k: record.get(k) for k in keys if k in record}


def same_wake(record, wake_id, receipt_id):
    """Fail unless the record names this wake and, once one is known, this receipt."""
    got = record.get("wake_id")
    if not isinstance(got, str) or not got:
        raise Fail("the router's answer has no wake_id")
    if got != wake_id:
        raise Fail("the router answered for another wake id")
    rid = record.get("receipt_id")
    if not isinstance(rid, str) or not rid:
        raise Fail("the router's answer has no receipt_id")
    if receipt_id is not None and rid != receipt_id:
        raise Fail("the receipt_id changed between answers for the same wake")
    return rid


def check_letter(record, letter):
    picked = record.get("picked_letter")
    if picked != letter:
        show("pick", summary(record))
        raise Fail("the router picked letter %r, not %s" % (picked, letter), 6)


def main(argv, environ, uid):
    if uid == 0:
        raise Fail("run this as the jay user, never root", 2)
    args = parse_args(argv)
    home = home_dir(uid, environ)
    wake_id = args.follow or args.wake_id or "canary-%s-%s" % (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), secrets.token_hex(4))
    body = None
    if not args.follow:
        message_id = int(args.message_id, 10)
        body = {
            "contract": CONTRACT,
            "seat": args.seat,
            "wake_id": wake_id,
            "message_id": message_id,
            "channel": args.channel,
            "topic": args.topic,
            "zulip_link": ZULIP % message_id,
            "sender_full_name": "wake canary",
            "letter": args.letter,
            "excerpt": "",
        }
        if args.test:
            body["test"] = True
    print("wake_id: " + wake_id)
    print("target: http://%s:%d/wake/%s letter %s" % (args.host, args.port, args.seat, args.letter))
    if args.dry_run:
        show("body", body)
        print("dry run: nothing sent")
        return 0

    status, health = request(args, "GET", "/health/%s" % args.seat)
    show("health", {"http": status, **health})
    if status != 200 or health.get("healthy") is not True:
        raise Fail("the router's %s seat is not healthy (%s)" % (args.seat, health.get("reason", status)))

    key = read_key(home, uid)
    receipt_path = "/receipt/%s/%s" % (args.seat, wake_id)
    receipt_id = None
    if body is not None:
        status, answer = request(args, "POST", "/wake/%s" % args.seat, key, body)
        if status not in (200, 202):
            show("refused", {"http": status, **answer})
            raise Fail("the router refused the wake (%d)" % status)
        receipt_id = same_wake(answer, wake_id, None)
        if args.test:
            if answer.get("test") is not True:
                raise Fail("the router did not answer as a test")
            if answer.get("delivered") is not False:
                raise Fail("a test answer must say delivered: false")
            check_letter(answer, args.letter)
            show("pick", summary(answer))
            print("proven: test pick of %s for %s" % (args.letter, wake_id))
            return 0
        show("accepted", {"http": status, **answer})
        record = answer
    else:
        record = {}

    deadline = time.monotonic() + args.wait
    while record.get("status") not in FINAL:
        if time.monotonic() >= deadline:
            print("pending: no final receipt within %d s;  follow it later with --follow %s"
                  % (args.wait, wake_id))
            return 5
        if record:
            time.sleep(min(args.poll_seconds, max(0.0, deadline - time.monotonic())))
        code, record = request(args, "GET", receipt_path, key)
        if code == 404 and args.follow and receipt_id is None:
            record = {}
            time.sleep(min(args.poll_seconds, max(0.0, deadline - time.monotonic())))
            continue
        if code != 200 and code != 202:
            show("refused", {"http": code, **record})
            raise Fail("the receipt could not be read (%d)" % code)
        receipt_id = same_wake(record, wake_id, receipt_id)
    show("receipt", summary(record))
    if record["status"] == "held":
        print("held: delivery is off for %s;  nothing was delivered" % args.seat)
        return 3
    check_letter(record, args.letter)
    if record["status"] == "dead" or record.get("delivered") is not True:
        print("not proven: status %s, delivered %s" % (record["status"], json.dumps(record.get("delivered"))))
        return 4
    print("proven: %s delivered to %s, receipt %s;  read the same wake id in the woken chat"
          % (wake_id, args.letter, receipt_id))
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:], dict(os.environ), os.getuid())
    except Fail as exc:
        if str(exc) != "bad arguments":
            print("%s: %s" % (NAME, exc), file=sys.stderr)
        code = exc.code
    raise SystemExit(code)
PY
