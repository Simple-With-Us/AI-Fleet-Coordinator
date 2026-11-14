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
