# Worker deploy automation

Two pipelines deploy this repository's Workers, and each Worker belongs to exactly one.  `scripts/worker-targets.json` is the one mapping: its `deployer` field names the pipeline and its `paths` name that Worker's own files.

| Worker | Pipeline | Gate |
| --- | --- | --- |
| `simple-tools-mcp` | `.github/workflows/worker-deploy.yml` | `WORKER_DEPLOY_ENABLED` and `SIMPLE_TOOLS_MCP_DEPLOY_ENABLED`, both off |
| `alert-ingress` | `.github/workflows/alert-workers-deploy.yml` | A reviewed credential qualification receipt, plus the per-commit review gate |
| `pr-conflict-watch` | `.github/workflows/alert-workers-deploy.yml` | The same, plus `docs/releases/pr-conflict-zulip-verified.json` |

`pr-conflict-watch` releases from `workers/pr-conflict-watch/zulip-cutover/`, the tree that is live.

Both pipelines test current main, serialize deployments, recheck main immediately before mutation, tag the Worker version with the exact SHA, verify 100% production routing to that version, then probe it.  Wrangler uses `--keep-vars` and `--strict`.  Existing schema, bindings and migrations remain owned by each Worker's reviewed configuration.  Every probe and API request sends an explicit User-Agent, because Cloudflare answers error 1010 to Python's default one.  No secrets are rotated by either workflow.  `CLOUDFLARE_FLEET_API_TOKEN` (Fleet-OPS, prod, root) is the existing scoped deploy credential.  Do not widen access or publish private receipt data to satisfy a probe.

## What Makes A Deploy

A redeploy replaces every isolate and restarts the Worker's Durable Objects, which drops in-memory caches and unflushed buffers.  So a merge that touches none of a Worker's own files must not deploy it.

On every trigger, the script compares the live version's tag with main over that target's `paths`, minus its `ignore` globs (tests, READMEs).  No difference means no deploy, however many commits main has moved.  A change to a deploy script or workflow alone therefore deploys nothing; the push filter on `worker-deploy.yml` lets such a change run its tests.  Any doubt (no tag, a malformed tag, a tag that is not an ancestor of main) counts as changed.

A test walks every target's imports from its Wrangler `main` and fails if one falls outside that target's `paths`, because a missed path would leave a change undeployed.

### Finding The Live Tag

`wrangler secret put` creates an untagged version holding the same code, so the scripts walk back past those to the tag they copied.  They stop at any other untagged version, because a hand or API upload may hold different code.  The version they reach must also be the one deployed before the secret change.  After a rollback or an upload that was never deployed the next lower number is not the code that was live, so the answer is unknown.  The alert pipeline then refuses with "No exact release baseline" until a tagged deploy sets one, and the mapped pipeline treats the Worker as changed.

### Live Limits

An upload that does not declare `limits` removes live CPU and subrequest limits:  on Nov 4 at 8:25 PM an API upload with no limits removed the ones live on `simple-tools-mcp`.  Both pipelines therefore refuse a deploy when the live Worker has a limit its config does not declare, so declare it in the config first.  A changed value in the config is allowed by the mapped pipeline.  In the alert pipeline any `wrangler.toml` change stays a manual reconciliation, as it was.

## The Budget Guard

The account watchdog stops a Worker by leaving a `workers/message` that starts with `GUARD-TRIPPED`.  Reconciling to main would silently undo that, so both pipelines read the live deployment and version first, and again just before upload, then refuse with a failed run.  In the alert pipeline the other target still proceeds, because qualification does not fail on a stopped Worker's probe.  The settings read must still succeed, so a receipt shows the credential reached it.  Its config and tag baseline are checked but cannot fail the step, because a stopped Worker is not released, and its probe is skipped.  Re-arming is manual:  fix the cause, then deploy by hand with a normal message or run the watchdog's `rearm`, and the next run reconciles as usual.  The refusal reads only the newest deployment.  Any later deployment clears it, including a `wrangler secret put`, so do not change a stopped Worker's secrets by hand before you mean to re-arm it.

## Alert Worker Receipts

The alert pipeline needs `docs/releases/alert-credentials-qualified.json` before its first release.  It records one successful nondeployment run, proving the credential reaches the workflow and reads each target that is not awaiting its Zulip cutover, with the release step skipped.

The receipt is void only when what it proved changes: the workflow's two credential steps (matched by name) or `scripts/infisical-fetch.mjs`.  A lockfile, an action bump or a target list change leaves it valid.  Those are not left unguarded: every commit in the release range that touches `RELEASE_PATHS` must still be a merged pull request with a qualified Kody review and resolved threads, or the release refuses.

To record a receipt:

1. Dispatch `Deploy reviewed alert Workers` on main with `qualify_only` true.
2. Open a pull request setting `run_id`, `source_sha` (the run's head), `nondeployment: true` and `gate: 2` in the receipt file, and merge it after review.

`gate` must match `RECEIPT_GATE` in `scripts/deploy-alert-workers.py`, so a receipt recorded before the gate narrowed cannot arm a release by itself.

## Arming `simple-tools-mcp`

`scripts/simple-tools-mcp/DEPLOY.md` still says this Worker is deployed by hand and never from CI.  That is an open owner decision, and nothing here changes it.  The automation is prepared and stays off until the owner answers.  To arm it after that answer:

1. Update the never-from-CI wording in `DEPLOY.md`, `wrangler.jsonc`, `package.json` and `agent-sync-mcp.yml`.
2. Declare the live limits in `wrangler.jsonc`.
3. Create the `MCP_CANARY_BEARER` repository secret with a bearer chosen for the canary.
4. Set `WORKER_DEPLOY_ENABLED` and `SIMPLE_TOOLS_MCP_DEPLOY_ENABLED` to `true`.

Once armed, a deploy waits if the Worker was deployed in the last hour, and the next scheduled run retries, so a burst of merges cannot restart the Durable Objects repeatedly.  The canary replaces trust in the static `/health` route:  an unauthenticated `initialize` must answer the OAuth 401, then the bearer must initialize, list the tools and call the read-only `identities` tool.  The job refuses to start a deploy when the canary bearer is absent, and it runs the same canary against the old version before uploading, so an expired bearer stops the deploy instead of failing it after new code is live.  Seat bearers last 90 days.  A failed canary fails the run and leaves the new version live, so recovery is the rollback below.  A run that deploys nothing certifies the live Worker the same way, so a failed canary keeps failing every scheduled run until it is fixed.  That costs six requests to the Worker per run, the probe and five for the canary, at two runs an hour, once armed.

## Rollback And Roll-Forward

While a pipeline is on, a dashboard or `wrangler rollback` of a Worker whose files differ from main is replaced within about 30 minutes.  To hold a manual rollback of `simple-tools-mcp`, first set `SIMPLE_TOOLS_MCP_DEPLOY_ENABLED` to anything other than `true`, then roll back.  To hold one of an alert Worker, disable `Deploy reviewed alert Workers`, then roll back.  The durable rollback is a revert pull request on main, which the pipeline deploys and verifies like any other merge.

The pre-deploy probe is deliberately fail-closed: neither pipeline mutates a Worker whose live version is unhealthy or whose probe credentials are missing.  Recovering a broken live Worker is an operator action (pause as above, then `wrangler rollback` or a manual `wrangler deploy --tag <sha>` of the fix).

No public health bypass is introduced.  Ambiguous `start-jays-services`, `fleet-share-api` and the BotFleet broker are not included.  No iOS workflow is changed.

CI reads the scoped Cloudflare token from Fleet-OPS/prod/root using a per-use-case
Infisical service token.  Bind the Fleet-OPS/prod root-scoped read token for this
workflow as `AFC_WORKER_DEPLOY_INFISICAL_TOKEN`; bind a separate Fleet-OPS/prod
root-scoped read token for alert-worker deploys as `AFC_ALERT_WORKERS_INFISICAL_TOKEN`.

The source migration also requires explicit project configuration before activation.  Telemetry snippet generation requires the component-specific `INFISICAL_TELEMETRY_CONFIG_PROJECT_ID` alongside `INFISICAL_TELEMETRY_CONFIG_READ_TOKEN`; the generic `INFISICAL_PROJECT_ID` never supplies this bootstrap.  Before running the telemetry generator, stage and read back the exact project's dedicated selector and reader binding together.  Wise relay execution requires `INFISICAL_FLEET_OPS_PROJECT_ID` alongside `WISE_RELAY_INFISICAL_TOKEN`.  Missing or malformed project configuration fails before network access, and token metadata must match the independently configured project.

The reusable Wise workflow retains the secret-based project configuration route used by the alert-worker deploy job.  A caller with an explicit secrets map must forward `wise_infisical_project_id: ${{ secrets.INFISICAL_FLEET_OPS_PROJECT_ID }}` and `wise_infisical_token`; a caller using `secrets: inherit` may supply the canonical `INFISICAL_FLEET_OPS_PROJECT_ID` and `WISE_RELAY_INFISICAL_TOKEN` names.  Before enabling `use_wise_relay` or updating its pinned reusable revision, stage the exact consuming repository's project and reader bindings, verify names-only delivery metadata, and run the bounded grant/project readback in the activation plan.  Source review does not establish these live bindings, install credentials, or activate a runtime.

Provisioning and registering either token is a separate setup step.  This code change
does not establish that either GitHub secret has been configured; deployment remains
off until the token binding and registry scope are verified.

The fleet-facts and needs-owner standalone installers carry the existing low-level scoped verifier (`fleet_secrets/__init__.py` and `core.py`) alongside fleet_knobs.  They do not install the fleet-secrets CLI.  Before activating either consumer, install its reviewed stable copy and stage its dedicated `FLEET_KNOBS_INFISICAL_TOKEN` reader with the established AI Fleet Coordinator project, prod environment, exact root folder, and read-only permission.  The explicit reader is still checked against that independent expected authority before any named secret read; no ambient project selector or machine login supplies a fallback.
