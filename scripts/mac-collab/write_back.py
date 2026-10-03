#!/usr/bin/env python3
"""Reverse-sync: board (findings.db) → live effort-log .md files + GitHub Issues.

The board is the write surface.  Copies:

  effort-row   → move the matching bullet in ~/apps/*-EFFORT-LOG.md
  github-issue → gh issue close/reopen (REST), only when state actually differs
  agent-report → append a bullet keyed by finding id (idempotent)

review-finding items are skipped.

This process must NOT treat inbound mac-collab-sync POSTs as board writes.
The server only bumps updated_at when a field changes, and this script also
keeps an applied-status map so a first run or a sync storm cannot close/reopen
every GitHub issue.  Markdown edits are surgical (no full-file re-serialize).
Git commit/push of docs/EFFORT-LOG.md is disabled: branch protection rejects
direct main pushes, and ~/Code trees are not this job's to touch.

Usage:
    python3 write_back.py --dry-run   # print what would change, write nothing
    python3 write_back.py             # one pass
    python3 write_back.py --loop      # run forever (pm2: mac-collab-writeback)
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APPS = Path.home() / "apps"
FINDINGS_DB = APPS / "mac-collab" / "findings.db"
CURSOR_FILE = APPS / "mac-collab" / "writeback_cursor.json"
SYNC_INTERVAL_S = 600
GH_TIMEOUT_S = 25

STATUS_TO_BUCKET = {
    "open": "planned",
    "in_progress": "in-progress",
    "completed": "completed",
    "deployed": "deployed",
    "addressed": "completed",
    "wontfix": "completed",
    "duplicate": "completed",
}

APP_REGISTRY: dict[str, tuple[Path, str | None]] = {
    "socratic-trade": (APPS / "TRADING-EFFORT-LOG.md", "Simple-With-Us/Socratic.Trade"),
    "congress-trade": (APPS / "CONGRESS-TRADE-EFFORT-LOG.md", "Simple-With-Us/Congress.Trade"),
    "usage-monitor": (APPS / "API-USAGE-MONITOR-EFFORT-LOG.md", "Simple-With-Us/Usage-Monitor"),
    "congress-trading-shared": (
        APPS / "CONGRESS-SHARED-EFFORT-LOG.md",
        "Simple-With-Us/congress-trading-shared",
    ),
    "dealdex": (APPS / "DEALDEX-EFFORT-LOG.md", "Simple-With-Us/DealDex"),
    "personal-site": (APPS / "PERSONAL-SITE-EFFORT-LOG.md", "Simple-With-Us/Personal-Site"),
    "autorotate": (APPS / "AUTOROTATE-EFFORT-LOG.md", "Simple-With-Us/Autorotate"),
    "contactlogo": (APPS / "CONTACTLOGO-EFFORT-LOG.md", "Simple-With-Us/ContactLogo"),
    "fleet-infra": (APPS / "FLEET-INFRA-EFFORT-LOG.md", "Simple-With-Us/AI-Fleet-Coordinator"),
    "botfleet": (APPS / "BOTFLEET-EFFORT-LOG.md", "Simple-With-Us/BotFleet"),
    "fleet-ops": (APPS / "FLEET-OPS-EFFORT-LOG.md", "Simple-With-Us/fleet-ops"),
}

APP_ALIASES: dict[str, str] = {
    "Socratic Trade": "socratic-trade",
    "Socratic.Trade": "socratic-trade",
    "ST": "socratic-trade",
    "socratic.trade": "socratic-trade",
    "trading": "socratic-trade",
    "CT": "congress-trade",
    "Congress.Trade": "congress-trade",
    "Congress Trade": "congress-trade",
    "congress.trade": "congress-trade",
    "UM": "usage-monitor",
    "Usage-Monitor": "usage-monitor",
    "Usage Monitor": "usage-monitor",
    "api-usage-monitor": "usage-monitor",
    "CTS": "congress-trading-shared",
    "congress-shared": "congress-trading-shared",
    "shared dependency": "congress-trading-shared",
    "shared": "congress-trading-shared",
    "DD": "dealdex",
    "DealDex.net": "dealdex",
    "DealDex": "dealdex",
    "deal dex": "dealdex",
    "PS": "personal-site",
    "Personal Site": "personal-site",
    "Personal-Site": "personal-site",
    "jays.services": "personal-site",
    "AR": "autorotate",
    "Autorotate.Codes": "autorotate",
    "Autorotate": "autorotate",
    "autorotate.codes": "autorotate",
    "CL": "contactlogo",
    "ContactLogo": "contactlogo",
    "contact-logo": "contactlogo",
    "contactlogo.com": "contactlogo",
    "AFC": "fleet-infra",
    "AFL": "fleet-infra",
    "AI Fleet Coordinator": "fleet-infra",
    "AI-Fleet-Coordinator": "fleet-infra",
    "ai-fleet-coordinator": "fleet-infra",
    "fleet": "fleet-infra",
    "BF": "botfleet",
    "BotFleet.app": "botfleet",
    "BotFleet": "botfleet",
    "botfleet": "botfleet",
    "OPS": "fleet-ops",
    "Fleet Ops": "fleet-ops",
    "fleet-ops": "fleet-ops",
}

SECTION_KEYWORDS = [
    ("deployed", "deployed"),
    ("completed", "completed"),
    ("recently closed", "completed"),
    ("recently completed", "completed"),
    ("historical", "completed"),
    ("archive", "completed"),
    ("closed", "completed"),
    ("in progress", "in-progress"),
    ("active", "in-progress"),
    ("planned", "planned"),
    ("reserved", "planned"),
]

BUCKET_HEADING = {
    "deployed": "## Deployed",
    "completed": "## Completed",
    "in-progress": "## In Progress",
    "planned": "## Planned / Reserved",
}

BULLET_RE = re.compile(r"^([-*])\s+(.*)$")
AGENT_REPORT_MARKER = "<!-- wb-agent-report:"


def classify_heading(text: str) -> str | None:
    low = text.strip().lower()
    if "changelog" in low:
        return None
    for keyword, bucket in SECTION_KEYWORDS:
        if keyword in low:
            return bucket
    return None


def sha1_key(first_line: str) -> str:
    text = re.sub(r"[*_`]", "", first_line)
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def resolve_app(app: str) -> tuple[Path, str | None] | None:
    if app in APP_REGISTRY:
        return APP_REGISTRY[app]
    alias = APP_ALIASES.get(app)
    if alias and alias in APP_REGISTRY:
        return APP_REGISTRY[alias]
    return None


# ── cursor ───────────────────────────────────────────────────────────────────

def load_cursor() -> dict:
    if CURSOR_FILE.is_file():
        try:
            data = json.loads(CURSOR_FILE.read_text())
            if isinstance(data, dict):
                data.setdefault("applied", {})
                data.setdefault("last_run", "1970-01-01T00:00:00+00:00")
                data.setdefault("bootstrapped", False)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_run": "1970-01-01T00:00:00+00:00",
        "applied": {},
        "bootstrapped": False,
    }


def save_cursor(data: dict) -> None:
    CURSOR_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


# ── db ───────────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{FINDINGS_DB}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_writeback_at_col() -> None:
    conn = sqlite3.connect(FINDINGS_DB, timeout=5.0)
    try:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(findings)")}
        if "writeback_at" not in existing:
            conn.execute("ALTER TABLE findings ADD COLUMN writeback_at TEXT")
            conn.commit()
            print("Migrated: added writeback_at column to findings.")
    finally:
        conn.close()


def stamp_writeback_at(finding_id: str, ts: str) -> None:
    conn = sqlite3.connect(FINDINGS_DB, timeout=5.0)
    try:
        conn.execute(
            "UPDATE findings SET writeback_at = ? WHERE id = ?", (ts, finding_id)
        )
        conn.commit()
    finally:
        conn.close()


def fetch_all_eligible() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, status, source_kind, app, external_uid, title,
                   addressed_by, reported_by, resolution, updated_at
            FROM findings
            WHERE source_kind IN ('effort-row', 'github-issue', 'agent-report')
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_changed_findings(since: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM findings
            WHERE updated_at > ?
              AND source_kind IN ('effort-row', 'github-issue', 'agent-report')
            ORDER BY updated_at ASC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── surgical markdown ────────────────────────────────────────────────────────

def _bullet_spans(lines: list[str]) -> list[tuple[int, int, str, str]]:
    """(start, end_exclusive, marker, first_line_content) for top-level bullets."""
    spans: list[tuple[int, int, str, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        m = BULLET_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        i += 1
        while i < n:
            if lines[i].startswith("## "):
                break
            if BULLET_RE.match(lines[i]):
                break
            if lines[i].startswith((" ", "\t")):
                i += 1
                continue
            break
        spans.append((start, i, m.group(1), m.group(2).strip()))
    return spans


def _heading_index_before(lines: list[str], idx: int) -> int | None:
    for i in range(idx, -1, -1):
        if lines[i].startswith("## "):
            return i
    return None


def _first_heading_for_bucket(lines: list[str], bucket: str) -> int | None:
    for i, line in enumerate(lines):
        if line.startswith("## ") and classify_heading(line[3:]) == bucket:
            return i
    return None


def _insert_after_heading(
    lines: list[str], heading_idx: int, block: list[str]
) -> list[str]:
    insert_at = heading_idx + 1
    if insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1
    return lines[:insert_at] + block + lines[insert_at:]


def move_bullet_in_file(
    md_path: Path, effort_key: str, target_bucket: str, dry_run: bool
) -> bool:
    if not md_path.is_file():
        print(f"  WARN: effort-log missing: {md_path}", file=sys.stderr)
        return False

    original = md_path.read_text(encoding="utf-8", errors="replace")
    newline = "\n" if original.endswith("\n") or "\r" not in original else "\r\n"
    lines = original.splitlines()

    found: tuple[int, int, str, str] | None = None
    for span in _bullet_spans(lines):
        if sha1_key(span[3]) == effort_key:
            found = span
            break
    if found is None:
        return False

    start, end, _marker, first_line = found
    heading_idx = _heading_index_before(lines, start)
    current_bucket = (
        classify_heading(lines[heading_idx][3:]) if heading_idx is not None else None
    )
    if current_bucket == target_bucket:
        return False

    print(
        f"  {'[DRY-RUN] ' if dry_run else ''}Moving bullet from "
        f"[{current_bucket}] → [{target_bucket}]: {first_line[:80]}"
    )
    if dry_run:
        return True

    block = lines[start:end]
    del lines[start:end]
    # headings after the removed block shift left by len(block)
    target_hi = _first_heading_for_bucket(lines, target_bucket)
    if target_hi is None:
        last_h = max(
            (i for i, l in enumerate(lines) if l.startswith("## ")),
            default=len(lines) - 1,
        )
        heading = BUCKET_HEADING.get(
            target_bucket, f"## {target_bucket.replace('-', ' ').title()}"
        )
        insert_at = last_h + 1
        lines[insert_at:insert_at] = ["", heading, ""]
        target_hi = _first_heading_for_bucket(lines, target_bucket)
    if target_hi is None:
        print(f"  WARN: could not place [{target_bucket}] in {md_path}", file=sys.stderr)
        return False
    lines = _insert_after_heading(lines, target_hi, block)
    md_path.write_text(newline.join(lines) + newline, encoding="utf-8")
    return True


def append_agent_report_to_file(
    md_path: Path, finding: dict, target_bucket: str, dry_run: bool
) -> bool:
    if not md_path.is_file():
        print(f"  WARN: effort-log missing: {md_path}", file=sys.stderr)
        return False

    original = md_path.read_text(encoding="utf-8", errors="replace")
    marker = f"{AGENT_REPORT_MARKER}{finding['id']} -->"
    if marker in original:
        key = None
        for line in original.splitlines():
            if marker in line:
                bm = BULLET_RE.match(line)
                if bm:
                    key = sha1_key(bm.group(2).strip())
                break
        if key:
            return move_bullet_in_file(md_path, key, target_bucket, dry_run)
        return False

    ts = datetime.now().strftime("%Y-%m-%d")
    by = finding.get("addressed_by") or finding.get("reported_by") or "AG"
    status_tag = target_bucket.upper().replace("-", "_")
    title = finding.get("title", "untitled")
    resolution = finding.get("resolution") or ""
    resolution_part = f"  {resolution}" if resolution else ""
    bullet_text = (
        f"**{ts} - {by} - {status_tag} - {title}.**{resolution_part} {marker}"
    ).strip()

    print(
        f"  {'[DRY-RUN] ' if dry_run else ''}Appending agent-report to "
        f"[{target_bucket}]: {bullet_text[:80]}"
    )
    if dry_run:
        return True

    newline = "\n" if original.endswith("\n") or "\r" not in original else "\r\n"
    lines = original.splitlines()
    target_hi = _first_heading_for_bucket(lines, target_bucket)
    if target_hi is None:
        heading = BUCKET_HEADING.get(
            target_bucket, f"## {target_bucket.replace('-', ' ').title()}"
        )
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend([heading, ""])
        target_hi = len(lines) - 2
    lines = _insert_after_heading(lines, target_hi, [f"- {bullet_text}"])
    md_path.write_text(newline.join(lines) + newline, encoding="utf-8")
    return True


# ── GitHub Issues (REST, idempotent) ─────────────────────────────────────────

def _desired_gh_state(board_status: str) -> str:
    if board_status in ("open", "in_progress"):
        return "open"
    return "closed"


def _gh_issue_state(repo: str, number: str) -> str | None:
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{number}", "--jq", ".state"],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
    )
    if r.returncode != 0:
        print(f"  WARN: gh api view {repo}#{number}: {r.stderr.strip()}", file=sys.stderr)
        return None
    return (r.stdout or "").strip().lower()


def sync_github_issue(finding: dict, dry_run: bool) -> bool:
    uid = finding.get("external_uid") or ""
    m = re.match(r"^issue-(.+)-(\d+)$", uid)
    if not m:
        print(f"  WARN: could not parse github-issue uid: {uid}", file=sys.stderr)
        return False

    repo = m.group(1)
    number = m.group(2)
    desired = _desired_gh_state(finding.get("status", "open"))

    current = _gh_issue_state(repo, number)
    if current is None:
        return False
    if current == desired:
        return False

    print(
        f"  {'[DRY-RUN] ' if dry_run else ''}GitHub #{number} in {repo}: "
        f"{current} → {desired} (board status={finding.get('status')})"
    )
    if dry_run:
        return True

    r = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}/issues/{number}",
            "-f",
            f"state={desired}",
        ],
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
    )
    if r.returncode != 0:
        print(
            f"  WARN: gh api patch {repo}#{number}: {r.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


# ── pass ─────────────────────────────────────────────────────────────────────

def bootstrap_applied(cursor: dict) -> dict:
    """Record current board statuses without writing copies.

    Prevents the first (or post-crash) pass from treating every finding as a
    freshly changed board write.
    """
    applied = dict(cursor.get("applied") or {})
    rows = fetch_all_eligible()
    for f in rows:
        applied[f["id"]] = f.get("status") or "open"
    cursor["applied"] = applied
    cursor["bootstrapped"] = True
    cursor["last_run"] = datetime.now(timezone.utc).isoformat()
    save_cursor(cursor)
    print(f"write_back: bootstrapped applied map for {len(applied)} finding(s); no copies written")
    return cursor


def should_advance_last_run(errors: int) -> bool:
    """Advance last_run only after a clean pass so failed items are retried."""
    return errors == 0


def write_back_once(dry_run: bool = False) -> None:
    ensure_writeback_at_col()
    cursor = load_cursor()

    if not cursor.get("bootstrapped") or not cursor.get("applied"):
        if dry_run:
            print("write_back: would bootstrap applied map; no copies written")
            return
        bootstrap_applied(cursor)
        return

    since = cursor.get("last_run") or "1970-01-01T00:00:00+00:00"
    now_ts = datetime.now(timezone.utc).isoformat()
    findings = fetch_changed_findings(since)
    applied: dict = cursor["applied"]

    if not findings:
        print(f"write_back: no changes since {since}")
        if not dry_run:
            cursor["last_run"] = now_ts
            save_cursor(cursor)
        return

    print(f"write_back: {len(findings)} updated finding(s) since {since}")
    stamped: list[str] = []
    acted = 0
    skipped_same = 0
    errors = 0

    for f in findings:
        fid = f["id"]
        kind = f.get("source_kind")
        board_status = f.get("status") or "open"
        if applied.get(fid) == board_status:
            skipped_same += 1
            continue

        target_bucket = STATUS_TO_BUCKET.get(board_status, "planned")
        reg = resolve_app(f.get("app") or "")
        if reg is None:
            print(f"  SKIP unknown app={f.get('app')!r} for finding {fid[:8]}", file=sys.stderr)
            continue

        board_path, _gh_repo = reg
        changed = False
        try:
            if kind == "effort-row":
                uid = f.get("external_uid") or ""
                effort_key = uid.removeprefix("effort-")
                if len(effort_key) == 40:
                    changed = move_bullet_in_file(
                        board_path, effort_key, target_bucket, dry_run
                    )
                else:
                    print(f"  SKIP malformed effort-row uid={uid!r}", file=sys.stderr)
            elif kind == "github-issue":
                changed = sync_github_issue(f, dry_run)
            elif kind == "agent-report":
                changed = append_agent_report_to_file(
                    board_path, f, target_bucket, dry_run
                )
        except subprocess.TimeoutExpired as e:
            print(f"  WARN: timeout on {kind} {fid[:8]}: {e}", file=sys.stderr)
            errors += 1
            continue
        except Exception as e:
            print(f"  WARN: {kind} {fid[:8]} failed: {e}", file=sys.stderr)
            errors += 1
            continue

        if dry_run:
            if changed:
                acted += 1
            continue

        # Record applied even when the copy was already in the right place so
        # we do not keep retrying a SHA1 miss or a no-op GH state.  Still stamp
        # writeback_at on that no-op: a claim (in_progress) against an already
        # OPEN issue must start the inbound grace window.
        applied[fid] = board_status
        stamped.append(fid)
        if changed:
            acted += 1

    if not dry_run:
        for fid in stamped:
            stamp_writeback_at(fid, now_ts)
        cursor["applied"] = applied
        # Do not walk last_run past a failed item.  A timeout used to drop the
        # retry, then inbound sync reverted the board write.
        if should_advance_last_run(errors):
            cursor["last_run"] = now_ts
        save_cursor(cursor)

    print(
        f"write_back: done. acted={acted} skipped_same_status={skipped_same} "
        f"errors={errors}"
    )


def run_forever() -> None:
    while True:
        try:
            write_back_once()
        except Exception as e:
            print(f"write_back_once failed: {e}", file=sys.stderr)
        time.sleep(SYNC_INTERVAL_S)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_forever()
    else:
        write_back_once(dry_run="--dry-run" in sys.argv)
