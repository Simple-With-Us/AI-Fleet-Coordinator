# Muster sidebar shell (AFC-597)

## Summary

The Muster window's eleven tabs became a sidebar.  At the window's usual width the tab bar folded most of its panels into an overflow menu, so a panel inside it was one the owner had to know to look for.  The sidebar names every panel at once, grouped by the job it belongs to, and a new Overview place carries the fleet verdict and the counts the last run gives.

## What changed

1. `FleetOverviewView` hosts a `NavigationSplitView` instead of a `TabView`.  The sidebar reads Overview, then Fleet (Compliance Grid, Apps & Work, Seats, Drift History), This Mac (Services, Wake & Listener, Controls, Seat Detail) and Server (Server Status, Hosts, Operations).  Every panel the tab bar held is still named once, and only the sidebar labels changed: no view, type or file was renamed, and `MiniMaxDetailView` itself is untouched behind its Seat Detail label.
2. The place the window was is saved under `musterSidebarSelection`.  A saved spelling this app no longer names reads as the overview, never as a guess.  The overview is the place a fresh window opens on.
3. `OverviewView` shows the verdict and six count cards: red cells, unknown, missing, drift, stale and the drift history's turns on record.  The counts come off the store's own cells, so the overview cannot disagree with the grid it points at, and every card opens the panel that holds its rows.  A run that could not be trusted leaves every engine count unknown, rendered as a dash, never as zero.
4. The engine footer and the fix activity now sit only under Overview and Compliance Grid, the two places that read the run's cells.  Under every other panel they read as belonging to it, including panels an engine failure says nothing about.
5. The toolbar Theme picker is gone.  Settings still holds it, and the stored choice still colours both windows.  One toolbar Refresh remains, and it still runs the engine.
6. Compliance Grid and Drift History carry the drift monitor's red count as a sidebar badge.  No count means the badge reads unknown rather than nothing.  No other place carries a badge, because no other panel has a count of its own to show.
7. A failed fleet-view refresh now clears the registry rows with the envelope it already cleared.  Read back without the run's answer those rows carried lane, pull request and claim counts of zero, which read as a fleet measured and found idle.  Only the unknown banner remains, which is what the view's own contract always promised.

## Deliberately left

- The toolbar Refresh still runs the engine from every place.  Panels with a source of their own keep their own refresh beside their rows, so a per-place refresh would have meant reaching into each panel's state for a button that already exists there.
- The Session Letters place waits on its data contract.  The VM section and the unified plan and Touch ID control are later slices, and no control path changed here.

## Files

- `muster/Muster/FleetOverviewView.swift`, `muster/Muster/Views/OverviewView.swift`, `muster/Muster/Model/MusterNavigation.swift`, `muster/Muster/FleetConfigCLI.swift`, `muster/Muster/FleetView.swift`, `muster/project.yml` and `muster/MusterTests/MusterNavigationTests.swift`.

## Verification

- `xcodebuild test -project Muster.xcodeproj -scheme Muster -destination 'platform=macOS' CODE_SIGNING_ALLOWED=NO` — 632 tests, 1 skipped, 0 failures.  The seven new tests pin the twelve places, the saved spelling and the counts' fail-closed nil.
- The Debug app launched and its window was captured without taking the foreground.  The sidebar shows all three sections at the default width, the window opens on Overview with the live verdict and counts, and the footer and fix activity appear only under Overview and Compliance Grid.
