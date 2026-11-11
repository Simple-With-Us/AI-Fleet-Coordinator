# Merge queue GitHub commands

Three default-off commands read a PR queue entry or enqueue/dequeue its exact current
head.  Enqueue uses GitHub expectedHeadOid atomically and always jump:false.  Dequeue
has an expected-head pre-read only, not an atomic lock because GitHub exposes none.
The fixed PR node comes from this authorized repo/number, never a caller node/URL.
Own App, explicit contents+pull_requests write ceilings, repo/action allowlists and
UNKNOWN no-replay apply.  Queue writes retain strict idempotency, no reapply.

Enqueue may lead to a merge when GitHub gates pass.  Source capability grants no
execution authority, bypass, deployment permission or review waiver.  No live queue
entry was changed.  GitHub must support/configure merge queue for the target repo.
No priority jump, queue configuration or arbitrary GraphQL input is supported.

Schema checked against https://raw.githubusercontent.com/octokit/graphql-schema/master/schema.graphql
