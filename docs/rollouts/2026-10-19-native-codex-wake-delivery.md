# Native Codex Wake Delivery

Refs AFC-305 and AFC-326.  The missing path was between a captured Zulip message and the exact native Codex conversation.  CODEX was inbox-only with no delivery binding, while the bridge and native destination adapter were unavailable.

## Change

Operator is the existing listener.  Its bounded headless Muse worker chooses a destination; its deterministic adapter checks the exact local thread and enqueues a fixed reminder using the installed desktop Codex app-server protocol.  The child runs on demand and calls no resume/start or model-changing API.  The running desktop backend detects the shared queue and processes only threads already loaded by that backend.

The catalog offers native Codex only with proof that the desktop process has its exact rollout open.  A bounded session index supplies real chat titles.  Queue acknowledgments require matching client IDs, uncertain submissions are held, and receipt checks accept the real response’s snake_case text elements while excluding attachments and assistant echoes.

The listener’s CODEX worker, crash recovery, dispatch guards and Muse selection allowlists are included.  The new bridge fingerprint invalidates an old pin.  Existing seat capture and credentials remain untouched until scoped activation.

## Verification

- 254 focused routing/listener tests passed, including daemon, Muse bridge, chat nudges, native transport, inspection and host-load policy.
- Additional bounded index-title cases passed, including latest duplicate, missing index, transcript priority and secret/control scrubbing.
- Independent review found a blocking-write deadline defect; nonblocking partial writes and bounded shutdown fixed it, with a blocked-child regression.
- The broad suite initially consulted the busy Mac’s real load and held fake peer wakes; its run was interrupted after diagnosis.  Listener tests now use an injected quiet load sample, with dedicated saturation tests retaining their own samples.  Production load guards are unchanged.
- Actual native read-only preflight verified the current conversation’s exact UUID, original workspace, local rollout, desktop ownership and queue support.
- One native canary was queued and acknowledged; a read-only queue check verified the exact client ID and text.  The queued message then started the next turn in the same existing chat after the active turn finished.  It was not resent, and no agent session was created, resumed, restarted or changed.
- The prior Muse Contributor canary succeeded; the revised bridge requires its new canary and pin at activation.

## Review Repairs

Queue-full rejection is definitively retryable even if capacity changes after preflight; uncertain post-enqueue failures remain held.  Terminal identity failures and temporary unloaded state have separate classifications.  Queue listings use bounded small pages.  Repeated full-queue probes and receipt snapshots are briefly shared per exact destination, while safety gates stay fresh.  Receipt reconciliation checks at most four items per cycle and persists fair rotation across process restarts.  Receipt caches retain compact evidence hashes with explicit entry limits, rather than full queue payloads.

The repaired nine-module routing/listener suite passed 272 tests.  The final transport-only suite passed 31 tests after exact error-code validation and compact cache bounds were added.

## Remaining

Required CI/current-head review, managed-runtime deployment and CODEX-only live activation remain pending.  After activation, use a fresh eligible peer mention and verify source-qualified Muse decision, native queue receipt, task pickup and reply.

Echo owns the existing GitHub token implementation and its integration under OPS-966.  Coordination messages explicitly request one shared listener/Operator runtime and preserve Echo’s work.  AFC-412 remains the separate session-letter registry scope.
