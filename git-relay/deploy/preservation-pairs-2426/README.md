# Isolated preservation-pairs 2426 source package

Preparation only. No activation follows from source merge or package
creation. Merging this PR activates nothing, stages nothing on the host and
authorizes no execution; every step below is a separate owner-executed gate.

The fixed transaction family preservation-pairs-2426 replaces only
`git_relay/reviewed_preservation.py` from
`f84708ceb6def508441af18770da5a91547c724ffda11e5e9d18a16b5455f132` (the
image installed before this release, byte-identical to the pre-#2424
`origin/main` blob) to
`3f72088feb8e69b29431fb8811a4a29d3c0e5c51cbed947de34cecf2783323a3` (the
reviewed module of main after #2513: the #2424 AFC-797 tuple, the AFC-793
ingest pair, and the renewed recall pair in place of the retired Nov 21 one).
No other module, policy, seat, Worker deployment or old transaction replay is
included.

The build refuses to run until the operator supplies the execution-time
`--config-sha256` (live `/etc/git-relay/config.env`) and `--policy-sha256`
(physical seats.json policy file) digests as explicit arguments; neither is
hardcoded in source. Only digests are captured, never file contents. The
existing finite recovery modules are hash-pinned at the main blobs of
2026-11-22 (refreshed after engine PR #2440; the module closure is unchanged -
deploy_entry_prep.py, deploy_manifest.py and deploy_proof.py are not imported
by the runner package, and deploy_proof.py is supplied by the rendered proof
template), with only the executor unit literal changed for each package.
Exclusive lock, frozen single-task no-child/no-pending-write qualification,
confirmed stop, durable before-image, atomic replacement, failed-exit/boot
recovery, new process/hash proof and two runtime proof passes remain
mandatory. Failure restores the exact old module and proves the old runtime
before cleanup. Existing transaction roots are never modified or reused.

Runtime proof pins BASELINE=BEFORE and CANDIDATE=AFTER, verifies the actual
process startup selects the single expected FILE seat input (logical and
physical paths identify the same file, physical policy digest equals the
captured `--policy-sha256`), then performs authenticated read-only own-seat
reads (`wf_repo_get` plus `wf_issue_list`; the candidate pass includes ten
100-character label names to qualify dispatch; empty results are valid). No
provider write probe or synthetic surrogate is sent. The proof bearer is the
operating seat's own, staged by root at execution time (step 5); no credential
is embedded in source, manifest, logs or command arguments.

## One root, one attempt

The engine keeps the supervisor journal (`supervisor.json`) of every attempt
as audit evidence and refuses to arm a root whose journal already exists or
whose package changed after it was written
(`git-relay/git_relay/deploy_journal.py`, `Journal.arm`). A transaction root
therefore admits exactly one attempt, and a relay that is busy at the freeze
(its contract: "busy freezes resume unchanged without stopping active provider
handlers") is retried under a new root, never in place:

- the first attempt uses the original root `preservation-pairs-2426`;
- each retry builds with `--name preservation-pairs-2426-aNN` (`-a01`, `-a02`,
  ...), which gives the package a root, a runner and a unit of its own. The
  candidate image, the file row and the engine modules are identical; only
  the unit-bound `manifest.json`, `runner.py` and
  `runner-package/git_relay/deploy_runner.py` differ between attempt roots.

On the release host the original root is spent, so retries there start at
`-a01`. Its journal holds two attempts that reached the relay: on 2026-11-23
the relay was busy at the freeze and was resumed unchanged, and on 2026-11-24
a package restaged into that same root - its proof still pinning the
pre-#2513 image - passed the freeze, applied the new module, failed the
runtime proof and restored the old module, each step as designed. Both are
why every attempt is built and staged as a root of its own, and why a staged
package is never edited or rebuilt in place.

## Exact execution runbook

Every command below runs on the production host as root (sudo). `<name>` is
the attempt's root name from the section above. Nothing here is executed by
merging this PR.

1. Custody step (required once, before anything else). The relay code
   directory `/opt/git-relay/git-relay/git_relay` and its module files were
   uid 1001 before this release. `LinuxHost.preflight`
   (`git-relay/git_relay/deploy_host.py`, the `[(self.CODE, 0), (self.STATE,
   65534)]` anchor check) requires the CODE anchor root-owned with no
   group/other write bits, and the transaction baseline check requires each
   manifest row's live file to be exactly uid 0, gid 0, mode 0644. Verify
   with `stat`; take custody only if it is not already held, without
   touching trust boundaries:

       chown root:root /opt/git-relay/git-relay/git_relay
       chown root:root /opt/git-relay/git-relay/git_relay/*.py
       chmod 0755 /opt/git-relay/git-relay/git_relay
       chmod 0644 /opt/git-relay/git-relay/git_relay/reviewed_preservation.py

   The engine guard stays fully intact; only ownership/mode change so the
   anchors and baseline metadata qualify.

2. Capture the execution-time digests immediately before each build (digest
   only, never print or store file contents):

       sha256sum /etc/git-relay/config.env
       sha256sum /var/lib/private/git-relay/seats.json

   (the physical seats path; the logical `/var/lib/git-relay/seats.json`
   must identify the same file - `readlink -f` both to confirm). The proof
   re-checks the policy digest after the new image starts, so a seats.json
   change between capture and proof fails the proof and rolls back; capture
   and attempt back to back.

3. Build the package in a fresh scratch directory (the builder creates the
   output directory itself and refuses an existing one) with the merged
   checkout and the live module bytes. The builder validates the BEFORE/AFTER
   pins, the pinned engine module hashes, the rendered proof and the AST of
   every generated file; it writes only to the scratch output, never `/opt`:

       python3 git-relay/deploy/preservation-pairs-2426/build.py \
         --repo /path/to/merged/checkout \
         --before /opt/git-relay/git-relay/git_relay/reviewed_preservation.py \
         --output <fresh scratch directory> \
         --config-sha256 <config.env sha256 from step 2> \
         --policy-sha256 <seats.json sha256 from step 2> \
         --name <name>

   (`--name` omitted is the original root.) The printed `root` and `unit`
   are the two names the next steps use.

4. Stage the transaction root root-owned per the contract. The staged files
   are exactly `manifest.json`, `0.candidate`, `runner.py` and the
   `runner-package/git_relay/` modules from the build output, plus the proof
   bearer from step 5:

       install -d -m 0755 -o root -g root /opt/git-relay/transactions/<name>
       install -m 0644 -o root -g root <build>/manifest.json \
         /opt/git-relay/transactions/<name>/manifest.json
       install -m 0644 -o root -g root <build>/0.candidate \
         /opt/git-relay/transactions/<name>/0.candidate
       install -m 0644 -o root -g root <build>/runner.py \
         /opt/git-relay/transactions/<name>/runner.py
       install -d -m 0755 -o root -g root \
         /opt/git-relay/transactions/<name>/runner-package/git_relay
       install -m 0644 -o root -g root <build>/runner-package/git_relay/*.py \
         /opt/git-relay/transactions/<name>/runner-package/git_relay/

   The root must be new: nothing else may be written into it, and a root
   that already holds a `supervisor.json` is spent. Rebuilding into a spent
   root is refused at preflight, and a package assembled outside the builder
   carries no proof-to-image binding: the builder is what ties the proof's
   CANDIDATE to the manifest's `after`, so a mismatch costs a relay stop and
   an automatic rollback before it is detected. `manifest.json` is
   exactly `{"config", "files", "runner", "unit"}` with the single row
   `{"root": "code", "name": "reviewed_preservation.py", "before":
   f84708ce…, "after": 3f72088f…, "meta": {"uid": 0, "gid": 0, "mode": 420}}`
   (the code root is already the `git_relay` directory, so the row name is
   the bare module filename). `runner` is this root's runner path and `unit`
   is `git-relay-<name>.service`.

5. Stage the proof bearer at execution time: the operating seat's OWN bearer,
   the one its fleet tools already authenticate with, which holds the two
   read-only proof verbs. Root copies the seat's bearer file; its value is
   never displayed, logged or passed as an argument:

       install -m 0600 -o root -g root <the seat's own bearer store file> \
         /opt/git-relay/transactions/<name>/own-seat-proof-bearer

   It must be a single regular file owned root:root, mode 0600 (the runner
   refuses any other custody). Another seat's credential is never used, even
   one left by an earlier transaction: the proof is an own-seat read. Never
   embed the bearer in source, the manifest, logs or command arguments, and
   remove the file from the root once the attempt is over.

6. Run via a transient oneshot supervisor unit (exact command line):

       systemd-run --unit=git-relay-<name>.service \
         --property=Type=oneshot \
         --property=User=root \
         --property=TimeoutStartSec=120 \
         --property=TimeoutStopSec=90 \
         --property=KillMode=control-group \
         --property=ExecStopPost=/usr/bin/python3\ -I\ /opt/git-relay/transactions/<name>/runner.py\ recover \
         --property=UMask=0077 \
         /usr/bin/python3 -I /opt/git-relay/transactions/<name>/runner.py deploy

   These are exactly the properties `supervisor_unit` requires and
   `Runner.verify_unit` checks against actual systemd state. The outcome is
   in the unit's journal: `freeze predicates=...` means the relay was busy at
   the freeze and nothing was written (the relay was resumed unchanged);
   continue with the next attempt root from step 2.

7. Verification. The engine itself proves, before committing: a new relay
   process identity, every pinned hash on disk, `healthz` 200, and two
   passing runtime proofs (the proof above runs against the new image). The
   operator must additionally confirm afterward: the module imports in the
   running service, `sha256sum` of
   `/opt/git-relay/git-relay/git_relay/reviewed_preservation.py` equals
   `3f72088f…`, `/healthz` still answers 200, and a rejection still refuses
   unapproved pairs (an unreviewed credential-shaped patch on an admitted
   path is still refused by the normal route). Then remove the proof bearer
   from the root that ran and from every root staged for a retry.

Rollback inside the transaction is automatic: any failed phase restores the
exact old module bytes and proves the old runtime before cleanup. Reversal
after a committed transaction is never performed in place; it is a NEW
transaction with before/after swapped, separately reviewed and executed.

Before activation qualify source review, tests, current baseline/config and
policy parity, credential purpose/access, maintenance serialization and the
finite executor. No runtime execution, credential acquisition or provider
effects are accepted by merging this directory.

## Refreshing this preparation

This preparation installs main's module over the installed image, so it moves
with the module (the unit test pins the first relationship):

- a change to `reviewed_preservation.py` on main (a record added, renewed or
  retired) refreshes `AFTER`, the proof's `CANDIDATE` and `PROOF_TEMPLATE`
  here. #2513 renewed a record without doing so and the build refused at
  execution time ("reviewed preservation module candidate drift");
- a change to a pinned engine module refreshes its `MODULES` entry (as #2504
  did after #2440);
- once this release is installed, the installed image is `3f72088f…`: a later
  release of the module starts from that as its BEFORE, in this directory or
  a successor prepared for it.
