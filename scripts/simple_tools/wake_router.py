"""wake_router: the ONE router for every platform (OPS-1048).

The shared core the owner ruled into existence, Nov 10-11:  one router, one letterer (the
Operator registry, keyed by platform), and the ONLY per-platform code is the delivery
mechanism - how a chat is woken once the router has decided if and where it may deliver.
A platform registers a RouterSpec (seat, display name, its delivery command template) and
calls serve_with(spec);  everything else - the signed POST receiver, dedupe and in-flight
reservation, the deterministic pick (letter -> topic words -> recency, ISO-safe), the
accept-then-deliver worker, receipts - lives here, once.

Reduced onto it so far:  ZCode (scripts/zc-wake-bridge).  Antigravity's reduction is the
identical mechanical step (its delivery:  agy --conversation <uuid> -p).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from dataclasses import dataclass


@dataclass(frozen=True)
class RouterSpec:
    """A platform's ONLY unique parts:  its seat tag, its display name (how senders
    address it), and its delivery mechanism - the command template the worker runs to wake
    the picked chat ({session_id} and {wake_text} substitute)."""

    seat: str
    display: str
    delivery_argv: tuple[str, ...] = ()
    delivery_enabled: bool = True
    timeout_seconds: int = 900
    target_host: str = "server"
    letter_pattern: str = ""  # optional override of the default mention regex


SPEC: RouterSpec | None = None


CONTRACT = "simple-tools-wake/1"
SPEC = None  # set by serve_with(); the per-platform RouterSpec
DEFAULT_PORT = 8791
MAX_BODY = 64 * 1024
MAX_TITLE = 120
SEEN_CAP = 200
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# A routing target is a ZCode session id, which begins `sess_`;  the prefix keeps a crafted
# registry row from ever steering an argv token that a child could read as a flag.
TARGET_RE = re.compile(r"^sess_[A-Za-z0-9._-]{1,123}$")
#: ZCode has no public thread URL today (no share links are minted, and the app has no URI
#: scheme), so a thread's link is the stable locator convention the registry records.  The
#: day the app grows a real link, this one form is replaced and rows rewrite from session ids.
WAKE_TEXT = (
    "[{seat}\u00b7wake] You were tagged in Zulip:  #{channel} / {topic}.  {link}\n"
    "Wake id {wake_id}.  Read your inbox with `simple-chat inbox --peek`, respond in that "
    "thread, and follow your seat rules.  This prompt was delivered by the Operator's wake "
    "bridge;  the message text is not quoted here on purpose."
)
THREAD_LINK_FORM = "zcode://session/{sid}"
WAKE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
STOP_WORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "at",
        "by",
        "from",
        "zcode",
        "ops",
        "ale",
        "the",
        "this",
        "that",
    ]
)

#: Dedupe is check-and-mutate against one file:  one lock guards both halves, so two posts of
#: the same wake id cannot both pass the check (ThreadingHTTPServer answers on threads).
_SEEN_LOCK = threading.Lock()
#: Wake ids whose delivery is running now, reserved under _SEEN_LOCK.  The seen file is only
#: written after a delivery is handled (a failed one must stay retryable), so without this
#: reservation a second post of the same id during a minutes-long delivery would pass the
#: seen check and start a second ZCode turn.
_INFLIGHT: set[str] = set()

ENV_URL = "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL"
ENV_PRINCIPAL = "SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL"
ENV_KEY = "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"


DEFAULT_CONFIG = {
    "delivery_enabled": True,
    "command_template": ["zcode", "--resume", "{session_id}", "-p", "{wake_text}"],
    "timeout_seconds": 900,
    "target_host": "server",
}


def state_dir(env: dict[str, str]) -> Path:
    base = env.get("ZC_WAKE_STATE") or os.path.join(
        str(env.get("HOME") or os.path.expanduser("~")), ".local", "state"
    )
    return Path(base) / "zc-wake"


def load_config(path: Path, *, required: bool = False) -> dict[str, Any]:
    """The delivery configuration.  A file the caller named explicitly (the flag, or the env
    var) must exist and must parse:  a config that cannot be read is a refusal that names the
    path, never a silent run on defaults.  The default path may be absent, which is the
    not-yet-armed state;  a file that IS there is validated either way."""
    config = dict(DEFAULT_CONFIG)
    text = ""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise SystemExit("zc-wake-bridge: the config file %s is not there" % path)
        return config
    except OSError as exc:
        raise SystemExit(
            "zc-wake-bridge: the config file %s is unreadable (%s)" % (path, type(exc).__name__)
        )
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        raise SystemExit("zc-wake-bridge: the config file %s is not valid JSON (%s)" % (path, exc))
    if not isinstance(loaded, dict):
        raise SystemExit("zc-wake-bridge: the config file %s is not a JSON object" % path)
    for key in DEFAULT_CONFIG:
        if key not in loaded:
            continue
        value = loaded[key]
        if key == "delivery_enabled":
            if not isinstance(value, bool):
                raise SystemExit("zc-wake-bridge: %s: delivery_enabled must be a boolean" % path)
        elif key == "command_template":
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(part, str) and part for part in value)
            ):
                raise SystemExit(
                    "zc-wake-bridge: %s: command_template must be a non-empty list of strings"
                    % path
                )
        elif key == "timeout_seconds":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SystemExit(
                    "zc-wake-bridge: %s: timeout_seconds must be a positive whole number" % path
                )
        elif key == "target_host":
            if value not in ("mac", "server", ""):
                raise SystemExit(
                    "zc-wake-bridge: %s: target_host must be mac, server or empty" % path
                )
        config[key] = value
    return config


def read_key(env: dict[str, str]) -> str:
    path = Path(env.get("ZC_WAKE_KEY_FILE") or str(state_dir(env) / "routine-key"))
    try:
        key = path.read_text(encoding="utf-8").strip()
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return ""
    if mode & 0o077 or len(key) < 16:
        return ""
    return key


# --------------------------------------------------------------------------------------------
# The registry read:  which lettered sessions are live, and which one the wake belongs to.
# --------------------------------------------------------------------------------------------


def registry_history(env: dict[str, str], limit: int = 50) -> list[dict[str, Any]]:
    """The Operator registry's zcode rows, over its read API.  Empty on any failure:  a wake
    that cannot see the registry is logged undelivered, never guessed."""
    seam = (env.get("ZC_WAKE_HISTORY_FILE") or "").strip()
    if seam:
        # A test seam, named like the other stand-in variables in this repo's tests:  the rows
        # a real call would have brought back, so no network is involved in a test.
        try:
            with open(seam, encoding="utf-8") as fh:
                answer = json.load(fh)
            if isinstance(answer, list):
                return answer
            return answer.get("items") or [] if isinstance(answer, dict) else []
        except (OSError, ValueError):
            return []
    url = (env.get(ENV_URL) or "").strip()
    if not url:
        return []
    principal = (env.get(ENV_PRINCIPAL) or "").strip() or "OPERATOR"
    key = (env.get(ENV_KEY) or "").strip()
    if not key:
        return []
    from simple_tools import listener_registry as RG

    path = RG.PREFIX + "history"
    query = "platform=zcode&limit=%d" % limit
    try:
        parts = urllib.parse.urlsplit(url)
        target = url.rstrip("/") + path + "?" + query
        request = urllib.request.Request(target, method="GET")
        request.add_header("User-Agent", USER_AGENT)
        headers = RG.headers(key, "GET", path, query, principal, b"", time.time())
        for name, value in headers.items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=10) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, KeyError):
        return []
    rows = answer.get("rows") or answer.get("records") or answer.get("items") or []
    return rows if isinstance(rows, list) else []


#: The registry API's host is Cloudflare-fronted, and Cloudflare refuses a bare urllib
#: signature (error 1010) - the same reason registry_api.py pins an explicit agent.  Every
#: request this bridge sends carries it.
USER_AGENT = "simple-tools-registry/1.0"


#: How a letter is read out of a wake:  the mention forms first (the daemon escapes the
#: display copy, so both the @**ZCode** AA form and a plain "ZCode AA" are accepted), and an
#: explicit payload field wins over both when a later daemon sends one.
def _letter_re() -> re.Pattern[str]:
    if SPEC.letter_pattern:
        return re.compile(SPEC.letter_pattern)
    return letter_re(SPEC.display)


def letter_re(display: str) -> re.Pattern[str]:
    star = re.escape(display)
    plain = re.escape(display.split()[0])
    return re.compile(r"@\*\*" + star + r"\*\*\s*([A-Z]{2})\b|\b" + plain + r"[ \t]+([A-Z]{2})\b")


def letter_of(payload: Mapping[str, Any]) -> str:
    """The session letter a sender addressed this wake to, or "".  An explicit field wins;
    else the first letter-shaped pair in the excerpt's mention forms."""
    explicit = str(payload.get("letter") or "").strip()
    if re.fullmatch(r"[A-Z]{2}", explicit):
        return explicit
    match = _letter_re().search(str(payload.get("excerpt") or ""))
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "") if match else ""


def resolve_letter(env: dict[str, str], letter: str) -> str:
    """The live native session id a letter resolves to on this platform, or "".

    The Operator's registry is the one allocator, so the bridge never guesses:  a letter
    that resolves to nothing (never issued, retired, or its registration closed) delivers
    nowhere, and the caller says so in its receipt."""
    url = (env.get(ENV_URL) or "").strip()
    key = (env.get(ENV_KEY) or "").strip()
    if not url or not key or not re.fullmatch(r"[A-Z]{2}", letter):
        return ""
    from simple_tools import listener_registry as RG

    path = RG.PREFIX + "resolve"
    query = "platform=zcode&letter=%s" % letter
    principal = (env.get(ENV_PRINCIPAL) or "").strip() or "OPERATOR"
    try:
        request = urllib.request.Request(url.rstrip("/") + path + "?" + query, method="GET")
        request.add_header("User-Agent", USER_AGENT)
        for name, value in RG.headers(key, "GET", path, query, principal, b"", time.time()).items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=10) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return ""
    if not isinstance(answer, dict) or not answer.get("found"):
        return ""
    sid = str(answer.get("native_session_id") or "")
    return sid if TARGET_RE.fullmatch(sid) else ""


def _as_time(value: object) -> float:
    """A row's assigned_at as a comparable epoch float:  the registry writes ISO timestamps,
    a test fixture may write a bare number, and both must rank against each other (a bare
    float() on the ISO string is the crash Claude AA's read-only pass caught before it bit)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _words(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text or "") if w.lower() not in STOP_WORDS}


def pick_target(
    rows: list[dict[str, Any]],
    topic: str,
    store: Path,
    target_host: str,
    letter_sid: str = "",
) -> tuple[str, str]:
    """(native session id, why) for the best live row, or ("", reason).  A letter the sender
    addressed wins outright (the registry already resolved it to one live session), passing
    only the same safety filter as every other candidate:  the store must not call it a
    sub-agent.  Otherwise rows are ranked by topic words over titles, else recency, and rows
    that live on another host never win.  The store is opened once for the whole scan, and a
    target id always begins `sess_`:  a crafted row can never steer an argv token a child
    would read as a flag."""
    if letter_sid:
        # Cross-host BY DESIGN:  the sender addressed one chat and the registry said which -
        # routing is registry-level, nothing executes locally, so a letter that resolves to a
        # session on the other host is still the right pick (its letter, its receipt).
        # Liveness is already enforced behind it:  resolve answers only a live holder, so a
        # retired or closed letter resolves nothing and the deterministic rules take over.
        if letter_sid in _subagent_ids(store, [letter_sid]):
            return "", "letter-target-is-subagent"
        return letter_sid, "letter"
    topic_words = _words(topic)
    best: tuple[float, float, str] = (0.0, 0.0, "")
    fallback: tuple[float, str] = (0.0, "")
    candidates: list[tuple[float, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("retired_at") or row.get("closed_at") or str(row.get("state") or "") == "closed":
            continue  # a retired letter or a closed registration is not a chat to wake
        host = str(row.get("verified_host") or "")
        if target_host and host and host != target_host:
            continue
        sid = str(row.get("native_session_id") or "")
        if not TARGET_RE.fullmatch(sid):
            continue
        candidates.append((_as_time(row.get("assigned_at")), sid, str(row.get("title") or "")))
    subagents = _subagent_ids(store, [sid for _, sid, _ in candidates])
    for seen, sid, title in candidates:
        if sid in subagents:
            continue
        title_words = _words(title)
        overlap = len(topic_words & title_words) if topic_words else 0
        if overlap > 0 and (float(overlap), seen) > best[:2]:
            best = (float(overlap), seen, sid)
        if seen > fallback[0]:
            fallback = (seen, sid)
    if best[2]:
        return best[2], "topic-match"
    if fallback[1]:
        return fallback[1], "most-recent"
    return "", "no-live-session"


def _subagent_ids(store: Path, sids: list[str]) -> set[str]:
    """The ids among `sids` the store calls sub-agent children, in one query:  a scan over the
    live registrations opens the store once, not once per candidate row."""
    if not sids:
        return set()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % store, uri=True, timeout=5)
    except sqlite3.Error:
        return set()
    try:
        placeholders = ",".join("?" * len(sids))
        rows = conn.execute(
            "SELECT id FROM session WHERE task_type = 'subagent_child' AND id IN (%s)"
            % placeholders,
            sids,
        ).fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    except sqlite3.Error:
        return set()
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------------------------
# Delivery:  one prompt into one session, once, with a lock and a receipt.
# --------------------------------------------------------------------------------------------


def seen_ids(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def remember_id(path: Path, wake_id: str) -> None:
    """One fsynced private file per rewrite, the way the letter installer writes:  the dedupe
    state survives a crash whole or not at all, and a torn temp never replaces the file."""
    ids = sorted(seen_ids(path) | {wake_id})[-SEEN_CAP:]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(ids) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def session_lock(store_dir: Path, sid: str):
    """An exclusive mark per target session, so one wake waits rather than two turns clobber
    one thread.  The lock lives on a descriptor this process holds for the delivery."""
    store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return open(
        os.open(str(store_dir / ("session-%s.lock" % sid)), os.O_CREAT | os.O_RDWR, 0o600), "w"
    )


def record(
    env: dict[str, str],
    config: dict[str, Any],
    *,
    wake_id: str,
    topic: str,
    channel: str,
    link: str,
    letter: str = "",
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Route the wake deterministically and record the pick.  Never raises, never messages
    anyone, never runs a turn in a chat (owner ruling, Nov 10:  the bridge routes;  the only
    thing a model may ever add is the PICK, in the minority of cases the deterministic rules
    cannot decide, and its only outbound message is a clarification question).

    The picked chat is surfaced, not delivered:  the message is already in the seat's inbox,
    the Operator's pending-inbox tells a live session, and this receipt is the record."""
    store = Path(
        env.get("ZC_WAKE_STORE")
        or os.path.join(
            str(env.get("HOME") or os.path.expanduser("~")), ".zcode", "cli", "db", "db.sqlite"
        )
    )
    letter_sid = resolve_letter(env, letter) if letter else ""
    reason = ""
    sid = ""
    picked = ""
    rows: list = []
    if not letter_sid:
        # No letter, or one that does not resolve live:  the deterministic rules stand either
        # way.  A dead letter never dead-ends the wake - the mention forms are a regex, and a
        # pair like "PR" after the seat name may be a word, so an unresolved letter falls
        # back to topic words and recency with the dead letter recorded in the receipt.
        try:
            rows = registry_history(env)
        except Exception:  # noqa: BLE001 - an unreadable registry is an unpicked wake, never a crash
            rows = []
    sid, picked = pick_target(
        rows, topic, store, str(config.get("target_host") or "server"), letter_sid
    )
    if not sid:
        reason = picked  # no-live-session / letter-target-is-subagent
        picked = ""
    elif letter and not letter_sid:
        reason = "letter-not-live-fell-back"
    if sid and not config.get("delivery_enabled"):
        pass  # the receipt below finalizes as delivery-disabled

    receipt = {
        "at": time.time(),
        "wake_id": wake_id,
        "letter": letter or "",
        "session_id": sid,
        "thread_link": THREAD_LINK_FORM.format(sid=sid) if sid else "",
        "picked": picked,
        "reason": "delivery-disabled" if (sid and not config.get("delivery_enabled")) else reason,
        "delivered": None if (sid and config.get("delivery_enabled")) else False,
        "channel": channel or "-",
        "topic": topic or "-",
        "zulip_link": link or "",
    }
    _append_receipt(env, receipt, log_path)
    return receipt


def _append_receipt(env, receipt, log_path) -> None:
    if log_path is None:
        log_path = state_dir(env) / "wakes.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, sort_keys=True) + "\n")
    except OSError:
        pass


def deliver_receipt(env, config, receipt, log_path=None):
    """The worker side of accept-then-deliver (BB's shape): the POST answers 202 once the
    wake is deduped, in-flight-checked and routed; this runs the resume off the handler
    thread, owns the retries the listener no longer makes, and finalizes the receipt."""
    import fcntl
    import subprocess

    sid = str(receipt.get("session_id") or "")
    attempts = 3
    while True:
        delivered = False
        delivery_reason = "no-command"
        text = WAKE_TEXT.format(
            seat=SPEC.seat,
            channel=receipt.get("channel") or "-",
            topic=receipt.get("topic") or "-",
            link=receipt.get("zulip_link") or "",
            wake_id=receipt.get("wake_id") or "",
        )
        argv = [
            str(part).replace("{session_id}", sid).replace("{wake_text}", text)
            for part in (config.get("command_template") or [])
        ]
        if argv and sid:
            with session_lock(state_dir(env), sid) as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    delivered, delivery_reason = False, "session-busy"
                else:
                    spawn_env = {
                        "HOME": env.get("HOME") or os.path.expanduser("~"),
                        "PATH": env.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
                        "AGENT_LAUNCHER": "simple-tools-wake",
                        "AGENT_LAUNCH_SEAT": SPEC.seat,
                    }
                    try:
                        proc = subprocess.run(
                            argv,
                            env=spawn_env,
                            capture_output=True,
                            timeout=int(config.get("timeout_seconds") or 900),
                            check=False,
                        )
                        delivered = proc.returncode == 0
                        delivery_reason = (
                            "ok" if delivered else "command-failed-%d" % proc.returncode
                        )
                    except subprocess.TimeoutExpired:
                        delivered, delivery_reason = False, "command-timeout"
                    except OSError as exc:
                        delivered, delivery_reason = False, "command-error-%s" % type(exc).__name__
        receipt = dict(receipt, delivered=delivered, reason=delivery_reason)
        attempts -= 1
        if delivered or attempts <= 0 or delivery_reason not in ("session-busy", "command-timeout"):
            break
        time.sleep(5)
    _append_receipt(env, receipt, log_path)
    return receipt


# --------------------------------------------------------------------------------------------
# The HTTP receiver.
# --------------------------------------------------------------------------------------------


def serve_with(spec: RouterSpec, argv: list[str] | None = None) -> int:
    """Run the one router for one platform:  set the spec, then the normal CLI
    (serve / record), with the spec's delivery fields folded into the config."""
    global SPEC
    SPEC = spec
    parsed = main(argv)
    return parsed


def make_handler(env: dict[str, str], config: dict[str, Any], receipts: list[dict[str, Any]]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # default logging prints paths
            pass

        def _answer(self, code: int, payload: dict[str, Any]) -> None:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._answer(200, {"ok": True, "seat": SPEC.seat})
            else:
                self._answer(404, {"error": "not found"})

        def do_POST(self) -> None:
            key = read_key(env)
            if not key:
                self._answer(503, {"error": "the routine key is missing or unreadable"})
                return
            auth = self.headers.get("Authorization") or ""
            # Constant time:  the bearer is the only thing between the tailnet and a wake.
            if not hmac.compare_digest(auth.encode("utf-8"), ("Bearer %s" % key).encode("utf-8")):
                self._answer(401, {"error": "bad bearer"})
                return
            if not re.fullmatch(r"/wake/[A-Z0-9][A-Z0-9_-]{0,31}", self.path or ""):
                self._answer(404, {"error": "not found"})
                return
            raw_length = (self.headers.get("Content-Length") or "").strip()
            if not raw_length.isdigit() or len(raw_length) > 9:
                self._answer(400, {"error": "Content-Length missing or malformed"})
                return
            length = int(raw_length)
            if length <= 0 or length > MAX_BODY:
                self._answer(413, {"error": "body size refused"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._answer(400, {"error": "body is not JSON"})
                return
            if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
                self._answer(400, {"error": "unknown contract"})
                return
            if str(payload.get("seat") or "") != SPEC.seat:
                self._answer(403, {"error": "seat mismatch"})
                return
            wake_id = str(payload.get("wake_id") or "")
            if not WAKE_ID_RE.fullmatch(wake_id):
                self._answer(400, {"error": "wake_id missing or malformed"})
                return
            seen_path = state_dir(env) / "seen.json"
            with _SEEN_LOCK:
                if wake_id in seen_ids(seen_path):
                    self._answer(
                        200, {"delivered": False, "reason": "already-seen", "wake_id": wake_id}
                    )
                    return
                if wake_id in _INFLIGHT:
                    # The first post of this id is still routing;  its own answer is the
                    # record, so this duplicate starts nothing.
                    self._answer(200, {"reason": "in-flight", "wake_id": wake_id})
                    return
                _INFLIGHT.add(wake_id)
            handled = False
            try:
                receipt = record(
                    env,
                    config,
                    wake_id=wake_id,
                    topic=str(payload.get("topic") or ""),
                    channel=str(payload.get("channel") or ""),
                    link=str(payload.get("zulip_link") or ""),
                    letter=letter_of(payload),
                )
                # BB's shape: the POST answers as soon as the wake is deduped, in-flight-checked
                # and ROUTED; the delivery runs off the handler thread (the listener's own
                # timeout is 15-60s and cannot wait a resume).  Accepted-and-routed is 202;
                # every not-handled case keeps the id unmarked so the listener retries.
                handled = bool(receipt.get("session_id"))
            finally:
                with _SEEN_LOCK:
                    _INFLIGHT.discard(wake_id)
                    if handled:
                        remember_id(seen_path, wake_id)
            receipts.append({"wake_id": wake_id, **receipt})
            del receipts[:-50]
            if handled:
                if receipt.get("delivered") is None and config.get("delivery_enabled"):
                    import threading

                    threading.Thread(
                        target=deliver_receipt,
                        args=(env, config, dict(receipt)),
                        daemon=True,
                    ).start()
                self._answer(202, {"wake_id": wake_id, **receipt})
            else:
                self._answer(503, {"wake_id": wake_id, **receipt})

    return Handler


def serve(env: dict[str, str], bind: str, port: int, config: dict[str, Any]) -> int:
    # Fail fast, at boot:  a bridge whose registry configuration is missing would answer every
    # wake "no-live-session" and look healthy doing it.  The unit restarts, and systemd holds
    # the refusal.  (After boot, a registry that stops answering is the request-time empty
    # answer:  an outage is an undelivered wake, not a crash.)
    for name in (ENV_URL, ENV_KEY):
        if not (env.get(name) or "").strip():
            raise SystemExit(
                "zc-wake-bridge: %s is not set:  the registry environment is missing"
                " (source the host's shared registry env file first)" % name
            )
    if not read_key(env):
        raise SystemExit(
            "zc-wake-bridge: the routine key file is missing, short, or group-readable"
            " (ZC_WAKE_KEY_FILE, default %s)" % (state_dir(env) / "routine-key")
        )
    receipts: list[dict[str, Any]] = []
    server = ThreadingHTTPServer((bind, port), make_handler(env, config, receipts))
    server.daemon_threads = True
    print(
        "wake_router listening on %s:%d for seat %s" % (bind, port, SPEC.seat if SPEC else "-"),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    env = dict(os.environ)
    parser = argparse.ArgumentParser(prog="zc-wake-bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--bind", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_deliver = sub.add_parser("record")
    p_deliver.add_argument("--wake-id", required=True)
    p_deliver.add_argument("--topic", default="")
    p_deliver.add_argument("--channel", default="")
    p_deliver.add_argument("--link", default="")
    p_deliver.add_argument("--letter", default="")
    for p in (p_serve, p_deliver):
        p.add_argument("--config", default="")
    args = parser.parse_args(argv)
    config_path = Path(
        args.config or env.get("ZC_WAKE_CONFIG") or str(state_dir(env) / "config.json")
    )
    config = load_config(config_path, required=bool(args.config or env.get("ZC_WAKE_CONFIG")))
    if SPEC is not None:
        config.setdefault("delivery_enabled", SPEC.delivery_enabled)
        if not config.get("command_template"):
            config["command_template"] = list(SPEC.delivery_argv)
        config.setdefault("timeout_seconds", SPEC.timeout_seconds)
        config.setdefault("target_host", SPEC.target_host)
    if args.cmd == "serve":
        return serve(env, args.bind, args.port, config)
    receipt = record(
        env,
        config,
        wake_id=args.wake_id,
        topic=args.topic,
        channel=args.channel,
        link=args.link,
        letter=args.letter,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
