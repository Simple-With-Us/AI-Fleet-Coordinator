"""Offline receipt classification, never dispatches or changes router state.

Evidence must be obtained from the owning native store/integration, not an ACK.
The caller supplies source-verified records; these predicates cannot authenticate them.
"""

from native_store import fetched_response


def classify(binding, command_id, text, native_message=None, turn=None, fetch=None, alert_id=None):
    result = {
        "admission": "unverified",
        "native_input": "unverified",
        "turn": "unverified",
        "owning_alert": "unverified",
    }
    if (
        not native_message
        or native_message.get("session_id") != binding["session_id"]
        or native_message.get("sourceCommandId") != command_id
        or native_message.get("role") != "user"
        or native_message.get("text") != text
        or not native_message.get("message_id")
    ):
        return result
    result.update(admission="proven-by-input", native_input="persisted")
    if (
        turn
        and turn.get("session_id") == binding["session_id"]
        and turn.get("sourceCommandId") == command_id
        and turn.get("parent_message_id") == native_message["message_id"]
        and turn.get("state") in ("started", "completed")
    ):
        result["turn"] = turn["state"]
        if (
            fetch
            and fetch.get("session_id") == binding["session_id"]
            and fetch.get("parent_message_id") == native_message["message_id"]
            and fetch.get("alert_id") == alert_id
            and alert_id is not None
            and fetch.get("status") == "completed"
            and fetch.get("output_nonempty") is True
            and fetched_response(fetch.get("native_part", {}), alert_id)
        ):
            result["owning_alert"] = "fetched"
    # Fetched evidence is not a semantic claim that the alert was acted upon.
    return result
