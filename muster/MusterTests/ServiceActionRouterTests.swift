import XCTest

/// Every action case,  and which machine it reaches.  A new case cannot compile without a
/// sample,  so this covers it the day it is added.
final class OpsActionPlaceTests: XCTestCase {
    private func builder() -> OpsCommandBuilder {
        OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/example/repo"),
                          home: URL(fileURLWithPath: "/example/home"),
                          sshAuthSocket: nil)
    }

    func testSshIsReachedExactlyWhenTheActionRunsOnTheServer() throws {
        for tag in OpsActionCase.allCases {
            let action = tag.sample
            XCTAssertEqual(action.caseTag, tag)
            let invocation = try builder().invocation(for: action, mode: .plan)
            XCTAssertEqual(invocation.executable == OpsControlRoute.ssh, action.runsOn == .server, "\(tag)")
            XCTAssertEqual(action.runsOnServer, action.runsOn == .server, "\(tag)")
            XCTAssertEqual(builder().requiredFile(for: action) == nil, action.runsOn == .server, "\(tag)")
        }
    }

    func testEveryTitleNamesThePlaceAndNeverAnOldWord() {
        for tag in OpsActionCase.allCases {
            let action = tag.sample
            XCTAssertTrue(action.title.contains(action.changes.inProse), action.title)
            for word in ["this Mac", "Coolify", "Hetzner"] {
                XCTAssertFalse(action.title.contains(word), action.title)
            }
        }
    }

    func testAKnobRunsOnTheMacAndChangesTheWholeFleet() {
        let knob = OpsActionCase.knobSet.sample
        XCTAssertEqual(knob.runsOn, .mac)
        XCTAssertEqual(knob.changes, .acrossFleet)
        XCTAssertEqual(knob.title, "Set FLEET_X to \"1\" for the whole fleet")
        XCTAssertEqual(knob.placeSentence, "Runs on the Mac.\u{00A0} Changes a value every machine reads.")
        XCTAssertEqual(knob.runButtonTitle, "Change For The Whole Fleet With Touch ID")
        XCTAssertEqual(OpsActionCase.knobUnset.sample.changes, .acrossFleet)
    }

    func testTheSheetsWordsComeFromTheSameValueAsTheRoute() {
        let server = OpsAction.serverUnit(verb: .restart, unit: OpsNames.reportTimerUnit)
        XCTAssertEqual(server.title, "Restart Server Report Timer on the server")
        XCTAssertEqual(server.placeSentence, "Runs on the server.")
        XCTAssertEqual(server.runButtonTitle, "Run On The Server With Touch ID")
        let mac = OpsAction.pm2App(verb: .restart, name: "shellular")
        XCTAssertEqual(mac.title, "Restart Shellular on the Mac")
        XCTAssertEqual(mac.placeSentence, "Runs on the Mac.")
        XCTAssertEqual(mac.runButtonTitle, "Run On The Mac With Touch ID")
        XCTAssertEqual(OpsAction.coolifyRedeploy(uuid: "abc123def456", name: "muster-web").title,
                       "Redeploy muster-web on the server")
    }

    func testFetchingTheReportSaysItRunsOnTheMacAndAsksTheServer() {
        let fetch = OpsAction.macAgent(verb: .restart, label: OpsNames.fetchAgentLabel)
        XCTAssertEqual(fetch.placeSentence, "Runs on the Mac.\u{00A0} Asks the server for its report.")
        XCTAssertEqual(fetch.runsOn, .mac)
    }

    func testAStopCarriesItsWarningToThePlan() {
        XCTAssertEqual(OpsAction.pm2App(verb: .stop, name: "shellular").stopWarning, FleetServices.pm2StopBody)
        XCTAssertEqual(OpsAction.macAgent(verb: .stop, label: "com.jay.x").stopWarning, FleetServices.launchAgentStopBody)
        XCTAssertNil(OpsAction.pm2App(verb: .restart, name: "shellular").stopWarning)
        XCTAssertTrue(OpsAction.serverUnit(verb: .stop, unit: OpsNames.reportTimerUnit).stopWarning?.contains("45 minutes") == true)
    }

    // MARK: - The answer names its machine

    private func parse(_ json: String, mode: OpsMode, action: OpsAction) -> OpsOutcome {
        OpsResultParser.parse(OpsProcessResult(exitCode: 0, stdout: json, stderr: ""),
                              mode: mode, action: action, timeout: 90)
    }

    private let timerRestart = OpsAction.serverUnit(verb: .restart, unit: "fleet-server-status.timer")

    func testAnAnswerFromTheOtherMachineIsRefusedOnAPlan() {
        let outcome = parse(#"{"ok":true,"result":"planned","commands":[["x"]],"host":"mac"}"#,
                            mode: .plan, action: timerRestart)
        XCTAssertEqual(outcome.kind, .refused)
        XCTAssertFalse(outcome.isSuccess(for: .plan))
        XCTAssertEqual(outcome.answeredFrom, .mac)
        XCTAssertEqual(outcome.detail, "The answer came from the Mac, but this button names the server, so nothing will run.")
    }

    func testAnAnswerFromTheOtherMachineIsRefusedOnARun() {
        let outcome = parse(#"{"ok":true,"result":"done","commands":[["x"]],"host":"mac"}"#,
                            mode: .yes, action: timerRestart)
        XCTAssertEqual(outcome.kind, .refused)
        XCTAssertFalse(outcome.isSuccess(for: .yes))
        XCTAssertTrue(outcome.detail.contains("this button names the server"))
    }

    func testAnAnswerNamingAMachineThisAppDoesNotKnowIsRefused() {
        let outcome = parse(#"{"ok":true,"result":"planned","commands":[["x"]],"host":"laptop"}"#,
                            mode: .plan, action: timerRestart)
        XCTAssertEqual(outcome.kind, .refused)
        XCTAssertNil(outcome.answeredFrom)
    }

    func testAMissingHostIsNotAnError() {
        let outcome = parse(#"{"ok":true,"result":"planned","commands":[["x"]]}"#, mode: .plan, action: timerRestart)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertNil(outcome.answeredFrom)
    }

    func testAMatchingHostIsRecorded() {
        let outcome = parse(#"{"ok":true,"result":"planned","commands":[["x"]],"host":"server"}"#,
                            mode: .plan, action: timerRestart)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertEqual(outcome.answeredFrom, .server)
    }

    func testAKnobAnswerIsNotCheckedForAHost() {
        let outcome = parse(#"{"ok":true,"result":"planned","commands":[["x"]],"host":"server"}"#,
                            mode: .plan, action: OpsActionCase.knobSet.sample)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertNil(outcome.answeredFrom)
    }

    func testAPlanThatCouldNotReachTheServerSaysNothingRan() {
        let silent = OpsProcessResult(exitCode: 255, stdout: "", stderr: "ssh: connect to host: Connection timed out")
        XCTAssertEqual(OpsResultParser.parse(silent, mode: .plan, action: timerRestart, timeout: 90).detail,
                       "The server did not answer, so nothing ran.")
        XCTAssertEqual(OpsResultParser.parse(silent, mode: .yes, action: timerRestart, timeout: 90).detail,
                       "The server did not answer, so the Mac cannot say whether this ran.")
        let lookup = OpsProcessResult(exitCode: 255, stdout: "", stderr: "ssh: Could not resolve hostname fleet-ops-ctl")
        XCTAssertEqual(OpsResultParser.parse(lookup, mode: .plan, action: timerRestart, timeout: 90).detail,
                       "The Mac could not look up the server.\u{00A0} Check the Mac's network, then the fleet-ops-ctl alias in ~/.ssh/config.")
    }

    // MARK: - The host on the services

    func testEveryRegistryServiceHasOneHostAndEveryMenuItemCarriesIt() {
        for descriptor in FleetServiceRegistry.all {
            XCTAssertEqual(descriptor.host, .mac, descriptor.id)
        }
        let services = FleetServices(serverState: .running, states: [:])
        XCTAssertTrue(services.menuItems.allSatisfy { $0.host == .mac })
    }

    func testAHostWordIsOnlyEverTheTwoExactWords() {
        XCTAssertEqual(FleetHost(reportValue: "mac"), .mac)
        XCTAssertEqual(FleetHost(reportValue: "server"), .server)
        XCTAssertNil(FleetHost(reportValue: "Mac"))
        XCTAssertNil(FleetHost(reportValue: ""))
        XCTAssertNil(FleetHost(reportValue: nil))
    }
}

/// The router:  one action,  one gate,  from either surface,  with no panel on screen.
@MainActor
final class ServiceActionRouterTests: XCTestCase {
    private static let planned = #"{"ok":true,"result":"planned","commands":[["pm2","stop","shellular"]],"detail":"plan"}"#
    private static let done = #"{"ok":true,"result":"done","commands":[["pm2","stop","shellular"]],"detail":"ran"}"#
    private static let failed = #"{"ok":false,"result":"failed","commands":[["pm2","stop","shellular"]],"detail":"pm2 said no"}"#

    private static let allowlist = OpsAllowlist(pm2Apps: [
        OpsAllowlist.Entry(name: "shellular", actions: ["start", "stop", "restart"]),
    ])

    private struct Rig {
        let server: FleetAdminServer
        let admin: FleetAdmin
        let controller: OpsController
        let router: ServiceActionRouter
        let confirmations: ConfirmationLog
        let notices: RouterNotices
        let runner: RouterRunner
    }

    private final class ConfirmationLog {
        var requests: [ServiceActionRouter.ConfirmRequest] = []
        var answer = true
    }

    /// Everything real but the processes,  and no view anywhere.
    private func makeRig(yes: String = ServiceActionRouterTests.done, exit: Int32 = 0) async -> Rig {
        let server = FleetAdminServer()
        let commands = RouterCommands()
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("router-tests-\(UUID().uuidString)", isDirectory: true)
        let agents = home.appendingPathComponent("Library/LaunchAgents", isDirectory: true)
        try? FileManager.default.createDirectory(at: agents, withIntermediateDirectories: true)
        for label in ["app.botfleet.server", "app.botfleet.companion"] {
            FileManager.default.createFile(atPath: agents.appendingPathComponent("\(label).plist").path,
                                           contents: Data("<plist/>".utf8))
        }
        let startup = StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
        let api = ControlAPI(session: FleetAdminServer.session(),
                             baseURL: URL(string: "http://127.0.0.1:\(server.port)")!)
        let control = FleetServiceControl(
            pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2"),
            startup: startup,
            mcpServer: nil,
            stopServer: { try await api.stopServer() })
        let admin = FleetAdmin(control: control,
                               startup: startup,
                               presence: ServerPresenceProbe(port: server.port, connect: { _ in server.presence() }),
                               api: api,
                               refreshInterval: nil)
        let runner = RouterRunner(yes: yes, exit: exit)
        let controller = OpsController(
            runner: runner,
            makeBuilder: { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/example/repo"),
                                             home: URL(fileURLWithPath: "/example/home"),
                                             sshAuthSocket: nil) },
            auditURL: nil,
            checksInstall: false
        )
        let confirmations = ConfirmationLog()
        let notices = RouterNotices()
        let router = ServiceActionRouter(
            admin: admin,
            controller: controller,
            readAllowlist: { ServiceActionRouterTests.allowlist },
            confirm: { request in
                confirmations.requests.append(request)
                return confirmations.answer
            },
            notifier: notices,
            settleDelay: 0
        )
        let deadline = Date().addingTimeInterval(5)
        while admin.services == nil, Date() < deadline {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return Rig(server: server, admin: admin, controller: controller, router: router,
                   confirmations: confirmations, notices: notices, runner: runner)
    }

    private func waitUntil(_ condition: () -> Bool, file: StaticString = #filePath, line: UInt = #line) async throws {
        let deadline = Date().addingTimeInterval(5)
        while !condition(), Date() < deadline {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(condition(), "The condition was never met.", file: file, line: line)
    }

    private func isReview(_ controller: OpsController) -> Bool {
        if case .review = controller.phase { return true }
        return false
    }

    private func action(_ action: ManagedServiceAction, _ serviceID: String, on rig: Rig) throws -> MenuAction {
        let entry = try XCTUnwrap(rig.admin.entries.first { $0.id == serviceID })
        return try XCTUnwrap(rig.router.action(action, on: entry))
    }

    private func recordedStop(_ rig: Rig) -> Bool? {
        rig.admin.entries.first { $0.id == "shellular" }?.stoppedByOwner
    }

    // MARK: - The owner's stop,  settled with no panel mounted

    func testAMenuStartedStopThatRanKeepsTheRecord() async throws {
        let rig = await makeRig()
        let stop = try action(.stop, "shellular", on: rig)
        XCTAssertEqual(stop.route, .ops(.pm2App(verb: .stop, name: "shellular")))
        XCTAssertEqual(rig.router.start(stop), .planOpened)
        try await waitUntil { self.isReview(rig.controller) }
        XCTAssertEqual(rig.router.actionState, .waiting(stop.subject))
        XCTAssertTrue(rig.router.isBusy)
        rig.controller.confirm()
        try await waitUntil {
            if case .finished = rig.controller.phase { return true }
            return false
        }
        await rig.admin.refresh()
        XCTAssertEqual(recordedStop(rig), true)
        if case .done(let subject, _) = rig.router.actionState {
            XCTAssertEqual(subject, stop.subject)
        } else {
            XCTFail("\(String(describing: rig.router.actionState))")
        }
    }

    func testAMenuStartedStopThatFailedRestoresTheRecord() async throws {
        let rig = await makeRig(yes: Self.failed, exit: 1)
        let stop = try action(.stop, "shellular", on: rig)
        rig.router.start(stop)
        try await waitUntil { self.isReview(rig.controller) }
        rig.controller.confirm()
        try await waitUntil {
            if case .finished = rig.controller.phase { return true }
            return false
        }
        await rig.admin.refresh()
        XCTAssertEqual(recordedStop(rig), false)
        XCTAssertEqual(rig.router.actionState, .failed(stop.subject, reason: "pm2 said no"))
    }

    func testADeclinedStopRestoresTheRecord() async throws {
        let rig = await makeRig()
        let stop = try action(.stop, "shellular", on: rig)
        rig.router.start(stop)
        try await waitUntil { self.isReview(rig.controller) }
        rig.controller.decline("Touch ID did not approve, so nothing ran.")
        // Only a read publishes the record the decline had to put back.
        await rig.admin.refresh()
        XCTAssertEqual(recordedStop(rig), false)
        XCTAssertEqual(rig.router.actionState,
                       .failed(stop.subject, reason: "Touch ID did not approve, so nothing ran."))
    }

    func testAClosedSheetRestoresTheRecordAndLeavesNoLine() async throws {
        let rig = await makeRig()
        let stop = try action(.stop, "shellular", on: rig)
        rig.router.start(stop)
        try await waitUntil { self.isReview(rig.controller) }
        // A read landing while the sheet is up publishes the stop as recorded.
        await rig.admin.refresh()
        XCTAssertEqual(recordedStop(rig), true)
        rig.controller.dismiss()
        XCTAssertEqual(recordedStop(rig), false)
        XCTAssertNil(rig.router.actionState)
        XCTAssertFalse(rig.router.isBusy)
    }

    func testARunReadsTheFleetAgainWithNoPanelMounted() async throws {
        let rig = await makeRig()
        let before = rig.admin.lastReadAt
        rig.router.start(try action(.restart, "shellular", on: rig))
        try await waitUntil { self.isReview(rig.controller) }
        try await Task.sleep(nanoseconds: 20_000_000)
        rig.controller.confirm()
        try await waitUntil { rig.admin.lastReadAt != before }
    }

    // MARK: - One gate

    func testASecondActionIsRefusedWhileASheetIsUp() async throws {
        let rig = await makeRig()
        rig.router.start(try action(.stop, "shellular", on: rig))
        XCTAssertEqual(rig.router.start(try action(.restart, "shellular", on: rig)), .refused)
        XCTAssertEqual(rig.router.start(try action(.stop, FleetServiceRegistry.server.id, on: rig)), .refused)
        try await waitUntil { self.isReview(rig.controller) }
    }

    func testAnActionFromAnotherPanelStillBlocksAndHasALine() async throws {
        let rig = await makeRig()
        rig.controller.plan(.serverUpdate(name: "fleet-cli"))
        XCTAssertTrue(rig.router.isBusy)
        XCTAssertEqual(rig.router.start(try action(.stop, "shellular", on: rig)), .refused)
        try await waitUntil { self.isReview(rig.controller) }
        if case .waiting(let subject) = rig.router.actionState {
            XCTAssertEqual(subject.host, .server)
        } else {
            XCTFail("\(String(describing: rig.router.actionState))")
        }
    }

    // MARK: - A run the server accepted and did not verify

    private static let redeploy = OpsAction.coolifyRedeploy(uuid: "abc123def456", name: "muster-web")

    /// Runs the redeploy from another panel,  the way the Server Status panel does.
    private func runRedeploy(on rig: Rig) async throws {
        rig.controller.plan(Self.redeploy)
        try await waitUntil { self.isReview(rig.controller) }
        rig.controller.confirm()
        try await waitUntil {
            if case .finished = rig.controller.phase { return true }
            return false
        }
    }

    func testASubmittedRunIsNotAFailureAndGoesWhenTheSheetCloses() async throws {
        let rig = await makeRig()
        try await runRedeploy(on: rig)
        XCTAssertEqual(rig.router.actionState, .running(ServiceActionSubject(action: Self.redeploy)))
        rig.controller.dismiss()
        XCTAssertNil(rig.router.actionState)
    }

    func testAVerifiedRedeployIsDone() async throws {
        let rig = await makeRig(yes: #"{"ok":true,"result":"done","commands":[["x"]],"verified":true,"executionState":"completed"}"#)
        try await runRedeploy(on: rig)
        if case .done = rig.router.actionState {} else {
            XCTFail("\(String(describing: rig.router.actionState))")
        }
    }

    func testARunWhoseCompletionCanNoLongerBeVerifiedIsAFailure() async throws {
        let rig = await makeRig(yes: #"{"ok":true,"result":"done","commands":[["x"]],"executionState":"expired"}"#)
        try await runRedeploy(on: rig)
        if case .failed = rig.router.actionState {} else {
            XCTFail("\(String(describing: rig.router.actionState))")
        }
    }

    // MARK: - The whole reason,  in the Services panel

    private func panelProblem(_ rig: Rig) -> String? {
        FleetActionWords.panelProblem(actionState: rig.router.actionState, adminProblem: rig.admin.problem)
    }

    /// Fails a direct action,  which writes the same sentence to both records.
    private func failADirectRestart(on rig: Rig) async throws -> String {
        rig.router.start(try action(.restart, "app.botfleet.companion", on: rig))
        try await waitUntil { !rig.router.isBusy }
        let problem = try XCTUnwrap(rig.admin.problem)
        XCTAssertEqual(panelProblem(rig), problem, "one failure reads once")
        return problem
    }

    func testAPlanFailureAfterADirectFailureShowsItsOwnReason() async throws {
        let rig = await makeRig(yes: Self.failed, exit: 1)
        _ = try await failADirectRestart(on: rig)
        rig.router.start(try action(.stop, "shellular", on: rig))
        XCTAssertNil(rig.admin.problem, "a new action clears the old problem")
        try await waitUntil { self.isReview(rig.controller) }
        rig.controller.confirm()
        try await waitUntil {
            if case .finished = rig.controller.phase { return true }
            return false
        }
        XCTAssertEqual(panelProblem(rig), "Shellular did not stop on the Mac:  pm2 said no")
    }

    func testAPlanThatWorksAfterADirectFailureLeavesNoProblem() async throws {
        let rig = await makeRig()
        _ = try await failADirectRestart(on: rig)
        rig.router.start(try action(.stop, "shellular", on: rig))
        try await waitUntil { self.isReview(rig.controller) }
        rig.controller.confirm()
        try await waitUntil {
            if case .finished = rig.controller.phase { return true }
            return false
        }
        XCTAssertNil(panelProblem(rig))
    }

    func testALoginToggleProblemAfterAFailedPlanHidesNeither() {
        let shellular = ServiceActionSubject(title: "Shellular", host: .mac, action: .stop)
        let toggle = "The login item did not change:  no"
        XCTAssertEqual(FleetActionWords.panelProblem(actionState: .failed(shellular, reason: "pm2 said no"),
                                                     adminProblem: toggle),
                       "Shellular did not stop on the Mac:  pm2 said no\n\n\(toggle)")
        XCTAssertEqual(FleetActionWords.panelProblem(actionState: .running(shellular), adminProblem: toggle), toggle)
        XCTAssertNil(FleetActionWords.panelProblem(actionState: .running(shellular), adminProblem: nil))
    }

    // MARK: - The routes

    func testPM2AndListedAgentsPlanAndBotFleetServerAndUnlistedAgentsGoDirect() async throws {
        let rig = await makeRig()
        let entries = rig.admin.entries
        let entry: (String) throws -> FleetServiceEntry = { id in
            try XCTUnwrap(entries.first { $0.id == id })
        }
        let list = OpsAllowlist(
            macAgents: [OpsAllowlist.Entry(name: "app.botfleet.companion", actions: ["restart"])],
            pm2Apps: [OpsAllowlist.Entry(name: "shellular", actions: ["stop"])]
        )
        let shellular = try entry("shellular")
        let server = try entry(FleetServiceRegistry.server.id)
        let companion = try entry("app.botfleet.companion")
        XCTAssertEqual(MenuRoute.forService(.stop, entry: shellular, allowlist: list),
                       .ops(.pm2App(verb: .stop, name: "shellular")))
        XCTAssertNil(MenuRoute.forService(.start, entry: shellular, allowlist: list), "the allowlist names no start")
        XCTAssertEqual(MenuRoute.forService(.stop, entry: server, allowlist: list),
                       .admin(.stop, serviceID: FleetServiceRegistry.server.id))
        XCTAssertEqual(MenuRoute.forService(.restart, entry: companion, allowlist: list),
                       .ops(.macAgent(verb: .restart, label: "app.botfleet.companion")))
        XCTAssertEqual(MenuRoute.forService(.stop, entry: companion, allowlist: OpsAllowlist()),
                       .admin(.stop, serviceID: "app.botfleet.companion"))
        XCTAssertNil(MenuRoute.forService(.stop, entry: companion, allowlist: nil), "an unread gate does not open")
        XCTAssertEqual(MenuRoute.forService(.stop, entry: shellular, allowlist: nil),
                       .ops(.pm2App(verb: .stop, name: "shellular")), "the helper still checks its own copy")
    }

    func testBotFleetServerRestartAndStopTitlesFollowTheDesign() async throws {
        let rig = await makeRig()
        XCTAssertEqual(try action(.restart, FleetServiceRegistry.server.id, on: rig).title, "Restart On The Mac")
        XCTAssertEqual(try action(.stop, FleetServiceRegistry.server.id, on: rig).title, "Stop On The Mac\u{2026}")
        XCTAssertEqual(try action(.restart, "shellular", on: rig).title, "Restart On The Mac\u{2026}")
    }

    // MARK: - The direct route

    func testADirectStopAsksAndNamesTheMachine() async throws {
        let rig = await makeRig()
        rig.router.start(try action(.stop, FleetServiceRegistry.server.id, on: rig))
        XCTAssertTrue(rig.router.isBusy, "busy from the click,  before the alert")
        try await waitUntil { !rig.router.isBusy }
        let request = try XCTUnwrap(rig.confirmations.requests.first)
        XCTAssertEqual(request.word, "Stop")
        XCTAssertEqual(request.title, "BotFleet Server")
        XCTAssertEqual(request.host, .mac)
        XCTAssertTrue(request.message.contains("Checked just now:  nothing is in flight."), request.message)
        XCTAssertEqual(rig.admin.localServiceState, .stopped)
        if case .done = rig.router.actionState {} else {
            XCTFail("\(String(describing: rig.router.actionState))")
        }
        try await waitUntil { !rig.notices.posts.isEmpty }
        XCTAssertTrue(rig.notices.posts.first?.contains("BotFleet Server stopped on the Mac at") == true)
    }

    func testADeclinedAlertRunsNothing() async throws {
        let rig = await makeRig()
        rig.confirmations.answer = false
        rig.router.start(try action(.stop, FleetServiceRegistry.server.id, on: rig))
        try await waitUntil { !rig.router.isBusy }
        XCTAssertEqual(rig.confirmations.requests.count, 1)
        XCTAssertEqual(rig.admin.localServiceState, .running)
        XCTAssertNil(rig.router.actionState)
    }

    func testADirectRestartDoesNotAsk() async throws {
        let rig = await makeRig()
        rig.router.start(try action(.restart, "app.botfleet.companion", on: rig))
        try await waitUntil { !rig.router.isBusy }
        XCTAssertTrue(rig.confirmations.requests.isEmpty)
    }

    func testRestartingBotFleetServerWithWorkInFlightAsksFirst() async throws {
        let rig = await makeRig()
        rig.server.blockingWorkJSON = #"[{"count":2,"label":"bot turn"}]"#
        rig.confirmations.answer = false
        rig.router.start(try action(.restart, FleetServiceRegistry.server.id, on: rig))
        try await waitUntil { !rig.router.isBusy }
        let request = try XCTUnwrap(rig.confirmations.requests.first)
        XCTAssertEqual(request.word, "Restart")
        XCTAssertEqual(request.host, .mac)
        XCTAssertTrue(request.message.contains("Checked just now:  2 bot turns in flight."), request.message)
        XCTAssertTrue(request.message.contains("on the Mac"), request.message)
        XCTAssertNil(rig.router.actionState, "a declined restart runs nothing")
    }

    func testARestartWarnsOnlyForBotFleetServerWithWorkNamed() async throws {
        let rig = await makeRig()
        let idle = await rig.admin.restartWarning(for: FleetServiceRegistry.server.id)
        XCTAssertNil(idle, "nothing in flight runs at once")
        rig.server.blockingWorkJSON = #"[{"count":1,"label":"bot turn"}]"#
        let other = await rig.admin.restartWarning(for: "app.botfleet.companion")
        XCTAssertNil(other, "only BotFleet Server holds work a restart can end")
        let busy = await rig.admin.restartWarning(for: FleetServiceRegistry.server.id)
        XCTAssertEqual(busy, FleetServices.restartBody(work: "1 bot turn"))
    }

    func testAskingTheServerIsARunnerCallAndNeverAPlan() async throws {
        let rig = await makeRig()
        let watch = PlaceWatch(readStatus: { ServerStatusReading() },
                               readTools: { FleetToolsReading() },
                               runner: rig.runner,
                               makeBuilder: { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/example/repo"),
                                                                home: URL(fileURLWithPath: "/example/home"),
                                                                sshAuthSocket: nil) },
                               interval: nil,
                               controller: rig.controller)
        watch.askServer()
        watch.askServer()
        XCTAssertTrue(watch.isAsking)
        try await waitUntil { !watch.isAsking }
        XCTAssertEqual(watch.asked?.answered, true)
        XCTAssertEqual(rig.runner.invocations.count, 1, "one at a time")
        XCTAssertEqual(rig.runner.invocations.first?.executable, OpsControlRoute.ssh)
        XCTAssertEqual(rig.controller.phase, .idle)
    }

    func testAServerThatDoesNotAnswerIsSaidToNotAnswer() async throws {
        let silent = RouterRunner(yes: "", exit: 255, plan: "")
        let rig = await makeRig()
        let watch = PlaceWatch(readStatus: { ServerStatusReading() },
                               readTools: { FleetToolsReading() },
                               runner: silent,
                               makeBuilder: { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/example/repo"),
                                                                home: URL(fileURLWithPath: "/example/home"),
                                                                sshAuthSocket: nil) },
                               interval: nil,
                               controller: rig.controller)
        watch.askServer()
        try await waitUntil { !watch.isAsking }
        XCTAssertEqual(watch.asked?.answered, false)
    }

    func testThePlaceWatchReadsTheFilesAndReadsAgainAfterARun() async throws {
        let rig = await makeRig()
        let watch = PlaceWatch(readStatus: { ServerStatusReading() },
                               readTools: { FleetToolsReading() },
                               runner: rig.runner,
                               interval: nil,
                               controller: rig.controller)
        XCTAssertNil(watch.status)
        watch.readNow()
        try await waitUntil { watch.status != nil && watch.tools != nil }
        let tick = watch.now
        try await Task.sleep(nanoseconds: 20_000_000)
        rig.router.start(try action(.restart, "shellular", on: rig))
        try await waitUntil { self.isReview(rig.controller) }
        rig.controller.confirm()
        try await waitUntil { watch.now != tick }
    }
}

/// A helper that answers from a script.
private final class RouterRunner: OpsRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [OpsInvocation] = []
    private let yes: String
    private let exit: Int32
    private let plan: String

    init(yes: String, exit: Int32, plan: String = #"{"ok":true,"result":"planned","commands":[["pm2","stop","shellular"]],"detail":"plan"}"#) {
        self.yes = yes
        self.exit = exit
        self.plan = plan
    }

    var invocations: [OpsInvocation] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        lock.lock()
        recorded.append(invocation)
        lock.unlock()
        if invocation.arguments.contains("--plan") {
            return OpsProcessResult(exitCode: plan.isEmpty ? exit : 0, stdout: plan, stderr: "")
        }
        return OpsProcessResult(exitCode: exit, stdout: yes, stderr: "")
    }
}

/// The notices an action posted.
private final class RouterNotices: ApplyNotifying, @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [String] = []

    var posts: [String] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    func post(title: String, body: String) async {
        lock.lock()
        recorded.append(body)
        lock.unlock()
    }
}

/// The commands the admin runs.  Nothing is running in launchd,  and pm2 knows nothing.
private final class RouterCommands: CommandRunning {
    private let boot = UUID().uuidString

    func run(_ executable: String, _ arguments: [String]) throws -> String {
        if executable == "/bin/launchctl", arguments == ["list"] {
            return "PID\tStatus\tLabel\n-\t0\tother.service"
        }
        if executable == "/usr/sbin/sysctl" { return boot }
        if arguments.contains("jlist") { return "[]" }
        return ""
    }
}
