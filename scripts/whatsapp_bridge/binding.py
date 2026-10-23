"""Owner binding verification for an approved, supervised ceremony.

The verification helper receives a candidate batch without polling.  The
supervised binding mode temporarily owns the sole poller lock.
The owner must start the ceremony and send its one-time code.  Do not run the
creator check live without approval: it emits a WhatsApp read receipt.
"""

import json
import re
import time

from .bridge import StopBridge, digest, normalize, request_json


def verify_candidate(
    payload, expected_code, api_key, opened_at, expires_at, request=request_json, now=time.time
):
    if not isinstance(expected_code, str) or not re.fullmatch(r"JET-[A-F0-9]{16}", expected_code):
        raise StopBridge("invalid_binding_code")
    if not opened_at <= now() <= expires_at or expires_at - opened_at > 600:
        raise StopBridge("binding_window_closed")
    normalized = normalize(payload, 0)
    raw_messages = payload["entry"][0]["changes"][0]["value"].get("messages", [])
    candidates = [
        (raw, msg)
        for raw, msg in zip(raw_messages, normalized["messages"])
        if msg["type"] == "text"
        and msg["text"] == expected_code
        and opened_at <= msg["timestamp"] <= expires_at
    ]
    if len(candidates) != 1:
        raise StopBridge("binding_candidate_not_unique")
    raw, msg = candidates[0]
    body = json.dumps(
        {"messaging_product": "whatsapp", "status": "read", "message_id": raw["id"]},
        separators=(",", ":"),
    ).encode()
    answer = request(
        "https://api.whatsapp.com/agent/v1/statuses",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        body,
    )
    if not isinstance(answer, dict) or answer.get("success") is not True:
        raise StopBridge("creator_not_verified")
    return {
        "agent_hash": normalized["agent_hash"],
        "sender_hash": digest(raw["from"]),
        "cutoff": msg["timestamp"],
        "verified_via": "code+creator_check",
    }


def bind_once(state, api_key, code, opened_at, expires_at, request=request_json, now=time.time):
    """Temporarily owns the same poller lock; never advances the live cursor."""
    import urllib.parse

    if not isinstance(code, str) or not re.fullmatch(r"JET-[A-F0-9]{16}", code):
        raise StopBridge("invalid_binding_code")
    if not opened_at <= now() <= expires_at or not 0 < expires_at - opened_at <= 600:
        raise StopBridge("binding_window_closed")
    offset = state.offset()
    while now() <= expires_at:
        query = urllib.parse.urlencode({"offset": offset, "timeout": 25, "limit": 100})
        payload = request(
            "https://api.whatsapp.com/agent/v1/updates?" + query,
            {"Authorization": f"Bearer {api_key}"},
        )
        if payload is None:
            continue
        batch = normalize(payload, offset)
        if any(m["type"] == "text" and m["text"] == code for m in batch["messages"]):
            return verify_candidate(payload, code, api_key, opened_at, expires_at, request, now)
        offset = int(batch["next_offset"])
    raise StopBridge("binding_window_closed")


if __name__ == "__main__":
    import os
    import sys

    from .bridge import BridgeError, State

    try:
        names = (
            "WHATSAPP_API_KEY",
            "WA_STATE_DIRECTORY",
            "WA_BINDING_CODE",
            "WA_BINDING_OPENED_AT",
            "WA_BINDING_EXPIRES_AT",
        )
        if any(not os.environ.get(name) for name in names):
            raise StopBridge("binding_configuration_missing")
        opened_at = int(os.environ["WA_BINDING_OPENED_AT"])
        expires_at = int(os.environ["WA_BINDING_EXPIRES_AT"])
        if not opened_at <= time.time() <= expires_at or not 0 < expires_at - opened_at <= 600:
            raise StopBridge("binding_window_closed")
        state = State(os.environ["WA_STATE_DIRECTORY"])
        try:
            pin = bind_once(
                state,
                os.environ["WHATSAPP_API_KEY"],
                os.environ["WA_BINDING_CODE"],
                opened_at,
                expires_at,
            )
            print(json.dumps(pin))
        finally:
            state.close()
    except (BridgeError, ValueError):
        print('{"ok":false,"reason":"binding_failed"}', file=sys.stderr)
        sys.exit(1)
