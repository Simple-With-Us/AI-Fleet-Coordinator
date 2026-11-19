# AFC-799 Alert Provisioner Reader

- Canonical reader source must be on main and the final provisioner unit must pass CI and review before installation.
- Verify the existing own AFC and Fleet-OPS read bindings, registry metadata, exact project/prod/folder read-only grants and finite expiry before use.
- Qualify the copied/installed manual provisioner using its authorized own bindings and preserve the existing apply/write gates; no provisioning, deployment or activation occurs in this source task.

Refs [AFC-799](https://linear.app/simple-with-us/issue/AFC-799), under [AFC-785](https://linear.app/simple-with-us/issue/AFC-785).

## Source Change

The manual `scripts/alert-ingress/provision-secrets.py` entrypoint no longer discovers or invokes a `fleet-secrets` executable, including its conditional external-app fallback.  It uses the adjacent canonical `scripts/fleet_infisical.py` library and its disposable bounded named reader directly.  The finite entrypoint payload contains those two public code files; read operations need no CLI package, installed app or writer ledger.  Missing, broken or misrouted canonical payload fails with a fixed diagnostic and exit 127 before a credential is passed to a reader or a provider operation begins.

The own read bindings are unchanged: `ALERT_INGRESS_AFC_READ_TOKEN` selects the existing pinned AFC project at prod `/zulip`, and `ALERT_INGRESS_FLEETOPS_READ_TOKEN` selects the existing Fleet-OPS project resolver at prod `/`.  No other project/path pair is accepted by this provisioner.  No generic, other-consumer or writer token is substituted.  Only provider/environment selectors and the exact read path enter the shared reader, together with the explicit own token and requested names.  The existing 90-second read bound is retained, with strict named-record validation and a finite 1 MiB output cap.

Existing synced process values keep precedence.  A missing optional name is omitted from the returned map; a missing required name remains a provisioning failure.  The CLI's stderr absence parsing, retry/fanout and single-versus-multiple stdout decoding are removed.  Existing compatibility function names remain.

Dry run, apply authorization, separate writer verification, optional PagerDuty and vendor-issued Sentry handling, generated-value reuse and Infisical/Worker unknown-write outcomes retain their existing behavior.  No actual provider, Worker, selftest, store or credential was used in this source task.  Remaining app consumers and installed acceptance are separate work; this unit does not establish global retirement completion.

## Verification Boundary

From `scripts`, `python3 -B -m unittest test_alert_ingress_provision -v` passes 14 tests.  These use real canonical workers against loopback-only synthetic metadata and named-record responses, plus the existing writer-verifier unit cases with mutating calls mocked.  An isolated copied payload proves a named read without an app or CLI package, no invocation of a temporary external-app sentinel, and fixed failure when the canonical library is missing or broken.  Empty-name reads perform no provider call.  Compilation and diff checks pass; the six existing control/write function ASTs are unchanged.  Source hashes and the unchanged-scanner comparison are recorded in the frozen handoff.  These checks do not qualify a live grant or installed consumer.
