# Wise relay principal guards

## Context

ST automation must use swu-wise[bot], never a personal-account fallback. This source change is staged: no existing caller is repinned here and no deployed credentials or relay policy are changed by merging source.

## Changes

- Shared armer requires an exact configured bot principal before arming.
- Disarm uses and verifies only repository github-actions[bot], with state readback even when the write response is lost.
- Opt-in Wise relay path fetches dedicated Infisical config inside the armer job. It verifies config, token lifetime, relay App and GitHub viewer; token never crosses job outputs.
- Infisical CLI is pinned with upstream release SHA256, fetched over HTTPS. No fetched shell script executes.
- Relay minter supports checks permission name; proposed Wise policy grants checks read only and exact enumerated owned repositories. Other seats and workflows/admin permissions are unchanged.

## Verification

`python3 scripts/test_wise_relay_exec.py` (9), `python3 scripts/test_merge_principal_guard.py` (13), `python3 scripts/test_auto_merge_reusable.py` (29), `python3 git-relay/proposals/test_wise_st_policy.py` (5). YAML parsing and extracted bash syntax checks pass. Full relay regression suite must pass before landing.

## Adoption requirements

Dedicated purpose-scoped read-only Infisical token, provisioned Wise shared bearer, reviewed additive deployed policy and checks-read capability, exact bot principal/read-only canary, then caller repin. Do not force shared guard onto unqualified callers. No personal fallback on failure. Deployed runtime and custody owners coordinate their own changes. This PR does not claim fleetwide enforcement or live migration completion.

## Review fixes

Job-local mint now execs trusted probe/principal/arm logic in the same step, without GITHUB_ENV or job-output persistence. The legacy path is explicitly skipped for relay consumers. Repository classify/disarm use github.token. Service token binds Infisical project, no project UUID in source. Null/malformed GitHub viewer responses refuse cleanly; explicit client User-Agent is shared by embedded and standalone code. Adapter11, principal/workflow containment14 and existing armer29 cases pass.
