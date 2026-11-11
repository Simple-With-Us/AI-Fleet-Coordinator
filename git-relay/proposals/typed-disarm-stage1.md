# Inactive typed disarm stage 1

Local implementation candidate, not deployment or admission. New names are registered-but-unimplemented at the live dispatch boundary; HTTP still refuses them. No Worker/tool registry changes, caller cutover, credential installation, live policy update or new permissions.

The service has fixed bounded GraphQL reads, an own-App viewer receipt, exact repository/PR/head preflight, one safety-only disable, and fresh readback. An ambiguous mutation gets one read, no mutation retry. Readback failure, head drift or still-armed state is unknown. The underlying client caps response bytes at 1 MiB and per-call timeout at30s; service rejects state reads exceeding35s. Each projection is max50 labels/events. Pagination is explicitly incomplete. Returned event records preserve actor/event identifiers but do not implement setter-epoch policy. hold_decision is always not_evaluated, never permission to arm. No comments or hold mutations. Text returned by reads remains untrusted.

The factory takes an already authenticated seat from broker, checks exact action/repo policy and permission ceiling, then requests only issues:read and pull_requests:read/write from existing installation minter. Returned client projection contains authenticated viewer login, not a raw credential. App acceptance/live permission remains unqualified. Actions GITHUB_TOKEN is not a substitute.

Important limitation: GitHub disablePullRequestAutoMerge has no atomic expectedHeadOid. Preflight/readback detect drift but cannot prevent a concurrent head change between query and mutation. Receipts expose atomic_head_guard=false. Source stage is not a strict atomic-head-fenced implementation. No enable implementation is present.

Before exposing this code: authenticated broker must be the sole constructor caller; integrate policy authorization once without double rate charging, durable outcome/idempotency/audit handling, per-PR locking if desired (does not solve upstream atomicity), contract tests through HTTP+Worker and scope reconciliation for new writes. A separate stage is needed to evaluate full hold epochs/checks/reviews/CODEOWNERS before enable. This candidate preserves that policy by not implementing enable at all. No private runtime API proxy or arbitrary query/path input exists.

Transported AFC992 base: main4b41b202, per existing backlog transport confirmation; original isolated preparation used7f7d08f2541e5b9a182371686f6151f569723847. Not based on983 a25e9f43. Transport owner applied additive patches with three-way validation;983 branch untouched.

Interactive policy is a separate commit/proposal for exact17 organization repos and existing seven writes, no mint/raw tokens/personal Fleet-OPS. Not installed or reviewed. Its presence does not qualify automation.

## Offline gateway addition

TypedGateway remains unregistered in server startup and Worker tools. It accepts an authenticated seat only through existing policy.authenticate, validates schema before policy/rate charge, authorizes once via for_action and binds the service to that action/repo. Durable OutcomeStore is mandatory for this gateway; pending/unknown persists across restarts. It records an intent audit before any GH query/mutation, and a confirmed audit only after readback. Failure to persist/confirm yields unknown. Recorded successes are not replayed as current disabled state because another actor could re-enable autoMerge. All repeats are reconcile-only refusals. Local HTTP test explicitly injects this gateway into an ephemeral loopback server; production make_handler/startup remains unchanged.

Remaining independent exposure dependencies: Worker success envelope currently accepts only outcome=unknown, so confirmed_disabled cannot be forwarded without a versioned typed schema/tool registration change. No new Worker operations are advertised in this candidate. Full setter-epoch evaluator, lock/reconciliation authorization, host acceptance tests and source-review approval remain required. Existing accepted agents MCP route can carry a future separately qualified typed contract; it does not by itself grant action enablement.


## Inactive Worker v1 contract

Separate gr-typed-v1.js has exact three versioned tool specs and fixed route mapping. It is not imported by hosted-tools or registered/deployed. It validates exact caller args, streams a64KiB bound, caps request lifetime110s (preflight/write/readback), validates closed success receipt plus expected own principal/head, rejects credential reflection and fails errors to sanitized unknown without retries. Existing seven schema/behavior is untouched. Twelve Node mock contracts pass, including host code no-registration assertion. No end-to-end live auth/Access/service test is claimed.

Typed-only policy entries now require own app_slug and nonempty ceiling at startup while remaining in REGISTERED_ACTIONS, not live APP_ACTIONS. Runtime unsupported-action refusal is preserved. Complete setter-epoch eligibility, atomic upstream head fence, live routing/admission and reviewed authorization remain separate dependencies. Shared interactive service readiness need not await automation or per-client setup; user owns client delivery/setup per corrected readiness scope.
