"""A stand-in `board` for the needs-owner tests.

It parses argv with the REAL parser of scripts/linear-board/board (so a flag the real CLI does not have
fails here too) and builds every answer with the real helpers (`row_dict`, `split_description`,
`parse_comment`, `read_text_input`, `comment_body`, `det_uuid`), but keeps its issues in a JSON file
instead of Linear.  Like Linear, it hands descriptions back re-serialized (`linear_markdown`:  `[ ] ~`
escaped outside code spans, `-` bullets turned into `*`).

Environment:  FAKE_STATE_DIR (board.json and calls.jsonl live there), FAKE_SCRIPTS_DIR (the repo's scripts/),
FAKE_BOARD_WRITES_OFF=1 (every write exits 6, as a write with no bearer does), FAKE_BOARD_FAIL=verb,verb (those verbs exit 1), FAKE_BOARD_STRIP_FENCE_INFO=1 (a description comes back
with a bare fence), FAKE_BOARD_DROP_FENCE=1 (it comes back with the fence turned into an indented block).
"""
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from linear_md import linear_markdown  # noqa: E402

FLEET_APP_USER_ID = "0b1c2d3e-0000-4000-8000-00000000f1ee"  # the Linear account of the Fleet app, which signs every comment `board` writes
STATE = os.environ["FAKE_STATE_DIR"]
SCRIPTS = os.environ["FAKE_SCRIPTS_DIR"]
BOARD_JSON = os.path.join(STATE, "board.json")
CALLS = os.path.join(STATE, "calls.jsonl")

_loader = importlib.machinery.SourceFileLoader("board_real", os.path.join(SCRIPTS, "linear-board", "board"))
_spec = importlib.util.spec_from_loader("board_real", _loader)
B = importlib.util.module_from_spec(_spec)
_loader.exec_module(B)


def _fake_verified_seat():
    """The test launcher's binding stands in for the ST-MCP verifier (import-only seam, like the real
    suite's BEARER_TRANSPORT):  FAKE_BEARER_SEAT is the seat the bearer at the locator proves."""
    seat = os.environ.get("FAKE_BEARER_SEAT", "")
    if not seat:
        B.die("no seat bearer is readable at its locator; refusing to act without a verified identity", B.EXIT_AUTH)
    return seat


B.bearer_verified_seat = _fake_verified_seat


def load():
    if os.path.exists(BOARD_JSON):
        with open(BOARD_JSON, encoding="utf-8") as fh:
            return json.load(fh)
    return {"next": 300, "issues": {}}


def save(world):
    tmp = BOARD_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(world, fh)
    os.replace(tmp, BOARD_JSON)


def now_iso(world):
    world["tick"] = world.get("tick", 0) + 1
    base = dt.datetime(2026, 10, 11, 8, 0, 0, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(minutes=world["tick"])).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def label_node(key):
    if "/" in key:
        parent, name = key.split("/", 1)
        return {"id": "lbl-" + key, "name": name, "parent": {"name": parent}}
    return {"id": "lbl-" + key, "name": key, "parent": None}


def issue_by_ref(world, ref):
    ref = (ref or "").strip()
    if ref.upper() in world["issues"]:
        return world["issues"][ref.upper()]
    for node in world["issues"].values():
        flat = node["id"].replace("-", "")
        if len(ref) >= 8 and flat.startswith(ref.lower().replace("-", "")):
            return node
    return None


def record_call(argv, files):
    entry = {"tool": "simple-board", "argv": argv, "seat": os.environ.get("FAKE_BEARER_SEAT"),
             "env_seat": os.environ.get("AGENT_SEAT"),
             "launch_seat": os.environ.get("AGENT_LAUNCH_SEAT"), "launcher": os.environ.get("AGENT_LAUNCHER"),
             "files": files}
    with open(CALLS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def snapshot_files(argv):
    """The mode and text of every --desc-file/--text-file at call time (the tool deletes them afterwards)."""
    found = {}
    for i, arg in enumerate(argv):
        if arg in ("--desc-file", "--text-file", "--fix-file") and i + 1 < len(argv):
            path = argv[i + 1]
            try:
                info = os.stat(path)
                with open(path, encoding="utf-8") as fh:
                    found[arg] = {"mode": "%04o" % stat.S_IMODE(info.st_mode), "text": fh.read()}
            except OSError as exc:
                found[arg] = {"error": str(exc)}
    return found


def show_json(world, node):
    comments = [B.parse_comment(c) for c in sorted(node.get("comments", []), key=lambda c: c["createdAt"])]
    parts = B.split_description(node.get("description"))
    r = B.row_dict(node)
    r["addressed_by"] = ",".join(B.holders_of(node)) or None
    r.update({
        "description": parts["description"], "recommended_fix": parts["recommended_fix"],
        "resolution": B.resolution_of(comments), "reported_by": parts["reported_by"],
        "location": B.location_of(comments, parts["footer"]), "provenance": parts["footer"],
        "attachments": [x.get("url") for x in (node.get("attachments") or {}).get("nodes") or []],
        "relations": [], "comments": comments,
    })
    return r


def main():
    argv = sys.argv[1:]
    a = B.build_parser().parse_args(argv)
    if a.cmd == "whoami":  # the seat probe:  the verified seat line only, not recorded as a board call
        seat = B.current_seat(required=False)
        print("seat:        %s (bearer-verified)" % seat if seat else "seat:        (no bearer: reads run without an identity, the four write verbs refuse)")
        return 0
    files = snapshot_files(argv)
    record_call(argv, files)
    write = a.cmd in B.WRITE_VERBS
    if write and os.environ.get("FAKE_BOARD_WRITES_OFF") == "1":
        B.die("`board %s` is a write, and no bearer-verified seat is available" % a.cmd, B.EXIT_AUTH)
    if a.cmd in (os.environ.get("FAKE_BOARD_FAIL") or "").split(","):
        B.die("fake board failure for %s" % a.cmd, B.EXIT_ERROR)
    seat = B.current_seat(required=write)
    for val in (getattr(a, "reported_by", None), getattr(a, "addressed_by", None), getattr(a, "author", None)):
        if val is not None:
            B.check_by(val, seat)
    B.prepare_inputs(a)
    world = load()
    code = {"list": cmd_list, "show": cmd_show, "file": cmd_file, "comment": cmd_comment, "label": cmd_label,
            "status": cmd_status, "claim": cmd_claim}[a.cmd](world, a, seat)
    save(world)
    return code


def cmd_list(world, a, seat):
    rows = list(world["issues"].values())
    for name in a.labels or []:
        rows = [n for n in rows if name in B.labels_of(n)]
    if a.source_kind:
        kinds = B.split_csv(a.source_kind)
        rows = [n for n in rows if B.group_value(n, "Kind") in kinds]
    if a.status:
        states = []
        for word in B.split_csv(a.status.lower()):
            states += B.LIST_STATUS_STATES[word]
        rows = [n for n in rows if n["state"]["name"] in states]
    if a.search:
        rows = [n for n in rows if a.search.lower() in n["title"].lower()]
    if a.app:
        team, _ = B.route_app(a.app)
        rows = [n for n in rows if n["team"]["key"] == team]
    if a.mine:
        rows = [n for n in rows if "claimed:" + B.norm_seat(a.mine) in B.labels_of(n)]
    rows.sort(key=lambda n: n["createdAt"], reverse=True)
    rows = rows[:int(a.limit)]
    out = [B.row_dict(n) for n in rows]
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print("%d shown of %d matching\n" % (len(out), len(out)))
        for r in out:
            print(B.fmt_row(r))
    return 0


def cmd_show(world, a, seat):
    node = issue_by_ref(world, a.id)
    if node is None:
        B.die("no issue %s" % a.id, B.EXIT_ERROR)
    print(json.dumps(show_json(world, node), indent=2))
    return 0


def cmd_file(world, a, seat):
    by = B.check_by(a.reported_by, seat)
    team, fslug = B.route_app(a.app)
    kind = a.source_kind or "agent-report"
    if kind not in B.KINDS:
        B.die("--kind must be one of %s" % ", ".join(B.KINDS), B.EXIT_USAGE)
    sev = (a.severity or "").upper() or None
    if a.external_uid:
        iid = B.det_uuid("issue", "%s:%s:%s" % (team, fslug or "", a.external_uid))
    else:
        iid = B.det_uuid("issue", "anon-%d" % len(world["issues"]))
    for node in world["issues"].values():
        if node["id"] == iid:
            print("filed %s  (%s %s)  %s  (already filed; unchanged)" % (
                node["id"], B.severity_of(node) or "--", fslug or team, node["identifier"]))
            return 0
    lines = ["%s Filed with the board CLI." % B.seat_tag(by, B.auto_sessions(None)), "",
             (a.desc or "").strip() or "_No description given._"]
    foot = ["", "---", "", "**Filed with the board CLI**", "", "- Reported by %s" % by, "- Kind: %s" % kind]
    if a.external_uid:
        foot.append("- External uid: `%s`" % a.external_uid)
    foot += ["", "<!-- board-cli: app=%s kind=%s -->" % (fslug or team, kind)]
    labels = ["Kind/" + kind] + (["Severity/" + sev] if sev else []) + list(a.labels or [])
    ident = "%s-%d" % (team, world["next"])
    world["next"] += 1
    description = linear_markdown("\n".join(lines + foot))
    if os.environ.get("FAKE_BOARD_STRIP_FENCE_INFO") == "1":  # an editor that drops a code language it does not know
        description = description.replace("```needs-owner", "```")
    if os.environ.get("FAKE_BOARD_DROP_FENCE") == "1":  # an editor that turns a fenced block into an indented one
        description = re.sub(r"```needs-owner\n(.*?)\n```", lambda m: "\n".join("    " + x for x in m.group(1).splitlines()),
                             description, flags=re.S)
    node = {"id": iid, "identifier": ident, "url": "https://linear.app/test-fleet/issue/" + ident,
            "title": a.title.strip(), "description": description,
            "priority": B.SEV_PRIORITY.get(sev, 0), "createdAt": now_iso(world), "updatedAt": now_iso(world),
            "state": {"name": "Todo"}, "team": {"key": team},
            "labels": {"nodes": [label_node(k) for k in labels]}, "attachments": {"nodes": []}, "comments": []}
    world["issues"][ident] = node
    print("filed %s  (%s %s)  %s" % (iid, sev or "--", fslug or team, ident))
    return 0


def cmd_comment(world, a, seat):
    node = issue_by_ref(world, a.id)
    if node is None:
        B.die("no issue %s" % a.id, B.EXIT_ERROR)
    by = B.check_by(a.author, seat)
    sessions = B.auto_sessions(a.session_ids)
    body = B.comment_body(by, sessions, a.text, a.location, B.norm_env(a.env))
    cid = B.comment_id(node["id"], by, "comment", body, sessions)
    if any(c["id"] == cid for c in node["comments"]):
        sys.stderr.write("the same comment is already on %s\n" % node["identifier"])
        return 0
    node["comments"].append({"id": cid, "body": body, "createdAt": now_iso(world),
                             "user": {"id": FLEET_APP_USER_ID, "name": "Fleet", "app": True}})
    print("commented on %s as %s  %s" % (B.short_id(node["id"]), by, node["identifier"]))
    return 0


def cmd_label(world, a, seat):
    node = issue_by_ref(world, a.id)
    if node is None:
        B.die("no issue %s" % a.id, B.EXIT_ERROR)
    have = B.labels_of(node)
    if a.action == "add" and a.label_name not in have:
        node["labels"]["nodes"].append(label_node(a.label_name))
    if a.action == "remove":
        node["labels"]["nodes"] = [x for x in node["labels"]["nodes"] if B.label_key(x) != a.label_name]
    print("%s %s %s" % (a.action, a.label_name, node["identifier"]))
    return 0


def cmd_status(world, a, seat):
    node = issue_by_ref(world, a.id)
    if node is None:
        B.die("no issue %s" % a.id, B.EXIT_ERROR)
    node["state"] = {"name": B.STATUS_TO_STATE[a.value]}
    if a.value in B.CLOSING_STATUSES:
        node["labels"]["nodes"] = [x for x in node["labels"]["nodes"] if B.label_key(x) not in B.CLOSING_REMOVES]
    print("%s -> %s  %s" % (B.short_id(node["id"]), a.value, node["identifier"]))
    return 0


def cmd_claim(world, a, seat):
    node = issue_by_ref(world, a.id)
    if node is None:
        B.die("no issue %s" % a.id, B.EXIT_ERROR)
    node["labels"]["nodes"].append(label_node("claimed:" + B.check_by(a.addressed_by, seat)))
    node["state"] = {"name": "In Progress"}
    print("claimed %s" % node["identifier"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
