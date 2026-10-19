#!/usr/bin/env python3
"""Sample Mac disk / RAM / CPU and optionally clean + wake Housekeeper.

Launchd: com.jay.mac-resource-watch (every 5 min).
On-demand: python3 ~/apps/mac-resource-watch.py --once
Never prints webhook secrets.  Load BOTFLEET_HOUSEKEEPER_WEBHOOK_* from
~/.secrets/botfleet-housekeeper-webhook.env and, if present,
GROK_BOT_HOUSEKEEPER_WEBHOOK_* from ~/.secrets/grok-bot-housekeeper-webhook.env
without echoing values.  BotFleet is the primary wake.  Grok Bot is optional.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".claude-disk-janitor"
STATE_FILE = STATE_DIR / "resource-watch-state.json"
LOG_FILE = HOME / "Library" / "Logs" / "mac-resource-watch.log"
SECRET_FILE = HOME / ".secrets" / "botfleet-housekeeper-webhook.env"
GB_SECRET_FILE = HOME / ".secrets" / "grok-bot-housekeeper-webhook.env"
CLEANUP = HOME / "apps" / "mac-auto-cleanup.sh"
JANITOR = HOME / ".claude-disk-janitor" / "janitor.sh"

# Defaults: start reclaiming before the old 50G janitor cliff.  Overnight
# 6-13G drops in 30 min were blowing through that floor.
DISK_FREE_WARN_GB = float(os.environ.get("RESOURCE_DISK_WARN_GB", "80"))
DISK_FREE_CRIT_GB = float(os.environ.get("RESOURCE_DISK_CRIT_GB", "50"))
DISK_DROP_ALERT_GB = float(os.environ.get("RESOURCE_DISK_DROP_GB", "6"))
SWAP_USED_PCT = float(os.environ.get("RESOURCE_SWAP_USED_PCT", "80"))
# Absolute used swap, not the size of the store.  macOS grows the store on demand, so
# swap_used_pct sits near 90% whether 2G or 20G is actually paged out.
SWAP_USED_GB = float(os.environ.get("RESOURCE_SWAP_USED_GB", "8"))
LOAD1_THRESHOLD = float(os.environ.get("RESOURCE_LOAD1", "16"))
CPU_ENABLED = os.environ.get("RESOURCE_CPU", "1") not in {"0", "false", "no"}
COOLDOWN_SEC = int(os.environ.get("RESOURCE_COOLDOWN_SEC", "2700"))
# 2026-09-01 (Claude): cleanup had NO cooldown -- only the webhook did.  With disk
# hovering under the 80G warn line this ran mac-auto-cleanup.sh --pressure every
# 5 min, back to back.  Measured: each run grew swap 3.6-7.3G (CleanMyMac purge +
# `optimize ram` force pages out) and 2 of 3 runs ended with LESS free disk than
# they started.  Cure was worse than the disease.
CLEAN_COOLDOWN_SEC = int(os.environ.get("RESOURCE_CLEAN_COOLDOWN_SEC", "3600"))
CLEAN_COOLDOWN_CRIT_SEC = int(os.environ.get("RESOURCE_CLEAN_COOLDOWN_CRIT_SEC", "900"))
LOCK_FILE = STATE_DIR / "resource-watch.lock"
LOCK_STALE_SEC = int(os.environ.get("RESOURCE_LOCK_STALE_SEC", "1800"))


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        raw = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(raw) > 800:
            LOG_FILE.write_text("\n".join(raw[-800:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        return subprocess.check_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def sample() -> dict:
    df = _run(["/bin/df", "-k", "/System/Volumes/Data"]) or _run(["/bin/df", "-k", "/"])
    disk_free_gb = disk_used_pct = 0.0
    for line in df.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            avail_k = float(parts[3])
            cap = parts[4].rstrip("%")
            disk_free_gb = avail_k / 1024 / 1024
            try:
                disk_used_pct = float(cap)
            except ValueError:
                disk_used_pct = 0.0
            break

    vm = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"])
    swap_total_gb = swap_used_gb = 0.0
    m = re.search(r"total\s*=\s*([\d.]+)M.*?used\s*=\s*([\d.]+)M", vm)
    if m:
        swap_total_gb = float(m.group(1)) / 1024
        swap_used_gb = float(m.group(2)) / 1024
    swap_used_pct = (swap_used_gb / swap_total_gb * 100) if swap_total_gb else 0.0

    up = _run(["/usr/bin/uptime"])
    load1 = load5 = load15 = 0.0
    lm = re.search(r"load averages?:\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", up)
    if lm:
        load1, load5, load15 = (float(lm.group(i)) for i in (1, 2, 3))

    return {
        "at": int(time.time()),
        "disk_free_gb": round(disk_free_gb, 2),
        "disk_used_pct": round(disk_used_pct, 1),
        "swap_total_gb": round(swap_total_gb, 2),
        "swap_used_gb": round(swap_used_gb, 2),
        "swap_used_pct": round(swap_used_pct, 1),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
    }


def evaluate(s: dict, prev_free: float | None) -> list[dict]:
    hits: list[dict] = []
    free = s["disk_free_gb"]
    if free <= DISK_FREE_CRIT_GB:
        hits.append({"metric": "disk_free_gb", "severity": "critical", "threshold": DISK_FREE_CRIT_GB, "cmp": "below", "value": free})
    elif free <= DISK_FREE_WARN_GB:
        hits.append({"metric": "disk_free_gb", "severity": "warn", "threshold": DISK_FREE_WARN_GB, "cmp": "below", "value": free})
    if prev_free is not None:
        drop = prev_free - free
        if drop >= DISK_DROP_ALERT_GB:
            hits.append({"metric": "disk_drop_gb", "severity": "warn", "threshold": DISK_DROP_ALERT_GB, "cmp": "above", "value": round(drop, 2)})
    # NOT `or swap_total_gb >= SWAP_TOTAL_GB`.  macOS grows the swap store on demand,
    # so once it passed 8G that disjunct was true forever and every tick reported a hit.
    # Absolute used-swap is the real signal; the store's size is not.
    if s["swap_used_pct"] >= SWAP_USED_PCT and s["swap_used_gb"] >= SWAP_USED_GB:
        hits.append({
            "metric": "swap",
            "severity": "critical" if s["swap_used_pct"] >= 90 else "warn",
            "threshold": SWAP_USED_PCT,
            "cmp": "above",
            "value": s["swap_used_pct"],
            "swap_used_gb": s["swap_used_gb"],
            "swap_total_gb": s["swap_total_gb"],
        })
    if CPU_ENABLED and s["load1"] >= LOAD1_THRESHOLD:
        hits.append({"metric": "load_1m", "severity": "warn", "threshold": LOAD1_THRESHOLD, "cmp": "above", "value": s["load1"]})
    return hits


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def _read_env_file(path: Path, url_key: str, secret_key: str) -> tuple[str, str]:
    url = os.environ.get(url_key, "").strip()
    secret = os.environ.get(secret_key, "").strip()
    if path.is_file() and not (url and secret):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(url_key + "=") and not url:
                url = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith(secret_key + "=") and not secret:
                secret = line.split("=", 1)[1].strip().strip('"')
    return url, secret


def load_webhooks() -> list[tuple[str, str, str]]:
    """Return list of (name, url, secret).  Never print url/secret."""
    hooks: list[tuple[str, str, str]] = []
    bf_url, bf_secret = _read_env_file(
        SECRET_FILE, "BOTFLEET_HOUSEKEEPER_WEBHOOK_URL", "BOTFLEET_HOUSEKEEPER_WEBHOOK_SECRET"
    )
    if bf_url and bf_secret:
        hooks.append(("BotFleet", bf_url, bf_secret))
    gb_url, gb_secret = _read_env_file(
        GB_SECRET_FILE, "GROK_BOT_HOUSEKEEPER_WEBHOOK_URL", "GROK_BOT_HOUSEKEEPER_WEBHOOK_SECRET"
    )
    if gb_url and gb_secret:
        hooks.append(("GrokBot", gb_url, gb_secret))
    return hooks


def run_cleanup(pressure: bool) -> int:
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + env.get("PATH", "")
    # We already hold the housekeeper lock (acquire_lock above); mark ourselves the owner
    # so the child scripts do not deadlock against it and skip themselves.
    env["HOUSEKEEPER_LOCK_OWNER"] = "1"
    args = ["/bin/bash", str(CLEANUP)]
    if pressure:
        args.append("--pressure")
        env["MAC_CLEANUP_PRESSURE"] = "1"
    rc = 0
    if CLEANUP.is_file():
        try:
            rc = subprocess.call(args, env=env, timeout=900)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"cleanup timeout/error: {exc}")
            rc = 1
    if pressure and JANITOR.is_file():
        try:
            subprocess.call(["/bin/bash", str(JANITOR)], env=env, timeout=900)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"janitor timeout/error: {exc}")
    # mac-auto-cleanup.sh --pressure already runs `cleanmymac clean` when the CLI
    # exists.  Do not run a second full CleanMyMac pass here (Codex #156 P2).
    # Never `optimize ram` unless RESOURCE_ALLOW_RAM_OPTIMIZE=1.  It purges
    # resident pages into swapfiles on the disk we are trying to free.
    if pressure and os.environ.get("RESOURCE_ALLOW_RAM_OPTIMIZE") == "1":
        cmm = _run(["/usr/bin/which", "cleanmymac"]).strip() or "/opt/homebrew/bin/cleanmymac"
        if Path(cmm).is_file():
            subprocess.call([cmm, "optimize", "ram"], timeout=120)
    return rc


def _event_name(hits: list[dict]) -> str:
    event = hits[0]["metric"]
    if event.startswith("disk"):
        return "resource.disk"
    if event.startswith("swap"):
        return "resource.ram"
    if event.startswith("load"):
        return "resource.cpu"
    return "resource.pressure"


def _webhook_started(raw: str, status: int) -> tuple[bool, str]:
    """202 is returned for accepted / ignored / captured / duplicate alike.

    Only a payload carrying runId (and not ignored/captured) actually starts a bot.
    Treating every 2xx as delivered set last_webhook_at and suppressed the next
    45 min on a no-op.
    """
    if not (200 <= status < 300):
        return False, f"http={status}"
    try:
        body = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return False, "unparseable body"
    if not isinstance(body, dict):
        return False, "non-object body"
    started = bool(body.get("runId")) and not body.get("ignored") and not body.get("captured")
    detail = str(body.get("reason") or body.get("status") or "")[:120]
    return started, detail


def fire_webhook(hits: list[dict], sample_data: dict, cleaned: bool) -> bool:
    hooks = load_webhooks()
    if not hooks:
        log("webhooks skipped: no valid urls/secrets found")
        return False
    event_name = _event_name(hits)
    payload = {
        "event": event_name,
        "task": (
            "Host resource pressure.  Run the Housekeeper cleanup playbook now.  "
            "Do not only report.  Regenerable caches and CleanMyMac are in scope."
        ),
        "hits": hits,
        "sample": sample_data,
        "cleanup_already_ran": cleaned,
        # NOTE: no `cleanmymac optimize ram`.  It purges resident pages into swapfiles
        # on the same APFS container as user data, turning RAM pressure into disk
        # consumption -- the opposite of the point.  Measured +3.6-7.3G swap per run.
        "playbook": [
            "python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook",
            "bash /Users/jay/apps/mac-auto-cleanup.sh --pressure",
            "bash /Users/jay/.claude-disk-janitor/janitor.sh",
            "cleanmymac clean --force",
        ],
        "never": [
            "cleanmymac optimize ram (converts RAM pressure into swapfile disk usage)",
            "deleting any git worktree, node_modules, or anything under ~/Library/CloudStorage",
            "bare `ps` or `pgrep -l/-fl` (both print argv, which carries live API keys here)",
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    any_started = False
    for name, url, secret in hooks:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
                # launchd receiver on :8800 historically read only x-webhook-event;
                # BotFleet origin/main (PR #78) also accepts x-botfleet-event.
                "X-BotFleet-Event": event_name,
                "X-Webhook-Event": event_name,
                "Idempotency-Key": f"resource-{sample_data['at']}-{event_name}-{name}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8, context=ssl.create_default_context()) as resp:
                raw = resp.read(4096).decode("utf-8", "replace")
                status = resp.status
            started, detail = _webhook_started(raw, status)
            log(
                f"webhook {name} {event_name} http={status} started={started} "
                f"{('reason=' + detail) if detail and not started else ''} "
                f"hits={[h['metric'] for h in hits]}"
            )
            if started:
                any_started = True
        except urllib.error.HTTPError as exc:
            log(f"webhook {name} http={exc.code} (secret_len={len(secret)})")
        except Exception as exc:
            log(f"webhook {name} error: {type(exc).__name__}")
    return any_started


def acquire_lock() -> bool:
    """Single-flight.  A cleanup run can outlive the 5-min StartInterval."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        if LOCK_FILE.exists():
            age = now - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SEC:
                log(f"another run holds the lock ({int(age)}s old); skipping")
                return False
            log(f"stealing stale lock ({int(age)}s old)")
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError as exc:
        log(f"lock error: {type(exc).__name__}; proceeding")
        return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--clean", action="store_true", help="run cleanup even without a hit")
    p.add_argument("--force", action="store_true", help="bypass the cleanup cooldown (manual use only)")
    p.add_argument("--no-webhook", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not args.dry_run and not acquire_lock():
        return 0
    try:
        return _main_body(args)
    finally:
        if not args.dry_run:
            release_lock()


def _main_body(args) -> int:
    s = sample()
    state = load_state()
    prev_free = state.get("disk_free_gb")
    hits = evaluate(s, prev_free)
    log(
        f"free={s['disk_free_gb']}G used={s['disk_used_pct']}% "
        f"swap={s['swap_used_gb']}/{s['swap_total_gb']}G ({s['swap_used_pct']}%) "
        f"load={s['load1']}/{s['load5']}/{s['load15']} hits={[h['metric'] for h in hits] or 'none'}"
    )

    critical = any(h.get("severity") == "critical" for h in hits)
    pressure = critical or any(h["metric"] in {"disk_free_gb", "disk_drop_gb"} for h in hits)

    # Cleanup is EXPENSIVE (CleanMyMac, brew, npm, a walk over every worktree).  Rate
    # limit it independently of the webhook, and never clean for a swap/load-only hit --
    # deleting caches does not reduce swap, it just burns I/O and RAM and makes it worse.
    now = int(time.time())
    last_clean = int(state.get("last_cleanup_at") or 0)
    clean_cd = CLEAN_COOLDOWN_CRIT_SEC if critical else CLEAN_COOLDOWN_SEC
    disk_hit = any(h["metric"] in {"disk_free_gb", "disk_drop_gb"} for h in hits)
    should_clean = args.clean or disk_hit
    # `--clean` means "clean even without a hit", NOT "ignore the cooldown".  All four
    # BF-Housekeeper prompt surfaces invoke `--once --clean`, so letting it bypass the
    # rate limit reopened the every-5-minutes loop through the bot.  Only --force does.
    if should_clean and not args.force and (now - last_clean) < clean_cd:
        log(f"cleanup cooldown {clean_cd - (now - last_clean)}s remaining (last_free_delta={state.get('last_clean_delta_gb')})")
        should_clean = False
    if bool(hits) and not disk_hit:
        log("pressure is swap/load only -- not a disk problem; skipping cache cleanup")

    cleaned = False
    if should_clean and not args.dry_run:
        free_before = s["disk_free_gb"]
        rc = run_cleanup(pressure=pressure or disk_hit)
        cleaned = rc == 0
        s_after = sample()
        delta = round(s_after["disk_free_gb"] - free_before, 2)
        swap_delta = round(s_after["swap_used_gb"] - s["swap_used_gb"], 2)
        log(
            f"after_clean free={s_after['disk_free_gb']}G (delta {delta:+}G) "
            f"swap={s_after['swap_used_gb']}G (delta {swap_delta:+}G) load={s_after['load1']}"
        )
        state["last_cleanup_at"] = now
        state["last_clean_delta_gb"] = delta
        state["last_clean_swap_delta_gb"] = swap_delta
        # Counterproductive run: back off hard so we stop digging.
        if delta <= 0:
            state["last_cleanup_at"] = now + max(0, 3 * CLEAN_COOLDOWN_SEC - clean_cd)
            log(f"cleanup FREED NOTHING ({delta:+}G, swap {swap_delta:+}G) -- extending cooldown 3x")
        s = s_after

    now = int(time.time())
    last_fire = int(state.get("last_webhook_at") or 0)
    fire = bool(hits) and not args.no_webhook and not args.dry_run
    if fire and (now - last_fire) < COOLDOWN_SEC:
        log(f"webhook cooldown {COOLDOWN_SEC - (now - last_fire)}s remaining")
        fire = False
    fired = False
    if fire:
        fired = fire_webhook(hits, s, cleaned)
        if fired:
            state["last_webhook_at"] = now

    state.update({"disk_free_gb": s["disk_free_gb"], "sample": s, "hits": hits, "ts": now})
    save_state(state)
    return 0 if not hits or cleaned or fired else 1


if __name__ == "__main__":
    sys.exit(main())
