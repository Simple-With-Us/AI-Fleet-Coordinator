# Mapped Worker deploy automation

Default off: set WORKER_DEPLOY_ENABLED=true only after credentials, exact-version
receipts, and functional probes are reviewed. No secrets are rotated by this
workflow. CLOUDFLARE_FLEET_API_TOKEN (Fleet-OPS/prod/root) is the existing scoped deploy credential;
Access-protected probes use WORKER_HEALTH_ACCESS_CLIENT_ID/SECRET.  Do not widen
access or publish private receipt data to satisfy a probe.  Every probe and API
request sends an explicit User-Agent, because Cloudflare answers error 1010 to
Python's default one.

The job tests current main, serializes deployments, rechecks main immediately
before mutation, tags the Worker version with the exact SHA, verifies 100%
production routing to that version, then runs the functional probe. Wrangler
uses --keep-vars and --strict. Existing schema, bindings and migrations remain
owned by each Worker's reviewed configuration. A half-hour drain catches merges
made with bot tokens. Already-live revisions still get a health check.

## Rollback and roll-forward

While WORKER_DEPLOY_ENABLED=true the job reconciles every mapped Worker to the
exact main SHA, so a dashboard or `wrangler rollback` is replaced by main within
about 30 minutes.  To hold a manual rollback, first set WORKER_DEPLOY_ENABLED to
anything other than `true` (the job's `if:` gate then skips every run), then roll
back.  The durable rollback is a revert PR on main, which the job deploys and
verifies like any other merge.

The pre-deploy probe is deliberately fail-closed: the job will not mutate a
Worker whose live version is unhealthy or whose probe credentials are missing.
Recovering a broken live Worker is an operator action (pause the job as above,
then `wrangler rollback` or a manual `wrangler deploy` of the fix); re-enable
the job once the live probe passes again.

Incomplete targets deliberately excluded from the matrix: pr-conflict-watch. Their private
or absent health routes need an approved probe before automation can be enabled.
No public health bypass is introduced. Ambiguous start-jays-services,
fleet-share-api and BotFleet broker are not included. No iOS workflow is changed.

CI reads the scoped Cloudflare token from Fleet-OPS/prod/root through the existing
automation identity. Repository bootstrap names: INFISICAL_AUTOMATION_CLIENT_ID
and INFISICAL_AUTOMATION_CLIENT_SECRET. Provisioning those CI bootstrap secrets
is a separate approved setup step; activation remains off until cutover.
