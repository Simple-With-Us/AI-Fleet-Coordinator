"""Owner binding verification for an approved, supervised ceremony.

The existing sole poller supplies the candidate batch; this helper never polls.
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
