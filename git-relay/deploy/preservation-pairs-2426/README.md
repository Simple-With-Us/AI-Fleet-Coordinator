# Isolated preservation-pairs 2426 source package

Preparation only. No activation follows from source merge or package
creation. Merging this PR activates nothing, stages nothing on the host and
authorizes no execution; every step below is a separate owner-executed gate.

The new fixed transaction preservation-pairs-2426 replaces only
`git_relay/reviewed_preservation.py` from
`f84708ceb6def508441af18770da5a91547c724ffda11e5e9d18a16b5455f132` (the live
installed module, byte-identical to the pre-#2424 `origin/main` blob) to
`65a92e57a31bdc984d3b9e7224b75ad899d64f0b754e92233e87315743e6eb77` (the exact
merged image of this PR: the #2424 AFC-797 tuple plus the two owner-approved
AFC-793 fixture pairs). No other module, policy, seat, Worker deployment or
old transaction replay is included.

The build refuses to run until the operator supplies the execution-time
`--config-sha256` (live `/etc/git-relay/config.env`) and `--policy-sha256`
(physical seats.json policy file) digests as explicit arguments; neither is
hardcoded in source. Only digests are captured, never file contents. The
existing finite recovery modules are hash-pinned at the current main blobs (refreshed 2026-11-22 after engine PR #2440; the module closure is unchanged - deploy_entry_prep.py, deploy_manifest.py and deploy_proof.py are not imported by the runner package, and deploy_proof.py is supplied by the rendered proof template),
with only the executor unit literal changed for this new package. Exclusive
lock, frozen single-task no-child/no-pending-write qualification, confirmed
stop, durable before-image, atomic replacement, failed-exit/boot recovery,
new process/hash proof and two runtime proof passes remain mandatory.
Failure restores the exact old module and proves the old runtime before
cleanup. Existing transaction roots are never modified or reused.

Runtime proof pins BASELINE=BEFORE and CANDIDATE=AFTER, verifies the actual
process startup selects the single expected FILE seat input (logical and
physical paths identify the same file, physical policy digest equals the
captured `--policy-sha256`), then performs authenticated read-only own-App
reads (`wf_repo_get` plus `wf_issue_list`; the candidate pass includes ten
100-character label names to qualify dispatch; empty results are valid). No
provider write probe or synthetic surrogate is sent. The proof bearer is
staged by the operator at execution time (step 5); no credential is embedded
in source, manifest, logs or command arguments.

## Exact execution runbook

Every command below runs on the production host as root (sudo; the owner has
full sudo). Nothing here is executed by merging this PR.

1. Custody step (required before anything else). The relay code directory
   `/opt/git-relay/git-relay/git_relay` and its module files are currently
   uid 1001. `LinuxHost.preflight` (`git-relay/git_relay/deploy_host.py`,
   the `[(self.CODE, 0), (self.STATE, 65534)]` anchor check at lines 179-192 of the current engine)
   requires the CODE anchor root-owned with no group/other write bits, and
   the transaction baseline check requires each manifest row's live file to
   be exactly uid 0, gid 0, mode 0644. Take qualified custody without touching
   trust boundaries:

       chown root:root /opt/git-relay/git-relay/git_relay
       chown root:root /opt/git-relay/git-relay/git_relay/*.py
       chmod 0755 /opt/git-relay/git-relay/git_relay
       chmod 0644 /opt/git-relay/git-relay/git_relay/reviewed_preservation.py

   The engine guard stays fully intact; only ownership/mode change so the
   anchors and baseline metadata qualify. Verify with `stat`.

2. Capture the execution-time digests via root (digest only, never print or
   store file contents):

       sha256sum /etc/git-relay/config.env
       sha256sum /var/lib/private/git-relay/seats.json

   (the physical seats path; the logical `/var/lib/git-relay/seats.json`
   must identify the same file - `readlink -f` both to confirm). If either
   digest has drifted by execution time, re-review before proceeding.

3. Build the package in a fresh scratch directory with the merged checkout
   and the live module bytes (the builder validates the BEFORE/AFTER pins,
   the pinned engine module hashes, the rendered proof and the AST of every
   generated file; it writes only to the scratch output, never `/opt`):

       python3 git-relay/deploy/preservation-pairs-2426/build.py \
         --repo /path/to/merged/checkout \
         --before /opt/git-relay/git-relay/git_relay/reviewed_preservation.py \
         --output /root/preservation-pairs-2426-build \
         --config-sha256 <config.env sha256 from step 2> \
         --policy-sha256 <seats.json sha256 from step 2>

4. Stage the transaction root root-owned per the contract. The staged files
   are exactly `manifest.json`, `0.candidate`, `runner.py` and the
   `runner-package/git_relay/` modules from the build output, plus the proof
   bearer from step 5:

       install -d -m 0755 -o root -g root /opt/git-relay/transactions/preservation-pairs-2426
       install -m 0644 -o root -g root <build>/manifest.json \
         /opt/git-relay/transactions/preservation-pairs-2426/manifest.json
       install -m 0644 -o root -g root <build>/0.candidate \
         /opt/git-relay/transactions/preservation-pairs-2426/0.candidate
       install -m 0644 -o root -g root <build>/runner.py \
         /opt/git-relay/transactions/preservation-pairs-2426/runner.py
       install -d -m 0755 -o root -g root \
         /opt/git-relay/transactions/preservation-pairs-2426/runner-package/git_relay
       install -m 0644 -o root -g root <build>/runner-package/git_relay/*.py \
         /opt/git-relay/transactions/preservation-pairs-2426/runner-package/git_relay/

   `manifest.json` is exactly `{"config", "files", "runner", "unit"}` with
   the single row `{"root": "code", "name": "reviewed_preservation.py",
   "before": f84708ce…, "after": 65a92e57…, "meta": {"uid": 0, "gid": 0,
   "mode": 420}}` (the code root is already the `git_relay` directory, so
   the row name is the bare module filename). `runner` is the transaction
   root runner path and `unit` is `git-relay-preservation-pairs-2426.service`.

5. Stage the proof bearer at execution time. Place a current own-seat proof
   bearer authorized for the read-only proof verbs at
   `/opt/git-relay/transactions/preservation-pairs-2426/own-seat-proof-bearer`,
   owned root:root, mode 0600, single regular file (the runner refuses any
   other custody). The existing root-private own-seat proof credential from
   an earlier transaction may be re-used by root for this fixed purpose if
   its seat still holds the read verbs; otherwise stage a fresh one through
   the normal credential handling process. Never embed the bearer in source,
   the manifest, logs or command arguments. Remove the file after the
   transaction finishes.

6. Run via a transient oneshot supervisor unit (exact command line):

       systemd-run --unit=git-relay-preservation-pairs-2426.service \
         --property=Type=oneshot \
         --property=User=root \
         --property=TimeoutStartSec=120 \
         --property=TimeoutStopSec=90 \
         --property=KillMode=control-group \
         --property=ExecStopPost=/usr/bin/python3\ -I\ /opt/git-relay/transactions/preservation-pairs-2426/runner.py\ recover \
         --property=UMask=0077 \
         /usr/bin/python3 -I /opt/git-relay/transactions/preservation-pairs-2426/runner.py deploy

   These are exactly the properties `supervisor_unit` requires and
   `Runner.verify_unit` checks against actual systemd state.

7. Verification. The engine itself proves, before committing: a new relay
   process identity, every pinned hash on disk, `healthz` 200, and two
   passing runtime proofs (the proof above runs against the new image). The
   operator must additionally confirm afterward: the module imports in the
   running service, `sha256sum` of
   `/opt/git-relay/git-relay/git_relay/reviewed_preservation.py` equals
   `65a92e57…`, `/healthz` still answers 200, and a rejection still refuses
   unapproved pairs (e.g. an unreviewed credential-shaped patch on the two
   admitted paths is still refused by the normal route).

Rollback inside the transaction is automatic: any failed phase restores the
exact old module bytes and proves the old runtime before cleanup. Reversal
after a committed transaction is never performed in place; it is a NEW
transaction with before/after swapped, separately reviewed and executed.

Before activation qualify source review, tests, current baseline/config and
policy parity, credential purpose/access, maintenance serialization and the
finite executor. No runtime execution, credential acquisition or provider
effects are accepted by merging this directory.
