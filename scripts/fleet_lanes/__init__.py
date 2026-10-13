"""fleet_lanes — one methodical folder structure for every fleet checkout on the Mac.

Modules:
  layout   registry loading, seat aliases, sanctioned and forbidden roots, location
           classification, and lane and branch naming rules (pure; no subprocess, no network)

Layout v2 (owner 2026-10-09): lanes live at ~/apps/lanes/<Repo>/<seat>-<slug>, where <Repo> is the
repo's folder name under ~/Code.  The FLEET_LAYOUT environment variable can still say `flat`
(~/apps/<prefix>-<seat>-<slug>), a legacy opt-out that is no longer documented as a way to make lanes.

Tests (standard library only):

    cd scripts && python3 -m unittest fleet_lanes.tests.test_layout -v
"""

__version__ = "0.1.0"
