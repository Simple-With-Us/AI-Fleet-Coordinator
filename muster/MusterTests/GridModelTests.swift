import XCTest

// The model half of the Compliance Grid's review findings:
//
//   * GRID-18:  the engine's compliance results reach the store with each rule's real state,
//     and a failing one goes through the drift alert path.
//   * GRID-37:  the watcher counts the streams that started,  apart from the paths asked for.
//   * GRID-33:  a wall-clock safety net measured from the last trusted run,  and a run after
//     a wake,  never a second run while one is in flight.
//   * Seat Setup spells each state the grid's one way (`gridWord`).

// MARK: - Builders

/// One compliance result,  as the engine writes it:  kind `compliance` and no seat.
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

/// One compliance rule,  as the manifest declares it.
private func complianceRule(
    _ id: String,
    scope: [String] = ["mac"],
    implemented: Bool? = true,
    reason: String? = nil
) -> ComplianceRule {
    ComplianceRule(
        id: id,
        ownerSeat: "CLAUDE",
        scope: scope,
        implemented: implemented,
        implementationReason: reason,
        check: nil
    )
}

/// Lets the monitor's own hops to the main actor finish.
@MainActor
private func settleGridModel() async {
    for _ in 0..<6 {
        await Task.yield()
    }
}

/// A wake observer the test fires by hand.
final class ManualWake: DriftWakeObserving {
    private var onWake: (() -> Void)?
    private(set) var startCount = 0

    func start(onWake: @escaping () -> Void) {
        startCount += 1
        self.onWake = onWake
    }

    func stop() {
        onWake = nil
    }

    /// The Mac woke.
    func wake() {
        onWake?()
    }
}

/// A grid that counts the monitor's runs and hands each one to a real store,  so a test sees
/// what the store itself reports.
final class CountingStoreGrid: DriftGridReading {
    let store: FleetStore
    /// How many times the monitor asked for a run.
    private(set) var refreshCount = 0

    init(store: FleetStore) {
        self.store = store
    }

    var cells: [GridCell] { store.cells }
    var manifest: SeatManifest? { store.manifest }
    var isRunning: Bool { store.isRunning }
    var runStartedAt: Date? { store.runStartedAt }
    var lastLandedRunStartedAt: Date? { store.lastLandedRunStartedAt }
    var complianceResults: [CheckResult] { store.complianceResults }
    var onRunFinished: ((Bool) -> Void)? {
        get { store.onRunFinished }
        set { store.onRunFinished = newValue }
    }

    func refresh() {
        refreshCount += 1
        store.refresh()
    }
}

// MARK: - GRID-18:  each compliance rule's real state

final class ComplianceStatusTests: XCTestCase {
    private let secrets = "compliance-secrets-hygiene"
    private let cert = "compliance-apple-cert-expiry"
    private let litestream = "compliance-litestream-heartbeat"

    func test_a_failing_result_is_the_rules_state_and_never_passing() throws {
        let statuses = ComplianceStatusBuilder.build(
            rules: [complianceRule(secrets)],
            results: [complianceResult(secrets, state: "DRIFT", reason: "3 findings, more than the 0 allowed")],
            notImplemented: [],
            answered: true
        )
        let status = try XCTUnwrap(statuses.first)
        XCTAssertEqual(status.standing, .answered(.drift))
        XCTAssertTrue(status.isFailing)
        XCTAssertFalse(status.isPassing)
        XCTAssertEqual(status.word, "DRIFT")
        XCTAssertEqual(status.sentence, "3 findings, more than the 0 allowed")
        XCTAssertEqual(status.placeWords, FleetHost.mac.onTitle)
    }

    func test_a_passing_result_reads_as_passing() throws {
        let status = try XCTUnwrap(ComplianceStatusBuilder.build(
            rules: [complianceRule(cert)],
            results: [complianceResult(cert, state: "PASS")],
            notImplemented: [],
            answered: true
        ).first)
        XCTAssertEqual(status.standing, .answered(.passed))
        XCTAssertTrue(status.isPassing)
        XCTAssertFalse(status.isFailing)
        XCTAssertEqual(status.word, CellState.passed.gridWord)
    }

    /// A rule that was never run or is not built never reads as passing (COPY-58).
    func test_a_rule_with_no_result_never_reads_as_passing() {
        let statuses = ComplianceStatusBuilder.build(
            rules: [
                complianceRule(secrets),
                complianceRule(litestream, scope: ["hetzner"], implemented: false, reason: "the check lands when the command does"),
                complianceRule(cert, implemented: nil),
            ],
            results: [],
            notImplemented: [],
            answered: true
        )
        XCTAssertEqual(statuses.map(\.ruleID), [secrets, litestream, cert], "manifest order")
        XCTAssertEqual(statuses[0].standing, .notInAnswer)
        XCTAssertEqual(statuses[0].word, ComplianceRuleStatus.noResultWord)
        XCTAssertEqual(statuses[1].standing, .notBuilt(reason: "the check lands when the command does"))
        XCTAssertEqual(statuses[1].word, ComplianceRuleStatus.notBuiltWord)
        XCTAssertTrue(statuses[1].sentence.hasSuffix("the check lands when the command does"))
        XCTAssertEqual(statuses[1].placeWords, FleetHost.server.onTitle)
        // `implemented` absent is not a pass either.
        XCTAssertEqual(statuses[2].standing, .notInAnswer)
        XCTAssertFalse(statuses.contains { $0.isPassing })
        XCTAssertFalse(statuses.contains { $0.isFailing })
    }

    func test_before_an_answer_every_built_rule_is_not_answered() {
        let statuses = ComplianceStatusBuilder.build(
            rules: [complianceRule(secrets)],
            // Results that are not an answer (a failed run's leftovers) are not read.
            results: [complianceResult(secrets, state: "PASS")],
            notImplemented: [],
            answered: false
        )
        XCTAssertEqual(statuses.first?.standing, .notAnswered)
        XCTAssertEqual(statuses.first?.word, ComplianceRuleStatus.notCheckedWord)
        XCTAssertFalse(statuses.first?.isPassing ?? true)
    }

    func test_the_engines_not_implemented_list_reads_as_not_built() {
        let statuses = ComplianceStatusBuilder.build(
            rules: [complianceRule(litestream, implemented: nil)],
            results: [],
            notImplemented: [NotImplementedRule(ruleId: litestream, ownerSeat: "CLAUDE", reason: "later")],
            answered: true
        )
        XCTAssertEqual(statuses.first?.standing, .notBuilt(reason: "later"))
    }

    /// One result per scope:  the worst one is the rule's state.
    func test_a_rule_that_runs_on_two_places_reads_as_the_worse() throws {
        let status = try XCTUnwrap(ComplianceStatusBuilder.build(
            rules: [complianceRule(secrets, scope: ["mac", "hetzner"])],
            results: [
                complianceResult(secrets, place: "mac", state: "PASS"),
                complianceResult(secrets, place: "hetzner", state: "MISSING"),
            ],
            notImplemented: [],
            answered: true
        ).first)
        XCTAssertEqual(status.standing, .answered(.missing))
        XCTAssertEqual(status.results.map(\.platform), ["hetzner", "mac"], "worst first")
        XCTAssertTrue(status.isFailing)
    }

    /// A result for a rule this copy of the manifest does not name is kept, not dropped.
    func test_a_result_the_manifest_does_not_name_is_kept() {
        let statuses = ComplianceStatusBuilder.build(
            rules: [],
            results: [complianceResult("compliance-new", place: "mac", state: "DRIFT")],
            notImplemented: [],
            answered: true
        )
        XCTAssertEqual(statuses.map(\.ruleID), ["compliance-new"])
        XCTAssertEqual(statuses.first?.scope, ["mac"])
        XCTAssertTrue(statuses.first?.isFailing ?? false)
    }

    /// An UNKNOWN the engine did not flag as failing still never reads as passing, and the
    /// grid's red states count as failing.
    func test_an_unflagged_unknown_counts_as_failing() {
        let unknown = complianceResult(secrets, state: "UNKNOWN", failing: false)
        XCTAssertTrue(unknown.failsCompliance)
        XCTAssertFalse(complianceResult(secrets, state: "PASS").failsCompliance)
        XCTAssertTrue(complianceResult(secrets, state: "DRIFT").failsCompliance)
    }
}

// MARK: - GRID-18:  the store keeps the results

final class ComplianceStoreTests: XCTestCase {
    /// An engine the test answers by hand.
    private final class HandEngine {
        private var pending: [(EngineRun) -> Void] = []

        func run(_ location: EngineLocation, _ completion: @escaping (EngineRun) -> Void) {
            pending.append(completion)
        }

        func answer(_ run: EngineRun) {
            guard !pending.isEmpty else { return }
            pending.removeFirst()(run)
        }
    }

    /// The fixture manifest with three compliance rules:  two built,  one not.
    private var manifest: String {
        Fixtures.manifest.replacingOccurrences(
            of: "\"schemaVersion\": 2,",
            with: """
            "schemaVersion": 2,
             "complianceRules": [
              {"id": "compliance-secrets-hygiene", "ownerSeat": "CLAUDE", "scope": ["mac"], "implemented": true},
              {"id": "compliance-apple-cert-expiry", "ownerSeat": "CLAUDE", "scope": ["mac"], "implemented": true},
              {"id": "compliance-litestream-heartbeat", "ownerSeat": "CLAUDE", "scope": ["hetzner"], "implemented": false,
               "implementationReason": "the check lands when the command does"}
             ],
            """
        )
    }

    /// The fixture envelope with a failing secrets result and the engine's not-built list.
    private var envelope: String {
        Fixtures.envelope.replacingOccurrences(
            of: "\"results\": [",
            with: """
            "notImplemented": [{"ruleId": "compliance-litestream-heartbeat", "ownerSeat": "CLAUDE", "reason": "later"}],
             "results": [
              {
               "ruleId": "compliance-secrets-hygiene", "seat": "", "platform": "mac", "kind": "compliance",
               "state": "DRIFT", "reason": "3 findings, more than the 0 allowed", "reasonCode": "drift",
               "expected": {}, "observed": {}, "proofLevelRequired": null, "proofLevelAchieved": null,
               "collector": "mac", "checkType": "command", "checkedAt": "2026-10-19T14:30:00Z",
               "expiresAt": null, "ttlSeconds": 900, "stale": false, "failing": true
              },
            """
        )
    }

    private func makeStore(engine: HandEngine) throws -> (FleetStore, URL) {
        let root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-compliance-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try manifest.write(to: root.appendingPathComponent("fleet-seats.json"), atomically: true, encoding: .utf8)
        try Fixtures.registry.write(to: root.appendingPathComponent("fleet-apps.json"), atomically: true, encoding: .utf8)
        let store = FleetStore(
            location: EngineLocation(root: root, interpreter: URL(fileURLWithPath: "/usr/bin/python3")),
            runEngine: { location, completion in engine.run(location, completion) },
            refreshOnStart: false
        )
        return (store, root)
    }

    private func land(_ run: EngineRun, on store: FleetStore, engine: HandEngine) {
        let landed = expectation(description: "the run landed")
        store.onRunFinished = { _ in landed.fulfill() }
        engine.answer(run)
        wait(for: [landed], timeout: 5)
        store.onRunFinished = nil
    }

    func test_the_store_keeps_each_compliance_rules_real_state() throws {
        let engine = HandEngine()
        let (store, root) = try makeStore(engine: engine)
        defer { try? FileManager.default.removeItem(at: root) }
        XCTAssertEqual(store.manifest?.allComplianceRules.count, 3)

        // Before any answer nothing is passing.
        XCTAssertTrue(store.complianceResults.isEmpty)
        XCTAssertEqual(store.complianceStatus(ruleID: "compliance-secrets-hygiene")?.standing, .notAnswered)
        XCTAssertFalse(store.complianceStatuses.contains { $0.isPassing })

        store.refresh()
        XCTAssertNotNil(store.runStartedAt, "a run in flight says when it started")
        let decoded = try EnvelopeDecoder.decode(Data(envelope.utf8))
        land(
            EngineRun(envelope: decoded, exitCode: 0, standardError: "", failure: nil, timedOut: false),
            on: store,
            engine: engine
        )
        XCTAssertNil(store.runStartedAt)

        XCTAssertEqual(store.complianceResults.map(\.ruleId), ["compliance-secrets-hygiene"])
        XCTAssertEqual(
            store.complianceStatuses.map(\.ruleID),
            ["compliance-secrets-hygiene", "compliance-apple-cert-expiry", "compliance-litestream-heartbeat"]
        )
        let secrets = try XCTUnwrap(store.complianceStatus(ruleID: "compliance-secrets-hygiene"))
        XCTAssertEqual(secrets.standing, .answered(.drift))
        XCTAssertTrue(secrets.isFailing)
        XCTAssertEqual(store.complianceStatus(ruleID: "compliance-apple-cert-expiry")?.standing, .notInAnswer)
        XCTAssertEqual(
            store.complianceStatus(ruleID: "compliance-litestream-heartbeat")?.word,
            ComplianceRuleStatus.notBuiltWord
        )
        XCTAssertNil(store.complianceStatus(ruleID: "compliance-nothing"))
        // The seat cells are untouched by the compliance results.
        XCTAssertEqual(store.cell(seat: "CLAUDE", kind: .hooks)?.state, .passed)
        XCTAssertEqual(store.complianceResults, (store as DriftGridReading).complianceResults)

        // A failed run clears the answer, and nothing reads as passing or failing.
        store.refresh()
        land(
            EngineRun(envelope: nil, exitCode: 1, standardError: "", failure: "the engine did not answer", timedOut: false),
            on: store,
            engine: engine
        )
        XCTAssertTrue(store.complianceResults.isEmpty)
        XCTAssertEqual(store.complianceStatus(ruleID: "compliance-secrets-hygiene")?.standing, .notAnswered)
        XCTAssertEqual(store.complianceStatus(ruleID: "compliance-litestream-heartbeat")?.word, ComplianceRuleStatus.notBuiltWord)
    }

    /// The statuses are built once per answer:  a list that looks each rule up row by row does
    /// not build them again for every row,  and the next answer builds them afresh.
    func test_the_statuses_are_built_once_per_answer() throws {
        let engine = HandEngine()
        let (store, root) = try makeStore(engine: engine)
        defer { try? FileManager.default.removeItem(at: root) }

        store.refresh()
        let decoded = try EnvelopeDecoder.decode(Data(envelope.utf8))
        land(
            EngineRun(envelope: decoded, exitCode: 0, standardError: "", failure: nil, timedOut: false),
            on: store,
            engine: engine
        )
        let ruleIDs = store.complianceStatuses.map(\.ruleID)
        XCTAssertEqual(ruleIDs.count, 3)
        let built = store.complianceBuildCount
        for ruleID in ruleIDs {
            XCTAssertEqual(store.complianceStatus(ruleID: ruleID)?.ruleID, ruleID)
            XCTAssertEqual(store.complianceStatuses.map(\.ruleID), ruleIDs)
        }
        XCTAssertNil(store.complianceStatus(ruleID: "compliance-nothing"))
        XCTAssertEqual(store.complianceBuildCount, built, "a lookup built the statuses again")
        XCTAssertEqual(store.complianceStatus(ruleID: "compliance-secrets-hygiene")?.standing, .answered(.drift))

        // A failed run is a new answer,  and the statuses follow it.
        store.refresh()
        land(
            EngineRun(envelope: nil, exitCode: 1, standardError: "", failure: "the engine did not answer", timedOut: false),
            on: store,
            engine: engine
        )
        XCTAssertEqual(store.complianceStatus(ruleID: "compliance-secrets-hygiene")?.standing, .notAnswered)
        XCTAssertEqual(store.complianceStatuses.map(\.ruleID), ruleIDs)
        XCTAssertEqual(store.complianceBuildCount, built + 1)
    }
}

// MARK: - GRID-18:  a failing compliance result goes through the drift alert path

final class ComplianceAlertFinderTests: XCTestCase {
    private let rule = "compliance-secrets-hygiene"

    func test_a_result_that_starts_failing_is_a_turn_and_one_that_stays_is_not() {
        let passing = [complianceResult(rule, state: "PASS")]
        let failing = [complianceResult(rule, state: "DRIFT")]
        let first = ComplianceAlertFinder.transitions(
            previous: ComplianceAlertFinder.marks(of: passing),
            current: failing
        )
        XCTAssertEqual(first.count, 1)
        XCTAssertEqual(first.first?.kind, CheckResult.complianceKind)
        XCTAssertEqual(first.first?.ruleID, rule)
        XCTAssertEqual(first.first?.seat, FleetHost.mac.onTitle)
        XCTAssertEqual(first.first?.fromState, "PASS")
        XCTAssertEqual(first.first?.toState, "DRIFT")
        XCTAssertEqual(first.first?.kindTitle, "Compliance")
        XCTAssertTrue(first.first?.isCompliance ?? false)

        let again = ComplianceAlertFinder.transitions(
            previous: ComplianceAlertFinder.marks(of: failing),
            current: failing
        )
        XCTAssertTrue(again.isEmpty, "a result that stays failing says nothing again")
    }

    /// A result that was not there before counts as not failing, and each place is its own.
    func test_a_new_result_and_a_second_place_are_turns_of_their_own() {
        let before = ComplianceAlertFinder.marks(of: [complianceResult(rule, place: "mac", state: "DRIFT")])
        let found = ComplianceAlertFinder.transitions(
            previous: before,
            current: [
                complianceResult(rule, place: "mac", state: "DRIFT"),
                complianceResult(rule, place: "hetzner", state: "MISSING"),
                complianceResult("compliance-apple-cert-expiry", place: "mac", state: "DRIFT"),
            ]
        )
        XCTAssertEqual(found.map { "\($0.ruleID ?? "")@\($0.seat)" }, [
            "\(rule)@\(FleetHost.server.onTitle)",
            "compliance-apple-cert-expiry@\(FleetHost.mac.onTitle)",
        ])
        XCTAssertNil(found.first?.fromState)
    }

    func test_seat_results_are_not_read_as_compliance() {
        let seatResult = CheckResult(
            ruleId: "claude-hooks", seat: "CLAUDE", platform: "claude", kind: "hooks",
            state: "MISSING", reason: "", reasonCode: "missing", expected: [:], observed: [:],
            proofLevelRequired: nil, proofLevelAchieved: nil, collector: "mac", checkType: "hook_registered",
            checkedAt: "2026-11-07T15:00:00Z", expiresAt: nil, ttlSeconds: 900, stale: false, failing: true
        )
        XCTAssertTrue(ComplianceAlertFinder.marks(of: [seatResult]).isEmpty)
        XCTAssertTrue(ComplianceAlertFinder.failures(in: [seatResult]).isEmpty)
        XCTAssertTrue(ComplianceAlertFinder.transitions(previous: [:], current: [seatResult]).isEmpty)
    }
}

@MainActor
final class ComplianceAlertMonitorTests: XCTestCase {
    private var home: URL!

    override func setUpWithError() throws {
        home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-compliance-monitor-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    private func makeMonitor(grid: FakeGrid, notifier: DriftRecordingNotifier) -> DriftMonitor {
        DriftMonitor(
            grid: grid,
            notifier: notifier,
            history: DriftHistoryStore(url: home.appendingPathComponent("drift-history.json")),
            watcher: FakeWatcher(),
            scheduler: ManualScheduler(),
            ticker: ManualTicker(),
            wake: ManualWake(),
            home: home
        )
    }

    private func land(_ grid: FakeGrid, _ results: [CheckResult], trusted: Bool = true) async {
        grid.complianceResults = results
        grid.finish(cells: [], trusted: trusted)
        await settleGridModel()
    }

    func test_a_compliance_result_that_starts_failing_is_one_notice_and_one_record() async throws {
        let rule = "compliance-secrets-hygiene"
        let grid = FakeGrid()
        let notifier = DriftRecordingNotifier()
        let monitor = makeMonitor(grid: grid, notifier: notifier)
        monitor.start()

        // The first trusted run is a reading, even with a failure in it.
        await land(grid, [complianceResult(rule, state: "PASS")])
        XCTAssertTrue(monitor.transitions.isEmpty)
        XCTAssertTrue(monitor.complianceFailures.isEmpty)

        // DRIFT is not a red cell, but it is a failing compliance result.
        await land(grid, [complianceResult(rule, state: "DRIFT", reason: "3 findings")])
        XCTAssertEqual(monitor.transitions.count, 1)
        XCTAssertEqual(monitor.transitions.first?.kind, CheckResult.complianceKind)
        XCTAssertEqual(monitor.transitions.first?.ruleID, rule)
        XCTAssertEqual(monitor.complianceFailures.map(\.ruleID), [rule])
        XCTAssertEqual(monitor.complianceFailures.first?.placeTitle, FleetHost.mac.onTitle)
        XCTAssertEqual(monitor.redCount, 0, "a compliance result is not a red cell")
        XCTAssertEqual(notifier.posts.count, 1)
        XCTAssertEqual(notifier.posts.first?.title, "Compliance Alert")
        XCTAssertTrue(notifier.bodies.first?.contains(rule) ?? false, notifier.bodies.first ?? "")
        XCTAssertEqual(DriftHistoryStore(url: home.appendingPathComponent("drift-history.json")).load().count, 1)

        // It stays failing:  nothing new.
        await land(grid, [complianceResult(rule, state: "DRIFT")])
        XCTAssertEqual(monitor.transitions.count, 1)
        XCTAssertEqual(notifier.posts.count, 1)

        // A run that cannot be trusted says nothing, and keeps the baseline.
        await land(grid, [], trusted: false)
        XCTAssertTrue(monitor.complianceFailures.isEmpty)
        await land(grid, [complianceResult(rule, state: "DRIFT")])
        XCTAssertEqual(monitor.transitions.count, 1, "it was already failing before the failed run")

        // It recovers and fails again:  a new event.
        await land(grid, [complianceResult(rule, state: "PASS")])
        await land(grid, [complianceResult(rule, state: "MISSING")])
        XCTAssertEqual(monitor.transitions.count, 2)
        XCTAssertEqual(notifier.posts.count, 2)
    }

    func test_the_notice_words_for_a_compliance_turn() {
        let transition = DriftTransition(
            id: UUID(), at: Date(), seat: FleetHost.mac.onTitle, kind: CheckResult.complianceKind,
            ruleID: "compliance-apple-cert-expiry", fromState: "PASS", toState: "DRIFT"
        )
        let words = DriftMonitor.noticeWords(for: transition)
        XCTAssertEqual(words.title, "Compliance Alert")
        XCTAssertEqual(words.body, "\(FleetHost.mac.onTitle) / Compliance:  compliance-apple-cert-expiry failed (PASS to DRIFT).")
    }
}

// MARK: - GRID-37:  the streams that started,  apart from the paths asked for

final class DriftWatchCoverageTests: XCTestCase {
    private let present = URL(fileURLWithPath: "/tmp/muster-coverage/present.json")
    private let absent = URL(fileURLWithPath: "/tmp/muster-coverage/later/absent.json")
    private let ancestor = URL(fileURLWithPath: "/tmp/muster-coverage")

    private var list: DriftWatchList {
        DriftWatchList(
            watches: [
                DriftWatchList.Watch(root: present, target: present, targetIsDirectory: false, rootIsTheTarget: true),
                DriftWatchList.Watch(root: absent, target: absent, targetIsDirectory: false, rootIsTheTarget: true),
                DriftWatchList.Watch(root: ancestor, target: absent, targetIsDirectory: false, rootIsTheTarget: false),
            ],
            targetPaths: [present, absent],
            seats: ["MM"]
        )
    }

    func test_only_started_streams_count_as_watching() {
        let partial = DriftWatchCoverage.of(list: list, started: [list.watches[0], list.watches[2]])
        XCTAssertEqual(partial.pathsAsked, 2)
        XCTAssertEqual(partial.pathsWatched, 2, "the folder above a missing path stands for it")
        XCTAssertEqual(partial.streamsAsked, 3)
        XCTAssertEqual(partial.streamsStarted, 2)
        XCTAssertEqual(partial.streamsFailed, 1)
        XCTAssertFalse(partial.isComplete)
        XCTAssertTrue(partial.unwatchedPaths.isEmpty)

        let none = DriftWatchCoverage.of(list: list, started: [])
        XCTAssertEqual(none.pathsWatched, 0)
        XCTAssertEqual(none.streamsStarted, 0)
        XCTAssertEqual(none.unwatchedPaths, [present, absent])

        let full = DriftWatchCoverage.of(list: list, started: list.watches)
        XCTAssertTrue(full.isComplete)
        XCTAssertEqual(full.pathsWatched, 2)
    }

    /// A stream whose `FSEventStreamStart` fails is let go and never counted as watching.
    func test_a_stream_that_does_not_start_is_not_armed() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-start-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let watch = DriftWatchList.Watch(
            root: directory, target: directory, targetIsDirectory: true, rootIsTheTarget: true
        )
        var starts = 0
        let watcher = FSEventsDriftWatcher(startStream: { _ in
            starts += 1
            return false
        })
        let armed = watcher.arm([watch]) { _ in }
        XCTAssertEqual(starts, 1, "the stream was asked to start")
        XCTAssertTrue(armed.isEmpty, "a stream that did not start was counted as armed")
        XCTAssertTrue(watcher.startedWatches.isEmpty)
        XCTAssertEqual(watcher.startedStreamCount, 0)
        watcher.stop()
    }

    func test_the_watch_line_counts_watched_paths_and_warns_on_a_failed_start() {
        let full = GridPresentation.WatchLine.make(
            isWatching: true,
            coverage: DriftWatchCoverage.of(list: list, started: list.watches),
            seatCount: 1,
            recheckSeconds: 600
        )
        XCTAssertEqual(full.tone, .watching)
        XCTAssertEqual(
            full.text,
            "Watching 2 paths on the Mac for 1 seat.\u{00A0} Secret store checks cannot be watched, so they are checked again at least every 10 minutes."
        )

        let partial = GridPresentation.WatchLine.make(
            isWatching: true,
            coverage: DriftWatchCoverage.of(list: list, started: [list.watches[0]]),
            seatCount: 1,
            recheckSeconds: 600
        )
        XCTAssertEqual(partial.tone, .warning)
        XCTAssertEqual(
            partial.text,
            "Watching 1 path of 2 on the Mac for 1 seat.\u{00A0} 2 watches could not be started, so a change to 1 path is seen only at the check every 10 minutes."
        )

        let none = GridPresentation.WatchLine.make(
            isWatching: true,
            coverage: DriftWatchCoverage.of(list: list, started: []),
            seatCount: 1,
            recheckSeconds: 600
        )
        XCTAssertEqual(none.tone, .warning)
        XCTAssertTrue(none.text.hasPrefix("Watching nothing:  none of the 3 watches"), none.text)
    }
}

@MainActor
final class DriftWatchCoverageMonitorTests: XCTestCase {
    func test_the_monitor_reports_started_streams_apart_from_paths_asked_for() async throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-coverage-monitor-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let healthy = URL(fileURLWithPath: "/tmp/muster-coverage-monitor/healthy.json")
        let broken = URL(fileURLWithPath: "/tmp/muster-coverage-monitor/broken.json")
        let grid = FakeGrid()
        let watcher = FakeWatcher()
        watcher.failingRoots = [broken]
        let monitor = DriftMonitor(
            grid: grid,
            notifier: DriftRecordingNotifier(),
            history: DriftHistoryStore(url: home.appendingPathComponent("drift-history.json")),
            watcher: watcher,
            scheduler: ManualScheduler(),
            ticker: ManualTicker(),
            wake: ManualWake(),
            home: home,
            buildWatchList: { _, _ in
                DriftWatchList(
                    watches: [
                        DriftWatchList.Watch(root: healthy, target: healthy, targetIsDirectory: false, rootIsTheTarget: true),
                        DriftWatchList.Watch(root: broken, target: broken, targetIsDirectory: false, rootIsTheTarget: true),
                    ],
                    targetPaths: [healthy, broken],
                    seats: ["MM"]
                )
            }
        )
        XCTAssertEqual(monitor.watchCoverage, DriftWatchCoverage.empty)
        monitor.start()
        XCTAssertTrue(monitor.isWatching)
        XCTAssertEqual(monitor.watchedPathCount, 2, "the count asked for")
        XCTAssertEqual(monitor.watchCoverage.pathsAsked, 2)
        XCTAssertEqual(monitor.watchCoverage.pathsWatched, 1)
        XCTAssertEqual(monitor.watchCoverage.streamsStarted, 1)
        XCTAssertEqual(monitor.watchCoverage.streamsFailed, 1)
        XCTAssertEqual(monitor.watchCoverage.unwatchedPaths, [broken])

        // The broken path can be watched now:  the next run arms it and the count follows.
        watcher.failingRoots = []
        grid.finish(cells: [], trusted: true)
        await settleGridModel()
        XCTAssertTrue(monitor.watchCoverage.isComplete)
        XCTAssertEqual(monitor.watchCoverage.pathsWatched, 2)

        monitor.stop()
        XCTAssertEqual(monitor.watchCoverage, DriftWatchCoverage.empty)
    }
}

// MARK: - GRID-33:  a wall-clock safety net,  and a run on wake

final class SafetyNetDueTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    func test_the_net_is_due_only_when_the_last_trusted_run_is_an_interval_old() {
        let interval = DriftMonitor.safetyNetInterval
        XCTAssertTrue(DriftMonitor.safetyNetIsDue(now: now, lastTrustedRunAt: nil, lastFailedRunAt: nil))
        XCTAssertFalse(DriftMonitor.safetyNetIsDue(
            now: now, lastTrustedRunAt: now.addingTimeInterval(-interval + 1), lastFailedRunAt: nil))
        XCTAssertTrue(DriftMonitor.safetyNetIsDue(
            now: now, lastTrustedRunAt: now.addingTimeInterval(-interval), lastFailedRunAt: nil))
        // A failed run holds the net for an interval, so a broken engine is not run every minute.
        XCTAssertFalse(DriftMonitor.safetyNetIsDue(
            now: now, lastTrustedRunAt: now.addingTimeInterval(-3 * interval),
            lastFailedRunAt: now.addingTimeInterval(-120)))
        // A sleep counts:  the wall clock moved on while the Mac slept.
        XCTAssertTrue(DriftMonitor.safetyNetIsDue(
            now: now, lastTrustedRunAt: now.addingTimeInterval(-8 * 3600), lastFailedRunAt: nil))
        // A clock set back is not trusted to hold the net forever.
        XCTAssertTrue(DriftMonitor.safetyNetIsDue(
            now: now, lastTrustedRunAt: now.addingTimeInterval(3600), lastFailedRunAt: nil))
    }

    func test_the_ticker_leeway_is_a_quarter_of_its_interval_at_most_a_minute() {
        XCTAssertEqual(DispatchDriftTicker.leewaySeconds(for: DriftMonitor.safetyNetCheckInterval), 15)
        XCTAssertEqual(DispatchDriftTicker.leewaySeconds(for: 600), 60)
        XCTAssertEqual(DispatchDriftTicker.leewaySeconds(for: 1), 1)
    }
}

@MainActor
final class SafetyNetAndWakeTests: XCTestCase {
    private var home: URL!
    private var grid: FakeGrid!
    private var scheduler: ManualScheduler!
    private var ticker: ManualTicker!
    private var wake: ManualWake!
    private var clock: DriftTestClock!

    override func setUpWithError() throws {
        home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-net-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        grid = FakeGrid()
        scheduler = ManualScheduler()
        ticker = ManualTicker()
        wake = ManualWake()
        clock = DriftTestClock()
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    private func makeMonitor(grid other: DriftGridReading? = nil, watcher: FakeWatcher? = nil) -> DriftMonitor {
        let clock = self.clock!
        return DriftMonitor(
            grid: other ?? grid,
            notifier: DriftRecordingNotifier(),
            history: DriftHistoryStore(url: home.appendingPathComponent("drift-history.json")),
            watcher: watcher ?? FakeWatcher(),
            scheduler: scheduler,
            ticker: ticker,
            wake: wake,
            home: home,
            clock: { clock.now }
        )
    }

    /// The safety net looks once a minute and runs nothing while a run is in flight,  and it
    /// asks for no run behind it:  that run lands and is the net's run.
    func test_the_net_never_starts_a_second_run_while_one_is_in_flight() async throws {
        let monitor = makeMonitor()
        monitor.start()
        XCTAssertEqual(ticker.interval, DriftMonitor.safetyNetCheckInterval)
        grid.isRunning = true
        clock.advance(by: 60)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0)
        grid.isRunning = false
        grid.finish(cells: [], trusted: true)
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "the net queued a run behind the run in flight")
        XCTAssertEqual(monitor.lastTrustedRunAt, clock.now)
    }

    /// A run that failed holds the net for a whole interval,  then the net tries again.
    func test_the_net_waits_an_interval_after_a_failed_run() async throws {
        let monitor = makeMonitor()
        monitor.start()
        grid.finish(cells: [], trusted: false)
        await settleGridModel()
        for _ in 0..<9 {
            clock.advance(by: 60)
            ticker.fire()
            await settleGridModel()
        }
        XCTAssertEqual(grid.refreshCount, 0)
        clock.advance(by: 60)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// The Mac woke:  the engine runs once the network has had a moment,  not at once.
    func test_a_wake_runs_the_engine_after_the_network_delay() async throws {
        let monitor = makeMonitor()
        monitor.start()
        XCTAssertEqual(wake.startCount, 1)
        grid.finish(cells: [], trusted: true)
        await settleGridModel()

        clock.advance(by: 8 * 3600)
        wake.wake()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "the run waits for the network")
        XCTAssertEqual(scheduler.liveDelays, [DriftMonitor.wakeRunDelay])

        // The net looks inside the hold and leaves the wake's run alone.
        clock.advance(by: 5)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0)

        clock.advance(by: 5)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// The look after a long sleep can come before the wake notice:  it is read as the wake,
    /// so the run still waits for the network,  and the notice that follows does not add a
    /// second run.
    func test_a_long_gap_between_looks_is_read_as_a_wake() async throws {
        let monitor = makeMonitor()
        monitor.start()
        grid.finish(cells: [], trusted: true)
        await settleGridModel()
        clock.advance(by: 60)
        ticker.fire()
        await settleGridModel()

        clock.advance(by: 8 * 3600)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "the net ran at once after a sleep, before the network was back")
        wake.wake()
        await settleGridModel()
        XCTAssertEqual(scheduler.liveDelays, [DriftMonitor.wakeRunDelay], "one waiting run, not two")

        clock.advance(by: DriftMonitor.wakeRunDelay)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// A sleep of a few minutes is a gap of a few look intervals,  far short of the safety
    /// net's ten minutes.  The timer can fire before the wake notice,  and when the net is due
    /// then,  its run still waits for the network.
    func test_a_short_sleep_between_looks_is_read_as_a_wake() async throws {
        let monitor = makeMonitor()
        monitor.start()
        grid.finish(cells: [], trusted: true, startedAt: clock.now)
        await settleGridModel()
        for _ in 0..<9 {
            clock.advance(by: 60)
            ticker.fire()
            await settleGridModel()
        }
        XCTAssertEqual(grid.refreshCount, 0)

        // A three-minute sleep:  the last trusted run is now twelve minutes old.
        clock.advance(by: 180)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "the net ran at once after a short sleep, before the network was back")
        XCTAssertEqual(scheduler.liveDelays, [DriftMonitor.wakeRunDelay])

        clock.advance(by: DriftMonitor.wakeRunDelay)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// A look that came late while the net is not due adds nothing:  the system may hold a
    /// timer back,  and a held-back look must never cost a run of its own.
    func test_a_late_look_when_the_net_is_not_due_adds_no_run() async throws {
        let monitor = makeMonitor()
        monitor.start()
        grid.finish(cells: [], trusted: true, startedAt: clock.now)
        await settleGridModel()
        clock.advance(by: 60)
        ticker.fire()
        await settleGridModel()

        clock.advance(by: 180)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0)
        XCTAssertEqual(scheduler.liveCount, 0, "a late look asked for a run the net did not need")

        // The net still runs once the trusted run is an interval old.
        for _ in 0..<6 {
            clock.advance(by: 60)
            ticker.fire()
            await settleGridModel()
        }
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// A run in flight that started once the hold after the wake was over is the run the wake
    /// asks for.
    func test_a_wake_does_not_add_a_run_to_one_that_started_after_it() async throws {
        let monitor = makeMonitor()
        monitor.start()
        wake.wake()
        await settleGridModel()
        grid.isRunning = true
        grid.runStartedAt = clock.now.addingTimeInterval(DriftMonitor.wakeRunDelay)
        clock.advance(by: DriftMonitor.wakeRunDelay)
        scheduler.fireAll()
        await settleGridModel()
        grid.isRunning = false
        grid.runStartedAt = nil
        grid.finish(cells: [], trusted: true)
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0)
        withExtendedLifetime(monitor) {}
    }

    /// A run in flight from before the sleep read the Mac as it was then,  so one more run
    /// follows it,  after it lands and never beside it.
    func test_a_wake_queues_one_run_behind_a_run_from_before_the_sleep() async throws {
        let monitor = makeMonitor()
        monitor.start()
        grid.isRunning = true
        grid.runStartedAt = clock.now
        clock.advance(by: 3600)
        wake.wake()
        await settleGridModel()
        clock.advance(by: DriftMonitor.wakeRunDelay)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "a second run started beside the one in flight")
        let started = try XCTUnwrap(grid.runStartedAt)
        grid.isRunning = false
        grid.runStartedAt = nil
        grid.finish(cells: [], trusted: true, startedAt: started)
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// A trusted run that started once the hold after the wake was over already covers it.
    func test_a_wake_is_covered_by_a_trusted_run_that_started_after_the_hold() async throws {
        let monitor = makeMonitor()
        monitor.start()
        wake.wake()
        await settleGridModel()
        clock.advance(by: DriftMonitor.wakeRunDelay)
        let started = clock.now
        clock.advance(by: 2)
        grid.finish(cells: [], trusted: true, startedAt: started)
        await settleGridModel()
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0)
        withExtendedLifetime(monitor) {}
    }

    /// A trusted run that started inside the hold after a wake,  such as one the owner asked
    /// for,  may have read the secret store before the network was back.  It lands after the
    /// wake and still never covers it:  the wake's run starts when the hold ends.
    func test_a_run_that_started_inside_the_hold_does_not_cover_the_wake() async throws {
        let monitor = makeMonitor()
        monitor.start()
        wake.wake()
        await settleGridModel()
        clock.advance(by: 3)
        let started = clock.now
        clock.advance(by: 2)
        grid.finish(cells: [], trusted: true, startedAt: started)
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "the wake's run still waits for the network")

        clock.advance(by: DriftMonitor.wakeRunDelay - 5)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1, "a run from inside the hold cancelled the wake's run")
        withExtendedLifetime(monitor) {}
    }

    /// A run still in flight when the hold ends,  which started inside it,  gets one more run
    /// behind it,  after it lands and never beside it.
    func test_a_run_in_flight_that_started_inside_the_hold_gets_one_run_behind_it() async throws {
        let monitor = makeMonitor()
        monitor.start()
        wake.wake()
        await settleGridModel()
        clock.advance(by: 4)
        let started = clock.now
        grid.isRunning = true
        grid.runStartedAt = started
        clock.advance(by: DriftMonitor.wakeRunDelay - 4)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "a second run started beside the one in flight")
        grid.isRunning = false
        grid.runStartedAt = nil
        grid.finish(cells: [], trusted: true, startedAt: started)
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1, "no run followed the run from inside the hold")
        withExtendedLifetime(monitor) {}
    }

    /// A watched change inside the hold after a wake starts no run before the network is back:
    /// the wake's run at the end of the hold reads it.
    func test_a_watched_change_inside_the_hold_waits_for_the_wakes_run() async throws {
        let watcher = FakeWatcher()
        let monitor = makeMonitor(watcher: watcher)
        monitor.start()
        wake.wake()
        await settleGridModel()
        clock.advance(by: 3)
        watcher.fire()
        await settleGridModel()
        scheduler.fireDebounce()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "a watched change started a run inside the hold")
        XCTAssertEqual(scheduler.liveDelays, [DriftMonitor.wakeRunDelay], "only the wake's run waits")

        clock.advance(by: DriftMonitor.wakeRunDelay - 3)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1, "the wake's run did not start when the hold ended")
        withExtendedLifetime(monitor) {}
    }

    /// The run a watched change left waiting for the end of the floor waits out the hold too,
    /// when the floor ends inside it,  and the wake's run reads its change.
    func test_the_floors_waiting_run_inside_the_hold_waits_for_the_wakes_run() async throws {
        let watcher = FakeWatcher()
        let monitor = makeMonitor(watcher: watcher)
        monitor.start()
        watcher.fire()
        await settleGridModel()
        scheduler.fireDebounce()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        grid.finish(cells: [], trusted: true, startedAt: clock.now)
        await settleGridModel()

        // A second change inside the floor waits for its end.
        let floorWait = DriftMonitor.watcherRunFloor - 10
        clock.advance(by: 10)
        watcher.fire()
        await settleGridModel()
        scheduler.fireDebounce()
        await settleGridModel()
        XCTAssertEqual(scheduler.liveDelays, [floorWait])

        // The Mac wakes just before the floor ends,  so the floor ends inside the hold.
        clock.advance(by: floorWait - 2)
        wake.wake()
        await settleGridModel()
        clock.advance(by: 2)
        scheduler.fire { $0 == floorWait }
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1, "the floor's run started inside the hold")

        clock.advance(by: DriftMonitor.wakeRunDelay - 2)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 2, "the wake's run did not start when the hold ended")
        withExtendedLifetime(monitor) {}
    }

    /// A run that was in flight when the Mac slept and lands inside the wake's wait read the
    /// Mac as it was before the sleep,  and perhaps the secret store with the network down.
    /// It lands after the wake and still never covers it:  the wake's run follows.
    func test_a_run_from_before_the_sleep_that_lands_after_the_wake_does_not_cover_it() async throws {
        let monitor = makeMonitor()
        monitor.start()
        let started = clock.now
        grid.isRunning = true
        grid.runStartedAt = started
        clock.advance(by: 3600)
        wake.wake()
        await settleGridModel()

        clock.advance(by: 3)
        grid.isRunning = false
        grid.runStartedAt = nil
        grid.finish(cells: [], trusted: true, startedAt: started)
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0, "the wake's run still waits for the network")

        clock.advance(by: DriftMonitor.wakeRunDelay - 3)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1, "the run from before the sleep cancelled the wake's run")
        withExtendedLifetime(monitor) {}
    }

    /// A trusted run whose start the grid cannot say covers nothing.
    func test_a_run_with_no_start_time_does_not_cover_a_wake() async throws {
        let monitor = makeMonitor()
        monitor.start()
        wake.wake()
        await settleGridModel()
        clock.advance(by: 3)
        grid.finish(cells: [], trusted: true, startedAt: nil)
        await settleGridModel()
        clock.advance(by: DriftMonitor.wakeRunDelay)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 1)
        withExtendedLifetime(monitor) {}
    }

    /// A store with no engine to run reports a run that could not be trusted,  so the net waits
    /// a whole interval before it asks again,  rather than asking at every look.
    func test_a_store_with_no_engine_holds_the_net_for_an_interval() async throws {
        let store = FleetStore(
            location: nil,
            runEngine: { _, _ in XCTFail("there is no engine to run") },
            refreshOnStart: false
        )
        let counting = CountingStoreGrid(store: store)
        let monitor = makeMonitor(grid: counting)
        monitor.start()

        clock.advance(by: 60)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(counting.refreshCount, 1, "with no run at all the net is due")
        XCTAssertEqual(monitor.lastRunWasTrusted, false, "no engine is a run that could not be trusted")
        XCTAssertNil(monitor.redCount)

        for _ in 0..<9 {
            clock.advance(by: 60)
            ticker.fire()
            await settleGridModel()
        }
        XCTAssertEqual(counting.refreshCount, 1, "the net asked again inside the interval")

        clock.advance(by: 60)
        ticker.fire()
        await settleGridModel()
        XCTAssertEqual(counting.refreshCount, 2)
        withExtendedLifetime(monitor) {}
    }

    /// A stop takes back the run a wake left waiting.
    func test_a_stop_takes_back_the_waiting_wake_run() async throws {
        let monitor = makeMonitor()
        monitor.start()
        wake.wake()
        await settleGridModel()
        XCTAssertEqual(scheduler.liveCount, 1)
        monitor.stop()
        XCTAssertEqual(scheduler.liveCount, 0)
        scheduler.fireAll()
        await settleGridModel()
        XCTAssertEqual(grid.refreshCount, 0)
    }
}

// MARK: - Seat Setup spells each state the grid's one way

final class SeatSetupSpellingTests: XCTestCase {
    private func rule(_ state: String, reasonCode: String = "ok") -> CheckResult {
        CheckResult(
            ruleId: "claude-hooks-rule", seat: "CLAUDE", platform: "claude", kind: "hooks",
            state: state, reason: "because", reasonCode: reasonCode,
            expected: [:], observed: [:],
            proofLevelRequired: "L0", proofLevelAchieved: nil,
            collector: "mac", checkType: "hook_registered",
            checkedAt: "2026-11-07T15:00:00Z", expiresAt: nil, ttlSeconds: 900,
            stale: false, failing: false
        )
    }

    /// The summary line uses `gridWord`:  a covered cell reads DOES NOT APPLY, never COVERED.
    func test_the_summary_uses_the_grids_word() {
        let covered = GridCell(
            seat: "CLAUDE", kind: .hooks, state: .covered, rules: [rule("PASS")], coverReason: "Not here."
        )
        let summary = SeatSetup.summary(for: covered)
        XCTAssertTrue(summary.hasPrefix("\(CellState.covered.gridWord), 1 rule"), summary)
        XCTAssertFalse(summary.contains(CellState.covered.rawValue), summary)
    }

    /// What Seat Setup raises carries the same word the grid's badge shows.
    ///
    /// This only pins the spelling for the future:  the states Seat Setup raises today
    /// (MISSING, DRIFT, UNKNOWN, UNPROVEN) are spelled the same by the grid's word and by the
    /// state's raw value,  and the two that are spelled differently (DOES NOT APPLY and NO
    /// GUARD) are never raised,  so this test would pass on the old spelling too.  The summary
    /// test above is the one that fails on the old spelling.
    func test_raised_items_carry_the_grids_word() {
        for state in ["MISSING", "DRIFT", "UNKNOWN", "UNPROVEN"] {
            let cells = GridBuilder.build(
                roster: ["CLAUDE"], manifest: nil, results: [rule(state, reasonCode: "drift")]
            )
            let hooks = cells.first { $0.kind == .hooks }
            let raised = SeatSetup.attention(for: cells).first { $0.kind == .hooks }
            XCTAssertEqual(raised?.state, hooks?.gridWord, state)
        }
    }
}
