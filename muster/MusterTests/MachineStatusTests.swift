import XCTest

/// What the menu bar says.  Views are not in the test bundle,  so the menu's words are built
/// in the model and read here as strings,  against the design's own wording.
final class MachineStatusTests: XCTestCase {
    private var directory: URL!
    /// 2:46 PM on the owner's clock.
    private let now = MachineStatusTests.time("2026-11-04T20:46:00Z")

    override func setUpWithError() throws {
        directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("machine-status-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private static func time(_ text: String) -> Date {
        guard let date = FleetTime.parse(text) else { fatalError("bad time \(text)") }
        return date
    }

    // MARK: - The fixtures

    private static let unitLines = [
        #"{"unit":"fleet-server-status.timer","scope":"user","activeState":"active","subState":"waiting","controllable":true}"#,
        #"{"unit":"fleet-cli-git-update.timer","scope":"system","activeState":"active","subState":"waiting","controllable":true}"#,
    ]

    private static let healthyMetrics =
        #"{"loadAverage":[0.5],"cpuCount":8,"memory":{"totalBytes":100,"availableBytes":56},"disks":[{"mount":"/","usedBytes":75,"freeBytes":25}]}"#

    /// A report on disk,  read back through the app's own reader.
    private func statusReading(
        generatedAt: String = "2026-11-04T20:45:00Z",
        copiedAt: String = "2026-11-04T20:46:00Z",
        schemaVersion: Int = 1,
        hostRole: String? = nil,
        units: [String] = MachineStatusTests.unitLines,
        running: Int = 17,
        exited: Int = 0,
        absent: Int = 0,
        starting: Int = 0,
        missingFiles: Int = 0,
        metrics: String? = MachineStatusTests.healthyMetrics
    ) throws -> ServerStatusReading {
        var apps: [String] = []
        for index in 0..<running { apps.append(#"{"uuid":"r\#(index)","name":"run\#(index)","type":"application","status":"running"}"#) }
        for index in 0..<exited { apps.append(#"{"uuid":"e\#(index)","name":"exit\#(index)","type":"application","status":"exited"}"#) }
        for index in 0..<absent { apps.append(#"{"uuid":"a\#(index)","name":"gone\#(index)","type":"application","status":"absent"}"#) }
        for index in 0..<starting { apps.append(#"{"uuid":"s\#(index)","name":"start\#(index)","type":"application","status":"starting"}"#) }
        var files: [String] = []
        for index in 0..<missingFiles { files.append(#"{"path":"/watched/\#(index)","drift":"missing"}"#) }
        var fields = [
            #""schemaVersion":\#(schemaVersion)"#,
            #""tool":"fleet-server-status""#,
            #""generatedAt":"\#(generatedAt)""#,
            #""host":"a-host""#,
            #""coolify":[\#(apps.joined(separator: ","))]"#,
            #""processes":[{"pid":1,"name":"init"}]"#,
            #""files":[\#(files.joined(separator: ","))]"#,
            #""services":[\#(units.joined(separator: ","))]"#,
        ]
        if let hostRole { fields.append(#""hostRole":"\#(hostRole)""#) }
        if let metrics { fields.append(#""hostMetrics":\#(metrics)"#) }
        let url = directory.appendingPathComponent("server-status-\(UUID().uuidString).json")
        try Data("{\(fields.joined(separator: ","))}".utf8).write(to: url)
        try FileManager.default.setAttributes([.modificationDate: Self.time(copiedAt)], ofItemAtPath: url.path)
        return ServerStatusReportReader.read(at: url, now: now)
    }

    private func missingStatus() -> ServerStatusReading {
        ServerStatusReportReader.read(at: directory.appendingPathComponent("not-there.json"), now: now)
    }

    /// The doctor's report on disk,  read back through the app's own reader.
    private func toolsReading(
        generatedAt: String = "2026-11-04T20:45:00Z",
        schemaVersion: Int = 1,
        serverReachable: Bool = true
    ) throws -> FleetToolsReading {
        let json = #"{"schemaVersion":\#(schemaVersion),"tool":"fleet-tools-doctor","generatedAt":"\#(generatedAt)","hosts":[{"host":"mac","reachable":true,"checks":[]},{"host":"server","reachable":\#(serverReachable),"checks":[]}]}"#
        let url = directory.appendingPathComponent("report-\(UUID().uuidString).json")
        try Data(json.utf8).write(to: url)
        return FleetToolsReportReader.read(at: url, now: now)
    }

    private static let allowlist = OpsAllowlist(
        macAgents: [OpsAllowlist.Entry(name: OpsNames.fetchAgentLabel, actions: ["start", "stop", "restart"])],
        pm2Apps: ["botfleet-mcp", "shellular", "code-main-keeper", "vision-worker", "xcode-health",
                  "agy-acp", "grok-leader", "grok-acp", "clutch-web", "seat-mcp"].map {
            OpsAllowlist.Entry(name: $0, actions: ["start", "stop", "restart"])
        },
        serverUnits: [
            OpsAllowlist.Entry(name: "fleet-server-status.timer", actions: ["start", "stop", "restart"]),
            OpsAllowlist.Entry(name: "fleet-cli-git-update.timer", actions: ["start", "stop", "restart"]),
        ]
    )

    /// Nine running,  two stopped,  one not installed,  and the Mac's listener retired and
    /// stopped,  as it has been since the one-listener cutover.
    private static func macServices(changing changes: [String: ManagedServiceState] = [:]) -> FleetServices {
        var states: [String: ManagedServiceState] = [
            "botfleet-mcp": .running, "app.botfleet.server": .running, "app.botfleet.companion": .notInstalled,
            "com.jay.operator": .stopped,
            "shellular": .running, "code-main-keeper": .running, "vision-worker": .running,
            "xcode-health": .running, "agy-acp": .stopped, "grok-leader": .stopped,
            "grok-acp": .running, "clutch-web": .running, "seat-mcp": .running,
        ]
        states.merge(changes) { _, new in new }
        return FleetServices(serverState: .running, states: states)
    }

    private func inputs(
        services: FleetServices? = MachineStatusTests.macServices(),
        lastReadAt: Date? = nil,
        redCells: [DriftRedCell]? = [],
        driftCells: [DriftRedCell] = [],
        unprovenCells: [DriftRedCell] = [],
        lastRunWasTrusted: Bool? = true,
        transitions: [DriftTransition] = [],
        status: ServerStatusReading?,
        tools: FleetToolsReading?,
        asked: ServerAskResult? = nil,
        allowlist: OpsAllowlist? = MachineStatusTests.allowlist,
        actionState: ServiceActionState? = nil
    ) -> MachineMenuInputs {
        MachineMenuInputs(
            services: services,
            lastReadAt: lastReadAt ?? now,
            redCells: redCells,
            driftCells: driftCells,
            unprovenCells: unprovenCells,
            lastRunWasTrusted: lastRunWasTrusted,
            transitions: transitions,
            status: status,
            tools: tools,
            asked: asked,
            allowlist: allowlist,
            actionState: actionState,
            now: now
        )
    }

    private func healthyMenu() throws -> MachineMenuInputs {
        inputs(status: try statusReading(), tools: try toolsReading())
    }

    private func lines(_ menu: MachineMenu) -> [String] {
        menu.plainText.components(separatedBy: "\n")
    }

    private func assertLines(_ menu: MachineMenu, contain wanted: [String],
                             file: StaticString = #filePath, line: UInt = #line) {
        let found = lines(menu)
        for text in wanted {
            XCTAssertTrue(found.contains(text), "Missing:  \(text)\nIn:\n\(menu.plainText)", file: file, line: line)
        }
    }

    // MARK: - The whole menu

    func testTheMenuWhenAllIsWellReadsTheDesignWordForWord() throws {
        let menu = MachineMenu(try healthyMenu())
        let expected = """
        The Mac
        Checked at 2:46 PM
        Services:  9 running, 2 stopped, 1 not installed, 1 retired
        Seat Setup:  no red or drifted cells
        Services On The Mac (13)
            BotFleet MCP Server (On The Mac):  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            BotFleet Server (On The Mac):  running
                Restart On The Mac
                Stop On The Mac\u{2026}
            BotFleet Companion:  not installed
            Operator Eavesdropper:  retired (since the Nov 10 one-listener cutover)
            Shellular:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            Code Main Keeper:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            Vision Worker:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            Xcode Health:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            Antigravity ACP:  stopped
                Start On The Mac\u{2026}
                Restart On The Mac\u{2026}
            Grok Leader:  stopped
                Start On The Mac\u{2026}
                Restart On The Mac\u{2026}
            Grok ACP:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            Clutch Web:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
            Seat MCP:  running
                Restart On The Mac\u{2026}
                Stop On The Mac\u{2026}
        Check The Mac Again
        ----------
        The Server
        Report from 2:45 PM, copied at 2:46 PM
        Timers:  2 of 2 active
        Apps:  17 running, 0 failed
        Fullest disk 75%, memory 44%
        Timers On The Server (2)
            Server Report Timer:  active
                Restart On The Server\u{2026}
                Read Logs On The Server\u{2026}
            Fleet Tools Update Timer:  active
                Restart On The Server\u{2026}
                Read Logs On The Server\u{2026}
        Open Server Status
        Ask The Server Now
        ----------
        Open Muster
        Launch Muster At Login
        """
        XCTAssertEqual(menu.plainText, expected)
        XCTAssertEqual(menu.barTitle(short: false), "Mac OK \u{00B7} Server OK")
        XCTAssertEqual(menu.barTitle(short: true), "M OK \u{00B7} S OK")
        XCTAssertEqual(menu.barSymbol, "shield")
    }

    func testAProblemOnTheMacComesToTheTopOfItsSection() throws {
        let red = DriftRedCell(seat: "CLAUDE", kind: ConfigKind.hooks.rawValue,
                               ruleID: "claude-hooks-lane-guard", state: .missing)
        let turned = DriftTransition(id: UUID(), at: Self.time("2026-11-04T19:47:00Z"), seat: "CLAUDE",
                                     kind: ConfigKind.hooks.rawValue, ruleID: "claude-hooks-lane-guard",
                                     fromState: "PASS", toState: "MISSING")
        let menu = MachineMenu(inputs(
            services: Self.macServices(changing: ["shellular": .failed]),
            redCells: [red],
            transitions: [turned],
            status: try statusReading(),
            tools: try toolsReading()
        ))
        let kind = ConfigKind.hooks.title
        let top = Array(lines(menu).prefix(10))
        XCTAssertEqual(top, [
            "The Mac",
            "Checked at 2:46 PM",
            "Shellular:  failed, should be running",
            "    Start On The Mac\u{2026}",
            "    Restart On The Mac\u{2026}",
            "Seat Setup:  1 red cell",
            "    CLAUDE / \(kind):  claude-hooks-lane-guard",
            "    Turned Red Most Recently",
            "    1:47 PM  CLAUDE / \(kind)",
            "    Open Compliance Grid",
        ])
        XCTAssertEqual(menu.barTitle(short: false), "Mac 2 \u{00B7} Server OK")
        XCTAssertEqual(menu.barSymbol, "exclamationmark.shield")
    }

    // MARK: - Every action names its machine

    private func actions(in items: [MenuItem]) -> [MenuAction] {
        items.flatMap { item -> [MenuAction] in
            switch item {
            case .line, .command: return []
            case .row(let row): return row.actions
            case .group(_, let children): return actions(in: children)
            case .action(let action): return [action]
            }
        }
    }

    func testEveryActionNamesTheMachineItsRouteRunsOnAndSitsUnderThatMachine() throws {
        let menus = [
            MachineMenu(try healthyMenu()),
            MachineMenu(inputs(status: try statusReading(generatedAt: "2026-11-04T19:02:00Z"), tools: try toolsReading())),
        ]
        for menu in menus {
            let onMac = actions(in: menu.mac.items)
            let onServer = actions(in: menu.server.items)
            XCTAssertFalse(onMac.isEmpty)
            XCTAssertFalse(onServer.isEmpty)
            for action in onMac {
                XCTAssertTrue(action.title.contains(action.route.runsOn.onTitle), action.title)
                XCTAssertEqual(action.route.runsOn, .mac, action.title)
            }
            for action in onServer {
                XCTAssertTrue(action.title.contains(action.route.runsOn.onTitle), action.title)
                XCTAssertEqual(action.route.runsOn, .server, action.title)
            }
        }
    }

    func testAnEllipsisAppearsExactlyWhenSomethingOpensBeforeTheActionRuns() throws {
        for action in actions(in: MachineMenu(try healthyMenu()).mac.items) {
            XCTAssertEqual(action.title.hasSuffix("\u{2026}"), action.route.asksFirst, action.title)
        }
    }

    func testStopOnAServerTimerStaysOutOfTheMenu() throws {
        let failed = #"{"unit":"fleet-server-status.timer","scope":"user","activeState":"failed","controllable":true}"#
        let active = #"{"unit":"fleet-cli-git-update.timer","scope":"system","activeState":"active","controllable":true}"#
        let menu = MachineMenu(inputs(status: try statusReading(units: [failed, active]), tools: try toolsReading()))
        let titles = actions(in: menu.server.items).map(\.title)
        XCTAssertFalse(titles.contains { $0.hasPrefix("Stop") }, "\(titles)")
        // A failed timer offers a start and its logs,  and it counts against the server.
        XCTAssertTrue(titles.contains("Start On The Server\u{2026}"))
        XCTAssertEqual(menu.server.health, .attention(1, someUnread: false))
    }

    func testAServerVerbTheAllowlistDoesNotNameIsNotOffered() throws {
        let reading = try statusReading()
        let none = MachineMenu(inputs(status: reading, tools: try toolsReading(), allowlist: nil))
        XCTAssertTrue(actions(in: none.server.items).isEmpty)
        let startOnly = OpsAllowlist(serverUnits: [
            OpsAllowlist.Entry(name: "fleet-server-status.timer", actions: ["start"]),
        ])
        let some = MachineMenu(inputs(status: reading, tools: try toolsReading(), allowlist: startOnly))
        XCTAssertEqual(actions(in: some.server.items).map(\.title), ["Read Logs On The Server\u{2026}"])
    }

    // MARK: - The server's reach,  first match wins

    func testTheMacsOwnFetchStoppingIsCountedOnTheMacAndSaysSo() throws {
        let menu = MachineMenu(inputs(
            status: try statusReading(generatedAt: "2026-11-04T16:45:00Z"),
            tools: try toolsReading(generatedAt: "2026-11-04T17:02:00Z")
        ))
        assertLines(menu, contain: [
            "The Mac stopped fetching the server's report.\u{00A0} Last fetch at 11:02 AM.",
            "Last report from 10:45 AM, so it is not current.\u{00A0} Whether the server is up is not known.",
            "Timers On The Server:  not known",
            "Fetch From The Server Again\u{2026}",
            "Ask The Server Now",
        ])
        XCTAssertEqual(menu.barTitle(short: false), "Mac 1 \u{00B7} Server ?")
        XCTAssertTrue(actions(in: menu.server.items).isEmpty)
    }

    func testAnUnreachableServerWinsOverAReportStillInsideItsWindow() throws {
        let menu = MachineMenu(inputs(
            status: try statusReading(generatedAt: "2026-11-04T20:30:00Z"),
            tools: try toolsReading(serverReachable: false)
        ))
        assertLines(menu, contain: [
            "The Mac could not reach the server at 2:45 PM.\u{00A0} Last report from 2:30 PM, so it is not current.",
            "Timers On The Server:  not known",
            "Fetch From The Server Again\u{2026}",
        ])
        XCTAssertEqual(menu.barTitle(short: false), "Mac OK \u{00B7} Server ?")
        XCTAssertTrue(actions(in: menu.server.items).isEmpty)
    }

    func testAReachedServerWhoseReportStoppedOffersTheTimerRestart() throws {
        let menu = MachineMenu(inputs(
            status: try statusReading(generatedAt: "2026-11-04T19:02:00Z"),
            tools: try toolsReading()
        ))
        assertLines(menu, contain: [
            "Reached at 2:45 PM, but its report stopped at 1:02 PM.",
            "Restart Server Report Timer On The Server\u{2026}",
            "Read Its Logs On The Server\u{2026}",
            "Ask The Server Now",
        ])
        XCTAssertEqual(menu.barTitle(short: false), "Mac OK \u{00B7} Server 1")
        XCTAssertEqual(actions(in: menu.server.items).map(\.route), [
            .ops(.serverUnit(verb: .restart, unit: "fleet-server-status.timer")),
            .ops(.logs(unit: "fleet-server-status.timer")),
        ])
    }

    func testNoReportOnTheMacIsNotKnownAndOffersAFetch() throws {
        let menu = MachineMenu(inputs(status: missingStatus(), tools: try toolsReading()))
        assertLines(menu, contain: ["No server report on the Mac yet.", "Fetch From The Server Again\u{2026}"])
        XCTAssertEqual(menu.server.health, .notKnown)
        XCTAssertTrue(actions(in: menu.server.items).isEmpty)
    }

    func testAVersionRefusalReachesTheMenuAndSaysWhatToUpdate() throws {
        let newer = MachineMenu(inputs(status: try statusReading(schemaVersion: 2), tools: try toolsReading()))
        assertLines(newer, contain: ["This Muster cannot read the server's report.\u{00A0} Update Muster."])
        XCTAssertEqual(newer.server.health, .notKnown)
        XCTAssertTrue(actions(in: newer.server.items).isEmpty)

        let older = MachineMenu(inputs(status: try statusReading(schemaVersion: 0), tools: try toolsReading()))
        assertLines(older, contain: ["This Muster cannot read the server's report.\u{00A0} Update the server's reporter."])

        let doctor = MachineMenu(inputs(status: try statusReading(), tools: try toolsReading(schemaVersion: 2)))
        assertLines(doctor, contain: ["This Muster cannot read the server's report.\u{00A0} Update Muster."])
        XCTAssertEqual(doctor.server.health, .notKnown)
    }

    func testAReportThatDidNotComeFromTheServerIsRefusedAndOffersAFetch() throws {
        let menu = MachineMenu(inputs(status: try statusReading(hostRole: "mac"), tools: try toolsReading()))
        // The doctor's next fetch writes the server's own report over the wrong file.
        assertLines(menu, contain: ["This report did not come from the server.", "Fetch From The Server Again\u{2026}"])
        XCTAssertEqual(menu.server.health, .notKnown)
        XCTAssertTrue(actions(in: menu.server.items).isEmpty)
    }

    func testAReportThatSaysItCameFromTheServerReadsAsUsual() throws {
        let reading = try statusReading(hostRole: "server")
        XCTAssertNil(reading.problem)
        XCTAssertEqual(MachineMenu(inputs(status: reading, tools: try toolsReading())).server.health, .ok)
    }

    func testAReportDatedInTheFutureIsNotCurrent() throws {
        let menu = MachineMenu(inputs(status: try statusReading(generatedAt: "2026-11-04T22:00:00Z"), tools: try toolsReading()))
        assertLines(menu, contain: ["The server's report has a time ahead of the Mac's clock, so it is not current."])
        XCTAssertEqual(menu.server.health, .notKnown)
    }

    func testBeforeTheFirstReadNothingIsKnownAndNothingIsEmpty() throws {
        let menu = MachineMenu(inputs(services: nil, lastReadAt: nil, redCells: nil, lastRunWasTrusted: nil,
                                      status: nil, tools: nil))
        XCTAssertEqual(MachineMenu(MachineMenuInputs(now: now)).barTitle(short: false), "Mac ? \u{00B7} Server ?")
        assertLines(MachineMenu(MachineMenuInputs(now: now)), contain: [
            "Reading the Mac\u{2026}",
            "Seat Setup:  checking\u{2026}",
            "Reading the server's report on the Mac\u{2026}",
        ])
        XCTAssertEqual(menu.server.health, .notKnown)
    }

    // MARK: - The server's count

    func testTodaysServerReadsOK() throws {
        // 34 missing watched files and 167 absent rows are deliberately not counted:  the
        // panel still shows them.
        let reading = try statusReading(absent: 167, missingFiles: 34)
        let menu = MachineMenu(inputs(status: reading, tools: try toolsReading()))
        XCTAssertEqual(menu.server.health, .ok)
        assertLines(menu, contain: ["Apps:  17 running, 0 failed", "Timers:  2 of 2 active"])
    }

    func testADiskAt95PercentCountsOne() throws {
        let metrics = #"{"loadAverage":[0.5],"cpuCount":8,"memory":{"totalBytes":100,"availableBytes":56},"disks":[{"mount":"/","usedBytes":95,"freeBytes":5}]}"#
        let menu = MachineMenu(inputs(status: try statusReading(metrics: metrics), tools: try toolsReading()))
        XCTAssertEqual(menu.server.health, .attention(1, someUnread: false))
        XCTAssertEqual(menu.barTitle(short: false), "Mac OK \u{00B7} Server 1")
    }

    func testMemoryLoadAndAnExitedAppEachCount() throws {
        let metrics = #"{"loadAverage":[20.0],"cpuCount":8,"memory":{"totalBytes":100,"availableBytes":10},"disks":[{"mount":"/","usedBytes":10,"freeBytes":90}]}"#
        let menu = MachineMenu(inputs(status: try statusReading(exited: 1, metrics: metrics), tools: try toolsReading()))
        XCTAssertEqual(menu.server.health, .attention(3, someUnread: false))
        assertLines(menu, contain: ["Apps:  17 running, 1 failed", "Load 2.5 per cpu"])
    }

    func testACurrentReportThatProvesNothingIsNotOK() throws {
        let reading = try statusReading(units: [], running: 0, metrics: nil)
        let menu = MachineMenu(inputs(status: reading, tools: try toolsReading()))
        XCTAssertEqual(menu.server.health, .notKnown)
        assertLines(menu, contain: ["Timers On The Server:  not known", "Apps:  not known"])
    }

    /// Reads one report that has one part missing,  and says the bar is not OK.
    private func assertNotKnown(_ reading: ServerStatusReading, saying wanted: String,
                                file: StaticString = #filePath, line: UInt = #line) throws {
        let menu = MachineMenu(inputs(status: reading, tools: try toolsReading()))
        XCTAssertEqual(menu.server.health, .notKnown, file: file, line: line)
        XCTAssertEqual(menu.barTitle(short: false), "Mac OK \u{00B7} Server ?", file: file, line: line)
        assertLines(menu, contain: [wanted], file: file, line: line)
    }

    func testAReportWithNoTimersIsNotOK() throws {
        try assertNotKnown(try statusReading(units: []), saying: "Timers On The Server:  not known")
    }

    func testAReportWithNoHostNumbersIsNotOK() throws {
        let reading = try statusReading(metrics: nil)
        try assertNotKnown(reading, saying: "Fullest disk not known, memory not known")
        try assertNotKnown(reading, saying: "Load not known")
    }

    func testAReportWhoseAppsAreAllAbsentIsNotOK() throws {
        try assertNotKnown(try statusReading(running: 0, absent: 3), saying: "Apps:  not known")
    }

    func testAnAppInAStateThisAppDoesNotKnowIsNotOK() throws {
        try assertNotKnown(try statusReading(starting: 1), saying: "Apps:  17 running, 0 failed, 1 not known")
    }

    func testADiskThatCouldNotBeMeasuredIsNotOK() throws {
        let metrics = #"{"loadAverage":[0.5],"cpuCount":8,"memory":{"totalBytes":100,"availableBytes":56},"disks":[{"mount":"/","usedBytes":75,"freeBytes":25},{"mount":"/data"}]}"#
        try assertNotKnown(try statusReading(metrics: metrics), saying: "Fullest disk 75%, 1 more not known, memory 44%")
    }

    func testMemoryThatCouldNotBeMeasuredIsNotOK() throws {
        let metrics = #"{"loadAverage":[0.5],"cpuCount":8,"disks":[{"mount":"/","usedBytes":75,"freeBytes":25}]}"#
        try assertNotKnown(try statusReading(metrics: metrics), saying: "Fullest disk 75%, memory not known")
    }

    func testLoadThatCouldNotBeMeasuredIsNotOK() throws {
        let metrics = #"{"memory":{"totalBytes":100,"availableBytes":56},"disks":[{"mount":"/","usedBytes":75,"freeBytes":25}]}"#
        try assertNotKnown(try statusReading(metrics: metrics), saying: "Load not known")
    }

    func testAProblemBesideSomethingUnreadCountsWithAPlus() throws {
        let menu = MachineMenu(inputs(status: try statusReading(exited: 1, starting: 1), tools: try toolsReading()))
        XCTAssertEqual(menu.server.health, .attention(1, someUnread: true))
        XCTAssertEqual(menu.barTitle(short: false), "Mac OK \u{00B7} Server 1+")
    }

    func testAnAnswerFromTheServerAddsALineAndNeverTurnsTheBarToOK() throws {
        let asked = ServerAskResult(answered: true, at: now)
        let menu = MachineMenu(inputs(status: missingStatus(), tools: try toolsReading(), asked: asked))
        assertLines(menu, contain: ["The server answered at 2:46 PM."])
        XCTAssertEqual(menu.server.health, .notKnown)
        let silent = MachineMenu(inputs(status: missingStatus(), tools: try toolsReading(),
                                        asked: ServerAskResult(answered: false, at: now)))
        assertLines(silent, contain: ["The server did not answer at 2:46 PM."])
    }

    // MARK: - The Mac's health

    func testAReadOlderThanFiveMinutesIsNotCurrentAndKeepsItsActions() throws {
        let menu = MachineMenu(inputs(lastReadAt: now.addingTimeInterval(-301),
                                      status: try statusReading(), tools: try toolsReading()))
        assertLines(menu, contain: ["Last checked at 2:40 PM, so this is not current."])
        XCTAssertEqual(menu.mac.health, .notKnown)
        XCTAssertFalse(actions(in: menu.mac.items).isEmpty)
    }

    func testAnEngineThatCouldNotBeTrustedIsNotKnownAndNotZero() throws {
        let menu = MachineMenu(inputs(redCells: nil, lastRunWasTrusted: false,
                                      status: try statusReading(), tools: try toolsReading()))
        assertLines(menu, contain: ["Seat setup:  the last check could not be trusted."])
        XCTAssertEqual(menu.mac.health, .notKnown)
    }

    /// A run whose only non-green cells are unproven is not an OK Mac:  the line counts them
    /// and the health is not known, never OK.
    func testUnprovenCellsCountSoOKIsNotClaimed() throws {
        let unproven = (0..<3).map {
            DriftRedCell(seat: "S\($0)", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .unproven)
        }
        let menu = MachineMenu(inputs(unprovenCells: unproven, status: try statusReading(), tools: try toolsReading()))
        XCTAssertEqual(menu.mac.health, .notKnown)
        XCTAssertNotEqual(menu.mac.health, .ok)
        assertLines(menu, contain: ["Seat Setup:  no red or drifted cells, 3 not proven"])
        // The counted cells are listed and the grid is one click away.
        XCTAssertTrue(menu.plainText.contains("S0 / \(unproven[0].kindTitle):  not proven"), menu.plainText)
        XCTAssertTrue(menu.plainText.contains("Open Compliance Grid"), menu.plainText)
    }

    /// With a red cell as well, the unproven count rides in the group title.
    func testUnprovenCellsAreNamedBesideRedOnes() throws {
        let red = DriftRedCell(seat: "MM", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .missing)
        let unproven = DriftRedCell(seat: "MA", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .unproven)
        let menu = MachineMenu(inputs(redCells: [red], unprovenCells: [unproven],
                                      status: try statusReading(), tools: try toolsReading()))
        XCTAssertEqual(menu.mac.health, .attention(1, someUnread: false))
        assertLines(menu, contain: ["Seat Setup:  1 red cell, 1 not proven"])
    }

    func testDriftedCellsCountSoOKIsTrue() throws {
        let drifted = DriftRedCell(seat: "MM", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .drift)
        let menu = MachineMenu(inputs(driftCells: [drifted], status: try statusReading(), tools: try toolsReading()))
        XCTAssertEqual(menu.mac.health, .attention(1, someUnread: false))
        assertLines(menu, contain: ["Seat Setup:  1 drifted cell", "    MM / \(ConfigKind.hooks.title):  drifted"])
    }

    private static let pm2SilentStates: [String: ManagedServiceState] = [
        "shellular": .unknown, "code-main-keeper": .unknown, "vision-worker": .unknown,
        "xcode-health": .unknown, "agy-acp": .unknown, "grok-leader": .unknown,
        "grok-acp": .unknown, "clutch-web": .unknown, "seat-mcp": .unknown,
    ]

    func testPM2NotAnsweringIsNotAZeroAndThoseRowsLoseTheirRestart() throws {
        let services = Self.macServices(changing: Self.pm2SilentStates)
        XCTAssertFalse(services.pm2Answered)
        let menu = MachineMenu(inputs(services: services, status: try statusReading(), tools: try toolsReading()))
        assertLines(menu, contain: ["pm2 did not answer, so 9 services are not known."])
        XCTAssertEqual(menu.mac.health, .notKnown)
        let shellular = actions(in: menu.mac.items).filter { $0.serviceID == "shellular" }
        XCTAssertTrue(shellular.isEmpty)
        XCTAssertEqual(menu.barTitle(short: false), "Mac ? \u{00B7} Server OK")
    }

    func testPM2NotAnsweringBesideARealProblemReadsWithAPlus() throws {
        let red = DriftRedCell(seat: "MM", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .missing)
        let two = DriftRedCell(seat: "MA", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .unknown)
        let menu = MachineMenu(inputs(services: Self.macServices(changing: Self.pm2SilentStates),
                                      redCells: [red, two],
                                      status: try statusReading(), tools: try toolsReading()))
        XCTAssertEqual(menu.barTitle(short: false), "Mac 2+ \u{00B7} Server OK")
        assertLines(menu, contain: ["    MM / \(ConfigKind.hooks.title):  missing", "    MA / \(ConfigKind.hooks.title):  could not be read"])
    }

    func testARowWithNothingToOfferSaysWhyAndHasNoSubmenu() throws {
        let unwanted = FleetServices(serverState: .stopped, states: ["botfleet-mcp": .stopped, "shellular": .running])
        let entry = try XCTUnwrap(unwanted.entries.first { $0.id == "botfleet-mcp" })
        let row = MachineMenuCopy.serviceRow(entry, allowlist: Self.allowlist, pm2Answered: true)
        XCTAssertTrue(row.actions.isEmpty)
        XCTAssertEqual(row.title, "BotFleet MCP Server (On The Mac):  stopped, it starts when BotFleet Server is running")
    }

    func testAnApplicationPM2NeverHeardOfIsNotCalledStoppedAndHasNoStart() throws {
        let services = Self.macServices(changing: ["seat-mcp": .notInPM2])
        let entry = try XCTUnwrap(services.entries.first { $0.id == "seat-mcp" })
        let row = MachineMenuCopy.serviceRow(entry, allowlist: Self.allowlist, pm2Answered: true)
        XCTAssertTrue(row.actions.isEmpty)
        XCTAssertEqual(row.title, "Seat MCP:  not in this Mac's pm2, pm2 on the Mac has no record of it, so it is not started from here")
    }

    func testAServiceTheOwnerStoppedSaysSoInTheMenuRow() throws {
        let when = Date(timeIntervalSince1970: 1_800_000_000)
        let services = FleetServices(serverState: .running,
                                     states: ["shellular": .stopped],
                                     stoppedByOwner: ["shellular"],
                                     stoppedAt: ["shellular": when])
        let entry = try XCTUnwrap(services.entries.first { $0.id == "shellular" })
        let row = MachineMenuCopy.serviceRow(entry, allowlist: Self.allowlist, pm2Answered: true)
        XCTAssertEqual(row.title, "Shellular:  stopped (stopped by you at \(FleetTime.clock(when)))")
    }

    func testWhatMusterDidAboutAServiceIsInTheMenuRow() throws {
        let at = Date(timeIntervalSince1970: 1_800_000_000)
        let note = ReconcileNote(.restarted(at: at))
        let services = FleetServices(serverState: .running,
                                     states: ["shellular": .running],
                                     reconcileNotes: ["shellular": note])
        let entry = try XCTUnwrap(services.entries.first { $0.id == "shellular" })
        let row = MachineMenuCopy.serviceRow(entry, allowlist: Self.allowlist, pm2Answered: true)
        XCTAssertEqual(row.title, "Shellular:  running (Restarted by Muster at \(FleetTime.clock(at)))")
    }

    func testAServiceMusterGaveUpOnCountsAsAProblemInTheMenu() throws {
        let note = ReconcileNote(.gaveUp(tries: 3))
        let services = FleetServices(serverState: .running,
                                     states: ["shellular": .stopped],
                                     reconcileNotes: ["shellular": note])
        let entry = try XCTUnwrap(services.entries.first { $0.id == "shellular" })
        XCTAssertTrue(entry.needsAttention)
        XCTAssertEqual(entry.menuNote, "Muster stopped retrying after 3 tries")
    }

    func testAnUnreadableAllowlistOffersNoDirectRouteAndSaysWhy() throws {
        let stopped = Self.macServices(changing: ["app.botfleet.companion": .stopped])
        let entry = try XCTUnwrap(stopped.entries.first { $0.id == "app.botfleet.companion" })
        let row = MachineMenuCopy.serviceRow(entry, allowlist: nil, pm2Answered: true)
        XCTAssertTrue(row.actions.isEmpty)
        XCTAssertTrue(row.title.contains("the allowlist could not be read"), row.title)
        // BotFleet Server's own route does not depend on the allowlist.
        let server = try XCTUnwrap(stopped.entries.first { $0.id == "app.botfleet.server" })
        XCTAssertFalse(MachineMenuCopy.serviceRow(server, allowlist: nil, pm2Answered: true).actions.isEmpty)
    }

    func testThePanelSaysWhyAButtonIsMissingInTheMenusWords() {
        XCTAssertEqual(MachineMenuCopy.noRouteSentence(allowlist: nil, someRouted: false),
                       "The allowlist could not be read.")
        XCTAssertEqual(MachineMenuCopy.noRouteSentence(allowlist: OpsAllowlist(), someRouted: false),
                       "The allowlist does not name it.")
        XCTAssertEqual(MachineMenuCopy.noRouteSentence(allowlist: OpsAllowlist(), someRouted: true),
                       "The allowlist does not name its other actions.")
    }

    func testMoreCellsThanTheMenuNamesPointAtTheGridWithNoEllipsis() throws {
        let cells = (0..<10).map { DriftRedCell(seat: "S\($0)", kind: ConfigKind.hooks.rawValue, ruleID: nil, state: .missing) }
        let two = MachineMenu(inputs(redCells: cells, status: try statusReading(), tools: try toolsReading()))
        assertLines(two, contain: ["    2 More Cells"])
        let one = MachineMenu(inputs(redCells: Array(cells.prefix(9)), status: try statusReading(), tools: try toolsReading()))
        assertLines(one, contain: ["    1 More Cell"])
    }

    func testOnlyAMacRowWhoseNameSaysServerIsQualified() {
        XCTAssertEqual(FleetServices.menuRowTitle(title: "BotFleet Server", host: .mac), "BotFleet Server (On The Mac)")
        XCTAssertEqual(FleetServices.menuRowTitle(title: "Shellular", host: .mac), "Shellular")
        XCTAssertEqual(FleetServices.menuRowTitle(title: "Serverless Thing", host: .mac), "Serverless Thing")
        XCTAssertEqual(FleetServices.menuRowTitle(title: "BotFleet Server", host: .server), "BotFleet Server")
    }

    // MARK: - Action states

    func testActionStatesReadTheDesignWordForWord() {
        let shellular = ServiceActionSubject(title: "Shellular", host: .mac, action: .restart)
        XCTAssertEqual(FleetActionWords.line(for: .waiting(shellular)),
                       "Shellular:  waiting for your answer on the Mac")
        XCTAssertEqual(FleetActionWords.line(for: .running(shellular)),
                       "Restarting Shellular on the Mac\u{2026}")
        XCTAssertEqual(FleetActionWords.line(for: .done(shellular, at: Self.time("2026-11-04T20:47:00Z"))),
                       "Shellular restarted on the Mac at 2:47 PM.")
        XCTAssertEqual(FleetActionWords.line(for: .failed(shellular, reason: "pm2 said no")),
                       "Shellular did not restart on the Mac:  pm2 said no")
    }

    func testAnActionsLineSitsUnderTheMachineItRunsOn() throws {
        let timer = ServiceActionSubject(action: .serverUnit(verb: .restart, unit: OpsNames.reportTimerUnit))
        let menu = MachineMenu(inputs(status: try statusReading(), tools: try toolsReading(),
                                      actionState: .running(timer)))
        let serverText = menu.server.items.map { item -> String in
            if case .line(let text) = item { return text }
            return ""
        }
        XCTAssertTrue(serverText.contains("Restarting Server Report Timer on the server\u{2026}"))
    }

    func testALongReasonIsCutForTheRowAndTheWholeOfItIsKept() {
        let reason = String(repeating: "pm2 said a very long thing ", count: 20)
        let long = ServiceActionSubject(title: "Shellular", host: .mac, action: .stop)
        let whole = FleetActionWords.line(for: .failed(long, reason: reason + "\nsecond line"))
        let row = MachineMenuCopy.oneRow(whole)
        XCTAssertLessThanOrEqual(row.count, MachineMenuCopy.reasonLimit + 1)
        XCTAssertFalse(row.contains("second line"))
        XCTAssertTrue(whole.contains("second line"))
    }

    // MARK: - The clock

    func testTheClockIsTwelveHourOnTheOwnersClock() {
        XCTAssertEqual(FleetTime.clock(Self.time("2026-11-04T20:46:00Z")), "2:46 PM")
        XCTAssertEqual(FleetTime.clock(Self.time("2026-11-04T06:05:00Z")), "12:05 AM")
        XCTAssertEqual(FleetTime.clock(Self.time("2026-11-04T18:00:00Z")), "12:00 PM")
    }

    // MARK: - The monitor

    /// The menu bar item's count does not need a window:  a monitor started before any view
    /// hears the store's first run land.
    @MainActor
    func testAMonitorReportsItsCountsWithNoView() async throws {
        let grid = FakeGrid()
        let monitor = DriftMonitor(grid: grid,
                                   notifier: DriftRecordingNotifier(),
                                   history: DriftHistoryStore(url: directory.appendingPathComponent("history.json")),
                                   watcher: FakeWatcher(),
                                   scheduler: ManualScheduler(),
                                   ticker: ManualTicker(),
                                   home: directory)
        monitor.start()
        grid.finish(cells: [
            GridCell(seat: "MM", kind: .hooks, state: .missing, rules: [], coverReason: nil),
            GridCell(seat: "MA", kind: .hooks, state: .drift, rules: [], coverReason: nil),
            GridCell(seat: "MC", kind: .hooks, state: .unproven, rules: [], coverReason: nil),
        ], trusted: true)
        let deadline = Date().addingTimeInterval(5)
        while monitor.redCount == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(monitor.redCount, 1)
        XCTAssertEqual(monitor.driftCells.map(\.seat), ["MA"])
        XCTAssertEqual(monitor.unprovenCells.map(\.seat), ["MC"])
        grid.finish(cells: [], trusted: false)
        let later = Date().addingTimeInterval(5)
        while monitor.redCount != nil, Date() < later {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertNil(monitor.redCount)
        XCTAssertTrue(monitor.driftCells.isEmpty)
        XCTAssertTrue(monitor.unprovenCells.isEmpty)
    }
}
