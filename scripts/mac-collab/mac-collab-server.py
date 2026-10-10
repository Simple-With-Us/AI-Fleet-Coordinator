#!/usr/bin/env python3
"""Mac collab file + findings server — mac.jays.services via Jay's Tunnel.

Public: GET /health (and GET /) only.
Basic-Auth-gated (any username, password = MAC_COLLAB_TOKEN): GET /board —
the page itself, not just its data.
Bearer/Basic-token-gated: GET /files, GET /files/<name>; GET/POST /findings,
GET /findings/stats, GET/PATCH /findings/<id>, GET/POST /findings/<id>/comments.
Allowlist only for /files: live effort boards, protocol, MAC-LOCAL-PROCESSES,
AGENT-SYNC. Secret files are not served. Names-only key list is GET
/files/key-names. Binds 127.0.0.1:8792. No home listing. No .bak / source
trees.

Findings store: SQLite at findings.db next to this script. Every request
opens its own short-lived connection (safe under ThreadingHTTPServer's
one-thread-per-request model); WAL mode is set once at startup for
read/write concurrency.
"""
from __future__ import annotations

import base64
import collections
import errno
import hashlib
import hmac
import html
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BIND = ("127.0.0.1", 8792)
# Startup-only probe used to tell a LIVE sibling server from a dead one that is
# still holding the port.  Generous because this Mac runs a dozen agent lanes and
# a loaded box can push /health into the seconds -- a slow server must never be
# mistaken for a stale one and killed.
HEALTH_PROBE_TIMEOUT = 20.0
STARTED = time.time()
HOME = Path("/Users/jay")
APPS = HOME / "apps"
SECRETS = HOME / ".secrets" / "mac-collab.env"
DB_PATH = Path(__file__).resolve().parent / "findings.db"

ALLOW = {
    "TRADING-EFFORT-LOG.md": APPS / "TRADING-EFFORT-LOG.md",
    "CONGRESS-TRADE-EFFORT-LOG.md": APPS / "CONGRESS-TRADE-EFFORT-LOG.md",
    "CONGRESS-SHARED-EFFORT-LOG.md": APPS / "CONGRESS-SHARED-EFFORT-LOG.md",
    "API-USAGE-MONITOR-EFFORT-LOG.md": APPS / "API-USAGE-MONITOR-EFFORT-LOG.md",
    "DEALDEX-EFFORT-LOG.md": APPS / "DEALDEX-EFFORT-LOG.md",
    "PERSONAL-SITE-EFFORT-LOG.md": APPS / "PERSONAL-SITE-EFFORT-LOG.md",
    "SOCRATIC-TRADE-EFFORT-LOG.md": APPS / "SOCRATIC-TRADE-EFFORT-LOG.md",
    "FLEET-INFRA-EFFORT-LOG.md": APPS / "FLEET-INFRA-EFFORT-LOG.md",
    "AUTOROTATE-EFFORT-LOG.md": APPS / "AUTOROTATE-EFFORT-LOG.md",
    "CONTACTLOGO-EFFORT-LOG.md": APPS / "CONTACTLOGO-EFFORT-LOG.md",
    "BOTFLEET-EFFORT-LOG.md": APPS / "BOTFLEET-EFFORT-LOG.md",
    "FLEET-OPS-EFFORT-LOG.md": APPS / "FLEET-OPS-EFFORT-LOG.md",
    "EFFORT-LOG-PROTOCOL.md": APPS / "EFFORT-LOG-PROTOCOL.md",
    "AGENT-SYNC.md": APPS / "AGENT-SYNC.md",
    "MAC-LOCAL-PROCESSES.md": APPS / "MAC-LOCAL-PROCESSES.md",
}
APP_CANONICAL: dict[str, str] = {
    "socratic-trade": "socratic-trade",
    "socratic.trade": "socratic-trade",
    "socratic trade": "socratic-trade",
    "st": "socratic-trade",
    "trading": "socratic-trade",
    "congress-trade": "congress-trade",
    "congress.trade": "congress-trade",
    "congress trade": "congress-trade",
    "ct": "congress-trade",
    "usage-monitor": "usage-monitor",
    "usage monitor": "usage-monitor",
    "um": "usage-monitor",
    "api-usage-monitor": "usage-monitor",
    "congress-trading-shared": "congress-trading-shared",
    "congress-shared": "congress-trading-shared",
    "cts": "congress-trading-shared",
    "shared": "congress-trading-shared",
    "shared dependency": "congress-trading-shared",
    "dealdex": "dealdex",
    "dealdex.net": "dealdex",
    "deal dex": "dealdex",
    "dd": "dealdex",
    "personal-site": "personal-site",
    "personal site": "personal-site",
    "jays.services": "personal-site",
    "ps": "personal-site",
    "autorotate": "autorotate",
    "autorotate.codes": "autorotate",
    "ar": "autorotate",
    "contactlogo": "contactlogo",
    "contact-logo": "contactlogo",
    "contactlogo.com": "contactlogo",
    "cl": "contactlogo",
    "fleet-infra": "fleet-infra",
    # Keyed lowercase: normalize_app() lowercases input before lookup.
    "ai-fleet-coordinator": "fleet-infra",
    "ai fleet coordinator": "fleet-infra",
    "fleet": "fleet-infra",
    "afc": "fleet-infra",
    "afl": "fleet-infra",
    "botfleet": "botfleet",
    "botfleet.app": "botfleet",
    "bf": "botfleet",
    "fleet-ops": "fleet-ops",
    "fleet ops": "fleet-ops",
    "ops": "fleet-ops",
    "fleetlink": "fleetlink",
    "fleetlink.online": "fleetlink",
    "fleet link": "fleetlink",
    "fl": "fleetlink",
}


def normalize_app(app: str) -> str:
    if not app:
        return ""
    low = app.strip().lower()
    return APP_CANONICAL.get(low, APP_CANONICAL.get(app.strip(), low))


HANDOFF_NAMES_FILE = HOME / ".secrets" / "global-api-keys"
AUDIT_LOG = Path(__file__).resolve().parent / "audit.log"
AUTH_FAIL_WINDOW_S = 60.0
AUTH_FAIL_MAX = 20
_AUTH_FAILS: dict[str, list[float]] = collections.defaultdict(list)
LOGIN_MAX_BODY = 16 * 1024

SEVERITIES = ("P0", "P1", "P2", "P3", "P4")
STATUSES = ("open", "in_progress", "completed", "deployed", "addressed", "wontfix", "duplicate")
WRITEBACK_GRACE_S = 900
OPEN_STATUSES = ("open", "in_progress")
# GitHub Issues are only open/closed.  Board refinements in the same GH state
# must survive inbound sync (a claim is in_progress; GH still reports OPEN).
GH_OPEN_STATUSES = frozenset(("open", "in_progress"))
GH_CLOSED_STATUSES = frozenset(
    ("completed", "deployed", "addressed", "wontfix", "duplicate")
)
SOURCE_KINDS = ("review-finding", "effort-row", "github-issue", "agent-report")
ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
FINDING_ID_ROUTE = r"^/findings/([a-f0-9]{8,32})$"
FINDING_COMMENTS_ROUTE = r"^/findings/([a-f0-9]{8,32})/comments$"

# Agent seat logos, same marks the fleet daily digest uses. Inlined as data
# URIs so /board stays a single self-contained response (no extra auth-gated
# subresource fetches). Seat -> logo slug mirrors the digest's AGENT_LOGO map;
# Monet/Renoir/Fable collapse onto the Claude mark.
LOGO_DIR = HOME / "Code" / "AI-Fleet-Coordinator" / "agent-logos"

# Seat -> how it should be shown. "slug" picks the logo file; "label" is the
# name the seat goes BY (which is not always the underlying model); "note"
# explains the difference on hover; "with" adds companion marks for seats that
# never really work alone.
#   - Monet/Renoir/Fable are Claude instances but keep their own names.
#   - GROK BOT is its own seat and a distinct product from Grok's own chats:
#     it is a cloud app, ALWAYS cloud, and it is the one that coordinates and
#     implements work through Cursor cloud agents.  Plain GROK runs on the Mac.
#     So CURSOR renders the Cursor mark next to Grok Bot's (Cursor work is Grok
#     Bot driving it), while a bare GROK tag stays just Grok, on the Mac.
# "env" here is the seat's DEFAULT environment, used only when an item does not
# carry an explicit one -- an explicit --env always wins.
AGENT_SEATS = {
    "GROK":        {"slug": "grok",   "label": "Grok", "env": "Mac",
                    "note": "Grok — the Grok mark, not xAI's; runs on the Mac "
                            "(Grok Bot is the separate cloud seat)"},
    "GROK BOT":    {"slug": "grok-bot", "label": "Grok Bot", "env": "cloud",
                    "note": "Grok Bot — cloud app, always cloud; coordinates/implements "
                            "via Cursor cloud agents (a distinct seat from Grok's own "
                            "Mac chats)"},
    "GROK-BOT":    {"slug": "grok-bot", "label": "Grok Bot", "env": "cloud",
                    "note": "Grok Bot — cloud app, always cloud; coordinates/implements "
                            "via Cursor cloud agents (a distinct seat from Grok's own "
                            "Mac chats)"},
    "GROKBOT":     {"slug": "grok-bot", "label": "Grok Bot", "env": "cloud",
                    "note": "Grok Bot — cloud app, always cloud; coordinates/implements "
                            "via Cursor cloud agents (a distinct seat from Grok's own "
                            "Mac chats)"},
    "CODEX":       {"slug": "codex",  "label": "Codex"},
    "CLAUDE":      {"slug": "claude", "label": "Claude", "env": "Mac"},
    "CURSOR":      {"slug": "cursor", "label": "Cursor", "env": "cloud",
                    "note": "Cursor cloud agents — driven by Grok Bot",
                    "with": ["GROK BOT"]},
    "AG":          {"slug": "ag",     "label": "Antigravity", "env": "Mac"},
    "ANTIGRAVITY": {"slug": "ag",     "label": "Antigravity", "env": "Mac"},
    "GEMINI":      {"slug": "gemini", "label": "Gemini"},
    "MONET":       {"slug": "claude", "label": "Monet",  "env": "Mac",
                    "note": "Monet — a Claude instance, on the Mac"},
    "RENOIR":      {"slug": "claude", "label": "Renoir", "env": "Mac",
                    "note": "Renoir — a Claude instance, on the Mac"},
    "FABLE":       {"slug": "claude", "label": "Fable",  "env": "Mac",
                    "note": "Fable — a Claude instance, on the Mac"},
}

# Where the agent is running. Deliberately just two values (owner, 2026-08-19):
# the useful signal is "is this seat on the Mac or in the cloud", not which
# exact client it is -- the seat chip already says who.
AGENT_ENVS = ("Mac", "cloud")


def load_agent_logos() -> dict:
    """slug -> data: URI. SVG preferred, PNG accepted (Grok Bot ships as PNG).
    Missing files are omitted; the chip falls back to a text badge."""
    out = {}
    for meta in AGENT_SEATS.values():
        slug = meta["slug"]
        if slug in out:
            continue
        for ext, mime in (("svg", "image/svg+xml"), ("png", "image/png")):
            try:
                raw = (LOGO_DIR / f"{slug}.{ext}").read_bytes()
            except OSError:
                continue
            out[slug] = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
            break
    return out


AGENT_LOGOS = load_agent_logos()


def _parse_token_assignments(lines) -> dict:
    """Parse MAC_COLLAB_TOKEN*= lines into {token: identity}.  Identity is
    None for the root token and the suffix for MAC_COLLAB_TOKEN_X."""
    tokens = {}
    for line in lines:
        s = line.strip()
        if s.startswith("export "):
            s = s[7:]
        if s.startswith("MAC_COLLAB_TOKEN"):
            parts = s.split("=", 1)
            if len(parts) == 2:
                k = parts[0].strip()
                v = parts[1].strip().strip('"').strip("'")
                if v:
                    identity = k[len("MAC_COLLAB_TOKEN_"):] if k != "MAC_COLLAB_TOKEN" else None
                    tokens[v] = identity
    return tokens


def _env_tokens() -> dict:
    tokens = {}
    for k, v in os.environ.items():
        if k.startswith("MAC_COLLAB_TOKEN") and v.strip():
            identity = k[len("MAC_COLLAB_TOKEN_"):] if k != "MAC_COLLAB_TOKEN" else None
            tokens[v.strip()] = identity
    return tokens


def _file_tokens() -> dict:
    if not SECRETS.is_file():
        return {}
    try:
        return _parse_token_assignments(SECRETS.read_text().splitlines())
    except OSError:
        return {}


# Logged at most once per process so a rotated file is a restart signal,
# not a line of noise on every request.
_LOGGED_FILE_WINS = False

AUTH_FAIL_HINT = (
    "token rejected.  If MAC_COLLAB_TOKEN was rotated, unset the env var "
    "(file ~/.secrets/mac-collab.env is canonical) and restart long-running "
    "processes that cached it at start.  mac-collab itself: "
    "pm2 restart mac-collab --update-env"
)


def load_tokens() -> dict:
    """Return {token: identity}.

    ~/.secrets/mac-collab.env is canonical when it has any MAC_COLLAB_TOKEN*
    assignment, so rotating the file takes effect on the next request without
    a server restart.  Process-env tokens are used only when the file is
    missing (cloud / test).  Never cache this at import -- that was the
    029f6346 silent-401 after rotation.
    """
    global _LOGGED_FILE_WINS
    file_tokens = _file_tokens()
    env_tokens = _env_tokens()
    if file_tokens:
        if env_tokens and not _LOGGED_FILE_WINS:
            _LOGGED_FILE_WINS = True
            mtime = None
            try:
                mtime = int(SECRETS.stat().st_mtime)
            except OSError:
                pass
            print(
                "mac-collab: ~/.secrets/mac-collab.env is canonical; "
                "ignoring process-env MAC_COLLAB_TOKEN* "
                "(unset the env var or restart after rotation).  "
                "mtime=%s pid=%d"
                % (mtime, os.getpid()),
                flush=True,
            )
        return file_tokens
    return env_tokens


def token_auth_meta() -> dict:
    """Staleness signal for /health.  Never includes token values."""
    mtime = None
    try:
        if SECRETS.is_file():
            mtime = int(SECRETS.stat().st_mtime)
    except OSError:
        mtime = None
    file_tokens = _file_tokens()
    env_tokens = _env_tokens()
    if file_tokens:
        source = "file"
        count = len(file_tokens)
    elif env_tokens:
        source = "env"
        count = len(env_tokens)
    else:
        source = "missing"
        count = 0
    return {
        "token_source": source,
        "token_count": count,
        "secrets_mtime": mtime,
        "secrets_newer_than_process": bool(mtime and mtime > STARTED),
        "pid": os.getpid(),
        "restart_hint": "pm2 restart mac-collab --update-env",
    }


# Browser session cookie so THE BOARD does not ask for MAC_COLLAB_TOKEN on
# every load.  HMAC key is derived from the live tokens (rotating them
# invalidates cookies).  The cookie value is never the collab token itself.
SESSION_COOKIE = "mac_collab_session"
SESSION_MAX_AGE = 30 * 24 * 3600


def get_session_key() -> bytes:
    tokens = load_tokens()
    if tokens:
        return hashlib.sha256(
            b"mac-collab-session-v1\0" + b"\0".join(sorted(t.encode("utf-8") for t in tokens))
        ).digest()
    return hashlib.sha256(b"mac-collab-session-v1-empty").digest()



def token_matches(got: str, want: str) -> bool:
    if not got or not want:
        return False
    left = got.encode("utf-8")
    right = want.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    cf = handler.headers.get("CF-Connecting-IP", "").strip()
    if cf:
        return cf
    xff = handler.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if xff:
        return xff
    return handler.client_address[0]


def auth_rate_limited(ip: str) -> bool:
    now = time.time()
    window = [t for t in _AUTH_FAILS[ip] if now - t < AUTH_FAIL_WINDOW_S]
    _AUTH_FAILS[ip] = window
    return len(window) >= AUTH_FAIL_MAX


def note_auth_fail(ip: str) -> None:
    _AUTH_FAILS[ip].append(time.time())


def audit(handler: BaseHTTPRequestHandler, action: str, name: str = "", ok: bool = True) -> None:
    line = "%s ip=%s action=%s name=%s ok=%s path=%s\n" % (
        now_iso(),
        client_ip(handler),
        action,
        name,
        "1" if ok else "0",
        handler.path.split("?", 1)[0],
    )
    try:
        AUDIT_LOG.open("a", encoding="utf-8").write(line)
    except OSError:
        pass


def key_names_only() -> list[str]:
    """Secret names from the Mac handoff file. Never returns values."""
    if not HANDOFF_NAMES_FILE.is_file():
        return []
    names: list[str] = []
    for raw in HANDOFF_NAMES_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if s.startswith("export "):
            s = s[7:].strip()
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", s)
        if m:
            names.append(m.group(1))
    return names


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def within_writeback_grace(writeback_at: str | None, now: datetime | None = None) -> bool:
    if not writeback_at:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        wb_dt = datetime.fromisoformat(writeback_at)
        if wb_dt.tzinfo is None:
            wb_dt = wb_dt.replace(tzinfo=timezone.utc)
        return now - wb_dt < timedelta(seconds=WRITEBACK_GRACE_S)
    except (ValueError, TypeError):
        return False


def same_github_state(left: str, right: str) -> bool:
    if left == right:
        return True
    if left in GH_OPEN_STATUSES and right in GH_OPEN_STATUSES:
        return True
    if left in GH_CLOSED_STATUSES and right in GH_CLOSED_STATUSES:
        return True
    return False


def resolve_upsert_status(
    existing_status: str,
    incoming_status: str,
    status_in_payload: bool,
    source_kind: str,
    writeback_at: str | None,
    now: datetime | None = None,
) -> str:
    """Status to keep after a sync POST.

    Board PATCH is the write.  Inbound sync must not clobber it when (1) the
    15-min writeback_at grace is still open, or (2) the finding is a GitHub
    issue and the incoming status is only GH's coarser open/closed mapping.
    """
    if not status_in_payload:
        return existing_status
    if within_writeback_grace(writeback_at, now):
        return existing_status
    if source_kind == "github-issue" and same_github_state(
        existing_status, incoming_status
    ):
        return existing_status
    return incoming_status


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def resolve_finding_id(conn: sqlite3.Connection, ident: str) -> tuple[str | None, str | None]:
    """Map a unique 8–32 hex prefix to the full finding id.

    `board` prints 8-char prefixes; the CLI used to 404 unless the caller
    pasted the full 32-char id. Returns (id, None) on a unique match, or
    (None, 'not_found' | 'ambiguous_id').
    """
    ident = (ident or "").strip().lower()
    if not ID_RE.fullmatch(ident):
        return None, "not_found"
    if len(ident) == 32:
        row = conn.execute("SELECT id FROM findings WHERE id = ?", (ident,)).fetchone()
        return (ident, None) if row else (None, "not_found")
    rows = conn.execute(
        "SELECT id FROM findings WHERE id LIKE ?", (ident + "%",)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"], None
    if not rows:
        return None, "not_found"
    return None, "ambiguous_id"


def init_db() -> None:
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS findings (
                id TEXT PRIMARY KEY,
                app TEXT NOT NULL,
                external_uid TEXT,
                source TEXT,
                title TEXT NOT NULL,
                severity TEXT,
                category TEXT,
                surface TEXT,
                description TEXT,
                recommended_fix TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                addressed_by TEXT,
                resolution TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(app, external_uid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                finding_id TEXT NOT NULL REFERENCES findings(id),
                author TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Migrate older DBs (pre source_kind/source_url/repo columns) in place.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(findings)")}
        if "source_kind" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'review-finding'")
        if "source_url" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN source_url TEXT")
        if "repo" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN repo TEXT")
        if "reported_by" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN reported_by TEXT")
        # Where the work is happening: worktree path, branch, host -- free text,
        # e.g. "~/apps/trading-claude @ claude/fix-stops" or "cloud (Codex)".
        if "location" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN location TEXT")
        # Interface the agent is running in: "Mac desktop", "Mac terminal",
        # "cloud", "Cursor cloud", ... (see AGENT_ENVS; free text allowed).
        if "env" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN env TEXT")
        # Timestamp set by write_back.py after a successful board→file reverse-sync.
        # sync_board.py reads this to skip status overwrites within a grace window,
        # preventing oscillation when the board is ahead of the effort-log file.
        if "writeback_at" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN writeback_at TEXT")
        if "claimed_at" not in existing_cols:
            conn.execute("ALTER TABLE findings ADD COLUMN claimed_at TEXT")

        comment_cols = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
        if "location" not in comment_cols:
            conn.execute("ALTER TABLE comments ADD COLUMN location TEXT")
        if "env" not in comment_cols:
            conn.execute("ALTER TABLE comments ADD COLUMN env TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_app ON findings(app)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_source_kind ON findings(source_kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_finding ON comments(finding_id)")
        conn.commit()
    finally:
        conn.close()


def file_meta(name: str, path: Path) -> dict:
    try:
        st = path.stat()
        return {
            "name": name,
            "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "exists": True,
        }
    except OSError:
        return {"name": name, "exists": False}


def findings_open_by_app() -> dict:
    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        rows = conn.execute(
            f"SELECT app, COUNT(*) AS n FROM findings WHERE status IN ({placeholders}) GROUP BY app",
            OPEN_STATUSES,
        ).fetchall()
        return {r["app"]: r["n"] for r in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def authorized(handler: BaseHTTPRequestHandler, tokens: dict | None = None):
    '''Returns the identity (str) if authorized, else None.

    Pass a tokens snapshot to keep one request on a single load_tokens()
    result.  Cookie HMAC still uses live tokens via get_session_key --
    rotating the file is supposed to invalidate cookies.
    '''
    if tokens is None:
        tokens = load_tokens()
    if not tokens:
        return None
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token_val = auth[7:].strip()
        for t, ident in tokens.items():
            if token_matches(token_val, t):
                return ident or "OWNER"
    if auth.lower().startswith("basic "):
        ident = basic_authorized(handler, tokens)
        if ident:
            return ident
    ident = cookie_authorized(handler)
    if ident:
        return ident
    return None


def _session_ident(ident: str | None) -> str:
    return ident or "OWNER"


def mint_session_value(ident: str | None, now: float | None = None) -> str:
    ident_s = _session_ident(ident)
    exp = int((now if now is not None else time.time()) + SESSION_MAX_AGE)
    payload = f"{ident_s}:{exp}"
    sig = hmac.new(get_session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{sig}.{ident_s}.{exp}"


def parse_session_value(val: str, now: float | None = None) -> str | None:
    if not val or val.count(".") < 2:
        return None
    sig, ident_s, exp_s = val.split(".", 2)
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(now if now is not None else time.time()):
        return None
    if not ident_s or any(c in ident_s for c in " \r\n"):
        return None
    payload = f"{ident_s}:{exp}"
    expect = hmac.new(get_session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    return ident_s


def cookie_authorized(handler: BaseHTTPRequestHandler) -> str | None:
    raw = handler.headers.get("Cookie", "")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(SESSION_COOKIE)
    if morsel is None:
        return None
    return parse_session_value(morsel.value)


def session_cookie_header(handler: BaseHTTPRequestHandler, ident: str | None) -> str:
    value = mint_session_value(ident)
    parts = [
        f"{SESSION_COOKIE}={value}",
        "Path=/",
        f"Max-Age={SESSION_MAX_AGE}",
        "HttpOnly",
        "SameSite=Lax",
    ]
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    host = (handler.headers.get("Host") or "").split(":")[0].lower()
    if proto == "https" or host.endswith("jays.services"):
        parts.append("Secure")
    return "; ".join(parts)


def session_headers(handler: BaseHTTPRequestHandler, ident: str | None) -> dict[str, str]:
    return {"Set-Cookie": session_cookie_header(handler, ident)}


def basic_authorized(handler: BaseHTTPRequestHandler, tokens: dict | None = None):
    """Gate the /board page itself (not just its data fetches). Username is
    ignored; password is checked against any MAC_COLLAB_TOKEN. Native
    browser login dialog via 401 + WWW-Authenticate."""
    tokens = tokens if tokens is not None else load_tokens()
    if not tokens:
        return None
    auth = handler.headers.get("Authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
    except Exception:
        return None
    _, _, password = decoded.partition(":")
    for t, ident in tokens.items():
        if token_matches(password, t):
            return ident or "OWNER"
    return None


def finding_row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def comment_row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


class Handler(BaseHTTPRequestHandler):
    server_version = "mac-collab/2.0"

    def log_message(self, fmt, *args):
        # Keep pm2 logs short; never log tokens or paths beyond the request line.
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body, content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, indent=2).encode("utf-8")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _require_basic_auth(self):
        return self._send(
            401,
            BOARD_401_HTML,
            "text/html; charset=utf-8",
            extra_headers={"WWW-Authenticate": 'Basic realm="Fleet Findings Board"'},
        )

    # ---- /login: an HTML form for browsers whose password manager cannot fill
    # the native Basic dialog (browser-only seats such as Instinct).  Same
    # tokens, same identity mapping, same 30-day HttpOnly session cookie as the
    # Basic path.  The submitted value is never echoed, logged, or audited.

    def _handle_login_page(self, status: int = 200, error: str = ""):
        slot = '<p class="err">%s</p>' % html.escape(error) if error else ""
        return self._send(status, LOGIN_HTML.replace("__ERROR__", slot), "text/html; charset=utf-8")

    def _handle_login_submit(self):
        ip = client_ip(self)
        if auth_rate_limited(ip):
            return self._handle_login_page(429, "Too many failed attempts.  Try again in a minute.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            return self._handle_login_page(415, "Submit the form as application/x-www-form-urlencoded.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > LOGIN_MAX_BODY:
            return self._handle_login_page(400, "Missing or oversized form body.")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        submitted = (parse_qs(raw, keep_blank_values=True).get("token") or [""])[0].strip()
        ident_hit = None
        for t, ident in load_tokens().items():
            if token_matches(submitted, t):
                ident_hit = ident or "OWNER"
                break
        if ident_hit is None:
            note_auth_fail(ip)
            audit(self, "login", ok=False)
            return self._handle_login_page(401, "Token not recognized.")
        audit(self, "login", name=ident_hit, ok=True)
        return self._send(
            303,
            "",
            "text/plain; charset=utf-8",
            extra_headers={
                "Location": "/board",
                "Set-Cookie": session_cookie_header(self, ident_hit),
            },
        )

    def _read_json_body(self) -> tuple[dict | None, str | None]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "bad_content_length"
        if length <= 0 or length > 1_000_000:
            return None, "missing_or_oversized_body"
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "invalid_json"
        if not isinstance(data, dict):
            return None, "expected_json_object"
        return data, None

    # ---- routing ---------------------------------------------------

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        if path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/board":
            ident = basic_authorized(self) or cookie_authorized(self)
            if not ident:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Fleet Findings Board"')
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", session_cookie_header(self, ident))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/health"):
            return self._handle_health()
        if path == "/files/key-names":
            return self._handle_key_names()
        if path == "/login":
            return self._handle_login_page()
        if path == "/board":
            ident = basic_authorized(self) or cookie_authorized(self)
            if not ident:
                return self._require_basic_auth()
            page = BOARD_HTML.replace("__AGENT_LOGOS_JSON__", json.dumps(AGENT_LOGOS))
            page = page.replace("__AGENT_SEATS_JSON__", json.dumps(AGENT_SEATS))
            page = page.replace("__AGENT_ENVS_JSON__", json.dumps(AGENT_ENVS))
            return self._send(
                200,
                page,
                "text/html; charset=utf-8",
                extra_headers=session_headers(self, ident),
            )
        if path in ("/files", "/effort-logs"):
            return self._handle_files_list()
        if path.startswith("/files/") or path.startswith("/effort-logs/"):
            return self._handle_file_get(path)
        if path == "/findings":
            return self._handle_findings_list(query)
        if path == "/findings/stats":
            return self._handle_findings_stats()
        m = re.match(FINDING_COMMENTS_ROUTE, path)
        if m:
            return self._handle_comments_list(m.group(1))
        m = re.match(FINDING_ID_ROUTE, path)
        if m:
            return self._handle_finding_get(m.group(1))
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        if path == "/login":
            return self._handle_login_submit()
        if path == "/findings":
            return self._handle_finding_create()
        m = re.match(FINDING_COMMENTS_ROUTE, path)
        if m:
            return self._handle_comment_create(m.group(1))
        return self._send(404, {"error": "not_found"})

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        m = re.match(FINDING_ID_ROUTE, path)
        if m:
            return self._handle_finding_update(m.group(1))
        return self._send(404, {"error": "not_found"})

    # ---- existing file endpoints ------------------------------------

    def _handle_health(self):
        files_ok = sum(1 for p in ALLOW.values() if p.is_file())
        tokens = load_tokens()
        token = next((t for t, ident in tokens.items() if ident is None), None)
        body = {
            "status": "ok" if token and files_ok else "degraded",
            "service": "mac-collab",
            "host": "mac.jays.services",
            "uptime_s": int(time.time() - STARTED),
        }
        # Anonymous callers (uptime monitors etc.) get bare status only.
        # Filenames and finding counts are reconnaissance — don't hand them
        # out for free.
        if authorized(self, tokens=tokens):
            body.update({
                "token_configured": bool(token),
                "allowlist": sorted(ALLOW),
                "files_present": files_ok,
                "findings_open_by_app": findings_open_by_app(),
                "auth": "Authorization: Bearer <MAC_COLLAB_TOKEN>",
            })
            body.update(token_auth_meta())
        return self._send(200, body)

    def _deny_auth(self):
        ip = client_ip(self)
        if auth_rate_limited(ip):
            return self._send(429, {"error": "too_many_auth_failures"})
        note_auth_fail(ip)
        return self._send(401, {"error": "unauthorized", "hint": AUTH_FAIL_HINT})

    def _handle_files_list(self):
        if not authorized(self):
            return self._deny_auth()
        return self._send(200, {
            "files": [file_meta(n, p) for n, p in sorted(ALLOW.items())],
        })

    def _handle_key_names(self):
        if not authorized(self):
            return self._deny_auth()
        names = key_names_only()
        audit(self, "key-names", ok=True)
        return self._send(200, {"names": names, "count": len(names)})

    def _handle_file_get(self, path: str):
        if not authorized(self):
            return self._deny_auth()
        name = path.split("/", 2)[2]
        if name in ("global-api-keys", "key-names"):
            audit(self, "file-denied-secret", name, ok=False)
            return self._send(404, {"error": "not_on_allowlist", "hint": "use GET /files/key-names"})
        if name not in ALLOW or "/" in name or ".." in name:
            audit(self, "file-miss", name, ok=False)
            return self._send(404, {"error": "not_on_allowlist"})
        target = ALLOW[name]
        if not target.is_file():
            return self._send(404, {"error": "missing"})
        audit(self, "file-get", name, ok=True)
        text = target.read_text(encoding="utf-8", errors="replace")
        return self._send(200, text, "text/markdown; charset=utf-8")

    # ---- findings endpoints ------------------------------------------

    def _handle_findings_list(self, query: dict):
        if not authorized(self):
            return self._deny_auth()
        clauses = []
        params: list = []
        if query.get("app"):
            clauses.append("app = ?")
            params.append(normalize_app(query["app"][0]))
        if query.get("status"):
            statuses = [s for s in query["status"][0].split(",") if s]
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if query.get("severity"):
            sevs = [s for s in query["severity"][0].split(",") if s]
            clauses.append(f"severity IN ({','.join('?' for _ in sevs)})")
            params.extend(sevs)
        if query.get("surface"):
            clauses.append("surface = ?")
            params.append(query["surface"][0])
        if query.get("source_kind"):
            kinds = [s for s in query["source_kind"][0].split(",") if s]
            clauses.append(f"source_kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if query.get("repo"):
            clauses.append("repo = ?")
            params.append(query["repo"][0])
        if query.get("search"):
            clauses.append("title LIKE ?")
            params.append("%" + query["search"][0].replace("%", "").replace("_", "") + "%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            limit = min(int(query.get("limit", ["2000"])[0]), 5000)
        except ValueError:
            limit = 2000
        sql = (
            f"SELECT * FROM findings {where} "
            "ORDER BY "
            "CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 "
            "WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END ASC, "
            "created_at DESC "
            "LIMIT ?"
        )
        conn = get_conn()
        try:
            rows = conn.execute(sql, [*params, limit]).fetchall()
            total = conn.execute(f"SELECT COUNT(*) AS n FROM findings {where}", params).fetchone()["n"]
        finally:
            conn.close()
        return self._send(200, {
            "findings": [finding_row_to_dict(r) for r in rows],
            "count": len(rows),
            "total_matching": total,
        })

    def _handle_findings_stats(self):
        if not authorized(self):
            return self._deny_auth()
        conn = get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"]
            open_n = conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE status IN ('open','in_progress')"
            ).fetchone()["n"]
            p0p1_open = conn.execute(
                "SELECT COUNT(*) AS n FROM findings "
                "WHERE status IN ('open','in_progress') AND severity IN ('P0','P1')"
            ).fetchone()["n"]
            done = conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE status IN ('completed','deployed','addressed')"
            ).fetchone()["n"]
            by_kind = {
                r["source_kind"]: r["n"]
                for r in conn.execute("SELECT source_kind, COUNT(*) AS n FROM findings GROUP BY source_kind")
            }
            apps = [r["app"] for r in conn.execute("SELECT DISTINCT app FROM findings ORDER BY app")]
        finally:
            conn.close()
        return self._send(200, {
            "total": total,
            "open": open_n,
            "p0p1_open": p0p1_open,
            "done": done,
            "by_kind": by_kind,
            "apps": apps,
        })

    def _resolved_finding_id(self, ident: str, conn: sqlite3.Connection) -> str | None:
        fid, err = resolve_finding_id(conn, ident)
        if err == "ambiguous_id":
            self._send(409, {"error": "ambiguous_id", "hint": "pass a longer id prefix"})
            return None
        if not fid:
            self._send(404, {"error": "not_found"})
            return None
        return fid

    def _handle_finding_get(self, finding_id: str):
        if not authorized(self):
            return self._deny_auth()
        conn = get_conn()
        try:
            finding_id = self._resolved_finding_id(finding_id, conn)
            if not finding_id:
                return
            row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
            comments = conn.execute(
                "SELECT * FROM comments WHERE finding_id = ? ORDER BY created_at ASC", (finding_id,)
            ).fetchall()
        finally:
            conn.close()
        result = finding_row_to_dict(row)
        result["comments"] = [comment_row_to_dict(c) for c in comments]
        return self._send(200, result)

    def _handle_finding_create(self):
        if not authorized(self):
            return self._deny_auth()
        data, err = self._read_json_body()
        if err:
            return self._send(400, {"error": err})
        app = normalize_app(str(data.get("app", "")).strip())
        title = str(data.get("title", "")).strip()
        if not app or not title:
            return self._send(400, {"error": "app_and_title_required"})
        severity = data.get("severity")
        if severity is not None and severity not in SEVERITIES:
            return self._send(400, {"error": "invalid_severity", "allowed": SEVERITIES})
        incoming_status = data.get("status")
        if incoming_status is None:
            incoming_status = "open"
        elif incoming_status not in STATUSES:
            return self._send(400, {"error": "invalid_status", "allowed": STATUSES})
        status_in_payload = "status" in data
        external_uid = data.get("external_uid")
        source_kind = data.get("source_kind", "review-finding")
        if source_kind not in SOURCE_KINDS:
            return self._send(400, {"error": "invalid_source_kind", "allowed": SOURCE_KINDS})
        source_url = data.get("source_url")
        repo = data.get("repo")
        reported_by = data.get("reported_by")
        location = data.get("location")
        env = data.get("env")
        addressed_by = data.get("addressed_by")
        resolution = data.get("resolution")
        ts = now_iso()

        conn = get_conn()
        try:
            existing = None
            if external_uid:
                existing = conn.execute(
                    "SELECT * FROM findings WHERE app = ? AND external_uid = ?", (app, external_uid)
                ).fetchone()
            if existing:
                finding_id = existing["id"]
                wb = existing["writeback_at"] if "writeback_at" in existing.keys() else None
                new_status = resolve_upsert_status(
                    existing["status"],
                    incoming_status,
                    status_in_payload,
                    source_kind,
                    wb,
                )

                new_source = data.get("source")
                new_severity = severity
                new_category = data.get("category")
                new_surface = data.get("surface")
                new_description = data.get("description")
                new_fix = data.get("recommended_fix")
                unchanged = (
                    existing["source"] == new_source
                    and existing["title"] == title
                    and existing["severity"] == new_severity
                    and existing["category"] == new_category
                    and existing["surface"] == new_surface
                    and existing["description"] == new_description
                    and existing["recommended_fix"] == new_fix
                    and existing["status"] == new_status
                    and existing["source_kind"] == source_kind
                    and existing["source_url"] == source_url
                    and existing["repo"] == repo
                )
                if unchanged:
                    code = 200
                else:
                    conn.execute(
                        """
                        UPDATE findings SET source=?, title=?, severity=?, category=?, surface=?,
                            description=?, recommended_fix=?, status=?, source_kind=?, source_url=?,
                            repo=?, reported_by=COALESCE(?, reported_by), location=COALESCE(?, location), env=COALESCE(?, env),
                            addressed_by=COALESCE(?, addressed_by),
                            resolution=COALESCE(?, resolution), updated_at=?
                        WHERE id = ?
                        """,
                        (
                            new_source, title, new_severity, new_category, new_surface,
                            new_description, new_fix, new_status, source_kind,
                            source_url, repo, reported_by, location, env, addressed_by,
                            resolution, ts, finding_id,
                        ),
                    )
                    code = 200
            else:
                finding_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO findings
                        (id, app, external_uid, source, title, severity, category, surface,
                         description, recommended_fix, status, source_kind, source_url, repo,
                         reported_by, location, env, addressed_by, resolution,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id, app, external_uid, data.get("source"), title, severity,
                        data.get("category"), data.get("surface"), data.get("description"),
                        data.get("recommended_fix"), incoming_status, source_kind, source_url, repo,
                        reported_by, location, env, addressed_by, resolution, ts, ts,
                    ),
                )
                code = 201
            conn.commit()
            row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        finally:
            conn.close()
        return self._send(code, finding_row_to_dict(row))

    def _handle_finding_update(self, finding_id: str):
        ident = authorized(self)
        if not ident:
            return self._deny_auth()
        data, err = self._read_json_body()
        if err:
            return self._send(400, {"error": err})
        fields = {}
        if "status" in data:
            if data["status"] not in STATUSES:
                return self._send(400, {"error": "invalid_status", "allowed": STATUSES})
            fields["status"] = data["status"]
        if "addressed_by" in data:
            if ident != "OWNER" and data["addressed_by"] and data["addressed_by"] != ident:
                return self._send(403, {"error": "forbidden", "message": f"Token is scoped to '{ident}'"})
            fields["addressed_by"] = data["addressed_by"]
        if "reported_by" in data:
            if ident != "OWNER" and data["reported_by"] and data["reported_by"] != ident:
                return self._send(403, {"error": "forbidden", "message": f"Token is scoped to '{ident}'"})
            fields["reported_by"] = data["reported_by"]
        if "location" in data:
            fields["location"] = data["location"]
        if "env" in data:
            fields["env"] = data["env"]
        if "resolution" in data:
            fields["resolution"] = data["resolution"]
        if not fields:
            return self._send(400, {"error": "nothing_to_update"})
        fields["updated_at"] = now_iso()
        # Board PATCH is the write surface.  Stamp writeback_at so inbound
        # sync cannot revert status before write_back.py moves the copy.
        if "status" in fields:
            fields["writeback_at"] = fields["updated_at"]

        conn = get_conn()
        try:
            finding_id = self._resolved_finding_id(finding_id, conn)
            if not finding_id:
                return
            row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
            if not row:
                return self._send(404, {"error": "not_found"})

            if data.get("status") == "in_progress":
                from datetime import datetime, timezone
                now_dt = datetime.now(timezone.utc)
                if row["status"] == "in_progress" and row["claimed_at"]:
                    claimed_dt = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
                    if (now_dt - claimed_dt).total_seconds() < 20 * 60:
                        existing_agent = row["addressed_by"]
                        incoming_agent = data.get("addressed_by")
                        if existing_agent and incoming_agent and existing_agent != incoming_agent:
                            mins_left = str(int(20 - (now_dt - claimed_dt).total_seconds()/60))
                            msg = "Finding is already claimed by " + str(existing_agent) + ". Lease expires in " + mins_left + " minutes."
                            return self._send(409, {"error": "conflict", "message": msg})
                fields["claimed_at"] = now_iso()

            set_clause = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE findings SET {set_clause} WHERE id = ?", (*fields.values(), finding_id))
            conn.commit()
            row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        finally:
            conn.close()
        return self._send(200, finding_row_to_dict(row))

    def _handle_comments_list(self, finding_id: str):
        if not authorized(self):
            return self._deny_auth()
        conn = get_conn()
        try:
            finding_id = self._resolved_finding_id(finding_id, conn)
            if not finding_id:
                return
            rows = conn.execute(
                "SELECT * FROM comments WHERE finding_id = ? ORDER BY created_at ASC", (finding_id,)
            ).fetchall()
        finally:
            conn.close()
        return self._send(200, {"comments": [comment_row_to_dict(r) for r in rows]})

    def _handle_comment_create(self, finding_id: str):
        ident = authorized(self)
        if not ident:
            return self._deny_auth()
        data, err = self._read_json_body()
        if err:
            return self._send(400, {"error": err})
        author = str(data.get("author", "")).strip()
        if ident != "OWNER" and author and author != ident:
            return self._send(403, {"error": "forbidden", "message": f"Token is scoped to '{ident}'"})
        text = str(data.get("text", "")).strip()
        if not author or not text:
            return self._send(400, {"error": "author_and_text_required"})

        conn = get_conn()
        try:
            finding_id = self._resolved_finding_id(finding_id, conn)
            if not finding_id:
                return
            comment_id = uuid.uuid4().hex
            ts = now_iso()
            conn.execute(
                "INSERT INTO comments (id, finding_id, author, text, location, env, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (comment_id, finding_id, author, text, data.get("location"), data.get("env"), ts),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        finally:
            conn.close()
        return self._send(201, comment_row_to_dict(row))


BOARD_401_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Authorization Required</title></head>
<body style="font:15px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;color:#0f172a;margin:12vh auto;max-width:420px;padding:0 16px">
<p>Authorization required.  Use the browser's login dialog, or the form at <a href="/login">/login</a> if your browser or password manager cannot fill the dialog.</p>
</body></html>
"""

LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign In</title>
<style>
:root{--ink:#0f172a;--ink-3:#64748b;--line:#e2e8f0;--paper:#f8fafc;--card:#fff;--bad:#b91c1c}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:420px;margin:12vh auto;padding:0 16px}
form{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:18px 20px}
h1{font-size:18px;margin:0 0 12px;font-weight:640}
label{display:block;font-size:13px;color:var(--ink-3);margin:10px 0 4px}
input{width:100%;box-sizing:border-box;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:var(--paper)}
button{margin-top:14px;font:inherit;padding:8px 14px;border-radius:7px;border:1px solid var(--ink);background:var(--ink);color:#fff;cursor:pointer}
p{font-size:13px;color:var(--ink-3)}
.err{color:var(--bad)}
</style></head>
<body><main>
<form method="post" action="/login" autocomplete="on">
<h1>Fleet Findings Board</h1>
__ERROR__
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username" value="board">
<label for="token">Token</label>
<input id="token" name="token" type="password" autocomplete="current-password" required>
<button type="submit">Unlock</button>
<p>Same token as the browser login dialog.  After one unlock this browser stays signed in for 30 days.  Rotating the token file signs every browser out.</p>
</form>
</main></body></html>
"""

BOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Fleet Findings Board</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --paper:#eef2f1; --card:#ffffff; --sunk:#e4eae8;
  --ink:#0f1c1f; --ink-2:#3c4f52; --ink-3:#6a7c7f;
  --line:#d2dbd8; --line-soft:#e4eae7;
  --accent:#0f6b6f; --accent-2:#0b5457; --accent-wash:#e0efee;
  --p0:#7a1710; --p0-wash:#f6dfdb;
  --p1:#a8342a; --p1-wash:#f8e8e5;
  --p2:#8a5b12; --p2-wash:#f7eedd;
  --p3:#54666c; --p3-wash:#e9eeef;
  --p4:#7a8a8d; --p4-wash:#eef2f1;
  --ok:#0f6b4f; --ok-wash:#e2f2ec;
  --shadow:0 1px 2px rgba(15,28,31,.05), 0 10px 26px -20px rgba(15,28,31,.5);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0a1315; --card:#101d20; --sunk:#0d181a;
  --ink:#e7efee; --ink-2:#a7bab9; --ink-3:#7a9092;
  --line:#22383b; --line-soft:#182a2d;
  --accent:#54c1c0; --accent-2:#87d7d4; --accent-wash:#0f2c2e;
  --p0:#ff9a8a; --p0-wash:#301410;
  --p1:#ef8c7f; --p1-wash:#2b1512;
  --p2:#d9a559; --p2-wash:#241a0d;
  --p3:#8ea3a8; --p3-wash:#162326;
  --p4:#6d8285; --p4-wash:#101c1e;
  --ok:#5cc79a; --ok-wash:#0f261e;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 12px 30px -22px rgba(0,0,0,.9);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 96px}
code{font-family:var(--mono);font-size:.86em;background:var(--sunk);border:1px solid var(--line-soft);
  border-radius:4px;padding:.05em .32em;color:var(--ink-2);word-break:break-word}
a{color:var(--accent);text-underline-offset:2px}
header.top{border-bottom:1px solid var(--line);background:var(--card);margin-bottom:28px}
.top .wrap{padding-top:36px;padding-bottom:24px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);margin:0 0 10px}
h1{font-size:clamp(24px,3.6vw,34px);line-height:1.05;letter-spacing:-.02em;margin:0 0 8px;font-weight:640}
.standfirst{margin:0;max-width:64ch;color:var(--ink-2);font-size:15px}
.tape{display:flex;flex-wrap:wrap;gap:0;margin-top:22px;border:1px solid var(--line);
  border-radius:9px;overflow:hidden;background:var(--sunk)}
.tape div{flex:1 1 108px;padding:10px 14px;background:var(--card);border-right:1px solid var(--line-soft);cursor:pointer}
.tape div:hover{background:var(--accent-wash)}
.tape div:last-child{border-right:0}
.tape b{display:block;font-size:21px;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.2}
.tape span{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
.filters{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 18px;
  padding:11px 12px;background:var(--card);border:1px solid var(--line);border-radius:9px}
.filters .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);margin-right:2px}
select,input[type=search]{font:inherit;font-size:12.5px;padding:4px 8px;border-radius:7px;
  border:1px solid var(--line);background:var(--card);color:var(--ink)}
input[type=search]{width:16em}
button{font:inherit;font-size:12.5px;padding:4px 11px;border-radius:99px;cursor:pointer;
  border:1px solid var(--line);background:transparent;color:var(--ink-2);transition:background .12s,border-color .12s,color .12s}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--card)}
.sep{width:1px;height:18px;background:var(--line);margin:0 4px}
.count{font-family:var(--mono);font-size:11px;color:var(--ink-3);margin-left:auto}
.rows{display:flex;flex-direction:column;gap:8px}
details.row{background:var(--card);border:1px solid var(--line);border-radius:9px;box-shadow:var(--shadow);overflow:hidden}
details.row[data-sev="P0"]{border-left:3px solid var(--p0)}
details.row[data-sev="P1"]{border-left:3px solid var(--p1)}
details.row[data-sev="P2"]{border-left:3px solid var(--p2)}
details.row[data-sev="P3"]{border-left:3px solid var(--p3)}
details.row[data-sev="P4"]{border-left:3px solid var(--p4)}
details.row[data-sev=""]{border-left:3px solid var(--line)}
/* Flow layout, not grid: a long meta line or an unbreakable path used to
   fight the title for column width and squeeze it into a sliver. */
details.row summary{list-style:none;cursor:pointer;padding:12px 34px 12px 15px;position:relative}
details.row summary::-webkit-details-marker{display:none}
.key{font-family:var(--mono);font-size:11px;color:var(--accent);white-space:nowrap}
.sumhead{display:flex;align-items:baseline;gap:8px;flex-wrap:nowrap}
.sumhead .pill{flex:0 0 auto;align-self:flex-start;margin-top:1px}
.rtitle{font-weight:590;letter-spacing:-.01em;min-width:0;flex:1 1 auto;
  overflow-wrap:anywhere;text-wrap:pretty;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
details.row[open] .rtitle{-webkit-line-clamp:unset;overflow:visible}
.rmeta{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center;
  font-family:var(--mono);font-size:10.5px;color:var(--ink-3);margin-top:5px;
  overflow-wrap:anywhere}
.pill{padding:1px 7px;border-radius:99px;border:1px solid var(--line);font-size:10px;
  letter-spacing:.04em;text-transform:uppercase}
.pill.sev-P0{color:var(--p0);background:var(--p0-wash);border-color:transparent}
.pill.sev-P1{color:var(--p1);background:var(--p1-wash);border-color:transparent}
.pill.sev-P2{color:var(--p2);background:var(--p2-wash);border-color:transparent}
.pill.sev-P3{color:var(--p3);background:var(--p3-wash);border-color:transparent}
.pill.sev-P4{color:var(--p4);background:var(--p4-wash);border-color:transparent}
.pill.ok{color:var(--ok);background:var(--ok-wash);border-color:transparent}
.chev{position:absolute;right:13px;top:12px;font-family:var(--mono);color:var(--ink-3);
  font-size:13px;line-height:1}
.chev::after{content:"+"}
details.row[open] .chev{color:var(--accent)}
details.row[open] .chev::after{content:"\\2212"}
.body{padding:2px 15px 15px;border-top:1px solid var(--line-soft);display:flex;flex-direction:column;gap:11px}
.body p,.body pre{margin:0}
pre{white-space:pre-wrap;font-family:var(--sans);font-size:13.5px;color:var(--ink-2)}
.lab{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);display:block;margin-bottom:3px}
.fixbox{background:var(--accent-wash);border-radius:7px;padding:10px 12px}
.statusrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.comments{margin-top:2px;padding-left:14px;border-left:2px solid var(--line-soft);display:flex;flex-direction:column;gap:8px}
.comment{font-size:13px}
.comment b{color:var(--ink)}
.comment .when{font-family:var(--mono);font-size:10px;color:var(--ink-3);margin-left:6px}
.addc{display:flex;gap:6px;flex-wrap:wrap}
.addc input{flex:1 1 10em}

/* agent seat chips -- same white-tile marks the fleet daily digest uses */
.agents{display:inline-flex;align-items:center;gap:3px;vertical-align:middle}
.agent{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;
  border-radius:5px;border:1px solid var(--line);background:#fff;padding:1.5px;flex-shrink:0}
.agent img{width:100%;height:100%;object-fit:contain;display:block}
.agent.txt{font-family:var(--mono);font-size:8.5px;font-weight:700;color:#0f172a;letter-spacing:-.02em}

/* new-item composer */
#composer{background:var(--card);border:1px solid var(--line);border-radius:9px;
  padding:14px 15px;margin:0 0 12px;box-shadow:var(--shadow)}
#composer[hidden]{display:none}
#composer h2{font-size:15px;margin:0 0 10px;font-weight:640;letter-spacing:-.01em}
.cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:8px}
#composer input,#composer select,#composer textarea{font:inherit;font-size:13px;padding:6px 9px;
  border-radius:7px;border:1px solid var(--line);background:var(--paper);color:var(--ink);width:100%}
#composer textarea{min-height:64px;resize:vertical;font-family:var(--sans)}
.crow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.cmsg{font-family:var(--mono);font-size:11px;color:var(--ok)}
footer{border-top:1px solid var(--line);margin-top:30px;padding-top:16px;color:var(--ink-3);font-size:12.5px}
@media (max-width:640px){
  .top .wrap{padding-top:26px}
  details.row summary{grid-template-columns:1fr auto}
  .key{grid-column:1/-1}
}
</style></head>
<body>
<header class="top"><div class="wrap">
<p class="eyebrow">mac-collab &middot; fleet-wide &middot; live</p>
<h1>Fleet Findings Board</h1>
<p class="standfirst">Review findings, effort-board rows, and GitHub issues from every app in one place. Mark things addressed and comment on each other's fixes here.</p>
<div class="tape" id="tape"></div>
</div></header>
<div class="wrap">
<div class="filters" id="tokenBar" style="display:none">
  <span class="lbl">Token</span>
  <input type="password" id="token" placeholder="MAC_COLLAB_TOKEN" style="width:16em">
  <button class="primary" onclick="saveToken()">Unlock</button>
  <span class="count">Needed only if this browser has no saved board session.  After one unlock it stays signed in on this device for 30 days.  Password managers: <a href="/login">/login</a>.</span>
</div>
<div class="filters">
  <span class="lbl">App</span><select id="fApp"><option value="">all</option></select>
  <span class="sep"></span>
  <span class="lbl">Kind</span><select id="fKind">
    <option value="" selected>all</option>
    <option value="review-finding">review findings</option>
    <option value="effort-row">effort board</option>
    <option value="github-issue">GitHub issues</option>
  </select>
  <span class="sep"></span>
  <span class="lbl">Status</span><select id="fStatus">
    <option value="open,in_progress" selected>open + in progress</option>
    <option value="">all</option>
    <option value="completed,deployed,addressed">done</option>
    <option value="wontfix,duplicate">wontfix / duplicate</option>
  </select>
  <span class="sep"></span>
  <span class="lbl">Severity</span><select id="fSev">
    <option value="" selected>all</option>
    <option value="P0,P1,P2">P0-P2</option>
    <option value="P0,P1">P0-P1</option>
    <option value="P0">P0 only</option>
  </select>
  <span class="sep"></span>
  <input type="search" id="fSearch" placeholder="search title...">
  <button onclick="load()">Refresh</button>
  <button class="primary" onclick="toggleComposer()">+ New item</button>
  <span class="count" id="count"></span>
</div>
<div id="composer" hidden>
  <h2>File a finding or issue</h2>
  <div class="cgrid">
    <input id="nTitle" placeholder="Title (what's wrong / what needs doing)">
    <select id="nApp"></select>
    <select id="nSeverity">
      <option value="">no severity</option>
      <option value="P0">P0 &mdash; broken / blocking / money / security</option>
      <option value="P1">P1 &mdash; clearly wrong, hurting users</option>
      <option value="P2" selected>P2 &mdash; should fix</option>
      <option value="P3">P3 &mdash; polish</option>
      <option value="P4">P4 &mdash; idea / opportunity</option>
    </select>
    <input id="nReporter" placeholder="your seat/name (e.g. CLAUDE, GROK-BOT, Jay)">
    <select id="nEnv"></select>
    <input id="nLocation" placeholder="location (worktree @ branch, or host)">
  </div>
  <textarea id="nDescription" placeholder="Detail: what you saw, where (path:line), how to reproduce, why it matters."></textarea>
  <div class="crow">
    <button class="primary" onclick="createItem()">File it</button>
    <button onclick="toggleComposer()">Cancel</button>
    <span class="cmsg" id="nMsg"></span>
  </div>
</div>
<div class="rows" id="list">Loading&hellip;</div>
</div>
<footer><div class="wrap"><p>Any agent can file a finding (<code>POST /findings</code>), mark one addressed, or comment on a fix &mdash; see <code>AGENT-SYNC.md</code> &sect; Findings tool.</p></div></footer>
<script>
function tok(){
  try {
    const saved = localStorage.getItem('mac_collab_token') || sessionStorage.getItem('mac_collab_token') || '';
    if (saved && !localStorage.getItem('mac_collab_token')) localStorage.setItem('mac_collab_token', saved);
    return saved;
  } catch (e) { return sessionStorage.getItem('mac_collab_token') || ''; }
}
function saveToken(){
  const v = document.getElementById('token').value.trim();
  try { localStorage.setItem('mac_collab_token', v); } catch (e) {}
  sessionStorage.setItem('mac_collab_token', v);
  document.getElementById('tokenBar').style.display = 'none';
  load();
}
function needsToken(){
  document.getElementById('tokenBar').style.display = 'flex';
  document.getElementById('list').innerHTML = '<p>Enter the token above to load the board.</p>';
}
async function api(path, opts){
  opts = opts || {};
  // Prefer the HttpOnly session cookie (set when /board loaded).  Fall back to
  // a stored Bearer so an old tab that never got the cookie still works.
  // Absolute origin+path avoids "URL that includes credentials" on some browsers.
  const headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  const t = tok();
  if (t) headers['Authorization'] = 'Bearer ' + t;
  opts.headers = headers;
  opts.credentials = 'include';
  const r = await fetch(location.origin + path, opts);
  if (r.status === 401) { needsToken(); throw new Error('unauthorized'); }
  if (!r.ok) throw new Error(path + ' -> ' + r.status);
  return r.json();
}
function esc(s){ return (s || '').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

const AGENT_LOGOS = __AGENT_LOGOS_JSON__;
const AGENT_SEATS = __AGENT_SEATS_JSON__;
const AGENT_ENVS  = __AGENT_ENVS_JSON__;
// Longest seat names first so "GROK BOT" wins over a bare "GROK".
const SEAT_KEYS = Object.keys(AGENT_SEATS).sort((a,b) => b.length - a.length);
const SEAT_RE = new RegExp('(?:^|[^A-Z0-9])(' +
  SEAT_KEYS.map(k => k.replace(/[-\\/\\\\^$*+?.()|[\\]{}]/g, '\\\\$&').replace(/ /g, '[ -]?')).join('|') +
  ')\\\\d*(?![A-Z0-9])', 'gi');

// Seats named anywhere in a string (title, reported_by, addressed_by, comment author).
function seatsIn(str){
  if (!str) return [];
  const out = [];
  for (const m of String(str).matchAll(SEAT_RE)) {
    const raw = m[1].toUpperCase().replace(/[- ]/g, ' ').trim();
    const seat = AGENT_SEATS[raw] ? raw : (AGENT_SEATS[raw.replace(/ /g, '')] ? raw.replace(/ /g,'') : raw);
    if (AGENT_SEATS[seat] && !out.includes(seat)) out.push(seat);
  }
  // Pull in companion seats (Cursor never really works without Grok Bot).
  for (const seat of [...out]) {
    for (const companion of (AGENT_SEATS[seat].with || [])) {
      if (!out.includes(companion)) out.push(companion);
    }
  }
  return out;
}
function agentChips(str){
  const seats = seatsIn(str);
  if (!seats.length) return '';
  return '<span class="agents">' + seats.map(seat => {
    const meta = AGENT_SEATS[seat];
    const tip = meta.note || meta.label;
    const uri = AGENT_LOGOS[meta.slug];
    return uri
      ? `<span class="agent" title="${esc(tip)}"><img src="${uri}" alt="${esc(meta.label)}"></span>`
      : `<span class="agent txt" title="${esc(tip)}">${esc(meta.label.slice(0,2))}</span>`;
  }).join('') + '</span>';
}

// Owner-facing timestamps are Central Time, labeled (AGENT-SYNC.md convention).
const CT = 'America/Chicago';
function fmtWhen(iso){
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString('en-US', {timeZone: CT, month:'short', day:'numeric',
      hour:'numeric', minute:'2-digit'}) + ' CT';
  } catch(e){ return iso; }
}
function relWhen(iso){
  if (!iso) return '';
  try {
    const secs = (Date.now() - new Date(iso).getTime()) / 1000;
    if (secs < 90) return 'just now';
    const mins = secs/60; if (mins < 60) return Math.round(mins) + 'm ago';
    const hrs = mins/60;  if (hrs  < 24) return Math.round(hrs)  + 'h ago';
    const days = hrs/24;  if (days < 30) return Math.round(days) + 'd ago';
    return Math.round(days/30) + 'mo ago';
  } catch(e){ return ''; }
}
const STATUSES = ['open','in_progress','completed','deployed','addressed','wontfix','duplicate'];
const APP_DISPLAY_NAMES = {
  'socratic-trade': 'Socratic Trade',
  'congress-trade': 'Congress.Trade',
  'usage-monitor': 'Usage Monitor',
  'congress-trading-shared': 'congress-trading-shared',
  'dealdex': 'DealDex.net',
  'personal-site': 'Personal Site',
  'autorotate': 'Autorotate.Codes',
  'contactlogo': 'ContactLogo',
  'fleet-infra': 'AI Fleet Coordinator',
  'AI-Fleet-Coordinator': 'AI Fleet Coordinator',
  'botfleet': 'BotFleet.app',
  'fleet-ops': 'Fleet Ops',
  'fleetlink': 'FleetLink',
};
function appLabel(a){ return APP_DISPLAY_NAMES[a] || a; }

// Apps that can be filed against, even before any item exists for them.
const KNOWN_APPS = [
  'socratic-trade',
  'congress-trade',
  'usage-monitor',
  'congress-trading-shared',
  'dealdex',
  'personal-site',
  'autorotate',
  'contactlogo',
  'fleet-infra',
  'botfleet',
  'fleet-ops',
  'fleetlink',
];

const TAPE_TILES = [
  {label: 'Total',           key: 'total',    filter: {kind: '', status: '', severity: ''}},
  {label: 'Open',            key: 'open',     filter: {kind: '', status: 'open,in_progress', severity: ''}},
  {label: 'P0/P1 open',      key: 'p0p1_open',filter: {kind: '', status: 'open,in_progress', severity: 'P0,P1'}},
  {label: 'Review findings', key: 'review-finding', filter: {kind: 'review-finding', status: '', severity: ''}},
  {label: 'Effort rows',     key: 'effort-row',     filter: {kind: 'effort-row', status: '', severity: ''}},
  {label: 'GitHub issues',   key: 'github-issue',   filter: {kind: 'github-issue', status: '', severity: ''}},
  {label: 'Done',            key: 'done',     filter: {kind: '', status: 'completed,deployed,addressed', severity: ''}},
];

async function loadStats(){
  const stats = await api('/findings/stats');
  document.getElementById('tape').innerHTML = TAPE_TILES.map((tile, i) => {
    const n = ['total','open','p0p1_open','done'].includes(tile.key) ? stats[tile.key] : (stats.by_kind[tile.key] || 0);
    return `<div data-tile="${i}"><b>${n}</b><span>${esc(tile.label)}</span></div>`;
  }).join('');
  const sel = document.getElementById('fApp');
  const cur = sel.value;
  sel.innerHTML = '<option value="">all</option>' + stats.apps.map(a => `<option value="${esc(a)}">${esc(appLabel(a))}</option>`).join('');
  sel.value = cur;
}

function currentFilterQuery(){
  const qs = new URLSearchParams();
  const app = document.getElementById('fApp').value;
  const kind = document.getElementById('fKind').value;
  const status = document.getElementById('fStatus').value;
  const sev = document.getElementById('fSev').value;
  const search = document.getElementById('fSearch').value.trim();
  if (app) qs.set('app', app);
  if (kind) qs.set('source_kind', kind);
  if (status) qs.set('status', status);
  if (sev) qs.set('severity', sev);
  if (search) qs.set('search', search);
  return qs;
}

let searchDebounce = null;
async function renderList(){
  document.getElementById('list').textContent = 'Loading\\u2026';
  let data;
  try { data = await api('/findings?' + currentFilterQuery().toString()); }
  catch (e) { document.getElementById('list').innerHTML = '<p>Error: ' + esc(e.message) + '</p>'; return; }
  const shown = data.count, total = data.total_matching;
  document.getElementById('count').textContent = shown === total ? (total + ' items') : (shown + ' of ' + total + ' shown');
  document.getElementById('list').innerHTML = data.findings.map(renderFinding).join('') || '<p>No items match these filters.</p>';
}
async function load(){
  try {
    await loadStats();
    applyUrlFilters();
    await renderList();
  } catch (e) {
    if (!tok()) needsToken();
  }
}
['fApp','fKind','fStatus','fSev'].forEach(id => document.getElementById(id).addEventListener('change', renderList));
document.getElementById('fSearch').addEventListener('input', () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(renderList, 350);
});

function kindLabel(k){ return {'review-finding':'review finding','effort-row':'effort board','github-issue':'GitHub issue','agent-report':'filed here'}[k] || k; }

// Seats have a home: Grok Bot is a cloud app (always cloud), plain Grok and the
// Claude-family seats run on the Mac. When an item carries no explicit env, show
// the seat's default, dimmed, rather than nothing.
function impliedEnv(f){
  const seats = seatsIn([f.addressed_by, f.reported_by, f.title].filter(Boolean).join(' '));
  for (const s of seats) { const e = AGENT_SEATS[s] && AGENT_SEATS[s].env; if (e) return e; }
  return '';
}
function renderFinding(f){
  const doneStatuses = ['completed','deployed','addressed'];
  const statusPillClass = doneStatuses.includes(f.status) ? 'ok' : ('sev-' + (f.severity || 'P4'));
  return `<details class="row" data-sev="${esc(f.severity)}">
<summary>
<span class="chev" aria-hidden="true"></span>
<div class="sumhead">
${f.severity ? `<span class="pill sev-${esc(f.severity)}">${esc(f.severity)}</span>` : ''}
<span class="rtitle">${esc(f.title)}</span>
</div>
<span class="rmeta">
${agentChips([f.addressed_by, f.reported_by, f.title].filter(Boolean).join(' '))}
<span>${esc(appLabel(f.app))}</span>
<span>&middot;</span><span>${esc(kindLabel(f.source_kind))}</span>
${f.category ? `<span>&middot;</span><span>${esc(f.category)}</span>` : ''}
${f.surface ? `<span>&middot;</span><span>${esc(f.surface)}</span>` : ''}
<span>&middot;</span><span class="pill ${statusPillClass}">${esc(f.status)}</span>
${f.addressed_by ? `<span>&middot;</span><span>by ${esc(f.addressed_by)}</span>` : ''}
${(() => { const e = f.env || impliedEnv(f); return e
  ? `<span>&middot;</span><span title="${f.env ? 'where this seat is running' : 'implied by the seat (no explicit env recorded)'}"${f.env ? '' : ' style="opacity:.65"'}>${esc(e)}</span>`
  : ''; })()}
${f.location ? `<span>&middot;</span><span title="where this work is happening">📍 ${esc(f.location)}</span>` : ''}
<span>&middot;</span><span title="updated ${esc(fmtWhen(f.updated_at))}">${esc(relWhen(f.updated_at))}</span>
${f.source_url ? `<span>&middot;</span><a href="${esc(f.source_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">source</a>` : ''}
</span>
</summary>
<div class="body">
${f.description ? `<div><span class="lab">Description</span><pre>${esc(f.description)}</pre></div>` : ''}
${f.recommended_fix ? `<div class="fixbox"><span class="lab">Fix</span><pre>${esc(f.recommended_fix)}</pre></div>` : ''}
<div><span class="lab">Timeline</span><span class="when">filed ${esc(fmtWhen(f.created_at))}${f.reported_by ? ' by ' + esc(f.reported_by) : ''} &middot; last update ${esc(fmtWhen(f.updated_at))} (${esc(relWhen(f.updated_at))})</span></div>
<div class="statusrow">
<span class="lab" style="margin:0">Status</span>
<select onchange="setStatus('${f.id}', this.value)">
${STATUSES.map(s => `<option value="${s}" ${s===f.status?'selected':''}>${s}</option>`).join('')}
</select>
<input placeholder="addressed by (e.g. CLAUDE)" value="${esc(f.addressed_by || '')}" style="width:11em" onchange="setAddressedBy('${f.id}', this.value)">
<select style="width:9em" onchange="setEnv('${f.id}', this.value)">${envOptions(f.env)}</select>
<input placeholder="location (worktree @ branch)" value="${esc(f.location || '')}" style="width:15em" onchange="setLocation('${f.id}', this.value)">
</div>
<div>
<span class="lab">Comments</span>
<div class="comments" id="comments-${f.id}">not loaded</div>
<div class="addc">
<input id="author-${f.id}" placeholder="your tag (e.g. CLAUDE)" style="width:9em" value="${esc(myAuthor())}">
<select id="cenv-${f.id}" style="width:9em">${envOptions(myEnv())}</select>
<input id="cloc-${f.id}" placeholder="location (optional)" style="width:10em" value="${esc(myLocation())}">
<input id="text-${f.id}" placeholder="comment on this fix / resolution">
<button onclick="addComment('${f.id}')">Comment</button>
</div>
</div>
</div>
</details>`;
}
async function setStatus(id, status){
  await api('/findings/' + id, {method: 'PATCH', body: JSON.stringify({status})});
  const f = ALL.find(x => x.id === id); if (f) f.status = status;
  renderTape(ALL);
}
async function setAddressedBy(id, addressed_by){
  await api('/findings/' + id, {method: 'PATCH', body: JSON.stringify({addressed_by})});
  const f = ALL.find(x => x.id === id); if (f) f.addressed_by = addressed_by;
}
async function setLocation(id, location){
  await api('/findings/' + id, {method: 'PATCH', body: JSON.stringify({location})});
  const f = ALL.find(x => x.id === id); if (f) f.location = location;
}
async function setEnv(id, env){
  await api('/findings/' + id, {method: 'PATCH', body: JSON.stringify({env})});
  const f = ALL.find(x => x.id === id); if (f) f.env = env;
}
async function addComment(id){
  const author = document.getElementById('author-' + id).value.trim();
  const text = document.getElementById('text-' + id).value.trim();
  const location = document.getElementById('cloc-' + id).value.trim();
  const env = document.getElementById('cenv-' + id).value;
  if (!author || !text) return;
  await api('/findings/' + id + '/comments', {method: 'POST',
    body: JSON.stringify({author, text, location, env})});
  document.getElementById('text-' + id).value = '';
  remember(author, location, env);
  loadComments(id);
}
async function loadComments(id){
  const data = await api('/findings/' + id + '/comments');
  document.getElementById('comments-' + id).innerHTML = data.comments.map(c => {
    const where = [c.env, c.location].filter(Boolean).join(' · ');
    return `<div class="comment">${agentChips(c.author)} <b>${esc(c.author)}</b>: ${esc(c.text)}` +
      `<span class="when" title="${esc(fmtWhen(c.created_at))}">${esc(relWhen(c.created_at))}` +
      `${where ? ' · ' + esc(where) : ''}</span></div>`;
  }).join('') || '<div class="when">No comments yet.</div>';
}

/* ---- identity: remember who/where you are so you type it once ---- */
function remember(author, location, env){
  if (author)   localStorage.setItem('mc_author', author);
  if (location) localStorage.setItem('mc_location', location);
  if (env)      localStorage.setItem('mc_env', env);
}
function myAuthor(){   return localStorage.getItem('mc_author') || ''; }
function myLocation(){ return localStorage.getItem('mc_location') || ''; }
function myEnv(){      return localStorage.getItem('mc_env') || ''; }
function envOptions(sel){
  return '<option value="">env…</option>' + AGENT_ENVS.map(e =>
    `<option value="${esc(e)}" ${e===sel?'selected':''}>${esc(e)}</option>`).join('');
}

/* ---- composer: file a new finding/issue straight from the board ---- */
function toggleComposer(){
  const c = document.getElementById('composer');
  c.hidden = !c.hidden;
  if (!c.hidden) {
    const appSel = document.getElementById('nApp');
    if (!appSel.options.length) {
      appSel.innerHTML = KNOWN_APPS.map(a => `<option value="${esc(a)}">${esc(appLabel(a))}</option>`).join('');
    }
    document.getElementById('nEnv').innerHTML = envOptions(myEnv());
    document.getElementById('nReporter').value = myAuthor();
    document.getElementById('nLocation').value = myLocation();
    document.getElementById('nTitle').focus();
  }
}
async function createItem(){
  const title = document.getElementById('nTitle').value.trim();
  const msg = document.getElementById('nMsg');
  if (!title) { msg.textContent = 'Title is required.'; return; }
  const payload = {
    title,
    app: document.getElementById('nApp').value,
    severity: document.getElementById('nSeverity').value || null,
    description: document.getElementById('nDescription').value.trim(),
    reported_by: document.getElementById('nReporter').value.trim(),
    location: document.getElementById('nLocation').value.trim(),
    env: document.getElementById('nEnv').value,
    source_kind: 'agent-report',
    source: 'filed on the board',
    status: 'open',
  };
  msg.textContent = 'Filing…';
  try { await api('/findings', {method: 'POST', body: JSON.stringify(payload)}); }
  catch (e) { msg.textContent = 'Failed: ' + e.message; return; }
  remember(payload.reported_by, payload.location, payload.env);
  msg.textContent = 'Filed.';
  document.getElementById('nTitle').value = '';
  document.getElementById('nDescription').value = '';
  await load();
  setTimeout(() => { msg.textContent = ''; }, 2500);
}
document.getElementById('list').addEventListener('toggle', e => {
  if (e.target.open) {
    const id = e.target.querySelector('[onchange^="setStatus"]').getAttribute('onchange').match(/'([a-f0-9]{32})'/)[1];
    loadComments(id);
  }
}, true);
function applyUrlFilters(){
  const p = new URLSearchParams(location.search);
  if (p.get('app')) document.getElementById('fApp').value = p.get('app');
  if (p.has('kind')) document.getElementById('fKind').value = p.get('kind');
  if (p.has('status')) document.getElementById('fStatus').value = p.get('status');
  if (p.has('severity')) document.getElementById('fSev').value = p.get('severity');
}
document.getElementById('tape').addEventListener('click', e => {
  const tile = e.target.closest('[data-tile]');
  if (!tile) return;
  const f = TAPE_TILES[Number(tile.getAttribute('data-tile'))].filter;
  document.getElementById('fKind').value = f.kind;
  document.getElementById('fStatus').value = f.status;
  document.getElementById('fSev').value = f.severity;
  renderList();
});
load();
</script>
</body></html>
"""


def _port_holder_pids():
    """PIDs LISTENing on our exact bind port, per lsof.  None on any failure --
    this is a best-effort recovery aid, never a hard dependency."""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-iTCP:%d" % BIND[1], "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    pids = []
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _all_board_server_pids() -> list[int]:
    """Fallback when lsof fails: finds all running mac-collab-server.py PIDs."""
    try:
        out = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid,command"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and "mac-collab-server.py" in parts[1]:
            try:
                pid = int(parts[0])
                if pid != os.getpid():
                    pids.append(pid)
            except ValueError:
                pass
    return pids


def _is_board_server(pid: int) -> bool:
    try:
        argv = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return False
    return "mac-collab-server.py" in argv


def _port_answers_health() -> bool:
    try:
        with urllib.request.urlopen(
            "http://%s:%d/health" % BIND, timeout=HEALTH_PROBE_TIMEOUT
        ) as r:
            r.read(1)
        return True
    except Exception:
        return False


def _is_stale_board_server(pid: int) -> bool:
    """True only for a process that is (a) another mac-collab-server.py and
    (b) not answering /health.  Both conditions are required -- we must never
    kill an unrelated process that happens to hold the port, and we must never
    kill a HEALTHY sibling server."""
    return _is_board_server(pid) and not _port_answers_health()


def _bind_or_reclaim():
    """Bind, and if the port is held by a STALE mac-collab server, reclaim it.

    2026-08-20 outage: pid 4783 was reparented to launchd and kept :8792
    LISTENing while serving nothing.  Every pm2-managed replacement then died
    instantly on `OSError: [Errno 48] Address already in use`, pm2 restarted it
    ~24000 times, and the board was hard-down for two hours behind a 28MB wall
    of identical tracebacks.  Nothing in the loop could ever clear the orphan,
    so it needed a human with lsof.  Reclaim it here instead.
    """
    try:
        return ThreadingHTTPServer(BIND, Handler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
    holders = _port_holder_pids()
    if holders is None:
        print("mac-collab: lsof failed (likely timeout under load), falling back to ps scanning", file=sys.stderr, flush=True)
        holders = _all_board_server_pids()
    stale = [pid for pid in holders if _is_stale_board_server(pid)]
    if not stale:
        if any(_is_board_server(pid) for pid in holders):
            why = "another board server that is answering /health -- not starting a duplicate"
        elif holders:
            why = "a process that is NOT a board server -- refusing to kill it"
        else:
            why = "something lsof could not attribute -- refusing to kill anything"
        print(
            "mac-collab: %s:%s is held by %s.  Run: lsof -nP -iTCP:%s -sTCP:LISTEN"
            % (BIND[0], BIND[1], why, BIND[1]),
            file=sys.stderr, flush=True,
        )
        raise SystemExit(3)
    for pid in stale:
        print("mac-collab: reclaiming %s:%s from stale server pid %d"
              % (BIND[0], BIND[1], pid), flush=True)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            return ThreadingHTTPServer(BIND, Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    for pid in stale:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(1.0)
    return ThreadingHTTPServer(BIND, Handler)


def main():
    tokens = load_tokens()
    token = next((t for t, ident in tokens.items() if ident is None), None)
    if not token:
        print("mac-collab: MAC_COLLAB_TOKEN missing; /files and /findings will 401", flush=True)
    meta = token_auth_meta()
    print(
        "mac-collab token_source=%s token_count=%s secrets_mtime=%s "
        "secrets_newer_than_process=%s pid=%s"
        % (
            meta["token_source"],
            meta["token_count"],
            meta["secrets_mtime"],
            meta["secrets_newer_than_process"],
            meta["pid"],
        ),
        flush=True,
    )
    init_db()
    httpd = _bind_or_reclaim()
    print("mac-collab listening on %s:%s" % BIND, flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
