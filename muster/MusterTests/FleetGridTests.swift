import XCTest
/// The grid the app draws from the envelope's flat list.
final class FleetGridTests: XCTestCase {
    private func manifest() throws -> SeatManifest {
        try JSONDecoder().decode(SeatManifest.self, from: Data(Fixtures.manifest.utf8))
    }

    private func registry() throws -> FleetRegistry {
        try JSONDecoder().decode(FleetRegistry.self, from: Data(Fixtures.registry.utf8))
    }

    /// The rows are the live seats, in registry order, and a retired seat is not one.
    func test_the_rows_are_the_live_seats_in_registry_order() throws {
        let roster = FleetStore.roster(from: try registry(), manifest: try manifest())
        XCTAssertEqual(roster, ["CLAUDE", "ECHO"])
    }

    /// A seat with no Mac surface reads as covered, not as unknown.
    func test_a_covered_pair_reads_as_covered() throws {
        let cells = GridBuilder.build(roster: ["ECHO"], manifest: try manifest(), results: [])
        XCTAssertEqual(cells.count, ConfigKind.columnOrder.count)
        XCTAssertTrue(cells.allSatisfy { $0.state == .covered })
        XCTAssertTrue(cells.allSatisfy { ($0.coverReason ?? "").isEmpty == false })
    }

    /// A pair with neither a rule nor a cover reads as unknown:  silence is not a pass.
    func test_a_pair_with_no_rule_and_no_cover_reads_as_unknown() throws {
        let cells = GridBuilder.build(roster: ["CLAUDE"], manifest: try manifest(), results: [])
        let hooks = cells.first { $0.kind == .hooks }
        XCTAssertEqual(hooks?.state, .unknown)
        let skills = cells.first { $0.kind == .skills }
        XCTAssertEqual(skills?.state, .unknown)
    }

    /// The worst rule in a cell is the one the cell shows.
    func test_the_worst_rule_wins_in_a_cell() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.mixedEnvelope.utf8))
        let cells = GridBuilder.build(
            roster: ["CLAUDE"],
            manifest: try manifest(),
            results: envelope.results
        )
        let hooks = cells.first { $0.kind == .hooks }
        XCTAssertEqual(hooks?.state, .drift)
        XCTAssertEqual(hooks?.rules.count, 2)
    }

    /// The rules behind a cell are ordered worst first, the way the CLI orders its table.
    func test_the_rules_of_a_cell_are_ordered_worst_first() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.mixedEnvelope.utf8))
        let cells = GridBuilder.build(
            roster: ["CLAUDE"],
            manifest: try manifest(),
            results: envelope.results
        )
        let hooks = cells.first { $0.kind == .hooks }
        XCTAssertEqual(hooks?.rules.first?.ruleId, "claude-hooks-b")
    }

    /// The rule that makes a run fail closed is the one the engine marks as failing.
    func test_the_failure_marker_comes_from_the_engine() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.mixedEnvelope.utf8))
        let failing = envelope.results.filter { $0.failing }
        XCTAssertEqual(failing.count, 1)
        XCTAssertEqual(failing.first?.ruleId, "claude-hooks-b")
    }

    /// The rule that matters most:  a run that produced nothing leaves every observed
    /// cell unknown, never the last green that was on screen.
    func test_no_results_leave_every_observed_cell_unknown() throws {
        let cells = GridBuilder.build(roster: ["CLAUDE"], manifest: try manifest(), results: [])
        XCTAssertTrue(cells.allSatisfy { $0.state == .unknown })
    }

    /// The cell shows the proof level the rules required and the age of the oldest one.
    func test_the_cell_carries_the_proof_level_and_the_age() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.envelope.utf8))
        let cells = GridBuilder.build(
            roster: ["CLAUDE"],
            manifest: try manifest(),
            results: envelope.results
        )
        let hooks = cells.first { $0.kind == .hooks }
        XCTAssertEqual(hooks?.proofLevel, "L1")
        XCTAssertEqual(hooks?.proofLevelAchieved, "L1")
        XCTAssertNotNil(hooks?.checkedAt)
        XCTAssertFalse((hooks?.ageText ?? "").isEmpty)
    }

    /// The manifest's cover reason is what a covered cell shows.
    func test_the_cover_reason_comes_from_the_manifest() throws {
        let manifest = try manifest()
        XCTAssertEqual(
            manifest.coverReason(seat: "ECHO", kind: .hooks),
            "Echo runs elsewhere."
        )
        XCTAssertNil(manifest.coverReason(seat: "CLAUDE", kind: .hooks))
    }
}
