"""Send a content-free Operator doorbell; never represents owner authorization.

Use a stable request UUID for retries.  Its signing key is separate from the
WhatsApp ingest key.  This script does not create credentials or subscriptions.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from urllib.parse import urlsplit

if __package__:
    from .bridge import request_json, retry_delay
    from .errors import BridgeError, StopBridge
else:
    from bridge import request_json, retry_delay
    from errors import BridgeError, StopBridge


def wake(base_url, signing_key, request_id, request=request_json):
    parsed = urlsplit(base_url)
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
    if len(signing_key.strip()) < 32 or str(uuid.UUID(request_id)) != request_id:
        raise StopBridge("invalid_operator_request")
    body = json.dumps(
        {
            "contract": "wa-operator/1",
            "seat": "JET",
            "sent_at": int(time.time()),
            "request_id": request_id,
        },
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(signing_key.strip().encode(), body, hashlib.sha256).hexdigest()
    result = request(
        base_url.rstrip("/") + "/internal/wa-operator/JET",
        {"Content-Type": "application/json", "X-WA-Bridge-Signature": signature},
        body,
    )

    if isinstance(result, dict) and result.get("ok") is False:
        raise StopBridge("operator_refused")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BridgeError("operator_not_acknowledged")
    return result


def main(argv=None, environment=None):
    argv = sys.argv[1:] if argv is None else argv
    environment = os.environ if environment is None else environment
    try:
        for name in ("WA_WORKER_URL", "WA_OPERATOR_HMAC_KEY_JET"):
            if not environment.get(name, "").strip():
                print(
                    json.dumps({"ok": False, "reason": "not_configured", "required": name}),
                    file=sys.stderr,
                )
                return 1
        result = wake(
            environment["WA_WORKER_URL"], environment["WA_OPERATOR_HMAC_KEY_JET"], argv[0]
        )
        print(json.dumps(result))
        return 0
    except BridgeError as error:
        code = str(error)
        if code.startswith("rate_limited:"):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "operator_busy",
                        "retry_after_s": retry_delay(code.partition(":")[2]),
                    }
                ),
                file=sys.stderr,
            )
        else:
            transient = (
                code
                in (
                    "network_error",
                    "invalid_json",
                    "response_too_large",
                    "operator_not_acknowledged",
                )
                or code == "http_408"
                or code.startswith("http_5")
            )
            allowed = {
                "invalid_worker_url",
                "invalid_operator_request",
                "redirect_refused",
                "operator_refused",
            }
            reason = (
                "outcome_unknown"
                if transient
                else code
                if code in allowed
                or (code.startswith("http_") and code[5:].isdigit() and len(code) == 8)
                else "operator_wake_failed"
            )
            print(json.dumps({"ok": False, "reason": reason}), file=sys.stderr)
        return 1
    except (KeyError, ValueError, IndexError):
        print('{"ok":false,"reason":"operator_wake_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
