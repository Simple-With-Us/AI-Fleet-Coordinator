# Default-off Linux native alert candidate

Source publication only. Nothing imports or enables this directory from the installed router. No deployment/config changes. Apply neither the adapter patch nor activation flags without the separate qualification described in REVISION1113.md.

Run `python prepare_dependencies.py`, then `node prepare_candidate.cjs SOURCE` with the authorized pinned installed source. Missing/mismatched generated files fail closed. Then run the offline test commands in REVISION1113.md. It unpacks pinned bundled dependencies without network access.

# Current review entry: REVISION1110.md

The integrated 11:10 source revision supersedes earlier status and test counts below. Nothing is activated.

# Linux native wake adapter, source-only implementation packet

2026-11-24, America/Chicago. Candidate for independent review, not deployed or live-verified. No real native endpoint, credential, conversation attachment, model request or wake dispatch was used.

## What changed

- `native-bridge.patch` and `candidate-zcode_wake.py`: default-off `native_delivery_review_only` seam through the current local single-wake/turn delivery functions. Existing per-session and legacy locks, session store/workspace/subagent checks remain. Native branch rechecks registry owner/generation and exact session binding. Native busy admission can proceed to the atomic queue route, while the unchanged standalone branch retains its busy guard. Rider batches are refused by the native branch. No native outcome is promoted to router success.
- `zcode_native.py`: private fsynced journal, one logical wake key independent of changing payload/binding, immutable full intent and fingerprint, dispatch intent saved before transport. Existing records query only, including dispatching crash records, failed ACKs and lost responses. Old attempts require explicit freshness proof and are refused by default. No resend or legacy fallback. Mac and remote bindings are refused.
- `native_flow.cjs`: authenticated hello/initialize, exact-session subscribe, one minimal sendText with queue preference, exact-key query and owned subscription cleanup. Whitelisted request fields only. Revalidates binding before initialize and before send. No createSession, guide, startNow request, held-queue disposition, settings or model override.
- `native_rpc.cjs` and `native_codec.cjs`: bounded authenticated localhost-only RPC, pinned installed-source serialization/framing. Only six native methods allowed. Redirects refused. Each outcome closes the client connection. No endpoint discovery, token reading or live entrypoint.
- `native_receipts.py`: conservative offline evidence predicate. Separates admission, persisted exact native input, started/completed turn and completed owning-alert fetch. Not yet connected to router state. A fetch is not proof of semantic handling.

## Tests performed

`python -m unittest test_native test_bridge test_receipts -v`: 34 pass. Durable intent before callback, restart query-only, uncertain/failed ACK no resend, changed text/workspace/generation conflict, old attempt exclusion, wrong ACK, Mac/remote refusal, stale registry, missing session, workspace mismatch, subagent exclusion, busy native seam and no-driver/no-fallback.

`test_flow.cjs`: 12 authenticated loopback fixture tests pass on the installed bundled Node and installed ws/zod packages. Real installed binary serialization and strict clientHello schema are evaluated. Envelope schema is extracted but its workspace/TTFT dependencies are restricted fixture stubs, explicitly not a full installed-schema pass. Tests cover normal/split frames, query-only, wrong query key, bad hello/client, stale binding, wrong ACK, lost/disconnected/timed-out/partial response and generation race after subscription. Race test unsubscribes without send. No user runtime socket was accessed.

The prior 14 isolated installed-source admission/inbox tests remain evidence for idle/busy/preempt behavior, not real application integration. This packet does not claim those mocks prove complete runtime behavior.

## Grounded host difference and Mac boundary

Linux `createServerHostServices` creates standalone-server local services without browserControlExecutor. Collector returns without reading tabs. Mac `/out/host/index.js` supplies desktop-local browserControlExecutor Xz through the main-process bridge. Native sendText invokes collection when configured; empty payload context/clientMode does not suppress it. Current URL sanitizer retains path/query/fragment after removing user/password. Actual user tabs/browser availability were not inspected. Mac native adoption remains excluded until a supported scoped omission is reviewed or scope is settled.

Linux server-core SHA256: ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0.
Active adapter base SHA256: 6b21e7847a31791dd120b6fab582eaffc4c2afd24567e512faf5f8fade23c439.
Mac host SHA256: 891b3425d9fe213a4adce82a80dd3c6ab406d1112e8c84731c7747eadd1399d9.
Mac main SHA256: 8b5822f2d2f8f03effc2b8b5fa2723d37c26b8de628dc95e920a13205c41c36e.

## Not ready for adoption

This is a component implementation with a default-off adapter seam, not a complete installed driver. In particular:

1. Production glue must combine host/source/process identity verification, endpoint/token retrieval, Python journal and RPC flow without weakening the repeated binding checks. No live launcher or credential loader is included. Driver and freshness callbacks cannot be supplied by untrusted wire requests.
2. Native freshness must come from the original router attempt ledger. A caller boolean is a fail-closed seam, not proof. Old persisted/unknown standalone attempts remain excluded.
3. Full exact installed payload/ACK/query dependency schemas and authoritative workspace conversion still require independent application-level tests. Current host methods, installed framing and strict hello are grounded, but partial schema fixtures are not that test.
4. Exact persisted cold-session readiness, idle/busy/held queue behavior and policy/provider sync need application integration coverage, including cleanup after initialization failure. Always-subscribe is conservative but can initialize an already resident session. No active-only prerequisite is invented.
5. Socket loss closes this client. Server-side route cleanup on disconnect/crash must be validated against real connection ownership teardown, particularly if subscription ACK was lost. Never stop shared runtime or cancel admitted work. Explicit unsubscribe is attempted when its identity is known; disconnect cases cannot guarantee RPC unsubscribe.
6. Real persistent transcript/timeline/input/discarded recovery and retention across runtime restart/cache loss remain unproven. Unknown/queryUnavailable never permits replay. Conservative journal behavior sacrifices delivery after a crash before actual send.
7. Accepted input remains unknown/unconfirmed in router output. SourceCommandId/session/message content evidence and actual owning integration fetch must be wired before claiming transport confirmation or whole-chain completion. No current delivery claim changed.
8. Framing caps are deliberately small and may refuse a real large subscription snapshot. Refusal after dispatch intent consumes the slot. Verify production sizes before trial, do not silently increase caps or resend.
9. No timers, deployment, config activation, exchange, new PR or host file installation occurred. Existing exception-only monitor remains separate.

## Review focus

Please inspect journal durability/identity and no-redrive semantics, bridge binding checks, native request/response shape, exact client cleanup and the missing production glue boundary. Return source changes needed before any live adoption proposal. Candidate tests do not settle the listed adoption gates.
