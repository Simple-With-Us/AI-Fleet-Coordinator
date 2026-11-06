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

    // MARK: What a badge says is what was proved

    private func hookResult(
        id: String,
        state: String,
        reasonCode: String,
        required: String = "L1",
        achieved: String?,
        guards: [String] = []
    ) -> CheckResult {
        CheckResult(
            ruleId: id, seat: "CLAUDE", platform: "claude", kind: "hooks",
            state: state, reason: "because", reasonCode: reasonCode,
            expected: guards.isEmpty ? [:] : ["guards": .array(guards.map { .string($0) })],
            observed: [:],
            proofLevelRequired: required, proofLevelAchieved: achieved,
            collector: "mac", checkType: "hook_registered",
            checkedAt: "2026-10-21T09:00:00Z", expiresAt: nil, ttlSeconds: 900,
            stale: false, failing: false)
    }

    private func hooksCell(_ results: [CheckResult], manifest: SeatManifest? = nil) throws -> GridCell {
        let cells = GridBuilder.build(roster: ["CLAUDE"], manifest: manifest, results: results)
        return try XCTUnwrap(cells.first { $0.kind == .hooks })
    }

    /// A cell whose rule needs L1 and reached nothing must not print L1 or say a write
    /// was denied:  the badge and the tooltip state the proof reached, not the proof asked.
    func test_a_cell_that_reached_no_proof_does_not_claim_the_one_it_needs() throws {
        let cell = try hooksCell([hookResult(id: "claude-hooks-lane-guard", state: "MISSING", reasonCode: "missing", achieved: nil)])
        XCTAssertEqual(cell.proofLevel, "L1")
        XCTAssertEqual(cell.proofBadgeText, "—")
        XCTAssertTrue(cell.proofDescription.hasPrefix("Not proved."))
        XCTAssertFalse(cell.proofDescription.hasPrefix("L1"))
        XCTAssertFalse(cell.proofDescription.contains("Reached"))
    }

    /// A cell that reached L0 and needs L1 says both, in that order.
    func test_a_cell_that_reached_l0_and_needs_l1_names_the_gap() throws {
        let cell = try hooksCell([hookResult(id: "claude-hooks-lane-guard", state: "UNPROVEN", reasonCode: "proof-not-run", achieved: "L0")])
        XCTAssertEqual(cell.proofBadgeText, "L0")
        XCTAssertTrue(cell.proofDescription.hasPrefix("Reached L0:"))
        XCTAssertTrue(cell.proofDescription.contains("Needs L1:"))
    }

    /// A cell that reached what it needs says it reached it.
    func test_a_cell_that_reached_l1_says_so() throws {
        let cell = try hooksCell([hookResult(id: "claude-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1")])
        XCTAssertEqual(cell.state, .passed)
        XCTAssertEqual(cell.proofBadgeText, "L1")
        XCTAssertTrue(cell.proofDescription.hasPrefix("Reached L1:"))
        XCTAssertFalse(cell.proofDescription.contains("Needs"))
    }

    /// The L0 words promise a read and a comparison, never a hash:  only the instruction
    /// blocks compare one.
    func test_the_l0_words_do_not_promise_a_hash() {
        XCTAssertFalse(GridCell.levelWords("L0").contains("hash"))
    }

    /// A shell-only guard reads PASS for the rule that asks for shell guarding, and the
    /// cell reads as partly proven, naming the file guarding that is missing.
    func test_a_shell_only_guard_is_partly_proven_and_names_what_is_missing() throws {
        let cell = try hooksCell([
            hookResult(id: "claude-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]),
            hookResult(id: "claude-hooks-file-guard", state: "UNPROVEN", reasonCode: "proof-not-run", achieved: "L0", guards: ["file"]),
        ])
        XCTAssertEqual(cell.state, .unproven)
        XCTAssertTrue(cell.isPartlyProven)
        XCTAssertEqual(cell.proofBadgeText, "L1 1/2")
        XCTAssertTrue(cell.tooltip.contains("claude-hooks-lane-guard:  PASS (shell guarding)"))
        XCTAssertTrue(cell.tooltip.contains("claude-hooks-file-guard:  UNPROVEN (file guarding)"))
    }

    /// The sentence about a partly proven cell does not claim the strongest level for
    /// the rules that did not reach it, and it names what was not proven.
    func test_a_partly_proven_cell_does_not_describe_the_strongest_level_for_all_of_it() throws {
        let cell = try hooksCell([
            hookResult(id: "claude-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]),
            hookResult(id: "claude-hooks-file-guard", state: "UNPROVEN", reasonCode: "proof-not-run", achieved: nil, guards: ["file"]),
        ])
        XCTAssertTrue(cell.proofDescription.hasPrefix("Partly proven:"))
        XCTAssertTrue(cell.proofDescription.contains("1 of 2"))
        XCTAssertTrue(cell.proofDescription.contains("file guarding"))
        XCTAssertFalse(cell.proofDescription.contains("Reached L1"))
    }

    /// A rule that does not apply asks for nothing:  a shell guard on a harness whose hook
    /// carries no file tool is a PASS for what the cell asks, and the tooltip says the file
    /// rule does not apply.
    func test_a_rule_that_does_not_apply_neither_helps_nor_hurts_the_cell() throws {
        let cell = try hooksCell([
            hookResult(id: "claude-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]),
            hookResult(id: "claude-hooks-file-guard", state: "UNSUPPORTED", reasonCode: "not-applicable", achieved: nil, guards: ["file"]),
        ])
        XCTAssertEqual(cell.state, .passed)
        XCTAssertFalse(cell.isNoGuard)
        XCTAssertFalse(cell.isPartlyProven)
        XCTAssertEqual(cell.proofBadgeText, "L1")
        XCTAssertTrue(cell.proofDescription.hasPrefix("Reached L1:"))
        XCTAssertTrue(cell.tooltip.contains("claude-hooks-file-guard:  does not apply (file guarding)"))
    }

    /// The scope a rule asks for comes from the engine's `guards` field.
    func test_the_guard_scope_comes_from_the_engine() {
        XCTAssertEqual(hookResult(id: "a", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]).guardScope, "shell guarding")
        XCTAssertEqual(hookResult(id: "a", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell", "file"]).guardScope, "shell and file guarding")
        XCTAssertNil(hookResult(id: "a", state: "PASS", reasonCode: "ok", achieved: "L1").guardScope)
    }

    /// A seat with no guard never reads PASS, and its badge says why in words.
    func test_a_seat_with_no_guard_reads_no_guard_and_not_pass() throws {
        let cell = try hooksCell([hookResult(id: "claude-hooks-none", state: "UNSUPPORTED", reasonCode: "not-applicable", required: "L0", achieved: nil)])
        XCTAssertTrue(cell.isNoGuard)
        XCTAssertNotEqual(cell.state, .passed)
        XCTAssertEqual(cell.stateName, "NO GUARD")
        XCTAssertEqual(cell.proofBadgeText, "—")
        XCTAssertTrue(cell.tooltip.contains("no guard to check"))
        XCTAssertTrue(cell.tooltip.contains("not a pass"))
    }

    /// An unsupported rule that is not a "no guard" rule keeps its own word.
    func test_an_unsupported_rule_is_not_a_no_guard_cell() throws {
        let cell = try hooksCell([hookResult(id: "zc-hooks-file-guard", state: "UNSUPPORTED", reasonCode: "unsupported", achieved: nil)])
        XCTAssertFalse(cell.isNoGuard)
        XCTAssertEqual(cell.stateName, "UNSUP")
    }

    /// PASS has one meaning, and the app states it from one place.
    func test_pass_means_one_thing() {
        XCTAssertTrue(PassMeaning.everywhere.contains("every check the rule asks for was proven"))
        XCTAssertTrue(PassMeaning.verdict("PASS").contains("Every check every rule asks for was proven"))
        XCTAssertTrue(PassMeaning.verdict("UNKNOWN").contains("not every check was proven"))
        XCTAssertTrue(PassMeaning.verdict(nil).contains("not every check was proven"))
        XCTAssertTrue(PassMeaning.appRow.contains("Nothing else about the app was checked"))
        // A badge with no sentence of its own never claims a config rule was proven.
        XCTAssertTrue(PassMeaning.panelRow.contains("this panel makes"))
        XCTAssertFalse(PassMeaning.panelRow.contains("the rule"))
    }

    /// The wake panel's matching state is not called PASS.
    func test_the_wake_panel_does_not_call_a_match_a_pass() {
        XCTAssertEqual(WakeSettingState.match.label, "MATCHES")
        XCTAssertNotEqual(WakeSettingState.unproven.label, "PASS")
        XCTAssertNotEqual(WakeSettingState.unpublished.label, "PASS")
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
