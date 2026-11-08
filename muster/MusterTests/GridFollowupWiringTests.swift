import XCTest

/// The views wired to PR 1496's model follow-ups:  the compliance rules list shows each
/// rule's real result (GRID-18), Drift History lists the compliance results failing now
/// (GRID-18), the Overview and Drift History say what the watcher actually holds and fold the
/// paths it does not (GRID-37), and the Overview measures its counts' age from the last
/// trusted run (GRID-33).
///
/// The decisions are plain functions in `GridPresentation`, tested here.  The views are
/// SwiftUI files the logic bundle does not compile, so their wiring is pinned by reading their
/// source, as `GridWiringTests` does.
final class GridFollowupWiringTests: XCTestCase {
    // MARK: Fixtures

    private func complianceResult(
        _ ruleID: String,
        place: String = "mac",
        state: String,
        failing: Bool? = nil,
        reason: String = "because"
    ) -> CheckResult {
        let fails = failing ?? (state == "DRIFT" || state == "MISSING")
        return CheckResult(
            ruleId: ruleID, seat: "", platform: place, kind: CheckResult.complianceKind,
            state: state, reason: reason, reasonCode: fails ? "drift" : "ok",
            expected: [:], observed: [:],
            proofLevelRequired: nil, proofLevelAchieved: nil,
            collector: "mac", checkType: "command",
            checkedAt: "2026-11-07T15:00:00Z", expiresAt: nil, ttlSeconds: 900,
            stale: false, failing: fails
        )
    }

    private func status(
        _ ruleID: String,
        scope: [String] = ["mac"],
        standing: ComplianceRuleStatus.Standing,
        results: [CheckResult] = []
    ) -> ComplianceRuleStatus {
        ComplianceRuleStatus(ruleID: ruleID, ownerSeat: nil, scope: scope, standing: standing, results: results)
    }

    private func source(_ relative: String) throws -> String {
        let muster = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Muster")
        let url = muster.appendingPathComponent(relative)
        try XCTUnwrap(
            FileManager.default.fileExists(atPath: url.path) ? url : nil,
            "muster/Muster/\(relative) is missing"
        )
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// The text of one function in a view's source, from its declaration to the next one.
    private func body(of function: String, before next: String, in text: String) throws -> String {
        let start = try XCTUnwrap(text.range(of: function), "the view no longer has \(function)")
        let rest = text[start.upperBound...]
        let end = try XCTUnwrap(rest.range(of: next), "the view no longer has \(next) after \(function)")
        return String(rest[..<end.lowerBound])
    }

    // MARK: The compliance rules' results (GRID-18)

    /// A rule the engine answered for wears the grid's own colour and symbol for its state.
    func test_an_answered_rule_wears_the_grids_colour_for_its_state() {
        let passing = GridPresentation.ComplianceRuleLook.of(
            status("compliance-secrets-hygiene", standing: .answered(.passed),
                   results: [complianceResult("compliance-secrets-hygiene", state: "PASS")])
        )
        XCTAssertEqual(passing.colourState, .passed)
        XCTAssertEqual(passing.word, CellState.passed.gridWord)
        XCTAssertEqual(passing.symbol, CellState.passed.gridSymbol)

        let drifted = GridPresentation.ComplianceRuleLook.of(
            status("compliance-secrets-hygiene", standing: .answered(.drift),
                   results: [complianceResult("compliance-secrets-hygiene", state: "DRIFT")])
        )
        XCTAssertEqual(drifted.colourState, .drift)
        XCTAssertEqual(drifted.word, CellState.drift.gridWord)

        let missing = GridPresentation.ComplianceRuleLook.of(
            status("compliance-apple-cert-expiry", standing: .answered(.missing),
                   results: [complianceResult("compliance-apple-cert-expiry", state: "MISSING")])
        )
        XCTAssertEqual(missing.colourState, .missing)
    }

    /// The engine's failing flag wins over a calm-looking state, so a failure never wears green.
    func test_a_flagged_failure_never_wears_a_calm_colour() {
        let flagged = status(
            "compliance-secrets-hygiene",
            standing: .answered(.passed),
            results: [complianceResult("compliance-secrets-hygiene", state: "PASS", failing: true)]
        )
        XCTAssertTrue(flagged.isFailing)
        let look = GridPresentation.ComplianceRuleLook.of(flagged)
        XCTAssertNotEqual(look.colourState, .passed)
        XCTAssertEqual(look.colourState, .unknown)
        // The word follows the escalated state, so the badge never shows a calm word on red.
        XCTAssertEqual(look.word, CellState.unknown.gridWord)
        XCTAssertNotEqual(look.word, flagged.word)
    }

    /// A rule with no result claims nothing:  the neutral colour, never green and never red,
    /// with the status's own word and a symbol of its own.
    func test_a_rule_with_no_result_wears_the_neutral_colour() {
        let notBuilt = GridPresentation.ComplianceRuleLook.of(
            status("compliance-litestream-heartbeat", standing: .notBuilt(reason: "no check type yet"))
        )
        XCTAssertNil(notBuilt.colourState)
        XCTAssertEqual(notBuilt.word, ComplianceRuleStatus.notBuiltWord)
        XCTAssertEqual(notBuilt.symbol, "hammer")

        let notInAnswer = GridPresentation.ComplianceRuleLook.of(
            status("compliance-apple-cert-expiry", standing: .notInAnswer)
        )
        XCTAssertNil(notInAnswer.colourState)
        XCTAssertEqual(notInAnswer.word, ComplianceRuleStatus.noResultWord)

        let notAnswered = GridPresentation.ComplianceRuleLook.of(
            status("compliance-secrets-hygiene", standing: .notAnswered)
        )
        XCTAssertNil(notAnswered.colourState)
        XCTAssertEqual(notAnswered.word, ComplianceRuleStatus.notCheckedWord)
        XCTAssertEqual(notAnswered.symbol, GridPresentation.checkingSymbol)

        for look in [notBuilt, notInAnswer, notAnswered] {
            XCTAssertNotEqual(look.symbol, CellState.unknown.gridSymbol, "a rule with no result is not a red UNKNOWN")
            XCTAssertNotEqual(look.symbol, CellState.passed.gridSymbol, "a rule with no result is not a pass")
        }
    }

    /// The statuses group by their own place words, the Mac first, each rule once, in the
    /// store's order within a group.
    func test_the_statuses_group_by_their_place_words() {
        let statuses = [
            status("server-rule", scope: ["hetzner"], standing: .notInAnswer),
            status("mac-one", scope: ["mac"], standing: .answered(.passed)),
            status("nowhere", scope: [], standing: .notBuilt(reason: nil)),
            status("mac-two", scope: ["mac"], standing: .notAnswered),
            status("fleet-rule", scope: ["fleet"], standing: .notAnswered),
            status("mac-one", scope: ["mac"], standing: .notAnswered),
        ]
        let groups = GridPresentation.complianceStatusGroups(statuses)
        XCTAssertEqual(groups.map(\.heading), [
            GridPresentation.scopeWords("mac"),
            GridPresentation.scopeWords("hetzner"),
            GridPresentation.scopeWords("fleet"),
            "No Place Named",
        ])
        XCTAssertEqual(groups.first?.ruleIDs, ["mac-one", "mac-two"])
        XCTAssertEqual(groups.map(\.heading), groups.map { group in
            statuses.first { $0.ruleID == group.ruleIDs.first }?.placeWords ?? ""
        })
        XCTAssertEqual(groups.flatMap(\.ruleIDs).count, 5, "each rule once")
    }

    /// The list's explanation once it shows results, and the failing list's words, carry the
    /// owner's gap and Title Case where they are headings.
    func test_the_compliance_words() {
        XCTAssertTrue(GridPresentation.complianceResultsExplanation.contains(GridPresentation.gap))
        XCTAssertEqual(GridPresentation.complianceFailuresTitle(count: 2), "Compliance Rules Failing Now (2)")
        XCTAssertTrue(GridPresentation.complianceFailuresExplanation.contains(ServerStatusCopy.sentenceGap))
        XCTAssertFalse(GridPresentation.complianceFailuresExplanation.contains(".  "))
    }

    /// A failing result wears its own warning colour, and a calm-looking state the engine
    /// flagged as failing wears UNKNOWN's red rather than a green.
    func test_a_failing_result_wears_a_warning_colour() {
        func failure(_ state: String) -> DriftComplianceFailure {
            DriftComplianceFailure(ruleID: "compliance-secrets-hygiene", place: "mac", state: state, reason: "3 findings")
        }
        XCTAssertEqual(GridPresentation.colourState(of: failure("DRIFT")), .drift)
        XCTAssertEqual(GridPresentation.colourState(of: failure("MISSING")), .missing)
        XCTAssertEqual(GridPresentation.colourState(of: failure("PASS")), .unknown)
        XCTAssertEqual(failure("DRIFT").placeTitle, GridPresentation.scopeWords("mac"))
    }

    /// The grid's rules list reads `FleetStore.complianceStatuses`, shows each rule's word,
    /// sentence and place words, and never paints a rule red or orange on its own.
    func test_the_grid_lists_each_rules_result() throws {
        let view = try source("Views/FleetGridView.swift")
        XCTAssertTrue(view.contains("let complianceStatuses = store.complianceStatuses"))
        XCTAssertTrue(view.contains("if !complianceStatuses.isEmpty {"))
        XCTAssertTrue(view.contains("complianceSection(complianceStatuses, rules: store.manifest?.allComplianceRules ?? [])"))
        XCTAssertTrue(view.contains("Text(GridPresentation.complianceResultsExplanation)"))
        XCTAssertFalse(view.contains("Text(GridPresentation.complianceExplanation)"),
                       "the list shows results now, so the old words would not be true")
        XCTAssertTrue(view.contains("GridPresentation.complianceStatusGroups(statuses)"))
        XCTAssertFalse(view.contains("GridPresentation.complianceStatus(rule)"))

        let row = try body(of: "private func complianceRow(", before: "private var panelHeightBinding", in: view)
        XCTAssertTrue(row.contains("GridPresentation.ComplianceRuleLook.of(status)"))
        XCTAssertTrue(row.contains("look.colourState?.tint ?? Color.secondary"))
        XCTAssertTrue(row.contains("Text(look.word)"))
        XCTAssertTrue(row.contains("Image(systemName: look.symbol)"))
        XCTAssertTrue(row.contains("Text(status.sentence)"))
        XCTAssertTrue(row.contains("Text(status.placeWords)"))
        XCTAssertFalse(row.contains("Color.red"), "a rule's colour comes from its look alone")
        XCTAssertFalse(row.contains("Color.orange"), "a rule's colour comes from its look alone")
        XCTAssertFalse(row.contains("Color.green"), "a rule's colour comes from its look alone")
    }

    /// Drift History lists the compliance results failing now, with their place in the app's
    /// words, only when there are some.
    func test_drift_history_lists_the_failing_compliance_results() throws {
        let view = try source("Views/DriftHistoryView.swift")
        XCTAssertTrue(view.contains("if !monitor.complianceFailures.isEmpty {"))
        XCTAssertTrue(view.contains("complianceFailures(monitor.complianceFailures)"))
        XCTAssertTrue(view.contains("GridPresentation.complianceFailuresTitle(count: failures.count)"))
        XCTAssertTrue(view.contains("Text(GridPresentation.complianceFailuresExplanation)"))
        XCTAssertTrue(view.contains("Text(failure.ruleID)"))
        XCTAssertTrue(view.contains("Text(failure.placeTitle)"))
        XCTAssertTrue(view.contains("Text(failure.state)"))
        XCTAssertTrue(view.contains("Text(failure.reason)"))
        XCTAssertTrue(view.contains("GridPresentation.colourState(of: failure).tint"))
    }

    // MARK: What the watcher holds (GRID-37)

    /// The disclosure's heading is there only when a path has no started watch, and its
    /// lines are the paths in the list's order, the home folder as `~`.
    func test_the_paths_not_watched() {
        XCTAssertNil(GridPresentation.unwatchedPathsTitle(.empty))
        XCTAssertEqual(GridPresentation.unwatchedPathLines(.empty), [])

        let elsewhere = URL(fileURLWithPath: "/tmp/muster-coverage/absent.json")
        let atHome = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".claude/settings.json")
        let coverage = DriftWatchCoverage(
            pathsAsked: 3,
            pathsWatched: 1,
            streamsAsked: 3,
            streamsStarted: 1,
            unwatchedPaths: [elsewhere, atHome]
        )
        XCTAssertEqual(GridPresentation.unwatchedPathsTitle(coverage), "Paths Not Watched (2)")
        XCTAssertEqual(GridPresentation.unwatchedPathLines(coverage), [
            "/tmp/muster-coverage/absent.json",
            "~/.claude/settings.json",
        ])

        // A failed stream whose path another stream still covers leaves no path unwatched.
        let covered = DriftWatchCoverage(
            pathsAsked: 2, pathsWatched: 2, streamsAsked: 3, streamsStarted: 2, unwatchedPaths: []
        )
        XCTAssertFalse(covered.isComplete)
        XCTAssertNil(GridPresentation.unwatchedPathsTitle(covered))
    }

    /// The Overview and Drift History use the coverage overload, so a stream that failed to
    /// start shows its warning, and both fold the paths not watched under the line.
    func test_the_overview_and_drift_history_say_what_the_watcher_holds() throws {
        for file in ["Views/OverviewView.swift", "Views/DriftHistoryView.swift"] {
            let view = try source(file)
            XCTAssertTrue(view.contains("coverage: monitor.watchCoverage,"), file)
            XCTAssertFalse(view.contains("pathCount: monitor.watchedPathCount"), "\(file) still uses the count-only line")
            XCTAssertTrue(view.contains("UnwatchedPathsDisclosure(coverage: monitor.watchCoverage)"), file)
        }
        let history = try source("Views/DriftHistoryView.swift")
        XCTAssertTrue(history.contains("struct UnwatchedPathsDisclosure: View"))
        XCTAssertTrue(history.contains("GridPresentation.unwatchedPathsTitle(coverage)"))
        XCTAssertTrue(history.contains("GridPresentation.unwatchedPathLines(coverage)"))
        XCTAssertTrue(history.contains("Text(GridPresentation.unwatchedPathsExplanation)"))
    }

    // MARK: The last trusted run (GRID-33)

    /// The Overview's counts and the grid's sidebar badge are a trusted run's, so their age
    /// is measured from the last trusted run.
    func test_the_counts_age_from_the_last_trusted_run() throws {
        let overview = try source("Views/OverviewView.swift")
        XCTAssertTrue(overview.contains(
            "monitor.lastTrustedRunAt.map { SeatCheckFreshness.isCurrent(checkedAt: $0, now: watch.now) } ?? true"
        ))
        let window = try source("FleetOverviewView.swift")
        XCTAssertTrue(window.contains("if let checkedAt = monitor.lastTrustedRunAt,"))
        XCTAssertFalse(window.contains("if let checkedAt = monitor.lastRunAt,"))
    }

    // MARK: Gone names

    /// `GridCell.stateFullName` is gone (PR 1496):  no source in the app names it.
    func test_no_source_names_the_removed_state_full_name() throws {
        let muster = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Muster")
        let files = try XCTUnwrap(FileManager.default.enumerator(at: muster, includingPropertiesForKeys: nil))
        var checked = 0
        for case let url as URL in files where url.pathExtension == "swift" {
            let text = try String(contentsOf: url, encoding: .utf8)
            XCTAssertFalse(text.contains("stateFullName"), url.lastPathComponent)
            checked += 1
        }
        XCTAssertGreaterThan(checked, 0)
    }
}
