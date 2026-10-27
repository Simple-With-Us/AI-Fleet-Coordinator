# Inactive typed disarm stage 1

Local implementation candidate, not deployment or admission. New names are registered-but-unimplemented at the live dispatch boundary; HTTP still refuses them. No Worker/tool registry changes, caller cutover, credential installation, live policy update or new permissions.

The service has fixed bounded GraphQL reads, an own-App viewer receipt, exact repository/PR/head preflight, one safety-only disable, and fresh readback. An ambiguous mutation gets one read, no mutation retry. Readback failure, head drift or still-armed state is unknown. The underlying client caps response bytes at 1 MiB and per-call timeout at30s; service rejects state reads exceeding35s. Each projection is max50 labels/events. Pagination is explicitly incomplete. Returned event records preserve actor/event identifiers but do not implement setter-epoch policy. hold_decision is always not_evaluated, never permission to arm. No comments or hold mutations. Text returned by reads remains untrusted.

The factory takes an already authenticated seat from broker, checks exact action/repo policy and permission ceiling, then requests only issues:read and pull_requests:read/write from existing installation minter. Returned client projection contains authenticated viewer login, not a raw credential. App acceptance/live permission remains unqualified. Actions GITHUB_TOKEN is not a substitute.

Important limitation: GitHub disablePullRequestAutoMerge has no atomic expectedHeadOid. Preflight/readback detect drift but cannot prevent a concurrent head change between query and mutation. Receipts expose atomic_head_guard=false. Source stage is not a strict atomic-head-fenced implementation. No enable implementation is present.

Before exposing this code: authenticated broker must be the sole constructor caller; integrate policy authorization once without double rate charging, durable outcome/idempotency/audit handling, per-PR locking if desired (does not solve upstream atomicity), contract tests through HTTP+Worker and scope reconciliation for new writes. A separate stage is needed to evaluate full hold epochs/checks/reviews/CODEOWNERS before enable. This candidate preserves that policy by not implementing enable at all. No private runtime API proxy or arbitrary query/path input exists.

Base: 7f7d08f2541e5b9a182371686f6151f569723847 (available local AFC source). Not based on current983 commit a25e9f43, which is not present in this local object store. Candidate is additive and can be rebased/cherry-picked by existing review transport owner after checking current heads, rather than modifying another owner's dirty branch.

Interactive policy is a separate commit/proposal for exact17 organization repos and existing seven writes, no mint/raw tokens/personal Fleet-OPS. Not installed or reviewed. Its presence does not qualify automation.
