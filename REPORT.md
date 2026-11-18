# AFC-398 — ST/CT Operational Health for Muster: Adapter Report

Owner slice: Muse-Assist (MA), under Jay's Operator completion push ([AFC-694](https://linear.app/simple-with-us/issue/AFC-694)).  Contract: Jet's AFC-398 sign-off of 2026-10-23 (Muster lead, P-AFC-7).  Branch: `Muse-Assist/afc398-st-ct-health-adapter`.  Status: built and fixture-tested;  not pushed, not deployed, not installed on the Mac.

## What was built

The reserved `api` collector, end to end, in the same shape as the existing Muster collectors (`fleet-mac-services`, `fleet-server-status`, `fleet-worker-health`):  a read-only Python collector polls the two apps' own health routes, maps the answers under the pinned contract, and writes one state file;  Muster reads the file and only displays it.  Polling is the adapter input;  the store file is the deliverable, matching AFC-398's target shape (collectors push to one state store, Muster reads the store).

Read-only throughout:  GET requests only, no credential held, no change to any Socratic Trade or Congress.Trade endpoint, no restarts, requeues or autofix, and no letter logic touched — the Operator decides letters.

## Files changed

| File | What |
| --- | --- |
| `scripts/fleet-api-health.py` | New.  The `api` collector:  bounded fetch, ST/CT mapping contract, atomic 0600 store writer, `store_reading` reference reader. |
| `scripts/test_fleet_api_health.py` | New.  15 fixture tests (fake fetcher, fixed clock, no network). |
| `scripts/launchd/com.jay.fleet-api-health.plist` | New.  LaunchAgent template, every 900 s, modeled on `com.jay.fleet-worker-health.plist`.  Not installed. |
| `muster/Muster/Model/ApiHealthReport.swift` | New.  Muster's display-only reader for the store file, mirroring `WorkerHealthReport.swift`. |
| `muster/MusterTests/ApiHealthReportTests.swift` | New.  8 reader tests (current, timeout, version, tool, malformed, missing, stale, path). |
| `muster/MusterTests/Fixtures/api_health_current.json` | New.  ST ok + CT warn (senate_efd down, ingestion alive), generated in the collector's exact output shape. |
| `muster/MusterTests/Fixtures/api_health_unreachable.json` | New.  Both apps timed out:  unknown rows with the fixed `timeout` code. |
| `muster/project.yml` | Registers `ApiHealthReport.swift` in the MusterTests source list (the app target picks it up from the `Muster/` tree). |
| `REPORT.md` | This file. |

## The mapping contract (pinned)

Store document:  `schemaVersion: 1`, `tool: "fleet-api-health"`, `collector: "api"`, `generatedAt`, `worst`, and one row per app with `state` / `reason` / `provenance {source, fetchedAt, httpStatus}`.  State vocabulary:  `ok`, `warn`, `fail`, `unknown` — `unknown` always means "not observed", never "observed healthy".

- ST `/api/live` is liveness only.  `ok` requires HTTP 200 AND `mango.reachable == true` AND a proved-fresh observation (age ≤ 120 s, from `mango.observedAt` / `observedAt` / `lastObservationAt` / an explicit age field).  HTTP 200 or the body's `ok` alone is never green.  Mango explicitly unreachable is `fail`;  a stale, missing or future observation is `unknown`.
- CT `/api/health` keeps its upstream aggregate verbatim (`upstreamStatus`) and its components (`house`, `senate_efd`, `oge`, `ingestion`).  Ingestion observed failed is `fail`.  Any other component failed while ingestion is alive is `warn` — a direct `senate_efd` failure does not mean ingestion stopped.  Any expected component unreported is `unknown` for that component and the app cannot read `ok` — a missing optional check is never zero and never green.
- AFC-441 discipline:  only the two pinned URLs may be fetched (scheme, host and path checked before any request exists;  redirects never followed), 10 s default timeout with a 30 s ceiling, 256 KB response cap, provenance and `generatedAt` on every row, and fail-closed — timeout, HTTP error or unparseable output yields an `unknown` row with a fixed error code (`timeout`, `unreachable`, `unparseable`, `http_<n>`, `refused_url`), the file is still written, and the run exits 1.  No upstream free text is copied into the store.

## How Muster reads the store file

`scripts/fleet-api-health.py` writes `~/.local/state/fleet-tools/api-health.json` atomically at mode 0600, the same directory and discipline as `server-status.json`, `worker-health.json` and `mac-services.json`.  `ApiHealthReportReader` in `ApiHealthReport.swift` reads that path and nothing else:  missing, malformed, wrong `schemaVersion`, wrong `tool`, future-dated or older than 45 minutes (three 15-minute collector runs) all read as `unknown` with no app rows shown;  inside the window, `ok` → PASS, `warn` → DRIFT, `fail` → MISSING, `unknown` → UNPROVEN in the grid vocabulary.  The Python `store_reading` function is the same judgment, kept as the contract's reference reader and covered by tests.

## Test results

Command:  `cd scripts && python3 -m unittest test_fleet_api_health -v`

```
Ran 15 tests in 0.004s

OK
```

Coverage, mapped to the required fixtures:  ST green;  ST mango unreachable (`fail`);  ST stale observation (`unknown`);  ST HTTP 200 + body `ok` alone never green;  ST missing observation;  CT healthy;  CT senate_efd down but ingestion alive (`warn`, `ingestionAlive: true`);  CT ingestion failure (`fail`);  CT missing optional check → `unknown`;  timeout fail-closed with the store file still written;  unparseable and HTTP-error fail-closed;  one app failing does not hide the other;  URL allowlist refusal before any request;  store shape, 0600 permissions and the reference reader's stale / wrong-version / wrong-tool rejection.

Neighboring suites in this lane:  `python3 -m unittest discover -s fleet_view/tests -t .` — 17 tests, OK.  `test_fleet_cli_runtime` shows one failure (`test_notes_reader_import_is_in_installed_runtime`);  the identical failure reproduces on the pristine source checkout, so it is pre-existing and unrelated to this slice.

Live smoke from this VM (not the Mac):  `python3 scripts/fleet-api-health.py --dry-run` reached both pinned URLs and both answered HTTP 403 from the VM's datacenter egress (the known Cloudflare challenge for these properties;  Congress.Trade's bare-curl 403 is long documented).  The collector failed closed exactly as designed — both rows `unknown` with `error: "http_403"`, exit 1, nothing written in dry-run.  The upstream payload shapes therefore still need one confirmation run from the Mac, where the collector is meant to run;  the mapping accepts the documented field spellings and treats anything unrecognized as `unknown`, never green, so a shape surprise degrades safely instead of misreporting health.

Swift tests were written but not run:  this lane was built on the Linux VM, which has no Xcode.  They run with the rest of MusterTests on the Mac (`cd muster && xcodegen generate --spec project.yml && xcodebuild -project Muster.xcodeproj -scheme Muster -destination 'platform=macOS' test`).

## What remains to wire it live (main-agent / Mac-side work — not attempted here)

1. Push and land this branch through the Fleet Tools relay route (MA's step;  this lane is committed locally only).
2. On the Mac:  install the LaunchAgent (`cp scripts/launchd/com.jay.fleet-api-health.plist ~/Library/LaunchAgents/` and `launchctl bootstrap`), confirm the managed runtime carries `scripts/fleet-api-health.py` to the path the plist names, and register the helper in `docs/MAC-LOCAL-PROCESSES.md` per repo convention.
3. Run the collector once on the Mac and confirm the live ST `/api/live` and CT `/api/health` payload shapes against the mapping's accepted spellings;  adjust the fixture spellings if the live bodies differ (the fail-closed design makes this a display gap, not a false green).
4. Run the Muster test suite on the Mac, then add the panel that renders `ApiHealthReading` (the reader and its tests are in this slice;  no view was added, and no existing panel was rewired).
5. Longer term, per AFC-398's target shape:  replace or supplement polling with ST/CT collectors pushing to the authenticated endpoint or store.  The store contract in this slice is the seam for that — the writer changes, the file and Muster's reader do not.
