"""cordis_patch: marked blocks inside Clutch's home-level cordis patch (`~/.clutch/dsh/cordis.patch.yml`).

The patch is a top-level YAML array of loader patch entries.  Two fleet installers add entries to it, each as its own
marked block (`mcp-agent-sync` from install_mcp, `hooks-fleet-guards` from install_tools), and neither touches
anything outside its block.

One fact shapes everything here, measured with the pinned engine on 2026-10-10:  an EMPTY patch file, or one that
holds only comments, makes `dsh --profile <name> --dump-config` exit 1, so the next start of `clutch-web` would fail.
`[]` is accepted.  So a patch the fleet creates always carries a block, and when the last block goes and nothing but
comments is left, the file is deleted (the backup keeps its text) instead of being left to break the engine.

Pure text in, text out.  Python 3.9 safe.  Tests: fleet_lanes/tests/test_install_mcp.py (CordisPatchTests).
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from . import marked_block as MB

HEADER = (
    "# Home-level cordis patch for the Clutch engine, applied to every profile after the profile's own patch.\n"
    "# A top-level YAML array of patch entries.  The blocks below are managed by the fleet installers\n"
    "# (python3 -m fleet_lanes.install_mcp, python3 -m fleet_lanes.install_tools); edit outside them.\n"
)


def has_entries(text: Optional[str]) -> bool:
    """True when the patch holds at least one array entry (a column-0 line that starts with `- `)."""
    for line in (text or "").split("\n"):
        line = line.rstrip("\r")
        if line.startswith("- ") or line == "-":
            return True
    return False


def is_list_patch(text: Optional[str]) -> bool:
    """A cordis patch is a top-level YAML array: every column-0 line is a comment, blank, or starts with `- `."""
    for line in (text or "").split("\n"):
        line = line.rstrip("\r")
        if not line or line[0] in (" ", "\t", "#"):
            continue
        if not (line.startswith("- ") or line == "-"):
            return False
    return True


def upsert(text: Optional[str], name: str, tool: str, body: Sequence[str]) -> Tuple[str, str]:
    """(new text, action): create, append, replace or none.  A new file starts with HEADER."""
    new, action = MB.upsert_block(text, name, tool, body)
    if action in ("create", "append") and (text is None or not text.strip()):
        new = HEADER + new
    return new, action


def remove(text: Optional[str], name: str) -> Tuple[Optional[str], str]:
    """(new text, action): remove or none.  A new text of None means "delete the file": nothing but comments is left,
    and a comment-only patch crashes the engine."""
    if text is None:
        return None, "none"
    new, action = MB.remove_block(text, name)
    if action == "none":
        return text, "none"
    if not has_entries(new):
        return None, "remove"
    return new, "remove"
