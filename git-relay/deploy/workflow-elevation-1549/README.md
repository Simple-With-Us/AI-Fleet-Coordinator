# One-file workflow elevation rollout (#1549)

Source preparation only. No activation occurs by merging this directory.

The new fixed transaction is git-relay-workflow-1549.service, rooted only at
/opt/git-relay/transactions/workflow-1549. It replaces workflow_verbs.py from
687610ffa3b886f24f2ce77f4513a0684cddf7d2580590028b6fcb8d9baeb078 to
33c9c8c35c2eed3af01a43b810e3672ea85f3418d1ba45f7eabc182040a2fc1b,
the exact merged #1549 image. The live-to-candidate diff is only #1549.

The builder pins the observed unchanged config digest, exact file ownership and
mode, and the existing reviewed recovery modules. Only the runner unit literal
changes in those modules. It writes an isolated scratch package, not root files
or a service. The fixed entry point must receive a private own-seat proof bearer
using the existing credential handling process. No bearer enters source,
manifest, logs or command arguments. No previous transaction manifest is reused.

Before activation: source review, tests, current baseline/config parity and
maintenance serialization must qualify. The exclusive host lock, no active
transaction guard/job, exact frozen single-task/no-child/no-pending-write proof,
confirmed stop, durable before-image, finite executor and failed-exit recovery
are mandatory. A conflict or drift aborts rather than widening the inventory.
Failure restores the exact old workflow and proves old runtime before cleanup.
The temporary start guard is transaction recovery wiring only; service config,
seat policy, auth bindings and Worker routing remain unchanged.

Runtime proof checks the own-App repository read and default branch, then one
null-object-fenced default-branch patch refusal. This checks deployed dispatch
without a ref/object write or workflow trigger. It does not prove provider write
permissions or exercise the elevation effect. Byte parity and #1549's source
tests qualify the new bearer-selection/compare-cap behavior. Two runtime proof
passes are intentional in the existing finite supervisor, with no retry loop.
Health, a new process identity and exact hashes are separate commit gates.
The dispatch component accepts exactly the two pinned image digests and refuses
unknown inventories before network calls. It deliberately has the same refusal
for both generations: rollback confirms inactive/MainPID zero before restoring
and starting, and LinuxHost pins process identity and disk hashes around proof.
This composed gate, not the refusal alone, excludes a retained candidate process.

No fulltree/main sync, auth/config/policy change, or historical outcome replay is
part of this transaction. The old #1511 builder remains untouched.
