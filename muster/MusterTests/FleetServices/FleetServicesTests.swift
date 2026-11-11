import XCTest

final class FleetServicesTests: XCTestCase {
    // MARK: - The registry of fleet services

    func testTheBotFleetMCPServerIsTheFirstFleetService() {
        let entries = FleetServices(serverState: .running, states: [:]).entries
        XCTAssertEqual(entries.first?.id, "botfleet-mcp")
        XCTAssertEqual(entries.first?.title, "BotFleet MCP Server")
    }

    func testEveryProcessBotFleetStartsIsInTheRegistry() {
        let ids = FleetServiceRegistry.all.map(\.id)
        // The pm2 applications the ecosystem file defines.
        for appName in ["shellular", "code-main-keeper", "vision-worker", "xcode-health",
                        "agy-acp", "grok-leader", "grok-acp", "clutch-web", "seat-mcp",
                        "botfleet-mcp"] {
            XCTAssertTrue(ids.contains(appName), "\(appName) is missing from the registry")
        }
        // The launchd agents.
        XCTAssertTrue(ids.contains("app.botfleet.server"))
        XCTAssertTrue(ids.contains("app.botfleet.companion"))
        XCTAssertTrue(ids.contains("com.jay.operator"))
        XCTAssertEqual(ids.count, 13)
    }

    /// The Mac's eavesdropper (`com.jay.operator`) is on the Services list as a LaunchAgent,
    /// so its state is launchd's.  It was retired at the one-listener cutover,  so its policy
    /// says so.  The wake alerter is not on the list:  whether it should run is the owner's call.
    func testTheOperatorEavesdropperIsALaunchAgentServiceAndTheWakeAlerterIsNot() {
        let descriptor = FleetServiceRegistry.descriptor(for: "com.jay.operator")
        XCTAssertEqual(descriptor?.kind, .launchAgent(label: "com.jay.operator"))
        XCTAssertEqual(descriptor?.policy, .retired)
        XCTAssertEqual(descriptor?.host, .mac)
        XCTAssertEqual(descriptor, FleetServiceRegistry.operatorEavesdropper)
        XCTAssertNil(FleetServiceRegistry.descriptor(for: "com.jay.operator-wake-alerter"))
        let labels = FleetServiceRegistry.all.compactMap { descriptor -> String? in
            if case .launchAgent(let label) = descriptor.kind { return label }
            return nil
        }
        XCTAssertTrue(labels.contains("com.jay.operator"))
        XCTAssertFalse(labels.contains("com.jay.operator-wake-alerter"))
        // Muster never starts or stops it on its own:  it was retired.
        XCTAssertEqual(ServicePolicyTable.policy(for: "com.jay.operator"), .leaveAlone)
        XCTAssertEqual(ServicePolicyTable.row(for: "com.jay.operator")?.source, .servicesList)
        XCTAssertTrue(ServicePolicyTable.reason(for: "com.jay.operator").contains("retired at the Nov 10 one-listener cutover"))
    }

    // MARK: - The retired Mac listener

    private func operatorEntry(_ state: ManagedServiceState?,
                               stoppedByOwner: Bool = false) -> FleetServiceEntry? {
        let id = FleetServiceRegistry.operatorEavesdropper.id
        let states: [String: ManagedServiceState] = state.map { [id: $0] } ?? [:]
        let services = FleetServices(serverState: .running, states: states,
                                     stoppedByOwner: stoppedByOwner ? [id] : [])
        return services.entries.first { $0.id == id }
    }

    /// Stopped is the retired listener's normal state:  its row reads Retired,  says since when
    /// and who holds the seats,  is never something to look at,  and offers nothing to start.
    func testAStoppedRetiredListenerReadsRetiredAndIsNotDown() throws {
        for state in [ManagedServiceState.stopped, .notInstalled] {
            let entry = try XCTUnwrap(operatorEntry(state))
            XCTAssertTrue(entry.isRetired)
            XCTAssertTrue(entry.isRetiredAndDown)
            XCTAssertEqual(entry.wanted, false)
            XCTAssertEqual(entry.summary, "retired")
            XCTAssertFalse(entry.needsAttention, "\(state)")
            XCTAssertFalse(MachineMenuCopy.isProblem(entry))
            XCTAssertFalse(entry.stateNoteIsWarning)
            XCTAssertEqual(entry.availableActions, [])
            XCTAssertNil(entry.whyNoActions)
            XCTAssertEqual(entry.marker, ServiceStateMarker(word: "Retired", symbol: "archivebox", tone: .neutral))
            XCTAssertEqual(entry.stateNote,
                           "Retired since the Nov 10 one-listener cutover.\u{00A0} The server listener holds every seat, so this Mac listener is expected to be stopped.")
            XCTAssertEqual(entry.menuNote, "since the Nov 10 one-listener cutover")
            let row = MachineMenuCopy.serviceRow(entry, allowlist: OpsAllowlist(), pm2Answered: true)
            XCTAssertEqual(row.title, "Operator Eavesdropper:  retired (since the Nov 10 one-listener cutover)")
            XCTAssertTrue(row.actions.isEmpty)
        }
        // It counts as retired,  never as stopped,  in the menu's line.
        let stopped = try XCTUnwrap(operatorEntry(.stopped))
        let shellular = FleetServiceEntry(descriptor: try XCTUnwrap(FleetServiceRegistry.descriptor(for: "shellular")),
                                          wanted: true, state: .running)
        XCTAssertEqual(MachineMenuCopy.serviceCounts([shellular, stopped]), "1 running, 0 stopped, 1 retired")
        // The sidebar's Services badge does not count it.
        let now = Date()
        XCTAssertNil(SidebarBadges.services([shellular, stopped], pm2Answered: true, readAt: now, now: now))
    }

    /// A retired listener found running is said plainly:  a warning,  and only a Stop.
    func testARunningRetiredListenerIsAPlainWarning() throws {
        let entry = try XCTUnwrap(operatorEntry(.running))
        XCTAssertTrue(entry.isRetiredButLive)
        XCTAssertEqual(entry.summary, "running, but it is retired and should hold no seat")
        XCTAssertEqual(entry.marker.word, "Running")
        XCTAssertEqual(entry.marker.tone, .warning)
        XCTAssertTrue(entry.stateNoteIsWarning)
        XCTAssertEqual(entry.stateNote,
                       "It is running, but it was retired at the Nov 10 one-listener cutover and should hold no seat.\u{00A0} The server listener holds every seat.")
        XCTAssertEqual(entry.menuNote, "the server listener holds every seat")
        XCTAssertEqual(entry.availableActions, [.stop])
        // A running listener is not down,  so it is not counted as down.
        XCTAssertFalse(entry.needsAttention)
        XCTAssertEqual(MachineMenuCopy.serviceCounts([entry]), "1 running, 0 stopped")
    }

    /// A state nobody read is still not known,  never running and never retired-and-stopped,
    /// and a failure is still a failure.
    func testAnUnreadOrFailedRetiredListenerIsStillSaidAsItIs() throws {
        let unread = try XCTUnwrap(operatorEntry(nil))
        XCTAssertEqual(unread.state, .unknown)
        XCTAssertFalse(unread.isRetiredAndDown)
        XCTAssertTrue(unread.needsAttention)
        XCTAssertEqual(unread.availableActions, [])
        let failed = try XCTUnwrap(operatorEntry(.failed))
        XCTAssertTrue(failed.needsAttention)
        XCTAssertEqual(failed.marker.word, "Failed")
        XCTAssertEqual(failed.availableActions, [])
        XCTAssertTrue(failed.stateNote?.contains("retired at the Nov 10 one-listener cutover") == true)
    }

    /// A stop the owner asked for stays on its row,  beside why the listener is retired.
    func testAnOwnerStopOnTheRetiredListenerKeepsBothLines() throws {
        let entry = try XCTUnwrap(operatorEntry(.stopped, stoppedByOwner: true))
        XCTAssertEqual(entry.menuNote, "stopped by you")
        XCTAssertTrue(entry.stateNote?.hasPrefix("Stopped on purpose") == true, entry.stateNote ?? "")
        XCTAssertTrue(entry.stateNote?.contains("Retired since the Nov 10 one-listener cutover.") == true, entry.stateNote ?? "")
        XCTAssertEqual(entry.availableActions, [])
    }

    func testTheMCPServerIsSupervisedByPM2AndFollowsTheBotFleetServer() {
        XCTAssertEqual(FleetServiceRegistry.mcpServer.kind, .pm2(appName: "botfleet-mcp"))
        XCTAssertEqual(FleetServiceRegistry.mcpServer.policy, .whileServerRuns)
        XCTAssertEqual(FleetServiceRegistry.server.kind, .launchAgent(label: "app.botfleet.server"))
        XCTAssertEqual(FleetServiceRegistry.companion.kind, .launchAgent(label: "app.botfleet.companion"))
    }

    func testEveryOtherServiceIsWantedOnItsOwn() {
        for descriptor in FleetServiceRegistry.all
        where descriptor.id != "botfleet-mcp" && descriptor.id != FleetServiceRegistry.operatorEavesdropper.id {
            XCTAssertEqual(descriptor.policy, .alwaysOn, "\(descriptor.id) follows the wrong rule")
        }
    }

    // MARK: - Wanted state

    func testTheMCPServerIsWantedExactlyWhileTheBotFleetServerRuns() {
        XCTAssertEqual(wanted(.whileServerRuns, serverState: .running), true)
        XCTAssertEqual(wanted(.whileServerRuns, serverState: .stopped), false)
        XCTAssertNil(wanted(.whileServerRuns, serverState: .unknown))
    }

    func testAFleetServiceIsWantedUnlessSomethingSaysOtherwise() {
        // A running service is wanted, and so is one that failed: it is not
        // running and nothing asked it to stop.
        XCTAssertEqual(wanted(.alwaysOn, state: .running), true)
        XCTAssertEqual(wanted(.alwaysOn, state: .failed), true)
        // A service that was stopped on purpose is the something that says
        // otherwise, and so is one that is not installed.
        XCTAssertEqual(wanted(.alwaysOn, state: .stopped), false)
        XCTAssertEqual(wanted(.alwaysOn, state: .notInstalled), false)
        // A service the Control app cannot see has no wanted state at all.
        XCTAssertNil(wanted(.alwaysOn, state: .unknown))
    }

    func testTheBotFleetServerRuleIsNeverUsedForAFleetService() {
        // The BotFleet Server is down, so the MCP server is not wanted.  The
        // other fleet services are wanted on their own and are untouched.
        let services = FleetServices(serverState: .stopped,
                                     states: ["shellular": .running, "botfleet-mcp": .running])
        let shellular = services.entries.first { $0.id == "shellular" }
        let mcpServer = services.entries.first { $0.id == "botfleet-mcp" }
        XCTAssertEqual(shellular?.wanted, true)
        XCTAssertEqual(mcpServer?.wanted, false)
    }

    // MARK: - A service the owner stopped

    func testAServiceTheOwnerStoppedCarriesItsRecordAndTime() {
        let when = Date(timeIntervalSince1970: 1_800_000_000)
        let services = FleetServices(serverState: .running,
                                     states: ["shellular": .failed],
                                     stoppedByOwner: ["shellular"],
                                     stoppedAt: ["shellular": when])
        let entry = services.entries.first { $0.id == "shellular" }
        XCTAssertEqual(entry?.stoppedByOwner, true)
        XCTAssertEqual(entry?.stoppedAt, when)
        XCTAssertTrue(entry?.ownerStopLine?.hasPrefix("Stopped by you at ") == true, entry?.ownerStopLine ?? "")
        XCTAssertTrue(entry?.menuNote?.hasPrefix("stopped by you at ") == true, entry?.menuNote ?? "")
    }

    // MARK: - The actions the owner may pick

    func testTheActionsForARunningServiceAreStopAndRestart() {
        XCTAssertEqual(FleetServices.availableActions(for: .running, wanted: true, policy: .alwaysOn), [.stop, .restart])
    }

    func testTheActionsForAStoppedServiceAreStartAndRestart() {
        XCTAssertEqual(FleetServices.availableActions(for: .stopped, wanted: false, policy: .alwaysOn), [.start, .restart])
    }

    func testTheActionsForAFailedServiceAreStartAndRestart() {
        XCTAssertEqual(FleetServices.availableActions(for: .failed, wanted: true, policy: .alwaysOn), [.start, .restart])
    }

    func testAServiceThatIsNotInstalledHasNoActions() {
        XCTAssertEqual(FleetServices.availableActions(for: .notInstalled, wanted: false, policy: .alwaysOn), [])
    }

    func testAServiceTheAppCannotSeeCanOnlyBeRestarted() {
        // The Control app does not stop a service it cannot see.
        XCTAssertEqual(FleetServices.availableActions(for: .unknown, wanted: nil, policy: .alwaysOn), [.restart])
    }

    func testAServiceThatFollowsTheServerCanBeStoppedWhileTheServerRuns() {
        // The BotFleet MCP Server follows the BotFleet Server, and a stop the
        // owner asked for is not one the Control app takes back.
        XCTAssertEqual(FleetServices.availableActions(for: .running, wanted: true, policy: .whileServerRuns), [.stop, .restart])
        XCTAssertEqual(FleetServices.availableActions(for: .stopped, wanted: true, policy: .whileServerRuns), [.start, .restart])
    }

    func testAServiceThatFollowsTheServerIsNotStartedWhileTheServerIsDown() {
        // It has nothing to serve then,  and the row says so instead of showing a bare empty menu.
        XCTAssertEqual(FleetServices.availableActions(for: .stopped, wanted: false, policy: .whileServerRuns), [])
        let services = FleetServices(serverState: .stopped, states: ["botfleet-mcp": .stopped])
        let entry = services.entries.first { $0.id == "botfleet-mcp" }
        XCTAssertEqual(entry?.whyNoActions, "it starts when BotFleet Server is running")
    }

    func testARunningServiceThatFollowsTheServerCanAlwaysBeStopped() {
        // The server it follows is down and the service is still up:  the owner can stop it.
        XCTAssertEqual(FleetServices.availableActions(for: .running, wanted: false, policy: .whileServerRuns), [.stop])
    }

    func testAnApplicationPM2NeverHeardOfIsNotOfferedAStart() {
        XCTAssertEqual(FleetServices.availableActions(for: .notInPM2, wanted: false, policy: .alwaysOn), [])
        let services = FleetServices(serverState: .running, states: ["seat-mcp": .notInPM2, "shellular": .running])
        let entry = services.entries.first { $0.id == "seat-mcp" }
        XCTAssertEqual(entry?.summary, "not in this Mac's pm2")
        XCTAssertEqual(entry?.availableActions, [])
        XCTAssertEqual(entry?.whyNoActions, "pm2 on the Mac has no record of it, so it is not started from here")
        // pm2 answered,  so the rest of its rows are known.
        XCTAssertTrue(services.pm2Answered)
    }

    func testAServiceTheOwnerStoppedCanBeStartedAgain() {
        XCTAssertEqual(FleetServices.availableActions(for: .stopped, wanted: false, policy: .whileServerRuns, stoppedByOwner: true),
                       [.start, .restart])
    }

    func testTheServerIsOfferedStopWhenTheInstalledWrapperDoesNotSupportThePolicy() {
        // The BotFleet Server's start and restart change its startup policy,
        // so an older wrapper is offered neither of those rather than an
        // action that would always fail.  A stop asks the server itself, so
        // it works with any wrapper and stays on the row.
        let services = FleetServices(serverState: .running,
                                    states: ["app.botfleet.server": .running],
                                    startupPolicySupported: false)
        let row = services.menuItems.first { $0.id == "app.botfleet.server" }
        XCTAssertEqual(row?.actions, [.stop])
    }

    func testAStoppedServerOnAnOldWrapperIsOfferedNothing() {
        // There is nothing to offer a stopped server whose wrapper cannot
        // carry the startup policy: a start and a restart both need it.
        let services = FleetServices(serverState: .stopped,
                                    states: ["app.botfleet.server": .stopped],
                                    startupPolicySupported: false)
        let row = services.menuItems.first { $0.id == "app.botfleet.server" }
        XCTAssertEqual(row?.actions, [])
    }

    func testTheServerIsOfferedItsActionsWhenTheInstalledWrapperSupportsThePolicy() {
        let services = FleetServices(serverState: .running,
                                    states: ["app.botfleet.server": .running],
                                    startupPolicySupported: true)
        let row = services.menuItems.first { $0.id == "app.botfleet.server" }
        XCTAssertEqual(row?.actions, [.stop, .restart])
    }

    func testTheMcpServerRowInTheMenuOffersStopWhileTheServerRuns() {
        let services = FleetServices(serverState: .running, states: ["botfleet-mcp": .running])
        let row = services.menuItems.first { $0.id == "botfleet-mcp" }
        XCTAssertEqual(row?.actions, [.stop, .restart])
    }

    // MARK: - The menu

    func testTheFleetServicesMenuHasOneRowPerService() {
        let services = FleetServices(serverState: .running, states: [:])
        XCTAssertEqual(services.menuItems.count, 13)
        XCTAssertEqual(services.menuItems.first?.id, "botfleet-mcp")
        XCTAssertEqual(services.menuTitle, "Services On The Mac (13)")
    }

    func testEachMenuRowShowsTheStateAndTheActions() {
        let services = FleetServices(serverState: .running,
                                    states: ["botfleet-mcp": .running, "shellular": .stopped])
        let mcpServer = services.menuItems.first { $0.id == "botfleet-mcp" }
        let shellular = services.menuItems.first { $0.id == "shellular" }
        XCTAssertEqual(mcpServer?.summary, "running")
        // The MCP server's state follows the BotFleet Server, and a stop the
        // owner asked for is kept.
        XCTAssertEqual(mcpServer?.actions, [.stop, .restart])
        XCTAssertEqual(shellular?.summary, "stopped")
        XCTAssertEqual(shellular?.actions, [.start, .restart])
    }

    // MARK: - Reading the pm2 applications

    func testThePM2StatesAreReadFromTheJSON() throws {
        let listing = """
        [
          {"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}},
          {"name": "botfleet-mcp", "pid": 0, "pm2_env": {"status": "stopped"}},
          {"name": "grok-acp", "pid": 0, "pm2_env": {"status": "errored"}}
        ]
        """
        let states = try XCTUnwrap(PM2Client.parseAppStates(listing))
        XCTAssertEqual(states["shellular"], .running)
        XCTAssertEqual(states["botfleet-mcp"], .stopped)
        XCTAssertEqual(states["grok-acp"], .failed)
    }

    func testAListingThatCannotBeReadIsNotAnEmptyRegistry() {
        // An empty registry would read every application as stopped, and the
        // reconciler would act on that, so a corrupt listing knows nothing.
        XCTAssertNil(PM2Client.parseAppStates("not json"))
        XCTAssertNil(PM2Client.parseAppStates(#"{"name": "shellular"}"#))
        XCTAssertEqual(PM2Client.parseAppStates("[]"), [:])
        let commands = FleetServiceCommands()
        commands.pm2Listing = "garbled"
        let pm2 = PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2")
        XCTAssertEqual(pm2.state(ofApp: "grok-acp"), .unknown)
    }

    func testAnApplicationPM2HasNoRecordOfIsNotStopped() {
        // pm2 answered and has never heard of it:  that is not a stopped application,  and a
        // start could make a second copy of a job that belongs to another machine.
        let commands = FleetServiceCommands()
        commands.pm2Listing = #"[{"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}}]"#
        let pm2 = PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2")
        XCTAssertEqual(pm2.state(ofApp: "grok-acp"), .notInPM2)
    }

    func testAPM2ThatCannotBeReachedKnowsNothing() {
        let commands = FleetServiceCommands()
        commands.failure = StartupPolicyError.commandFailed
        let pm2 = PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2")
        XCTAssertNil(pm2.appStates())
        XCTAssertEqual(pm2.state(ofApp: "shellular"), .unknown)
    }

    func testAnApplicationWithNoStateIsUnknown() {
        XCTAssertEqual(PM2Client.state(forStatus: nil), .unknown)
        XCTAssertEqual(PM2Client.state(forStatus: "launching"), .unknown)
    }

    func testAnApplicationPM2CallsOnlineWithNoProcessBehindItIsNotRunning() throws {
        // pm2 keeps the record of every application it started, and the record
        // outlives the process.  The BotFleet MCP adapter read as online for
        // days while nothing served its port, and a record that says online
        // with no pid behind it is exactly that:  a stale record, and a stale
        // record is not a running service.
        let listing = """
        [
          {"name": "botfleet-mcp", "pm2_env": {"status": "online"}},
          {"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}}
        ]
        """
        let states = try XCTUnwrap(PM2Client.parseAppStates(listing))
        XCTAssertEqual(states["botfleet-mcp"], .failed)
        XCTAssertEqual(states["shellular"], .running)
    }

    func testAnApplicationPM2ReportsOnlineWithNoPidAtAllIsNotRunning() throws {
        // A listing with no pid proves nothing about a process, so it cannot
        // read as running either.
        let listing = #"[{"name": "botfleet-mcp", "pm2_env": {"status": "online"}}]"#
        let states = try XCTUnwrap(PM2Client.parseAppStates(listing))
        XCTAssertEqual(states["botfleet-mcp"], .failed)
    }

    func testTheStaleApplicationsAreTheOnesWithNoProcessBehindThem() throws {
        let listing = """
        [
          {"name": "botfleet-mcp", "pm2_env": {"status": "online"}},
          {"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}},
          {"name": "grok-acp", "pid": 0, "pm2_env": {"status": "errored"}}
        ]
        """
        XCTAssertEqual(PM2Client.snapshot(from: listing)?.stale, ["botfleet-mcp"])
    }

    func testAPidInTheNestedEnvironmentIsReadWhenTheEntryHasNone() throws {
        // pm2 has carried the process id in more than one place in its listing.
        // The entry level is the one that decides, and the nested value is the
        // fallback, so a shape that moves the field cannot make every running
        // application read as failed.
        let listing = #"[{"name": "shellular", "pm2_env": {"status": "online", "pid": 4242}}]"#
        let states = try XCTUnwrap(PM2Client.parseAppStates(listing))
        XCTAssertEqual(states["shellular"], .running)
        XCTAssertEqual(PM2Client.snapshot(from: listing)?.stale, [])
    }

    func testAnEntryPidOfZeroIsNotOverriddenByANestedOne() throws {
        // The nested environment can keep the id of a process that is long
        // gone, so an explicit zero at the entry level means no process.
        let listing = #"[{"name": "botfleet-mcp", "pid": 0, "pm2_env": {"status": "online", "pid": 4242}}]"#
        let states = try XCTUnwrap(PM2Client.parseAppStates(listing))
        XCTAssertEqual(states["botfleet-mcp"], .failed)
    }

    func testAListingThatCannotBeReadNamesNoStaleApplication() {
        XCTAssertNil(PM2Client.snapshot(from: "not json"))
        XCTAssertEqual(PM2Client.snapshot(from: "[]")?.stale, [])
    }

    // MARK: - Changing the pm2 applications

    func testStartingAPM2ApplicationRunsPM2Start() throws {
        let commands = FleetServiceCommands()
        try pm2(commands).start(appName: "shellular")
        // A start is one pm2 command.  The listing that would tell a stale
        // record from a running one is a second command, and a caller that
        // cannot tell them apart finds out from a start that achieved
        // nothing rather than from a listing every start would pay for.
        XCTAssertEqual(commands.calls, [["start", "shellular"]])
    }

    func testAStartWithNoStaleSetReadsNoListing() throws {
        // The reconciler reads the listing and hands the stale set over, so a
        // start never has to read one of its own.
        let commands = FleetServiceCommands()
        commands.pm2Listing = #"[{"name": "botfleet-mcp", "pm2_env": {"status": "online"}}]"#
        try pm2(commands).start(appName: "botfleet-mcp")
        XCTAssertEqual(commands.calls, [["start", "botfleet-mcp"]])
    }

    func testStartingAStaleApplicationRestartsItInsteadOfStartingIt() throws {
        // pm2 answers `pm2 start` on a record it believes is online with
        // "already started" and starts nothing, so a stale application would
        // never come back and the start would report a success it did not
        // achieve.  A stale record is restarted, which runs the script again.
        let commands = FleetServiceCommands()
        try pm2(commands).start(appName: "botfleet-mcp", stale: ["botfleet-mcp"])
        XCTAssertTrue(commands.calls.contains { $0 == ["restart", "botfleet-mcp"] })
        XCTAssertFalse(commands.calls.contains { $0.first == "start" })
    }

    func testStartingAStaleApplicationFallsBackToTheEcosystemFile() throws {
        // A stale record whose launcher is gone is exactly the botfleet-mcp
        // case, and the ecosystem file is the way back, so the stale path
        // carries the same fallback as any other restart.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("apps/pm2-ecosystem.config.cjs"))
        let commands = FleetServiceCommands()
        commands.restartFailure = CommandExitError(output: "", status: 1,
                                                   errorOutput: "[PM2][ERROR] Process or Namespace botfleet-mcp not found")
        try pm2(commands, home: home).start(appName: "botfleet-mcp", stale: ["botfleet-mcp"])
        XCTAssertEqual(commands.calls.count, 2)
        XCTAssertEqual(commands.calls[0], ["restart", "botfleet-mcp"])
        XCTAssertEqual(commands.calls[1], ["start",
                                          home.appendingPathComponent("apps/pm2-ecosystem.config.cjs").path,
                                          "--only", "botfleet-mcp"])
    }

    func testStartingAPM2ApplicationFallsBackToTheEcosystemFile() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("apps/pm2-ecosystem.config.cjs"))
        let commands = FleetServiceCommands()
        commands.startFailure = CommandExitError(output: "", status: 1,
                                                 errorOutput: "[PM2][ERROR] Process or Namespace shellular not found")
        try pm2(commands, home: home).start(appName: "shellular")
        XCTAssertEqual(commands.calls.count, 2)
        XCTAssertEqual(commands.calls[0], ["start", "shellular"])
        XCTAssertEqual(commands.calls[1], ["start",
                                          home.appendingPathComponent("apps/pm2-ecosystem.config.cjs").path,
                                          "--only", "shellular"])
    }

    func testAStartThatTimedOutIsNeverRunAgainFromTheEcosystemFile() throws {
        // A timeout says nothing about whether pm2 knows the application,  and a second start
        // could start a second copy of one that is coming up.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("apps/pm2-ecosystem.config.cjs"))
        let commands = FleetServiceCommands()
        commands.startFailure = CommandTimedOutError(executable: "/usr/bin/node",
                                                     arguments: ["/usr/bin/pm2", "start", "shellular"])
        XCTAssertThrowsError(try pm2(commands, home: home).start(appName: "shellular"))
        XCTAssertEqual(commands.calls, [["start", "shellular"]])
    }

    func testAStartThatFailedForAnotherReasonIsNeverRunAgainFromTheEcosystemFile() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("apps/pm2-ecosystem.config.cjs"))
        let commands = FleetServiceCommands()
        commands.startFailure = CommandExitError(output: "", status: 1, errorOutput: "script failed to spawn")
        XCTAssertThrowsError(try pm2(commands, home: home).start(appName: "shellular"))
        XCTAssertEqual(commands.calls, [["start", "shellular"]])
    }

    func testStoppingAPM2ApplicationRunsPM2Stop() throws {
        let commands = FleetServiceCommands()
        try pm2(commands).stop(appName: "shellular")
        XCTAssertEqual(commands.calls, [["stop", "shellular"]])
    }

    func testRestartingAPM2ApplicationRunsPM2Restart() throws {
        let commands = FleetServiceCommands()
        try pm2(commands).restart(appName: "shellular")
        XCTAssertEqual(commands.calls, [["restart", "shellular"]])
    }

    func testRestartingAPM2ApplicationFallsBackToTheEcosystemFile() throws {
        // pm2 no longer remembers the application, so the ecosystem file is
        // the way back, exactly as it is for a start.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("apps/pm2-ecosystem.config.cjs"))
        let commands = FleetServiceCommands()
        commands.restartFailure = CommandExitError(output: "", status: 1,
                                                   errorOutput: "[PM2][ERROR] Process or Namespace shellular not found")
        try pm2(commands, home: home).restart(appName: "shellular")
        XCTAssertEqual(commands.calls.count, 2)
        XCTAssertEqual(commands.calls[0], ["restart", "shellular"])
        XCTAssertEqual(commands.calls[1], ["start",
                                          home.appendingPathComponent("apps/pm2-ecosystem.config.cjs").path,
                                          "--only", "shellular"])
    }

    func testTheStateAfterTheActionIsTheOneThatIsReported() async throws {
        let commands = FleetServiceCommands()
        commands.pm2Listing = #"[{"name": "shellular", "pm2_env": {"status": "stopped"}}]"#
        commands.pm2ListingAfterChange = #"[{"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}}]"#
        let state = try await pm2(commands).setRunning(true, appName: "shellular", attempts: 2)
        XCTAssertEqual(state, .running)
        XCTAssertTrue(commands.calls.contains { $0.first == "start" })
    }

    func testAWaitForAPM2ChangeDoesNotSpawnAProcessForEveryHalfSecond() async throws {
        // Every look at a pm2 application spawns a node client, so a wait
        // that never settles has to be made of few wide looks rather than
        // many narrow ones.
        let commands = FleetServiceCommands()
        commands.pm2Listing = #"[{"name": "shellular", "pm2_env": {"status": "stopped"}}]"#
        _ = try await pm2(commands).setRunning(true, appName: "shellular", attempts: 3, interval: 0.01)
        let looks = commands.calls.filter { $0.first == "jlist" }.count
        // One look before the change, one for each attempt of the wait, and
        // one after it.
        XCTAssertLessThanOrEqual(looks, 5)
        // The wait still covers the twenty seconds it always did, and it
        // looks ten times at most, where the old one looked forty-two.
        let window = Double(PM2Client.settleAttempts) * PM2Client.settleInterval
        XCTAssertGreaterThanOrEqual(window, 20.0)
        XCTAssertLessThanOrEqual(PM2Client.settleAttempts + 2, 10)
    }

    func testAnApplicationThatIsAlreadyRunningIsNotStartedAgain() async throws {
        let commands = FleetServiceCommands()
        commands.pm2Listing = #"[{"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}}]"#
        let state = try await pm2(commands).setRunning(true, appName: "shellular", attempts: 1)
        XCTAssertEqual(state, .running)
        XCTAssertFalse(commands.calls.contains { $0.first == "start" })
    }

    // MARK: - The launchd agents

    func testALaunchAgentStateIsReadFromLaunchctl() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let commands = LaunchAgentCommands()
        commands.listing = "PID\tStatus\tLabel\n4210\t0\tapp.botfleet.companion\n-\t0\tapp.botfleet.server"
        let policy = StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
        // An agent with a live process behind it is running.
        XCTAssertEqual(policy.state(ofAgent: "app.botfleet.companion"), .running)
        // An agent that is loaded without one is stopped.
        XCTAssertEqual(policy.state(ofAgent: "app.botfleet.server"), .stopped)
    }

    func testALaunchAgentWithoutItsPlistIsNotInstalled() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let policy = StartupPolicy(home: home, commands: LaunchAgentCommands(), uid: 501, environment: [:])
        XCTAssertEqual(policy.state(ofAgent: "app.botfleet.server"), .notInstalled)
    }

    func testStartingALaunchAgentEnablesLoadsAndKickstartsIt() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = LaunchAgentCommands()
        try StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
            .startAgent(label: "app.botfleet.companion")
        XCTAssertEqual(commands.calls, [
            "/bin/launchctl enable gui/501/app.botfleet.companion",
            "/bin/launchctl print gui/501/app.botfleet.companion",
            "/bin/launchctl bootstrap gui/501 \(home.path)/Library/LaunchAgents/app.botfleet.companion.plist",
            "/bin/launchctl kickstart gui/501/app.botfleet.companion"
        ])
    }

    func testStoppingALaunchAgentBootsItOut() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = LaunchAgentCommands()
        try StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
            .stopAgent(label: "app.botfleet.companion")
        XCTAssertEqual(commands.calls, ["/bin/launchctl bootout gui/501/app.botfleet.companion"])
    }

    func testRestartingALaunchAgentReplacesTheRunningInstance() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = LaunchAgentCommands()
        commands.loaded = true
        try StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
            .restartAgent(label: "app.botfleet.companion")
        XCTAssertEqual(commands.calls, [
            "/bin/launchctl print gui/501/app.botfleet.companion",
            "/bin/launchctl kickstart -k gui/501/app.botfleet.companion"
        ])
    }

    func testRestartingALaunchAgentThatIsNotLoadedStartsIt() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = LaunchAgentCommands()
        try StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
            .restartAgent(label: "app.botfleet.companion")
        XCTAssertTrue(commands.calls.contains("/bin/launchctl kickstart gui/501/app.botfleet.companion"))
    }

    // MARK: - Waiting for a change

    func testAWaitWithAnImpossibleIntervalDoesNotTrap() async throws {
        // A negative or an enormous interval would trap the conversion to
        // nanoseconds, and this is public API.
        var observations = 0
        _ = await ManagedServices.settle(to: .running, attempts: 2, interval: -1) {
            observations += 1
            return .stopped
        }
        XCTAssertEqual(observations, 3)
        var endless = 0
        _ = await ManagedServices.settle(to: .running, attempts: 1, interval: .infinity) {
            endless += 1
            return .stopped
        }
        XCTAssertEqual(endless, 2)
    }

    func testAMCPChangeKeepsItsTwentySecondWindow() {
        // The MCP server is observed through an HTTP health endpoint, so a
        // look at it costs no process and its window stays the twenty
        // seconds it always was.
        XCTAssertEqual(PM2Client.mcpSettleWindow, 20)
    }

    func testAWaitWithAWallClockBudgetStopsAtItsDeadline() async {
        // A look at the MCP server is an HTTP probe, and a probe that hangs
        // costs its whole request timeout.  A count of looks would let the
        // app sit busy for far longer than the window it promises, so the
        // wait is bounded by the time it is given.
        var looks = 0
        let start = Date()
        let observed = await ManagedServices.settle(to: .running, within: 1.0, interval: 0.1) {
            looks += 1
            try? await Task.sleep(nanoseconds: 400_000_000)
            return .stopped
        }
        let elapsed = Date().timeIntervalSince(start)
        XCTAssertEqual(observed, .stopped)
        XCTAssertGreaterThan(looks, 1)
        XCTAssertLessThan(elapsed, 3.0)
    }

    func testATimedSettleDoesNotSleepPastItsBudget() async {
        // A step longer than what is left must not carry the wait past its budget.
        let start = Date()
        let observed = await ManagedServices.settle(to: .running, within: 0.2, interval: 30) {
            .stopped
        }
        XCTAssertEqual(observed, .stopped)
        XCTAssertLessThan(Date().timeIntervalSince(start), 5.0)
    }

    // MARK: - A stop the owner asked for

    func testAStopIsRemembered() {
        var stops = OwnerStops()
        stops.record(.stop, id: "botfleet-mcp")
        XCTAssertEqual(stops.ids, ["botfleet-mcp"])
    }

    func testAStartTakesTheRecordBack() {
        var stops = OwnerStops(ids: ["botfleet-mcp"])
        stops.record(.start, id: "botfleet-mcp")
        XCTAssertEqual(stops.ids, [])
    }

    func testARestartTakesTheRecordBack() {
        // A restart is a start as far as the record is concerned: the owner
        // asked for the service to be running.
        var stops = OwnerStops(ids: ["botfleet-mcp"])
        stops.record(.restart, id: "botfleet-mcp")
        XCTAssertEqual(stops.ids, [])
    }

    func testARefusedChangePutsTheRecordBackTheWayItWas() {
        // A refused change is not a stop the owner asked for, so the record
        // goes back to what it was rather than to what the action would have
        // made of it.
        var stops = OwnerStops()
        stops.record(.stop, id: "botfleet-mcp")
        let previous = stops.ids
        stops.record(.start, id: "botfleet-mcp")
        stops.restore(previous)
        XCTAssertEqual(stops.ids, ["botfleet-mcp"])
    }

    func testARefusedStartDoesNotRecordAStop() {
        // The owner asked for the service to run, so a start that was
        // refused must not leave the service recorded as stopped on purpose.
        var stops = OwnerStops()
        let previous = stops.ids
        stops.record(.start, id: "botfleet-mcp")
        stops.restore(previous)
        XCTAssertEqual(stops.ids, [])
    }

    func testAServiceTheOwnerStoppedIsNotWanted() {
        // The BotFleet MCP Server is wanted exactly while the BotFleet
        // Server runs, but a stop the owner asked for is not one the Control
        // app undoes.
        let services = FleetServices(serverState: .running,
                                    states: ["botfleet-mcp": .stopped],
                                    stoppedByOwner: ["botfleet-mcp"])
        let entry = services.entries.first { $0.id == "botfleet-mcp" }
        XCTAssertEqual(entry?.wanted, false)
    }

    func testAServiceTheOwnerStoppedIsWantedAgainWhenTheOwnerStartsIt() {
        let services = FleetServices(serverState: .running, states: ["botfleet-mcp": .stopped])
        let entry = services.entries.first { $0.id == "botfleet-mcp" }
        XCTAssertEqual(entry?.wanted, true)
    }

    func testTheOwnersStopsKeepTheirTimes() {
        let when = Date(timeIntervalSince1970: 1_800_000_000)
        var stops = OwnerStops()
        stops.record(.stop, id: "shellular", at: when)
        XCTAssertEqual(stops.stoppedAt["shellular"], when)
        let before = stops
        stops.record(.start, id: "shellular")
        XCTAssertNil(stops.stoppedAt["shellular"])
        // Putting the whole record back brings the time back too.
        stops.restore(before)
        XCTAssertEqual(stops.ids, ["shellular"])
        XCTAssertEqual(stops.stoppedAt["shellular"], when)
    }

    // MARK: - Stopping the BotFleet Server

    func testStoppingTheBotFleetServerAsksTheServerToStopItself() async throws {
        // launchd does not always own the server process: the installed
        // wrapper exits when the harness is already healthy, and the desktop
        // app forks its own harness when nothing answers on the port.  A
        // bootout can leave a server that is still serving.
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = LaunchAgentCommands()
        let asked = Counter()
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil,
            stopServer: { asked.increment() })
        _ = try await control.perform(.stop, serviceID: FleetServiceRegistry.server.id)
        XCTAssertEqual(asked.value, 1)
        XCTAssertFalse(commands.calls.contains { $0.contains("bootout") })
    }

    func testTheMCPWaitIsBoundedByItsWindowAndNotByTheCostOfALook() async throws {
        // A look at the MCP server is an HTTP probe, and a probe that hangs
        // costs its whole request timeout.  A count of looks would let the
        // menu bar app sit busy for far longer than the window it
        // promises, so the wait is bounded by the time it is given.
        FleetServiceURLProtocol.handler = { _ in
            Thread.sleep(forTimeInterval: 0.5)
            return .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let mcp = try McpServerControl(session: mockSession(),
                                       baseURL: URL(string: "http://127.0.0.1:8794"),
                                       pm2: PM2Client(commands: FleetServiceCommands(),
                                                      nodePath: "/usr/bin/node",
                                                      pm2Path: "/usr/bin/pm2"))
        // The server never goes away, so the wait runs for the whole window
        // instead of ending as soon as the wanted state is seen.
        let start = Date()
        let observed = try await mcp.setRunning(false, within: 2)
        let elapsed = Date().timeIntervalSince(start)
        XCTAssertEqual(observed, .running)
        XCTAssertLessThan(elapsed, 8.0)
    }

    func testTheServersStopIsWaitedForWithTheSameWindowAsThePM2Wait() {
        // The window the stop promises has to be the window it gets, and it
        // is the same twenty seconds the pm2 wait gives.
        XCTAssertEqual(FleetServiceControl.settleInterval, 2.5)
        XCTAssertEqual(Double(PM2Client.settleAttempts) * PM2Client.settleInterval, 20.0)
        XCTAssertEqual(Double(PM2Client.settleAttempts) * FleetServiceControl.settleInterval, 20.0)
    }

    func testTheServersStopReportsTheStateThatWasObserved() async throws {
        // The server answers the stop request before it has exited, so the
        // state that is reported is the one that was observed after it and
        // not a constant that happens to be the expected one.
        //
        // The home is a directory of its own, because the state of the
        // server is read from the LaunchAgent plist that is installed: a
        // machine that has the BotFleet Server installed and a build
        // machine that has none would otherwise answer differently.
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = LaunchAgentCommands()
        commands.listing = "PID\tStatus\tLabel\n123\t0\tapp.botfleet.server"
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil,
            stopServer: { })
        let state = try await control.perform(.stop, serviceID: FleetServiceRegistry.server.id, attempts: 2)
        XCTAssertEqual(state, .running)
    }

    func testTheServersStopGivesUpWithinItsBudgetWhenALookIsSlow() async throws {
        // A look can cost a whole request timeout, so the budget is time and
        // not a number of looks.  A count of eight would run nine looks here.
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let looks = Counter()
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: LaunchAgentCommands(), uid: 501, environment: [:]),
            mcpServer: nil,
            stopServer: { },
            observeServer: {
                looks.increment()
                try? await Task.sleep(nanoseconds: 200_000_000)
                return .running
            },
            agentSettleInterval: 0.05)
        let state = try await control.perform(.stop, serviceID: FleetServiceRegistry.server.id, attempts: 8)
        XCTAssertEqual(state, .running)
        XCTAssertLessThan(looks.value, 9)
    }

    func testAPM2ThatTimesOutDuringARestartIsNotKnownAndNotARefusal() async throws {
        // pm2 can take a restart and then be slow to answer,  so the outcome is not known.
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = FleetServiceCommands()
        commands.restartFailure = CommandTimedOutError(executable: "/usr/bin/node",
                                                       arguments: ["/usr/bin/pm2", "restart", "shellular"])
        let control = FleetServiceControl(pm2: pm2(commands),
                                          startup: StartupPolicy(home: home, commands: LaunchAgentCommands(), uid: 501, environment: [:]),
                                          mcpServer: nil)
        do {
            _ = try await control.perform(.restart, serviceID: "shellular")
            XCTFail("a restart that timed out should not be reported as a success")
        } catch {
            XCTAssertEqual(error as? FleetServiceError,
                           .notKnown("pm2 did not finish in time to restart shellular, so it is not known whether it did."))
        }
    }

    func testStoppingTheBotFleetServerIsRefusedWhenTheServerCannotBeAsked() async throws {
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let control = FleetServiceControl(pm2: pm2(FleetServiceCommands()),
                                          startup: StartupPolicy(home: home, commands: LaunchAgentCommands(), uid: 501, environment: [:]),
                                          mcpServer: nil)
        do {
            _ = try await control.perform(.stop, serviceID: FleetServiceRegistry.server.id)
            XCTFail("the stop should not have been reported as a success")
        } catch {
            XCTAssertTrue("\(error)".contains("stop itself"))
            // The app is Muster;  the retired BotFleet Control name is not shown.
            XCTAssertTrue("\(error)".contains("Muster could not ask it to"))
            XCTAssertFalse("\(error)".contains("Control app"))
        }
    }

    // MARK: - Restarting the BotFleet Server
    func testARestartThatDoesNotReplaceTheServerIsNotReportedAsASuccess() async throws {
        // A kickstart replaces the process the launchd job owns.  When the
        // job owns no process, the server that is running was started by
        // something else and the restart changed nothing.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let marker = home.appendingPathComponent(".botfleet/stopped.marker")
        try Data().write(to: marker)
        let commands = LaunchAgentCommands()
        commands.loaded = false
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil)
        do {
            _ = try await control.perform(.restart, serviceID: FleetServiceRegistry.server.id, attempts: 2)
            XCTFail("a restart that replaced nothing should not be reported as a success")
        } catch {
            XCTAssertTrue("\(error)".contains("not replaced"))
        }
        // The restart did not replace the server, so the owner's deliberate
        // stop stands: the marker has to be back where it was, or the
        // wrapper would bring the server back at the next launchd run.
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path))
    }

    func testTheMarkerIsNotPutBackWhileTheServerItselfIsRunning() async throws {
        // launchd's view of the wrapper says no process, but the server's own
        // API and port say it is serving.  That is not proof the server is
        // down, so the deliberate-stop marker must not come back.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let marker = home.appendingPathComponent(".botfleet/stopped.marker")
        try Data().write(to: marker)
        let commands = LaunchAgentCommands()
        commands.loaded = false
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil,
            observeServer: { .running })
        do {
            _ = try await control.perform(.restart, serviceID: FleetServiceRegistry.server.id, attempts: 2)
            XCTFail("a restart that replaced nothing should not be reported as a success")
        } catch {
            XCTAssertTrue("\(error)".contains("not replaced"))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
    }

    func testARefusedRestartDoesNotPutTheMarkerBackWhenTheStateIsUnknown() async throws {
        // launchctl could not be asked, so nothing is known about the
        // server.  The marker must not be re-created on that, and the
        // answer is not a refusal that claims the server was not replaced.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let marker = home.appendingPathComponent(".botfleet/stopped.marker")
        try Data().write(to: marker)
        let commands = LaunchAgentCommands()
        commands.loaded = false
        commands.listingFails = true
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil)
        do {
            _ = try await control.perform(.restart, serviceID: FleetServiceRegistry.server.id, attempts: 2)
            XCTFail("a restart that could not be checked should not be reported as a success")
        } catch {
            XCTAssertEqual(error as? FleetServiceError,
                           .notKnown("launchctl could not be asked after the restart"))
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
    }

    func testTheStopCopyPromisesOnlyWhatTheMenuOffers() {
        // On a wrapper that cannot carry the startup policy the row has no
        // Restart, so the copy must not point the owner at one.
        let services = FleetServices(serverState: .running,
                                    states: ["app.botfleet.server": .running],
                                    startupPolicySupported: false)
        let row = services.menuItems.first { $0.id == "app.botfleet.server" }
        let body = FleetServices.stopBody(for: row!, startupPolicySupported: false)
        XCTAssertFalse(body.contains("Restart brings it back"))
        XCTAssertTrue(body.contains("stopped.marker"))
    }

    func testTheStopCopyOnAWrapperThatCanRestartPromisesTheRestart() {
        let services = FleetServices(serverState: .running,
                                    states: ["app.botfleet.server": .running],
                                    startupPolicySupported: true)
        let row = services.menuItems.first { $0.id == "app.botfleet.server" }
        let body = FleetServices.stopBody(for: row!, startupPolicySupported: true)
        XCTAssertTrue(body.contains("Restart brings it back"))
        XCTAssertFalse(body.contains("marker"))
    }

    func testTheStopCopyNamesTheMacInEveryBranch() {
        for supported in [true, false] {
            for id in [FleetServiceRegistry.server.id, "app.botfleet.companion", "shellular"] {
                let body = FleetServices.stopBody(forServiceID: id, startupPolicySupported: supported)
                XCTAssertTrue(body.contains("on the Mac"), body)
                XCTAssertFalse(body.contains("this Mac"), body)
            }
        }
    }

    func testPM2AnsweredIsDecidedByItsOwnApplications() {
        XCTAssertFalse(FleetServices(serverState: .running, states: ["shellular": .unknown]).pm2Answered)
        XCTAssertTrue(FleetServices(serverState: .running, states: ["shellular": .stopped]).pm2Answered)
        // The MCP server is read through its own health endpoint,  so it says nothing about pm2.
        XCTAssertFalse(FleetServices(serverState: .running, states: ["botfleet-mcp": .running]).pm2Answered)
    }

    func testAServerRestartWaitsNoLongerThanTheCallerAskedFor() async throws {
        // The launchd path has to honour the caller's wait, the way the pm2
        // path does, or a caller cannot bound the time it spends here.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let commands = LaunchAgentCommands()
        commands.loaded = false
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil)
        _ = try? await control.perform(.restart, serviceID: FleetServiceRegistry.server.id, attempts: 2)
        // Two attempts and the look that follows them are three looks, and
        // every one of them asks launchctl what the state is.
        let looks = commands.calls.filter { $0.contains("launchctl list") }.count
        XCTAssertEqual(looks, 3)
    }

    func testRestartingTheBotFleetServerClearsTheDeliberateStopMarker() async throws {
        // The installed wrapper stays down while the deliberate-stop marker
        // exists, so a restart that left the marker in place would report a
        // server that never came back.
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let marker = home.appendingPathComponent(".botfleet/stopped.marker")
        try Data().write(to: marker)

        let commands = LaunchAgentCommands()
        commands.loaded = true
        commands.listing = "PID\tStatus\tLabel\n123\t0\tapp.botfleet.server"
        let control = FleetServiceControl(
            pm2: pm2(FleetServiceCommands()),
            startup: StartupPolicy(home: home, commands: commands, uid: 501, environment: [:]),
            mcpServer: nil)
        _ = try await control.perform(.restart, serviceID: FleetServiceRegistry.server.id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
    }

    // MARK: - The BotFleet MCP Server keeps its own health endpoint
    func testTheMCPServerIsObservedThroughItsOwnHealthEndpoint() async throws {
        FleetServiceURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let control = makeControl(commands: FleetServiceCommands(), home: home)
        let states = await control.states(serverState: .running)
        XCTAssertEqual(states["botfleet-mcp"], .running)
    }

    func testTheMCPServerIsUnknownWhenItsHealthEndpointDoesNotAnswer() async throws {
        FleetServiceURLProtocol.handler = { _ in .failure(URLError(.timedOut)) }
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let control = makeControl(commands: FleetServiceCommands(), home: home)
        let states = await control.states(serverState: .running)
        XCTAssertEqual(states["botfleet-mcp"], .unknown)
    }

    func testTheOtherPM2ApplicationsAreReadFromPM2() async throws {
        FleetServiceURLProtocol.handler = { _ in .failure(URLError(.timedOut)) }
        let commands = FleetServiceCommands()
        commands.pm2Listing = """
        [{"name": "shellular", "pid": 4242, "pm2_env": {"status": "online"}},
         {"name": "grok-acp", "pid": 0, "pm2_env": {"status": "errored"}}]
        """
        let home = try makeHome(serverInstalled: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let control = makeControl(commands: commands, home: home)
        let states = await control.states(serverState: .running)
        XCTAssertEqual(states["shellular"], .running)
        XCTAssertEqual(states["grok-acp"], .failed)
        // An application pm2 has no record of is not stopped:  it is not in this Mac's pm2.
        XCTAssertEqual(states["seat-mcp"], .notInPM2)
    }

    // MARK: - Helpers

    private func wanted(_ policy: FleetServiceWantedPolicy,
                         serverState: ManagedServices.ServerState = .running,
                         state: ManagedServiceState = .running) -> Bool? {
        let descriptor = FleetServiceDescriptor(id: "shellular", title: "Shellular",
                                                kind: .pm2(appName: "shellular"), policy: policy)
        let states = policy == .alwaysOn ? ["shellular": state] : [:]
        return FleetServices(serverState: serverState, states: states).wanted(for: descriptor)
    }

    private func pm2(_ commands: FleetServiceCommands,
                     home: URL = URL(fileURLWithPath: "/Users/tester", isDirectory: true)) -> PM2Client {
        PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2", home: home)
    }

    /// A home directory of its own, with the LaunchAgents the fleet
    /// services read.
    ///
    /// A test must not read the LaunchAgents of the machine it runs on: a
    /// workstation that has the BotFleet Server installed answers a
    /// different state from a build machine that has none.
    private func makeHome(serverInstalled: Bool = false) throws -> URL {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"),
                                                withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.companion.plist"))
        if serverInstalled {
            try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        }
        return home
    }

    /// A control that reads the fleet services out of a home of its own.
    ///
    /// The home is the caller's, so it can be removed when the test is
    /// done with it.
    private func makeControl(commands: FleetServiceCommands,
                             home: URL) -> FleetServiceControl {
        FleetServiceControl(pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node",
                                           pm2Path: "/usr/bin/pm2", home: home),
                            startup: StartupPolicy(home: home, commands: LaunchAgentCommands(), uid: 501, environment: [:]),
                            mcpServer: try? McpServerControl(session: mockSession(),
                                                            baseURL: URL(string: "http://127.0.0.1:8794"),
                                                            pm2: PM2Client(commands: commands,
                                                                           nodePath: "/usr/bin/node",
                                                                           pm2Path: "/usr/bin/pm2", home: home)))
    }

    private func mockSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FleetServiceURLProtocol.self]
        return URLSession(configuration: configuration)
    }
}

/// Counts how many times something was asked for.
private final class Counter {
    private(set) var value = 0
    func increment() { value += 1 }
}

/// A stand-in for the pm2 client.
///
/// pm2 is run through node, so the arguments the mock sees begin with the
/// path of the pm2 client itself.  The mock records the arguments pm2 was
/// given, without that path.
private final class FleetServiceCommands: CommandRunning {
    var calls: [[String]] = []
    /// What `pm2 jlist` answers with.
    var pm2Listing = ""
    /// What `pm2 jlist` answers with once a change has been made.
    var pm2ListingAfterChange: String?
    /// The failure every command raises.
    var failure: Error?
    /// The failure only `pm2 start` raises.
    var startFailure: Error?
    /// The failure only `pm2 restart` raises.
    var restartFailure: Error?

    func run(_ executable: String, _ arguments: [String]) throws -> String {
        let pm2Arguments = Array(arguments.dropFirst())
        calls.append(pm2Arguments)
        if pm2Arguments.first == "start", let startFailure {
            // pm2 refuses the first start, so the ecosystem file is the way
            // back.
            self.startFailure = nil
            throw startFailure
        }
        if pm2Arguments.first == "restart", let restartFailure {
            // pm2 refuses the first restart, so the ecosystem file is the way
            // back.
            self.restartFailure = nil
            throw restartFailure
        }
        if let failure { throw failure }
        if pm2Arguments.first == "jlist" { return pm2Listing }
        if pm2Arguments.first == "start" || pm2Arguments.first == "stop" || pm2Arguments.first == "restart" {
            if let pm2ListingAfterChange { pm2Listing = pm2ListingAfterChange }
            return ""
        }
        return ""
    }
}

/// A stand-in for the commands launchctl is asked with.
private final class LaunchAgentCommands: CommandRunning {
    var calls: [String] = []
    /// What `launchctl list` answers with.
    var listing = "PID\tStatus\tLabel\n-\t0\tother.service"
    /// True when launchctl already knows the agent.
    var loaded = false
    /// True when launchctl cannot be asked at all.
    var listingFails = false
    /// The boot id the startup policy records.
    var boot = UUID().uuidString

    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append(([executable] + arguments).joined(separator: " "))
        if executable == "/usr/sbin/sysctl" { return boot }
        if arguments == ["list"] {
            if listingFails { throw StartupPolicyError.commandFailed }
            return listing
        }
        if arguments.first == "print" {
            if loaded { return "state = running" }
            throw StartupPolicyError.commandFailed
        }
        return ""
    }
}

/// A stand-in for the health endpoint of the BotFleet MCP server.
private final class FleetServiceURLProtocol: URLProtocol {
    enum Reply {
        case response(Int, Data)
        case failure(Error)
    }
    static var handler: ((URLRequest) -> Reply) = { _ in .response(500, Data()) }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        switch Self.handler(request) {
        case .response(let status, let body):
            guard let url = request.url,
                  let response = HTTPURLResponse(url: url, statusCode: status,
                                                 httpVersion: nil, headerFields: nil) else {
                client?.urlProtocol(self, didFailWithError: URLError(.badURL))
                return
            }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        case .failure(let error):
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
