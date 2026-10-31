# Minimal deployed Slack hardening backport

This entry point is based on the actual pr-conflict-watch artifact read from Cloudflare on 2026-10-31, not the newer Infisical/Zulip source. It preserves Slack destination, state keys/transitions, existing secrets, methods, and path-independent POST destinations. Only request buffering/validation and background concurrency change. No automatic deployment is added.

The parent source PR is independent. This release must be reviewed and approved before manual deployment, with live settings/bindings preserved and artifact readback.

Baseline artifact SHA256: `60edb0ff9d18706c2f8dfb14d9a66bf5f7fb82e3962e0e8f7f72f9d6b92b2ae5`. The original-fixture.js file is test-only and must never be deployed.

Reviewed release configuration is `slack-backport/wrangler.toml`, not the parent wrangler.toml. It targets only the existing script and KV binding. `keep_vars = true` preserves deployed vars; existing secret bindings must remain unchanged. No secret commands or new runtime resources are part of this patch.
