"""Single-reader WhatsApp -> Jet bridge.  Never sends outbound WhatsApp messages.

Credentials come only from the approved runtime environment.  Starting this
program with live credentials is an activation step requiring separate approval.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_RESPONSE = 8 * 1024 * 1024
MAX_OFFSET = 2**63 - 1


class BridgeError(Exception):
    """Fixed, content-free failure code."""


class StopBridge(BridgeError):
    pass


def digest(value, key=None):
    if key is None:
        raise StopBridge("missing_hash_key")
    derived = hmac.new(key.encode(), b"wa-id-hashes/1", hashlib.sha256).digest()
    return hmac.new(derived, value.encode(), hashlib.sha256).hexdigest()


def bounded_tree(value):
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 16 or count > 20000:
            raise BridgeError("schema_limits")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def normalize(payload, offset, hash_key):
    """Meta manual's boundary schema; preserves signed 64-bit offsets exactly."""
    bounded_tree(payload)
    try:
        if payload["object"] != "whatsapp_agent_platform" or len(payload["entry"]) != 1:
            raise ValueError
        entry = payload["entry"][0]
        if (
            not isinstance(entry["id"], str)
            or not entry["id"].isascii()
            or not entry["id"].isdigit()
            or len(entry["changes"]) != 1
        ):
            raise ValueError
        change = entry["changes"][0]
        value = change["value"]
        if change["field"] != "messages" or value["messaging_product"] != "whatsapp":
            raise ValueError
        nxt = payload["next_offset"]
        if type(nxt) is not int or not offset <= nxt <= MAX_OFFSET:
            raise ValueError
        messages, statuses = value.get("messages", []), value.get("statuses", [])
        if (
            not isinstance(messages, list)
            or not isinstance(statuses, list)
            or len(messages) + len(statuses) > 100
        ):
            raise ValueError
        result = []
        for m in messages:
            if (
                not isinstance(m["id"], str)
                or not m["id"].startswith("wamid.")
                or not 6 < len(m["id"]) <= 512
            ):
                raise ValueError
            if (
                not isinstance(m["from"], str)
                or not m["from"].startswith("user:")
                or not 5 < len(m["from"]) <= 128
            ):
                raise ValueError
            if (
                not isinstance(m["timestamp"], str)
                or not m["timestamp"].isascii()
                or not m["timestamp"].isdigit()
            ):
                raise ValueError
            if not isinstance(m["type"], str) or not 0 < len(m["type"]) <= 64:
                raise ValueError
            text = m["text"]["body"] if m["type"] == "text" else m.get(m["type"], {}).get("caption")
            if text is not None and (not isinstance(text, str) or len(text) > 4096):
                raise ValueError
            result.append(
                {
                    "id_hash": digest(m["id"], hash_key),
                    "sender_hash": digest(m["from"], hash_key),
                    "timestamp": int(m["timestamp"]),
                    "type": m["type"],
                    "text": text,
                }
            )
        for s in statuses:
            if (
                not isinstance(s["id"], str)
                or not isinstance(s["status"], str)
                or not s["status"]
                or not s["recipient_id"].startswith("user:")
                or not s["timestamp"].isascii()
                or not s["timestamp"].isdigit()
            ):
                raise ValueError
        return {
            "agent_hash": digest(entry["id"], hash_key),
            "messages": result,
            "next_offset": str(nxt),
        }
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise BridgeError("schema_error") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, headers, body=None):
    request = urllib.request.Request(
        url,
        data=body,
        headers={**headers, "User-Agent": "AFC-WhatsApp-Bridge/1", "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=40) as response:
            if response.status == 204:
                return None
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise BridgeError("response_too_large")
            try:
                return json.loads(data)
            except (ValueError, RecursionError):
                raise BridgeError("invalid_json") from None
    except urllib.error.HTTPError as error:
        status = error.code
        retry = error.headers.get("Retry-After", "30")
        error.close()
        if 400 <= status < 500 and status not in (408, 429):
            # Stand down on competing pollers or rejected credentials.  Never
            # recover by fetching another credential or fighting the poller.
            raise StopBridge(f"http_{status}") from None
        if 300 <= status < 400:
            raise StopBridge("redirect_refused") from None
        if status == 429:
            raise BridgeError(f"rate_limited:{retry if retry.isdigit() else '30'}") from None
        raise BridgeError(f"http_{status}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BridgeError("network_error") from None


class State:
    def __init__(self, directory):
        directory = Path(os.path.abspath(directory))
        for component in (directory, *directory.parents):
            # macOS has root-owned /var and /tmp aliases; the untrusted target
            # and every other symlink remain forbidden.
            trusted_alias = (
                component in (Path("/var"), Path("/tmp")) and component.lstat().st_uid == 0
            )
            if component.is_symlink() and not trusted_alias:
                raise StopBridge("unsafe_state_directory")
        for name in ("poller.lock", "state.sqlite3"):
            if (directory / name).is_symlink():
                raise StopBridge("unsafe_state_file")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.lock = (directory / "poller.lock").open("a")
        os.chmod(directory / "poller.lock", 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise StopBridge("poller_already_running") from None
        self.db = sqlite3.connect(directory / "state.sqlite3")
        os.chmod(directory / "state.sqlite3", 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK(id=1), offset TEXT NOT NULL)"
        )
        self.db.commit()

    def begin_binding(self, signing_key, now, code=None):
        import secrets

        code = code or "JET-" + secrets.token_hex(8).upper()
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS binding_window (id INTEGER PRIMARY KEY, code_hash TEXT, opened INTEGER, expires INTEGER, spent INTEGER)"
            )
            self.db.execute(
                "INSERT OR REPLACE INTO binding_window VALUES(1,?,?,?,0)",
                (digest(code, signing_key), int(now), int(now) + 600),
            )
        return code

    def binding_window(self, code, signing_key, now):
        row = self.db.execute(
            "SELECT code_hash,opened,expires,spent FROM binding_window WHERE id=1"
        ).fetchone()
        if (
            not row
            or row[3]
            or not row[1] <= now <= row[2]
            or not hmac.compare_digest(row[0], digest(code, signing_key))
        ):
            raise StopBridge("binding_window_closed")
        return row[1], row[2]

    def spend_binding(self, code, signing_key, now):
        window = self.binding_window(code, signing_key, now)
        with self.db:
            self.db.execute("UPDATE binding_window SET spent=1 WHERE id=1")
        return window

    def offset(self):
        row = self.db.execute("SELECT offset FROM cursor WHERE id=1").fetchone()
        return int(row[0]) if row else 0

    def save(self, offset):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cursor VALUES(1, ?)", (str(offset),))

    def close(self):
        self.db.close()
        self.lock.close()


class Bridge:
    def __init__(
        self, state, api_key, signing_key, worker_url, request=request_json, clock=time.time
    ):
        parsed = urllib.parse.urlsplit(worker_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise StopBridge("invalid_worker_url")
        if not api_key or len(signing_key.strip()) < 32:
            raise StopBridge("missing_credentials")
        self.state, self.api_key, self.signing_key = state, api_key, signing_key.strip()
        self.worker_url, self.request, self.clock = worker_url.rstrip("/"), request, clock

    def step(self):
        offset = self.state.offset()
        poll_url = "https://api.whatsapp.com/agent/v1/updates?" + urllib.parse.urlencode(
            {"offset": offset, "timeout": 25, "limit": 100}
        )
        payload = self.request(poll_url, {"Authorization": f"Bearer {self.api_key}"})
        batch = (
            normalize(payload, offset, self.signing_key)
            if payload is not None
            else {
                "agent_hash": os.environ.get("WA_AGENT_HASH_JET", ""),
                "messages": [],
                "next_offset": str(offset),
            }
        )
        sender_hash = os.environ.get("WA_SENDER_HASH_JET", "")
        if not sender_hash:
            raise StopBridge("missing_owner_binding")
        batch["messages"] = [m for m in batch["messages"] if m["sender_hash"] == sender_hash]
        for message in batch["messages"]:
            if message["text"] is not None:
                for value in (self.api_key, self.signing_key):
                    if len(value) >= 8:
                        message["text"] = message["text"].replace(
                            value, "[redacted:loaded credential]"
                        )
        body = json.dumps(
            {"contract": "wa-ingest/1", "seat": "JET", "sent_at": int(self.clock()), **batch},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        signature = hmac.new(self.signing_key.encode(), body, hashlib.sha256).hexdigest()
        ack = self.request(
            self.worker_url + "/internal/wa-ingest/JET",
            {"Content-Type": "application/json", "X-WA-Bridge-Signature": signature},
            body,
        )
        if (
            not isinstance(ack, dict)
            or ack.get("ok") is not True
            or ack.get("processed") is not True
            or ack.get("next_offset") != batch["next_offset"]
        ):
            raise BridgeError("ingest_not_acknowledged")
        self.state.save(batch["next_offset"])
        return ack


def main():
    names = (
        "WHATSAPP_API_KEY",
        "WA_BRIDGE_HMAC_KEY_JET",
        "WA_WORKER_URL",
        "WA_STATE_DIRECTORY",
        "WA_AGENT_HASH_JET",
        "WA_SENDER_HASH_JET",
    )
    for name in names:
        if not os.environ.get(name):
            raise StopBridge(f"missing_{name}_from_approved_Infisical_runtime")
    if os.environ.get("WHATSAPP_BRIDGE_ENABLED") != "on":
        raise StopBridge("disabled")
    state = State(os.environ["WA_STATE_DIRECTORY"])
    bridge = Bridge(
        state,
        os.environ["WHATSAPP_API_KEY"],
        os.environ["WA_BRIDGE_HMAC_KEY_JET"],
        os.environ["WA_WORKER_URL"],
    )
    errors = 0
    schema_errors = 0
    try:
        while not (Path(os.environ["WA_STATE_DIRECTORY"]) / "disabled").exists():
            try:
                bridge.step()
                errors = 0
                schema_errors = 0
                print('{"state":"running"}', flush=True)
            except StopBridge:
                raise
            except BridgeError as error:
                errors += 1
                print(json.dumps({"state": str(error).split(":")[0]}), flush=True)
                schema_failure = str(error).startswith(
                    ("schema_", "invalid_json", "response_too_large")
                )
                schema_errors = schema_errors + 1 if schema_failure else 0
                if schema_errors >= 3:
                    raise StopBridge("schema_stop") from None
                delay = min(120, 5 * 2 ** min(errors - 1, 5))
                if str(error).startswith("rate_limited:"):
                    delay = min(3600, max(1, int(str(error).split(":")[1])))
                # Check the local stop switch every second, including backoff.
                for _ in range(delay):
                    if (Path(os.environ["WA_STATE_DIRECTORY"]) / "disabled").exists():
                        return
                    time.sleep(1)
    finally:
        state.close()


if __name__ == "__main__":
    try:
        main()
    except StopBridge as error:
        print(json.dumps({"state": str(error)}), file=sys.stderr)
        sys.exit(1)
