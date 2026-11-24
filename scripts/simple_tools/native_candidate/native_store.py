"""Read-only exact-command native input reconciliation. Never resends.

Uses installed message metadata.conversationInputIntent and session_input payload.
Missing/cancelled/discarded records are evidence states, never retry permissions.
"""

import json
import re
import sqlite3
import urllib.parse


def fetched_response(part, alert_id):
    # Exact installed tool/identity and framed response, not a parameter match.
    if (
        type(alert_id) is not int
        or alert_id <= 0
        or part.get("type") != "tool"
        or part.get("tool") != "mcp__fleet-tools__zulip_get_message"
    ):
        return False
    state = part.get("state", {})
    inp = state.get("input", {})
    output = state.get("output")
    if (
        state.get("status") != "completed"
        or not isinstance(inp, dict)
        or inp.get("identity") != "ZC"
        or type(inp.get("message_id")) is not int
        or inp["message_id"] != alert_id
        or not isinstance(output, str)
        or len(output) > 2 * 1024 * 1024
    ):
        return False
    lines = output.splitlines()
    if (
        len(lines) < 6
        or lines[0] != 'simple-tools get_message:  message_id "' + str(alert_id) + '"'
        or lines[1] != "1 message"
    ):
        return False
    starts = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"BEGIN_UNTRUSTED_ZULIP nonce=[0-9a-f]{16}", line)
    ]
    if len(starts) != 1:
        return False
    i = starts[0]
    nonce = lines[i].split("nonce=")[1]
    if i + 2 != len(lines) - 1 or lines[i + 2] != "END_UNTRUSTED_ZULIP nonce=" + nonce:
        return False
    try:
        message = json.loads(lines[i + 1])
    except (ValueError, TypeError):
        return False
    return (
        isinstance(message, dict)
        and type(message.get("id")) is int
        and message["id"] == alert_id
        and message.get("truncated") is False
        and isinstance(message.get("body"), str)
        and bool(message["body"])
    )


def reconcile(db_path, binding, command_id, text, alert_id=None):
    c = sqlite3.connect(
        "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro", uri=True, timeout=2
    )
    c.row_factory = sqlite3.Row
    c.execute("BEGIN")
    result = {
        "native_input": "unverified",
        "turn": "unverified",
        "owning_alert": "unverified",
    }
    try:
        sid = binding["session_id"]
        row = c.execute(
            "SELECT id,directory FROM session WHERE id=?", (sid,)
        ).fetchone()
        if not row or row["directory"] != binding["workspace"]:
            return result
        ledger = c.execute(
            "SELECT status,status_reason,promoted_message_id,payload FROM session_input WHERE session_id=? AND json_extract(payload,'$.conversationInputIntent.sourceCommandId')=?",
            (sid, command_id),
        ).fetchall()
        if len(ledger) > 1:
            return {**result, "conflict": True}
        if ledger:
            fact = ledger[0]
            payload = json.loads(fact["payload"])
            intent = payload.get("conversationInputIntent", {})
            if (
                intent.get("sourceCommandId") != command_id
                or payload.get("text") != text
                or intent.get("text") != text
            ):
                return {**result, "conflict": True}
            result["input_ledger"] = fact["status"]
            if fact["status"] == "promoted" and fact["promoted_message_id"]:
                result["promoted_message_id"] = fact["promoted_message_id"]
        rows = c.execute(
            "SELECT id,data FROM message WHERE session_id=? AND json_extract(data,'$.role')='user' AND (json_extract(data,'$.metadata.conversationInputIntent.sourceCommandId')=? OR json_extract(data,'$.anchor.sourceCommandId')=?)",
            (sid, command_id, command_id),
        ).fetchall()
        if len(rows) != 1:
            return result
        message = rows[0]
        if (
            result.get("promoted_message_id")
            and result["promoted_message_id"] != message["id"]
        ):
            return {**result, "conflict": True}
        parts = c.execute(
            "SELECT data FROM part WHERE session_id=? AND message_id=? ORDER BY sequence,time_created,id",
            (sid, message["id"]),
        ).fetchall()
        texts = [
            json.loads(p["data"]).get("text", "")
            for p in parts
            if json.loads(p["data"]).get("type") == "text"
        ]
        if texts != [text]:
            return {**result, "conflict": True}
        result.update(
            native_input="persisted",
            message_id=message["id"],
            sourceCommandId=command_id,
        )
        assistants = c.execute(
            "SELECT id,data FROM message WHERE session_id=? AND json_extract(data,'$.role')='assistant' AND json_extract(data,'$.parentID')=?",
            (sid, message["id"]),
        ).fetchall()
        if assistants:
            result["turn"] = "started"
            if all(
                json.loads(a["data"]).get("time", {}).get("completed")
                for a in assistants
            ):
                result["turn"] = "completed"
            if alert_id is not None:
                for a in assistants:
                    for part in c.execute(
                        "SELECT id,data FROM part WHERE session_id=? AND message_id=?",
                        (sid, a["id"]),
                    ):
                        p = json.loads(part["data"])
                        state = p.get("state", {})
                        inp = state.get("input", {})
                        # Parameter match is recorded separately from authenticated native
                        # tool name/identity and exact structured response evidence.
                        if (
                            p.get("type") == "tool"
                            and inp.get("message_id") == alert_id
                            and state.get("status") == "completed"
                            and state.get("output")
                        ):
                            result["fetch_candidate"] = {
                                "tool": p.get("tool"),
                                "part_message_id": a["id"],
                                "alert_id": alert_id,
                            }
                        if fetched_response(p, alert_id):
                            result.update(
                                owning_alert="fetched",
                                alert_id=alert_id,
                                fetch_message_id=a["id"],
                                fetch_part_id=part["id"],
                            )
        return result
    finally:
        c.close()
