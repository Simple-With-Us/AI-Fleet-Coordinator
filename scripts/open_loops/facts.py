"""gather:  build the facts document (schema 1) from the F6 gatherers and, on a host with no Mac, the signed
Mac facts.

    {"schema": 1, "tool": "open-loops", "version": "1.0.0",
     "facts_id": "ol-20261011-0700-<host>",   "built_at": "2026-10-11T12:00:00Z",  "built_at_local": "Sun, Oct 11, 7:00am",
     "host": "...", "no_mac": false,
     "sources": {"github": {"status": "ok", "reason": null}, "lanes": {...}, "mac_facts": {...}, "zulip": {...}, ...},
     "missing": [{"source": "autopush", "reason": "fleet-autopush is not installed"}],
     "decisions": {...}, "stuck_prs": {...}, "not_on_github": {...}, "unpushed_over_a_day": {...},
     "claims": {...}, "owner_asks": {...},
     "full_list_url": "https://github.com/...", "urls": [...every URL the gatherer made...],
     "redacted": []}                         # paths whose secret-shaped text was withheld (`fleet-facts:` ones came from F6)

`sources[...].status` is ok, absent or failed (with a reason).  `missing` is what the digest must show as
"Missing:  <source> (<reason>)":  every failed source, and every absent source the digest sections need
(github, linear, lanes, autopush), plus a Mac facts file that was ignored.  A count is None, never 0, when
its source could not be read.

Every free-text field was cleaned by the F3 `render_untrusted` (plain form) before it got here, the whole
document passes F3 `redact_fields`, and only this module makes URLs (urls.py).
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from fleet_facts import gather as ff_gather
from fleet_facts import safety
from fleet_facts.activity import parse_time
from fleet_facts.runner import Runner, absent
from fleet_facts.runner import run as default_run

from . import __version__, central, claims, macfacts, sections, urls, views, zulip_topics

__all__ = ["LAUNCH_ENV", "REQUIRED_SOURCES", "SCHEMA", "build_facts"]

SCHEMA = 1
LAUNCH_ENV = "CLAIM_EXPIRY_LAUNCH_AT"
LOCAL_MAC_PARTS = ("lanes", "autopush", "needs_owner", "asks", "report")
REQUIRED_SOURCES = ("github", "linear", "lanes", "autopush")
MAC_FILE_PARTS = ("lanes", "autopush")
# C6's `needs-owner list --all` document may ride in the signed file too (F7 is asked to include it):  with it GB
# can tell an answered decision from a waiting one.  Without it, GB's section 1 is as before (answered unknown).
MAC_FILE_OPTIONAL = ("needs_owner",)


def _answer(document: Mapping, name: str) -> dict:
    source = (document.get("sources") or {}).get(name) or {
        "status": "failed",
        "reason": "not gathered",
    }
    return {"status": source["status"], "reason": source.get("reason"), "data": document.get(name)}


def _not_built(name: str, answer: dict) -> dict:
    """`simple-chat asks status` is C3's, built in Phase 2:  today simple-chat answers it with a usage error (exit 2).
    That is "not built yet", not a failure to show as Missing every morning."""
    if (
        name == "asks"
        and answer["status"] == "failed"
        and str(answer.get("reason") or "").endswith("exited 2")
    ):
        return {"status": "absent", "reason": "not built yet (C3, Phase 2)", "data": None}
    return answer


def _host() -> str:
    name = (safety.clean(socket.gethostname().split(".")[0], 40) or "host").lower()
    return re.sub(r"[^a-z0-9-]+", "-", name).strip("-") or "host"


def _view_or_fail(answer: dict, view_fn, label: str) -> tuple[dict, dict | None]:
    """(the answer, its view).  A part that says ok but whose document this version cannot read is failed."""
    if answer["status"] != "ok":
        return answer, None
    view = view_fn(answer["data"])
    if not view["ok"]:
        return {"status": "failed", "reason": view["reason"], "data": None}, None
    return answer, view


def _launch(text: str | None, report: dict | None) -> datetime | None:
    when = parse_time(text) if text else None
    if when is None and report and isinstance(report.get("document"), dict):
        when = parse_time(report["document"].get("launch_at"))
    return when


def _missing(sources: Mapping[str, Mapping], unpushed: Mapping | None = None) -> list[dict]:
    out = []
    for name, source in sources.items():
        status, reason = source["status"], source.get("reason") or "no reason given"
        if (
            status == "failed"
            or (status == "absent" and name in REQUIRED_SOURCES)
            or (name == "mac_facts" and status == "absent" and reason.startswith("ignored"))
        ):
            out.append({"source": name, "reason": reason})
    # "Unpushed Work Older Than a Day" cannot say "none" about lanes whose age nobody can tell (the installed lane
    # doctor reports no last-change or dirty-age times yet):  that is Missing, so the DM has to say so.
    unaged = (unpushed or {}).get("unknown_age") or 0
    if unaged and sources.get("lanes", {}).get("status") == "ok":
        out.append(
            {
                "source": "lanes",
                "reason": "ages unavailable for %d lane%s with unpushed or dirty work"
                % (unaged, "" if unaged == 1 else "s"),
            }
        )
    return out


def build_facts(
    *,
    no_mac: bool = False,
    mac_facts: str | None = None,
    launch_at: str | None = None,
    run: Runner = default_run,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    fetch: macfacts.Fetcher = macfacts.fetch_https,
    environ: Mapping[str, str] | None = None,
) -> dict:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    use_file = bool(mac_facts)
    local_mac = not no_mac and not use_file
    mac = (
        macfacts.load(mac_facts, run=run, now=current, fetch=fetch, environ=environ)
        if use_file
        else None
    )
    parts = ["github", "linear"] + (list(LOCAL_MAC_PARTS) if local_mac else [])
    document = ff_gather.build(parts, no_mac=not local_mac, run=run, now=current, sleep=sleep)

    book = urls.UrlBook()
    mac_facts_url: str | None = None
    if mac_facts and mac_facts.startswith("https://"):
        mac_facts_url = book.add(mac_facts)
    answers: dict[str, dict] = {name: _not_built(name, _answer(document, name)) for name in parts}
    for name in LOCAL_MAC_PARTS:
        if name in answers:
            continue
        if mac is not None and mac["status"] == "ok" and name in MAC_FILE_PARTS:
            answers[name] = macfacts.part(mac["document"], name)
        elif mac is not None and mac["status"] != "ok" and name in MAC_FILE_PARTS:
            answers[name] = absent(
                "Mac facts file ignored" if mac["status"] == "absent" else "Mac facts file failed"
            )
        elif mac is not None and mac["status"] == "ok" and name in MAC_FILE_OPTIONAL:
            carried = macfacts.part(mac["document"], name)
            answers[name] = carried if carried["status"] != "absent" else absent("Mac only")
        else:
            answers[name] = absent(
                "--no-mac and no Mac facts file" if name in MAC_FILE_PARTS else "Mac only"
            )

    autopush, ap_view = _view_or_fail(answers["autopush"], views.autopush_view, "autopush")
    needs_owner, no_view = _view_or_fail(
        answers["needs_owner"], views.needs_owner_view, "needs_owner"
    )
    asks, asks_view = _view_or_fail(answers["asks"], views.asks_view, "asks")
    answers.update(autopush=autopush, needs_owner=needs_owner, asks=asks)
    launch = _launch(launch_at, answers["report"] if answers["report"]["status"] == "ok" else None)

    def zulip_reader(wanted: list[dict]) -> dict:
        return zulip_topics.read_signals(run, wanted)

    decisions = sections.decisions(answers["linear"], no_view, current, book)
    stuck = sections.stuck_prs(answers["github"], current, book)
    off_github = sections.not_on_github(autopush, ap_view, book)
    named: set[str] = set()
    for key in ("snapshot_only", "armed_local_work"):
        named |= {i["lane"] for i in ((off_github.get(key) or {}).get("items") or []) if i["lane"]}
    unpushed = sections.unpushed_over_a_day(answers["lanes"], named, current)
    claim_block = claims.build(
        answers["linear"],
        answers["github"],
        autopush,
        ap_view["rows"] if ap_view else [],
        launch_at=launch,
        now=current,
        zulip=zulip_reader,
        book=book,
    )
    asks_block = sections.owner_asks(asks, asks_view, book)

    sources = {
        name: {"status": answers[name]["status"], "reason": answers[name]["reason"]}
        for name in ("github", "linear", "lanes", "autopush", "needs_owner", "asks", "report")
    }
    zulip_info = claim_block.get("zulip")
    sources["zulip"] = (
        {"status": zulip_info["status"], "reason": zulip_info["reason"]}
        if zulip_info
        else {"status": "absent", "reason": "no claim needed it"}
    )
    if mac is not None:
        sources["mac_facts"] = {"status": mac["status"], "reason": mac["reason"]}

    day, hhmm = central.id_stamp(current)
    host = _host()
    facts = {
        "schema": SCHEMA,
        "tool": "open-loops",
        "version": __version__,
        "facts_id": "ol-%s-%s-%s" % (day, hhmm, host),
        "built_at": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_at_local": central.stamp(current),
        "host": host,
        "no_mac": bool(no_mac),
        "sources": sources,
        "missing": _missing(sources, unpushed),
        "decisions": decisions,
        "stuck_prs": stuck,
        "not_on_github": off_github,
        "unpushed_over_a_day": unpushed,
        "claims": claim_block,
        "owner_asks": asks_block,
        "full_list_url": book.add(urls.FULL_LIST_URL),
    }
    if mac_facts_url is not None:
        facts["mac_facts_url"] = mac_facts_url
    facts["urls"] = book.sorted()
    redacted, paths = safety.redact(facts)
    # What fleet_facts already withheld (paths into its own document) is kept, so the count is the whole story.
    redacted["redacted"] = ["fleet-facts:" + p for p in document.get("redacted", [])] + paths
    return redacted
