# Reviewed fixture 2540 narrow activation package

Source preparation only. This package does not activate itself or establish permission to stop a service. It uses the existing single-module transaction family without expanding the old five-file transaction.

## Exact installed scope

Only `git_relay/reviewed_fixtures.py` changes, from SHA256 `b56913a91a4d89b10ef82ec5f820cc3f79b7605644a264bdab536202fa92cd1f` to `1ceb9b2583248e5da133b27785d6ca00977b46d0d70334291ddbc078af1267d5`, the module merged by AFC #2540 at `be3cce30d63b73f6f7ca97129ad8d063b7848713`. The complete diff adds exactly the reviewed Congress.Trade repository/path/full-file digest row. The runtime scan function and existing rows are unchanged. No client, App token source, workflow dispatch, collaborator code, policy, credential, Worker or other runtime file changes. Therefore this package does not activate #2532/#2533 or introduce the newer canonical credential reader.

## Transaction and proof

The builder pins the same finite recovery-engine modules as the existing preservation-pairs-2426 package. These modules are staged inside this transaction's runner package, not installed into the serving relay. It requires explicit execution-time configuration and physical seat-policy digests. It checks exact baseline/candidate bytes, engine hashes, proof hash, generated AST and manifest bindings. Each fresh attempt root admits one attempt; a busy freeze resumes unchanged and requires a new numbered root next time. No active handler is cancelled.

The existing engine provides exclusive lock, frozen single-task/no-child/no-pending-write qualification, confirmed stop, durable before-image, atomic replacement, failed-exit/boot recovery, new process/hash proof and two runtime proof passes. Failure restores the exact old module and proves the old runtime before cleanup. Custody and mode prerequisites remain enforced, not bypassed. The runtime proof retains exact policy selector/digest and authenticated own-seat `wf_repo_get` plus `wf_issue_list` reads. It does not use provider writes, synthetic messages, or the unrelated ten-label probe from the model package. Read proofs plus new-process and exact-module hashes establish this installation boundary; subsequent ordinary target publication is separate capability acceptance.

## Preparation and execution boundary

Build in a fresh scratch directory with this package and the merged candidate module available in the supplied checkout:

```text
python3 git-relay/deploy/reviewed-fixture-2540/build.py --repo CHECKOUT --before /opt/git-relay/git-relay/git_relay/reviewed_fixtures.py --output FRESH_SCRATCH --config-sha256 ACTUAL_CONFIG_DIGEST --policy-sha256 ACTUAL_POLICY_DIGEST --name reviewed-fixture-2540-a01
```

Before any activation, independently review this package and qualify its builder/proof regressions. At execution, re-read the service, configuration, physical policy and module identity. Stage the immutable package with the existing family's root custody and the operating seat's own proof bearer, never its value in logs or arguments. Keep the existing installed service and root untouched until those prerequisites pass. Use the existing runner/supervisor entry and recovery process, not an ad-hoc restart or reuse of an old transaction root. Execution authority must come from Jay's actual instructions; this repository document supplies none.
