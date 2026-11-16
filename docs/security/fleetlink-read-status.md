# FleetLink read status and retained own-share metadata

Source proposal only. Requires coordinated source review and normal release gates. This does not enable a gateway or backend deployment.

The hosted gateway preserves only the backend MCP not_found and gone codes for reads as fleetlink_not_found and fleetlink_expired. Unknown codes, write errors, transport failures, and unknown write outcomes retain their existing behavior. Error messages are sanitized as before. A missing or expired read is not a create receipt or proof that an earlier write failed.

The list contract gains bounded before pagination and include_expired. The backend accepts expired metadata only for scope mine; other scopes are refused. Existing content access and expiry checks are unchanged. Purged/deleted shares and missing historical audit events cannot be reconstructed from this list. Exhaustion proves only exhaustion of currently retained rows under that query.

Backend and gateway releases must be coordinated. Gateway-only adoption cannot make an older backend accept the new list arguments. An older gateway may still collapse the backend status into its generic refusal.

Future durable create receipts, idempotency keys, and retained operation history need separate design. This change supplies none and does not authorize replaying an unknown create.
