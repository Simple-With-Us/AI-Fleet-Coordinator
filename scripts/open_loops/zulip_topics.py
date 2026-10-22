"""The claimants' posts in their `<ID> ...` Zulip topics, one of the five activity signals.

    read_signals(run, wanted)    wanted = [{"identifier": "FFX-1", "seat": "CLAUDE"}, ...]

Read through the installed simple-chat (the running seat's own bot, read-only):

    simple-chat topics --json --limit 1000                      the topic names of #agent-sync, newest first
    simple-chat read --topic NAME --json --limit 30 --include-self    one topic's newest messages

An issue's topic is any topic whose name is the identifier or starts with `<identifier> `.  Only messages that
name a seat count (a person's post is not the claimant's), and the activity function compares the seat.  The
whole pass is bounded (80 topic reads, 5 minutes);  a claim that did not get a read is counted in `skipped`
and left out of `by_ident`, and claims.py then marks it {"ok": False} so its band is "unknown", never idle.  A
read that failed makes that claim's band "unknown" the same way, and so does a topic list that failed (status
"failed").  Only a host with no simple-chat (status "absent") leaves the source unchecked.  If the topic list is as long as the limit, a claim with no topic found is unknown too (its topic
may be past the limit).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime

from fleet_facts import safety
from fleet_facts.runner import Runner, which

__all__ = ["MAX_READS", "READ_LIMIT", "TOPICS_LIMIT", "argv_read", "argv_topics", "read_signals"]

MAX_READS = 80
TOTAL_BUDGET_S = 300
TOPICS_LIMIT = 1000
READ_LIMIT = 30
TIMEOUT_S = 45
MAX_TOPICS_PER_ISSUE = 2


def argv_topics(exe: str) -> list[str]:
    return [exe, "topics", "--json", "--limit", str(TOPICS_LIMIT)]


def argv_read(exe: str, topic: str) -> list[str]:
    return [exe, "read", "--topic", topic, "--json", "--limit", str(READ_LIMIT), "--include-self"]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("a message line is not an object")
        out.append(value)
    return out


def _events(messages: list[dict]) -> list[dict]:
    events = []
    for message in messages:
        stamp = message.get("timestamp")
        seat = message.get("seat")
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not isinstance(seat, str)
            or not seat.strip()
        ):
            continue
        try:
            events.append(
                {"at": _iso(int(stamp)), "by": safety.safe_token(seat, 40) or seat.strip()[:40]}
            )
        except (OverflowError, OSError, ValueError):
            continue
    return events


def read_signals(
    run: Runner, wanted: list[dict], *, monotonic: Callable[[], float] = time.monotonic
) -> dict:
    """{"status", "reason", "by_ident": {ID: {"ok", "events"}}, "reads", "skipped"}."""
    out: dict = {"status": "ok", "reason": None, "by_ident": {}, "reads": 0, "skipped": 0}
    if not wanted:
        return out
    exe = which("simple-chat")
    if exe is None:
        out.update(status="absent", reason="simple-chat is not installed", skipped=len(wanted))
        return out
    result = run(argv_topics(exe), timeout=TIMEOUT_S)
    if result.reason or result.rc != 0:
        out.update(
            status="failed",
            reason="simple-chat topics %s" % (result.reason or "exited %s" % result.rc),
            skipped=len(wanted),
        )
        return out
    try:
        listing = json.loads(result.out)
        if not isinstance(listing, list):
            raise ValueError("not a list")
        names = [
            (t.get("name"), t.get("max_id") if isinstance(t.get("max_id"), int) else 0)
            for t in listing
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]
    except (ValueError, AttributeError):
        out.update(
            status="failed",
            reason="simple-chat topics did not print a topic list",
            skipped=len(wanted),
        )
        return out
    truncated = len(names) >= TOPICS_LIMIT
    names.sort(key=lambda item: -item[1])
    started = monotonic()
    for item in wanted:
        ident = item["identifier"]
        matches = [n for n, _ in names if n == ident or n.startswith(ident + " ")][
            :MAX_TOPICS_PER_ISSUE
        ]
        if not matches:
            out["by_ident"][ident] = {"ok": False} if truncated else {"ok": True, "events": []}
            continue
        if out["reads"] + len(matches) > MAX_READS or monotonic() - started > TOTAL_BUDGET_S:
            out["skipped"] += 1
            continue
        events: list[dict] = []
        good = True
        for name in matches:
            out["reads"] += 1
            read = run(argv_read(exe, name), timeout=TIMEOUT_S)
            if read.reason or read.rc != 0:
                good = False
                break
            try:
                events.extend(_events(_lines(read.out)))
            except ValueError:
                good = False
                break
        out["by_ident"][ident] = {"ok": True, "events": events} if good else {"ok": False}
    return out
