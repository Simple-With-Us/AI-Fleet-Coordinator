"""A stand-in `agent-sync` for the needs-owner tests.

It parses argv with the REAL parser of scripts/agent_sync/cli.py (so a wrong flag, or a flag in the wrong
place, fails as it would for real), keeps topics and messages in a JSON file, and records every call with
its standard input.

Environment:  FAKE_STATE_DIR, FAKE_SCRIPTS_DIR.  state/zulip.json (a test edits it) holds:
  post_mode       ok | exit2 | exit3 | exit5_landed | exit5_lost | exit6_landed | exit6_lost | nojson
  no_credential   seats whose bot has no credential:  every send exits 3
  read_fails      true:  `read` exits 5
  messages        {id, channel, topic, content, seat, is_bot, sender_id}:  what the fake sends and what a test
                  adds with World.add_message (a person's message has is_bot false and no seat)

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


def message_json(m):
    """One stored message as the real `agent-sync read --json` prints it.  The bot's email is the seat tag
    lower-cased plus -bot@ (what the real `seat_tag_for` reads back), and a person has no seat."""
    is_bot = bool(m.get("is_bot", True))
    seat = str(m.get("seat") or "") if is_bot else None
    email = "%s-bot@simplewithus.zulipchat.com" % seat.lower() if is_bot else "jay@example.test"
    return {"id": m["id"], "sender_id": m.get("sender_id", bot_id(seat) if is_bot else 7), "client": "fake",
            "channel": m["channel"], "topic": m["topic"], "dm": False, "sender_email": email,
            "sender_full_name": seat.title() if is_bot else "Jay", "is_bot": is_bot, "seat": seat or None, "seat_app": None,
            "timestamp": m.get("timestamp", 1791700000), "content": m["content"], "flags": [], "sibling": False,
            "mentioned": False}


def main():
    argv = sys.argv[1:]
    args = C.build_parser().parse_args(argv)
    stdin = ""
    if getattr(args, "text", None) == ["-"]:
        stdin = sys.stdin.read()
    entry = {"tool": "agent-sync", "argv": argv, "stdin": stdin, "seat": os.environ.get("AGENT_SEAT"),
             "launch_seat": os.environ.get("AGENT_LAUNCH_SEAT"), "launcher": os.environ.get("AGENT_LAUNCHER")}
    with open(CALLS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    world = load()
    seat = str(getattr(args, "as_seat", "") or "").upper()
    if args.command in ("post", "reply"):
        return send(world, args, seat, stdin)
    if args.command == "read":
        if world.get("read_fails"):
            sys.stderr.write("fake zulip: read failed\n")
            return 5
        for m in world["messages"]:
            if m["topic"] == args.topic and m["channel"] == args.channel:
                print(json.dumps(message_json(m)))
        return 0
    sys.stderr.write("fake agent-sync: unexpected command %s\n" % args.command)
    return 64


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
        channel, topic = (original["channel"], original["topic"]) if original else ("agent-sync", known or "OWNER x")
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
