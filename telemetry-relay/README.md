# fleet-telemetry-relay

One OTLP relay between the coding agents and the two sinks (audit sections B.4
and C.1):  traces + logs go to Sentry project `fleet-infra`, metrics + turn
records go to Usage-Monitor.  Every record is tagged `signal=agent`, prompts and
responses pass through with secret patterns scrubbed, and `hook_execution_*`
noise is dropped.

- `otelcol.yaml` — the collector config (receivers, transform, redaction,
  connectors, exporters, pipelines).  Start here.
- `Dockerfile` — pinned contrib image + config, no secrets baked in.
- `COOLIFY.md` — deploy doc: the env/secret names Jay must set and where.
- `tests/` — transform contract tests with recorded sample payloads, the
  secretscan redaction-parity test, and the `configure-agents.sh` harness test.
- `tests/fixtures/` — representative OTLP log payloads for Claude Code, Codex,
  Grok CLI and the hook shim (benign values only).

Producer configs are minted by `scripts/telemetry/configure-agents.sh`, which is
shipped only:  it pulls credentials from Infisical by name, prints no secrets,
and never writes to `$HOME`.  The existing `agent-sessions` producers are left
untouched; cutover is a later config change.
