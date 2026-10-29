"""The outbound leg: one queued reply at a time, sent once, never resent.

Sections 9 and 10 of docs/plans/2026-10-22-whatsapp-wake-bridge.md are the
contract this module implements: a reply whose outcome is unknown stays
unknown for good, a definite refusal releases the client_ref, a decline is a
result and never an unknown outcome, and there is no resend.

The bridge keeps its own write-ahead ledger, so a restart can tell "never
sent" from "maybe sent".  The local state holds no bodies: only the intent
id, the state, the body hash and the sent message id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.request

if __package__:
    from .bridge import NoRedirect, request_json, retry_delay
    from .errors import BridgeError
else:
    from bridge import NoRedirect, request_json, retry_delay
    from errors import BridgeError

SEND_URL = "https://api.whatsapp.com/agent/v1/messages"
MAX_RESPONSE = 8 * 1024 * 1024
MAX_ATTEMPTS = 3
RETRYABLE_CODE = 131016
# Meta allows twelve sends a minute; the bridge stays under ten.
SENDS_PER_MINUTE = 10
SEND_TIMEOUT = 60
TARGET_RING = 500


def body_hash(text):
    """The hash the local ledger keeps.  Never the text itself."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def signed(worker_url, signing_key, path, payload, request=request_json):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signature = hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()
    return request(
        worker_url + path,
        {"Content-Type": "application/json", "X-WA-Bridge-Signature": signature},
        body,
    )


def claim(worker_url, signing_key, clock=time.time, request=request_json):
    """Ask the Worker for one queued reply.  None means the outbox is empty."""
    answer = signed(
        worker_url,
        signing_key,
        "/internal/wa-outbox/JET",
        {"contract": "wa-outbox/1", "seat": "JET", "sent_at": int(clock())},
        request,
    )
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise BridgeError("outbox_not_acknowledged")
    reply = answer.get("reply")
    return reply if isinstance(reply, dict) else None


def report(
    worker_url, signing_key, intent_id, outcome, clock=time.time, request=request_json, **fields
):
    """Tell the Worker what happened to one reply."""
    payload = {
        "contract": "wa-outbox-result/1",
        "seat": "JET",
        "sent_at": int(clock()),
        "intent_id": intent_id,
        "outcome": outcome,
    }
    for name, value in fields.items():
        if value is not None:
            payload[name] = value
    answer = signed(
        worker_url,
        signing_key,
        "/internal/wa-outbox/JET/result",
        payload,
        request,
    )
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise BridgeError("outbox_result_not_acknowledged")
    return answer


def provider_code(error):
    """Meta's error code, when the answer carries one.  Never its message."""
    try:
        raw = error.read(64 * 1024)
        parsed = json.loads(raw)
    except Exception:
        return None
    code = parsed.get("error", {}).get("code") if isinstance(parsed, dict) else None
    return code if isinstance(code, int) else None


def never_opened(error):
    """True when the connection never opened, so no request byte was written."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror, socket.herror))


def send(api_key, recipient, text, context_id=None, timeout=SEND_TIMEOUT):
    """One attempt at Meta.  The answer is a class, never a guess.

    sent, refused, unknown, retry and requeue are the five answers of the
    plan's table.  An unknown answer is never retried.
    """
    payload = {"to": recipient, "type": "text", "text": {"body": text}}
    if context_id:
        payload["context"] = {"message_id": context_id}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    request = urllib.request.Request(
        SEND_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "AFC-WhatsApp-Bridge/1",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                return {"outcome": "unknown", "error": "response_too_large"}
            try:
                parsed = json.loads(data)
            except (ValueError, RecursionError):
                return {"outcome": "unknown", "error": "invalid_json"}
            messages = parsed.get("messages") if isinstance(parsed, dict) else None
            message_id = (
                messages[0].get("id")
                if isinstance(messages, list) and messages and isinstance(messages[0], dict)
                else None
            )
            if not isinstance(message_id, str) or not message_id:
                return {"outcome": "unknown", "error": "no_message_id"}
            return {"outcome": "sent", "message_id": message_id}
    except urllib.error.HTTPError as error:
        status = error.code
        retry = error.headers.get("Retry-After", "30") if error.headers else "30"
        code = provider_code(error)
        error.close()
        if status == 429:
            return {
                "outcome": "retry",
                "status": status,
                "code": code,
                "retry_after_s": retry_delay(retry),
            }
        if status == 503 and code == RETRYABLE_CODE:
            return {
                "outcome": "retry",
                "status": status,
                "code": code,
                "retry_after_s": retry_delay(retry),
            }
        if 400 <= status < 500:
            return {"outcome": "refused", "status": status, "code": code}
        return {"outcome": "unknown", "status": status, "code": code}
    except TimeoutError:
        return {"outcome": "unknown", "error": "read_timeout"}
    except (urllib.error.URLError, OSError) as error:
        if never_opened(error):
            return {"outcome": "requeue"}
        return {"outcome": "unknown", "error": "connection_lost"}


class Outbox:
    """The bridge side of the outbound leg: claim, send, record, never resend."""

    def __init__(
        self,
        state,
        api_key,
        signing_key,
        worker_url,
        request=request_json,
        clock=time.time,
        sleeper=time.sleep,
        enabled=None,
    ):
        self.state = state
        self.api_key = api_key
        self.signing_key = signing_key
        self.worker_url = worker_url.rstrip("/")
        self.request = request
        self.clock = clock
        self.sleeper = sleeper
        self.enabled = enabled if callable(enabled) else (lambda: bool(enabled))
        self._sent_at = []

    def now(self):
        return int(self.clock())

    def step(self):
        """One reply at most.  Nothing queued means nothing happens."""
        self.recover()
        if not self.enabled():
            return {"state": "send_disabled"}
        recipient = self.state.recipient()
        if not recipient:
            # The bridge never guesses the conversation: the bound sender's
            # own id is the only address it may use.
            return {"state": "no_recipient"}
        reply = claim(self.worker_url, self.signing_key, self.clock, self.request)
        if not reply:
            return {"state": "idle"}
        return self.deliver(reply, recipient)

    def recover(self):
        """A restart finds the local ledger: never sent, or maybe sent.

        The outcome the bridge knows is reported again, and the row is made
        final only after the Worker has acked it.
        """
        for intent_id, state, digest, message_id, pending in self.state.ledger_pending():
            token = self.state.ledger_token(intent_id)
            if pending:
                # The outcome is known and the Worker has not acked it yet.
                outcome = pending
                fields = {}
                if outcome == "sent":
                    fields["message_id"] = message_id
            elif state == "claimed":
                # The POST never started, so the reply goes back to the queue.
                outcome = "requeue"
                fields = {}
            else:
                # The POST may have gone out: unknown for good, never resent.
                outcome = "outcome_unknown"
                fields = {"error": "bridge_restart"}
            report(
                self.worker_url,
                self.signing_key,
                intent_id,
                outcome,
                self.clock,
                self.request,
                token=token,
                **fields,
            )
            if outcome in ("requeue", "declined"):
                self.state.ledger_delete(intent_id)
            else:
                self.state.ledger_put(intent_id, outcome, digest, message_id, self.now())

    def rate_ok(self):
        """At most ten sends a minute, and the window is the last minute."""
        now = self.clock()
        self._sent_at = [at for at in self._sent_at if now - at < 60]
        return len(self._sent_at) < SENDS_PER_MINUTE

    def settle(self, intent_id, outcome, digest, token, message_id=None, started=True, **fields):
        """Report the outcome, and only then make the local row final.

        The row keeps its claimable state until the Worker acks, so a report
        that fails is reported again by recover() instead of losing the
        outcome.  A row whose POST never started stays claimed, and a row
        that may have sent stays sending.
        """
        state = "sending" if started else "claimed"
        self.state.ledger_put(intent_id, state, digest, message_id, self.now(), outcome)
        if outcome == "sent" and message_id is not None:
            fields["message_id"] = message_id
        report(
            self.worker_url,
            self.signing_key,
            intent_id,
            outcome,
            self.clock,
            self.request,
            token=token,
            **fields,
        )
        if outcome in ("requeue", "declined"):
            self.state.ledger_delete(intent_id)
        else:
            self.state.ledger_put(intent_id, outcome, digest, message_id, self.now())

    def deliver(self, reply, recipient):
        intent_id = reply.get("intent_id")
        text = reply.get("text") or ""
        token = reply.get("token")
        digest = body_hash(text)
        # The write-ahead: claimed, then sending, before the POST.  A restart
        # reads these two states and never guesses.
        self.state.ledger_put(intent_id, "claimed", digest, None, self.now())
        self.state.ledger_set_token(intent_id, token)
        if not self.enabled():
            # The switch flipped before the POST: a decline is a result, so
            # the reply goes back to held and never becomes unknown.
            self.settle(intent_id, "declined", digest, token, None, started=False)
            return {"state": "declined"}
        context_id = None
        if reply.get("reply_to_id_hash"):
            context_id = self.state.target_for(reply["reply_to_id_hash"])
        context_missing = bool(reply.get("reply_to_id_hash")) and context_id is None
        if not self.rate_ok():
            # The claim is given back: the reply is queued again and nothing
            # is sent this minute.
            self.settle(intent_id, "requeue", digest, token, None, started=False)
            return {"state": "rate_limited"}
        self._sent_at.append(self.clock())
        self.state.ledger_put(intent_id, "sending", digest, None, self.now())
        answer = self.attempt(recipient, text, context_id, reply.get("attempts") or 0)
        outcome = answer["outcome"]
        if outcome == "sent":
            self.settle(
                intent_id,
                "sent",
                digest,
                token,
                answer.get("message_id"),
                context_missing=context_missing or None,
            )
            return {"state": "sent"}
        if outcome == "refused":
            self.settle(
                intent_id,
                "refused",
                digest,
                token,
                None,
                status=answer.get("status"),
                code=answer.get("code"),
                error=answer.get("error"),
                retry_after_s=answer.get("retry_after_s"),
            )
            return {"state": "refused"}
        if outcome == "requeue":
            self.settle(
                intent_id,
                "requeue",
                digest,
                token,
                None,
                started=False,
                retry_after_s=answer.get("retry_after_s"),
            )
            return {"state": "requeue"}
        # Unknown: it stays unknown for good, and it is never sent again.
        self.settle(
            intent_id,
            "outcome_unknown",
            digest,
            token,
            None,
            status=answer.get("status"),
            code=answer.get("code"),
            error=answer.get("error"),
        )
        return {"state": "outcome_unknown"}

    def attempt(self, recipient, text, context_id, attempts=0):
        """The bounded retries of the plan: 429 and 503 with 131016 only.

        The attempt cap across claims belongs to the Worker, which counts the
        claims on the ledger row.
        """
        for number in range(1, MAX_ATTEMPTS + 1):
            answer = send(self.api_key, recipient, text, context_id)
            if answer["outcome"] != "retry":
                return answer
            if number >= MAX_ATTEMPTS:
                # The retry cap is reached: a definite refusal, and the
                # client_ref is released for a later authorized attempt.
                return {
                    "outcome": "refused",
                    "status": answer.get("status"),
                    "code": answer.get("code"),
                    "error": "retry_cap_reached",
                    "retry_after_s": answer.get("retry_after_s"),
                }
            delay = answer.get("retry_after_s") or min(60, 5 * 2 ** (number - 1))
            if delay > 2:
                # Never park the inbound poller on provider backoff. Worker owns
                # the delayed queue and cross-claim attempt cap.
                return {"outcome": "requeue", "retry_after_s": min(3600, delay)}
            self.sleeper(delay)
        return {"outcome": "unknown", "error": "retry_cap_reached"}
