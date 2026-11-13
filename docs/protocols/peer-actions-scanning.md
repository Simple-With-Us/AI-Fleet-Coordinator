# Peer-Action Text Scanning

This source adds two defenses to the shared tools.  It does not change the registry destination, credential source, permissions or deployment settings.

The existing `simple_tools/secretscan.py` module is already part of the fleet CLI release.  It now has a neutral stdin command, usable from any repository without importing the AFC package.  Invoke the absolute path in the reviewed release with an isolated interpreter, for example `python3 -I ~/.local/share/fleet-cli/current/scripts/simple_tools/secretscan.py --field request`.  Supply text through stdin from the caller's memory or incoming stream, never argv or a temporary preflight file.

The command returns JSON containing only `ok`, an allow-listed field identifier, a safe code and the scanner's match kind.  Exit status is `0` for safe text, `1` for a credential-shaped input, and `2` for invalid or oversized input.  Inputs are bounded to 256 KiB.  Invalid argument and field-name paths do not echo supplied values.  Use `text` for a field outside the supported names.  The scanner recognizes the existing shared patterns; no new detection rules or exemptions are introduced.

Python and hosted registry builders also run their existing shared scanners on every text field before creating a row.  This includes `--dry-run`: a flagged row is neither printed nor persisted.  A refusal names the field, never the suspected value or prefix.  CLI, stdio and hosted consumers use the same builders, so a caller cannot skip the guard by omitting a separate preflight.

Source tests prove field-level refusal, no append on a CLI dry-run refusal, safe error output, the standalone isolated command, and Python/hosted scanner parity.  They do not prove deployed behavior.  Verify the released Python module and hosted Worker build separately after merge; until both routes are confirmed, the documentation in PR #1813 remains conservative about deployed registry scans.
