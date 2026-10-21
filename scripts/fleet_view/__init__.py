"""fleet_view:  the read-only lanes, PRs and claims collector under Muster.

AFC-394.  The Muster Fleet View shows, per seat, the lanes on this Mac, the
open pull requests across the fleet repos, and the In Progress Linear claims.
This package is the engine under that view, in the style of ``fleet_config``
and ``fleet_knobs``:  stdlib only, Python 3.9 safe, one pinned JSON envelope.

The collector is read only.  It never writes a lane, a PR or a Linear issue,
and it never carries a secret value:  it records names, branches, states and
timestamps only.  A collector that fails, times out or returns unparseable
output yields an ``UNKNOWN`` section, never an empty healthy one.
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION"]

# The pinned envelope version, mirroring fleet_config.contract.  A breaking
# envelope change raises this deliberately.
SCHEMA_VERSION = 1
