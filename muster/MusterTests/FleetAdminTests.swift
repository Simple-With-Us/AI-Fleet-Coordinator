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
                            server: FleetAdminServer) -> FleetAdmin {
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
        FleetAdminServer.current = server

        let startup = StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
        let api = ControlAPI(session: FleetAdminServer.session(),
                             baseURL: URL(string: "http://127.0.0.1:8799")!)
        let control = FleetServiceControl(
            pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2"),
            startup: startup,
            mcpServer: nil,
            stopServer: { try await api.stopServer() })
        return FleetAdmin(control: control,
                          startup: startup,
                          presence: ServerPresenceProbe(port: 8799, connect: { _ in server.presence() }),
                          api: api)
    }
}

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
    private(set) var calls: [FleetAdminCall] = []

    /// The launchctl reads,  which are the ones that block.
    var launchctlReads: [FleetAdminCall] {
        calls.filter { $0.executable == "/bin/launchctl" && $0.arguments == ["list"] }
    }

    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append(FleetAdminCall(executable: executable,
                                     arguments: arguments,
                                     onMainThread: Thread.isMainThread))
        if executable == "/bin/launchctl", arguments == ["list"] {
            // Neither the server's agent nor the companion's has a process
            // behind it,  so launchd reports both as stopped.
            return "PID\tStatus\tLabel\n-\t0\tother.service"
        }
        if executable == "/usr/sbin/sysctl" { return "4F9389E9-AFE4-42C8-AE2C-7FEEDBA31B6C" }
        if arguments.contains("jlist") { return "[]" }
        return ""
    }
}

/// The BotFleet Server the admin talks to,  and the port it listens on.
final class FleetAdminServer {
    /// True once the server has been asked to stop.
    private(set) var stopped = false
    /// How many times the port was asked after the stop was requested.
    private(set) var looksAfterTheStop = 0
    /// The server the requests go to.
    static var current: FleetAdminServer?

    /// The server has been asked to stop.
    func requestStop() { stopped = true }

    /// What the port says.
    ///
    /// The server is up until it is asked to stop,  and it lets go of the port a
    /// moment later:  the first look after the stop still finds it,  and the second
    /// does not.
    func presence() -> ServerPresence {
        guard stopped else { return .present }
        looksAfterTheStop += 1
        return looksAfterTheStop >= 2 ? .absent : .present
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
        let server = FleetAdminServer.current
        let path = request.url?.path ?? ""
        var status = 200
        var body = "{}"
        switch path {
        case "/api/health":
            if server?.stopped == true {
                status = 503
            } else {
                body = #"{"app":"botfleet","pid":4242,"ready":true}"#
            }
        case "/api/system/control":
            body = #"{"activeBotCount":0,"localVms":[],"persistOnExit":true}"#
        case "/api/system/control/stop":
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
