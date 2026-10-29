# Seat control MCP contract (AFC-549)

This is a code-only contract and adapter seam. `scripts/agent_sync/seat_control.py`
exports the separate `SEAT_CONTROL_TOOL_SCHEMAS` family catalog and
`SeatControlMCP`; no current MCP registry imports or registers them. Activation
needs a later, explicit integration decision.

Every target names `host_scope`, the exact catalog `host`, `platform`, native
`session_id`, and catalog-derived `owner`. The owner must still match the
resolved row. A title is returned for display only and is never used to resolve
a target. Missing, stale, duplicate, or mismatched targets fail closed. No path
creates a replacement session for an unresolved target.

## Capability matrix

`implemented` means the adapter code exists in this checkout. It does not mean
the target is active or that a live host was tested. `runtime_unverified` means
the code's behavior on that host has not been verified. Catalog discovery by
itself never grants a control capability.

| Host scope | Platform | List/status | Bounded read/output | Start/create | Separate resume | Prompt existing session | Cancel turn | Existing adapter evidence |
|---|---|---:|---:|---:|---:|---:|---:|---|
| mac | `grok-leader` | implemented | implemented | unsupported | unsupported | implemented | unsupported | `ChatCatalog` and `GrokTransport`; existing leader `session/prompt` path with exact-session preflight |
| mac | `codex` | implemented | implemented | unsupported | unsupported | implemented | unsupported | `ChatCatalog` and `CodexChatTransport`; native `thread/read` and `thread/queue/*` |
| cloud | `cursor-cloud` | implemented | implemented | unsupported | unsupported | implemented | unsupported | `CursorCloudTransport` existing-agent list/detail/conversation and run APIs |
| mac | `claude` | implemented | implemented | unsupported | unsupported | unsupported | unsupported | Catalog metadata and bounded transcript read only |
| mac | `cursor-local` | implemented | unsupported | unsupported | unsupported | unsupported | unsupported | Catalog metadata only; local transcript read/control is absent |
| mac | `minimax` | implemented | implemented | unsupported | unsupported | unsupported | unsupported | Catalog metadata and bounded transcript read only |
| mac | `muse`, `fx` | implemented | unsupported | unsupported | unsupported | unsupported | unsupported | Catalog discovery/status only in this contract |
| server | known platforms | runtime-unverified | runtime-unverified | unsupported | unsupported | runtime-unverified | unsupported | No server-host canary or runtime verification in this slice |
| any | unlisted platform | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | Unknown providers are not promoted from discovery |

Stale records report `stale` for otherwise implemented capabilities. Existing
adapter refusals (busy, permission wait, missing workspace, unavailable host)
remain refusals; the wrapper does not choose a different target.
Prompt eligibility additionally requires `capabilities.wake is True` from the
catalog. Missing, malformed, false, or merely truthy non-boolean wake flags
refuse dispatch. Busy, active, unknown, and permission-waiting rows retain a
`held` receipt without reaching transport preflight or send.

### List bound review disposition

The review claim that `ChatCatalog.discover()` performs an unbounded scan and
returns an unbounded collection is not borne out by the implementation at this
branch head. `ChatCatalog` defaults to `max_records=24` and clamps it to
`1..1000`; its 30-second default TTL caches the bounded result under a scan
lock. Filesystem and Grok enumerators cap at `MAX_FILES_PER_SOURCE=160`;
Cursor Cloud discovery caps at two pages and `_MAX_AGENTS=200`, and the catalog
limits those records by `max_records`. The aggregate cache is capped to
`max_records` before `discover()` returns. The seat-control adapter applies its
own response limit (maximum 24) and exact host filter. Evidence:
`scripts/agent_sync/chat_catalog.py` (`DEFAULT_TTL`, `MAX_FILES_PER_SOURCE`,
constructor, `_scan_cursor`, `_discover`, `discover`),
`scripts/agent_sync/cursor_chat_transport.py` (`_MAX_PAGES`, `_MAX_AGENTS`,
`discover`), and the existing `test_catalog_cap_is_fair_across_platforms`,
`test_discovery_is_ttl_cached_and_ids_are_stable`, and
`test_discovery_caps_results_and_page_fetches` fixtures. The adapter does not
need a shared catalog rewrite for this finding.

## Authorization and delivery

The MCP call context supplies `caller_id`; tool arguments cannot assert an
identity. The adapter requires injected caller authorization, an audit sink,
and the existing per-seat `IdemStore`. The feature is disabled by default and
advertises no tools until explicitly enabled. Enabling read tools does not
enable mutations; prompt dispatch has a separate default-off gate and requires
a per-request approval callback. Start/create, separate resume, and cancel-turn
are explicitly unsupported and are never advertised as runnable tools.
The approval callback must be a stable policy check for that caller, request,
and target: the adapter checks it before dispatch and the transport rechecks it
at the send boundary. It must not consume a one-shot approval token.

Prompt dispatch re-resolves the exact host/platform/session/owner and retains
that record's workspace binding and native model context. The caller cannot
override either workspace or model. Before any user-supplied prompt is
fingerprinted, persisted, logged, or relayed, the adapter scans it with the
fleet `secretscan` module (including integrator-supplied `scan_known` forms for
the caller's own credentials). A refused secret shape returns `invalid_request`
with outcome `secret_refused` in audit, names only the `prompt` field to the
caller, and persists nothing. It checks the existing `ChatNudges` snapshot and
holds when a pending, dispatching, or uncertain nudge already targets the same
session. It then delegates to the existing `ChatTransport`; it does not spawn
an ACP process or use shell commands. Provider timeouts stay in those native
transports. The adapter returns only `accepted`, `held`, `failed`, or `unknown`
outcomes. Existing acknowledgements do not prove pickup or completion, so both
remain false until a provider offers a separately verified receipt.

The idempotency key is scoped to authenticated caller plus request ID. A
different target or prompt under the same key is a conflict. Accepted requests
are stored as **ok** (`sent` rows whose `result.status` is `accepted`) and replay
as duplicate without a second send. Held and failed outcomes are stored as
**refused** (`refused` rows with request-scoped TTL) so the same receipt is
returned until expiry, then the caller may retry. Transient adapter busy/deferred
results before or after preflight release the idempotency row immediately so the
caller can retry when the target becomes ready. Definite authorization,
validation, and catalog-resolution failures do not persist an idempotency row and
never erase an existing no-replay receipt for the same request id. An uncertain
delivery outcome is stored as **unknown** with a long-lived tombstone and is never
replayed automatically. Removing tombstones or resetting the store can erase
no-replay evidence for ok or unknown sends, so integrations must preserve those
rows as long as old request IDs can be retried. OAuth token minting and HTTP
status handling (401, 403, 429, invalid_grant) stay in the existing native
transports; this adapter does not release rows based on provider auth responses.
If terminal persistence fails after dispatch, the result is unknown and the
adapter makes a best-effort unknown-state write. Audit entries contain event,
operation, outcome, authenticated seat, requested acting identity (redacted when
secret-shaped; otherwise the target owner when a session is named), optional
mutation gate and delegation markers, prompt length, and timestamp; host,
platform, native session, request ID, prompt content, and hashes of those
identifiers are omitted. Integrators must inject `scan_known` when enabling
mutations, and provide the existing durable store and audit sink before
registering the tools.

## MCP integration boundary

The separate family catalog is intentionally not added to `scripts/agent_sync/mcp/tools.json`.
That file is consumed by both stdio and hosted MCP paths, while the current hosted
contract filters and authorizes a separate tool list. Adding schemas alone could
advertise a tool without a handler or authorization rule. A later integration
must add, in the same reviewed change:

1. stdio registration plus handler dispatch and explicit read/write identity classification;
2. hosted `HOSTED_TOOL_LIST`, `HOSTED_READ_TOOLS`/`HOSTED_WRITE_TOOLS` classification,
   identity parity, and handler/proxy dispatch;
3. a server-side feature gate that defaults off and gates both read and mutation
   exposure until each host adapter is enabled;
4. tests proving parity between stdio and hosted schemas, caller identity,
   mutation approval, and fail-closed behavior when the handler is absent.

Echo has an exclusive reservation for the unified ST-MCP/Git-relay auth,
admin, and Worker integration. This slice does not edit those shared files.

## Compatibility and eventual cutover

This patch leaves `scripts/seat-mcp`, its Mac-only APIs, the `/hooks/` proxy,
and every webhook consumer unchanged. BotFleet compatibility for BF-2457 and
BF-2543 needs a consumer inventory, a parallel compatibility period, fixture
contract tests, owner-approved migration steps, and rollback proof before any
legacy route can be retired. This document does not authorize or perform that
cutover.

Shellular keeps its existing paired-client and encrypted relay protocol. The
private `AgentsManager` pairing path is not imported or copied. OPS-1000 remains
the single Shellular server supervisor; this adapter does not start, stop, or
supervise daemons. No credentials, permissions, network listeners, or runtime
host settings are provisioned here.

## Fixture verification

`python3 -m unittest discover -s agent_sync/tests -p 'test_seat_control.py'`
from `scripts/` runs local-only fixtures for identity separation, host mismatch,
stale/ambiguous targets, permissions, mutation gating, idempotency, unknown
outcomes, bounded output, audit redaction, busy/queued state, and cancellation
semantics, strict catalog wake eligibility, and non-replay of held/failed
receipts. The existing chat-catalog fixtures verify its cache and per-source
bounds. These checks do not call provider APIs or start a session.
