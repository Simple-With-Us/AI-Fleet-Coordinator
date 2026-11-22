"""Dedicated exact CODEX origin read. No registry mutation or general issuer key."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import parse_qsl, urlsplit

from . import operator_registry as OR

PATH = "/registry/v1/review-origin"
CONTRACT = "simple-tools-review-origin/1"
KEY_ENV = "REVIEW_ORIGIN_CODEX_READ_KEY"
SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def signature(key, query, timestamp):
    # Purpose, method, path, exact query and timestamp are all authenticated.
    message = f"{CONTRACT}\nGET\n{PATH}\n{query}\n{timestamp}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


class ReviewOriginProjection:
    def __init__(self, db_path, key="", *, clock=time.time):
        self.db_path, self.key, self.clock = db_path, key, clock

    def handle(self, method, raw_path, headers, body):
        hidden = (404, {"code": "not_found"})
        if not isinstance(self.key, str) or len(self.key) < 32:
            return hidden
        parts = urlsplit(raw_path)
        if parts.path != PATH or parts.fragment:
            return hidden
        if method != "GET":
            return 405, {"code": "method_not_allowed"}
        if body or len(parts.query) > 256:
            return 400, {"code": "invalid_request"}
        lowered = {str(k).lower(): v for k, v in headers.items()}
        stamp = lowered.get("x-simple-tools-timestamp", "")
        given = lowered.get("x-simple-tools-signature", "")
        if (
            not isinstance(stamp, str)
            or not re.fullmatch(r"[0-9]{1,12}", stamp)
            or abs(self.clock() - int(stamp)) > 300
            or not isinstance(given, str)
            or not re.fullmatch(r"[0-9a-f]{64}", given)
            or not hmac.compare_digest(signature(self.key, parts.query, stamp), given)
        ):
            return 401, {"code": "unauthorized"}
        fields = parse_qsl(parts.query, keep_blank_values=True)
        if (
            len(fields) != 1
            or fields[0][0] != "native_session_id"
            or not SESSION.fullmatch(fields[0][1])
        ):
            return 400, {"code": "invalid_request"}
        session = fields[0][1]
        try:
            rows = OR.registrations_for_native_id(self.db_path, "codex", session)
        except Exception:
            return 503, {"code": "origin_unavailable"}
        # Never choose one location, a pair or another seat when custody conflicts.
        if not isinstance(rows, list) or len(rows) != 1:
            return 409, {"code": "origin_not_unique"}
        row = rows[0]
        if (
            not isinstance(row, dict)
            or row.get("seat") != "CODEX"
            or row.get("state") != "active"
            or not isinstance(row.get("label"), str)
            or not re.fullmatch(r"[A-Z]{2}", row["label"])
            or type(row.get("generation")) is not int
            or not 1 <= row["generation"] <= 2**53 - 1
            or any(
                not isinstance(row.get(name), str) or not row[name]
                for name in ("verified_host", "profile_scope")
            )
        ):
            return 409, {"code": "origin_not_eligible"}
        return 200, {
            "origin_session_claim": session,
            "origin_letter_claim": row["label"],
            "origin_generation_claim": row["generation"],
        }
