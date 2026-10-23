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
    from .bridge import BridgeError, StopBridge, request_json
else:
    from bridge import BridgeError, StopBridge, request_json


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

    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BridgeError("operator_not_acknowledged")
    return result


if __name__ == "__main__":
    try:
        result = wake(
            os.environ["WA_WORKER_URL"], os.environ["WA_OPERATOR_HMAC_KEY_JET"], sys.argv[1]
        )
        print(json.dumps(result))
    except BridgeError as error:
        code = str(error)
        if code.startswith("rate_limited:"):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "operator_busy",
                        "retry_after_s": int(code.split(":")[1]),
                    }
                ),
                file=sys.stderr,
            )
        else:
            print('{"ok":false,"reason":"operator_not_acknowledged"}', file=sys.stderr)
        sys.exit(1)
    except (KeyError, ValueError, IndexError):
        print('{"ok":false,"reason":"operator_wake_failed"}', file=sys.stderr)
        sys.exit(1)
