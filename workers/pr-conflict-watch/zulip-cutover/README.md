# Existing-runtime Zulip cutover

Preserves deployed GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN, PR_STATE and webhook destinations. Adds only existing MA Alerts bot ZULIP_SITE/ZULIP_BOT_EMAIL/ZULIP_BOT_API_KEY bindings. No Infisical service token or new machine identity. There is no Slack code in this release. Main source implementation supplies the reviewed Zulip formatting, seat mentions and delivery-confirmed state changes; request bounds and concurrency match AFC-583.

Release configuration: this directory's wrangler.toml. `keep_vars = true` preserves existing runtime settings; do not use parent config or source. Bind delivery secrets before release without changing existing GitHub credentials. Remove obsolete SLACK_BOT_TOKEN only after successful replacement verification. No automatic release added.

Offline: npm ci --ignore-scripts; node --test *.test.js. No arbitrary test alert is authorized. Verify actual delivery using an existing real conflict event and preserve dedupe.

The release bundle must contain no Slack destination code. Bind delivery secrets additively, preserve GitHub credentials and state, and verify actual Zulip delivery before deleting obsolete Slack credentials.
