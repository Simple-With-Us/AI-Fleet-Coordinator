# Workflow file writes through Fleet Tools

## Defect and repair

The `publish` client rejected every `.github/workflows/` file before calling the hosted relay. That client rule lagged the relay's existing `wf_files_put` workflow-permission support. A mixed source/workflow batch could not reach the supported permission check.

The client now sends workflow files through the same full-file, head-fenced `gr_wf_files_put` route as ordinary files. This changes no server permission ceiling, App installation permission, scanner, credential source or outcome record. The directory entries `.github` and `.github/workflows` remain invalid write targets.

The relay must authorize the caller's own seat and require `workflows:write` for a workflow edit. It mints only the permissions needed for that operation. An App installation accepting `workflows:write` is not evidence that the relay's seat ceiling allows it. A hosted refusal remains a stop, not a reason to try another identity or API.

## Qualification

Local candidate, based on `8e5cb6078ec562461a686ad8ac26ee133f58a1ce`:

```
cd scripts/git-tools
python3 -m unittest test_git_transport test_scan_category test_reviewed_generic_cli test_owner_only_pr test_simple_git_wrapper test_push test_git_remote test_helper_plan test_export_catalog
```

Result: 326 tests, OK, 4 platform skips on macOS / Python 3.15.

```
cd git-relay
PYTHONPATH=. python3 -m unittest git_relay.tests.test_workflow_write_permissions git_relay.tests.test_workflow_verbs
```

Result: 73 tests, OK. These existing relay tests cover conditional workflow scope, missing-ceiling refusal before minting and parent-directory refusal. This is source qualification, not proof of a deployed write.

New client regression checks that an ordinary file and a workflow file remain in the same batch, carrying the caller identity, expected head and exact bytes. A separate regression preserves directory refusal before any commit.

## Boundaries and remaining deficiencies

- `gr_wf_file_patch` still refuses workflow paths. Its unchanged-generic-assignment preservation contract is not a replacement for full-file workflow scanning.
- Git-object `push` still refuses workflow paths. Its separate object/ref route is not upgraded by this change.
- Previously refused or outcome-unknown operations are not automatically replayed. Their owners must preserve the original evidence and reconcile outcomes before any permitted continuation.
- Frozen secret-scanner packets are not cleared by client route repair, successful tests, CI, App permission metadata or another workflow file succeeding.
- No default-branch write, new credential, alternate seat, review waiver or production relay activation is included.

Source review, exact-head CI, immutable client release qualification and an authorized full-file workflow operation with readback are still needed before calling the route restored end to end. This document records the bounded repair, not completion of the wider Fleet Tools deficiency backlog.
