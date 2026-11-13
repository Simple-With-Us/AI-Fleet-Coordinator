# AFC-394 Fleet View

Tue, Oct 20, 2026.  Seat MC, lane `muse-code-afc-394-fleet-view` at `~/apps/lanes/AI-Fleet-Coordinator/muse-code-afc-394-fleet-view`, PR #773.

## Summary

Muster gains its first live data.  A new stdlib-only collector, `scripts/fleet_view/`, answers lanes, open PRs and Linear claims in one pinned JSON envelope, and a new Fleet tab in the Muster app renders it per seat.

## Why

The Muster scaffold (PR #719) rendered only a placeholder.  Phase 2 merged the `fleet-config` engine (PR #724), and AFC-389 (MM) and AFC-391 (CLAUDE) own the config grid and the apply flow.  AFC-394 is the read-only fleet inventory beside them:  which lanes exist, which PRs are open and what blocks them, and who holds which Linear claim.

## Files

- `scripts/fleet_view/types.py`, `collectors.py`, `contract.py`, `__main__.py`:  the collectors, the request budget (60 GitHub, 30 Linear per cycle, counted in the envelope), the blocker mapping and the fail-closed envelope at `schemaVersion` 1.
- `scripts/fleet_view/tests/`:  14 tests with fake runners, including the budget test, the one-bad-repo case and the isolated `python3 -I` packaging test.
- `muster/Muster/FleetView.swift`, `FleetViewModels.swift`, `FleetViewService.swift`:  the Fleet tab, its envelope mirror and the subprocess service.  A failed refresh clears to UNKNOWN and never shows the last green.
- `muster/Muster/MusterApp.swift`:  a tab beside the existing overview.  This is the only shared file this lane touched.
- `.github/workflows/ci.yml`:  the fleet_view tests run beside the fleet_config tests.
- `muster/README.md`:  the scope line names the Fleet View.

## Verification actually run

- `cd scripts && python3 -m unittest discover -s fleet_view/tests -t .` — 14 tests, OK.
- `python3 -m unittest discover -s muster/script -t muster/script` — 16 tests, OK.
- `xcodegen generate --spec muster/project.yml` and `xcodebuild -project muster/Muster.xcodeproj -scheme Muster -configuration Debug build CODE_SIGNING_ALLOWED=NO` — BUILD SUCCEEDED.
- Live `python3 -m fleet_view status` on this Mac:  444 lanes OK, 64 open PRs observed, 148 claims OK, 17 of 60 GitHub requests and 1 of 30 Linear requests spent.  The PR section reports UNKNOWN because `gh` cannot resolve `Simple-With-Us/fleetlink-legacy`.  Spot checks against `gh pr list` and `simple-board list --json` matched.

## Follow-ups left open on AFC-394

- Screenshot acceptance of the Fleet tab against live data (JET review asked for in the AFC-354 topic).
- Read the claim expiry ceiling from the fleet_knobs registry instead of `CLAIM_EXPIRY_HOURS` with a 72-hour default.
- The `fleetlink-legacy` registry row names a GitHub repo that does not resolve.  It needs its own registry fix, and this lane did not edit `fleet-apps.json`.
