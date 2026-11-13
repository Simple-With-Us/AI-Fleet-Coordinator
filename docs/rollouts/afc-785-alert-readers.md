# Direct Alert Credential Readers

Refs [AFC-785](https://linear.app/simple-with-us/issue/AFC-785).  Requires the shared `fleet_infisical.py` module from [#2167](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/2167).

The listener heartbeat reads only `SENTRY_FLEET_DSN` in Fleet-OPS, `prod`, exact folder `/simple-tools`, using its own explicit `INFISICAL_TOKEN` binding.  Existing environment precedence, optional-off behavior, in-memory DSN caching and retry backoff remain intact.

The shared wake alerter reads only its configured routine-key name through the same direct library on every poll.  `OPERATOR_SECRET_PROJECT` and `OPERATOR_SECRET_PATH` preserve their existing defaults, `fleet-ops` and `/`; selecting AI Fleet Coordinator requires that project's own binding, never the Fleet-OPS token.  The consumer's metadata must prove the exact folder, production environment and read-only grant, and the returned row must prove its provenance.

Neither reader starts the fleet-secrets application, logs a token or reads a credential store.  The alerter loads the canonical module from the managed scripts tree under isolated Python and strips Infisical authentication bindings from hook-child environments.  Banner and hook execution remain the existing last-mile behavior.

The new library must ship in the managed runtime before activation, and each consumer must already have its own scoped service-token binding.  A source template is not evidence of a live Mac configuration; this change does not install or launch a Mac process.

Focused tests cover the heartbeat and wake alerter, exact-folder pinning, project-specific bindings, missing-token and timeout behavior, key rotation on every poll, child-environment scrubbing and isolated script imports.  Both the alerter and the canonical library retain Python 3.9 syntax compatibility; a newer-host syntax check alone does not prove execution on the Mac's interpreter.
