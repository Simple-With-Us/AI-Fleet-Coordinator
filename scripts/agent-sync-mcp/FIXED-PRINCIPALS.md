# Credential-assigned acting seats

A verified MCP credential or OAuth grant assigns one acting seat. Its `identity`
argument asserts that seat; it does not select another seat. Legacy CURSOR and
GROK-WEB OAuth grants no longer route to each other or to Grok Bot personas.
Each persona needs its own assigned credential. Every hosted tool family passes
through the server's fixed allow-set check before backend dispatch. Existing
admin provisioning endpoints are separate and unchanged by this repair.

This source repair is not a deployed enforcement receipt. Host rollout and
negative cross-seat checks remain required. It does not mint, copy or rotate
credentials, revoke existing OAuth access, change Relay policy, or touch UNKNOWN
outcomes. Existing own-seat reads and writes remain available within their
existing scopes.

Local stdio is a separate boundary: unpinned sessions can discover all identities
available through the host automation reader. A client flag or launcher default
cannot enforce fixed-principal security while that caller can read all seats'
credentials or change its root-owned startup. Migrate agent-facing connections
to their assigned hosted bearer or a host-enforced credential-scoped service
before claiming fixed-seat enforcement for local clients. Keep broad automation
credentials out of agent-controlled contexts; coordinate the migration with the
host owner without abruptly removing working access.
