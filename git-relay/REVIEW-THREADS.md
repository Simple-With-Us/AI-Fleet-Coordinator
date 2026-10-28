# Review thread state GitHub commands

Default-off wf_review_thread_resolve/unresolve use own App pull_requests:write plus contents:write,
existing repo/action allowlists and ceilings.  The command refuses if either
permission is absent; no configured seat or App grant changes.  GitHub requirement
source: https://github.com/github/gh-aw/issues/35726 . A supplied node ID must occur in
that repository PR first 100 review threads before any fixed mutation is allowed.
Threads outside that bounded page refuse; this is not complete-history coverage.
No raw GraphQL, arbitrary node lookup or cross-repo identity inference.

Mutation receipts must identify the same thread and requested boolean state.
GraphQL partial/unknown mutations stay UNKNOWN, no replay.  Known successes may be
reapplied as desired state setters; pending/UNKNOWN holds never re-arm.  Capability
never authorizes hiding an unresolved finding or bypassing review gates.  No live
thread was resolved/unresolved to test this source.
