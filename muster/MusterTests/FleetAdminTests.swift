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

        let startup = StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
        let api = ControlAPI(session: FleetAdminServer.session(),
                             baseURL: URL(string: "http://127.0.0.1:\(server.port)")!)
        let control = FleetServiceControl(
            pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2"),
            startup: startup,
            mcpServer: nil,
            stopServer: { try await api.stopServer() })
        return FleetAdmin(control: control,
                          startup: startup,
                          presence: ServerPresenceProbe(port: server.port, connect: { _ in server.presence() }),
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
    ///
    /// The admin reads off the main thread,  so the recording is guarded:  an
    /// array that two threads write at once is not an array any more.
    private let lock = NSLock()
    private var recorded: [FleetAdminCall] = []
    /// A boot-session identifier made up for this run.
    private let boot = UUID().uuidString

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
        lock.lock()
        recorded.append(FleetAdminCall(executable: executable,
                                       arguments: arguments,
                                       onMainThread: Thread.isMainThread))
        lock.unlock()
        if executable == "/bin/launchctl", arguments == ["list"] {
            // Neither the server's agent nor the companion's has a process
            // behind it,  so launchd reports both as stopped.
            return "PID\tStatus\tLabel\n-\t0\tother.service"
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

    /// What the port says.
    ///
    /// The server is up until it is asked to stop,  and it lets go of the port a
    /// moment later:  the first look after the stop still finds it,  and the second
    /// does not.
    func presence() -> ServerPresence {
        state.lock()
        defer { state.unlock() }
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
