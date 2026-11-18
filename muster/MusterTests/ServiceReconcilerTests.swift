import XCTest

/// The loop around the engine:  what Muster starts,  what it leaves alone,  and what it says.
@MainActor
final class ServiceReconcilerTests: XCTestCase {
    /// Everything the reconciler reads and does,  as plain values.
    @MainActor
    private final class Rig {
        var states: [String: ManagedServiceState] = [:]
        /// The owner's stops as the last read carried them.
        var stoppedInEntries: Set<String> = []
        /// The owner's stops as they are right now.
        var stoppedLive: Set<String> = []
        var enabled = true
        var blocked = false
        var now = Date(timeIntervalSince1970: 1_800_000_000)
        /// Every service a start was sent for,  in order.
        var starts: [String] = []
        /// What the next start comes to.
        var result: ReconcileRun = .started
        var published: [String: ReconcileNote] = [:]

        lazy var reconciler: ServiceReconciler = ServiceReconciler(
            enabled: { [unowned self] in self.enabled },
            blocked: { [unowned self] in self.blocked },
            entries: { [unowned self] in
                FleetServices(serverState: .running, states: self.states,
                              stoppedByOwner: self.stoppedInEntries).entries
            },
            stoppedByOwner: { [unowned self] in self.stoppedLive.contains($0) },
            start: { [unowned self] entry in
                self.starts.append(entry.id)
                return self.result
            },
            publish: { [unowned self] in self.published = $0 },
            config: ServiceReconcileConfig(maxTries: 3, firstDelay: 60, stableAfter: 600),
            now: { [unowned self] in self.now })

        func pass() async { await reconciler.pass() }

        func advance(_ seconds: TimeInterval) { now = now.addingTimeInterval(seconds) }
    }

    // MARK: - What it starts

    func testAStoppedKeepRunningServiceIsStartedOnce() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        await rig.pass()
        XCTAssertEqual(rig.starts, ["shellular"])
        XCTAssertEqual(rig.published["shellular"]?.line, "Restarted by Muster at \(FleetTime.clock(rig.now))")
        // The same read again,  a moment later,  does not start it twice.
        rig.advance(5)
        await rig.pass()
        XCTAssertEqual(rig.starts, ["shellular"])
    }

    func testAFailedServiceIsStartedToo() async {
        let rig = Rig()
        rig.states = ["seat-mcp": .failed]
        await rig.pass()
        XCTAssertEqual(rig.starts, ["seat-mcp"])
    }

    func testRunningUnknownNotInstalledAndNotInPM2ServicesAreNeverStarted() async {
        let rig = Rig()
        rig.states = ["shellular": .running, "seat-mcp": .unknown, "agy-acp": .notInstalled, "grok-acp": .notInPM2]
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
        XCTAssertEqual(rig.published, [:])
    }

    func testAServiceLeftAloneIsNeverStarted() async {
        let rig = Rig()
        rig.states = ["botfleet-mcp": .failed, "app.botfleet.server": .stopped, "app.botfleet.companion": .failed]
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
    }

    func testSeveralServicesAreStartedOneAfterAnother() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped, "vision-worker": .failed]
        await rig.pass()
        XCTAssertEqual(rig.starts, ["shellular", "vision-worker"])
    }

    // MARK: - The owner's stop wins

    func testAServiceTheOwnerStoppedIsNeverStarted() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped, "seat-mcp": .failed]
        rig.stoppedInEntries = ["shellular", "seat-mcp"]
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
        XCTAssertEqual(rig.published, [:])
    }

    func testAStopTheOwnerMadeSinceTheLastReadStillWins() async {
        // The read that the entries come from was taken before the stop.
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        rig.stoppedLive = ["shellular"]
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
    }

    func testAStopMadeWhileAnEarlierStartRanStopsTheNextOne() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped, "vision-worker": .stopped]
        // The owner stops the second service while the first is being started.
        rig.result = .started
        let reconciler = ServiceReconciler(
            enabled: { true },
            blocked: { false },
            entries: { FleetServices(serverState: .running, states: rig.states).entries },
            stoppedByOwner: { rig.stoppedLive.contains($0) },
            start: { entry in
                rig.starts.append(entry.id)
                rig.stoppedLive.insert("vision-worker")
                return .started
            },
            publish: { _ in },
            now: { rig.now })
        await reconciler.pass()
        XCTAssertEqual(rig.starts, ["shellular"])
    }

    func testTheOwnersStopOnTheFirstLookAtLoginStillWins() async {
        let rig = Rig()
        rig.states = ["grok-leader": .stopped]
        rig.stoppedInEntries = ["grok-leader"]
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
    }

    // MARK: - The setting

    func testTurningTheSettingOffStartsNothingAndTurningItOnStartsAgain() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        rig.enabled = false
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
        rig.enabled = true
        rig.advance(120)
        await rig.pass()
        XCTAssertEqual(rig.starts, ["shellular"])
    }

    func testNothingIsLookedAtWhileTheRoadIsBlocked() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        rig.blocked = true
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
        rig.blocked = false
        await rig.pass()
        XCTAssertEqual(rig.starts, ["shellular"])
    }

    func testAStartSomethingGotInTheWayOfIsNotCountedAndStopsThePass() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped, "vision-worker": .stopped]
        rig.result = .skipped
        await rig.pass()
        // Only the first was sent,  and nothing is on the record.
        XCTAssertEqual(rig.starts, ["shellular"])
        XCTAssertNil(rig.reconciler.records["shellular"])
        XCTAssertEqual(rig.published, [:])
        // The next look tries again,  with no try used up.
        rig.result = .started
        await rig.pass()
        XCTAssertEqual(rig.starts, ["shellular", "shellular", "vision-worker"])
    }

    // MARK: - The backoff and the cap

    func testAServiceThatStopsAgainIsStartedAfterTheDelayAndThenGivenUpOn() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 1)

        // 30 seconds later:  too soon.
        rig.advance(30)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 1)

        // 60 seconds after the first try:  the second.
        rig.advance(30)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 2)

        // A look a little later finds it down again,  and the row says so.
        rig.advance(10)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 2)
        XCTAssertTrue(rig.published["shellular"]?.line.contains("stopped again (try 2)") == true,
                      rig.published["shellular"]?.line ?? "")

        // 120 seconds after the second try:  the third.
        rig.advance(110)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 3)

        // 240 seconds after the third,  the service is still down:  Muster stops.
        rig.advance(240)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 3)
        XCTAssertEqual(rig.published["shellular"]?.line, "Muster stopped retrying after 3 tries")

        // And it never starts it again,  however long it waits.
        rig.advance(86_400)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 3)
        XCTAssertEqual(rig.published["shellular"]?.isProblem, true)
    }

    func testAServiceGivenUpOnIsStartedAgainOnceItRunsAndFailsAgain() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        for step in [0.0, 60, 120, 240, 240] {
            rig.advance(step)
            await rig.pass()
        }
        XCTAssertEqual(rig.starts.count, 3)
        // Somebody starts it:  Muster sees it running,  and the count starts again.
        rig.states = ["shellular": .running]
        rig.advance(10)
        await rig.pass()
        XCTAssertEqual(rig.published["shellular"]?.isProblem, false)
        rig.states = ["shellular": .stopped]
        rig.advance(10)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 4)
    }

    func testAStartThatDidNotWorkSaysWhyAndCountsAsATry() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        rig.result = .notStarted("pm2 no longer remembers this application")
        await rig.pass()
        XCTAssertEqual(rig.reconciler.records["shellular"]?.attempts, 1)
        let line = rig.published["shellular"]?.line ?? ""
        XCTAssertTrue(line.hasPrefix("Muster could not restart it at "), line)
        XCTAssertTrue(line.contains("pm2 no longer remembers this application"), line)
        XCTAssertEqual(rig.published["shellular"]?.isProblem, true)
    }

    // MARK: - Start at login only

    func testALoginOnlyServiceIsStartedOnTheFirstLookAndNeverAfter() async {
        let rig = Rig()
        rig.states = ["grok-leader": .stopped]
        await rig.pass()
        XCTAssertEqual(rig.starts, ["grok-leader"])
        // It stops again an hour later:  Muster does not start it a second time.
        rig.advance(3_600)
        await rig.pass()
        rig.advance(3_600)
        await rig.pass()
        XCTAssertEqual(rig.starts, ["grok-leader"])
    }

    func testALoginOnlyServiceWhoseStateWasNotKnownAtFirstGetsItsStartWhenItIsKnown() async {
        let rig = Rig()
        rig.states = ["clutch-web": .unknown]
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
        // pm2 answers on the next read:  that is still the first look at its state.
        rig.states = ["clutch-web": .stopped]
        rig.advance(120)
        await rig.pass()
        XCTAssertEqual(rig.starts, ["clutch-web"])
        rig.advance(120)
        await rig.pass()
        XCTAssertEqual(rig.starts, ["clutch-web"])
    }

    func testALoginStartIsNotUsedUpByAPassThatCouldNotStartTheService() async {
        // The setting was off at the first look,  and a start something got in the way of did
        // not happen:  neither of those is the one start at login.
        let rig = Rig()
        rig.states = ["grok-leader": .stopped]
        rig.enabled = false
        await rig.pass()
        rig.enabled = true
        rig.result = .skipped
        await rig.pass()
        XCTAssertEqual(rig.starts, ["grok-leader"])
        rig.result = .started
        await rig.pass()
        XCTAssertEqual(rig.starts, ["grok-leader", "grok-leader"])
        rig.advance(3_600)
        await rig.pass()
        XCTAssertEqual(rig.starts.count, 2)
    }

    func testALoginOnlyServiceThatWasRunningAtLoginIsNotStartedWhenItStopsLater() async {
        let rig = Rig()
        rig.states = ["grok-leader": .running]
        await rig.pass()
        rig.states = ["grok-leader": .stopped]
        rig.advance(3_600)
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
    }

    // MARK: - What it says

    func testAServiceThatStaysUpKeepsTheNoteOfTheLastStart() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        await rig.pass()
        let started = rig.now
        rig.states = ["shellular": .running]
        rig.advance(700)
        await rig.pass()
        XCTAssertEqual(rig.published["shellular"]?.line, "Restarted by Muster at \(FleetTime.clock(started))")
        XCTAssertEqual(rig.reconciler.records["shellular"]?.attempts, 0)
    }

    func testAServiceTheOwnerStopsAfterMusterStartedItLosesTheNote() async {
        let rig = Rig()
        rig.states = ["shellular": .stopped]
        await rig.pass()
        XCTAssertNotNil(rig.published["shellular"])
        rig.states = ["shellular": .stopped]
        rig.stoppedInEntries = ["shellular"]
        rig.stoppedLive = ["shellular"]
        rig.advance(120)
        await rig.pass()
        XCTAssertNil(rig.published["shellular"])
        XCTAssertEqual(rig.starts.count, 1)
    }

    func testThePassDoesNotTouchServicesItCannotSee() async {
        let rig = Rig()
        // Nothing has been read yet:  every service is unknown.
        await rig.pass()
        XCTAssertEqual(rig.starts, [])
    }
}
