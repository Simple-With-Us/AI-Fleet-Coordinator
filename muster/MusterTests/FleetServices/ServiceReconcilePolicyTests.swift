import XCTest

/// The policy table,  the engine that decides one service at a time,  and the words about it.
final class ServiceReconcilePolicyTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)
    private let config = ServiceReconcileConfig(maxTries: 3, firstDelay: 60, stableAfter: 600)

    private func decide(state: ManagedServiceState = .stopped,
                        policy: ServiceReconcilePolicy = .keepRunning,
                        stoppedByOwner: Bool = false,
                        enabled: Bool = true,
                        record: ReconcileRecord = ReconcileRecord(),
                        now: Date? = nil,
                        firstLook: Bool = true) -> ReconcileDecision {
        ServiceReconcileEngine.decide(state: state, policy: policy, stoppedByOwner: stoppedByOwner,
                                      enabled: enabled, record: record, now: now ?? t0,
                                      firstLook: firstLook, config: config)
    }

    // MARK: - The table

    func testEveryServiceInTheServicesListHasAnExplicitRowAndAReason() {
        for descriptor in FleetServiceRegistry.all {
            XCTAssertTrue(ServicePolicyTable.listedServiceIDs.contains(descriptor.id),
                          "\(descriptor.id) has no row in the policy table")
            let row = ServicePolicyTable.row(for: descriptor.id)
            XCTAssertEqual(row?.source, .servicesList)
            XCTAssertFalse(row?.reason.isEmpty ?? true, descriptor.id)
        }
        XCTAssertEqual(Set(ServicePolicyTable.listedServiceIDs).count, ServicePolicyTable.listedServiceIDs.count)
        XCTAssertEqual(ServicePolicyTable.listedServiceIDs.count, FleetServiceRegistry.all.count)
    }

    func testTheBotFleetServicesStoppedOnPurposeAreLeftAlone() {
        // BotFleet Server,  its MCP server and its Companion are held down on purpose today
        // (`~/.botfleet/stopped.marker`),  and starting the server clears that marker.
        for id in ["app.botfleet.server", "botfleet-mcp", "app.botfleet.companion"] {
            XCTAssertEqual(ServicePolicyTable.policy(for: id), .leaveAlone, id)
        }
    }

    func testMostServicesKeepRunning() {
        let rows = ServicePolicyTable.rows.filter { $0.source == .servicesList }
        let keeping = rows.filter { $0.policy == .keepRunning }
        XCTAssertGreaterThan(keeping.count, rows.count / 2)
        for id in ["shellular", "code-main-keeper", "vision-worker", "xcode-health", "agy-acp", "grok-acp", "seat-mcp"] {
            XCTAssertEqual(ServicePolicyTable.policy(for: id), .keepRunning, id)
        }
    }

    func testAServiceThatStopsItselfOnPurposeIsStartedAtLoginOnly() {
        XCTAssertEqual(ServicePolicyTable.policy(for: "grok-leader"), .startAtLoginOnly)
        XCTAssertEqual(ServicePolicyTable.policy(for: "clutch-web"), .startAtLoginOnly)
    }

    func testEveryAllowlistedAgentIsLeftAlone() {
        for label in ServicePolicyTable.agentLabels {
            let row = ServicePolicyTable.row(for: label)
            XCTAssertEqual(row?.policy, .leaveAlone, label)
            XCTAssertEqual(row?.source, .allowlistedAgent, label)
            XCTAssertFalse(row?.reason.isEmpty ?? true, label)
        }
    }

    func testTheTableNamesEveryAgentTheAllowlistFileNames() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let file = root.appendingPathComponent("scripts/fleet-ops-control.json")
        // The checkout this test runs in carries the allowlist.  Without it there is
        // nothing to compare,  and that is not a failure of the table.
        guard let data = try? Data(contentsOf: file) else { throw XCTSkip("The allowlist file is not in this checkout.") }
        let allowlist = try XCTUnwrap(OpsAllowlist.parse(data))
        let named = Set(allowlist.macAgents.map(\.name))
        XCTAssertEqual(named, Set(ServicePolicyTable.agentLabels),
                       "The policy table and scripts/fleet-ops-control.json name different LaunchAgents.")
    }

    func testAServiceNobodyWroteARowForIsLeftAlone() {
        XCTAssertEqual(ServicePolicyTable.policy(for: "something-new"), .leaveAlone)
        XCTAssertEqual(ServicePolicyTable.reason(for: "something-new"), ServicePolicyTable.unlistedReason)
    }

    func testThePoliciesAreNamedInTitleCase() {
        XCTAssertEqual(ServiceReconcilePolicy.allCases.map(\.title),
                       ["Keep Running", "Start Once Per Launch", "Leave Alone"])
    }

    func testARowSaysWhatMusterDoesForItsPolicy() {
        let services = FleetServices(serverState: .running, states: [:])
        let shellular = services.entries.first { $0.id == "shellular" }
        XCTAssertEqual(shellular?.reconcilePolicyLine(), "Keep Running:  Muster starts it again when it stops.")
        let server = services.entries.first { $0.id == "app.botfleet.server" }
        XCTAssertTrue(server?.reconcilePolicyLine().hasPrefix("Leave Alone:  ") == true)
    }

    func testAStartOncePerLaunchRowDoesNotSayLogin() {
        let services = FleetServices(serverState: .running, states: [:])
        let leader = services.entries.first { $0.id == "grok-leader" }
        XCTAssertEqual(leader?.reconcilePolicyLine(),
                       "Start Once Per Launch:  Muster starts it once when Muster starts, and never again after that.")
        for row in ServicePolicyTable.rows {
            XCTAssertFalse(row.reason.lowercased().contains("login"), row.id)
        }
    }

    func testARowDoesNotPromiseAStartWhenKeepServicesRunningIsOff() {
        let services = FleetServices(serverState: .running, states: [:])
        let off = "Turned off in Settings, so Muster does not start it."
        for id in ["shellular", "grok-leader", "clutch-web"] {
            let line = services.entries.first { $0.id == id }?.reconcilePolicyLine(keepServicesRunning: false)
            XCTAssertTrue(line?.hasSuffix(off) == true, id)
            XCTAssertFalse(line?.contains("starts it again") == true, id)
        }
        // A Leave Alone row keeps its own reason,  which is true either way.
        let server = services.entries.first { $0.id == "app.botfleet.server" }
        XCTAssertEqual(server?.reconcilePolicyLine(keepServicesRunning: false),
                       server?.reconcilePolicyLine(keepServicesRunning: true))
    }

    // MARK: - What it does

    func testAStoppedOrFailedKeepRunningServiceIsStarted() {
        XCTAssertEqual(decide(state: .stopped), .start(attempt: 1))
        XCTAssertEqual(decide(state: .failed), .start(attempt: 1))
    }

    func testItNeverActsOnAServiceInAnyOtherState() {
        for state in [ManagedServiceState.running, .unknown, .notInstalled, .notInPM2] {
            XCTAssertEqual(decide(state: state), .skip(.nothingToDo), "\(state)")
        }
    }

    func testAnApplicationPM2NeverHeardOfIsNeverStarted() {
        // A start could make a second copy of a job that belongs to another machine.
        XCTAssertEqual(decide(state: .notInPM2), .skip(.nothingToDo))
    }

    func testALeaveAlonePolicyIsNeverStarted() {
        XCTAssertEqual(decide(state: .stopped, policy: .leaveAlone), .skip(.leaveAlone))
        XCTAssertEqual(decide(state: .failed, policy: .leaveAlone), .skip(.leaveAlone))
    }

    func testTurningTheSettingOffStopsEverything() {
        for policy in ServiceReconcilePolicy.allCases {
            XCTAssertEqual(decide(policy: policy, enabled: false), .skip(.turnedOff), policy.title)
        }
    }

    // MARK: - The owner's stop wins

    func testTheOwnersStopWinsOverEveryPolicyAndEveryState() {
        for policy in ServiceReconcilePolicy.allCases {
            for state in [ManagedServiceState.stopped, .failed, .running, .unknown, .notInstalled, .notInPM2] {
                XCTAssertEqual(decide(state: state, policy: policy, stoppedByOwner: true),
                               .skip(.stoppedByOwner), "\(policy.title) \(state)")
            }
        }
    }

    func testTheOwnersStopWinsEvenWhenAStopReadsAsFailed() {
        // A service the owner stopped can read as failed,  and a failed service would
        // otherwise be started.
        XCTAssertEqual(decide(state: .failed, stoppedByOwner: true), .skip(.stoppedByOwner))
    }

    func testTheOwnersStopWinsEvenOnTheFirstLookAtLogin() {
        XCTAssertEqual(decide(state: .stopped, policy: .startAtLoginOnly, stoppedByOwner: true, firstLook: true),
                       .skip(.stoppedByOwner))
    }

    func testTheOwnersStopWinsOverARecordThatWasDueForAnotherTry() {
        var record = ReconcileRecord()
        record.attempts = 1
        record.lastAttemptAt = t0
        XCTAssertEqual(decide(state: .stopped, stoppedByOwner: true, record: record,
                              now: t0.addingTimeInterval(3_600)),
                       .skip(.stoppedByOwner))
    }

    func testTheOwnersStopStartsTheCountAgain() {
        var record = ReconcileRecord()
        record.attempts = 2
        record.lastAttemptAt = t0
        record.gaveUp = true
        record.lastSuccessAt = t0
        let next = ServiceReconcileEngine.settle(record, state: .stopped, decision: .skip(.stoppedByOwner),
                                                 now: t0, config: config)
        XCTAssertEqual(next.attempts, 0)
        XCTAssertFalse(next.gaveUp)
    }

    func testAServiceStartedAgainByTheOwnerIsFairGameAgain() {
        // The owner's start takes the stop off the record,  so the engine sees an ordinary
        // service that is not running.
        XCTAssertEqual(decide(state: .stopped, stoppedByOwner: false), .start(attempt: 1))
    }

    // MARK: - The backoff

    func testTheDelayDoublesAfterEveryTry() {
        XCTAssertEqual(config.delay(afterAttempts: 0), 0)
        XCTAssertEqual(config.delay(afterAttempts: 1), 60)
        XCTAssertEqual(config.delay(afterAttempts: 2), 120)
        XCTAssertEqual(config.delay(afterAttempts: 3), 240)
    }

    func testAServiceIsNotStartedAgainBeforeItsDelayIsUp() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .keepRunning,
                                                      outcome: .started, at: t0)
        let due = t0.addingTimeInterval(60)
        XCTAssertEqual(decide(record: record, now: t0.addingTimeInterval(30)), .skip(.waiting(until: due)))
        XCTAssertEqual(decide(record: record, now: due), .start(attempt: 2))
    }

    func testTheSecondDelayIsLongerThanTheFirst() {
        var record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .keepRunning,
                                                      outcome: .started, at: t0)
        let second = t0.addingTimeInterval(60)
        record = ServiceReconcileEngine.recordTry(record, policy: .keepRunning, outcome: .started, at: second)
        // 60 seconds after the second try is not enough,  120 is.
        XCTAssertEqual(decide(record: record, now: second.addingTimeInterval(60)),
                       .skip(.waiting(until: second.addingTimeInterval(120))))
        XCTAssertEqual(decide(record: record, now: second.addingTimeInterval(120)), .start(attempt: 3))
    }

    // MARK: - The cap

    func testItStopsRetryingAfterTheLastTry() {
        var record = ReconcileRecord()
        var now = t0
        for attempt in 1...3 {
            XCTAssertEqual(decide(record: record, now: now), .start(attempt: attempt))
            record = ServiceReconcileEngine.recordTry(record, policy: .keepRunning, outcome: .started, at: now)
            now = now.addingTimeInterval(config.delay(afterAttempts: attempt))
        }
        // The third try had its whole delay to take,  and the service is still not running.
        let decision = decide(record: record, now: now)
        XCTAssertEqual(decision, .skip(.gaveUp(tries: 3)))
        let settled = ServiceReconcileEngine.settle(record, state: .stopped, decision: decision, now: now, config: config)
        XCTAssertTrue(settled.gaveUp)
        // Once given up it is never started again,  however long it waits.
        XCTAssertEqual(decide(record: settled, now: now.addingTimeInterval(86_400)), .skip(.gaveUp(tries: 3)))
    }

    func testTheThirdTryGetsItsTimeToTakeBeforeMusterGivesUp() {
        var record = ReconcileRecord()
        for attempt in 1...3 {
            record = ServiceReconcileEngine.recordTry(record, policy: .keepRunning, outcome: .started,
                                                      at: t0.addingTimeInterval(Double(attempt)))
        }
        let early = t0.addingTimeInterval(10)
        XCTAssertEqual(decide(record: record, now: early), .skip(.waiting(until: t0.addingTimeInterval(3 + 240))))
    }

    func testAFailedTryCountsLikeAnyOther() {
        var record = ReconcileRecord()
        for _ in 1...3 {
            record = ServiceReconcileEngine.recordTry(record, policy: .keepRunning,
                                                      outcome: .notStarted("pm2 said no"), at: t0)
        }
        XCTAssertEqual(record.attempts, 3)
        XCTAssertEqual(decide(record: record, now: t0.addingTimeInterval(10_000)), .skip(.gaveUp(tries: 3)))
    }

    func testAServiceThatStaysUpStartsItsCountAgain() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .keepRunning,
                                                      outcome: .started, at: t0)
        // Up,  but not for long enough yet:  the count stays.
        let early = ServiceReconcileEngine.settle(record, state: .running, decision: .skip(.nothingToDo),
                                                  now: t0.addingTimeInterval(120), config: config)
        XCTAssertEqual(early.attempts, 1)
        // Up for ten minutes:  the count starts again,  and the last start that worked is kept.
        let stable = ServiceReconcileEngine.settle(record, state: .running, decision: .skip(.nothingToDo),
                                                   now: t0.addingTimeInterval(600), config: config)
        XCTAssertEqual(stable.attempts, 0)
        XCTAssertEqual(stable.lastSuccessAt, t0)
    }

    func testAServiceThatKeepsCrashingIsNotRestartedForeverEvenIfItStaysUpBriefly() {
        // It is seen running a minute after each try and then fails again:  it never gets to
        // ten minutes up,  so the count is never cleared and the cap is reached.
        var record = ReconcileRecord()
        var now = t0
        var tries = 0
        for _ in 0..<20 {
            let decision = decide(record: record, now: now)
            if case .start = decision {
                tries += 1
                record = ServiceReconcileEngine.recordTry(record, policy: .keepRunning, outcome: .started, at: now)
                // A look at it running,  a minute later.
                record = ServiceReconcileEngine.settle(record, state: .running, decision: .skip(.nothingToDo),
                                                       now: now.addingTimeInterval(60), config: config)
            } else {
                // It crashed again,  and stays down.
                record = ServiceReconcileEngine.settle(record, state: .stopped, decision: decision, now: now, config: config)
            }
            now = now.addingTimeInterval(300)
        }
        XCTAssertEqual(tries, 3)
        XCTAssertTrue(record.gaveUp)
    }

    func testAServiceGivenUpOnThatIsRunningAgainIsFairGameAgain() {
        var record = ReconcileRecord()
        record.attempts = 3
        record.gaveUp = true
        record.lastAttemptAt = t0
        let next = ServiceReconcileEngine.settle(record, state: .running, decision: .skip(.nothingToDo),
                                                 now: t0.addingTimeInterval(5), config: config)
        XCTAssertFalse(next.gaveUp)
        XCTAssertEqual(next.attempts, 0)
    }

    // MARK: - Start at login only

    func testALoginOnlyServiceIsStartedOnTheFirstLookAndNeverAgain() {
        XCTAssertEqual(decide(state: .stopped, policy: .startAtLoginOnly, firstLook: true), .start(attempt: 1))
        XCTAssertEqual(decide(state: .stopped, policy: .startAtLoginOnly, firstLook: false), .skip(.notAtLogin))
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .startAtLoginOnly,
                                                      outcome: .started, at: t0)
        XCTAssertTrue(record.loginStartUsed)
        XCTAssertEqual(decide(state: .stopped, policy: .startAtLoginOnly, record: record,
                              now: t0.addingTimeInterval(86_400), firstLook: true),
                       .skip(.loginStartUsed))
    }

    func testAFailedLoginStartIsStillTheOneStart() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .startAtLoginOnly,
                                                      outcome: .notStarted("no"), at: t0)
        XCTAssertEqual(decide(state: .failed, policy: .startAtLoginOnly, record: record, firstLook: true),
                       .skip(.loginStartUsed))
    }

    func testTheLoginStartStaysUsedWhenTheCountStartsAgain() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .startAtLoginOnly,
                                                      outcome: .started, at: t0)
        let settled = ServiceReconcileEngine.settle(record, state: .running, decision: .skip(.nothingToDo),
                                                    now: t0.addingTimeInterval(700), config: config)
        XCTAssertTrue(settled.loginStartUsed)
    }

    // MARK: - What the owner reads

    func testTheNotesSayWhatMusterDid() {
        let at = t0
        var record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .keepRunning, outcome: .started, at: at)
        let time = FleetTime.clock(at)
        // Started and running:  the example the owner gave.
        let running = ReconcileNote.make(record: record, state: .running, config: config)
        XCTAssertEqual(running?.line, "Restarted by Muster at \(time)")
        XCTAssertEqual(running?.isProblem, false)
        // Started and stopped again.
        let stopped = ReconcileNote.make(record: record, state: .stopped, config: config)
        XCTAssertEqual(stopped?.line,
                       "Restarted by Muster at \(time), and it stopped again (try 1).\u{00A0} Next try at \(FleetTime.clock(at.addingTimeInterval(60))).")
        XCTAssertEqual(stopped?.isProblem, true)
        XCTAssertEqual(stopped?.shortLine, "Restarted by Muster at \(time), and it stopped again")
        // Gave up.
        record.gaveUp = true
        record.attempts = 3
        let gaveUp = ReconcileNote.make(record: record, state: .stopped, config: config)
        XCTAssertEqual(gaveUp?.line, "Muster stopped retrying after 3 tries")
        XCTAssertEqual(gaveUp?.isProblem, true)
    }

    func testANoteForAStartThatDidNotWorkSaysWhy() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .keepRunning,
                                                      outcome: .notStarted("pm2 no longer remembers this application"),
                                                      at: t0)
        let note = ReconcileNote.make(record: record, state: .stopped, config: config)
        XCTAssertEqual(note?.line,
                       "Muster could not restart it at \(FleetTime.clock(t0)):  pm2 no longer remembers this application.\u{00A0} Next try at \(FleetTime.clock(t0.addingTimeInterval(60))).")
        XCTAssertEqual(note?.shortLine, "Muster could not restart it at \(FleetTime.clock(t0))")
    }

    func testALoginOnlyServiceHasNoNextTry() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .startAtLoginOnly,
                                                      outcome: .started, at: t0)
        let note = ReconcileNote.make(record: record, state: .stopped, policy: .startAtLoginOnly, config: config)
        XCTAssertFalse(note?.line.contains("Next try") ?? true)
    }

    func testAOneTryGiveUpIsSaidInTheSingular() {
        XCTAssertEqual(ReconcileNote(.gaveUp(tries: 1)).line, "Muster stopped retrying after 1 try")
    }

    func testAStartedServiceWhoseStateCannotBeSeenIsNotReportedAsRestarted() {
        let record = ServiceReconcileEngine.recordTry(ReconcileRecord(), policy: .keepRunning,
                                                      outcome: .started, at: t0)
        for state in [ManagedServiceState.unknown, .notInstalled, .notInPM2] {
            let note = ReconcileNote.make(record: record, state: state, config: config)
            XCTAssertEqual(note?.line, "Muster started it at \(FleetTime.clock(t0)), and its state is not known.", "\(state)")
            XCTAssertEqual(note?.isProblem, true, "\(state)")
        }
    }

    func testAStableServiceKeepsTheNoteOfTheLastStartThatWorked() {
        var record = ReconcileRecord()
        record.lastSuccessAt = t0
        XCTAssertEqual(ReconcileNote.make(record: record, state: .running, config: config)?.line,
                       "Restarted by Muster at \(FleetTime.clock(t0))")
        XCTAssertNil(ReconcileNote.make(record: record, state: .stopped, config: config))
        XCTAssertNil(ReconcileNote.make(record: ReconcileRecord(), state: .running, config: config))
    }

    func testNoNoteUsesTheWordWantedOrAnEmDash() {
        let notes: [ReconcileNote] = [
            ReconcileNote(.restarted(at: t0)),
            ReconcileNote(.stoppedAgain(tries: 2, at: t0, next: t0)),
            ReconcileNote(.couldNotStart(reason: "no", at: t0, next: nil)),
            ReconcileNote(.gaveUp(tries: 3)),
            ReconcileNote(.startedStateNotKnown(at: t0)),
        ]
        for note in notes {
            for line in [note.line, note.shortLine] {
                XCTAssertFalse(line.lowercased().contains("wanted"), line)
                XCTAssertFalse(line.contains("\u{2014}"), line)
            }
        }
    }

    // MARK: - The marker on a row

    func testEveryStateHasAWordASymbolAndATone() {
        let states: [ManagedServiceState] = [.running, .stopped, .failed, .notInstalled, .notInPM2, .unknown]
        var words: Set<String> = []
        for state in states {
            let marker = ServiceStateMarker.make(state: state)
            XCTAssertFalse(marker.word.isEmpty)
            XCTAssertFalse(marker.symbol.isEmpty)
            words.insert(marker.word)
        }
        // The words differ,  so the state never rests on colour alone.
        XCTAssertEqual(words.count, 6)
        XCTAssertEqual(ServiceStateMarker.make(state: .running).tone, .good)
        XCTAssertEqual(ServiceStateMarker.make(state: .failed).tone, .bad)
        XCTAssertEqual(ServiceStateMarker.make(state: .unknown).tone, .warning)
    }

    func testAStopByTheOwnerIsInTheWordAndARunningServiceThatWasToStopIsAWarning() {
        XCTAssertEqual(ServiceStateMarker.make(state: .stopped, stoppedByOwner: true).word, "Stopped By You")
        XCTAssertEqual(ServiceStateMarker.make(state: .running, stoppedByOwner: true).tone, .warning)
    }

    func testEveryRowHasAMarker() {
        let services = FleetServices(serverState: .running, states: [:])
        XCTAssertEqual(services.entries.count, FleetServiceRegistry.all.count)
        for entry in services.entries {
            XCTAssertFalse(entry.marker.word.isEmpty, entry.id)
        }
    }

    // MARK: - Real failure reasons

    func testACommandsReasonIsCleanedAndBounded() {
        XCTAssertEqual(CommandText.clean("\u{1B}[31mred\u{1B}[0m words"), "red words")
        XCTAssertEqual(CommandText.clean("a\n\nb\n"), "a b")
        // A table pm2 draws is not a reason.
        XCTAssertEqual(CommandText.clean("\u{250C}\u{2500}\u{2510}\n\u{2502} id \u{2502}\n\u{2514}\u{2500}\u{2518}"), "")
        let long = String(repeating: "x", count: 1_000)
        XCTAssertLessThanOrEqual(CommandText.clean(long).count, CommandText.reasonLimit + 1)
        // Only the last few lines are kept,  because the reason comes last.
        XCTAssertEqual(CommandText.clean("1\n2\n3\n4\n5\n6"), "3 4 5 6")
    }

    func testAnExitErrorPrefersStandardError() {
        let both = CommandExitError(output: "on stdout", status: 1, errorOutput: "on stderr")
        XCTAssertEqual(both.reason, "on stderr")
        XCTAssertEqual(both.errorDescription, "The system command failed:  on stderr")
        let stdoutOnly = CommandExitError(output: "on stdout", status: 1)
        XCTAssertEqual(stdoutOnly.reason, "on stdout")
        let silent = CommandExitError(output: "", status: 1)
        XCTAssertEqual(silent.errorDescription, "The system command failed.")
    }

    func testATimeoutNamesTheCommand() {
        let launchctl = CommandTimedOutError(executable: "/bin/launchctl", arguments: ["kickstart", "gui/501/x"])
        XCTAssertEqual(launchctl.errorDescription, "launchctl did not finish in time.")
        let pm2 = CommandTimedOutError(executable: "/opt/homebrew/bin/node",
                                       arguments: ["/opt/homebrew/lib/node_modules/pm2/bin/pm2", "jlist"])
        XCTAssertEqual(pm2.errorDescription, "pm2 did not finish in time.")
    }
}
