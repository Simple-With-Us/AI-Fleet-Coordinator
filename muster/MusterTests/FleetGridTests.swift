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

    /// Z Code's hook is Claude-shaped, so it carries a file rule like the other guarded
    /// seats:  a passing shell rule beside an unproven file rule reads UNPRV with 1/2,
    /// never a bare PASS.
    func test_zcode_with_a_passing_shell_rule_is_partly_proven_not_pass() throws {
        func zc(_ id: String, state: String, reasonCode: String, achieved: String?, guards: [String]) -> CheckResult {
            CheckResult(
                ruleId: id, seat: "ZC", platform: "zcode", kind: "hooks",
                state: state, reason: "because", reasonCode: reasonCode,
                expected: ["guards": .array(guards.map { .string($0) })],
                observed: [:],
                proofLevelRequired: "L1", proofLevelAchieved: achieved,
                collector: "mac", checkType: "hook_registered",
                checkedAt: "2026-10-21T09:00:00Z", expiresAt: nil, ttlSeconds: 900,
                stale: false, failing: false)
        }
        let cells = GridBuilder.build(roster: ["ZC"], manifest: nil, results: [
            zc("zc-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]),
            zc("zc-hooks-file-guard", state: "UNPROVEN", reasonCode: "proof-not-run", achieved: nil, guards: ["file"]),
        ])
        let cell = try XCTUnwrap(cells.first { $0.kind == .hooks })
        XCTAssertEqual(cell.state, .unproven)
        XCTAssertNotEqual(cell.state, .passed)
        XCTAssertTrue(cell.isPartlyProven)
        XCTAssertEqual(cell.proofBadgeText, "L1 1/2")
        XCTAssertTrue(cell.proofDescription.contains("file guarding"))
    }

    /// A guard that denies both a shell sentinel and a file write into a forbidden tree
    /// passes both rules, and the cell reads PASS with nothing missing.  A guard the
    /// harness hands file tools to that lets the forbidden write through reads DRIFT.
    func test_a_guard_that_denies_the_forbidden_file_write_passes_the_cell() throws {
        let passing = try hooksCell([
            hookResult(id: "claude-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]),
            hookResult(id: "claude-hooks-file-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["file"]),
        ])
        XCTAssertEqual(passing.state, .passed)
        XCTAssertFalse(passing.isPartlyProven)
        XCTAssertEqual(passing.proofBadgeText, "L1")
        let leaking = try hooksCell([
            hookResult(id: "claude-hooks-lane-guard", state: "PASS", reasonCode: "ok", achieved: "L1", guards: ["shell"]),
            hookResult(id: "claude-hooks-file-guard", state: "DRIFT", reasonCode: "proof-failed", achieved: nil, guards: ["file"]),
        ])
        XCTAssertEqual(leaking.state, .drift)
        XCTAssertNotEqual(leaking.state, .passed)
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
        // A missing or unrecognised verdict is never a soft OK:  it claims nothing.
        for unknown in [nil, "", "pass", "MAYBE", "ERROR"] as [String?] {
            XCTAssertEqual(PassMeaning.verdict(unknown), "No verdict was returned, so nothing is claimed.")
            XCTAssertFalse(PassMeaning.verdict(unknown).contains("Nothing failed"))
        }
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

    // MARK: GRID-10:  an empty cell says why it is empty

    /// "No rule covers this pair" is said only when the manifest was read and names no
    /// rule;  a pair with a rule says the engine has not answered,  is checking,  or left it
    /// out,  and a pair with no manifest says the rules are not known.
    func test_an_empty_cell_says_why_it_is_empty() throws {
        let loaded = try manifest()
        func cell(
            _ kind: ConfigKind,
            manifest: SeatManifest?,
            checking: Bool = false,
            answered: Bool = true
        ) throws -> GridCell {
            let cells = GridBuilder.build(
                roster: ["CLAUDE"],
                manifest: manifest,
                results: [],
                checking: checking,
                answered: answered
            )
            return try XCTUnwrap(cells.first { $0.kind == kind })
        }
        // The manifest names no instructions rule for CLAUDE:  a true hole.
        let hole = try cell(.instructions, manifest: loaded)
        XCTAssertEqual(hole.gap, .noRule)
        XCTAssertTrue(hole.tooltip.contains("no rule covers this pair"))
        // The manifest names a hooks rule, so the words follow the engine.
        let waiting = try cell(.hooks, manifest: loaded, answered: false)
        XCTAssertEqual(waiting.gap, .notAnswered)
        XCTAssertFalse(waiting.tooltip.contains("no rule covers"))
        let checking = try cell(.hooks, manifest: loaded, checking: true, answered: false)
        XCTAssertEqual(checking.gap, .beingChecked)
        XCTAssertTrue(checking.isCheckingWithNoAnswer)
        XCTAssertTrue(checking.tooltip.contains("being checked now"))
        XCTAssertEqual(try cell(.hooks, manifest: loaded).gap, .notInAnswer)
        // With no manifest the rules are not known, whatever the engine is doing.
        let unread = try cell(.hooks, manifest: nil, answered: false)
        XCTAssertEqual(unread.gap, .noManifest)
        XCTAssertTrue(unread.tooltip.contains("manifest was not read"))
        XCTAssertFalse(unread.tooltip.contains("no rule covers"))
        // Every state stays fail closed:  an empty cell is unknown, never a pass.
        XCTAssertEqual(checking.state, .unknown)
        XCTAssertEqual(unread.state, .unknown)
    }

    /// A rule the last answer left out stays a finding while the next run checks it:  it is
    /// not "being checked with no answer",  so the Red and Needing Attention filters keep its
    /// seat,  and its tooltip says the engine answered without it.
    func test_a_rule_the_last_answer_left_out_stays_a_finding_during_a_run() throws {
        let cells = GridBuilder.build(
            roster: ["CLAUDE"],
            manifest: try manifest(),
            results: [],
            checking: true,
            answered: true
        )
        let hooks = try XCTUnwrap(cells.first { $0.kind == .hooks })
        XCTAssertEqual(hooks.gap, .notInAnswer)
        XCTAssertTrue(hooks.isBeingChecked)
        XCTAssertFalse(hooks.isCheckingWithNoAnswer)
        XCTAssertEqual(hooks.state, .unknown)
        XCTAssertTrue(GridStateFilter.red.includes([hooks], commonCheckedAt: nil))
        XCTAssertTrue(GridStateFilter.attention.includes([hooks], commonCheckedAt: nil))
        XCTAssertFalse(hooks.tooltip.contains("has not answered"), hooks.tooltip)
        XCTAssertTrue(hooks.tooltip.hasSuffix(GridCell.checkingLine), hooks.tooltip)
    }

    /// With only the last good copy of a manifest that changed and could not be read again,
    /// a pair it names no rule for is not called a hole in the coverage.
    func test_a_stale_manifest_does_not_say_no_rule_covers_the_pair() throws {
        let cells = GridBuilder.build(
            roster: ["CLAUDE"],
            manifest: try manifest(),
            results: [],
            manifestCurrent: false
        )
        let instructions = try XCTUnwrap(cells.first { $0.kind == .instructions })
        XCTAssertEqual(instructions.gap, .manifestStale)
        XCTAssertFalse(instructions.tooltip.contains("no rule covers"), instructions.tooltip)
        XCTAssertTrue(instructions.tooltip.contains("last good copy"), instructions.tooltip)
        XCTAssertEqual(instructions.state, .unknown)
        // A pair the copy names a rule for is unchanged.
        XCTAssertEqual(try XCTUnwrap(cells.first { $0.kind == .hooks }).gap, .notInAnswer)
    }

    /// A cell with an answer keeps it while a run checks it,  and says so.
    func test_a_cell_being_checked_keeps_its_answer_and_says_so() throws {
        let envelope = try EnvelopeDecoder.decode(Data(Fixtures.envelope.utf8))
        let cells = GridBuilder.build(
            roster: ["CLAUDE"],
            manifest: try manifest(),
            results: envelope.results,
            checking: true
        )
        let hooks = try XCTUnwrap(cells.first { $0.kind == .hooks })
        XCTAssertEqual(hooks.state, .passed)
        XCTAssertTrue(hooks.isBeingChecked)
        XCTAssertFalse(hooks.isCheckingWithNoAnswer)
        XCTAssertTrue(hooks.tooltip.hasSuffix(GridCell.checkingLine))
    }

    /// A cell still being checked with no earlier answer does not list its seat as needing
    /// attention;  the same cell once the engine has not answered does.
    func test_a_cell_being_checked_for_the_first_time_is_not_a_finding() {
        let checking = GridCell(
            seat: "MM", kind: .hooks, state: .unknown, rules: [], coverReason: nil,
            gap: .beingChecked, isBeingChecked: true
        )
        let unanswered = GridCell(
            seat: "MM", kind: .hooks, state: .unknown, rules: [], coverReason: nil,
            gap: .notAnswered
        )
        XCTAssertFalse(GridStateFilter.attention.includes([checking], commonCheckedAt: nil))
        XCTAssertFalse(GridStateFilter.red.includes([checking], commonCheckedAt: nil))
        XCTAssertTrue(GridStateFilter.attention.includes([unanswered], commonCheckedAt: nil))
        XCTAssertTrue(GridStateFilter.all.includes([checking], commonCheckedAt: nil))
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
