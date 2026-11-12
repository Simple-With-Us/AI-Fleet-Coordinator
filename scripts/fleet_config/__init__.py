"""fleet_config:  the read and write engine under the Muster Fleet Admin Mac app.

    from fleet_config import contract, manifest, types

See ``docs/plans/2026-10-19-fleet-config-design.md`` for the pinned contract and the
module ownership map.  Stdlib only;  a stable copy run with ``python3 -I`` carries just
this package, matching ``fleet_knobs``.
"""

from . import contract, manifest, types
from .contract import SCHEMA_VERSION
from .manifest import ManifestError
from .types import CheckState, ConfigKind, ProofLevel

__all__ = [
    "SCHEMA_VERSION",
    "CheckState",
    "ConfigKind",
    "ManifestError",
    "ProofLevel",
    "contract",
    "manifest",
    "types",
]
