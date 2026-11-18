# Direct Infisical Readers

Refs [AFC-785](https://linear.app/simple-with-us/issue/AFC-785).  Depends on [#2167](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/2167), including its names-only API and server image payload.

These consumers call the shared `fleet_infisical` library in process.  None invokes the fleet-secrets application to read credentials; hosted Notes bearer routing and seat credential precedence remain intact.

| Consumer | Project | Exact folder | Explicit service-token binding | Cache behavior |
| --- | --- | --- | --- | --- |
| Zulip named credentials | Fleet-OPS | `/zulip` | `INFISICAL_TOKEN` | Memory cache and outage backoff, partitioned by project, API origin, binding and folder |
| Zulip credential-name discovery | Fleet-OPS | `/zulip` | `INFISICAL_TOKEN` | Names-only metadata API; credential values are never requested for discovery |
| Operator Notes backend URL | AI Fleet Coordinator | `/` | `INFISICAL_AI_FLEET_COORDINATOR_TOKEN` | Existing fleet-knob resolution first, then the same named value through the direct reader |
| Operator Notes backend key | AI Fleet Coordinator | `/operator` | `INFISICAL_AI_FLEET_COORDINATOR_TOKEN` | Existing principal-specific in-memory cache and invalidation on authentication refusal |
| FleetLink seat token | AI Fleet Coordinator | `/fleetlink` | `INFISICAL_AI_FLEET_COORDINATOR_TOKEN` | Only definite answers are cached; project, API origin and binding partition the cache |

The existing project-id settings select the intended project.  The shared reader verifies the service token's own read-only, exact-folder, production grant and each returned row's provenance.  Ambient declarations cannot widen the requested folder, and a binding change cannot reuse another binding's cached credentials.  No CLI login, machine identity, token borrowing or credential-store scan is introduced.

Legacy Python helper names and the Notes runner argument remain compatibility seams.  Their implementations perform direct library calls, with the existing safe error classifications and bounded deadlines.  The token values remain in memory and never appear in subprocess arguments or diagnostics.

The server image and the test-owned minimal listener image must include `scripts/fleet_infisical.py`; the fleet-secrets package is unnecessary for these readers.  Existing hosted Notes requests continue to use the calling seat's fleet bearer and never fall back to a shared backend credential.

Validation covers 240 focused tests across credentials, cache partitioning, Notes clients and hosted routes, FleetLink, MCP identity, and the minimal listener image.  Names-only discovery is tested with value reads forbidden; token rotation, project isolation, exact-folder pinning, transient failure backoff and missing bindings are covered.

Deploy only after the foundation's provider contract and this PR's current-head CI and reviews are complete.  Verify the intended consumer with its own scoped service token and a read-only operation; retain a working path until the replacement succeeds.  Neither a local test nor a healthy listener alone proves the consumer's authentication or runtime acceptance.
