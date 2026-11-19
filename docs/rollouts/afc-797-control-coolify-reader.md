# Control Coolify Direct Reader

Refs AFC-797, child of AFC-785.  This is a source change; it does not install a payload, qualify a grant, contact a live provider or perform a control action.

## Change

The control helper reads only `COOLIFY_API_TOKEN` from Fleet-OPS prod `/coolify` through the canonical bounded named reader.  It no longer discovers or executes a separately installed fleet-secrets application.

The helper retains its existing `FLEET_OPS_CONTROL_INFISICAL_TOKEN` process binding and protected `~/.config/fleet-tools/ops-control.env` parser.  A valid file still wins over the process binding; malformed, unowned, shared or symlinked files refuse.  An absent own binding never selects another consumer's token, a writer, machine identity, CLI login or retired flat file.

The shared source is the fixed adjacent `fleet_infisical.py`, read through the helper's existing descriptor, no-follow, ownership, mode and size checks.  The private module handle carries the actual adjacent path for the canonical isolated worker and ignores public module-cache and executable-path substitutes.  No second client or persistent process is added.

The operation is bounded to 30 seconds and an 8 KiB result, requests one exact name, and requires strict record provenance and a read-only finite-expiry grant for the exact project, prod environment and folder.  Explicit non-prod configuration refuses.  The existing Fleet-OPS project coordinate is resolved by the canonical helper; no project ID is embedded or inferred from another grant.

Plans still read no authentication and contact no provider.  Read failures have fixed safe diagnostics and remain known credential failures before the Coolify HTTP request.  The existing request-sent uncertainty handling, approval/nonce discipline, action lock, audit and GUI wire meanings are unchanged.  A successful redeploy still means `submitted`, not verified healthy.

## Payload And Verification

The existing resource-control installer already ships the exact canonical reader beside the control helper.  Its finite inventory is unchanged.  The reader requires a protected payload; a group-writable source checkout is not a qualified runtime install.

Tests cross the actual disposable worker and loopback provider metadata/named-record verifier.  They cover exact successful reads, own-binding precedence, rejected ambient credentials, custody failures, wrong scope/project/environment/access, missing or invalid expiry, strict row failures, absence, whitespace, result size and the distinction between a credential-read timeout and an actually sent Coolify request.

The copied payload is exercised with Python isolation, an empty executable search path, an untrusted working directory and a preloaded foreign public module.  A two-file read-only copy succeeds without the compatibility package or writer ledger.  Missing/broken reader payloads fail closed before provider or Coolify access; the test checks no writer state is created.  All provider and credential data in these tests are synthetic.

Commands:

```text
cd scripts && python3 -m unittest test_fleet_ops_control
cd scripts/hetzner && python3 -m unittest test_install_resource_control
cd scripts && python3 -m unittest fleet_secrets.tests.test_bounded_reads
```

These passed 315 control tests, 21 installer tests and 20 shared bounded-reader tests.  The control suite includes the real scoped-reader credential cases; these counts describe the named suites, not fleet-wide runtime acceptance.

## Gates

- Merge the canonical strict/bounded reader dependency and complete source CI/review before this unit lands.
- Verify this control consumer's actual Fleet-OPS prod `/coolify` read-only finite-expiry grant, registry metadata and existing protected binding before activation.  The source tests do not prove these facts.
- Separately approve and verify the managed copied payload and its normal authorized control/read-only diagnostic behavior.  Preserve the working installed route until acceptance is established; no install, service restart or live `--check-token` is performed by this source unit.
- When root composes this change into the settings backend, regenerate and verify the actual helper wire fixture because `fleet-ops-control.py` is one of its producer files.  Do not manually rewrite another lane's fixture metadata.
- Parent AFC-785 remains active; global application retirement, the separately observed retired RAG fallback and draft native CODEX delivery are not completed here.
