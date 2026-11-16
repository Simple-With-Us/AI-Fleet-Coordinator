# Exact-file review record: git_relay/server.py correlation wiring (v2)

This standalone file is the durable complete review record for the registry admission in git-relay/git_relay/patch_secretscan.py (the `_REVIEWED_GENERIC_FIXTURES` entry for `git-relay/git_relay/server.py`), and is linked from the admission PR.  The matching entry in the monolithic docs/security/reviewed-generic-fixtures.md index is pending:  that doc's pristine bytes are refused whole-file by the bot-route scanner because of pre-existing fixture text in its existing sections, not because of anything in this record.  The monolithic index entry is tracked follow-through under its own route resolution and is not a blocker for this admission.

## AI-Fleet-Coordinator git-relay server, reviewed November 13, 2026 (v2)

- Repository: `Simple-With-Us/AI-Fleet-Coordinator`
- Path: `git-relay/git_relay/server.py`
- SHA-256: `873109fe354a21ea6907722ebb14d35907f2c06fbbedd6dc49b1d48e99c0e4d1`
- Size: 79,095 UTF-8 bytes, 1,728 lines.
- Immutable local evidence repository
  `/Users/jay/.local/state/fleet/afc-serverpy-corr-evidence-20261114-v2` on the Mac,
  commit `0e09325518b5b9738a1ba860da6f7e8b712d2d9c`, blob
  `cbcd0f325189ea3f7a2b7e91d18a4a46cb5e3939`.  This is a local immutable
  full-content snapshot, not a claim that the final bytes are already published.
- Baseline: main `ce6fd24a6a1fc1f9f55ccd38dfdce48f2613c0da` pristine same path,
  SHA-256 `2c06fe036dac53cf0be463e3e6c4c4912524edf75b4a8190316fd180f27e853d`,
  78,304 bytes;  the change is +15/-1 and binds a per-attempt correlation id
  into the audit funnel.  These are the identical wiring hunks previously
  reviewed against the older base;  this v2 review covers the new bytes in full.

The complete file was read and reviewed, not only its diff.  It is the git-relay
MCP server:  seat authentication, policy gates, the audit funnel, outcome dedup
and dispatch to the GitHub client.  Independent reviewer
agent-01M5DQZ8N090A65VBX6ZN99WKD recomputed the full-file digest, reversed the
+15/-1 patch to the exact baseline digest, read all 1,728 lines / 79,095 bytes,
found no credential literal and no injected content, confirmed the single
scanner hit is the same method-call false positive as v1 - assigned secret at
line 1051 of the v1 numbering, where the local variable named token receives
the result of self.app_minter.token_for_namespace, an App-token minter call -
and executed the supplied scanner to confirm the exact new triple is the only
admission.  It is an unquoted routine call, not a stored key or credential
value;  the identical construct is in the pristine baseline, which fails the
same scan.  Pre-existing is provenance only, not a reason to waive a secret.

The v1 admission (digest 395d4567, reviewed against base d0cc7fc8) is
superseded:  its bytes no longer apply to main after #2215 changed this file.
The v1 review is not void for its original bytes;  it is unusable for the new
bytes, which is why this v2 review exists.  No digest transfer occurred.

Independent pattern enumeration on the exact v2 bytes:  the generic assignment
hit is the admitted one;  zero dedicated-provider or bearer-header hits;  zero
Zulip-shaped key hits.  Loaded-credential enforcement is unchanged and remains
absolute;  an offline review without live loaded credentials is not evidence
about their current values.

The registry admits only this repository, full path, and full-file digest.  A
byte change, another repository or path, message prose, or another action does
not gain admission;  any byte drift requires renewed independent exact-file
review.  This source review must pass the normal review/check/merge gates
before its scanner is installed.  Do not patch a live registry to unblock
publication.  This record is an independent source review;  it is not an
owner approval.

## v3 (2026-11-14): admission advanced to the final wired bytes - independent verdict

Causal record: the v2 admission (873109fe) covered pristine main plus the +15/-1
corr wiring, and the wiring had not landed, so it matched no live branch:  not
main (2c06fe03, byte-identical to the ce6fd24a pristine baseline) and not the
PR branches (older pre-#2122 bytes).  Kody and Sentry detected the inert waiver
on #2219.  An intermediate re-admission of 2c06fe03 based on Echo's own delta
review was held:  a delta review is not the independent exact-file review this
process requires.

Final admitted bytes:  pristine main + corr wiring + the begin() caller-error
400 arm (a malformed corr is a definite 400 bad_request, never the capacity
503), 1,735 lines / 79,506 UTF-8 bytes,
SHA-256 0d7371110d7b59af92415da4892ebaab46b2ed841e8d8623248fc0a0ef21f2de.

Independent reviewer agent-01M5DQZ8N090A65VBX6ZN99WKD verdict (renewed
whole-file review, 2026-11-14):  independently recomputed the digest (1,735
lines / 79,506 bytes), compiled the file, reproduced pristine-main + wiring +
arm byte-identically, enumerated the full file, and executed the scanner:  the
only hit is the same generic-assignment false positive (line 1058 of the final
numbering);  the exact admission clears it;  absolute loaded-credential
protections are unchanged.  The current-main bytes independently match the
reconstructed pristine base;  no drift.

Conditions recorded with the verdict:  this registry admits EXACTLY the final
triple - no dead v1/v2 entries alongside;  the relay host's vendored scanner is
synced to this admission before the final bytes are written through it;  any
further byte change renews independent exact-file review.  Echo-side checks:
begin() raises the caller-input ValueError before any durable row write
(pinned by test_begin_rejects_a_non_string_corr_before_it_can_persist and
test_held_key_replay_wins_over_corr_validation), so a 400 audit stamp cannot
misrepresent an existing ledger row.  Landing path:  #2219 admits the final
triple;  #2216 then syncs main and lands the wiring plus the 400 arm as
exactly these bytes.
## v4 - D2 import-order correction (2026-11-14, UTC)

The v3 final bytes (0d737111) carried an isort violation: the corr wiring had
placed "import uuid" between shutil and tempfile. autofix.ci (ruff format +
ruff check --fix-only, default rule set) would have rewritten that one line on
any branch landing the bytes, breaking byte equality with the admission. The
bytes were corrected BEFORE any landing: the import moved to sorted position,
one line moved, no other byte change. Corrected candidate: sha256
320d5958b16102a222434e8513c64c9df1f1758446f1c67b17a422868824cb15 (1,735 lines,
79,506 bytes), qualified with ruff 0.17.5 format --check (already formatted),
ruff check --fix-only --diff (no changes - exact autofix.ci reproduction),
py_compile, and the 96-test outcome_archive/capacity/typed_gateway suites.

Independent renewed whole-file review of the corrected bytes: digest
recomputed, compiles, the supplied one-line diff reproduces the candidate
exactly; full enumeration found zero dedicated-credential, high-entropy or
instruction hits; the verified scanner reports only the same benign
generic-assignment false positive (line 1058), which this exact D2 triple
clears without broadening. The reviewer holds no Ruff; formatter and test
results are Echo source evidence plus CI. Verdict: suitable for exact-file
admission. This PR replaces the 0d737111 triple with the D2 triple only; no
dead entries are retained.

### Recorded retractions (Echo, 2026-11-14)

1. The outcome-archive branch was reported as carrying pre-#2122 server.py
   bytes. Verified false: 2581f00b's server.py is pristine 2c06fe03. The write
   refusal's sole cause was the missing installed admission.
2. The "Signal the server to update its fleet tools" workflow was treated as
   deploying the relay service. Verified false: it updates only the fleet-cli
   tools tree (journal: UPDATED ad56cff9 -> c0d0f74e, 08:29:46Z). The relay
   service under /opt/git-relay has no auto-sync; it updates only through a
   reviewed, hash-pinned transaction package activated on the host.
