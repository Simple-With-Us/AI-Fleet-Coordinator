# Exact-file review record: git_relay/server.py correlation wiring

This standalone file is the durable complete review record for the registry admission in git-relay/git_relay/patch_secretscan.py (the `_REVIEWED_GENERIC_FIXTURES` entry for `git-relay/git_relay/server.py`), and is linked from the admission PR.  The matching entry in the monolithic docs/security/reviewed-generic-fixtures.md index is pending:  that doc's pristine bytes are refused whole-file by the bot-route scanner because of pre-existing fixture text in its existing sections, not because of anything in this record.  The monolithic index entry is tracked follow-through under its own route resolution and is not a blocker for this admission.

## AI-Fleet-Coordinator git-relay server, reviewed November 13, 2026

- Repository: `Simple-With-Us/AI-Fleet-Coordinator`
- Path: `git-relay/git_relay/server.py`
- SHA-256: `395d4567b2d28579d5311c620031153a04f08f262de09a6d4b05446d1988cf25`
- Size: 77,874 UTF-8 bytes, 1,699 lines.
- Immutable local evidence repository
  `/Users/jay/.local/state/fleet/afc-serverpy-corr-evidence-20261114` on the Mac,
  commit `f5446e606fbc5f6ea7c477bab97961db2ee545ee`, blob
  `0b113791b8a1f2c6a04bb473e2c2ddf743759e69`.  This is a local immutable
  full-content snapshot, not a claim that the final bytes are already published.
- Baseline: main `d0cc7fc8d876ce20e7dab4a630376f437e8db76a` pristine same path,
  SHA-256 `1731a9eb438bcc2bd66c9a9317130ae6027a4e69a6728ce415dae07e4245ab78`,
  77,083 bytes, 1,685 lines;  the change is +15/-1 and binds a per-attempt
  correlation id into the audit funnel.

The complete file was read and reviewed, not only its diff.  It is the git-relay
MCP server:  seat authentication, policy gates, the audit funnel, outcome dedup
and dispatch to the GitHub client.  Independent reviewer
agent-01M5DQZ8N090A65VBX6ZN99WKD recomputed the full-file digest, reversed the
patch to the exact baseline digest, found no credential literal and no injected
content, and confirmed the single scanner hit is a method-call false positive.
The hit is assigned secret at line 1051, where the local variable named token
receives the result of self.app_minter.token_for_namespace, an App-token minter
call.  It is an unquoted routine call, not a stored key or credential value.
The identical line is present in the pristine baseline, which fails the same
scan;  being pre-existing is provenance only, not a reason to waive a secret.

Independent pattern enumeration on the exact bytes found exactly one generic
assignment hit, no other generic assignment hits, no dedicated-provider or
bearer-header hits, and no Zulip-shaped key hits.  Loaded-credential
enforcement is unchanged and remains absolute;  an offline review without live
loaded credentials is not evidence about their current values.

The registry admits only this repository, full path, and full-file digest.  A
byte change, another repository or path, message prose, or another action does
not gain admission;  any byte drift requires renewed independent exact-file
review.  This source review must pass the normal review/check/merge gates
before its scanner is installed.  Do not patch a live registry to unblock
publication.  This record is an independent source review;  it is not an
owner approval.
