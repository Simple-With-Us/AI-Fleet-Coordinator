# ECHO mail relay

Not deployed. No endpoint or mail receipts exist yet.

Two routes, separate credentials:
- POST /listener/echo: agent-sync-wake/1, ECHO seat, X-Agent-Sync-Signature HMAC-SHA256 over exact request bytes. ECHO_LISTENER_HMAC_KEY. Reads each original trigger message by ID, ignoring the excerpt.
- POST /zulip/echo: native Zulip outgoing JSON, ECHO_ZULIP_WEBHOOK_TOKEN, ECHO_BOT_EMAIL. Full text/time comes from the native message, not data/rendered_content.
- GET /health: outbox counts/ages and provider acceptance, never message contents or credentials. Acceptance is NOT a verified delivery receipt.

Config: ECHO_BOT_EMAIL, ECHO_ZULIP_API_KEY (needed for listener read-by-ID), RELAY_RESEND_API_KEY, RELAY_EMAIL_FROM, optional PORT (8080), RELAY_DB (/data/outbox.sqlite). Recipient is fixed in code at jaywedgeworth@mail.instinct.com. Realm is fixed in code. Both ingress secrets must be different and generated securely. Use Infisical or a vault-backed secret injection path, never commit values.

Persistent /data volume required, owned by UID 10001. SQLite WAL/FULL commits before HTTP acknowledgement; ID dedup crosses both transports. Retries use the provider Idempotency-Key. Provider idempotency expiry means exactly-once external delivery cannot be promised across long ambiguous failures.

Build from repository root with Dockerfile scripts/echo-mail-relay/Dockerfile. Route HTTPS externally to port 8080. No automatic Zulip responses. Keep the 15-minute reader until real end-to-end receipts and restart/catch-up tests pass.

Tests: cd scripts/echo-mail-relay && python3 -m unittest -v

Deployment gates: owner-approved sender and provider key; working hosting access; active ECHO identity validation after any bot replacement; secure route-key provisioning; endpoint smoke tests; recipient email receipt/wake. Code is preparation, not evidence that these passed.
