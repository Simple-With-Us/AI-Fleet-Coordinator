# AFC-842 collector mapping

Source-only change. No app deployment, collector installation or wake test is part of this patch.

## Source contracts

Read on November 18, 2026:

- https://raw.githubusercontent.com/Simple-With-Us/Socratic-Trade/main/app/api/proxy-use/route.ts
- https://raw.githubusercontent.com/Simple-With-Us/Socratic-Trade/main/src/lib/mango-probe.ts
- https://raw.githubusercontent.com/Simple-With-Us/Socratic-Trade/main/app/api/live/route.ts
- https://raw.githubusercontent.com/Simple-With-Us/Congress.Trade/main/app/src/shared/pipelineHealth.ts
- https://raw.githubusercontent.com/Simple-With-Us/Congress.Trade/main/app/src/delivery/rest.ts

ST's proxy-use GET is the supported public monitoring route. It returns the cached host-to-Mango probe, including reachable and checkedAt. Success cache is 60 seconds, failure cache 5 seconds. A request may record a metrics gauge and cause a cached TCP reachability probe. It does not change trading settings. The collector's existing 15-minute cadence is unchanged. The exact URL pin changes from process-only live to proxy-use, not to a wildcard. Credentials and redirects remain forbidden.

CT's public health response nests checks under pipeline.checks. Polling checks describe recent successful discovery for house, senate and executive. Ingestion backlog measures pending outbox age, not whether a polling lane is alive. Senate eFD is a distinct functional canary; its failure alone must not claim ingestion stopped.

## Mapping choice

For source-native CT checks, all three chamber polls must be explicitly ok to report ingestionAlive true. Any critical or stalled poll reports false; a missing, unknown, degraded or duplicate poll keeps it null. This is a conservative aggregate of polling liveness, not proof that every downstream stage completed. The upstream aggregate is preserved and cannot be hidden by a healthy display subset.

The displayed house and oge components follow their chamber polling checks. senate_efd follows its own canary. No backlog-to-liveness alias is used. Legacy payload support remains for existing callers, but malformed native checks cannot fall back to legacy green.

## Fixtures and limits

Fixtures are sanitized field projections from actual unauthenticated Mac-origin GET responses at 2026-11-18T17:23:22Z and 17:23:23Z. ST retains only ok and selected Mango probe fields; CT retains aggregate/time and check IDs/statuses. Hostnames inside response values, details, request data, build, secrets metadata and unrelated state are excluded.

At capture, ST's Mango probe was reachable and fresh. CT's aggregate was degraded with its Senate eFD canary degraded. These fixtures are observations at that time, not claims about current health or installed collector behavior. Tests also mutate copies to exercise missing, stalled, duplicate and malformed cases. A fresh installed collector-to-Muster receipt remains required after reviewed source adoption.
