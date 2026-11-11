"""`open-loops render --facts PATH --markdown [--brief]`:  the full list, every item of every section (the DM
shows five).  `--brief` shows at most five items per list and "+ N more" for the rest:  what a routine reads to write
the DM, in a few hundred tokens instead of the whole file.

Every piece of text that came from outside (a title, a question, a lane name, a branch, a reason) is written
inside a code span by the F3 `render_untrusted`, so nothing in it can mention anyone, open a widget or link
anywhere.  Links are the ones the gatherer made (`facts["urls"]`).  Sentence gaps are U+00A0 plus a space.
A source that could not be read says so ("Missing:  <source> (<reason>)");  a count is never printed as 0
when its source was unreadable.
"""
from __future__ import annotations

from typing import Any, Mapping

from fleet_facts import safety

from . import central

__all__ = ["render_markdown", "SECTION_TITLES", "section_title", "BRIEF_LIMIT"]

BRIEF_LIMIT = 5

GAP = "  "
SECTION_TITLES = (
    "Decisions Waiting on You",
    "PRs Stuck 48 Hours or More",
    "Work Not on GitHub Yet",
    "Unpushed Work Older Than a Day",
    "Claims Nobody Is Working",
    "Your Earlier Asks Still Open",
)


def section_title(number: int) -> str:
    return SECTION_TITLES[number - 1]


def _code(value: Any, cap: int = 160) -> str:
    return safety.render(value, cap)


def _plain(value: Any, cap: int = 160) -> str:
    return safety.clean(value, cap)


def _age(hours: Any) -> str:
    return central.age_text(hours if isinstance(hours, (int, float)) and not isinstance(hours, bool) else None)


def _n(count: int, singular: str, plural: str | None = None) -> str:
    return "%d %s" % (count, singular if count == 1 else (plural or singular + "s"))


def _shown(items: list, limit: int | None) -> tuple[list, list[str]]:
    """(the items to list, ["+ N more"] when some were left out)."""
    if limit is None or len(items) <= limit:
        return items, []
    return items[:limit], ["+ %d more" % (len(items) - limit)]


def _link(label: str, url: str | None) -> str:
    return "[%s](%s)" % (label, url) if url else label


def _missing(source: str, block: Mapping) -> str:
    return "Missing:  %s (%s)" % (source, _plain(block.get("reason") or "no reason given"))


def _decisions(facts: Mapping, limit: int | None) -> list[str]:
    block = facts["decisions"]
    lines = ["## 1. %s%s" % (section_title(1), "" if block["count"] is None else " (%d)" % block["count"]), ""]
    if block["count"] is None:
        lines += [_missing("linear", block) + ("  Showing what was read." if block["items"] else ""), ""]
    elif block["count"] == 0:
        lines += ["None.", ""]
    shown, more = _shown(block["items"], limit)
    for item in shown:
        extra = []
        if item.get("tap_to_answer"):
            extra.append("tap to answer: option%s %s" % ("" if len(item["low_stakes"]) == 1 else "s", ", ".join(str(n) for n in item["low_stakes"])))
        topic = ("  " + _link("OWNER topic", item["topic_url"])) if item.get("topic_url") else ""
        tail = ("; " + "; ".join(extra)) if extra else ""
        lines.append("- %s  %s  (%s old%s)%s" % (_link(item["identifier"], item["url"]), _code(item["question"]), _age(item["age_hours"]), tail, topic))
    lines += ["- " + m for m in more]
    if block.get("over_five"):
        lines += ["", "More than 5 open from one seat:  %s." % ", ".join(_plain(s, 40) for s in block["over_five"])]
    if block.get("answered_awaiting_close"):
        lines += ["", "%d answered and waiting for the filer to close." % block["answered_awaiting_close"]]
    return lines + [""]


def _stuck(facts: Mapping, limit: int | None) -> list[str]:
    block = facts["stuck_prs"]
    lines = ["## 2. %s%s" % (section_title(2), "" if block["count"] is None else " (%d)" % block["count"]), ""]
    if block["count"] is None:
        lines += [_missing("github", block) + ("  Showing what was read." if block["items"] else ""), ""]
    elif block["count"] == 0:
        lines += ["None.", ""]
    shown, more = _shown(block["items"], limit)
    for pr in shown:
        state = " and ".join(s.replace("_", " ") for s in pr["state"])
        label = "%s#%s" % (_plain(pr["repo"], 100), pr["number"])
        lines.append("- %s  %s  %s;  head %s old%s" % (_link(label, pr["url"]), _code(pr["title"]), state, _age(pr["head_age_hours"]),
                                                      ";  draft" if pr["is_draft"] else ""))
    lines += ["- " + m for m in more]
    if block.get("unknown_age"):
        lines += ["", "%s could not be aged (no head commit time)." % _n(block["unknown_age"], "more PR", "more PRs")]
    return lines + ["", "Age is the head commit's age, not the time spent in that state.", ""]


def _lane_line(item: Mapping) -> str:
    bits = []
    where = " ".join(x for x in (item.get("repo"), item.get("branch")) if x)
    if where:
        bits.append(_code(where, 120))
    if item.get("idle_hours") is not None:
        bits.append("unchanged %s" % _age(item["idle_hours"]))
    if item.get("unpushed"):
        bits.append("%d unpushed" % item["unpushed"])
    if item.get("dirty"):
        bits.append("%d not snapshotted" % item["dirty"])
    if item.get("pr_url"):
        bits.append(_link("PR #%s" % item["pr_number"], item["pr_url"]))
    return "- %s  %s" % (_code(item.get("lane") or "(unnamed)", 80), ";  ".join(bits))


def _off_github(facts: Mapping, limit: int | None) -> list[str]:
    block = facts["not_on_github"]
    lines = ["## 3. %s%s" % (section_title(3), "" if block["count"] is None else " (%d)" % block["count"]), ""]
    if block["count"] is None:
        return lines + [_missing("autopush", block), ""]
    snap, armed, refused = block["snapshot_only"], block["armed_local_work"], block["refusals"]
    for title, group, line in (("Snapshot Only (%d, no age floor)" % snap["count"], snap, _lane_line),
                               ("Armed PRs With Local Work (%d)" % armed["count"], armed, _lane_line),
                               ("Refused (%d)" % refused["count"], refused,
                                lambda i: "- %s  %s" % (_code(i.get("lane") or "(unnamed)", 80), _code(i["reason"])))):
        shown, more = _shown(group["items"], limit)
        lines += ["### " + title, ""] + [line(i) for i in shown] + ["- " + m for m in more] + [""]
    return lines


def _unpushed(facts: Mapping, limit: int | None) -> list[str]:
    block = facts["unpushed_over_a_day"]
    lines = ["## 4. %s%s" % (section_title(4), "" if block["count"] is None else " (%d)" % block["count"]), ""]
    if block["count"] is None:
        return lines + [_missing("lanes", block), ""]
    if block["count"] == 0:
        unaged = block.get("unknown_age") or 0
        lines += ["None known;  %s could not be aged." % _n(unaged, "lane") if unaged else "None.", ""]
    shown, more = _shown(block["items"], limit)
    for item in shown:
        bits = ["%s old" % _age(item["age_hours"])]
        if item.get("unpushed"):
            bits.append("%d unpushed" % item["unpushed"])
        if item.get("dirty"):
            bits.append("%d dirty" % item["dirty"])
        if item.get("never_pushed"):
            bits.append("never pushed")
        where = " ".join(x for x in (item.get("repo"), item.get("branch")) if x)
        lines.append("- %s  %s;  %s" % (_code(item["lane"] or "(unnamed)", 80), _code(where, 120) if where else "", ";  ".join(bits)))
    lines += ["- " + m for m in more]
    notes = []
    if block.get("unknown_age"):
        notes.append("%s could not be aged" % _n(block["unknown_age"], "lane"))
    if block.get("active"):
        notes.append("%s being worked on right now" % (_n(block["active"], "lane") + (" is" if block["active"] == 1 else " are")))
    if block.get("landed"):
        notes.append("%s already landed (the doctor says safe to remove)" % _n(block["landed"], "lane"))
    return lines + (["", ";  ".join(notes) + "."] if notes else []) + [""]


def _claim_line(item: Mapping) -> str:
    return "- %s  %s  %s;  %s idle" % (_link(item["identifier"], item["url"]), _code(item["title"], 120),
                                      _plain(item.get("seat") or "no seat", 40), _age(item["idle_hours"]))


def _claims(facts: Mapping, limit: int | None) -> list[str]:
    block = facts["claims"]
    lines = ["## 5. %s" % section_title(5), ""]
    if block["counts"] is None:
        return lines + [_missing("linear", block), ""]
    if block["note"]:
        lines += [block["note"][0].upper() + block["note"][1:] + ".", ""]
    counts = block["counts"]
    lines += ["%d claims:  %d under 36 hours, %d at 36 hours or more, %d at 48 hours or more, %d unknown." % (
        block["total"], counts["none"], counts["36h"], counts["48h"], counts["unknown"]), ""]
    for title, group in (("48 Hours or More", block["idle_48h"]), ("36 Hours or More", block["idle_36h"])):
        shown, more = _shown(group, limit)
        lines += ["### %s (%d)" % (title, len(group)), ""] + [_claim_line(i) for i in shown] + ["- " + m for m in more] + [""]
    if block["unknown"]:
        shown, more = _shown(block["unknown"], limit)
        lines += ["### Unknown (%d)" % len(block["unknown"]), ""]
        lines += ["- %s  could not be told (%s)" % (_link(i["identifier"], i["url"]), ", ".join(_plain(s, 30) for s in i["unknown_sources"])) for i in shown]
        lines += ["- " + m for m in more] + [""]
    unchecked = sorted({s for i in block["idle_36h"] + block["idle_48h"] for s in i["unchecked"]})
    if unchecked:
        lines += ["Some of these were not checked against:  %s." % ", ".join(unchecked), ""]
    if block.get("orphans") or block.get("unreadable") or block.get("owner_held"):
        lines += ["Not banded:  %s, %s with an unreadable holder, %s held by the owner." % (
            _n(block.get("orphans") or 0, "orphan"), block.get("unreadable") or 0, block.get("owner_held") or 0), ""]
    return lines


def _asks(facts: Mapping, limit: int | None) -> list[str]:
    block = facts["owner_asks"]
    lines = ["## 6. %s%s" % (section_title(6), "" if block["count"] is None else " (%d)" % block["count"]), ""]
    if block["count"] is None:
        text = "Not available:  %s." % _plain(block.get("reason") or "no reason given")
        return lines + [text, ""]
    if block["count"] == 0:
        lines += ["None.", ""]
    shown, more = _shown(block["items"], limit)
    for item in shown:
        ident = ("%s  " % item["identifier"]) if item.get("identifier") else ""
        lines.append("- %s%s  (%s old)" % (ident, _code(item["text"]), _age(item.get("age_hours"))))
    return lines + ["- " + m for m in more] + [""]


def render_markdown(facts: Mapping, *, limit: int | None = None) -> str:
    """The list.  `limit` caps every list of items (None is the full list;  BRIEF_LIMIT is five)."""
    ok = [name for name, s in facts["sources"].items() if s["status"] == "ok"]
    lines = ["# Open Loops as of %s" % facts["built_at_local"], ""]
    lines += ["Facts %s built on %s." % (_code(facts["facts_id"], 80), _code(facts["host"], 60)) + GAP + "Sources ok:  %s." % (", ".join(_plain(n, 40) for n in ok) or "none")]
    for item in facts["missing"]:
        lines += ["", "Missing:  %s (%s)" % (_plain(item["source"], 40), _plain(item["reason"]))]
    lines.append("")
    for build in (_decisions, _stuck, _off_github, _unpushed, _claims, _asks):
        lines += build(facts, limit)
    lines += ["Full list:  %s" % facts["full_list_url"], ""]
    return "\n".join(lines).rstrip("\n") + "\n"
