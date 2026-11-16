"""Owner binding verification for an approved, supervised ceremony.

The verification helper receives a candidate batch without polling.  The
supervised binding mode temporarily owns the sole poller lock.
The owner must start the ceremony and send its one-time code.  Do not run the
creator check live without approval: it emits a WhatsApp read receipt.
"""

import json
import re
import time

from .bridge import digest, normalize, request_json
from .errors import StopBridge


def verify_candidate(
    payload,
    expected_code,
    api_key,
    opened_at,
    expires_at,
    request=request_json,
    now=time.time,
    hash_key=None,
):
    if not isinstance(expected_code, str) or not re.fullmatch(r"JET-[A-F0-9]{16}", expected_code):
        raise StopBridge("invalid_binding_code")
    observed_at = now()
    if not opened_at <= observed_at <= expires_at or expires_at - opened_at > 600:
        raise StopBridge("binding_window_closed")
    normalized = normalize(payload, 0, hash_key)
    raw_messages = payload["entry"][0]["changes"][0]["value"].get("messages", [])
    candidates = [
        (raw, msg)
        for raw, msg in zip(raw_messages, normalized["messages"])
        if msg["type"] == "text"
        and msg["text"] == expected_code
        and opened_at <= msg["timestamp"] <= min(expires_at, observed_at + 60)
    ]
    if len(candidates) != 1:
        raise StopBridge("binding_candidate_not_unique")
    raw, msg = candidates[0]
    body = json.dumps(
        {"messaging_product": "whatsapp", "status": "read", "message_id": raw["id"]},
        separators=(",", ":"),
    ).encode()
    remaining = expires_at - now()
    if remaining <= 0:
        raise StopBridge("binding_window_closed")
    answer = request(
        "https://api.whatsapp.com/agent/v1/statuses",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body,
        timeout=min(40, remaining),
    )
    if not isinstance(answer, dict) or answer.get("success") is not True:
        raise StopBridge("creator_not_verified")
    return {
        "agent_hash": normalized["agent_hash"],
        "sender_hash": digest(raw["from"], hash_key),
        "cutoff": msg["timestamp"],
        "verified_via": "code+creator_check",
    }


def bind_once(
    state, api_key, code, opened_at, expires_at, request=request_json, now=time.time, hash_key=None
):
    """Temporarily owns the same poller lock; never advances the live cursor."""
    import urllib.parse

    if not isinstance(code, str) or not re.fullmatch(r"JET-[A-F0-9]{16}", code):
        raise StopBridge("invalid_binding_code")
    if not opened_at <= now() <= expires_at or not 0 < expires_at - opened_at <= 600:
        raise StopBridge("binding_window_closed")
    state.binding_window(code, hash_key, now())
    offset = state.offset()
    attempts = {}
    while now() < expires_at:
        remaining = expires_at - now()
        if remaining <= 0:
            break
        query = urllib.parse.urlencode(
            {"offset": offset, "timeout": min(25, int(remaining)), "limit": 100}
        )
        payload = request(
            "https://api.whatsapp.com/agent/v1/updates?" + query,
            {"Authorization": f"Bearer {api_key}"},
            timeout=min(40, remaining),
        )
        if payload is None:
            continue
        batch = normalize(payload, offset, hash_key)
        raw_messages = payload["entry"][0]["changes"][0]["value"].get("messages", [])
        for raw, message in zip(raw_messages, batch["messages"]):
            text = message["text"]
            sender = message["sender_hash"]
            if (
                message["type"] != "text"
                or not opened_at <= message["timestamp"] <= min(expires_at, now() + 60)
                or not re.fullmatch(r"JET-[A-F0-9]{16}", text or "")
                or attempts.get(sender, 0) >= 3
            ):
                continue
            if text != code:
                attempts[sender] = attempts.get(sender, 0) + 1
                continue
            state.spend_binding(code, hash_key, now())
            candidate = {
                "object": "whatsapp_agent_platform",
                "entry": [
                    {
                        "id": payload["entry"][0]["id"],
                        "changes": [
                            {
                                "field": "messages",
                                "value": {
                                    "messaging_product": "whatsapp",
                                    "messages": [raw],
                                    "statuses": [],
                                },
                            }
                        ],
                    }
                ],
                "next_offset": int(batch["next_offset"]),
            }
            return verify_candidate(
                candidate, code, api_key, opened_at, expires_at, request, now, hash_key=hash_key
            )
        offset = int(batch["next_offset"])
    raise StopBridge("binding_window_closed")


def show_binding_code(code):
    try:
        with open("/dev/tty", "w", encoding="utf-8") as console:
            print(
                "Send this one-time code in the intended WhatsApp Jet chat within ten minutes: "
                + code,
                file=console,
                flush=True,
            )
    except OSError:
        raise StopBridge("binding_console_unavailable") from None


def failure(error):
    """Report fixed repair targets without exposing provider/credential text."""
    code = str(error)
    if code in {"binding_configuration_missing", "missing_hash_key"}:
        source = "approved_Infisical_runtime"
    elif code in {"unsafe_state_directory", "unsafe_state_file", "poller_already_running"}:
        source = "durable_binding_state"
    elif code in {
        "schema_error",
        "schema_limits",
        "response_too_large",
        "invalid_json",
        "redirect_refused",
        "network_error",
        "creator_not_verified",
    } or re.fullmatch(r"http_[1-5][0-9]{2}|rate_limited:[0-9]{1,6}", code):
        source = "WhatsApp_provider"
    elif code in {
        "invalid_binding_code",
        "binding_window_closed",
        "binding_candidate_not_unique",
        "binding_console_unavailable",
    }:
        source = "supervised_binding"
    else:
        source, code = "supervised_binding", "binding_failed"
    return {"ok": False, "reason": "binding_failed", "source": source, "detail": code}


if __name__ == "__main__":
    import os
    import sys

    from .bridge import State
    from .errors import BridgeError

    try:
        names = ("WHATSAPP_API_KEY", "WA_BRIDGE_HMAC_KEY_JET", "WA_STATE_DIRECTORY")
        if any(not os.environ.get(name) for name in names):
            raise StopBridge("binding_configuration_missing")
        state = State(os.environ["WA_STATE_DIRECTORY"])
        try:
            opened_at = int(time.time())
            key = os.environ["WA_BRIDGE_HMAC_KEY_JET"].strip()
            if len(key) < 32:
                raise StopBridge("binding_configuration_missing")
            code = state.begin_binding(key, opened_at)
            show_binding_code(code)
            pin = bind_once(
                state,
                os.environ["WHATSAPP_API_KEY"],
                code,
                opened_at,
                opened_at + 600,
                hash_key=key,
            )
            print(json.dumps(pin))
        finally:
            state.close()
    except (BridgeError, ValueError) as error:
        print(json.dumps(failure(error)), file=sys.stderr)
        sys.exit(1)
