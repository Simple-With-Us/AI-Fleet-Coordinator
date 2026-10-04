#!/usr/bin/env python3
"""
fleet-sentry-monitor — single-pass fleet health check reported to Sentry.

Design: this script does ONE pass per invocation and exits. It is registered
under pm2 as a long-lived process (`pm2 start ... --name fleet-sentry-monitor`)
whose "loop" is pm2's own restart-on-exit behavior: the process sleeps for
CHECK_INTERVAL_SECONDS then exits 0, and pm2 immediately restarts it, giving
an effective ~120s cadence without a big in-process loop to babysit. This also
means a hang in any one check is bounded by pm2's restart semantics rather
than wedging a single eternal process.

Checks performed each pass:
  1. pm2 jlist: per-app status + restart-count delta since last pass.
     - restart delta >= 5 within one interval -> error "pm2 crash loop: <app>"
     - status != online for `trading` or `trading-main` -> error
     - status != online for any other app -> warning
     Fingerprinted per (app, condition); state file dedups re-emission to at
     most once per hour per fingerprint while the condition persists.
  2. Claude desktop process presence + total RSS -> breadcrumb/context only.
     Not-running is NOT an error condition.
  3. Disk free space on / -> warning < 20GB, error < 8GB.
     Known SQLite WAL files > 512MB -> warning (per-file, deduped hourly).
  4. `gh api rate_limit` -> warning if core or graphql remaining < 300,
     includes reset time in the message.
  5. Self-hosted Actions runner status -> breadcrumb/context only. Offline is
     expected/normal and must never raise a warning/error.
  6. Sentry Crons self-check-in for this monitor itself (slug
     `fleet-host-monitor`), so a dead monitor alerts by absence rather than by
     a signal it can no longer send.

Secrets: SENTRY_FLEET_DSN is read from .env in this directory (or the
environment) and is NEVER printed, logged, or included in any exception text.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "state.json"
ENV_PATH = SCRIPT_DIR / ".env"

CHECK_INTERVAL_SECONDS = 120

# Apps whose non-online status is an ERROR (production-critical); all other
# pm2 apps get a WARNING instead. Empty since 2026-07-08: production moved to
# the Coolify box (socratictrade.com, app socratic-trade-prod) and the Mac's
# trading/preview pm2 apps were retired+deleted — production is now watched by
# check_prod_health() below, not via pm2. That check covers every endpoint in
# PROD_HEALTH_ENDPOINTS (<YOUR_PROJECT_NAME> + the Usage Monitor).
CRITICAL_APPS: set[str] = set()

# External production health check (the app on the Coolify box).
# Production health endpoints. Each entry is monitored independently with its
# own fingerprint and its own sustained-failure state, so one app being down
# never masks or dedupes another.
#
# 2026-07-20: usage.jays.services was added after it served HTTP 502 for ~35
# minutes with nothing alerting. Its Oracle deploy workflow reported SUCCESS
# throughout — that workflow observes /api/ready once during a window and exits,
# so a crash-loop starting after the observation is invisible to it. This
# monitor is the only thing that would have caught it.
#
# <YOUR_OTHER_PROJECT_NAME> is deliberately NOT listed: congress.trade/api/health returns
# HTTP 403 to non-browser User-Agents (Cloudflare managed challenge), so it
# needs browser-UA handling rather than this plain probe — adding it naively
# would emit a permanent false outage.
PROD_HEALTH_ENDPOINTS = [
    {"name": "socratic-trade", "url": "https://socratictrade.com/api/health"},
    {"name": "usage-monitor", "url": "https://usage.jays.services/api/health"},
    {"name": "dealdex", "url": "https://dealdex.net/"},
    {"name": "botfleet", "url": "https://botfleet.app/"},
    {"name": "mac-collab", "url": "https://mac.jays.services/health"},
    {"name": "agent-sync", "url": "http://127.0.0.1:8787/health"},
    {"name": "cloudflared-ready", "url": "http://127.0.0.1:20241/ready"},
]
PROD_HEALTH_TIMEOUT_SECONDS = 20

RESTART_DELTA_ERROR_THRESHOLD = 5
DEDUP_WINDOW_SECONDS = 60 * 60  # re-emit a persisting fingerprint at most hourly

DISK_WARN_FREE_GB = 20
DISK_ERROR_FREE_GB = 8
WAL_WARN_BYTES = 512 * 1024 * 1024  # 512MB

GH_RATE_LIMIT_WARN_REMAINING = 300

# Oracle primary -> Coolify Garage backup path. These checks run from the
# existing Mac singleton so no additional host daemon or alert credential is
# needed. Both SSH commands are read-only except the weekly restore drill,
# which writes an exact scratch path on Oracle and removes it in a trap.
USAGE_MONITOR_ORACLE_SSH = os.environ.get("USAGE_MONITOR_SSH_HOST", "coolify")
USAGE_MONITOR_COOLIFY_SSH = os.environ.get("COOLIFY_SSH_HOST", "coolify")
USAGE_MONITOR_SSH_KEY = Path.home() / ".ssh" / "id_ed25519"
USAGE_MONITOR_GARAGE_CONTAINER = os.environ.get("USAGE_MONITOR_GARAGE_CONTAINER", "garage")
USAGE_MONITOR_BACKUP_CHECK_INTERVAL_SECONDS = 15 * 60
USAGE_MONITOR_RESTORE_DRILL_INTERVAL_SECONDS = 7 * 24 * 60 * 60
USAGE_MONITOR_REPLICA_MAX_AGE_SECONDS = 60 * 60
USAGE_MONITOR_COOLIFY_DISK_WARN_FREE_GB = 15
USAGE_MONITOR_COOLIFY_DISK_ERROR_FREE_GB = 8
USAGE_MONITOR_BACKUP_MONITOR_SLUG = "usage-monitor-garage-backup"

WAL_GLOBS = [
    str(Path.home() / "apps" / "*" / "data" / "app.db-wal"),
    "<YOUR_PROJECT_DIR>/data/app.db-wal",
]

MONITOR_SLUG = "fleet-host-monitor"
GH_REPO = "Simple-With-Us/Socratic-Trade"
DEFAULT_TAGS = {"agent": "FLEET", "app": "fleet-infra"}
PM2_TAGS = {
    "trading-codex": {"agent": "CODEX", "app": "socratic-trade"},
    "trading-claude": {"agent": "CLAUDE", "app": "socratic-trade"},
    "trading": {"agent": "FLEET", "app": "socratic-trade"},
    "trading-main": {"agent": "FLEET", "app": "socratic-trade"},
    "congress-scout": {"agent": "FLEET", "app": "congress-trade"},
    "dealdex": {"agent": "FLEET", "app": "dealdex"},
    "botfleet": {"agent": "FLEET", "app": "botfleet"},
}
CODEX_SESSION_GLOB = str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl")


def load_env_value(want_key: str) -> str | None:
    """Read a secret from the environment or this dir's chmod-600 .env file.

    Never logs or returns the value in any exception message — callers must
    not print this return value either.
    """
    val = os.environ.get(want_key)
    if val:
        return val.strip() or None
    if not ENV_PATH.exists():
        return None
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == want_key:
                value = value.strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def gh_cmd_env() -> dict | None:
    """Env for gh calls: GH_TOKEN from .env so the pm2 daemon (no keychain session)
    measures the FLEET'S token pool instead of silently falling back to GitHub's
    anonymous per-IP pool (which reads graphql 0/0 + core<=60 and spammed
    FLEET-INFRA-3/4 hourly from 2026-07-04 to 2026-07-10 while the real pool went
    unwatched)."""
    token = load_env_value("GH_TOKEN")
    if not token:
        return None
    return {**os.environ, "GH_TOKEN": token}


def load_dsn() -> str | None:
    """Read SENTRY_FLEET_DSN from the environment or this dir's .env file.

    Never logs or returns the value in any exception message — callers must
    not print this return value either.
    """
    dsn = os.environ.get("SENTRY_FLEET_DSN")
    if dsn:
        return dsn.strip() or None
    if not ENV_PATH.exists():
        return None
    try:
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "SENTRY_FLEET_DSN":
                value = value.strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"restart_counts": {}, "fingerprints": {}}
    try:
        data = json.loads(STATE_PATH.read_text())
        data.setdefault("restart_counts", {})
        data.setdefault("fingerprints", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"restart_counts": {}, "fingerprints": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def should_emit(state: dict, fingerprint: str, now: float) -> bool:
    """Dedup: only emit a given fingerprint at most once per DEDUP_WINDOW_SECONDS."""
    last = state["fingerprints"].get(fingerprint)
    if last is not None and (now - last) < DEDUP_WINDOW_SECONDS:
        return False
    state["fingerprints"][fingerprint] = now
    return True


def prune_fingerprints(state: dict, now: float) -> None:
    cutoff = now - (DEDUP_WINDOW_SECONDS * 4)
    state["fingerprints"] = {
        fp: ts for fp, ts in state["fingerprints"].items() if ts >= cutoff
    }


def run_cmd(args: list[str], timeout: int = 30, env: dict | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


# ── Sentry plumbing ─────────────────────────────────────────────────────────

_sentry_sdk = None
_sentry_available = False


def init_sentry(dsn: str | None) -> None:
    global _sentry_sdk, _sentry_available
    if not dsn:
        print("[fleet-sentry-monitor] SENTRY_FLEET_DSN not set; running in log-only mode.")
        return
    try:
        import sentry_sdk as sdk

        sdk.init(
            dsn=dsn,
            environment="fleet",
            traces_sample_rate=0.0,
            send_default_pii=False,
        )
        _sentry_sdk = sdk
        _sentry_available = True
    except ImportError:
        print(
            "[fleet-sentry-monitor] sentry-sdk not installed; falling back to raw "
            "envelope HTTP is not implemented in this fallback path for events. "
            "Check-ins will still attempt raw HTTP.",
        )


def capture_message(
    message: str,
    level: str,
    fingerprint: list[str],
    extra: dict,
    tags: dict | None = None,
) -> None:
    event_tags = {**DEFAULT_TAGS, **(tags or {})}
    print(f"[fleet-sentry-monitor] {level.upper()}: {message} | tags={event_tags} | extra={extra}")
    if _sentry_available and _sentry_sdk is not None:
        with _sentry_sdk.new_scope() as scope:
            scope.fingerprint = fingerprint
            for k, v in event_tags.items():
                scope.set_tag(k, v)
            for k, v in extra.items():
                scope.set_extra(k, v)
            _sentry_sdk.capture_message(message, level=level)


def add_breadcrumb(category: str, message: str, data: dict) -> None:
    if _sentry_available and _sentry_sdk is not None:
        _sentry_sdk.add_breadcrumb(category=category, message=message, data=data, level="info")
    else:
        print(f"[fleet-sentry-monitor] breadcrumb[{category}]: {message} | {data}")


def sentry_cron_checkin(
    status: str,
    dsn: str | None,
    *,
    monitor_slug: str = MONITOR_SLUG,
    interval_minutes: int = 2,
    max_runtime_minutes: int = 2,
) -> None:
    """Send a Sentry Cron check-in, upserting the bounded interval config."""
    monitor_config = {
        "schedule": {"type": "interval", "value": interval_minutes, "unit": "minute"},
        "checkin_margin": 5,
        "max_runtime": max_runtime_minutes,
        "timezone": "America/Chicago",
    }
    if _sentry_available and _sentry_sdk is not None:
        try:
            _sentry_sdk.crons.capture_checkin(
                monitor_slug=monitor_slug,
                status=status,
                monitor_config=monitor_config,
            )
            print(
                f"[fleet-sentry-monitor] Sentry Crons check-in sent: "
                f"{monitor_slug}={status}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - monitoring must never crash the pass
            print(f"[fleet-sentry-monitor] sentry-sdk check-in failed, falling back: {exc}")
    if dsn:
        _raw_envelope_checkin(dsn, status, monitor_config, monitor_slug)


def _parse_dsn(dsn: str) -> tuple[str, str, str] | None:
    """Parse a Sentry DSN into (ingest_host, project_id, public_key). Never logs the DSN."""
    try:
        # DSN shape: https://<public_key>@<host>/<project_id>
        scheme_sep = dsn.index("://")
        rest = dsn[scheme_sep + 3 :]
        key, _, hostpath = rest.partition("@")
        host, _, project_id = hostpath.partition("/")
        if not key or not host or not project_id:
            return None
        return host, project_id, key
    except (ValueError, IndexError):
        return None


def _raw_envelope_checkin(
    dsn: str,
    status: str,
    monitor_config: dict,
    monitor_slug: str = MONITOR_SLUG,
) -> None:
    """Fallback path: send a check-in envelope over raw HTTP via urllib (no sentry-sdk)."""
    import urllib.request
    import uuid
    from datetime import datetime, timezone

    parsed = _parse_dsn(dsn)
    if not parsed:
        print("[fleet-sentry-monitor] could not parse DSN for raw check-in fallback.")
        return
    host, project_id, public_key = parsed
    url = f"https://{host}/api/{project_id}/envelope/"

    checkin_id = uuid.uuid4().hex
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope_header = json.dumps({"sent_at": now_iso})
    item_payload = {
        "check_in_id": checkin_id,
        "monitor_slug": monitor_slug,
        "status": status,
        "monitor_config": monitor_config,
    }
    item_body = json.dumps(item_payload)
    item_header = json.dumps({"type": "check_in", "length": len(item_body.encode("utf-8"))})
    envelope = "\n".join([envelope_header, item_header, item_body]) + "\n"

    auth_header = (
        f"Sentry sentry_version=7, sentry_client=fleet-monitor-raw/1.0, "
        f"sentry_key={public_key}"
    )
    req = urllib.request.Request(
        url,
        data=envelope.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-sentry-envelope",
            "X-Sentry-Auth": auth_header,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[fleet-sentry-monitor] raw check-in HTTP status: {resp.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"[fleet-sentry-monitor] raw check-in failed: {exc}")


# ── Checks ───────────────────────────────────────────────────────────────────


def check_pm2(state: dict, now: float) -> None:
    code, out, err = run_cmd(["pm2", "jlist"], timeout=60)
    if code != 0 or not out.strip():
        capture_message(
            "pm2 jlist failed or returned no output",
            "warning",
            ["pm2-jlist-failure"],
            {"stderr": err[-500:]},
        )
        return
    try:
        apps = json.loads(out)
    except json.JSONDecodeError:
        capture_message(
            "pm2 jlist returned unparseable JSON", "warning", ["pm2-jlist-parse-error"], {}
        )
        return

    prior_counts: dict = state["restart_counts"]
    new_counts: dict = {}

    for app in apps:
        name = app.get("name", "unknown")
        tags = {**PM2_TAGS.get(name, DEFAULT_TAGS), "process": name}
        env = app.get("pm2_env", {}) or {}
        status = env.get("status", "unknown")
        try:
            # pm2 occasionally reports restart_time as a string; state.json may
            # carry old string values too — coerce both before arithmetic.
            restart_time = int(env.get("restart_time", 0) or 0)
        except (TypeError, ValueError):
            restart_time = 0
        new_counts[name] = restart_time

        prior = prior_counts.get(name)
        try:
            prior = int(prior) if prior is not None else None
        except (TypeError, ValueError):
            prior = None
        if prior is not None:
            delta = restart_time - prior
            if delta >= RESTART_DELTA_ERROR_THRESHOLD:
                fp = f"pm2-crash-loop:{name}"
                if should_emit(state, fp, now):
                    capture_message(
                        f"pm2 crash loop: {name}",
                        "error",
                        [fp],
                        {
                            "app": name,
                            "restart_delta": delta,
                            "restart_time_total": restart_time,
                            "interval_seconds": CHECK_INTERVAL_SECONDS,
                        },
                        tags,
                    )

        if status != "online":
            level = "error" if name in CRITICAL_APPS else "warning"
            fp = f"pm2-status:{name}:{status}"
            if should_emit(state, fp, now):
                capture_message(
                    f"pm2 app not online: {name} (status={status})",
                    level,
                    [fp],
                    {"app": name, "status": status, "restart_time_total": restart_time},
                    tags,
                )

    state["restart_counts"] = new_counts


def check_prod_health(state: dict, now: float) -> None:
    """GET each production /api/health; ERROR if unreachable or not ok.

    Replaces the old pm2-based CRITICAL_APPS coverage for `trading` after the
    2026-07-08 migration. Fingerprinted + deduped like every other check, and
    since 2026-07-20 covers every entry in PROD_HEALTH_ENDPOINTS rather than a
    single hard-coded app — each with independent state, so a persistent outage
    in one app cannot suppress a new outage in another.
    """
    for endpoint in PROD_HEALTH_ENDPOINTS:
        _check_one_prod_health(endpoint, state, now)


def _check_one_prod_health(endpoint: dict, state: dict, now: float) -> None:
    import urllib.request

    name = endpoint["name"]
    url = endpoint["url"]
    fp = f"prod-health:{name}"
    # Per-endpoint key. The pre-2026-07-20 single-app key was
    # "prod_health_prev_fail"; it is intentionally not reused so a stale value
    # cannot make a different app appear to have already failed once.
    state_key = f"prod_health_prev_fail:{name}"
    problem: str | None = None
    # Retry within the pass: a single blown probe (deploy restart, SSL handshake blip) is not
    # an outage. 3 attempts, ~10s apart — both FLEET-INFRA-29 events (07-08 disk-day, 07-09
    # migration deploy) lasted <2min and would have been absorbed here or by the
    # sustained-across-passes gate below.
    for attempt in range(3):
        problem = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fleet-sentry-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=PROD_HEALTH_TIMEOUT_SECONDS) as resp:
                status_code = resp.status
                raw_data = resp.read()

            if status_code != 200:
                problem = f"HTTP status {status_code}"
            else:
                try:
                    body = json.loads(raw_data.decode("utf-8"))
                    if isinstance(body, dict) and "ok" in body and body.get("ok") is not True:
                        problem = f"health ok={body.get('ok')!r} checks={body.get('checks')!r}"[:400]
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Non-JSON HTML/static response with HTTP 200 is healthy
                    body = {}

                if not problem:
                    add_breadcrumb(
                        "prod",
                        f"prod health ok ({name})",
                        {
                            "app": name,
                            "schedulerAgeSeconds": (body.get("checks") or {}).get("schedulerAgeSeconds") if isinstance(body, dict) else None,
                            "revision": body.get("revision") if isinstance(body, dict) else None,
                        },
                    )
                    break
        except Exception as exc:  # noqa: BLE001 - any failure to reach prod is the finding
            # A 502/503 raises HTTPError here, which is exactly the shape the
            # 2026-07-20 usage-monitor outage took (app container exited, proxy up).
            problem = f"unreachable: {type(exc).__name__}: {exc}"[:400]
        if problem and attempt < 2:
            time.sleep(10)

    if problem:
        # Only page when the condition SUSTAINS across two consecutive passes (~2 min apart).
        prev_fail = state.get(state_key, False)
        state[state_key] = True
        if prev_fail and should_emit(state, fp, now):
            capture_message(
                f"PRODUCTION health check failed ({url}): {problem}",
                "error",
                [fp],
                {"app": name, "url": url, "problem": problem},
                {},
            )
    else:
        state[state_key] = False


def _process_summary(pattern: str) -> tuple[bool, int, float]:
    code, out, _ = run_cmd(["bash", "-c", f"ps aux | grep -i {pattern!r} | grep -v grep"])
    if code != 0:
        return False, 0, 0.0
    lines = [line for line in out.splitlines() if line.strip()]
    total_rss_kb = 0
    for line in lines:
        parts = line.split()
        if len(parts) > 5:
            try:
                total_rss_kb += int(parts[5])
            except ValueError:
                continue
    return bool(lines), len(lines), round(total_rss_kb / 1024, 1)


def check_claude_desktop() -> None:
    running, count, rss_mb = _process_summary("/Applications/Claude.app")
    if not running:
        add_breadcrumb("claude-desktop", "Claude desktop not running", {"running": False})
        return
    add_breadcrumb(
        "claude-desktop",
        "Claude desktop running",
        {"running": True, "process_count": count, "total_rss_mb": rss_mb},
    )


def check_codex_desktop_and_sessions(state: dict, now: float) -> None:
    running, count, rss_mb = _process_summary("/Applications/Codex.app")
    add_breadcrumb(
        "codex-desktop",
        "Codex desktop running" if running else "Codex desktop not running",
        {"running": running, "process_count": count, "total_rss_mb": rss_mb},
    )

    session_paths = glob.glob(CODEX_SESSION_GLOB, recursive=True)
    if not session_paths:
        fp = "codex-sessions-missing"
        if should_emit(state, fp, now):
            capture_message(
                "Codex session telemetry files not found",
                "warning",
                [fp],
                {"glob": CODEX_SESSION_GLOB},
                {"agent": "CODEX", "app": "codex"},
            )
        return

    latest_path = max(session_paths, key=lambda p: os.path.getmtime(p))
    latest_mtime = os.path.getmtime(latest_path)
    add_breadcrumb(
        "codex-sessions",
        "latest Codex session file",
        {
            "count": len(session_paths),
            "latest_age_seconds": round(now - latest_mtime),
            "latest_path": latest_path.replace(str(Path.home()), "~", 1),
        },
    )

def check_disk(state: dict, now: float) -> None:
    try:
        st = os.statvfs("/")
        free_bytes = st.f_bavail * st.f_frsize
        free_gb = free_bytes / (1024**3)
    except OSError as exc:
        capture_message("disk free-space check failed", "warning", ["disk-check-failure"], {"error": str(exc)})
        return

    if free_gb < DISK_ERROR_FREE_GB:
        fp = "disk-free-error"
        if should_emit(state, fp, now):
            capture_message(
                f"disk free space critically low: {free_gb:.1f}GB free on /",
                "error",
                [fp],
                {"free_gb": round(free_gb, 1)},
            )
    elif free_gb < DISK_WARN_FREE_GB:
        fp = "disk-free-warning"
        if should_emit(state, fp, now):
            capture_message(
                f"disk free space low: {free_gb:.1f}GB free on /",
                "warning",
                [fp],
                {"free_gb": round(free_gb, 1)},
            )

    for pattern in WAL_GLOBS:
        for path in glob.glob(pattern):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > WAL_WARN_BYTES:
                fp = f"wal-size:{path}"
                if should_emit(state, fp, now):
                    capture_message(
                        f"SQLite WAL file oversized: {path} ({size / (1024*1024):.1f}MB)",
                        "warning",
                        [fp],
                        {"path": path, "size_mb": round(size / (1024 * 1024), 1)},
                    )


def check_gh_rate_limit(state: dict, now: float) -> None:
    code, out, err = run_cmd(["gh", "api", "rate_limit"], env=gh_cmd_env())
    if code != 0 or not out.strip():
        capture_message(
            "gh api rate_limit failed", "warning", ["gh-rate-limit-failure"], {"stderr": err[-500:]}
        )
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return
    resources = data.get("resources", {})
    # Anonymity guard: the anonymous per-IP pool has core limit <= 60. Alerting on it is
    # meaningless (and it means we are NOT watching the fleet token) — emit one distinct
    # warning instead of per-kind spam.
    if (resources.get("core", {}).get("limit") or 0) <= 60:
        fp = "gh-rate-limit-unauthenticated"
        if should_emit(state, fp, now):
            capture_message(
                "fleet monitor gh is UNAUTHENTICATED — rate-limit check is reading the anonymous "
                "per-IP pool, not the fleet token. Add GH_TOKEN to fleet-sentry-monitor/.env.",
                "warning",
                [fp],
                {"core_limit": resources.get("core", {}).get("limit")},
            )
        return
    for kind in ("core", "graphql"):
        info = resources.get(kind, {})
        remaining = info.get("remaining")
        reset_epoch = info.get("reset")
        if remaining is None:
            continue
        if (info.get("limit") or 0) == 0:
            continue  # no quota exists for this kind on this token — nothing to alert on
        if remaining < GH_RATE_LIMIT_WARN_REMAINING:
            reset_str = (
                time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(reset_epoch))
                if reset_epoch
                else "unknown"
            )
            fp = f"gh-rate-limit:{kind}"
            if should_emit(state, fp, now):
                capture_message(
                    f"gh {kind} rate limit low: {remaining} remaining (resets {reset_str})",
                    "warning",
                    [fp],
                    {"kind": kind, "remaining": remaining, "reset_at": reset_str},
                )


def check_self_hosted_runner() -> None:
    code, out, _ = run_cmd(
        ["gh", "api", f"repos/{GH_REPO}/actions/runners"], env=gh_cmd_env()
    )
    if code != 0 or not out.strip():
        add_breadcrumb("gh-runner", "runner status lookup failed (non-fatal, context only)", {})
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return
    runners = data.get("runners", [])
    if not runners:
        add_breadcrumb("gh-runner", "no self-hosted runners registered", {"count": 0})
        return
    for runner in runners:
        add_breadcrumb(
            "gh-runner",
            f"runner {runner.get('name')} status={runner.get('status')}",
            {
                "name": runner.get("name"),
                "status": runner.get("status"),
                "busy": runner.get("busy"),
            },
        )


def _ssh(host: str, remote_command: str, timeout: int) -> tuple[int, str, str]:
    return run_cmd(
        [
            "ssh",
            "-i",
            str(USAGE_MONITOR_SSH_KEY),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=yes",
            host,
            remote_command,
        ],
        timeout=timeout,
    )


def _parse_ltx_listing(listing: str) -> tuple[str, datetime, int]:
    """Return (maximum txid, newest object timestamp, parsed row count)."""
    max_txid = -1
    latest: datetime | None = None
    rows = 0
    for raw_line in listing.splitlines():
        parts = raw_line.split()
        if len(parts) != 5 or not parts[0].isdigit():
            continue
        _, _min_txid, max_txid_text, _size, created_text = parts
        try:
            parsed_txid = int(max_txid_text, 16)
            parsed_created = datetime.fromisoformat(created_text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        max_txid = max(max_txid, parsed_txid)
        latest = parsed_created if latest is None else max(latest, parsed_created)
        rows += 1
    if max_txid < 0 or latest is None or rows == 0:
        raise ValueError("Litestream returned no parseable LTX rows")
    return f"{max_txid:016x}", latest, rows


def _usage_monitor_oracle_backup_probe() -> dict:
    command = r"""
set -eu
cd /opt/Usage-Monitor/deploy/oracle
scheduler=$(sudo awk -F= '$1=="USAGE_SCHEDULER_ENABLED" {print $2; exit}' /etc/usage-monitor/usage-monitor.env | tr -d "[:space:]'\"")
printf 'SCHEDULER=%s\n' "${scheduler:-unknown}"
printf 'LTX_BEGIN\n'
sudo docker compose exec -T app bin/litestream ltx -config /app/litestream.yml -level all /data/prod.db
printf 'LTX_END\n'
sudo docker compose exec -T app bin/litestream restore -config /app/litestream.yml -dry-run -o /data/.garage-backup-monitor-dry-run.db /data/prod.db >/dev/null
printf 'DRY_RUN_OK\n'
""".strip()
    code, out, err = _ssh(USAGE_MONITOR_ORACLE_SSH, command, timeout=300)
    if code != 0:
        raise RuntimeError(f"Oracle Garage probe failed (exit={code}): {err[-300:]}")
    if "DRY_RUN_OK" not in out or "LTX_BEGIN\n" not in out or "\nLTX_END" not in out:
        raise RuntimeError("Oracle Garage probe omitted required completion markers")
    scheduler_line = next(
        (line for line in out.splitlines() if line.startswith("SCHEDULER=")),
        "SCHEDULER=unknown",
    )
    scheduler_enabled = scheduler_line.partition("=")[2] == "true"
    listing = out.split("LTX_BEGIN\n", 1)[1].split("\nLTX_END", 1)[0]
    max_txid, latest, row_count = _parse_ltx_listing(listing)
    return {
        "scheduler_enabled": scheduler_enabled,
        "max_txid": max_txid,
        "latest_created": latest,
        "ltx_rows": row_count,
    }


def _usage_monitor_restore_drill() -> None:
    command = r"""
set -eu
cd /opt/Usage-Monitor/deploy/oracle
sudo docker compose exec -T app sh -lc '
set -eu
scratch=/data/.garage-backup-monitor-restore.db
rm -f /data/.garage-backup-monitor-restore.db /data/.garage-backup-monitor-restore.db-wal /data/.garage-backup-monitor-restore.db-shm
trap "rm -f /data/.garage-backup-monitor-restore.db /data/.garage-backup-monitor-restore.db-wal /data/.garage-backup-monitor-restore.db-shm" EXIT
bin/litestream restore -config /app/litestream.yml -integrity-check full -o "$scratch" /data/prod.db >/dev/null
test -s "$scratch"
'
printf 'RESTORE_DRILL_OK\n'
""".strip()
    code, out, err = _ssh(USAGE_MONITOR_ORACLE_SSH, command, timeout=900)
    if code != 0 or "RESTORE_DRILL_OK" not in out:
        raise RuntimeError(f"Garage restore drill failed (exit={code}): {err[-300:]}")


def _usage_monitor_coolify_disk_probe() -> dict:
    command = f"""
python3 - <<'PY'
import json
import os
import subprocess

st = os.statvfs('/var/lib/docker')
inspect = subprocess.run(
    ['docker', 'inspect', '--format', '{{{{.State.Status}}}} {{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}', '{USAGE_MONITOR_GARAGE_CONTAINER}'],
    capture_output=True,
    text=True,
    timeout=20,
    check=False,
)
print(json.dumps({{
    'free_bytes': st.f_bavail * st.f_frsize,
    'total_bytes': st.f_blocks * st.f_frsize,
    'garage': inspect.stdout.strip(),
    'inspect_exit': inspect.returncode,
}}))
PY
""".strip()
    code, out, err = _ssh(USAGE_MONITOR_COOLIFY_SSH, command, timeout=60)
    if code != 0:
        raise RuntimeError(f"Coolify disk probe failed (exit={code}): {err[-300:]}")
    try:
        result = json.loads(out.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Coolify disk probe returned invalid JSON") from exc
    if result.get("inspect_exit") != 0 or result.get("garage") != "running healthy":
        raise RuntimeError(f"Garage container unhealthy: {result.get('garage')!r}")
    return result


def check_usage_monitor_garage_backup(state: dict, now: float, dsn: str | None) -> None:
    """Throttled authenticated replica, restore, and Coolify disk checks."""
    last_attempt = float(state.get("usage_monitor_backup_last_attempt", 0) or 0)
    if now - last_attempt < USAGE_MONITOR_BACKUP_CHECK_INTERVAL_SECONDS:
        return
    state["usage_monitor_backup_last_attempt"] = now

    problems: list[tuple[str, str, dict]] = []
    backup: dict | None = None
    disk: dict | None = None
    try:
        backup = _usage_monitor_oracle_backup_probe()
        latest = backup["latest_created"]
        replica_age = max(0, now - latest.timestamp())
        backup["replica_age_seconds"] = round(replica_age)
        backup["latest_created"] = latest.isoformat()
        if backup["scheduler_enabled"] and replica_age > USAGE_MONITOR_REPLICA_MAX_AGE_SECONDS:
            problems.append((
                "usage-monitor-garage-replica-stale",
                f"Usage Monitor Garage replica is stale ({replica_age / 60:.0f} minutes)",
                backup,
            ))
    except Exception as exc:  # noqa: BLE001 - convert each probe failure into one alert
        problems.append((
            "usage-monitor-garage-probe-failed",
            "Usage Monitor Garage authenticated replica probe failed",
            {"error": f"{type(exc).__name__}: {exc}"[:500]},
        ))

    try:
        disk = _usage_monitor_coolify_disk_probe()
        free_gb = disk["free_bytes"] / (1024**3)
        total_gb = disk["total_bytes"] / (1024**3)
        disk = {"free_gb": round(free_gb, 1), "total_gb": round(total_gb, 1), "garage": disk["garage"]}
        threshold = (
            USAGE_MONITOR_COOLIFY_DISK_ERROR_FREE_GB
            if free_gb < USAGE_MONITOR_COOLIFY_DISK_ERROR_FREE_GB
            else USAGE_MONITOR_COOLIFY_DISK_WARN_FREE_GB
        )
        if free_gb < USAGE_MONITOR_COOLIFY_DISK_WARN_FREE_GB:
            problems.append((
                "usage-monitor-coolify-disk-low",
                f"Coolify Garage disk is low ({free_gb:.1f}GB free)",
                {**disk, "threshold_gb": threshold},
            ))
    except Exception as exc:  # noqa: BLE001
        problems.append((
            "usage-monitor-coolify-probe-failed",
            "Coolify Garage health/disk probe failed",
            {"error": f"{type(exc).__name__}: {exc}"[:500]},
        ))

    last_restore = float(state.get("usage_monitor_backup_last_restore", 0) or 0)
    if not problems and now - last_restore >= USAGE_MONITOR_RESTORE_DRILL_INTERVAL_SECONDS:
        try:
            _usage_monitor_restore_drill()
            state["usage_monitor_backup_last_restore"] = now
        except Exception as exc:  # noqa: BLE001
            problems.append((
                "usage-monitor-garage-restore-failed",
                "Usage Monitor Garage full restore drill failed",
                {"error": f"{type(exc).__name__}: {exc}"[:500]},
            ))

    for fingerprint, message, extra in problems:
        if should_emit(state, fingerprint, now):
            capture_message(
                message,
                "error" if "disk-low" not in fingerprint else "warning",
                [fingerprint],
                extra,
                {"agent": "FLEET", "app": "api-usage-monitor"},
            )

    status = "error" if problems else "ok"
    if not problems:
        add_breadcrumb(
            "usage-monitor-backup",
            "Garage replica, restore plan, container, and disk healthy",
            {"backup": backup, "disk": disk, "last_restore": state.get("usage_monitor_backup_last_restore")},
        )
    sentry_cron_checkin(
        status,
        dsn,
        monitor_slug=USAGE_MONITOR_BACKUP_MONITOR_SLUG,
        interval_minutes=15,
        max_runtime_minutes=15,
    )


def main() -> int:
    dsn = load_dsn()
    init_sentry(dsn)

    now = time.time()
    state = load_state()

    status = "ok"
    try:
        check_pm2(state, now)
        check_prod_health(state, now)
        check_claude_desktop()
        check_codex_desktop_and_sessions(state, now)
        check_disk(state, now)
        check_gh_rate_limit(state, now)
        check_self_hosted_runner()
        check_usage_monitor_garage_backup(state, now, dsn)
    except Exception as exc:  # noqa: BLE001 - never let one bad check crash the pass
        status = "error"
        capture_message(f"fleet-sentry-monitor pass raised: {exc}", "error", ["monitor-internal-error"], {})

    prune_fingerprints(state, now)
    save_state(state)

    sentry_cron_checkin(status, dsn)

    if _sentry_available and _sentry_sdk is not None:
        _sentry_sdk.flush(timeout=5)

    print(f"[fleet-sentry-monitor] pass complete at {time.strftime('%Y-%m-%d %H:%M:%S')}; sleeping {CHECK_INTERVAL_SECONDS}s")
    time.sleep(CHECK_INTERVAL_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
