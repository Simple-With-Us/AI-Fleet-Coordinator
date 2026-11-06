import XCTest

/// The Seat Setup panel's logic:  which seat it shows, what it raises, and the one line each
/// kind carries.  The panel is the same for every platform, so these tests use more than one.
final class SeatSetupTests: XCTestCase {
    /// What a test changes about one kind of a seat.
    private struct Spec {
        var state = "PASS"
        var reasonCode = "ok"
        var reason = "because"
        var checkedAt = "2026-11-06T09:00:00Z"
    }

    private func result(seat: String, platform: String, kind: ConfigKind, spec: Spec) -> CheckResult {
        CheckResult(
            ruleId: "\(platform)-\(kind.rawValue)-rule", seat: seat, platform: platform, kind: kind.rawValue,
            state: spec.state, reason: spec.reason, reasonCode: spec.reasonCode,
            expected: [:], observed: [:],
            proofLevelRequired: "L0", proofLevelAchieved: spec.state == "PASS" ? "L0" : nil,
            collector: "mac", checkType: "file_present",
            checkedAt: spec.checkedAt, expiresAt: nil, ttlSeconds: 900,
            stale: false, failing: false)
    }

    /// One seat's cells, with every kind proven except the ones the test changes.
    private func cells(
        seat: String,
        platform: String,
        _ changes: [ConfigKind: Spec] = [:],
        leaving out: Set<ConfigKind> = []
    ) -> [GridCell] {
        let results = ConfigKind.columnOrder
            .filter { !out.contains($0) }
            .map { result(seat: seat, platform: platform, kind: $0, spec: changes[$0] ?? Spec()) }
        return GridBuilder.build(roster: [seat], manifest: nil, results: results)
    }

    private func cell(_ all: [GridCell], _ kind: ConfigKind) throws -> GridCell {
        try XCTUnwrap(all.first { $0.kind == kind })
    }

    // MARK: - which seat

    func test_the_seat_chosen_before_is_kept_while_it_is_on_the_roster() {
        XCTAssertEqual(SeatSetup.resolvedSeat(stored: "CODEX", roster: ["CLAUDE", "CODEX"]), "CODEX")
    }

    func test_a_seat_that_left_the_roster_falls_back_to_the_first_one() {
        XCTAssertEqual(SeatSetup.resolvedSeat(stored: "GONE", roster: ["CLAUDE", "CODEX"]), "CLAUDE")
        XCTAssertEqual(SeatSetup.resolvedSeat(stored: "", roster: ["CLAUDE", "CODEX"]), "CLAUDE")
    }

    func test_an_empty_roster_shows_no_seat() {
        XCTAssertNil(SeatSetup.resolvedSeat(stored: "CLAUDE", roster: []))
    }

    // MARK: - what needs a look

    /// Any platform's seat raises the same states:  a missing file and a drifted one are
    /// raised, a proven one is not, and a platform that lacks the surface is not a problem.
    func test_missing_and_drifting_kinds_are_raised_for_any_platform() {
        let claude = cells(seat: "CLAUDE", platform: "claude", [
            .hooks: Spec(state: "MISSING", reasonCode: "missing", reason: "no hook registered"),
        ])
        let raised = SeatSetup.attention(for: claude)
        XCTAssertEqual(raised.map(\.kind), [.hooks])
        XCTAssertEqual(raised.first?.state, "MISSING")
        XCTAssertEqual(raised.first?.text, "no hook registered")

        let kimi = cells(seat: "KIMI", platform: "kimi", [
            .mcp: Spec(state: "DRIFT", reasonCode: "drift", reason: "mcp.json differs"),
            .skills: Spec(state: "UNSUPPORTED", reasonCode: "unsupported"),
        ])
        let kimiRaised = SeatSetup.attention(for: kimi)
        XCTAssertEqual(kimiRaised.map(\.kind), [.mcp])
        XCTAssertEqual(kimiRaised.first?.text, "mcp.json differs")
    }

    func test_raised_items_follow_the_grids_column_order() {
        let zcode = cells(seat: "ZC", platform: "zcode", [
            .permissions: Spec(state: "MISSING", reasonCode: "missing"),
            .hooks: Spec(state: "UNPROVEN", reasonCode: "proof-not-run"),
            .instructions: Spec(state: "UNKNOWN", reasonCode: "error"),
        ])
        XCTAssertEqual(SeatSetup.attention(for: zcode).map(\.kind), [.instructions, .hooks, .permissions])
    }

    func test_a_seat_with_every_kind_proven_raises_nothing() {
        XCTAssertEqual(SeatSetup.attention(for: cells(seat: "CODEX", platform: "codex")), [])
    }

    /// A seat with no hook mechanism has nothing to guard, which is a fact about the
    /// platform and not a problem with the seat.
    func test_a_seat_with_no_guard_to_check_raises_nothing() throws {
        let amp = cells(seat: "AMP", platform: "amp", [
            .hooks: Spec(state: "UNSUPPORTED", reasonCode: "not-applicable"),
        ])
        XCTAssertTrue(try cell(amp, .hooks).isNoGuard)
        XCTAssertEqual(SeatSetup.attention(for: amp), [])
    }

    func test_a_raised_item_with_no_reason_says_the_engine_gave_none() {
        let mm = cells(seat: "MM", platform: "minimax", [
            .mcp: Spec(state: "MISSING", reasonCode: "missing", reason: ""),
        ])
        XCTAssertEqual(SeatSetup.attention(for: mm).first?.text, "The engine gave no reason.")
    }

    /// A kind with no rule at all is a hole in the coverage, which the grid counts as
    /// unknown.  The panel raises it and says nothing was checked.
    func test_a_kind_with_no_rule_is_raised_as_nothing_checked() throws {
        let grok = cells(seat: "GROK", platform: "grok", leaving: [.skills])
        let raised = SeatSetup.attention(for: grok)
        XCTAssertEqual(raised.map(\.kind), [.skills])
        XCTAssertEqual(raised.first?.state, "UNKNOWN")
        XCTAssertEqual(raised.first?.text, "The engine has no rule for this kind on this seat, so nothing was checked.")
    }

    // MARK: - the platform and the one-line summary

    func test_the_platform_is_the_one_the_rules_name() {
        XCTAssertEqual(SeatSetup.platform(of: cells(seat: "AG", platform: "antigravity")), "antigravity")
        XCTAssertNil(SeatSetup.platform(of: []))
    }

    func test_a_kind_the_engine_has_no_cell_for_says_so() {
        XCTAssertEqual(SeatSetup.summary(for: nil), "The engine has no rule for this seat and kind.")
    }

    func test_the_summary_names_the_state_the_rule_count_and_the_proof() throws {
        let summary = SeatSetup.summary(for: try cell(cells(seat: "CODEX", platform: "codex"), .skills))
        XCTAssertTrue(summary.hasPrefix("PASS, 1 rule, proof L0, checked "), summary)
        XCTAssertTrue(summary.hasSuffix(" ago"), summary)
    }

    func test_a_cell_with_no_check_time_says_it_was_not_checked() throws {
        let codex = cells(seat: "CODEX", platform: "codex", [
            .skills: Spec(state: "MISSING", reasonCode: "missing", checkedAt: "not a time"),
        ])
        let summary = SeatSetup.summary(for: try cell(codex, .skills))
        XCTAssertTrue(summary.hasSuffix("not checked yet"), summary)
    }

    func test_the_summary_for_a_seat_with_no_guard_says_so_in_words() throws {
        let amp = cells(seat: "AMP", platform: "amp", [
            .hooks: Spec(state: "UNSUPPORTED", reasonCode: "not-applicable"),
        ])
        XCTAssertEqual(SeatSetup.summary(for: try cell(amp, .hooks)), "No guard to check on this platform.")
    }

    func test_the_summary_for_a_kind_with_no_rule_says_why_nothing_covers_it() throws {
        let grok = cells(seat: "GROK", platform: "grok", leaving: [.skills])
        XCTAssertEqual(
            SeatSetup.summary(for: try cell(grok, .skills)),
            "No rule covers this seat and kind."
        )
    }
}
