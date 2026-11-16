import XCTest

/// The grid while the engine runs,  and the manifest and registry read again when they
/// change.
///
/// The engine here is answered by hand,  so a test can look at the store while a run is
/// in flight:  SHELL-14,  GRID-8 and PERF-2 were all the grid turning red UNKNOWN for the
/// whole of every run,  and SHELL-29 was a seat added to `fleet-seats.json` never getting
/// a row until the app was relaunched.
final class FleetStoreRunTests: XCTestCase {
    /// An engine the test answers by hand.
    private final class HandEngine {
        private var pending: [(EngineRun) -> Void] = []
        private(set) var runs = 0

        func run(_ location: EngineLocation, _ completion: @escaping (EngineRun) -> Void) {
            runs += 1
            pending.append(completion)
        }

        /// Answer the oldest run in flight.
        func answer(_ run: EngineRun) {
            guard !pending.isEmpty else { return }
            let completion = pending.removeFirst()
            completion(run)
        }
    }

    /// A manifest with only the CLAUDE seat.
    private let claudeOnlyManifest = """
    {
     "schemaVersion": 2,
     "seats": {
      "CLAUDE": {
       "platforms": ["claude"],
       "rules": [
        {
         "id": "claude-hooks-lane-guard",
         "kind": "hooks",
         "platforms": ["claude"],
         "check": {"type": "hook_registered", "detail": "fleet-guard-hook", "target": "~/.claude/settings.json"}
        }
       ],
       "notApplicable": []
      }
     }
    }
    """

    private func makeRoot(manifest: String) throws -> URL {
        let root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-store-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try write(manifest, to: root)
        try Fixtures.registry.write(
            to: root.appendingPathComponent("fleet-apps.json"),
            atomically: true,
            encoding: .utf8
        )
        return root
    }

    private func write(_ manifest: String, to root: URL) throws {
        try manifest.write(
            to: root.appendingPathComponent("fleet-seats.json"),
            atomically: true,
            encoding: .utf8
        )
    }

    private func makeStore(root: URL, engine: HandEngine) -> FleetStore {
        FleetStore(
            location: EngineLocation(root: root, interpreter: URL(fileURLWithPath: "/usr/bin/python3")),
            runEngine: { location, completion in engine.run(location, completion) },
            refreshOnStart: false
        )
    }

    private func answer(_ envelope: StatusEnvelope) -> EngineRun {
        EngineRun(envelope: envelope, exitCode: 0, standardError: "", failure: nil, timedOut: false)
    }

    private var failedRun: EngineRun {
        EngineRun(envelope: nil, exitCode: 1, standardError: "", failure: "the engine did not answer", timedOut: false)
    }

    /// Answer the run in flight,  and wait for the store to take the answer on the main queue.
    private func land(_ run: EngineRun, on store: FleetStore, engine: HandEngine) {
        let landed = expectation(description: "the run landed")
        store.onRunFinished = { _ in landed.fulfill() }
        engine.answer(run)
        wait(for: [landed], timeout: 5)
        store.onRunFinished = nil
    }

    private func passingEnvelope() throws -> StatusEnvelope {
        try EnvelopeDecoder.decode(Data(Fixtures.envelope.utf8))
    }

    // MARK: SHELL-14, GRID-8, PERF-2:  a run keeps the last answer

    /// During a run the cells keep the last run's states,  each marked as being checked,
    /// and the summary and the clock stay with them.
    func test_a_run_keeps_the_last_answer_and_marks_it_as_being_checked() throws {
        let root = try makeRoot(manifest: Fixtures.manifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let engine = HandEngine()
        let store = makeStore(root: root, engine: engine)

        // The first run has no earlier answer:  its cells are being checked, not a hole.
        store.refresh()
        XCTAssertTrue(store.isChecking)
        XCTAssertFalse(store.isShowingLastAnswer)
        XCTAssertEqual(store.checkingNote, "Checking now.\u{00A0} The engine has not answered yet.")
        let first = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .hooks))
        XCTAssertTrue(first.isCheckingWithNoAnswer)
        XCTAssertEqual(first.gap, .beingChecked)
        land(answer(try passingEnvelope()), on: store, engine: engine)
        XCTAssertFalse(store.isChecking)
        XCTAssertEqual(store.phase, .loaded)
        XCTAssertEqual(store.cell(seat: "CLAUDE", kind: .hooks)?.state, .passed)

        // The second run:  the answer stays on screen while it is checked again.
        store.refresh()
        XCTAssertEqual(engine.runs, 2)
        XCTAssertEqual(store.phase, .running)
        XCTAssertTrue(store.isChecking)
        XCTAssertTrue(store.isShowingLastAnswer)
        let during = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .hooks))
        XCTAssertEqual(during.state, .passed)
        XCTAssertFalse(during.state.isRed)
        XCTAssertTrue(during.isBeingChecked)
        XCTAssertFalse(during.isCheckingWithNoAnswer)
        XCTAssertTrue(during.tooltip.hasSuffix(GridCell.checkingLine))
        XCTAssertNotNil(store.summary)
        XCTAssertNotNil(store.generatedAt)
        XCTAssertNotNil(store.commonCheckedAt)
        XCTAssertTrue(store.checkingNote?.hasPrefix("Checking again now.") ?? false)
        XCTAssertEqual(store.cell(seat: "ECHO", kind: .hooks)?.state, .covered)
        XCTAssertFalse(store.cells.contains { $0.state.isRed && !$0.rules.isEmpty })

        land(answer(try passingEnvelope()), on: store, engine: engine)
        XCTAssertFalse(store.isChecking)
        XCTAssertFalse(store.cell(seat: "CLAUDE", kind: .hooks)?.isBeingChecked ?? true)
    }

    /// A run that fails clears the last answer:  nothing stale stays on screen as current.
    func test_a_failed_run_clears_the_last_answer() throws {
        let root = try makeRoot(manifest: Fixtures.manifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let engine = HandEngine()
        let store = makeStore(root: root, engine: engine)
        store.refresh()
        land(answer(try passingEnvelope()), on: store, engine: engine)
        XCTAssertEqual(store.cell(seat: "CLAUDE", kind: .hooks)?.state, .passed)

        store.refresh()
        XCTAssertEqual(store.cell(seat: "CLAUDE", kind: .hooks)?.state, .passed)
        land(failedRun, on: store, engine: engine)

        let after = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .hooks))
        XCTAssertEqual(after.state, .unknown)
        XCTAssertTrue(after.rules.isEmpty)
        XCTAssertFalse(after.isBeingChecked)
        XCTAssertEqual(after.gap, .notAnswered)
        XCTAssertFalse(store.isChecking)
        XCTAssertNil(store.summary)
        XCTAssertNil(store.generatedAt)
        XCTAssertNil(store.commonCheckedAt)
        XCTAssertNil(store.checkingNote)
        guard case .failed = store.phase else {
            return XCTFail("a failed run should leave the store failed, not \(store.phase)")
        }
    }

    // MARK: GRID-10:  the panel's words for an empty cell

    /// The panel says why a cell is empty,  and "no rule covers" only when none does.
    func test_the_panel_says_why_an_empty_cell_is_empty() throws {
        let root = try makeRoot(manifest: Fixtures.manifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = makeStore(root: root, engine: HandEngine())
        let hooks = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .hooks))
        XCTAssertEqual(store.artifacts(for: hooks).emptyText, CellGap.notAnswered.sentence)
        let instructions = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .instructions))
        XCTAssertEqual(store.artifacts(for: instructions).emptyText, "No rule covers this seat and kind.")
        let covered = try XCTUnwrap(store.cell(seat: "ECHO", kind: .hooks))
        XCTAssertEqual(store.artifacts(for: covered).emptyText, "Echo runs elsewhere.")
    }

    // MARK: SHELL-29:  the manifest and the registry are read again when they change

    /// A seat added to the manifest gets its row on the next run,  and a manifest that
    /// cannot be read is named while the last good copy keeps the roster.
    func test_a_changed_manifest_is_read_again_before_the_next_run() throws {
        let root = try makeRoot(manifest: claudeOnlyManifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let engine = HandEngine()
        let store = makeStore(root: root, engine: engine)
        XCTAssertEqual(store.roster, ["CLAUDE"])
        XCTAssertFalse(store.reloadInputsIfChanged(), "nothing changed, so nothing is read")

        // A seat is added while the app runs.
        try write(Fixtures.manifest, to: root)
        store.refresh()
        XCTAssertEqual(store.roster, ["CLAUDE", "ECHO"])
        XCTAssertNotNil(store.manifest?.seats["ECHO"])
        XCTAssertEqual(store.cell(seat: "ECHO", kind: .hooks)?.state, .covered)
        land(answer(try passingEnvelope()), on: store, engine: engine)

        // A bad write is named, and the last good copy keeps the roster.
        try write("{ this is not a manifest", to: root)
        store.refresh()
        XCTAssertNotNil(store.manifestProblem)
        XCTAssertNotNil(store.manifest)
        XCTAssertEqual(store.roster, ["CLAUDE", "ECHO"])
        land(failedRun, on: store, engine: engine)
        // The kept copy is not the file on disk,  so a pair it names no rule for is not
        // called a hole in the coverage.
        let stale = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .instructions))
        XCTAssertEqual(stale.gap, .manifestStale)
        XCTAssertFalse(stale.tooltip.contains("no rule covers"), stale.tooltip)
        XCTAssertEqual(store.artifacts(for: stale).emptyText, CellGap.manifestStale.sentence)

        // A good write clears the problem.
        try write(claudeOnlyManifest, to: root)
        store.refresh()
        XCTAssertNil(store.manifestProblem)
        XCTAssertEqual(store.roster, ["CLAUDE"])
        XCTAssertEqual(store.cell(seat: "CLAUDE", kind: .instructions)?.gap, .noRule)
        land(failedRun, on: store, engine: engine)
    }

    // MARK: The MiniMax panel's hook proofs during a run

    /// During a run the hook proofs are the last run's,  so they are handed out only to a
    /// caller that says so:  by default a hook must not read "Proved by" beside "the engine
    /// has not answered".
    func test_hook_proofs_during_a_run_are_given_only_when_asked_for() throws {
        let root = try makeRoot(manifest: Fixtures.manifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let engine = HandEngine()
        let store = makeStore(root: root, engine: engine)
        store.refresh()
        land(answer(try passingEnvelope()), on: store, engine: engine)
        XCTAssertEqual(store.hookProofs(seat: "CLAUDE").map { $0.hookName }, ["lane-guard"])

        store.refresh()
        XCTAssertTrue(store.isShowingLastAnswer)
        XCTAssertTrue(store.hookProofs(seat: "CLAUDE").isEmpty)
        XCTAssertEqual(
            store.hookProofs(seat: "CLAUDE", includingLastAnswer: true).map { $0.hookName },
            ["lane-guard"]
        )
        land(answer(try passingEnvelope()), on: store, engine: engine)
        XCTAssertEqual(store.hookProofs(seat: "CLAUDE").map { $0.hookName }, ["lane-guard"])
    }

    // MARK: Reading a cell's artifacts off the main thread

    /// The panels read a cell's artifacts in a detached task while the main thread may read
    /// the manifest again;  the read takes the manifest under a lock,  so both run at once.
    func test_artifacts_can_be_read_off_the_main_thread_during_a_re_read() throws {
        let root = try makeRoot(manifest: Fixtures.manifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = makeStore(root: root, engine: HandEngine())
        let cell = try XCTUnwrap(store.cell(seat: "CLAUDE", kind: .instructions))
        let done = expectation(description: "the background reads finished")
        DispatchQueue.global(qos: .userInitiated).async {
            for _ in 0..<200 { _ = store.artifacts(for: cell) }
            done.fulfill()
        }
        for index in 0..<20 {
            try write(index.isMultiple(of: 2) ? claudeOnlyManifest : Fixtures.manifest, to: root)
            store.reloadInputsIfChanged()
        }
        wait(for: [done], timeout: 10)
        XCTAssertNotNil(store.manifest)
    }

    // MARK: GRID-19:  the surfaces the engine does not check

    /// The engine's uncovered surfaces reach the store,  and a failed run clears them.
    func test_the_uncovered_surfaces_reach_the_store() throws {
        let root = try makeRoot(manifest: Fixtures.manifest)
        defer { try? FileManager.default.removeItem(at: root) }
        let engine = HandEngine()
        let store = makeStore(root: root, engine: engine)
        let json = Fixtures.envelope.replacingOccurrences(
            of: "\"results\": [",
            with: """
            "uncovered": [{"seat": "MM", "surface": "~/.minimax/identity.yaml", "reason": "No kind covers it."}],
             "results": [
            """
        )
        store.refresh()
        land(answer(try EnvelopeDecoder.decode(Data(json.utf8))), on: store, engine: engine)
        XCTAssertEqual(store.uncoveredSurfaces(seat: "MM").map { $0.surface }, ["~/.minimax/identity.yaml"])
        XCTAssertTrue(store.uncoveredSurfaces(seat: "CLAUDE").isEmpty)
        store.refresh()
        land(failedRun, on: store, engine: engine)
        XCTAssertTrue(store.uncovered.isEmpty)
    }
}
