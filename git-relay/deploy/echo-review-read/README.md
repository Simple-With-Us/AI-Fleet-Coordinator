# ECHO review-thread read restoration

Source preparation only. Merging does not activate this package.

The current relay selects GIT_RELAY_SEATS_FILE, not inline JSON. This fixed
transaction appends only wf_review_thread_comments to the existing swu-echo
action list. All other seats, fields, own-App identity, auth, repository scopes
and permission ceilings are preserved. The handler already exists and requires
pull_requests:read; ECHO already has that ceiling. No code/config replacement,
new seat, provider permission, identity or credential audience is introduced.

The exact observed before/candidate hashes and config hash are pinned in the
builder. The only manifest target is state/seats.json, mode0600 and existing
65534 ownership. Policy contents stay on the host; candidates are private0600,
never source/log material. Baseline drift refuses rather than widening scope.

The existing finite supervisor/recovery package is hash-pinned and unchanged
except its fixed unit literal. Its lock, frozen single-task/no-child/no-pending
write checks, stop confirmation, durable before-image, atomic replacement,
failed-exit/boot recovery and rollback runtime proof remain mandatory. A new
transaction directory/unit/manifest is used, never an earlier operation replay.

The fixed runner reads the existing root-private WISE proof credential directly
at its existing purpose path. It neither copies it nor acquires ECHO credentials.
Runtime proof accepts only the two exact policy hashes, verifies the actual
process startup selects the single expected FILE input and that both logical
and physical file paths identify the same file, then performs a WISE own-App
repository read. LinuxHost separately pins process identity and hashes around
proof. This proves service/unchanged-seat health, not ECHO access.

After successful activation ECHO must perform its own exact-head thread-comment
read through its normal route. That result is required for task closeout, not
substituted by WISE reads. Rollback restores the exact old policy and proves the
old process/config selector and WISE read. No provider write probe is needed.

Before activation qualify source review, tests, current baseline/config parity,
credential purpose/access, maintenance serialization and the finite executor.
No blanket seat expansion, selector migration or completed1549restart is allowed.
