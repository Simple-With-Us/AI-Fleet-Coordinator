"""Narrow Operator adapter for the Mac handbook checker.  No ordinary seat or admin API escape hatch."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

from . import live as L
from . import operator_candidates as ledger
from . import secretscan, textfmt
from . import service_identity as SI
from . import zulip as Z

CHANNEL = "Work-In-Progress"
TOPIC = "AFC handbook changes"


def record(path, key, row):
    with L.locked(path):
        rows = ledger._sends(path)
        rows[key] = row
        L.write_json(path, {"sends": rows}, durable=True)


def reserve(path, key, now):
    # No expiry pruning: an unrelated send cannot erase an uncertain outcome.
    with L.locked(path):
        rows = ledger._sends(path)
        if key in rows:
            return False
        rows[key] = {"at": now, "state": "pending", "message_id": None}
        L.write_json(path, {"sends": rows}, durable=True)
        return True


def release(path, key):
    with L.locked(path):
        rows = ledger._sends(path)
        rows.pop(key, None)
        L.write_json(path, {"sends": rows}, durable=True)


def reconcile(client, operator_id, marker):
    # Read-only positive reconciliation. Absence never authorizes timeout replay.
    anchor = "newest"
    for _ in range(50):
        data = client.get(
            "messages",
            {
                "anchor": anchor,
                "num_before": 100,
                "num_after": 0,
                "narrow": json.dumps(
                    [
                        {"operator": "channel", "operand": CHANNEL},
                        {"operator": "topic", "operand": TOPIC},
                    ]
                ),
            },
        )
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise TypeError("malformed reconciliation history")
        matches = [
            m
            for m in messages
            if m.get("sender_id") == operator_id
            and marker in html.unescape(re.sub(r"<[^>]+>", "", str(m.get("content", ""))))
        ]
        if len(matches) == 1 and isinstance(matches[0].get("id"), int):
            return matches[0]["id"]
        if len(matches) > 1:
            raise ValueError("ambiguous reconciliation")
        ids = [m.get("id") for m in messages if isinstance(m.get("id"), int)]
        if data.get("found_oldest") or not ids:
            break
        next_anchor = min(ids)
        if anchor != "newest" and next_anchor >= anchor:
            break
        anchor = next_anchor
    raise ValueError("delivery unresolved; no replay without positive reconciliation")


def execute(args, *, env=None, client=None, credentials=None):
    env = dict(os.environ if env is None else env)
    if (
        env.get("SIMPLE_CHAT_REMOTE", "").strip() == "1"
        or env.get("AGENT_LAUNCHER")
        or env.get("AGENT_LAUNCH_SEAT")
    ):
        raise ValueError("handbook service is local unattended automation, not a launched seat")
    if (
        any(
            isinstance(x, bool) or not isinstance(x, int) or x <= 0
            for x in (args.owner_id, args.operator_id)
        )
        or "@" not in args.operator_email
    ):
        raise ValueError("owner and Operator identity pins are required")
    if not args.site.startswith("https://"):
        raise ValueError("Operator requires an HTTPS realm")
    env.update(
        OPERATOR_ZULIP_USER_ID=str(args.operator_id),
        OPERATOR_ZULIP_EMAIL=args.operator_email,
        ZULIP_SITE=args.site,
    )
    if credentials is None:
        env[SI.PROJECT_ENV] = Z.infisical_project_id(env, Path(env.get("HOME", str(Path.home()))))
        # Pin the destination to the service store, not a caller-selected HTTPS site.
        names = ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE")
        values = Z._default_infisical_fetch(
            names, env, Path(env.get("HOME", str(Path.home()))), SI.OPERATOR_INFISICAL_PATH
        )
        if values.get("ZULIP_OPERATOR_SITE", "").rstrip("/") != args.site.rstrip("/"):
            raise ValueError("realm does not match the registered Operator realm")
        credentials = SI.operator_credentials(
            env, fetch=lambda requested: {n: values.get(n, "") for n in requested}
        )
    if args.site.rstrip("/") != credentials.site.rstrip("/"):
        raise ValueError("realm does not match the service credential realm")
    client = client or Z.ZulipClient(credentials, args.site, owner_user_id=args.owner_id)
    if not SI.operator_bot(client.get("users/me"), env, args.owner_id):
        raise ValueError("authenticated bot does not match pinned Operator and owner")
    if args.action == "read":
        if (
            isinstance(args.message_id, bool)
            or not isinstance(args.message_id, int)
            or args.message_id <= 0
        ):
            raise ValueError("positive message id required")
        message = client.get(f"messages/{args.message_id}").get("message")
        if not isinstance(message, dict):
            raise ValueError("malformed message response")
        # Existing approval ids may have been written by the former CLAUDE sender.  Their author
        # is not the approver:  the checker still verifies only the pinned human reaction.
        if (
            message.get("type") != "stream"
            or message.get("display_recipient") != CHANNEL
            or message.get("subject") != TOPIC
        ):
            raise ValueError("approval message is outside the handbook topic")
        return {"message": message}
    if args.action != "post":
        raise ValueError("only post/read are allowed")
    if args.repo != "Simple-With-Us/Fleet-OPS":
        raise ValueError("invalid repository slug")
    body_path = Path(args.body_file)
    if body_path.stat().st_size > 40000:
        raise ValueError("body file exceeds bounded UTF-8 input")
    # Bounded read also closes the stat/read growth race.
    with body_path.open("rb") as body_stream:
        encoded = body_stream.read(40001)
    if len(encoded) > 40000:
        raise ValueError("body file grew beyond bounded input")
    raw = encoded.decode("utf-8")
    hidden = list(secretscan.basic_forms(credentials.email, credentials.key))
    if secretscan.scan(raw, hidden):
        raise ValueError("handbook body matched the secret scanner")
    # Caller text cannot mention anybody.  Only the configured owner gets the explicit mention.
    raw = raw.replace("@", "＠").replace("#**", "＃**")
    body = "repo: {}\n[OPERATOR·handbook-sync]\n{}{}".format(
        args.repo,
        f"@**|{args.owner_id}**\n" if args.to_owner else "",
        raw,
    )
    body, _ = textfmt.fix_outbound(body, limit=10000)
    if len(body) > 9900 or secretscan.scan(body, hidden):
        raise ValueError("handbook message failed the outbound boundary")
    # Reserve before sending.  An uncertain result is never replayed automatically.
    fingerprint = hashlib.sha256((CHANNEL + "\0" + TOPIC + "\0" + body).encode()).hexdigest()
    ledger_path = str(
        Path(env.get("HOME", str(Path.home()))) / ".agent-sync" / "handbook-operator-delivery.json"
    )
    marker = "handbook-ref:" + fingerprint
    body += "\n\n" + marker
    now = time.time()
    stored = ledger._sends(ledger_path).get(fingerprint, {})
    if stored.get("state") == ledger.DELIVERY_OK and isinstance(stored.get("message_id"), int):
        return {"id": stored["message_id"]}
    if stored.get("state") in (ledger.DELIVERY_PENDING, ledger.DELIVERY_UNKNOWN):
        mid = reconcile(client, args.operator_id, marker)
        record(ledger_path, fingerprint, {"at": now, "state": "ok", "message_id": mid})
        return {"id": mid}
    if not reserve(ledger_path, fingerprint, now):
        # Another process may have completed after our first read.  Return that cached
        # successful id, otherwise keep the in-flight/unknown result blocked.
        with L.locked(ledger_path):
            raced = ledger._sends(ledger_path).get(fingerprint, {})
            mid = raced.get("message_id")
            if (
                raced.get("state") == ledger.DELIVERY_OK
                and isinstance(mid, int)
                and not isinstance(mid, bool)
                and mid > 0
            ):
                return {"id": mid}
        raise ValueError("handbook delivery is already reserved")
    try:
        result = client.post(
            "messages", {"type": "stream", "to": CHANNEL, "topic": TOPIC, "content": body}
        )
        mid = result.get("id")
        if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
            raise ValueError("malformed send response")
    except Exception as exc:
        definite = isinstance(exc, Z.NetworkError) and not exc.maybe_sent
        definite = definite or (
            isinstance(exc, Z.ApiError) and isinstance(exc.status, int) and 400 <= exc.status < 429
        )
        if definite:
            release(ledger_path, fingerprint)
        else:
            record(ledger_path, fingerprint, {"at": now, "state": "unknown", "message_id": None})
        raise
    record(ledger_path, fingerprint, {"at": now, "state": "ok", "message_id": mid})
    return {"id": mid}


def main(argv=None):
    p = argparse.ArgumentParser(prog="simple-chat handbook-service")
    p.add_argument("action", choices=["post", "read"])
    p.add_argument("--owner-id", type=int, required=True)
    p.add_argument("--operator-id", type=int, required=True)
    p.add_argument("--operator-email", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--repo", default="")
    p.add_argument("--body-file")
    p.add_argument("--to-owner", action="store_true")
    p.add_argument("--message-id", type=int, default=0)
    a = p.parse_args(argv)
    try:
        print(json.dumps(execute(a)))
        return 0
    except Exception as exc:  # noqa: BLE001 - output must never contain provider text
        # Source exceptions can contain provider text or body snippets.  Never print them.
        print(
            f"handbook-service: refused ({type(exc).__name__}); verify service pins, credentials or delivery ledger",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
