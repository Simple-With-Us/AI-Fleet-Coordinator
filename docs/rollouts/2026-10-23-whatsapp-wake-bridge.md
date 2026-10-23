# WhatsApp Wake Bridge For Jet (AFC-477)

Implementation in `codex/afc-477-whatsapp-wake`, based on main `9af6f459`.  Disabled by default; not deployed or live-tested.  Claude confirmed no implementation overlap in public Work-In-Progress message 634168889, and offered PR review.  He reported no named Worker deployment owner.

Adds automatic authenticated owner-message wakes, leading-👀 silent retention, a distinct Operator doorbell, a sole Python poller, exact durable offset acknowledgments, private read/surface/ack state, 35-day dedupe tombstones and content-free MCP event delivery.  WhatsApp subscriptions must share Jet's existing Zulip callback; existing Zulip and spike delivery remain isolated.

Validation: all 571 Worker unit tests passed.  All 55 local workerd scenarios passed, including real Durable Object storage, signed ingestion, owner wake, silent prefix, read/ack and Operator provenance.  Twenty-seven Python tests passed.  Wrangler dry-run bundle passed.  These tests use synthetic credentials and mocked upstreams; none is a production acceptance receipt.

The implementation is receiving/waking/reading only.  The proposal's reply outbox and send tool, admin binding UI, automatic creator re-verification, crypto-shredding and host-specific monitoring are not included.  No outbound WhatsApp message is sent or retried.  Supervised code-plus-creator binding helper is included, but was not run live.

Exact configuration, retention semantics, constraints and action-time approval steps are in [the bridge runbook](../../scripts/whatsapp_bridge/README.md).  Approval is required before credentials are provisioned, Worker bindings deployed or a persistent poller activated.  No ST-MCP parent access was attempted and no authentication was altered.

Credential plan correction: the owner selected two dedicated read-only AFC service tokens for exact prod `/jet` and `/operator-jet-wake`, superseding the earlier machine-identity/Fleet-OPS plan.  The parent relayed explicit owner approval for both tokens with 90-day expiry from provisioning, preserving exact scopes and recipients with no automatic self-renewal or credential-management permission.  No token was created; required CI/review readiness and deployment coordination still gate setup.  Existing SSH over the host's Tailscale peer was verified without configuration changes.  See the runbook for protected storage, recipients and server-side scope verification.
