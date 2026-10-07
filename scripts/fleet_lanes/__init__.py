"""fleet_lanes — one methodical folder structure for every fleet checkout on the Mac.

Modules:
  layout   registry loading, seat aliases, sanctioned and forbidden roots, location
           classification, and lane and branch naming rules (pure; no subprocess, no network)

Layout choice is switched by the FLEET_LAYOUT environment variable: `nested` (default,
~/apps/lanes/<prefix>/<seat>-<slug>) or `flat` (~/apps/<prefix>-<seat>-<slug>).

Tests (standard library only):

    cd scripts && python3 -m unittest fleet_lanes.tests.test_layout -v
"""

__version__ = "0.1.0"
