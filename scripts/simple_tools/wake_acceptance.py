"""Read-only correlation of one Operator wake, from a real mention to target-chat input.

Run ``python3 -m simple_tools.wake_acceptance --help`` for the snapshot inputs.
No network calls, queue writes, transcript writes or replay occur here.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
WAKE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _user_text(row: Mapping[str, Any]) -> str:
    """Only actual user input; quoted assistant output is never delivery evidence."""
    payload = _object(row.get("payload"))
    if row.get("type") == "event_msg" and payload.get("type") == "user_message":
        return payload.get("message") if isinstance(payload.get("message"), str) else ""
    if (
        row.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    ):
        parts = payload.get("content")
        if isinstance(parts, list):
            return "".join(
                p["text"]
                for p in parts
                if isinstance(p, dict)
                and p.get("type") == "input_text"
                and isinstance(p.get("text"), str)
            )
    return ""


def assess(
    *,
    wake_id: str,
    message_id: int,
    session_id: str,
    generation: int,
    seat: str,
    mention: str,
    source: Mapping[str, Any],
    listener: Iterable[Mapping[str, Any]],
    receipt: Mapping[str, Any],
    transcript: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """A proven result requires independently matching source, listener, router and transcript.

    This proves receipt of the metadata prompt in the intended chat, not completion of
    the work requested by the original message.  The caller supplies expected identity,
    rather than trusting the router's pick as its own acceptance criterion.
    """
    if not WAKE_ID.fullmatch(wake_id) or type(message_id) is not int or message_id <= 0:
        raise ValueError("invalid expected wake identity")
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError("noncanonical session")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid expected session") from None
    if type(generation) is not int or generation <= 0:
        raise ValueError("invalid expected generation")
    if (
        not isinstance(seat, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,31}", seat)
        or not isinstance(mention, str)
        or not mention
    ):
        raise ValueError("invalid expected seat")

    checks: dict[str, str] = {}
    errors: list[str] = []
    source = _object(source.get("message", source))
    content = source.get("content")
    loud = re.compile(r"@\*\*" + re.escape(mention) + r"(?:\|[0-9]+)?\*\*")
    if type(source.get("id")) is not int or source.get("id") != message_id:
        checks["source"] = "mismatch"
        errors.append("source-message-mismatch")
    elif not isinstance(content, str) or not loud.search(content):
        checks["source"] = "mismatch"
        errors.append("source-has-no-loud-mention")
    else:
        checks["source"] = "matched"

    rows = [_object(row) for row in listener if _object(row).get("wake_id") == wake_id]
    matching = [row for row in rows if row.get("seat") == seat]
    trigger_lists = [row.get("trigger_ids", []) for row in matching]
    invalid_triggers = any(not isinstance(ids, list) for ids in trigger_lists)
    triggers = {
        mid for ids in trigger_lists if isinstance(ids, list) for mid in ids if type(mid) is int
    }
    states = [str(row.get("state") or "") for row in matching]
    if not rows:
        checks["listener"] = "pending"
    elif invalid_triggers:
        checks["listener"] = "mismatch"
        errors.append("listener-invalid-trigger-ids")
    elif not matching or message_id not in triggers:
        checks["listener"] = "mismatch"
        errors.append("listener-source-or-seat-mismatch")
    elif any(state in states for state in ("failed", "dropped", "held_back", "expired")):
        checks["listener"] = "failed"
        errors.append("listener-terminal-failure")
    elif "done" in states:
        checks["listener"] = "matched"
    else:
        checks["listener"] = "pending"

    if not receipt:
        checks["router"] = "pending"
    elif receipt.get("wake_id") != wake_id or receipt.get("seat") != seat:
        checks["router"] = "mismatch"
        errors.append("router-wake-or-seat-mismatch")
    elif receipt.get("test") is not False:
        checks["router"] = "mismatch"
        errors.append("router-is-not-a-real-wake")
    elif receipt.get("session_id") != session_id:
        checks["router"] = "mismatch"
        errors.append("router-target-mismatch")
    elif (
        type(receipt.get("picked_generation")) is not int
        or receipt.get("picked_generation") != generation
    ):
        checks["router"] = "mismatch"
        errors.append("router-generation-mismatch")
    elif receipt.get("status") in ("dead", "failed", "refused"):
        checks["router"] = "failed"
        errors.append("router-terminal-failure")
    elif (
        receipt.get("status") == "delivered"
        and "delivered" in receipt
        and (receipt.get("delivered") is None or receipt.get("delivered") is True)
    ):
        # A native queue handoff may still have delivered=null.  Transcript evidence
        # below, independent of the queue receipt, must establish actual arrival.
        checks["router"] = "matched"
    else:
        checks["router"] = "pending"

    events = [_object(row) for row in transcript]
    headers = [_object(row.get("payload")) for row in events if row.get("type") == "session_meta"]
    if not headers or any(header.get("id") != session_id for header in headers):
        checks["target_chat"] = "mismatch"
        errors.append("transcript-session-not-proven")
    else:
        token = re.compile(r"(?<![A-Za-z0-9._-])" + re.escape(wake_id) + r"(?![A-Za-z0-9._-])")
        # Both ids must be in the same genuine user input.  A coincidental wake-id
        # quote elsewhere, or a longer id with this prefix, does not establish arrival.
        message_token = re.compile(r"(?<![0-9])" + str(message_id) + r"(?![0-9])")
        arrived = any(
            token.search(text) and message_token.search(text)
            for row in events
            if (text := _user_text(row))
        )
        checks["target_chat"] = "matched" if arrived else "pending"

    proven = not errors and all(value == "matched" for value in checks.values())
    return {
        "wake_id": wake_id,
        "message_id": message_id,
        "session_id": session_id,
        "generation": generation,
        "seat": seat,
        "status": "proven" if proven else "rejected" if errors else "pending",
        "checks": checks,
        "errors": errors,
    }


def json_file(path: str) -> Mapping[str, Any]:
    with open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("snapshot exceeds size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("expected an object snapshot")
    return value


def json_lines(path: str) -> list[Mapping[str, Any]]:
    """Bounded JSONL tail, retaining the session header for exact transcript identity."""
    with open(path, "rb") as stream:
        first = stream.readline(MAX_BYTES + 1)
        if len(first) > MAX_BYTES:
            raise ValueError("JSONL row exceeds size limit")
        stream.seek(0, 2)
        start = max(0, stream.tell() - MAX_BYTES)
        stream.seek(start)
        if start:
            stream.readline()  # omit the incomplete row at the tail boundary
        lines = stream.read(MAX_BYTES + 1).splitlines()
    if start and first.strip():
        lines.insert(0, first)
    result = []
    for raw in lines:
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("expected JSONL object")
        result.append(value)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "wake-id",
        "message-id",
        "session-id",
        "generation",
        "source",
        "listener",
        "receipt",
        "transcript",
    ):
        parser.add_argument(
            "--" + name, required=True, type=int if name in ("message-id", "generation") else str
        )
    parser.add_argument("--seat", default="CODEX")
    parser.add_argument("--mention", default="Codex")
    args = parser.parse_args(argv)
    try:
        result = assess(
            wake_id=args.wake_id,
            message_id=args.message_id,
            session_id=args.session_id,
            generation=args.generation,
            seat=args.seat,
            mention=args.mention,
            source=json_file(args.source),
            listener=json_lines(args.listener),
            receipt=json_file(args.receipt),
            transcript=json_lines(args.transcript),
        )
    except (OSError, ValueError, TypeError, RecursionError):
        # File contents and exceptions can carry untrusted text or secrets.  Neither
        # is echoed; this tool emits only ids, fixed codes and component states.
        sys.stdout.write(json.dumps({"status": "invalid", "code": "invalid-snapshot"}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["status"] == "proven" else 3


if __name__ == "__main__":
    raise SystemExit(main())
