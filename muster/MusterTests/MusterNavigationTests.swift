import XCTest

/// The sidebar's places, the saved place and the overview's counts.
final class MusterNavigationTests: XCTestCase {
    // MARK: Destinations

    /// Every place the tab bar held is still named once.
    ///
    /// Eleven panels sat behind the tab bar;  with the overview the sidebar names
    /// twelve places, and a place named twice or not at all is a panel lost or
    /// doubled in the move.
    func test_sidebar_names_every_place_once() {
        let named = [MusterDestination.overview] + MusterDestination.fleet + MusterDestination.thisMac + MusterDestination.server
        XCTAssertEqual(named.count, 12)
        XCTAssertEqual(Set(named), Set(MusterDestination.allCases))
        XCTAssertEqual(MusterDestination.allCases.count, 12)
    }

    func test_saved_spelling_restores_its_place() {
        for destination in MusterDestination.allCases {
            XCTAssertEqual(MusterDestination.restored(from: destination.rawValue), destination)
        }
    }

    func test_unsaved_or_unnamed_place_restores_the_overview() {
        XCTAssertEqual(MusterDestination.restored(from: nil), .overview)
        XCTAssertEqual(MusterDestination.restored(from: ""), .overview)
        XCTAssertEqual(MusterDestination.restored(from: "aPlaceTheAppDropped"), .overview)
    }

    /// The saved spellings are a contract with the owner's defaults:  renaming one
    /// silently would put every window back on the overview once.
    func test_saved_spellings_stay_stable() {
        XCTAssertEqual(MusterDestination.overview.rawValue, "overview")
        XCTAssertEqual(MusterDestination.complianceGrid.rawValue, "complianceGrid")
        XCTAssertEqual(MusterSettings.sidebarSelectionDefaultsKey, "musterSidebarSelection")
    }

    // MARK: Overview counts

    private func cell(_ state: CellState, rules: [CheckResult] = []) -> GridCell {
        GridCell(seat: "MM", kind: .skills, state: state, rules: rules, coverReason: nil)
    }

    func test_counts_read_off_the_cells() {
        let cells = [cell(.unknown), cell(.missing), cell(.unknown), cell(.drift), cell(.passed)]
        let counts = OverviewCounts.make(cells: cells, trusted: true, commonCheckedAt: Date())
        XCTAssertEqual(counts.red, 3)
        XCTAssertEqual(counts.unknown, 2)
        XCTAssertEqual(counts.missing, 1)
        XCTAssertEqual(counts.drift, 1)
    }

    /// The fail-closed rule:  a run that could not be trusted counts nothing.
    ///
    /// Zero here would be a fleet measured and found clean, which is the one claim a
    /// failed run cannot make.  Nil is the only honest answer, and it is what the
    /// overview renders as unknown.
    func test_untrusted_run_counts_nothing_never_zero() {
        let cells = [cell(.unknown), cell(.passed)]
        let counts = OverviewCounts.make(cells: cells, trusted: false, commonCheckedAt: Date())
        XCTAssertNil(counts.red)
        XCTAssertNil(counts.unknown)
        XCTAssertNil(counts.missing)
        XCTAssertNil(counts.drift)
        XCTAssertNil(counts.stale)
    }

    /// Stale is only counted against a shared check time.
    ///
    /// Without one there is nothing to be stale against, so the count stays unknown
    /// rather than reading as none.
    func test_stale_stays_unknown_without_a_shared_check_time() {
        let counts = OverviewCounts.make(cells: [cell(.passed)], trusted: true, commonCheckedAt: nil)
        XCTAssertEqual(counts.red, 0)
        XCTAssertNil(counts.stale)
    }
}
