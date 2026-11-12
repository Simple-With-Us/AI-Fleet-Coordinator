# Workflow dispatch intents

A successful legacy dispatch with the same caller, repo, workflow, ref and inputs is replayed from the existing outcome store for 24 hours. A moving branch does not create a new request. Historically that cached acceptance looked like a new dispatch. Do not clear outcomes or vary irrelevant arguments to force a run.

For an intentional new dispatch, supply `intent_id` (8-100 ASCII letters, numbers, dot, colon, underscore or hyphen, first character alphanumeric). The intent is a meaningful caller-owned operation identifier. Keep the same identifier and payload when retrying that one operation. A genuinely new approved operation gets a new identifier. Reuse with a different payload is refused; pending and unknown outcomes remain held. No new permission, token, policy, identity or dedupe reset is introduced.

Add `expected_head_sha` (40 lowercase hex) with the same `intent_id` when the dispatch must be pinned to a branch head before send.  `expected_head_sha` without `intent_id` is refused.  `intent_id` alone is valid and skips the pre-dispatch source read.

An `intent_id` call must name a canonical branch ref.  The local rule is a letter, number or underscore, then up to 199 of those characters plus dot, slash or hyphen, with no `..` and not a 40-hex commit SHA.  The relay checks that shape before it mints a token.  It does not read the branch, and it does not prove the name is a branch rather than a tag.  GitHub accepts a branch or tag ref, so an intent-only call can dispatch a tag whose name passes that shape.  The receipt omits `expected_head_sha`, `source_checked_before_send` and `source_fence_atomic`.

When `expected_head_sha` is present, the relay reads that branch head before the POST and refuses a mismatch, and the minted token includes `contents: read` for that one read.  GitHub's dispatch contract still accepts a branch or tag ref, not an atomic expected-SHA condition.  `source_fence_atomic:false` makes that limitation explicit: a branch can change between GET and POST.  Completion requires reading the actual run and checking its head SHA, not trusting acceptance or preflight.  A qualification workflow must keep its own no-deployment guards.

A new successful call returns `accepted:true`, `replayed:false`, `upstream_request_sent:true`, `execution_verified:false`. A successful replay returns the stored receipt with `replayed:true`, `upstream_request_sent:false`, `execution_verified:false`. Legacy cached responses get the same truthful replay markers. Acceptance never proves a run executed. If GitHub supplies a run ID and canonical URLs, only those validated receipt fields are returned. Older empty responses require run discovery. Arbitrary response content is excluded.

Current official contract: https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event . It documents branch/tag refs and a newer run-ID response. No assumed branch SHA dispatch or atomic source fence is claimed.

Runtime rollout must compose this delta against the held live baseline, preserving unrelated drift and the write-only credential custodian. Source merge does not activate the route. A real new requester-side qualification intent and actual run/head/result evidence are required afterward.
