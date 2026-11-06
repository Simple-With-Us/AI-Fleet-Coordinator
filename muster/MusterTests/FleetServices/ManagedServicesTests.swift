import XCTest
import Darwin

final class ManagedServicesTests: XCTestCase {
    // MARK: - Wanted state derivation

    func testMCPServerIsWantedExactlyWhileTheServerRuns() {
        let running = ManagedServices(serverState: .running, mcpServerState: .stopped)
        XCTAssertEqual(running.mcpServerWanted, true)
        let stopped = ManagedServices(serverState: .stopped, mcpServerState: .running)
        XCTAssertEqual(stopped.mcpServerWanted, false)
        let unknown = ManagedServices(serverState: .unknown, mcpServerState: .running)
        XCTAssertNil(unknown.mcpServerWanted)
    }

    func testWantedStateIsNeverStoredOnTheEntry() {
        let entry = ManagedServiceEntry(id: ManagedServices.mcpServerID, title: ManagedServices.mcpServerTitle,
                                    wanted: nil, state: .running)
        XCTAssertNil(entry.wanted)
        XCTAssertEqual(entry.state, .running)
    }

    func testAServerThatAnswersOrHasALiveProcessIsRunning() {
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: true, installedServerState: .stopped,
                                               serverPresence: .absent), .running)
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: false, installedServerState: .running,
                                               serverPresence: .absent), .running)
    }

    func testAnUnreachableServerThatStillHasAListenerIsUnknown() {
        // The API did not answer, but something is still listening on the
        // server's port: it is busy or booting, and it is not stopped.
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: false, installedServerState: .stopped,
                                               serverPresence: .present), .unknown)
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: false, installedServerState: .unknown,
                                               serverPresence: .present), .unknown)
    }

    func testTheServerIsStoppedOnlyWhenNothingIsListening() {
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: false, installedServerState: .stopped,
                                               serverPresence: .absent), .stopped)
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: false, installedServerState: .notInstalled,
                                               serverPresence: .absent), .stopped)
    }

    func testAnUnreachableServerIsNeverKnownToBeStopped() {
        XCTAssertEqual(ManagedServices.serverState(statusAvailable: false, installedServerState: .stopped,
                                               serverPresence: .unknown), .unknown)
    }

    func testAnUnreachableServerNeverAsksForTheMCPServerToBeStopped() {
        // The regression this guards: a transport error used to read as "the
        // server is stopped", which stopped the MCP server under a live one.
        let managedServices = ManagedServices(serverState: .unknown, mcpServerState: .running)
        XCTAssertNil(managedServices.mcpServerWanted)
        // Muster only ever starts a service,  so a running one is never touched whatever
        // the server is doing.
        XCTAssertEqual(managedServices.entries[0].summary, "running, not sure if it should be")
        XCTAssertEqual(ServiceReconcileEngine.decide(state: .running, policy: .keepRunning, stoppedByOwner: false,
                                                     enabled: true, record: ReconcileRecord(), now: Date(),
                                                     firstLook: true),
                       .skip(.nothingToDo))
    }

    func testManagedServicesListsTheBotFleetMCPServer() {
        let managedServices = ManagedServices(serverState: .running, mcpServerState: .running)
        XCTAssertEqual(managedServices.entries.count, 1)
        XCTAssertEqual(managedServices.entries.first?.id, "botfleet-mcp")
        XCTAssertEqual(managedServices.entries.first?.title, "BotFleet MCP Server")
        XCTAssertEqual(managedServices.entries.first?.wanted, true)
    }

    // MARK: - Proving whether the server is really there

    func testThePresenceProbeSeesSomethingListening() throws {
        let (port, handle) = try openLoopbackPort()
        defer { close(handle) }
        XCTAssertEqual(ServerPresenceProbe(port: port).presence(), .present)
    }

    func testThePresenceProbeSeesAClosedPort() throws {
        let (port, handle) = try openLoopbackPort()
        close(handle)
        XCTAssertEqual(ServerPresenceProbe(port: port).presence(), .absent)
    }

    func testThePresenceProbeCanBeGivenItsOwnProbe() {
        let probe = ServerPresenceProbe(port: 1234, connect: { _ in .absent })
        XCTAssertEqual(probe.presence(), .absent)
    }

    // MARK: - Summaries

    func testSummaryStatesFactsInPlainWords() {
        XCTAssertEqual(entry(wanted: true, state: .running).summary, "running")
        XCTAssertEqual(entry(wanted: true, state: .stopped).summary, "stopped, should be running")
        XCTAssertEqual(entry(wanted: true, state: .failed).summary, "failed, should be running")
        XCTAssertEqual(entry(wanted: true, state: .unknown).summary, "not known, should be running")
        XCTAssertEqual(entry(wanted: false, state: .running).summary, "running, should be stopped")
        XCTAssertEqual(entry(wanted: false, state: .stopped).summary, "stopped")
        XCTAssertEqual(entry(wanted: nil, state: .running).summary, "running, not sure if it should be")
        XCTAssertEqual(entry(wanted: nil, state: .stopped).summary, "stopped, not sure if it should be")
    }

    func testNoSummaryUsesTheWordWanted() {
        let states: [ManagedServiceState] = [.running, .stopped, .failed, .notInstalled, .notInPM2, .unknown]
        for wanted in [true, false, nil] as [Bool?] {
            for state in states {
                let words = ManagedServices.summary(wanted: wanted, state: state)
                XCTAssertFalse(words.lowercased().contains("wanted"), words)
                XCTAssertFalse(words.contains("\u{2014}"), words)
            }
        }
    }

    func testAnApplicationPM2NeverHeardOfIsNotStopped() {
        XCTAssertEqual(entry(wanted: false, state: .notInPM2).summary, "not in this Mac's pm2")
        XCTAssertNotEqual(ManagedServiceState.notInPM2.label, ManagedServiceState.stopped.label)
    }

    // MARK: - The pm2 client the Control app has to use

    func testPM2EnvironmentCarriesHomeAndPM2Home() {
        let home = URL(fileURLWithPath: "/Users/tester", isDirectory: true)
        let environment = PM2Client.pm2Environment([:], home: home)
        XCTAssertEqual(environment["HOME"], "/Users/tester")
        XCTAssertEqual(environment["PM2_HOME"], "/Users/tester/.pm2")
        XCTAssertFalse(environment["PATH"]?.isEmpty ?? true)
    }

    func testPM2EnvironmentKeepsAnExplicitPM2HomeAndPath() {
        let home = URL(fileURLWithPath: "/Users/tester", isDirectory: true)
        let environment = PM2Client.pm2Environment(["PM2_HOME": "/tmp/pm2", "PATH": "/bin"], home: home)
        XCTAssertEqual(environment["PM2_HOME"], "/tmp/pm2")
        XCTAssertEqual(environment["PATH"], "/bin")
        XCTAssertEqual(environment["HOME"], "/Users/tester")
    }

    func testPM2ClientIsMissingWithoutNodeAndPM2() {
        XCTAssertFalse(PM2Client(commands: PM2Commands(), nodePath: "", pm2Path: "").isAvailable)
        XCTAssertFalse(PM2Client(commands: PM2Commands(), nodePath: "/usr/bin/node", pm2Path: "").isAvailable)
        XCTAssertTrue(PM2Client(commands: PM2Commands(), nodePath: "/usr/bin/node",
                                pm2Path: "/usr/bin/pm2").isAvailable)
    }

    // MARK: - Observing the MCP server

    func testMCPServerIsRunningOnlyWhenItAnswersAsTheBotFleetMCPServer() async throws {
        let control = try makeControl()
        ManagedServicesURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp","tools":26}"#.utf8))
        }
        let state = await control.state()
        XCTAssertEqual(state, .running)
    }

    func testMCPServerIsStoppedWhenNothingAnswersIt() async throws {
        let control = try makeControl()
        ManagedServicesURLProtocol.handler = { _ in .failure(URLError(.cannotConnectToHost)) }
        let state = await control.state()
        XCTAssertEqual(state, .stopped)
    }

    func testMCPServerIsUnknownWhenSomethingElseAnswersIt() async throws {
        let control = try makeControl()
        ManagedServicesURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"some-other-service"}"#.utf8))
        }
        let state = await control.state()
        XCTAssertEqual(state, .unknown)
    }

    func testMCPServerIsUnknownWhenItAnswersWithSomethingBroken() async throws {
        let control = try makeControl()
        ManagedServicesURLProtocol.handler = { _ in .response(200, Data(#"{"status":"ok"}"#.utf8)) }
        let state = await control.state()
        XCTAssertEqual(state, .unknown)
    }

    func testTheMCPControlRefusesToStartWithoutAnEndpoint() {
        XCTAssertThrowsError(try McpServerControl(session: mockSession(), baseURL: nil,
                                                 pm2: PM2Client(commands: PM2Commands(),
                                                               nodePath: "/usr/bin/node",
                                                               pm2Path: "/usr/bin/pm2"))) { error in
            XCTAssertEqual(error as? McpServerError,
                           .refused("Could not determine the BotFleet MCP Server endpoint."))
        }
    }

    // MARK: - Changing the MCP server

    func testUnknownStateIsNeverChanged() async throws {
        ManagedServicesURLProtocol.handler = { _ in .failure(URLError(.timedOut)) }
        let commands = PM2Commands()
        let control = try makeControl(commands: commands)
        let state = try await control.setRunning(true)
        XCTAssertEqual(state, .unknown)
        XCTAssertTrue(commands.calls.isEmpty)
    }

    func testStoppingTheMCPServerRunsPM2Stop() async throws {
        var probes = 0
        ManagedServicesURLProtocol.handler = { _ in
            probes += 1
            return probes <= 1
                ? .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
                : .failure(URLError(.cannotConnectToHost))
        }
        let commands = PM2Commands()
        let control = try makeControl(commands: commands)
        let state = try await control.setRunning(false, within: 2)
        XCTAssertEqual(state, .stopped)
        XCTAssertEqual(commands.calls.count, 1)
        XCTAssertTrue(commands.calls[0].contains("stop"))
        XCTAssertTrue(commands.calls[0].contains("botfleet-mcp"))
    }

    func testStartingTheMCPServerRunsPM2Start() async throws {
        var probes = 0
        ManagedServicesURLProtocol.handler = { _ in
            probes += 1
            return probes <= 1
                ? .failure(URLError(.cannotConnectToHost))
                : .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let commands = PM2Commands()
        let control = try makeControl(commands: commands)
        let state = try await control.setRunning(true, within: 2)
        XCTAssertEqual(state, .running)
        XCTAssertEqual(commands.calls.count, 1)
        XCTAssertTrue(commands.calls[0].contains("start"))
        XCTAssertTrue(commands.calls[0].contains("botfleet-mcp"))
    }

    func testAStartThatDoesNotBringTheMCPServerUpIsRestartedWhenPM2ShowsAStaleRecord() async throws {
        // pm2 answers a start on a record it believes is online with "already
        // started" and starts nothing.  A start that leaves the server down
        // is looked at once, and only a record pm2 shows as online with no
        // process behind it is restarted.
        ManagedServicesURLProtocol.handler = { _ in
            .failure(URLError(.cannotConnectToHost))
        }
        let commands = PM2Commands()
        commands.listing = #"[{"name": "botfleet-mcp", "pid": 0, "pm2_env": {"status": "online"}}]"#
        let control = try makeControl(commands: commands)
        let state = try await control.setRunning(true, within: 0.1)
        XCTAssertNotEqual(state, .running)
        XCTAssertTrue(commands.calls.contains { $0.contains("restart") })
        XCTAssertTrue(commands.calls.contains { $0.contains("botfleet-mcp") })
    }

    func testASlowStartIsNotRestarted() async throws {
        // The record is not stale:  pm2 shows a process behind it, so the server is only slow
        // to answer, and a restart would kill a start that is still coming up.
        ManagedServicesURLProtocol.handler = { _ in
            .failure(URLError(.cannotConnectToHost))
        }
        let commands = PM2Commands()
        commands.listing = #"[{"name": "botfleet-mcp", "pid": 4242, "pm2_env": {"status": "online"}}]"#
        let control = try makeControl(commands: commands)
        _ = try await control.setRunning(true, within: 0.1)
        XCTAssertFalse(commands.calls.contains { $0.contains("restart") })
    }

    func testAStartIsNotRestartedWhenPM2CannotBeAskedWhichRecordIsStale() async throws {
        ManagedServicesURLProtocol.handler = { _ in
            .failure(URLError(.cannotConnectToHost))
        }
        let commands = PM2Commands()
        commands.listing = "garbled"
        let control = try makeControl(commands: commands)
        _ = try await control.setRunning(true, within: 0.1)
        XCTAssertFalse(commands.calls.contains { $0.contains("restart") })
    }

    func testARefusedPM2IsReportedRatherThanClaimedAsSuccess() async throws {
        ManagedServicesURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let commands = PM2Commands()
        commands.failure = CommandExitError(output: "pm2 said no")
        let control = try makeControl(commands: commands)
        do {
            _ = try await control.setRunning(false, within: 1)
            XCTFail("Expected the refusal to be reported")
        } catch {
            XCTAssertEqual(error as? McpServerError, .refused("pm2 said no"))
        }
    }

    /// A command that ended but whose output could not be read did not time out,  so it is not
    /// called one.
    func testAPM2CommandWhoseOutputCouldNotBeReadIsNotCalledATimeout() async throws {
        ManagedServicesURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let commands = PM2Commands()
        commands.failure = StartupPolicyError.commandFailed
        let control = try makeControl(commands: commands)
        do {
            _ = try await control.setRunning(false, within: 1)
            XCTFail("Expected the failure to be reported")
        } catch {
            XCTAssertEqual(error as? McpServerError,
                           .refused("pm2 could not change the BotFleet MCP Server."))
        }
    }

    func testARunnerTimeoutNamesPM2AndNotSomeOtherService() async throws {
        ManagedServicesURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let commands = PM2Commands()
        commands.failure = CommandTimedOutError(executable: "/usr/bin/node",
                                                arguments: ["/usr/bin/pm2", "stop", "botfleet-mcp"])
        let control = try makeControl(commands: commands)
        do {
            _ = try await control.setRunning(false, within: 1)
            XCTFail("Expected the timeout to be reported")
        } catch {
            // pm2 can take a change and then be slow to answer,  so a timeout is not a refusal.
            XCTAssertEqual(error as? McpServerError,
                           .notKnown("pm2 did not finish in time to change the BotFleet MCP Server, so it is not known whether it did."))
        }
    }

    func testAPM2ErrorReadsFromStandardErrorAndIsBounded() async throws {
        ManagedServicesURLProtocol.handler = { _ in
            .response(200, Data(#"{"status":"ok","app":"botfleet-admin-mcp"}"#.utf8))
        }
        let commands = PM2Commands()
        let table = "\u{250C}\u{2500}\u{2500}\u{2510}\n\u{2502} id \u{2502}\n\u{2514}\u{2500}\u{2500}\u{2518}"
        commands.failure = CommandExitError(output: table, status: 1,
                                            errorOutput: "\u{1B}[31m[PM2][ERROR]\u{1B}[39m spawn failed")
        let control = try makeControl(commands: commands)
        do {
            _ = try await control.setRunning(false, within: 1)
            XCTFail("Expected the refusal to be reported")
        } catch {
            XCTAssertEqual(error as? McpServerError, .refused("[PM2][ERROR] spawn failed"))
        }
    }

    // MARK: - Helpers

    private func entry(wanted: Bool?, state: ManagedServiceState) -> ManagedServiceEntry {
        ManagedServiceEntry(id: ManagedServices.mcpServerID, title: ManagedServices.mcpServerTitle,
                        wanted: wanted, state: state)
    }

    private func makeControl(commands: PM2Commands = PM2Commands()) throws -> McpServerControl {
        let url = try XCTUnwrap(URL(string: "http://127.0.0.1:8794"))
        return try McpServerControl(session: mockSession(),
                                    baseURL: url,
                                    pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node",
                                                   pm2Path: "/usr/bin/pm2"))
    }

    private func mockSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ManagedServicesURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    /// Opens a loopback listener on an ephemeral port, so the presence probe
    /// can be tested against a real socket.
    private func openLoopbackPort() throws -> (port: Int, handle: Int32) {
        let handle = socket(AF_INET, SOCK_STREAM, 0)
        guard handle >= 0 else { throw SocketTestError.cannotCreate }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let bound = withUnsafePointer(to: &address) { pointer -> Int32 in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
                Darwin.bind(handle, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { close(handle); throw SocketTestError.cannotBind }
        guard Darwin.listen(handle, 1) == 0 else { close(handle); throw SocketTestError.cannotListen }
        var assigned = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &assigned) { pointer -> Int32 in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
                getsockname(handle, sockaddrPointer, &length)
            }
        }
        guard named == 0 else { close(handle); throw SocketTestError.cannotName }
        return (Int(UInt16(bigEndian: assigned.sin_port)), handle)
    }
}

private enum SocketTestError: Error {
    case cannotCreate
    case cannotBind
    case cannotListen
    case cannotName
}

private final class PM2Commands: CommandRunning {
    var calls: [[String]] = []
    var failure: Error?
    /// What `pm2 jlist` answers with.
    var listing = "[]"
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append(arguments)
        if let failure { throw failure }
        if arguments.contains("jlist") { return listing }
        return ""
    }
}

private final class ManagedServicesURLProtocol: URLProtocol {
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
