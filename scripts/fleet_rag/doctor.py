"""Platform parity report (`recall doctor --platforms`) and the contribution digest
(`recall digest`).

Every row of the parity table is (status, check, detail) with status OK / WARN / FAIL.  The
detail is a file name, a route name, an age in hours, or a boolean word -- never a config value,
never a URL with a credential, never the body of an HTTP response.  The report is pure given its
inputs: the probes (HTTP GET, ssh, the Qdrant factory, the clock) are injected so the tests run
against a throwaway HOME with no network.

The digest lists agent contributions in a window grouped by app, then category, for the weekly
Oracle routine and the owner's note.  It only reads (scroll with the recall_api filter, which
carries the meta must_not).
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import urllib.error
import urllib.request
import zoneinfo
from typing import Any, Callable

from . import core, health, recall_api, search_log
from .core import FleetRagError, now_ms

SERVER_NAME = "fleet-recall"
SKILL_NAME = "fleet-recall"
SEAT_MCP_HEALTH = "http://127.0.0.1:8793/health"
SEAT_MCP_ROUTES = ("/recall/search", "/recall/stats", "/recall/contribute")
BOTFLEET_ROUTINES = "http://127.0.0.1:8799/api/routines"
REQUIRED_ROUTINES = ("Fleet RAG nightly ingest", "Fleet RAG weekly health + recall eval")
MAX_INGEST_HOURS = 30
HOOK_FILES = ("fleet-recall-session-start.sh", "fleet-recall-stop.py")
BOX_HOST = "coolify"
BOX_HEALTH = "/usr/local/sbin/fleet-qdrant-health.sh"
HTTP_TIMEOUT = 5
SSH_TIMEOUT = 30
_CT = zoneinfo.ZoneInfo("America/Chicago")


def _row(status: str, check: str, detail: str = "") -> dict:
    return {"status": status, "check": check, "detail": detail}


# --------------------------------------------------------------------------- config files

def mcp_config_paths(home: pathlib.Path) -> list[tuple[str, pathlib.Path, str, bool]]:
    """(label, path, kind, optional) for every MCP config the installer manages."""
    return [
        ("claude", home / ".claude.json", "json", False),
        ("cursor", home / ".cursor" / "mcp.json", "json", False),
        ("gemini", home / ".gemini" / "config" / "mcp_config.json", "json", False),
        ("codex", home / ".codex" / "config.toml", "toml", False),
        ("grok", home / ".grok" / "config.toml", "toml", False),
        ("grok-acp", home / "apps" / "grok-acp-runtime" / "acp-home-config.toml", "toml", True),
    ]


def skill_dirs(home: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    return [
        ("claude", home / ".claude" / "skills" / SKILL_NAME / "SKILL.md"),
        ("cursor", home / ".cursor" / "skills" / SKILL_NAME / "SKILL.md"),
        ("codex", home / ".codex" / "skills" / SKILL_NAME / "SKILL.md"),
    ]


def json_has_server(path: pathlib.Path) -> bool | None:
    """True/False when the file parses; None when it is absent or not JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and SERVER_NAME in servers


def toml_has_server(path: pathlib.Path) -> bool | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    header = re.compile(r"^\s*\[\s*mcp_servers\.(?:\"?)" + re.escape(SERVER_NAME) + r"(?:\"?)\s*\]\s*$")
    return any(header.match(line) for line in text.splitlines())


def _config_rows(home: pathlib.Path) -> list[dict]:
    rows = []
    for label, path, kind, optional in mcp_config_paths(home):
        check = f"mcp:{label}"
        if not path.exists():
            rows.append(_row("WARN" if optional else "FAIL", check, f"{path.name} absent"))
            continue
        present = json_has_server(path) if kind == "json" else toml_has_server(path)
        if present is None:
            rows.append(_row("FAIL", check, f"{path.name} unreadable"))
        elif present:
            rows.append(_row("OK", check, f"{path.name} has {SERVER_NAME}"))
        else:
            rows.append(_row("FAIL", check, f"{path.name} missing {SERVER_NAME}"))
    for label, path in skill_dirs(home):
        rows.append(_row("OK" if path.is_file() else "FAIL", f"skill:{label}",
                         f"{path.parent.name}/SKILL.md {'present' if path.is_file() else 'missing'}"))
    return rows


def expected_hook_commands(hooks_dir: pathlib.Path) -> dict[str, str]:
    """The exact command string install-fleet-rag.sh --hooks writes per event.

    Ownership is equality on this string (mirrors the installer's is_ours): a wrapper that merely
    mentions the hook file is somebody else's entry.
    """
    return {"SessionStart": f"{hooks_dir}/{HOOK_FILES[0]}", "Stop": f"python3 {hooks_dir}/{HOOK_FILES[1]}"}


def _hook_commands(settings_path: pathlib.Path) -> dict[str, list[str]]:
    """Every hook command string settings.json holds under SessionStart / Stop."""
    out: dict[str, list[str]] = {"SessionStart": [], "Stop": []}
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return out
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event in out:
        entries = hooks.get(event)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or []:
                if isinstance(h, dict):
                    out[event].append(str(h.get("command", "")))
    return out


def hooks_registered(settings_path: pathlib.Path, hooks_dir: pathlib.Path | None = None) -> dict[str, bool]:
    """Which of our two hooks settings.json registers with exactly the installer's command.

    hooks_dir defaults to the hooks/ directory next to settings.json (~/.claude/hooks).
    """
    exact = expected_hook_commands(hooks_dir or settings_path.parent / "hooks")
    found = _hook_commands(settings_path)
    return {event: exact[event] in found[event] for event in exact}


def hooks_foreign(settings_path: pathlib.Path, hooks_dir: pathlib.Path | None = None) -> dict[str, bool]:
    """Events with an entry that mentions our hook file but is not the installer's exact command."""
    exact = expected_hook_commands(hooks_dir or settings_path.parent / "hooks")
    found = _hook_commands(settings_path)
    return {event: any(fname in c and c != exact[event] for c in found[event])
            for event, fname in zip(("SessionStart", "Stop"), HOOK_FILES)}


def _hook_rows(home: pathlib.Path) -> list[dict]:
    rows = []
    hooks_dir = home / ".claude" / "hooks"
    settings = home / ".claude" / "settings.json"
    reg = hooks_registered(settings, hooks_dir)
    foreign = hooks_foreign(settings, hooks_dir)
    for fname, event in zip(HOOK_FILES, ("SessionStart", "Stop")):
        installed = (hooks_dir / fname).is_file()
        if installed and reg[event]:
            rows.append(_row("OK", f"hook:{event}", f"{fname} installed and registered"))
        elif installed and foreign[event]:
            rows.append(_row("WARN", f"hook:{event}",
                             f"{fname} installed, settings.json entry is not the installer's (foreign)"))
        elif installed:
            rows.append(_row("WARN", f"hook:{event}", f"{fname} installed, not in settings.json"))
        else:
            rows.append(_row("WARN", f"hook:{event}", f"{fname} not installed (install-fleet-rag.sh --hooks)"))
    return rows


# --------------------------------------------------------------------------- services

def default_http_get(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    """GET JSON; raises on any failure.  The body is parsed and never logged."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _seat_mcp_row(http_get: Callable[[str], Any]) -> dict:
    try:
        body = http_get(SEAT_MCP_HEALTH)
    except Exception as e:  # noqa: BLE001 - class only
        return _row("FAIL", "seat-mcp:/health", f"unreachable ({type(e).__name__})")
    routes = body.get("recall") if isinstance(body, dict) else None
    routes = [str(r) for r in routes] if isinstance(routes, list) else []
    missing = [r for r in SEAT_MCP_ROUTES if r not in routes]
    if missing:
        return _row("FAIL", "seat-mcp:/health", "missing " + " ".join(missing))
    return _row("OK", "seat-mcp:/health", "lists " + " ".join(SEAT_MCP_ROUTES))


def _normalized_routine_name(name: str) -> str:
    """Case-insensitive, whitespace-normalized key so "Fleet RAG nightly ingest" and the
    BotFleet UI's "Fleet RAG Nightly Ingest" match the same required routine."""
    return " ".join(str(name).lower().split())


def _routine_rows(http_get: Callable[[str], Any]) -> list[dict]:
    try:
        body = http_get(BOTFLEET_ROUTINES)
    except Exception as e:  # noqa: BLE001
        return [_row("FAIL", f"botfleet:{name}", f"api unreachable ({type(e).__name__})")
                for name in REQUIRED_ROUTINES]
    items = body.get("routines") if isinstance(body, dict) else body
    by_name: dict[str, dict] = {}
    for r in items if isinstance(items, list) else []:
        if isinstance(r, dict) and isinstance(r.get("name"), str):
            by_name[_normalized_routine_name(r["name"])] = r
    rows = []
    for name in REQUIRED_ROUTINES:
        r = by_name.get(_normalized_routine_name(name))
        if r is None:
            rows.append(_row("FAIL", f"botfleet:{name}", "missing"))
        elif r.get("enabled") is False:
            rows.append(_row("FAIL", f"botfleet:{name}", "present, disabled"))
        else:
            rows.append(_row("OK", f"botfleet:{name}", "present, enabled"))
    return rows


def _last_run_row(home: pathlib.Path, now: int) -> dict:
    path = home / "apps" / "fleet-rag" / "state" / "last-run.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _row("FAIL", "ingest:last-run", f"{path.name} absent or unreadable")
    finished = data.get("finished_at") if isinstance(data, dict) else None
    if not isinstance(finished, (int, float)):
        return _row("FAIL", "ingest:last-run", f"{path.name} has no finished_at")
    hours = (now - int(finished)) / 3_600_000
    ok = bool(data.get("ok"))
    detail = f"age_hours={hours:.1f} ok={'true' if ok else 'false'}"
    if hours < 0 or hours > MAX_INGEST_HOURS:
        return _row("FAIL", "ingest:last-run", f"{detail} (stale>{MAX_INGEST_HOURS}h)")
    if not ok:
        return _row("FAIL", "ingest:last-run", f"{detail} (last run failed)")
    return _row("OK", "ingest:last-run", detail)


DIRECT_PATH_BLOCKED_DETAIL = "tailscale logged out; direct path skipped (use recall-tunnel or tailscale login)"
RERANK_BLOCKED_DETAIL = "direct path skipped (Tailscale down); rerank served by recall.jays.services"


def _sentinel_row(qdrant_factory: Callable[[], Any], now: int, direct_path_blocked: bool = False) -> dict:
    if direct_path_blocked:
        # The caller (recall's cmd_doctor) has already established Tailscale is believed down
        # with no operator override -- never even attempt the Qdrant call, which would otherwise
        # sit in a multi-minute retry storm before failing.  See public_fallback.require_direct_path.
        return _row("WARN", "ingest:sentinel", DIRECT_PATH_BLOCKED_DETAIL)
    try:
        q = qdrant_factory()
        payload = health.read_ingest_sentinel(q)
    except Exception as e:  # noqa: BLE001 - never echo the message (could carry a host or body)
        return _row("FAIL", "ingest:sentinel", f"qdrant unreachable ({type(e).__name__})")
    if not payload or not payload.get("updated_at"):
        return _row("FAIL", "ingest:sentinel", "none_yet")
    hours = (now - int(payload["updated_at"])) / 3_600_000
    ok = bool(payload.get("ok"))
    detail = f"age_hours={hours:.1f} ok={'true' if ok else 'false'}"
    if hours > MAX_INGEST_HOURS:
        return _row("FAIL", "ingest:sentinel", f"{detail} (stale>{MAX_INGEST_HOURS}h)")
    if not ok:
        return _row("FAIL", "ingest:sentinel", f"{detail} (last run failed)")
    return _row("OK", "ingest:sentinel", detail)


def default_rerank_check() -> bool | None:
    """True/False when TEI_RERANK_URL/TEI_RERANK_API_KEY are configured; None otherwise.

    Credential lookup failures (no config anywhere reachable) are treated the same as "not
    configured" rather than raised -- this is one optional row in a larger report, not a reason
    to fail the whole doctor run.
    """
    try:
        cfg = recall_api.get_config(need_write=False)
    except FleetRagError:
        return None
    if not core.rerank_configured(cfg):
        return None
    return core.rerank_healthy(cfg)


def _rerank_row(rerank_check: Callable[[], bool | None], direct_path_blocked: bool = False) -> dict:
    if direct_path_blocked:
        # Same reasoning as _sentinel_row: TEI_RERANK_URL is a Tailscale-mesh address too (see
        # docs/RAG-FLEET-INFRA.md), so probing it directly when the caller has already
        # established Tailscale is believed down (with no operator override) would just spend a
        # probe timeout to rediscover what's already known, and would report a live rerank
        # service as FAIL.  See public_fallback.require_direct_path.
        return _row("WARN", "tei:rerank", RERANK_BLOCKED_DETAIL)
    try:
        healthy = rerank_check()
    except Exception as e:  # noqa: BLE001 - class only
        return _row("WARN", "tei:rerank", f"check unavailable ({type(e).__name__})")
    if healthy is None:
        return _row("WARN", "tei:rerank", "not configured (TEI_RERANK_URL/TEI_RERANK_API_KEY unset)")
    return _row("OK" if healthy else "FAIL", "tei:rerank", "reachable" if healthy else "unreachable")


def default_ssh_run(host: str, command: str, timeout: int = SSH_TIMEOUT) -> tuple[int, str]:
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                          capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout


_BOX_LINE = re.compile(r"^(OK|WARN|FAIL)\s+(\S+)\s*(.*)$")


def parse_box_output(text: str) -> list[dict]:
    """fleet-qdrant-health.sh prints `OK  name detail` rows; keep only the names and ages."""
    rows = []
    for line in text.splitlines():
        m = _BOX_LINE.match(line.strip())
        if not m:
            continue
        status, name, detail = m.groups()
        rows.append(_row(status, f"box:{name}", detail.strip()))
    return rows


def _box_rows(ssh_run: Callable[[str, str], tuple[int, str]]) -> list[dict]:
    try:
        rc, out = ssh_run(BOX_HOST, BOX_HEALTH)
    except Exception as e:  # noqa: BLE001
        return [_row("FAIL", "box:ssh", f"ssh failed ({type(e).__name__})")]
    rows = parse_box_output(out)
    if not rows:
        return [_row("FAIL", "box:ssh", f"no health rows (exit {rc})")]
    return rows


# --------------------------------------------------------------------------- report

def default_qdrant_factory() -> Any:
    cfg = recall_api.get_config(need_write=False)
    return recall_api.Qdrant(cfg)


def platforms_report(home: pathlib.Path | str | None = None, box: bool = False,
                     http_get: Callable[[str], Any] | None = None,
                     ssh_run: Callable[[str, str], tuple[int, str]] | None = None,
                     qdrant_factory: Callable[[], Any] | None = None,
                     rerank_check: Callable[[], bool | None] | None = None,
                     now: int | None = None, direct_path_blocked: bool = False) -> dict:
    """{"rows": [...], "ok": bool, "counts": {"OK": n, "WARN": n, "FAIL": n}}.

    `direct_path_blocked` is decided by the caller (recall's cmd_doctor, via
    public_fallback.require_direct_path's same Tailscale-down-with-no-override check) -- this
    module never probes Tailscale itself, keeping the report pure given its inputs.  When set,
    the ingest:sentinel and tei:rerank rows are WARN "direct path skipped" instead of attempting
    (and, on this Mac tonight, waiting to fail or falsely FAILing a healthy box) the real
    Qdrant / TEI rerank calls -- both addresses are Tailscale-mesh-only (see
    docs/RAG-FLEET-INFRA.md).
    """
    home = pathlib.Path(home) if home else pathlib.Path(os.path.expanduser("~"))
    now = now if now is not None else now_ms()
    http_get = http_get or default_http_get
    ssh_run = ssh_run or default_ssh_run
    qdrant_factory = qdrant_factory or default_qdrant_factory
    rerank_check = rerank_check or default_rerank_check
    rows: list[dict] = []
    rows += _config_rows(home)
    rows += _hook_rows(home)
    rows.append(_seat_mcp_row(http_get))
    rows += _routine_rows(http_get)
    rows.append(_last_run_row(home, now))
    rows.append(_sentinel_row(qdrant_factory, now, direct_path_blocked=direct_path_blocked))
    rows.append(_rerank_row(rerank_check, direct_path_blocked=direct_path_blocked))
    if box:
        rows += _box_rows(ssh_run)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("OK", "WARN", "FAIL")}
    return {"rows": rows, "ok": counts["FAIL"] == 0, "counts": counts}


def format_platforms(report: dict) -> str:
    width = max((len(r["check"]) for r in report["rows"]), default=10)
    lines = [f"{'STATUS':<6} {'check':<{width}}  detail", f"{'------':<6} {'-' * width}  ------"]
    for r in report["rows"]:
        lines.append(f"{r['status']:<6} {r['check']:<{width}}  {r['detail']}")
    c = report["counts"]
    lines.append("")
    lines.append(f"overall: {'ok' if report['ok'] else 'PROBLEMS'}  "
                 f"(OK {c['OK']}, WARN {c['WARN']}, FAIL {c['FAIL']})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- digest

def _first_line(text: str, n: int = 100) -> str:
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= n else line[:n - 3].rstrip() + "..."


def _day(ms: int) -> str:
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(int(ms) / 1000, _CT).strftime("%Y-%m-%d")


def contribution_digest(qdrant: Any, days: int = 7, app: str | None = None,
                        now: int | None = None, include_searches: bool = False,
                        state_dir: str | None = None) -> dict:
    """Agent contributions from the last `days` days grouped app -> category -> [entries].

    Each entry: seat, date (Central), doc_id, title (or the first line of the text), url.
    Entries are newest first inside a category; apps and categories are sorted by name.
    When include_searches is True, includes search volume and search-to-contribute ratios.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise FleetRagError("days must be a positive integer")
    app = recall_api._opt_str("app", app)
    if app:
        app = app.lower()
    now = now if now is not None else now_ms()
    flt = recall_api.build_filter(source="agent-contribution", app=app, since_days=days, now=now)
    entries = []
    seat_contribs: dict[str, int] = {}
    for p in qdrant.scroll(flt, limit=256):
        pl = p.get("payload") or {}
        seat_name = pl.get("seat") or "-"
        seat_contribs[seat_name] = seat_contribs.get(seat_name, 0) + 1
        entries.append({
            "app": pl.get("app") or "fleet",
            "category": pl.get("category") or "lesson",
            "seat": seat_name,
            "created_at": int(pl.get("created_at") or 0),
            "date": _day(int(pl.get("created_at") or 0)),
            "doc_id": pl.get("doc_id") or str(p.get("id", "")),
            "title": pl.get("title") or _first_line(pl.get("text", "")),
            "url": pl.get("url") or "",
        })
    entries.sort(key=lambda e: (e["app"], e["category"], -e["created_at"], e["doc_id"]))
    apps: dict[str, dict[str, list[dict]]] = {}
    for e in entries:
        apps.setdefault(e["app"], {}).setdefault(e["category"], []).append(e)

    res: dict[str, Any] = {
        "days": days,
        "app": app,
        "since": now - days * recall_api.DAY_MS,
        "total": len(entries),
        "apps": apps,
    }

    if include_searches:
        searches = search_log.search_summary(state_dir=state_dir, days=days, app=app, now_ms=now)
        ratios = search_log.compute_ratios(seat_contribs, searches.get("by_seat", {}))
        res["searches"] = searches
        res["ratios"] = ratios

    return res


def format_digest(d: dict) -> str:
    scope = f" for app {d['app']}" if d.get("app") else ""
    lines = [f"{d['total']} agent contribution(s) in the last {d['days']} day(s){scope}"]
    if not d["total"] and not d.get("searches"):
        return lines[0]
    for app, cats in d.get("apps", {}).items():
        n = sum(len(v) for v in cats.values())
        lines.append("")
        lines.append(f"{app}  ({n})")
        for cat, items in cats.items():
            lines.append(f"  {cat}  ({len(items)})")
            for e in items:
                lines.append(f"    {e['date']}  {e['seat']:<10} {e['title'] or e['doc_id']}")
                lines.append(f"    {'':<22}{e['doc_id']}" + (f"  {e['url']}" if e["url"] else ""))

    if d.get("searches"):
        s = d["searches"]
        lines.append("")
        lines.append(f"=== Fleet Retrieval Telemetry ({d['days']}d) ===")
        lines.append(f"Total searches: {s.get('total_searches', 0)} across {len(s.get('by_seat', {}))} seat(s) (avg latency: {s.get('avg_latency_ms', 0)} ms)")
        if d.get("ratios"):
            lines.append("Search-to-Contribute Ratios (target >= 3.0x; search before diagnosing):")
            for r in d["ratios"]:
                ratio_str = f"{r['ratio']:.2f}x" if r["ratio"] is not None else "N/A"
                warn = "  [WARN: low retrieval ratio - search before diagnosing]" if r.get("warning") else ""
                lines.append(f"  {r['seat']:<10} {r['searches']:>3} search(es) / {r['contributions']:>3} contrib(s) = {ratio_str}{warn}")

    return "\n".join(lines)
