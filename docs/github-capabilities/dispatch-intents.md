# Workflow dispatch intents

A successful legacy dispatch with the same caller, repo, workflow, ref and inputs is replayed from the existing outcome store for 24 hours. A moving branch does not create a new request. Historically that cached acceptance looked like a new dispatch. Do not clear outcomes or vary irrelevant arguments to force a run.

For an intentional new dispatch, supply both `intent_id` (8-100 ASCII letters, numbers, dot, colon, underscore or hyphen, first character alphanumeric) and `expected_head_sha` (40 lowercase hex). The intent is a meaningful caller-owned operation identifier. Keep the same identifier and payload when retrying that one operation. A genuinely new approved operation gets a new identifier. Reuse with a different payload is refused; pending and unknown outcomes remain held. No new permission, token, policy, identity or dedupe reset is introduced.

Intent calls use a branch name. The relay reads the branch head before the POST and refuses a mismatch. GitHub's dispatch contract accepts a branch/tag ref, not an atomic expected-SHA condition. `source_fence_atomic:false` makes that limitation explicit: a branch can change between GET and POST. Completion requires reading the actual run and checking its head SHA, not trusting acceptance or preflight. A qualification workflow must keep its own no-deployment guards.

A new successful call returns `accepted:true`, `replayed:false`, `upstream_request_sent:true`, `execution_verified:false`. A successful replay returns the stored receipt with `replayed:true`, `upstream_request_sent:false`, `execution_verified:false`. Legacy cached responses get the same truthful replay markers. Acceptance never proves a run executed. If GitHub supplies a run ID and canonical URLs, only those validated receipt fields are returned. Older empty responses require run discovery. Arbitrary response content is excluded.

Current official contract: https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event . It documents branch/tag refs and a newer run-ID response. No assumed branch SHA dispatch or atomic source fence is claimed.

Runtime rollout must compose this delta against the held live baseline, preserving unrelated drift and the write-only credential custodian. Source merge does not activate the route. A real new requester-side qualification intent and actual run/head/result evidence are required afterward.
