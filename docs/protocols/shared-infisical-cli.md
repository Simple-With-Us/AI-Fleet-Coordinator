# Shared Direct Infisical Command

`fleet_infisical_cli.py` is the one command implementation for callers that cannot import Python directly.  It imports the shared `fleet_infisical` reader; the legacy `fleet_secrets.cli` module is only an alias.  There is no new app, service, transport, installer or platform-specific reader.

Use an isolated interpreter and the absolute script path in the reviewed checkout or release, for example `python3 -I ~/.local/share/fleet-cli/current/scripts/fleet_infisical_cli.py names --path /folder`.  The entry inserts only its own real directory before importing the shared reader, so an unrelated current directory or PYTHONPATH cannot substitute a reader that receives the caller's bindings.  Shell, Swift and MJS callers use that same argv surface, with their own explicit scoped process bindings; this command never discovers another consumer's store.

`names` uses the metadata-only API and prints names only.  Bound `get` uses the shared named-read API, so asking for one key does not decrypt every value in the folder.  Existing output and exit contracts, write allowlists and stdin value handling remain compatible.  Consumer reads require their own scoped service-token binding in both interactive and noninteractive processes; a TTY never authorizes an administrative CLI-session fallback.

The unqualified legacy `doctor` contract still probes Fleet-OPS `/` plus AI Fleet Coordinator `/fleetlink`.  An explicit `doctor --project fleet-ops --path /` selects only that project/folder and never probes the other project; an explicit path alone selects the default Fleet-OPS project.  Both forms intentionally read values to count unresolved references and return only masked status/counts, so neither is a metadata-only listing.  Selection uses the same caller's own scoped read binding; it adds no grant or credential fallback.

The shared command is added to new release payload requirements and the doctor inventory; retained older releases remain valid rollback targets.  The existing compatibility installer also copies it beside the shared library, so the legacy module alias never imports a missing file after a fresh install.

This source-only change contains no Muster Swift or native-test files.  Muster's own runtime binding and its Sparkle-triggered activation remain in a separate held source branch.  Merge this interface only after its shared-reader and package dependencies, CI and final review are verified; source tests do not establish any consumer deployment or grant.
