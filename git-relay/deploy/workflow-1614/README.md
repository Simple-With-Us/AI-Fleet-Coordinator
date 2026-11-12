# Isolated workflow 1614 source package

Preparation only. No activation follows from source merge or package creation.
The new fixed transaction workflow-1614 replaces only workflow_verbs.py from
33c9c8c35c2eed3af01a43b810e3672ea85f3418d1ba45f7eabc182040a2fc1b to
88c1b914e3fcf4a6ad691e4076d1b508b483588c627c4916c4747a54f5b91604,
the exact merged 1614 image. No 1629/1638 batch, Worker deployment or old
transaction replay is included. The config digest, FILE selector and unchanged
policy digest 30aaa6b1 are separate gates. All seats and ceilings stay unchanged.

The existing finite recovery modules are hash-pinned, with only the executor
unit literal changed for this new package. Exclusive lock, frozen single-task
no-child/no-pending-write qualification, confirmed stop, durable before-image,
atomic replacement, failed-exit/boot recovery, new process/hash proof and two
runtime proof passes remain mandatory. Failure restores the exact old workflow
and proves the old runtime before cleanup. Existing transaction roots are never
modified or reused.

Runtime proof pins the actual FILE selector, physical policy hash and own-App
repository read. It uses only issue-list reads; the candidate read includes ten
100-character label names to qualify dispatch. Empty results are valid. No
provider write probe or synthetic surrogate is sent. Unit tests cover encoding,
withholding and lone-surrogate handling. ECHO must supply its own normal reads
following a separately accepted deployment. WISE proof is not ECHO access proof.

The runner reads the existing root-private WISE own-seat proof credential at
its existing purpose path directly, without copying it or acquiring an ECHO
credential. Its purpose/access and safe custody must be requalified by the
executor before any activation. Source review, current baseline/config/policy,
serialization, exact package custody and original authority must all qualify.
No runtime execution, credential acquisition or Worker effects are accepted here.
