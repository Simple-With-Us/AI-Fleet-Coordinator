"""The four verbs:  file, adopt, list and status.

`file` and `adopt` are idempotent:  the uid is `needs-owner:<parent or SEAT>:<sha8 of the normalized
question and options>`, so running the same command again finds the same item, never files a second
one, and finishes whatever was left undone (the notice).  A change to an item that was already filed
(other options, other low-stakes marks, another recommendation or context) is refused unless `--again`,
which files a new version that quotes the earlier item and its message.  Nothing here ever closes an
item or edits one:  the filing seat closes its own item with `board status`, which also removes the label.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping, TextIO

from . import f3
from . import items as itemsmod
from . import links as linkmod
from . import notify as notifymod
from . import records
from . import safety
from . import seat as seatmod
from . import text as T
from .block import Block, render_block, render_description
from .errors import BoardFailed, NotFound, Refusal
from .items import AWAITING, CLOSED_STATUSES, KIND, LABEL, Item, OVER_FIVE
from .proc import Runner, first_line

_FILED_RE = re.compile(r"^filed\s+(\S+)\s+\(([^)]*)\)\s+([A-Za-z]{1,8}-[0-9]{1,9})\b(.*)$", re.M)
_MAX_VERSION_TRIES = 6


@dataclass
class Fields:
    question: str
    options: list[str]
    low_stakes: list[int]
    recommend: int | None
    context: str | None
    urgent: bool


@dataclass
class Streams:
    environ: Mapping[str, str]
    out: TextIO
    err: TextIO

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")

    def warn(self, line: str) -> None:
        self.err.write("needs-owner: %s\n" % line)


def emit_json(io: Streams, value) -> None:
    io.out.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


# ---- request fields ----------------------------------------------------------------------------

def collect_fields(args, *, default_question: str | None = None) -> Fields:
    question = T.clean_text(args.question if args.question is not None else default_question, "--question", T.QUESTION_MAX)
    raw = list(args.option or [])
    if not 1 <= len(raw) <= T.MAX_OPTIONS:
        raise Refusal("give from 1 to %d --option values (%d given)" % (T.MAX_OPTIONS, len(raw)))
    options = [T.clean_text(o, "--option %d" % n, T.OPTION_MAX) for n, o in enumerate(raw, 1)]
    if len({T.normalize(o) for o in options}) != len(options):
        raise Refusal("two options say the same thing;  each --option must differ")
    low = T.parse_index_list(args.low_stakes, len(options), "--low-stakes")
    urgent = bool(getattr(args, "urgent", False))
    if urgent and low:
        raise Refusal("--low-stakes cannot go with --urgent:  an urgent decision is never answered by a tap")
    recommend = None
    if args.recommend is not None:
        if not 1 <= args.recommend <= len(options):
            raise Refusal("--recommend %d is not an option number from 1 to %d" % (args.recommend, len(options)))
        recommend = args.recommend
    context = T.clean_text(args.context, "--context", T.CONTEXT_MAX, required=False) or None
    checked = {"the question": question, "the context": context}
    checked.update({"option %d" % n: o for n, o in enumerate(options, 1)})
    safety.check_fields(checked)
    return Fields(question, options, low, recommend, context, urgent)


def make_block(seat: str, scope: str, f: Fields, *, parent: str | None, version: int = 1, again_of: str | None = None,
               mirror: linkmod.TopicLink | None = None) -> Block:
    uid = T.make_uid(scope, f.question, f.options, version=version, mirror_msg=mirror.message_id if mirror else None)
    return Block(uid=uid, question=f.question, options=f.options, low_stakes=f.low_stakes, recommend=f.recommend,
                 parent=parent, filed_by=seat, context=f.context, version=version, again_of=again_of,
                 mirror_of=mirror.url if mirror else None)


def resolve_parent(runner: Runner, seat: str, ref: str | None) -> str | None:
    """The parent's identifier, when the filer holds the parent's `claimed:` label.  Resolving first makes
    `AFC-123` and a board id the same uid."""
    if not ref:
        return None
    wanted = T.parse_ident(ref, "--parent") if T.IDENT_RE.fullmatch(ref.strip()) else ref.strip()
    parent = itemsmod.fetch(runner, wanted)
    if parent is None:
        raise Refusal("--parent %s: no such issue" % wanted[:40])
    if "claimed:" + seat not in parent.labels:
        raise Refusal("--parent %s is not claimed by %s.  A decision can only hang off an issue its filer holds" % (parent.identifier, seat))
    return parent.identifier


def again_phrase(item: Item) -> str:
    return item.identifier + (" msg %d" % item.notified[0] if item.notified else "")


# ---- the shared tail:  announce, count, print ---------------------------------------------------

def finish(io: Streams, runner: Runner, args, seat: str, item: Item, others: list[Item], *, new: bool, rescan: bool,
           words: tuple[str, str], extra: dict, no_notify: bool = False) -> int:
    """Announce `item`, count the seat's open decisions (`others` are the other items;  `item` is added),
    and print the result.  `words` is (what a new one is called, what an existing one is called)."""
    count = itemsmod.open_count_for(others + [item], seat)
    over_five = count > OVER_FIVE
    if over_five:
        io.warn("%s has %d decisions waiting on Jay (more than %d).  This one was filed anyway;  the digest shows the warning" % (
            seat, count, OVER_FIVE))
    if no_notify:
        notice = notifymod.Notice("notified" if item.notified else "notify_pending", item.notified[0] if item.notified else None,
                                  item.topic, recorded=True)
    elif item.state == "replied":
        notice = notifymod.Notice("notified", item.notified[0] if item.notified else None, item.topic, already=True, recorded=True)
    else:
        notice = notifymod.notify(runner, item, rescan=rescan, environ=dict(io.environ))
    if not notice.recorded and notice.state == "notified":
        io.warn("the notice went out but its record on %s did not;  run the same command again to record it (it will not post twice)" % item.identifier)
    record = {"identifier": item.identifier, "id": item.id, "url": item.url, "uid": item.block.uid if item.block else None,
              "seat": seat, "over_five": over_five}
    record.update(extra)
    record.update(notice.to_json())
    if args.json:
        emit_json(io, record)
        return 0
    io.say("%s %s  %s" % (words[0] if new else words[1], item.identifier, item.url))
    if notice.state == "notified":
        io.say("%s message %s in \"%s\"" % ("already announced:" if notice.already else "announced:", notice.msg_id, notice.topic))
        if notice.linkback == "failed":
            io.warn("the reply with the Linear link in the mirrored topic failed;  post it with agent-sync reply")
    else:
        io.say("notify_pending (%s):  %s" % (notice.reason, "skipped by --no-notify" if no_notify else "the fallback notifier will send it"))
    return 0


# ---- file --------------------------------------------------------------------------------------

def run_file(args, io: Streams) -> int:
    seat = seatmod.verified_seat(args.by, io.environ)
    fields = collect_fields(args)
    app = (args.app or "").strip()
    if not app:
        raise Refusal("--app is required")
    mirror = linkmod.parse_topic_link(args.mirror_of, dict(io.environ)) if args.mirror_of else None
    if mirror and args.again:
        raise Refusal("--again and --mirror-of do not go together:  a mirrored item is one persona request")
    runner = Runner(seat, io.environ)
    runner.require_board()
    parent = resolve_parent(runner, seat, args.parent)
    scope = parent or seat
    items, truncated = itemsmod.load(runner)
    if truncated:
        io.warn("the board returned %d open items, the most it lists;  a duplicate beyond them may go unseen" % itemsmod.LIST_LIMIT)

    version, again_of, existing = 1, None, None
    wanted = make_block(seat, scope, fields, parent=parent, mirror=mirror)
    family = [i for i in items if i.block and i.block.scope == scope and not i.block.mirror_of and not mirror
              and T.normalize(i.block.question) == T.normalize(fields.question)]
    if mirror:
        family = [i for i in items if i.block and i.block.uid == wanted.uid]
    if args.again:
        top = max(family, key=lambda i: (i.block.version, i.created_at or "")) if family else None
        if top and top.block.version >= 2 and top.block.same_request(wanted):
            existing = top  # the same re-ask, run twice
        else:
            version = (top.block.version if top else 1) + 1
            again_of = again_phrase(top) if top else None
            wanted = make_block(seat, scope, fields, parent=parent, version=version, again_of=again_of)
    else:
        exact = [i for i in family if i.block.uid == wanted.uid]
        if exact:
            existing = exact[0]
            if not existing.block.same_request(wanted) or (existing.severity == "P1") != fields.urgent:
                raise Refusal("%s already asks this question with other low-stakes marks, recommendation, context or urgency.  "
                              "The filed item is not edited;  close it and file again, or use --again" % existing.identifier)
        elif family:
            raise Refusal("%s already asks this question with other options%s.  An item that was filed is not changed;  "
                          "use --again to ask it again (it quotes the earlier one)" % (
                              family[0].identifier, " and was already sent to Jay" if family[0].notified else ""))

    if args.dry_run:
        plan = {"dry_run": True, "uid": wanted.uid, "title": T.title_for(fields.question), "severity": "P1" if fields.urgent else "P2",
                "existing": existing.identifier if existing else None, "options": len(fields.options)}
        if args.json:
            emit_json(io, plan)
        else:
            io.say("dry run:  would %s %s  (%s)" % ("reuse" if existing else "file", plan["title"], wanted.uid))
        return 0

    new = existing is None
    item = existing
    if new:
        item, new = file_new(io, runner, seat, app, fields, scope, parent, mirror, wanted, args.again)
        if parent and new:
            comment_on_parent(io, runner, parent, item.identifier)
    others = [i for i in items if i.identifier != item.identifier]
    return finish(io, runner, args, seat, item, others, new=new, rescan=not new, words=("filed", "already filed"),
                  extra={"already_filed": not new, "again": bool(args.again)})


def file_new(io: Streams, runner: Runner, seat: str, app: str, f: Fields, scope: str, parent: str | None,
             mirror: linkmod.TopicLink | None, block: Block, again: bool) -> tuple[Item, bool]:
    """(item, created).  `created` is False when the board already had an open issue with this uid."""
    severity = "P1" if f.urgent else "P2"
    title = T.title_for(f.question)
    for _ in range(_MAX_VERSION_TRIES):
        with runner.private_file(render_description(block)) as path:
            result = runner.board("file", "--kind", KIND, "--label", LABEL, "--uid", block.uid, "--by", seat, "--app", app,
                                  "--severity", severity, "--title", title, "--desc-file", path)
        if not result.ok:
            raise BoardFailed("board file failed:  %s" % (first_line(result.err) or "exit %d" % result.code), result.code)
        match = _FILED_RE.search(result.out)
        if not match:
            raise BoardFailed("board file answered with a line I cannot read")
        item = itemsmod.fetch(runner, match.group(3))
        if item is None:
            raise BoardFailed("%s was filed but cannot be read back;  run the same command again" % match.group(3))
        if "already filed" not in match.group(4):
            return item, True
        # An issue with this uid exists that the open list did not show (closed, or past the list's limit).
        if item.status in CLOSED_STATUSES:
            if not again:
                raise Refusal("%s asked this exact question and is closed.  Use --again to ask it again" % item.identifier)
            block = make_block(seat, scope, f, parent=parent, version=block.version + 1, again_of=item.identifier, mirror=mirror)
            continue
        return item, False
    raise BoardFailed("could not find an unused version of this question after %d tries" % _MAX_VERSION_TRIES)


def comment_on_parent(io: Streams, runner: Runner, parent: str, identifier: str) -> None:
    with runner.private_file("waiting on owner:  %s" % identifier) as path:
        result = runner.board("comment", parent, "--by", runner.seat, "--text-file", path)
    if not result.ok:
        io.warn("filed %s, but the 'waiting on owner' comment on %s failed" % (identifier, parent))


# ---- adopt -------------------------------------------------------------------------------------

def run_adopt(args, io: Streams) -> int:
    seat = seatmod.verified_seat(args.by, io.environ)
    runner = Runner(seat, io.environ)
    runner.require_board()
    ref = args.id.strip()
    item = itemsmod.fetch(runner, ref)
    if item is None:
        raise NotFound("no issue %s" % ref[:40])
    if item.kind != KIND:
        raise Refusal("%s is not a Kind/agent-report issue" % item.identifier)
    if item.status in CLOSED_STATUSES:
        raise Refusal("%s is closed" % item.identifier)
    if item.reported_by and item.reported_by != seat:
        raise Refusal("%s was filed by %s;  a seat adopts only its own issues" % (item.identifier, item.reported_by))
    default_q = item.title.removeprefix(T.TITLE_PREFIX.strip()).strip() if item.title else None
    fields = collect_fields(args, default_question=default_q)
    parent = resolve_parent(runner, seat, args.parent)
    scope = parent or seat
    wanted = make_block(seat, scope, fields, parent=parent)
    new = False
    if item.block is not None:
        if item.block.uid != wanted.uid or not item.block.same_request(wanted):
            raise Refusal("%s is already a needs-owner item with other content.  An adopted item is not changed" % item.identifier)
    else:
        if args.dry_run:
            io.say("dry run:  would adopt %s as %s" % (item.identifier, wanted.uid))
            return 0
        if LABEL not in item.labels:
            result = runner.board("label", "add", item.identifier, LABEL, "--by", seat)
            if not result.ok:
                raise BoardFailed("board label add failed:  %s" % (first_line(result.err) or "exit %d" % result.code), result.code)
        with runner.private_file(records.block_comment_text(render_block(wanted))) as path:
            result = runner.board("comment", item.identifier, "--by", seat, "--text-file", path)
        if not result.ok:
            raise BoardFailed("board comment failed:  %s" % (first_line(result.err) or "exit %d" % result.code), result.code)
        item = itemsmod.fetch(runner, item.identifier)
        if item is None or item.block is None:
            raise BoardFailed("%s was adopted but its block cannot be read back" % ref[:40])
        new = True
    others, _ = itemsmod.load(runner)
    others = [i for i in others if i.identifier != item.identifier]
    return finish(io, runner, args, seat, item, others, new=new, rescan=True, words=("adopted", "already adopted"),
                  no_notify=args.no_notify, extra={"adopted": new, "already_adopted": not new})


# ---- list and status ---------------------------------------------------------------------------

def run_list(args, io: Streams) -> int:
    runner = Runner(None, io.environ)
    runner.require_board()
    items, truncated = itemsmod.load(runner, closed_too=(args.which == "all"))
    if truncated:
        io.warn("the board returned %d issues, the most it lists;  the list may be incomplete" % itemsmod.LIST_LIMIT)
    flagged = itemsmod.over_five_seats(items)
    chosen = []
    seat_filter = f3.identity.canonical_seat(args.seat) if args.seat else None
    for item in items:
        state = item.state
        if args.which == "open" and state not in AWAITING:
            continue
        if args.which == "replied" and state != "replied":
            continue
        if seat_filter and item.seat != seat_filter:
            continue
        chosen.append(item)
    at = itemsmod.now()
    records_ = [itemsmod.to_json(i, at, flagged) for i in chosen]
    if args.json:
        emit_json(io, records_)
        return 0
    if not records_:
        io.say("no decisions waiting on Jay" if args.which == "open" else "none")
        return 0
    for r in records_:
        io.say("%-9s %-14s %-3s %-12s %6.1fh  %s" % (r["identifier"], r["state"], r["severity"] or "--", r["seat"] or "(unadopted)",
                                                  r["age_hours"] or 0.0, f3.textsafe.render_untrusted(r["question"], 90, code_span=False)))
    return 0


def run_status(args, io: Streams) -> int:
    runner = Runner(None, io.environ)
    runner.require_board()
    item = itemsmod.fetch(runner, args.id.strip())
    if item is None or not item.is_item:
        raise NotFound("%s is not a needs-owner item (it needs Kind/agent-report and the needs-owner label)" % args.id.strip()[:40])
    others, _ = itemsmod.load(runner)
    flagged = itemsmod.over_five_seats(others)
    record = itemsmod.to_json(item, itemsmod.now(), flagged)
    if args.json:
        emit_json(io, record)
    else:
        io.say("%s  %s  %s  seat %s%s" % (record["identifier"], record["state"], record["severity"] or "--", record["seat"] or "(unadopted)",
                                        "" if record["seat_verified"] else " (not verified)"))
        io.say(f3.textsafe.render_untrusted(record["question"], 200, code_span=False))
        for n, option in enumerate(record["options"], 1):
            io.say("  %d%s  %s" % (n, "*" if n in record["low_stakes"] else " ", f3.textsafe.render_untrusted(option, 120, code_span=False)))
        io.say("  topic: %s" % (record["topic"] or "-"))
    return 0
