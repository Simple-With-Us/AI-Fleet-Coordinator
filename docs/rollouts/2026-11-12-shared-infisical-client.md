# Shared Infisical Client Rollout

Refs AFC-785.  This foundation moves the existing scoped REST implementation into `scripts/fleet_infisical.py`; the legacy Python import aliases that same module.  Knob readers and writer verification import the shared library directly, and stable installer/container payloads include it.  No service or credential is activated by this source change.

The shared verifier now requires a future, timezone-aware token expiry in addition to the exact project, prod environment, folder set and permissions.  Missing or indefinite expiry fails before secret values are read, including names-only and writer verification.  Names-only listing requests hidden values and returns sorted names; it accepts only empty values or the exact documented provider mask with a literal boolean hidden flag.

Before updating an installed consumer, verify its own service-token metadata against that consumer's intended scope and finite expiry.  The new letter-reader binding has independently verified finite expiry.  Other bindings have not all been verified and are not claimed compliant.  A known indefinite grant blocks that consumer's rollout until a same-purpose scoped replacement is installed and its actual read succeeds.  Preserve the working installed release until replacement access is live and verified; do not broaden a grant or borrow another consumer's binding.

Deploy the shared module together with its consumer package using the existing installer or container payload.  Re-run the consumer's actual named read or names-only operation without printing values, then verify its normal function.  The live names-only provider call succeeded before the expiry tightening, and the same new binding's finite expiry was separately verified; the final installed function still needs its own acceptance check.  Broader Eavesdropper tunable registration and Muster Controls remain AFC-773 work and are not completed by this foundation.

If acceptance fails, restore the previous code payload using that consumer's existing deployment procedure while retaining its working authorized binding.  Do not revoke an old binding, move secrets or retire a working path until the replacement has passed acceptance.  The runtime rollout owner records deployment and rollback receipts separately from this source merge.

Local validation includes knob, legacy/shared-client, affected consumer, packaging/installer, seat-key and scoped-folder-copy tests; these groups overlap and are not a unique-test total.  The shared-client security tests are in the knob package's existing CI discovery path.  They prove missing, null, malformed, expired and timezone-free expiry stop names, reader and writer routes before values.  Full repository CI and final-head review remain merge gates.

## Run receipts (head f2c3b134d546, 2026-11-16)

Commands run from `scripts/` of a clean checkout of `f2c3b134d5468f037d80bb0c5816905931dd0250` unless noted.  Result format: tests run / outcome.

- `python3 -m unittest discover -s fleet_knobs/tests -t .` - 177 / OK.
- `python3 -m unittest discover -s fleet_secrets/tests -t .` - 126 / 125 pass, 1 pre-existing macOS-only failure: `DoctorTests.test_doctor_checks_the_second_project_with_its_scoped_token` expects exit 1 when the second project id is unresolvable, but `cli.doctor_report` intentionally relaxes `other_ok` off Linux ("the server needs the FleetLink tokens; a Mac reads its own").  Both the carve-out and the test date from c7493f42c (#703); the Linux CI `test` job is green on the previous head and was still running on this head when recorded.
- `python3 -m unittest fleet_facts.tests.test_install` - 12 / OK.
- `python3 -m unittest fleet_rag.tests.test_core_config` - 28 / 27 pass, 1 environment failure: the standalone installer subprocess pins `PATH=/usr/bin:/bin`, and this Mac's `/usr/bin/python3` (3.9) lacks `tomllib`.  Not a source regression; CI Linux python has it.
- `python3 -m unittest fleet_rag.tests.test_infisical_settings` - 56 / OK.
- `python3 -m unittest fleet_rag.tests.test_service` - 65 / OK.
- `python3 -m unittest fleet_lanes.tests.test_code_mirrors_install` - 6 / 6 environment errors: macOS tempdirs are symlinked (`/var` -> `/private/var`) and `install-code-mirrors-runtime.py:_real_parents` refuses symlinked install paths by design.  Linux CI green.
- `python3 -m unittest simple_tools.tests.test_cursor_cloud_letters_unit` - 11 / OK.
- `python3 -m unittest simple_tools.tests.test_subscribe_all` - 7 / OK.
- `python3 -m unittest whatsapp_bridge.tests.test_bridge` - 33 / OK.
- `python3 -m unittest test_alert_ingress_provision` - 9 / OK.
- `python3 -m unittest test_fleet_cli_release` - 89 / OK.
- `python3 -m unittest test_install_operator_sheets_drain` - 28 / 16 pass, 12 environment failures: the installer uses GNU `stat -c`, absent from macOS BSD stat; `stat -c` predates this PR (introduced in 5373ef52f, #1509; untouched by this branch).
- `python3 -m unittest test_wise_relay_exec` - 25 / OK.
- `python3 -m unittest hetzner.test_install_resource_control` - 18 / OK (1 skip).
- `python3 -m unittest discover -s tests -t tests` from `telemetry-relay/` - 37 / OK.
- `python3 simple-tools-mcp/test_install_seat_key.py` - 2 / OK.
- `python3 simple-tools-mcp/scripts/test_populate_scoped_folders.py` - 28 / OK.

CI on `f2c3b134d546`: `gitleaks` completed/success; the Linux `test` job was still running when these receipts were recorded (completed/success on the previous head `7cba99a69484`, which differs only by a main merge).

The earlier claim that a live names-only provider call succeeded, and that the letter-reader binding's finite expiry was verified, is a historical claim with no retained command or output.  It is not reproducible here without a service token and is not used as evidence in these receipts; each installed consumer's own acceptance check remains required as stated above.
