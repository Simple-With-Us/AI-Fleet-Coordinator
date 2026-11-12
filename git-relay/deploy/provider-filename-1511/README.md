# Bounded provider filename rollout for the existing relay

Source preparation only. This follow-up makes the runtime adaptation reviewable.
The merged #1511 matcher was not installed by its source merge, and its review
did not cover this builder, fixed runner binding or runtime proof.

## Exact scope

The builder accepts one deployed workflow image, SHA256
`7a4a7cc41f9be1764fb830fdc8eeeb61f47062bb2cb14f7dc814347b6c55fc3b`.
It replaces exactly 18 provider-regex constants with the merged shared matcher
and adds one import. Slack/AWS alternatives and every other workflow operation
remain unchanged. The result must hash to
`2d193e4d0699bd31ca978398ad2d7a571567878ec9edf9ee695058722adfa08e`.
The second image is the exact merged `provider_scan.py`, SHA256
`e3879275fca91f450cebd5151d929c64376e842df5906de94fef175c1e89b685`.

No whole-main workflow adoption, new capability, seat policy, fixture registry,
credential binding or retained outcome is changed. The existing service remains
`git-relay.service`. The new finite executor binding is exclusively
`git-relay-provider-1511.service`, with a fixed private transaction directory.
No previous #1429 manifest, command or authority is reused.

`build.py` only writes a new scratch directory. It reads the pinned baseline and
8 hash-pinned recovery modules from the checkout, changes the finite runner's
unit literal, adds this follow-up's runtime proof and creates a pinned manifest
and entry point. It does not stage root files, collect a bearer, install a unit,
restart a service or activate anything. The generated entry point still needs
its private own-seat proof credential under the existing credential handling
rules. Never place it in source, manifests, logs or command arguments.

## Recovery and qualification gates

The recovery modules are byte-identical to the reviewed existing finite
transaction except for the new fixed runner unit and this runtime proof.
Exclusive flock, exact source/config/metadata checks, durable before-images and
journal, exact process quiescence with no handlers/children/pending writes,
finite systemd execution and failed-exit recovery remain required. A durable
start guard is installed before freezing. Two-file writes occur only after a
confirmed stop. Failure restores the old workflow and removes the previously
absent provider module. The old runtime must pass proof before recovery ends.
A concurrent change, wrong baseline or existing transaction guard refuses closed.

The runtime proof first verifies the own-App repository read and that the
repository's default branch is `main`. It submits bounded patch requests only
against `main`, which the actual workflow refuses before content reads or any
object/ref write. Ordinary long risk/disk-admin messages distinguish the old
invalid-message refusal from the new default-branch refusal. A runtime-assembled
synthetic full-format OpenAI shape must still fail at the message gate. It is
not a real credential. There are no live create/replay probes. Successful
qualification needs new process identity, exact file hashes, health and the
own-seat no-write proof. The finite supervisor's existing commit verification
may evaluate that bounded proof twice; there is no network retry loop.

## Evidence before activation

- 38 matcher/issue/label/review consumer tests pass against a copy of the actual
  deployed package plus only these two candidate images.
- The 172 tests shipped with the old service have the same 3 failures and 11
  errors before and after. Those old test/dependency gaps are not a green suite.
- 48 recovery/transaction/host/guard tests pass with the adapted finite modules
  under umask 022, including per-file crash and repeated recovery cases.
- The new proof was evaluated against both actual old and candidate workflow
  classes with a read-only fake provider. Both expected refusal sequences pass.

No live mutation has happened. Normal source review, CI, current maintenance
custody checks, exact current baseline verification and qualified operator
activation are still required. Review of #1511 alone is not review of this code.
The known supported full-format corpus remains detected; undocumented future
markerless embedded formats retain #1511's explicit uncertainty.
