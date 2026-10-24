"""A stand-in `agent-sync` for the needs-owner tests.

It parses argv with the REAL parser of scripts/agent_sync/cli.py (so a wrong flag, or a flag in the wrong
place, fails as it would for real), keeps topics and messages in a JSON file, and records every call with
its standard input.

Environment:  FAKE_STATE_DIR, FAKE_SCRIPTS_DIR.  state/zulip.json (a test edits it) holds:
  post_mode       ok | exit2 | exit3 | exit5_landed | exit5_lost | exit6_landed | exit6_lost | nojson
  no_credential   seats whose bot has no credential:  every send exits 3
  no_bot_for      seats `post --to` cannot find (exit 2)
  read_fails      true:  `read` exits 5
  owner_user_id   the pinned owner (7, the id of a person's message);  0 means not pinned
  whoami_fails    true:  `whoami` exits 5
  verify_fails    true:  `verify-owner-msg` exits 5 (the network)
  messages        {id, channel, topic, content, seat, is_bot, sender_id, client, reactions}:  what the fake sends and
                  what a test adds with World.add_message (a person's message has is_bot false and no seat)

Besides post, reply and read it answers whoami, verify-owner-msg (the real rule:  the owner's id, a human client, the
topic, newer than --after), topics and channels.  `read --json` prints the keys of the real `message_json`, with the
real `message_reactions` and the real seat_app lookup.

`read --json` prints one line per message with the keys of the real `message_json` in scripts/agent_sync/cli.py
(a test compares the two key sets), so a filter on `is_bot` or `seat` passes or fails here as it would for real.
"""
import json
import os
import sys
import zlib

STATE = os.environ["FAKE_STATE_DIR"]
SCRIPTS = os.environ["FAKE_SCRIPTS_DIR"]
ZULIP_JSON = os.path.join(STATE, "zulip.json")
CALLS = os.path.join(STATE, "calls.jsonl")
sys.path.insert(0, SCRIPTS)

from agent_sync import cli as C  # noqa: E402


def load():
    if os.path.exists(ZULIP_JSON):
        with open(ZULIP_JSON, encoding="utf-8") as fh:
            return json.load(fh)
    return {"next": 630000000, "messages": [], "post_mode": "ok", "no_credential": []}


def save(world):
    tmp = ZULIP_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(world, fh)
    os.replace(tmp, ZULIP_JSON)


def bot_id(seat):
    """A stable fake Zulip user id for a seat's bot."""
    return 1000 + zlib.crc32(str(seat).encode()) % 90000


OWNER_ID = 7  # the id of a person's message unless a test says otherwise:  Jay
HUMAN_CLIENTS = ("website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop")
REALM = "simplewithus.zulipchat.com"


def bot_email(seat):
    """The email a seat's bot signs as, so the real `seat_tag_for` reads the seat back."""
    seat = str(seat)
    if seat.startswith("GB-"):
        return "%s-grok-bot@%s" % (seat[3:].lower(), REALM)
    return "%s-bot@%s" % (seat.lower(), REALM)


def seat_app(seat):
    """The real F2 lookup against the repo's fleet-apps.json (None when it cannot say)."""
    try:
        return C.I.seat_app(seat, registry=C.I.load_seat_registry())
    except Exception:  # noqa: BLE001 - the real message_json does the same
        return None


def message_json(m):
    """One stored message as the real `agent-sync read --json` prints it.  The bot's email is the seat tag
    lower-cased plus -bot@ (what the real `seat_tag_for` reads back), and a person has no seat."""
    is_bot = bool(m.get("is_bot", True))
    seat = str(m.get("seat") or "") if is_bot else None
    email = m.get("email") or (bot_email(seat) if is_bot else "jay@example.test")
    return {"id": m["id"], "sender_id": m.get("sender_id", bot_id(seat) if is_bot else OWNER_ID),
            "client": m.get("client", "ZulipPython" if is_bot else "website"),
            "channel": m["channel"], "topic": m["topic"], "dm": False, "sender_email": email,
            "sender_full_name": seat.title() if is_bot else "Jay", "is_bot": is_bot, "seat": seat or None,
            "seat_app": seat_app(seat) if seat else None,
            "timestamp": m.get("timestamp", 1791700000), "content": m["content"], "flags": [], "sibling": False,
            "mentioned": False, "reactions": C.message_reactions({"reactions": m.get("reactions", [])})}


def main():
    argv = sys.argv[1:]
    # The fake realm is "work-in-progress" (the renamed #agent-sync):  pin it the way
    # agent_sync/tests/harness.py pins its own, or channel-less commands lose the realm.
    args = C.build_parser(channel_default="work-in-progress").parse_args(argv)
    stdin = ""
    if getattr(args, "text", None) == ["-"]:
        stdin = sys.stdin.read()
    entry = {"tool": "simple-chat", "argv": argv, "stdin": stdin, "seat": os.environ.get("AGENT_SEAT"),
             "launch_seat": os.environ.get("AGENT_LAUNCH_SEAT"), "launcher": os.environ.get("AGENT_LAUNCHER")}
    world = load()
    seat = str(getattr(args, "as_seat", "") or "").upper()
    code = dispatch(world, args, seat, stdin)
    entry["code"] = code  # the exit code, so a test can tell a post that went out from one that failed
    with open(CALLS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return code


def dispatch(world, args, seat, stdin):
    if args.command in ("post", "reply"):
        return send(world, args, seat, stdin)
    if args.command == "read":
        return read(world, args)
    if args.command == "whoami":
        return whoami(world, args, seat)
    if args.command == "verify-owner-msg":
        return verify_owner_msg(world, args)
    if args.command == "topics":
        return topics(world, args)
    if args.command == "channels":
        print(json.dumps([{"name": "work-in-progress", "description": "", "stream_id": world.get("stream_id", 12)}]))
        return 0
    sys.stderr.write("fake agent-sync: unexpected command %s\n" % args.command)
    return 64


def owner_id(world):
    return world.get("owner_user_id", OWNER_ID)


def read(world, args):
    if world.get("read_fails"):
        sys.stderr.write("fake zulip: read failed\n")
        return 5
    same = sorted((m for m in world["messages"] if m["topic"].casefold() == args.topic.casefold()
                   and m["channel"].casefold() == args.channel.casefold()), key=lambda m: m["id"])
    if getattr(args, "since", None) is not None:
        shown = [m for m in same if m["id"] > args.since][:args.limit]
    else:
        shown = same[-args.limit:]
    for m in shown:
        print(json.dumps(message_json(m)))
    return 0


def whoami(world, args, seat):
    if world.get("whoami_fails"):
        sys.stderr.write("fake zulip: whoami failed\n")
        return 5
    owner = owner_id(world)
    print(json.dumps({"full_name": seat.title(), "email": bot_email(seat), "user_id": bot_id(seat), "is_bot": True, "seat": seat,
                      "bot_seat": seat, "seat_source": "--as", "launcher": None, "verified": True, "problem": None,
                      "session": getattr(args, "session", None), "credential": "fake", "realm": "https://" + REALM,
                      "owner_user_id": owner if owner and owner > 0 else None}))
    return 0


def verify_owner_msg(world, args):
    """The real rule of `agent-sync verify-owner-msg`, over the stored messages."""
    if world.get("verify_fails"):
        sys.stderr.write("fake zulip: verify failed\n")
        return 5
    owner = owner_id(world)
    if not owner or owner <= 0:
        sys.stderr.write("no owner to verify against:  owner_user_id is not pinned\n")
        return 2
    m = next((x for x in world["messages"] if x["id"] == args.message_id), None)
    checks = {"found": m is not None, "sender_is_owner": False, "client_is_human": False, "place": False, "newer": False}
    reasons = []
    if m is None:
        reasons.append("message %d was not found" % args.message_id)
    else:
        row = message_json(m)
        checks["sender_is_owner"] = row["sender_id"] == owner and not row["is_bot"]
        checks["client_is_human"] = row["client"] in HUMAN_CLIENTS
        wanted = (args.topic or "").casefold()
        checks["place"] = (row["channel"].casefold() == args.channel.casefold()
                           and row["topic"].casefold() in (wanted, ("\u2714 " + args.topic).casefold()))
        checks["newer"] = args.after is None or row["id"] > args.after
        for name, ok in checks.items():
            if not ok:
                reasons.append(name)
    verdict = {"ok": not reasons, "message_id": args.message_id, "owner_user_id": owner, "checks": checks, "reasons": reasons}
    print(json.dumps(verdict))
    return 0 if verdict["ok"] else 7


def topics(world, args):
    latest = {}
    for m in world["messages"]:
        if m["channel"].casefold() == args.channel.casefold():
            latest[m["topic"]] = max(latest.get(m["topic"], 0), m["id"])
    rows = [{"name": name, "max_id": mid} for name, mid in sorted(latest.items(), key=lambda kv: kv[1], reverse=True)][:args.limit]
    print(json.dumps(rows))
    return 0


def store(world, channel, topic, seat, content):
    mid = world["next"]
    world["next"] += 1
    world["messages"].append({"id": mid, "channel": channel, "topic": topic, "content": content, "seat": seat,
                              "is_bot": True, "sender_id": bot_id(seat)})
    return mid


def send(world, args, seat, stdin):
    mode = world.get("post_mode", "ok")
    if seat in world.get("no_credential", []):
        sys.stderr.write("credential file not found for %s\n" % seat)
        return 3
    if args.command == "post":
        channel, topic = args.channel, args.topic
    else:
        known = (world.get("topics_by_id") or {}).get(str(args.id))
        original = next((m for m in world["messages"] if m["id"] == args.id), None)
        channel, topic = (original["channel"], original["topic"]) if original else ("work-in-progress", known or "OWNER x")
    if getattr(args, "to_owner", False) and not (owner_id(world) and owner_id(world) > 0):
        sys.stderr.write("no owner to message:  daemon.owner_user_id is not pinned\n")
        return 2
    for name in getattr(args, "to", None) or []:
        if str(name).upper() in [str(x).upper() for x in world.get("no_bot_for", [])]:
            sys.stderr.write("no active user or bot answers to %s\n" % name)
            return 2
    if mode == "exit2":
        sys.stderr.write("no owner to message\n")
        return 2
    if mode == "exit3":
        sys.stderr.write("credential problem\n")
        return 3
    if mode in ("exit5_lost", "exit6_lost"):
        sys.stderr.write("network trouble\n")
        return int(mode[4])
    mid = store(world, channel, topic, seat, stdin)
    save(world)
    if mode in ("exit5_landed", "exit6_landed"):
        sys.stderr.write("timed out;  the message may or may not have been sent\n")
        return int(mode[4])
    if mode == "nojson":
        print("posted")
        return 0
    print(json.dumps({"id": mid, "channel": channel, "topic": topic}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
