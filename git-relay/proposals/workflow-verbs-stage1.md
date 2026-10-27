# Inactive workflow verbs, stage 1 (reads, search, run control, guarded writes)

Source only. Not registered in server startup, policy action lists, the Worker tool registry or any seat policy. No deployment, credential, bearer, installation or permission change. Existing seven live verbs and their schemas are untouched. Disarm and state reads stay with #992 (`typed_disarm.py`, `typed_gateway.py`); this module has no auto-merge verb and does not touch `policy.py` or `server.py`, so the two PRs have no file overlap.

Fifteen `wf_*` verbs run as the seat's own App through the existing minter, each asking for only its own permissions inside the shared ceiling (contents, pull_requests, actions, metadata; checks is outside the ceiling and is not requested):

- Reads: PR list, PR files, file get, tree, branches, commit history (optional path), code search scoped to one repo, workflow runs by branch/head sha/status, run jobs and steps. Bounded projections, never complete pagination (`complete: false`).
- Writes: update_branch (GitHub `expected_head_sha`, atomic), rerun-failed-jobs, cancel, approve (run id), file put on a non-default branch (refuses default branch, `.github/workflows/`, credential-shaped content, bad paths), merge with GitHub `sha` (atomic head match).

Known limits: no check-run reads (needs checks:read); no job logs (GitHub serves logs by redirect and the client refuses redirects); no auto-merge verbs here (#992); file put is a per-file commit, not a multi-file push.

Before exposure: an authenticated broker must be the only `for_action` caller; add policy action names and `ACTION_REQUIRED_PERMISSIONS`, durable outcomes and audit like #992's gateway for the writes, Worker/tool registration, HTTP contract tests and host qualification. Canonical per-bot identities (including Echo) are policy entries only; none are added here.
