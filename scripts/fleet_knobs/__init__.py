"""fleet_knobs:  Infisical knobs for the fleet's short-lived jobs, with a last-known-good copy.

    from fleet_knobs import knobs
    mode = knobs.get("FLEET_AUTOPUSH_MODE", "off", "enum")

See README.md in this directory and the "Fleet knobs" section of INFISICAL.md.  Stdlib only;
a stable copy run with ``python3 -I`` carries just this package.
"""
from . import knobs
from .knobs import Knob, KnobError, SecretUnavailable, get, refresh, report, resolve
from .registry import BY_NAME, REGISTRY, Spec

__all__ = ["knobs", "get", "resolve", "report", "refresh", "Knob", "KnobError",
           "SecretUnavailable", "REGISTRY", "BY_NAME", "Spec"]
