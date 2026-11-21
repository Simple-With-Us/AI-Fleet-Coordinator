# Dedicated service alert bindings

Doctor, simulator cleanup and TestFlight freshness use the dedicated service sender rather than an agent's credentials. The transport fixes each workflow's existing channel and topic, verifies the bot's live numeric identity before a write, and refuses agent identities. A binding is not permission to disclose information or activate a workflow. Do not copy an existing credential merely because its account matches. Credential access and reuse must be covered by the workflow's established authority.

## Local status

    python3 scripts/service_zulip.py --service doctor status

Use testflight or sim-reaper for the other consumers. This command makes no network call. staged_unbound means no valid local binding; bound_unverified means a local binding passed structural checks. Neither state proves live identity, delivery or workflow readiness. Exit zero is not a delivery receipt.

## Server doctor

The systemd unit reads optional /etc/fleet-services/doctor.env before dropping to jay. It supplies FLEET_DOCTOR_ZULIP_EMAIL, FLEET_DOCTOR_ZULIP_API_KEY and FLEET_DOCTOR_ZULIP_USER_ID. Keep the file root-owned and mode 0600. No credential values belong in source control.

The automatic unit does not pass --seats. Passive registration and configuration checks remain, but automatic authenticated agent MCP readiness probes are excluded. This is a diagnostic coverage limit, not a claim those MCP endpoints are ready.

## Mac consumers

The helper reads a fixed per-user file at ~/.config/fleet-services/testflight.json or sim-reaper.json. A user doctor invocation can use doctor.json if no injected doctor binding exists. Each JSON object contains email, api_key and integer user_id. The file must be regular, non-symlink, owned by the executing user, mode 0600 and at most 16 KiB. Its configuration directories must be owned, non-symlink directories without group or other write access. Partial environment bindings refuse rather than mixing sources.

## Installation and activation

TestFlight's --no-launchd installer flag and simulator cleanup's --skip-launchd --skip-hooks flags copy code without activating jobs. Tests use temporary homes and verify this distinction. Source publication does not activate a service, create credentials, expand permissions or authorize shipping. Activate only within the workflow's established authority after live identity and installed behavior checks. Keep any workflow-specific hold in force.

## Alert outcomes

Preflight refusal, including a missing binding or scanner refusal, means no message write occurred. The helper exits 3 with a fixed diagnosis, without provider response bodies or credentials.

An uncertain write exits 6. Doctor preserves uncertain edit state and retries the same message id; uncertain new posts suppress blind repetition. Simulator cleanup uses its cooldown. TestFlight retains an alert stamp marked outcome=unknown. None is a delivery receipt. Existing channels, topics and cleanup mentions are unchanged.
