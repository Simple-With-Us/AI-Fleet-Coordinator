# Inactive workflow verbs, stage 1 (reads, search, run control, guarded writes)

Source only. Not registered in server startup, policy action lists, the Worker tool registry or any seat policy. No deployment, credential, bearer, installation or permission change. Existing seven live verbs and their schemas are untouched. Disarm and state reads stay with #992 (`typed_disarm.py`, `typed_gateway.py`); this module has no auto-merge verb and does not touch `policy.py` or `server.py`, so the two PRs have no file overlap.

Fifteen `wf_*` verbs (a sixteenth, `wf_checks`, is refused until a seat ceiling carries checks:read) run as the seat's own App through the existing minter, each asking for only its own permissions inside the shared ceiling (contents, pull_requests, actions, metadata; checks is outside the ceiling and is requested only by wf_checks, which is refused unless the ceiling carries it):

- Reads: PR list, PR files, file get, tree, branches, commit history (optional path), code search scoped to one repo, workflow runs by branch/head sha/status, run jobs and steps. Bounded projections, never complete pagination (`complete: false`).
- Writes: update_branch (GitHub `expected_head_sha`, atomic), rerun-failed-jobs, cancel, approve (run id), file put on a non-default branch (refuses default branch, `.github/workflows/`, credential-shaped content, bad paths), merge with GitHub `sha` (atomic head match).

Known limits: no check-run reads (needs checks:read); no job logs (GitHub serves logs by redirect and the client refuses redirects); no auto-merge verbs here (#992); file put is a per-file commit, not a multi-file push.

Before exposure: an authenticated broker must be the only `for_action` caller; add policy action names and `ACTION_REQUIRED_PERMISSIONS`, durable outcomes and audit like #992's gateway for the writes, Worker/tool registration, HTTP contract tests and host qualification. Canonical per-bot identities (including Echo) are policy entries only; none are added here.

Stage 2c adds `wf_files_put` (contents:write): one commit over up to 20 files via git-data (blobs, tree, commit, fast-forward ref update). Non-default branch only, workflow paths refused, atomic on `expected_head_sha` (a moved branch is refused, and the ref update is never forced), credential-shaped content refused, and an existing file's mode (for example an executable bit) is preserved.

Stage 3a adds the policy names: `SeatPolicy.WORKFLOW_ACTIONS` lists every `wf_*` verb, so a reviewed seat config can allow them per repo and a seat that lists one needs its own `swu-` App like any App action.  `WorkflowVerbs.for_action` still authorizes once per call and mints the least permission in `SPECS`, so `ACTION_REQUIRED_PERMISSIONS` stays the map for the earlier App actions and `SPECS` is the map for these.  The HTTP server refuses every `wf_*` action as registered but not wired: no broker, durable outcome or audit path exists for them yet, and that wiring is the next stage.

Stage 3b wires the `wf_` verbs into `Relay.handle`, off by default: unless `GIT_RELAY_ENABLE_WORKFLOW_VERBS=1` every `wf_` action is still refused as registered but not wired.  When enabled, the call follows the same order as every other action: secret scan, envelope check, `policy.authorize` (allowlist, repo, hourly quota, counted once), idempotency, dispatch, one audit row.  Dispatch calls `WorkflowVerbs.for_action(..., preauthorized=True)`, which only checks the seat's App ceiling and mints the least permission in `SPECS`; a ceiling shortfall is a 403, an unavailable App credential a 503, and a bad shape a 400 before any upstream call.  Reads need no outcome key.  Writes take an outcome key, so an identical replay is served from the stored result and an upstream timeout or 5xx holds the key as unknown and refuses replay (409).  The audit row for a `wf_` call keeps only bounded identifiers and counts (run, job, ref, branch, path, file count, result commit); file bytes, log text, commit messages and result bodies are never logged.  Canonical identities, including Echo, stay seat entries in the reviewed `GIT_RELAY_SEATS_JSON`; no seat is added by code.  Host rollout, the flag and any seat entries remain Echo's.

## Stage 2b supersession (PR1005 conflict resolution)

The original job-log tail proposal is superseded by the existing main
`wf_job_log(repo, job_id, offset?, length?)` range contract. It returns
`content`, `offset`, `next_offset`, `total`, `complete` and `job_id`.
`tail_bytes`, `log_tail` and `truncated` are not supported arguments/receipts.
Current main already implements bounded own-App first-hop download, validated
single unauthenticated storage fetch, UTF-8 boundary handling and secret scanning.
Conflict resolution preserves that implementation and hosted receipt contract;
it does not add a second handler, obsolete caller schema or unused tail helper.
The prior tail tests are replaced by main's binary/log tests. No deployment,
new permissions, review waiver or live log fetch is performed by this resolution.
