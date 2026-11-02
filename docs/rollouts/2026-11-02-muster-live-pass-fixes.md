# Muster live-pass fixes (AFC-592)

## Summary

The live verification of the Muster overhaul (PR 1239, AFC-590) drove the merged app through Settings, Wake Settings, the Fleet filter, Controls, Recall Settings and a refresh cycle.  Three defects and one misleading error survived the fake-only tests, and this change fixes them.  Nothing here touches a secret value: names, exit codes and booleans only.

## Findings and fixes

1. `fleet-secrets names` failed fleet-wide (exit 5) whenever any value at the path was an unexpanded Infisical reference (observed: `BF_BUILDER_LINEAR_CLIENT_ID`).  `cmd_names` called `reject_unresolved` on every value although it prints only keys, against `core.fetch_all`'s own contract that callers reject the values they use.  Listing keys now skips the rejection.  `get` and `run` still fail closed on the keys they use, and `doctor` still reports the unresolved count.
2. Muster reported every Secret Names failure as "could not read / in time", including plain non-zero exits.  `CommandExitError` now carries the exit status, and `FleetSecretError` maps exit 3 to missing, any other exit to a new refused case, and only a no-answer failure to unavailable.  The same mapping now serves `get`, so an unresolved key no longer reports as absent.
3. The Settings window ignored the stored theme (Dark rendered only in the main window).  `MusterSettingsView` now applies `preferredColorScheme` from the same AppStorage value.
4. The Fleet filter's "App Kind" label wrapped letter by letter in a 16pt column beside the segmented picker.  The picker is now `labelsHidden`; the options name themselves and the title remains the accessibility label.

## Files

- `scripts/fleet_secrets/cli.py` and its tests.
- `muster/Muster/FleetServices/FleetSecrets.swift`, `muster/Muster/FleetServices/StartupPolicy.swift`, `muster/Muster/MusterApp.swift`, `muster/Muster/FleetView.swift`, and `muster/MusterTests/B3bPanelsTests.swift`.

## Verification

- New tests failed first: the Python names test saw exit 5 instead of 0, and the Swift tests did not compile against the missing refused case and status.
- `cd scripts && python3 -m unittest fleet_secrets.tests.test_fleet_secrets` — 103 tests, OK.
- Focused Muster tests (`SecretNamesTests`, `RecallSettingsTests`) pass; the full Muster suite and a live re-check (Secret Names listing, Settings theme, filter label) run after merge, with `bash scripts/install-fleet-secrets.sh` re-run so the Mac's installed copy matches.

## Follow-ups left open on purpose

- The `BF_BUILDER_LINEAR_CLIENT_ID` value in Infisical is still an unexpanded reference.  Correcting a secret value is the owner's call; names no longer depends on it, and `get` on that key still fails closed.
- Test Wake accept spends wake budget and can start a worker, and knob writes end at Touch ID.  Both remain owner-only; their dialogs, plans and cancel paths were verified live under AFC-590.

## Second live pass (same day)

Re-verifying the merged build live found the Secret Names panel still down, now on Muster's own parse: exactly one of the 679 root names, a legacy mixed-case key, failed the command-argument pattern the listing reused.  The listing parse now accepts real Infisical names of any case for display (`isListableName`) while still refusing value-shaped lines whole-answer, and the strict pattern still guards every name passed to a command.  Covered by `test_names_parse_accepts_mixed_case_names_but_still_refuses_value_shapes` (red first on the live name); full Muster suite 625 tests, 0 failures.
