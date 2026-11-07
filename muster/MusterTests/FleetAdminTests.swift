import Combine
import XCTest

/// The fleet admin:  the control the standalone BotFleet Control app used to carry,
///  now part of Muster.
///
/// Three things have to be true of the admin on its own:  the fleet has to be in the
/// menu bar without the owner opening the window,  the read has to happen off the
/// main thread,  and a stop of the BotFleet Server has to wait for the server itself
/// rather than for launchd.
@MainActor
final class FleetAdminTests: XCTestCase {

    // MARK: - The read

    /// The menu bar carries the fleet from the moment the app is up,  and not only
    /// after the owner opens the window and the panel asks for a read.
    func testTheFleetIsReadWhenTheAdminComesUp() async {
        let commands = FleetAdminCommands()
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())

        await waitForTheFleet(admin)

        XCTAssertNotNil(admin.services, "The admin never read the fleet services.")
        XCTAssertEqual(admin.entries.count, 12, "The admin did not read every fleet service.")
    }

    /// The read shells out to launchctl and opens a socket,  so it cannot run on the
    /// main thread:  the menu has to stay alive while they answer.
    func testTheLaunchctlReadDoesNotHappenOnTheMainThread() async {
        let commands = FleetAdminCommands()
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())

        await admin.refresh()

        let onTheMainThread = commands.launchctlReads.filter(\.onMainThread)
        XCTAssertTrue(onTheMainThread.isEmpty,
                      "launchctl was read on the main thread \(onTheMainThread.count) times.")
        XCTAssertFalse(commands.launchctlReads.isEmpty, "launchctl was never read.")
    }

    /// The BotFleet Server's own agent is a wrapper that exits as soon as the server
    /// is healthy,  so launchctl cannot say whether the server is still serving:  it
    /// says stopped while the server is up.  The port can say it,  so a stop waits
    /// for the port to close.
    func testStoppingTheServerWaitsForItsPortToClose() async {
        let commands = FleetAdminCommands()
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server)

        await admin.refresh()
        await waitForTheFleet(admin)
        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.services?.states[FleetServiceRegistry.server.id], .stopped,
                       "The server was not reported as stopped.")
        XCTAssertGreaterThan(server.looksAfterTheStop, 0,
                             "The port was never asked whether the server was still there.")
    }

    /// The menu says when it last read the Mac,  so a read has to say when it landed.
    func testAReadPublishesItsTimeAndTheServersThreeStates() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer())
        XCTAssertEqual(admin.localServiceState, .unknown, "An unread server is not a stopped one.")

        await waitForTheFleet(admin)

        XCTAssertNotNil(admin.lastReadAt)
        XCTAssertEqual(admin.localServiceState, .running)
        XCTAssertEqual(admin.pm2Answered, true)
    }

    /// Two routine reads asked for at once are one read:  the second waits for the first
    /// rather than running pm2 and launchctl again beside it.
    func testTwoReadsAskedForAtOnceShareOneRead() async {
        let commands = FleetAdminCommands()
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)
        await admin.refresh()
        commands.listingDelay = 0.2
        let before = commands.calls.filter { $0.arguments.contains("jlist") }.count

        let first = Task { await admin.refresh() }
        let second = Task { await admin.refresh() }
        await first.value
        await second.value

        let reads = commands.calls.filter { $0.arguments.contains("jlist") }.count - before
        XCTAssertEqual(reads, 1, "Two reads asked for at once ran pm2 \(reads) times.")
    }

    /// A read that began before a change is not joined by the read the change asks for:
    /// that caller wants to see what the change did.
    func testAReadAfterAControllerRunIsNotJoinedToAnOlderRead() async {
        let commands = FleetAdminCommands()
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)
        await admin.refresh()
        commands.listingDelay = 0.2
        let before = commands.calls.filter { $0.arguments.contains("jlist") }.count

        let older = Task { await admin.refresh() }
        await Task.yield()
        admin.beginControllerAction(.stop, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: true)
        let after = Task { await admin.refresh() }
        await older.value
        await after.value

        let reads = commands.calls.filter { $0.arguments.contains("jlist") }.count - before
        XCTAssertEqual(reads, 2, "The read after the run joined a read that began before it.")
    }

    /// A controller run with no sheet behind it (a start Muster takes on its own) is a change
    /// too:  the read its end asks for does not join a routine read that began before it.
    func testAReadAfterAnUnattendedRunIsNotJoinedToAnOlderRead() async {
        let commands = FleetAdminCommands()
        let runs = PassthroughSubject<Int, Never>()
        let admin = makeAdmin(commands: commands, server: FleetAdminServer(),
                              runsCompleted: runs.eraseToAnyPublisher())
        await waitForTheFleet(admin)
        await admin.refresh()
        commands.listingDelay = 0.2
        let before = commands.calls.filter { $0.arguments.contains("jlist") }.count

        let older = Task { await admin.refresh() }
        await Task.yield()
        runs.send(1)
        let after = Task { await admin.refresh() }
        await older.value
        await after.value

        let reads = commands.calls.filter { $0.arguments.contains("jlist") }.count - before
        XCTAssertEqual(reads, 2, "The read after an unattended run joined a read that began before it.")
    }

    /// A visit to the panel shortly after a read shows that read and does not read again.
    func testAVisitSoonAfterAReadDoesNotReadAgain() async {
        let commands = FleetAdminCommands()
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)
        await admin.refresh()
        let before = commands.calls.count

        await admin.refreshIfOlder(than: 60)
        XCTAssertEqual(commands.calls.count, before, "A visit a moment after a read read the Mac again.")

        await admin.refreshIfOlder(than: 0)
        XCTAssertGreaterThan(commands.calls.count, before, "A visit after the window did not read.")
    }

    /// A menu that was true at launch is not true days later.
    func testTheFleetIsReadAgainOnATimer() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer(), refreshInterval: 0.05)
        await waitForTheFleet(admin)
        let first = admin.lastReadAt

        let deadline = Date().addingTimeInterval(5)
        while admin.lastReadAt == first, Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }

        XCTAssertNotEqual(admin.lastReadAt, first, "The fleet was not read again.")
    }

    /// A stop that did not land is not a stop the owner made,  and the owner is told.
    func testAStopThatDidNotLandSaysSoAndKeepsNoRecord() async {
        let commands = FleetAdminCommands()
        commands.companionStaysUp = true
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)

        await admin.perform(.stop, serviceID: "app.botfleet.companion")

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.reason, "it still reads running")
        XCTAssertEqual(admin.problem, "BotFleet Companion did not stop on the Mac:  it still reads running")
        XCTAssertEqual(admin.entries.first { $0.id == "app.botfleet.companion" }?.stoppedByOwner, false)
    }

    /// launchctl and the agent do not settle at the same moment,  so a stop that landed is
    /// not read as one that did not,  and the owner's stop stays on the record.
    func testAStopThatLandsAMomentLaterIsAStopAndKeepsTheRecord() async {
        let commands = FleetAdminCommands()
        commands.companionRunning = true
        commands.looksBeforeAChangeShows = 2
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)

        await admin.perform(.stop, serviceID: "app.botfleet.companion")

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
        XCTAssertEqual(admin.entries.first { $0.id == "app.botfleet.companion" }?.stoppedByOwner, true)
    }

    /// A call that did nothing answers nil,  so a caller never mistakes the result of an
    /// older call for its own.
    func testACallThatDidNothingAnswersNilAndNotAnOlderResult() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer())
        await waitForTheFleet(admin)

        let first = await admin.perform(.start, serviceID: "app.botfleet.companion")
        XCTAssertEqual(first?.serviceID, "app.botfleet.companion")

        let none = await admin.perform(.start, serviceID: "no-such-service")
        XCTAssertNil(none)
        // The older result is still there to read,  which is why callers use the answer.
        XCTAssertEqual(admin.lastResult?.serviceID, "app.botfleet.companion")
    }

    func testAStartThatLandsAMomentLaterIsAStart() async {
        let commands = FleetAdminCommands()
        commands.looksBeforeAChangeShows = 2
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)

        await admin.perform(.start, serviceID: "app.botfleet.companion")

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
    }

    /// launchd's view of the wrapper says nothing about the server,  so a restart is not
    /// done until the port answers.
    func testARestartedServerWhosePortStaysClosedIsNotReportedAsRestarted() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.closePort(forLooks: .max)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.problem,
                       "BotFleet Server did not restart on the Mac:  nothing answers on its port on the Mac yet")
    }

    func testARestartedServerWhosePortAnswersIsRestarted() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 3, interval: 0))
        await waitForTheFleet(admin)
        server.closePort(forLooks: 2)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
    }

    /// The wrapper exits at once when something already answers on the port,  so a kickstart
    /// can land and replace nothing.  A port that answers does not make that a restart.
    func testARestartThatLeavesTheSameProcessOnThePortDidNotRestart() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true)
        await waitForTheFleet(admin)
        server.keepItsProcess()
        // The launchctl calls landed and the stop marker went,  so the stop is lifted.
        admin.beginControllerAction(.stop, serviceID: FleetServiceRegistry.server.id)
        admin.endControllerAction(serviceID: FleetServiceRegistry.server.id, succeeded: true)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.notKnown, false)
        XCTAssertEqual(admin.problem,
                       "BotFleet Server did not restart on the Mac:  the same process is still on its port, so it was not replaced")
        XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, false)
    }

    /// The old process can still answer for a moment after the kickstart.
    func testARestartThatTakesAMomentToReplaceTheProcessIsRestarted() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 100, interval: 0.02))
        await waitForTheFleet(admin)
        server.replaceItsProcess(afterLooks: 2)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
    }

    /// Something answers on the port and does not say which process it is.
    func testARestartWhoseNewProcessCannotBeReadIsNotKnown() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true)
        await waitForTheFleet(admin)
        server.stopSayingWhoItIsAtTheKickstart()

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.problem,
                       "Whether BotFleet Server restarted on the Mac is not known:  the process on its port on the Mac could not be read")
    }

    /// With nothing to compare against,  a process that answers afterwards proves nothing:  it
    /// may be the same one,  back from being too busy to answer.
    func testARestartOfAServerThatDidNotSayWhoItWasIsNotKnown() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true)
        await waitForTheFleet(admin)
        server.stopSayingWhoItIs()

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.lastResult?.reason, "the process on its port on the Mac could not be read")
    }

    /// Nothing held the port before,  so whatever answers now is new.
    func testARestartOfAServerThatWasDownIsRestartedOnceAProcessAnswers() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              stopWait: ServerPortWait(attempts: 8, interval: 0))
        await waitForTheFleet(admin)
        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)
        XCTAssertEqual(admin.lastResult?.succeeded, true)
        server.closePort(forLooks: 1)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
    }

    /// launchctl could not be asked after the kickstart,  which is not a restart that was
    /// refused,  and the kickstart may have landed.
    func testARestartWhoseWrapperCannotBeListedAfterTheKickstartIsNotKnown() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true)
        await waitForTheFleet(admin)
        admin.beginControllerAction(.stop, serviceID: FleetServiceRegistry.server.id)
        admin.endControllerAction(serviceID: FleetServiceRegistry.server.id, succeeded: true)
        commands.listingFailsAfterAKickstart = true

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.problem,
                       "Whether BotFleet Server restarted on the Mac is not known:  launchctl could not be asked after the restart")
        XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, false,
                       "what the owner asked for stays on the record")
    }

    /// A bootout that landed and a listing that then cannot be read says nothing about the
    /// agent,  so it is not a stop that did not land.
    func testAStopWhoseAgentCannotBeListedAfterwardsIsNotKnownAndKeepsTheRecord() async {
        let commands = FleetAdminCommands()
        commands.companionRunning = true
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)
        commands.listingFails = true

        await admin.perform(.stop, serviceID: "app.botfleet.companion")

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.problem,
                       "Whether BotFleet Companion stopped on the Mac is not known:  its state on the Mac could not be read")
        XCTAssertEqual(admin.entries.first { $0.id == "app.botfleet.companion" }?.stoppedByOwner, true)
    }

    /// The server can go before it answers the stop,  so a stop whose answer was lost and whose
    /// port then closed is a stop.
    func testAStopWhoseAnswerWasLostAndWhosePortClosedIsAStop() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server,
                              stopWait: ServerPortWait(attempts: 8, interval: 0))
        await waitForTheFleet(admin)
        server.loseTheStopAnswer(stopLands: true)

        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
        XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, true)
    }

    /// The answer was lost and the port is still held,  which is not proof either way:  the
    /// server may be taking its time.
    func testAStopWhoseAnswerWasLostAndWhosePortStaysHeldIsNotKnownAndKeepsTheRecord() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server,
                              stopWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.loseTheStopAnswer(stopLands: false)

        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.problem,
                       "Whether BotFleet Server stopped on the Mac is not known:  BotFleet Server did not answer, so the change may or may not have been made.\u{00A0} Refresh before trying again.")
        XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, true)
    }

    /// The launchctl calls landed and the stop marker went,  so the wrapper may bring the
    /// server up:  the row does not go back to saying it stays stopped.
    func testARestartOrStartWhosePortStaysClosedLiftsTheStop() async {
        for action in [ManagedServiceAction.restart, .start] {
            let commands = FleetAdminCommands()
            commands.serverWrapperRuns = true
            let server = FleetAdminServer()
            let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                                  startWait: ServerPortWait(attempts: 2, interval: 0))
            await waitForTheFleet(admin)
            admin.beginControllerAction(.stop, serviceID: FleetServiceRegistry.server.id)
            admin.endControllerAction(serviceID: FleetServiceRegistry.server.id, succeeded: true)
            server.closePort(forLooks: .max)

            await admin.perform(action, serviceID: FleetServiceRegistry.server.id)

            XCTAssertEqual(admin.lastResult?.reason, "nothing answers on its port on the Mac yet")
            XCTAssertEqual(admin.lastResult?.notKnown, false)
            XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, false)
        }
    }

    /// A start has its own budget:  a server slower to come up than a port is to close is
    /// not a start that failed.
    func testAStartIsGivenItsOwnBudgetAndNotTheStopWait() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server, wrapperInstalled: true,
                              stopWait: ServerPortWait(attempts: 1, interval: 0),
                              startWait: ServerPortWait(attempts: 6, interval: 0))
        await waitForTheFleet(admin)
        server.closePort(forLooks: 4)

        await admin.perform(.start, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
    }

    func testAStartThatNeverAnswersSaysSo() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.closePort(forLooks: .max)

        await admin.perform(.start, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.reason, "nothing answers on its port on the Mac yet")
    }

    /// A probe that could not tell is not a port that answered,  and not one still held.
    func testARestartWhosePortCannotBeReadIsNotKnown() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.portCannotBeRead()

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, false)
        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.problem,
                       "Whether BotFleet Server restarted on the Mac is not known:  its port on the Mac could not be read")
    }

    func testAStopWhosePortCannotBeReadIsNotKnown() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server,
                              stopWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.portCannotBeRead()

        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.notKnown, true)
        XCTAssertEqual(admin.lastResult?.reason, "its port on the Mac could not be read")
        XCTAssertTrue(admin.problemIsNotKnown)
        // The stop may have landed,  so what the owner asked for stays on the record.
        XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, true)
    }

    /// A port still held is known,  so it stays a stop that did not land.
    func testAStopWhosePortStaysOpenDidNotLand() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server,
                              stopWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.closePort(forLooks: 0) // Answers on every look,  even after the stop.

        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.notKnown, false)
        XCTAssertEqual(admin.problem, "BotFleet Server did not stop on the Mac:  its port on the Mac is still answering")
        XCTAssertFalse(admin.problemIsNotKnown)
        XCTAssertEqual(admin.entries.first { $0.id == FleetServiceRegistry.server.id }?.stoppedByOwner, false)
    }

    /// The flag goes with the sentence it describes,  so the next problem does not inherit it.
    func testANotKnownFlagDoesNotOutliveItsProblem() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server,
                              stopWait: ServerPortWait(attempts: 2, interval: 0))
        await waitForTheFleet(admin)
        server.portCannotBeRead()
        await admin.perform(.stop, serviceID: FleetServiceRegistry.server.id)
        XCTAssertTrue(admin.problemIsNotKnown)

        admin.beginControllerAction(.start, serviceID: "fixture-service")

        XCTAssertNil(admin.problem)
        XCTAssertFalse(admin.problemIsNotKnown)
    }

    /// The port and the process share one wait,  so a restart holds the row for one budget.
    func testARestartLooksForTheProcessWithinThePortsBudget() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 8, interval: 0.1))
        await waitForTheFleet(admin)
        server.keepItsProcess()
        // The port takes most of the budget to answer,  which leaves a few looks.
        server.closePort(forLooks: 7)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.reason, "the same process is still on its port, so it was not replaced")
        // A second budget of its own would make nine looks,  and the read that follows adds one.
        XCTAssertLessThan(server.looksAtTheProcessAfterAKickstart, 7)
    }

    /// A port opens before the server behind it can answer,  so one that answered at the very
    /// end of the wait is not judged on a single look.
    func testAPortThatAnswersLateStillLeavesTimeToReadTheNewProcess() async {
        let commands = FleetAdminCommands()
        commands.serverWrapperRuns = true
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: commands, server: server, wrapperInstalled: true,
                              startWait: ServerPortWait(attempts: 3, interval: 0.4))
        await waitForTheFleet(admin)
        // Every look in the budget finds the port closed,  and the last one finds it open.
        server.closePort(forLooks: 3)
        server.staySilentAfterTheKickstart(forLooks: 1)

        await admin.perform(.restart, serviceID: FleetServiceRegistry.server.id)

        XCTAssertEqual(admin.lastResult?.succeeded, true)
        XCTAssertNil(admin.problem)
    }

    func testTheLateGraceIsAFractionOfTheWaitAndNothingWithoutOne() {
        XCTAssertEqual(ServerPortWait.start.lateGrace, 5)
        XCTAssertEqual(ServerPortWait(attempts: 8, interval: 0).lateGrace, 0)
    }

    /// A look can cost launchctl's whole timeout,  so the budget is time and not a number of
    /// looks:  each look here costs half the budget,  so a count of eight looks would run all
    /// eight,  and the read that follows the action adds only a few.
    func testAnAgentWhoseLooksAreSlowGivesUpWithinItsBudget() async {
        let commands = FleetAdminCommands()
        commands.companionStaysUp = true
        let admin = makeAdmin(commands: commands, server: FleetAdminServer())
        await waitForTheFleet(admin)
        commands.listingDelay = 0.2
        let before = commands.launchctlReads.count

        await admin.perform(.stop, serviceID: "app.botfleet.companion")

        XCTAssertEqual(admin.lastResult?.reason, "it still reads running")
        XCTAssertLessThan(commands.launchctlReads.count - before, 9)
    }

    func testAStartWaitsAsLongAsAnyOtherServiceAndLongerThanAStop() {
        XCTAssertEqual(Double(ServerPortWait.start.attempts) * ServerPortWait.start.interval, 20)
        XCTAssertLessThan(Double(ServerPortWait.stop.attempts) * ServerPortWait.stop.interval, 20)
    }

    /// The stop alert reads the work in flight now,  and an answer that did not come is not
    /// the same as nothing in flight.
    func testTheStopConfirmationReadsWhatIsInFlightNow() async {
        let server = FleetAdminServer()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: server)
        await waitForTheFleet(admin)

        let body = await admin.stopConfirmation(for: FleetServiceRegistry.server.id)
        XCTAssertTrue(body.contains("Checked just now:  nothing is in flight."), body)
        XCTAssertTrue(body.contains("on the Mac"), body)

        let other = await admin.stopConfirmation(for: "shellular")
        XCTAssertFalse(other.contains("in flight"), other)
    }

    // MARK: - Panel actions on the controller path

    /// A stop run through the plan sheet is still a stop the owner asked for, so the
    /// record has to carry it once the run succeeded.
    func testAControllerStopThatRanIsRecordedAsAnOwnerStop() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer())
        await waitForTheFleet(admin)

        admin.beginControllerAction(.stop, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: true)
        await admin.refresh()

        let entry = admin.entries.first { $0.id == "shellular" }
        XCTAssertEqual(entry?.stoppedByOwner, true,
                       "A controller stop that ran was not recorded as an owner stop.")
    }

    /// A controller action that never ran, or one the helper refused, changes no
    /// intent: the record goes back to what it was.
    func testAControllerActionThatDidNotRunLeavesTheRecordAlone() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer())
        await waitForTheFleet(admin)

        admin.beginControllerAction(.stop, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: false)
        await admin.refresh()

        let entry = admin.entries.first { $0.id == "shellular" }
        XCTAssertEqual(entry?.stoppedByOwner, false,
                       "A controller stop that never ran was recorded as an owner stop.")
    }

    /// Starting a service again from the panel clears a stop recorded any other way.
    func testAControllerStartClearsAnEarlierOwnerStop() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer())
        await waitForTheFleet(admin)

        admin.beginControllerAction(.stop, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: true)
        admin.beginControllerAction(.start, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: true)
        await admin.refresh()

        let entry = admin.entries.first { $0.id == "shellular" }
        XCTAssertEqual(entry?.stoppedByOwner, false,
                       "A controller start left an owner stop behind.")
    }

    // MARK: - The test rig

    /// Waits for the admin to have read the fleet.
    ///
    /// The admin reads the fleet as it comes up,  and a read that is still in
    /// flight when another one starts gives way to it,  so the test waits for
    /// the fleet to be there rather than for a particular read to have ended.
    private func waitForTheFleet(_ admin: FleetAdmin) async {
        let deadline = Date().addingTimeInterval(5)
        while admin.services == nil, Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    /// An admin that talks to fakes rather than to this Mac.
    private func makeAdmin(commands: FleetAdminCommands,
                            server: FleetAdminServer,
                            refreshInterval: TimeInterval? = nil,
                            wrapperInstalled: Bool = false,
                            stopWait: ServerPortWait = .stop,
                            startWait: ServerPortWait = ServerPortWait(attempts: 8, interval: 0),
                            ownerStopStore: OwnerStopStoring? = nil,
                            runsCompleted: AnyPublisher<Int, Never>? = nil) -> FleetAdmin {
        // The startup policy looks for the installed wrapper and the launch agents
        // under the home it is given,  so a home of its own keeps the test away
        // from the real Mac.
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("fleet-admin-tests-\(UUID().uuidString)", isDirectory: true)
        let agents = home.appendingPathComponent("Library/LaunchAgents", isDirectory: true)
        try? FileManager.default.createDirectory(at: agents, withIntermediateDirectories: true)
        for label in ["app.botfleet.server", "app.botfleet.companion"] {
            FileManager.default.createFile(atPath: agents.appendingPathComponent("\(label).plist").path,
                                           contents: Data("<plist/>".utf8))
        }
        if wrapperInstalled {
            // A wrapper that can carry the startup policy,  which a start or a restart of
            // the server needs.
            let apps = home.appendingPathComponent("apps", isDirectory: true)
            try? FileManager.default.createDirectory(at: apps, withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: apps.appendingPathComponent("botfleet-server-start.sh").path,
                                           contents: Data("BF_CONTROL_STARTUP_POLICY_V1".utf8))
        }

        commands.onKickstart = { server.kickstarted() }
        let startup = StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
        let api = ControlAPI(session: FleetAdminServer.session(),
                             baseURL: URL(string: "http://127.0.0.1:\(server.port)")!)
        let control = FleetServiceControl(
            pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2"),
            startup: startup,
            mcpServer: nil,
            stopServer: { try await api.stopServer() },
            agentSettleInterval: 0.05)
        return FleetAdmin(control: control,
                          startup: startup,
                          presence: ServerPresenceProbe(port: server.port, connect: { _ in server.presence() }),
                          api: api,
                          stopWait: stopWait,
                          startWait: startWait,
                          ownerStopStore: ownerStopStore,
                          refreshInterval: refreshInterval,
                          runsCompleted: runsCompleted ?? Empty<Int, Never>().eraseToAnyPublisher())
    }

    // MARK: - The owner's stops outlive the app

    /// A stop that ran is written to the store,  and an admin that comes up later reads it
    /// back,  so Muster never starts a service the owner stopped.
    func testAnOwnerStopIsKeptAndComesBackAtTheNextLaunch() async {
        let store = FakeOwnerStopStore()
        let first = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer(), ownerStopStore: store)
        await waitForTheFleet(first)
        first.beginControllerAction(.stop, serviceID: "shellular")
        first.endControllerAction(serviceID: "shellular", succeeded: true)
        XCTAssertEqual(store.saved?.ids, ["shellular"])
        XCTAssertNotNil(store.saved?.stoppedAt["shellular"])

        let second = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer(), ownerStopStore: store)
        await waitForTheFleet(second)
        let entry = second.entries.first { $0.id == "shellular" }
        XCTAssertEqual(entry?.stoppedByOwner, true)
        XCTAssertNotNil(entry?.stoppedAt)
        XCTAssertTrue(second.isStoppedByOwner("shellular"))
    }

    /// A stop whose sheet is still open is not a stop yet,  so it is not written.
    func testAStopThatIsStillOnTheSheetIsNotWrittenYet() async {
        let store = FakeOwnerStopStore()
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer(), ownerStopStore: store)
        await waitForTheFleet(admin)
        admin.beginControllerAction(.stop, serviceID: "shellular")
        XCTAssertNil(store.saved)
        // Muster still treats it as the owner's,  so nothing starts it meanwhile.
        XCTAssertTrue(admin.isStoppedByOwner("shellular"))
        admin.endControllerAction(serviceID: "shellular", succeeded: false)
        XCTAssertFalse(admin.isStoppedByOwner("shellular"))
        XCTAssertEqual(store.saved?.ids, Set<String>())
    }

    /// A record that cannot be read blocks everything and is never written over.
    func testARecordThatCannotBeReadIsNeverWrittenOver() async {
        let store = FakeOwnerStopStore()
        store.loaded = .unreadable("Muster could not read its record.")
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer(), ownerStopStore: store)
        await waitForTheFleet(admin)
        XCTAssertTrue(admin.ownerStopsUnreadable)
        XCTAssertEqual(admin.ownerStopProblem, "Muster could not read its record.")
        admin.beginControllerAction(.stop, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: true)
        XCTAssertNil(store.saved)
    }

    /// A stop that could not be written is said out loud.
    func testAStopThatCouldNotBeWrittenIsSaid() async {
        let store = FakeOwnerStopStore()
        store.saveProblem = "Muster could not save your stop."
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer(), ownerStopStore: store)
        await waitForTheFleet(admin)
        admin.beginControllerAction(.stop, serviceID: "shellular")
        admin.endControllerAction(serviceID: "shellular", succeeded: true)
        XCTAssertEqual(admin.ownerStopProblem, "Muster could not save your stop.")
    }

    func testNotesAboutWhatMusterDidReachTheServices() async {
        let admin = makeAdmin(commands: FleetAdminCommands(), server: FleetAdminServer())
        await waitForTheFleet(admin)
        let note = ReconcileNote(.gaveUp(tries: 3))
        admin.setReconcileNotes(["shellular": note])
        XCTAssertEqual(admin.entries.first { $0.id == "shellular" }?.reconcileNote, note)
        // A read keeps them.
        await admin.refresh()
        XCTAssertEqual(admin.entries.first { $0.id == "shellular" }?.reconcileNote, note)
    }
}

/// An owner-stop store that lives in memory.
private final class FakeOwnerStopStore: OwnerStopStoring {
    var saved: OwnerStops?
    var loaded: OwnerStopLoad?
    var saveProblem: String?

    func load() -> OwnerStopLoad {
        if let loaded { return loaded }
        return .stops(saved ?? OwnerStops())
    }

    func save(_ stops: OwnerStops) -> String? {
        if saveProblem == nil { saved = stops }
        return saveProblem
    }
}

/// launchctl could not be asked.
private struct FleetAdminListingFailed: Error {}

/// One command the admin ran.
private struct FleetAdminCall {
    let executable: String
    let arguments: [String]
    /// True when the command ran on the main thread.
    let onMainThread: Bool
}

/// The commands the admin runs.
///
/// Only the two-argument form is answered,  because every other form of the command
/// protocol ends there.
private final class FleetAdminCommands: CommandRunning {
    /// Every command that was run.
    ///
    /// The admin reads off the main thread,  so the recording is guarded:  an
    /// array that two threads write at once is not an array any more.
    private let lock = NSLock()
    private var recorded: [FleetAdminCall] = []
    /// A boot-session identifier made up for this run.
    private let boot = UUID().uuidString
    /// True when launchd keeps reporting the companion running,  whatever it is asked.
    var companionStaysUp = false
    /// True when launchd has a process behind the companion's agent.
    var companionRunning = false
    /// True when launchd has a process behind the server's wrapper.
    var serverWrapperRuns = false
    /// How many listings still show the companion as it was after launchd is asked to
    /// change it.
    var looksBeforeAChangeShows = 0
    /// How long launchctl takes to answer a listing.
    var listingDelay: TimeInterval = 0
    /// True when launchctl cannot be asked for a listing.
    var listingFails = false
    /// True when launchctl stops answering listings once a kickstart has gone through.
    var listingFailsAfterAKickstart = false
    /// Told about every kickstart,  which is what the server sees of a restart.
    var onKickstart: (() -> Void)?
    /// The change launchd was asked for,  and how many more listings until it shows.
    private var pendingChange: (running: Bool, looks: Int)?

    var calls: [FleetAdminCall] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    /// The launchctl reads,  which are the ones that block.
    var launchctlReads: [FleetAdminCall] {
        calls.filter { $0.executable == "/bin/launchctl" && $0.arguments == ["list"] }
    }

    func run(_ executable: String, _ arguments: [String]) throws -> String {
        if arguments == ["list"], listingDelay > 0 { Thread.sleep(forTimeInterval: listingDelay) }
        lock.lock()
        recorded.append(FleetAdminCall(executable: executable,
                                       arguments: arguments,
                                       onMainThread: Thread.isMainThread))
        defer { lock.unlock() }
        if executable == "/bin/launchctl" {
            if arguments.first == "bootout" {
                pendingChange = (running: false, looks: looksBeforeAChangeShows)
            } else if arguments.first == "kickstart" {
                pendingChange = (running: true, looks: looksBeforeAChangeShows)
                if listingFailsAfterAKickstart { listingFails = true }
                onKickstart?()
            }
        }
        if executable == "/bin/launchctl", arguments == ["list"] {
            if listingFails { throw FleetAdminListingFailed() }
            if let change = pendingChange {
                if change.looks > 0 {
                    pendingChange = (running: change.running, looks: change.looks - 1)
                } else {
                    companionRunning = change.running
                    pendingChange = nil
                }
            }
            // By default neither the server's agent nor the companion's has a process
            // behind it,  so launchd reports both as stopped.
            var rows = ["PID\tStatus\tLabel", "-\t0\tother.service"]
            if companionStaysUp || companionRunning { rows.append("4242\t0\tapp.botfleet.companion") }
            if serverWrapperRuns { rows.append("4243\t0\tapp.botfleet.server") }
            return rows.joined(separator: "\n")
        }
        if executable == "/usr/sbin/sysctl" { return boot }
        if arguments.contains("jlist") { return "[]" }
        return ""
    }
}

/// The BotFleet Server the admin talks to,  and the port it listens on.
///
/// Every server has a port of its own,  so a request that arrives late finds the
/// server it was meant for and not whichever one the rig happens to be on.
final class FleetAdminServer {
    /// The servers the rig knows about,  by port.
    private static let lock = NSLock()
    private static var servers: [Int: FleetAdminServer] = [:]
    /// The port the next server is given.
    private static var lastPort = 21000

    /// The port this server answers on.
    let port: Int

    private let state = NSLock()
    private var stoppedNow = false
    private var looks = 0
    private var work: String?
    /// How many more looks find the port closed.  Nil when the port follows the server.
    private var closedLooks: Int?
    /// True when the probe cannot tell.
    private var unreadable = false
    /// The process behind the port,  as the server's health says it.
    private var pid = 4242
    /// True when a kickstart replaces nothing.
    private var keepsProcess = false
    /// How many looks the old process still answers after a kickstart.
    private var slowLooks = 0
    /// The looks left before a slow kickstart's new process answers.  Nil when none is coming.
    private var looksBeforeReplaced: Int?
    /// True when the health check does not answer.
    private var saysNothing = false
    /// True when the health check stops answering at the next kickstart.
    private var saysNothingAfterKickstart = false
    /// True when the stop's answer is lost on the way back.
    private var losesStopAnswer = false
    /// True when a stop whose answer is lost still reaches the server.
    private var lostStopLands = false
    /// True once launchd was asked to run the wrapper.
    private var wasKickstarted = false
    /// How many times the health check named the process since the kickstart.
    private var processLooks = 0
    /// How many health checks after the kickstart still get no answer.
    private var silentLooks = 0

    /// How many times the health check named the process since the kickstart.
    var looksAtTheProcessAfterAKickstart: Int {
        state.lock()
        defer { state.unlock() }
        return processLooks
    }

    /// The `blockingWork` value the server's status carries,  as JSON,  or nil for none.
    var blockingWorkJSON: String? {
        get {
            state.lock()
            defer { state.unlock() }
            return work
        }
        set {
            state.lock()
            work = newValue
            state.unlock()
        }
    }

    /// True once the server has been asked to stop.
    var stopped: Bool {
        state.lock()
        defer { state.unlock() }
        return stoppedNow
    }

    /// How many times the port was asked after the stop was requested.
    var looksAfterTheStop: Int {
        state.lock()
        defer { state.unlock() }
        return looks
    }

    init() {
        FleetAdminServer.lock.lock()
        FleetAdminServer.lastPort += 1
        self.port = FleetAdminServer.lastPort
        FleetAdminServer.servers[self.port] = self
        FleetAdminServer.lock.unlock()
    }

    /// The server that answers on a port.
    static func server(for port: Int) -> FleetAdminServer? {
        lock.lock()
        defer { lock.unlock() }
        return servers[port]
    }

    /// The server has been asked to stop.
    func requestStop() {
        state.lock()
        stoppedNow = true
        state.unlock()
    }

    /// Nothing answers on the port for this many looks,  and then the server does.
    func closePort(forLooks count: Int) {
        state.lock()
        closedLooks = count
        state.unlock()
    }

    /// Every look from now on cannot tell.
    func portCannotBeRead() {
        state.lock()
        unreadable = true
        state.unlock()
    }

    /// A kickstart will replace nothing.
    func keepItsProcess() {
        state.lock()
        keepsProcess = true
        state.unlock()
    }

    /// A kickstart's new process answers only after the old one has answered this many looks.
    func replaceItsProcess(afterLooks looks: Int) {
        state.lock()
        slowLooks = looks
        state.unlock()
    }

    /// The health check does not answer from now on.
    func stopSayingWhoItIs() {
        state.lock()
        saysNothing = true
        state.unlock()
    }

    /// The health check stops answering at the next kickstart.
    func stopSayingWhoItIsAtTheKickstart() {
        state.lock()
        saysNothingAfterKickstart = true
        state.unlock()
    }

    /// The next stop's answer is lost on the way back,  whether or not the stop landed.
    func loseTheStopAnswer(stopLands: Bool) {
        state.lock()
        losesStopAnswer = true
        lostStopLands = stopLands
        state.unlock()
    }

    /// launchd was asked to run the wrapper.
    func kickstarted() {
        state.lock()
        defer { state.unlock() }
        stoppedNow = false
        wasKickstarted = true
        if saysNothingAfterKickstart { saysNothing = true }
        guard !keepsProcess else { return }
        if slowLooks > 0 {
            looksBeforeReplaced = slowLooks
        } else {
            pid += 1
        }
    }

    /// True when a stop request should see its connection drop,  and asks the server to stop
    /// first when the stop gets there anyway.
    func dropsTheStop() -> Bool {
        state.lock()
        defer { state.unlock() }
        guard losesStopAnswer else { return false }
        if lostStopLands { stoppedNow = true }
        return true
    }

    /// The first health checks after the kickstart get no answer.
    func staySilentAfterTheKickstart(forLooks looks: Int) {
        state.lock()
        silentLooks = looks
        state.unlock()
    }

    /// True when the health check does not answer on this look.
    func isSilentOnThisLook() -> Bool {
        state.lock()
        defer { state.unlock() }
        if wasKickstarted, silentLooks > 0 {
            silentLooks -= 1
            return true
        }
        return stoppedNow || saysNothing
    }

    /// The process the health check names on this look.
    func nextPID() -> Int {
        state.lock()
        defer { state.unlock() }
        if wasKickstarted { processLooks += 1 }
        if let looks = looksBeforeReplaced {
            if looks == 0 {
                pid += 1
                looksBeforeReplaced = nil
            } else {
                looksBeforeReplaced = looks - 1
            }
        }
        return pid
    }

    /// What the port says.
    ///
    /// The server is up until it is asked to stop,  and it lets go of the port a
    /// moment later:  the first look after the stop still finds it,  and the second
    /// does not.
    func presence() -> ServerPresence {
        state.lock()
        defer { state.unlock() }
        if unreadable { return .unknown }
        if let closed = closedLooks {
            guard closed > 0 else { return .present }
            closedLooks = closed - 1
            return .absent
        }
        guard stoppedNow else { return .present }
        looks += 1
        return looks >= 2 ? .absent : .present
    }

    /// A session that answers like the server does.
    static func session() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FleetAdminURLProtocol.self]
        return URLSession(configuration: configuration)
    }
}

/// The answers the BotFleet Server gives.
final class FleetAdminURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let port = request.url?.port ?? 0
        let server = FleetAdminServer.server(for: port)
        let path = request.url?.path ?? ""
        var status = 200
        var body = "{}"
        switch path {
        case "/api/health":
            if server?.isSilentOnThisLook() == true {
                status = 503
            } else {
                body = #"{"app":"botfleet","pid":\#(server?.nextPID() ?? 4242),"ready":true}"#
            }
        case "/api/system/control":
            let work = server?.blockingWorkJSON.map { #","blockingWork":\#($0)"# } ?? ""
            body = #"{"activeBotCount":0,"localVms":[],"persistOnExit":true\#(work)}"#
        case "/api/system/control/stop":
            if server?.dropsTheStop() == true {
                client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
                return
            }
            server?.requestStop()
            body = #"{"ok":true}"#
        default:
            body = "{}"
        }
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: status,
                                             httpVersion: nil, headerFields: nil) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
