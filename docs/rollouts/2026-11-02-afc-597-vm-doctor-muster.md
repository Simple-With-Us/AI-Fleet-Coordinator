# AFC-597 slice S5 rollout: VM status, doctor carry and the Muster Virtual Machines section

## What shipped

The doctor gained an optional VM pull.  `scripts/fleet-tools-doctor.py --vms` reads `~/.config/fleet-tools/vm-hosts.json`, pulls each named VM's `fleet-vm-status.py --json` answer over its read-only forced-command key, and writes it to `~/.local/state/fleet-tools/vms/<label>.json` (0600, atomically).  Muster gained a Virtual Machines destination in the sidebar's Server section, between Hosts and Operations.  It decodes every report in that folder and shows one card per VM: host label, seats, load against the cpu count, memory, swap, disks, harness processes, the newest harness session, and each checkout's branch, dirty counts and an UNPUSHED badge.  The panel runs nothing and controls nothing.

This lane was built by merging two unmerged parents first: `origin/muse-code/afc-597-muster-round2` (the sidebar shell, fast-forward) and then `origin/muse-code/afc-597-vm-status` (the collector, clean merge).  S5 is the two commits on top.

## Design calls

The pull is a `--vms` flag on the existing doctor, not part of `--remote` or the combined report.  The server route is one host with its own document that the combined report embeds.  The VMs are several hosts whose reports Muster reads as standalone files, so folding them into the combined report would have meant a second reader contract for no gain.  The flag also keeps every existing doctor invocation byte-identical: no flag means no config read, no ssh, no writes.  Adding `--vms` to the LaunchAgent's scheduled run is a one-word owner step, listed below, and deliberately not made here.

A bad answer never replaces a good report.  Oversize (over 512 KiB), non-JSON, wrong `tool` or wrong `schemaVersion` are all refused before any write, and the previous file stays.  Its age then carries the truth: past 45 minutes Muster reads the VM as unknown, never as zeros.  The label is validated before any ssh runs, because the label becomes a filename.

In Muster, a file that cannot be decoded is still shown as a card with its problem and an UNKNOWN badge, never skipped: a VM the owner configured must not silently vanish from the panel.  With no folder or no files, the empty state names the `vm-hosts.json` config the owner must create and points at `docs/protocols/fleet-vm-status.md`.

## Tests

Python: `scripts/test_fleet_tools_doctor_vms.py`, 11 tests, written first against a fake ssh runner.  They pin the missing-config no-op, the strict label and destination refusals (before any ssh), the oversize refusal, bad JSON and wrong tool or version keeping the old file, one down VM not blocking another, a timeout as a failed pull rather than a crash, and a good report landing 0600.  The existing doctor suite (119 tests) and the collector suite (22 tests) stay green.

Swift: `VmStatusReportTests`, 8 tests following the `ServerStatusReportTests` pattern.  They pin the full decode, omitted fields staying omitted rather than zero, the 45-minute staleness boundary (at the mark still current, one second past unknown), wrong tool and version refused, a future-dated report refused, a directory read that keeps a broken file as an unknown card in label order, and a missing directory reading as no VMs.  `MusterNavigationTests` moved its destination count from 12 to 13 for the new destination.  The full Muster suite runs 650 tests with 0 failures (642 at the post-merge baseline, plus the 8 new ones; 1 skipped, as before).

Visual: a Debug build was launched with one clearly fake fixture report (`fixture-vm`, invented seats, loads and checkouts) placed in `~/.local/state/fleet-tools/vms/`, the saved sidebar selection pointed at Virtual Machines, and the window captured.  The fixture showed the PASS badge, load and memory bars, harness processes and sessions, and the checkout row with its dirty counts and UNPUSHED badge.  The fixture file was deleted afterwards and the app was quit by PID.  Stale rendering was not captured visually: it is pinned by the boundary tests instead, and a second launch for an orange box was not worth another fixture.

## Owner steps to turn it on, per VM

1. Install the collector on the VM (docs/protocols/fleet-vm-status.md, "Install on one VM").
2. Add the forced-command `authorized_keys` line on the VM for a fresh pull key (same doc, "Pull transport").
3. Add an `~/.ssh/config` host stanza on the Mac pointing an alias at that key.
4. Add the VM to `~/.config/fleet-tools/vm-hosts.json` as `{"label", "destination", "user"?}` and `chmod 600` the file.
5. Run `fleet-tools-doctor.py --vms` once by hand and check the file lands.  Then add `--vms` to the doctor LaunchAgent's arguments so the pull rides the existing schedule.

Until step 4 exists, nothing anywhere changes: the flag reads a file that is not there and stops.

## Non-goals

No control verbs, no wakes, no Zulip posting, no new timer, and no LaunchAgent change.  Telemetry stays read-only end to end.
