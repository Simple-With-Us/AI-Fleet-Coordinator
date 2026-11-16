import XCTest

final class ControlTests: XCTestCase {
    func testNoImplicitNetworkCalls() {
        MockURLProtocol.requests = []
        _ = ControlAPI(session: mockSession())
        XCTAssertTrue(MockURLProtocol.requests.isEmpty)
    }

    func testRejectsForeignHealthBeforeMutation() async {
        MockURLProtocol.handler = { request in
            (200, Data(#"{"app":"other","pid":1}"#.utf8))
        }
        let api = ControlAPI(session: mockSession())
        do { try await api.stopServer(); XCTFail("Expected identity refusal") }
        catch { XCTAssertEqual(error as? ControlAPIError, .foreignService) }
        XCTAssertEqual(MockURLProtocol.requests.map(\.url!.path), ["/api/health"])
    }

    func testMalformedStatusIsNotReportedAsRunning() async {
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" { return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
            return (200, Data(#"{"activeBotCount":"bad"}"#.utf8))
        }
        do { _ = try await ControlAPI(session: mockSession()).status(); XCTFail("Expected invalid response") }
        catch { XCTAssertEqual(error as? ControlAPIError, .invalidResponse) }
    }

    func testAStatusWithoutTheServersFullShapeIsRefused() async {
        // The Local VM list is not read by Muster, but it is part of the
        // server's answer, and an answer without it is not the server's.
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" { return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
            return (200, Data(#"{"activeBotCount":0,"persistOnExit":true}"#.utf8))
        }
        do { _ = try await ControlAPI(session: mockSession()).status(); XCTFail("Expected invalid response") }
        catch { XCTAssertEqual(error as? ControlAPIError, .invalidResponse) }
    }

    func testTheBlockingWorkIsReadFromTheStatus() async throws {
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" { return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
            return (200, Data(#"{"activeBotCount":1,"persistOnExit":true,"localVms":[],"blockingWork":[{"count":2,"label":"bot turn"},{"count":0,"label":"lease"}]}"#.utf8))
        }
        let status = try await ControlAPI(session: mockSession()).status()
        XCTAssertEqual(status.activeBotCount, 1)
        XCTAssertEqual(status.blockingWorkSummary, "2 bot turns")
    }

    /// Muster asks only the BotFleet Server on this Mac.  A server anywhere
    /// else is refused before a request is made, so nothing leaves the Mac.
    func testAServerThatIsNotOnThisMacIsNeverAsked() async {
        MockURLProtocol.handler = { _ in (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
        for base in ["https://server.example.test:8799", "http://server.example.test:8799", "https://127.0.0.1:8799"] {
            let session = mockSession()
            let api = ControlAPI(session: session, baseURL: URL(string: base)!)
            do { _ = try await api.status(); XCTFail("Expected a refusal for \(base)") }
            catch { XCTAssertEqual(error as? ControlAPIError, .invalidResponse, base) }
            do { try await api.stopServer(); XCTFail("Expected a refusal for \(base)") }
            catch { XCTAssertEqual(error as? ControlAPIError, .invalidResponse, base) }
            let pid = await api.serverPID()
            XCTAssertNil(pid, base)
            XCTAssertTrue(MockURLProtocol.requests.isEmpty, base)
        }
    }

    /// Muster holds no credential, so no request it sends carries one.
    func testNoRequestCarriesACredential() async throws {
        MockURLProtocol.failure = nil
        MockURLProtocol.handler = { request in
            switch request.url!.path {
            case "/api/health": return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8))
            case "/api/system/control/stop": return (200, Data(#"{"ok":true}"#.utf8))
            default: return (200, Data(#"{"activeBotCount":0,"persistOnExit":true,"localVms":[]}"#.utf8))
            }
        }
        let api = ControlAPI(session: mockSession())
        _ = try await api.status()
        try await api.stopServer()
        XCTAssertEqual(MockURLProtocol.requests.map(\.url!.path),
                       ["/api/health", "/api/system/control", "/api/health", "/api/system/control/stop"])
        for request in MockURLProtocol.requests {
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            XCTAssertNil(request.value(forHTTPHeaderField: "x-botfleet-gate-token"))
            XCTAssertEqual(request.url?.host, "127.0.0.1")
            XCTAssertEqual(request.url?.scheme, "http")
        }
    }

    func testAChangeThatGetsNoAnswerHasAnUnknownOutcome() async {
        // The health check answers, and the stop is sent and then dropped:
        // the server may have applied it, so it is not reported as unavailable.
        MockURLProtocol.handler = { _ in (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
        MockURLProtocol.failure = { request in
            request.url!.path == "/api/system/control/stop" ? URLError(.timedOut) : nil
        }
        defer { MockURLProtocol.failure = nil }
        do {
            try await ControlAPI(session: mockSession()).stopServer()
            XCTFail("Expected an unknown outcome")
        } catch {
            XCTAssertEqual(error as? ControlAPIError, .outcomeUnknown)
        }
    }

    func testAChangeThatIsRefusedBeforeSendingIsUnavailableNotUnknown() async {
        // The health check answers, and the connection for the stop is refused:
        // nothing was sent, so a retry is safe and the server is unavailable.
        MockURLProtocol.handler = { _ in (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
        MockURLProtocol.failure = { request in
            request.url!.path == "/api/system/control/stop" ? URLError(.cannotConnectToHost) : nil
        }
        defer { MockURLProtocol.failure = nil }
        do {
            try await ControlAPI(session: mockSession()).stopServer()
            XCTFail("Expected unavailable")
        } catch {
            XCTAssertEqual(error as? ControlAPIError, .unavailable)
        }
    }

    func testAChangeWhoseConnectionDropsMidFlightHasAnUnknownOutcome() async {
        // The health check answers, and the stop's connection is lost while the
        // request is in flight:  the server may have applied it, and this code
        // must never join the safe-to-retry set.
        MockURLProtocol.handler = { _ in (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
        MockURLProtocol.failure = { request in
            request.url!.path == "/api/system/control/stop" ? URLError(.networkConnectionLost) : nil
        }
        defer { MockURLProtocol.failure = nil }
        do {
            try await ControlAPI(session: mockSession()).stopServer()
            XCTFail("Expected an unknown outcome")
        } catch {
            XCTAssertEqual(error as? ControlAPIError, .outcomeUnknown)
        }
    }

    func testACancelledReadIsPassedOnAsACancellation() async {
        MockURLProtocol.handler = { _ in (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
        MockURLProtocol.failure = { _ in URLError(.cancelled) }
        defer { MockURLProtocol.failure = nil }
        do {
            _ = try await ControlAPI(session: mockSession()).status()
            XCTFail("Expected a cancellation")
        } catch {
            XCTAssertTrue(error is CancellationError, "got \(error)")
        }
    }

    func testBusyRefusalIsSurfaced() async {
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" { return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
            return (409, Data(#"{"error":"Active bot turn"}"#.utf8))
        }
        do { try await ControlAPI(session: mockSession()).stopServer(); XCTFail("Expected refusal") }
        catch { XCTAssertEqual(error as? ControlAPIError, .refused("Active bot turn")) }
    }

    /// A server that answers the health check but is too slow for the status
    /// route is running, not stopped.  Reporting it as unavailable would offer
    /// to start a server that is already up.
    func testSlowServerIsUnresponsiveRatherThanUnavailable() async {
        MockURLProtocol.failure = { request in
            request.url!.path == "/api/system/control" ? URLError(.timedOut) : nil
        }
        defer { MockURLProtocol.failure = nil }
        MockURLProtocol.handler = { request in
            (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8))
        }
        do { _ = try await ControlAPI(session: mockSession()).status(); XCTFail("Expected unresponsive") }
        catch { XCTAssertEqual(error as? ControlAPIError, .unresponsive) }
    }

    /// The identity check answers, then the status read's connection is
    /// refused:  the server proved it was alive a moment ago, so the refusal
    /// is surfaced as itself instead of reading as a slow answer.
    func testAStatusReadRefusedBeforeSendingSurfacesTheRefusal() async {
        MockURLProtocol.failure = { request in
            request.url!.path == "/api/system/control" ? URLError(.cannotConnectToHost) : nil
        }
        defer { MockURLProtocol.failure = nil }
        MockURLProtocol.handler = { request in
            (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8))
        }
        do { _ = try await ControlAPI(session: mockSession()).status(); XCTFail("Expected a refusal") }
        catch {
            guard case .refused = (error as? ControlAPIError) else {
                return XCTFail("Expected a refusal, got \(error)")
            }
        }
    }

    /// A server that does not answer the health check at all really is gone.
    func testUnreachableServerIsStillUnavailable() async {
        MockURLProtocol.failure = { _ in URLError(.cannotConnectToHost) }
        defer { MockURLProtocol.failure = nil }
        do { _ = try await ControlAPI(session: mockSession()).status(); XCTFail("Expected unavailable") }
        catch { XCTAssertEqual(error as? ControlAPIError, .unavailable) }
    }

    /// The status route waits longer than the identity check, because the
    /// identity check is cheap and the status route is not.
    func testStatusProbeWaitsLongerThanTheIdentityCheck() async {
        MockURLProtocol.failure = nil
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" { return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
            return (200, Data(#"{"activeBotCount":0,"persistOnExit":true,"localVms":[]}"#.utf8))
        }
        _ = try? await ControlAPI(session: mockSession()).status()
        let health = MockURLProtocol.requests.first { $0.url!.path == "/api/health" }
        let control = MockURLProtocol.requests.first { $0.url!.path == "/api/system/control" }
        XCTAssertEqual(health?.timeoutInterval, 3)
        XCTAssertEqual(control?.timeoutInterval, ControlAPI.statusTimeout)
        XCTAssertEqual(ControlAPI.statusTimeout, 30)
    }

    func testSuccessfulHTTPWithFailedActionIsRejected() async {
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" { return (200, Data(#"{"app":"botfleet","pid":42,"ready":true}"#.utf8)) }
            return (200, Data(#"{"ok":false}"#.utf8))
        }
        do { try await ControlAPI(session: mockSession()).stopServer(); XCTFail("Expected failure") }
        catch { XCTAssertEqual(error as? ControlAPIError, .invalidResponse) }
    }

    func testStartupPolicyRequiresSupportedWrapperAndDoesNotStartWhenToggling() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        let runner = MockCommands()
        let policy = StartupPolicy(home: home, commands: runner, uid: 501, environment: [:])
        XCTAssertEqual(policy.loginPolicy, .legacy)
        XCTAssertFalse(policy.startsAtLogin)
        XCTAssertFalse(policy.supportsPolicy)
        XCTAssertThrowsError(try policy.setStartsAtLogin(false))
        try Data("# BF_CONTROL_STARTUP_POLICY_V1\n".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try policy.setStartsAtLogin(false)
        XCTAssertEqual(policy.loginPolicy, .disabled)
        XCTAssertFalse(policy.startsAtLogin)
        XCTAssertTrue(FileManager.default.fileExists(atPath: home.appendingPathComponent(".botfleet/startup-disabled.marker").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.appendingPathComponent(".botfleet/startup-enabled.marker").path))
        XCTAssertEqual(try String(contentsOf: home.appendingPathComponent(".botfleet/startup-session")).trimmingCharacters(in: .whitespacesAndNewlines), runner.boot)
        try policy.setStartsAtLogin(true)
        XCTAssertEqual(policy.loginPolicy, .enabled)
        XCTAssertTrue(policy.startsAtLogin)
        XCTAssertTrue(FileManager.default.fileExists(atPath: home.appendingPathComponent(".botfleet/startup-enabled.marker").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.appendingPathComponent(".botfleet/startup-disabled.marker").path))
        XCTAssertEqual(runner.calls, ["/usr/sbin/sysctl -n kern.bootsessionuuid", "/usr/sbin/sysctl -n kern.bootsessionuuid"])
    }

    func testStartupPolicyHonorsConfiguredDataDirectory() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let dataDirectory = home.appendingPathComponent("separate-data")
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        let policy = StartupPolicy(home: home, commands: MockCommands(), uid: 501,
                                   environment: ["OMB_DATA_DIR": dataDirectory.path])
        try policy.setStartsAtLogin(false)
        XCTAssertEqual(policy.loginPolicy, .disabled)
        XCTAssertTrue(FileManager.default.fileExists(atPath: dataDirectory.appendingPathComponent("startup-disabled.marker").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.appendingPathComponent(".botfleet/startup-disabled.marker").path))
    }

    func testInstalledServerDistinguishesStoppedAndRunningWithoutStarting() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        let runner = MockCommands()
        let policy = StartupPolicy(home: home, commands: runner, uid: 501, environment: [:])
        XCTAssertEqual(policy.installedServerState, .notInstalled)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        XCTAssertEqual(policy.installedServerState, .stopped)
        runner.launchctlListing = "PID\tStatus\tLabel\n742\t0\tapp.botfleet.server"
        XCTAssertEqual(policy.installedServerState, .running)
        XCTAssertEqual(runner.calls, ["/bin/launchctl list", "/bin/launchctl list"])
    }

    func testStartServerUsesNonKillingKickstartOnlyAfterMarkerAndAgentCheck() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        try Data().write(to: home.appendingPathComponent(".botfleet/stopped.marker"))
        let runner = MockCommands()
        try StartupPolicy(home: home, commands: runner, uid: 501, environment: [:]).startServer()
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.appendingPathComponent(".botfleet/stopped.marker").path))
        XCTAssertEqual(runner.calls, [
            "/usr/sbin/sysctl -n kern.bootsessionuuid",
            "/bin/launchctl enable gui/501/app.botfleet.server",
            "/bin/launchctl print gui/501/app.botfleet.server",
            "/bin/launchctl bootstrap gui/501 \(home.path)/Library/LaunchAgents/app.botfleet.server.plist",
            "/bin/launchctl kickstart gui/501/app.botfleet.server"
        ])
    }

    func testFailedKickstartRestoresDeliberateStop() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let marker = home.appendingPathComponent(".botfleet/stopped.marker")
        try Data().write(to: marker)
        let runner = MockCommands()
        runner.failKickstart = true
        XCTAssertThrowsError(try StartupPolicy(home: home, commands: runner, uid: 501, environment: [:]).startServer())
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path))
    }

    func testRestartServerClearsTheDeliberateStopAndReplacesTheRunningInstance() throws {
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
        let runner = MockCommands()
        runner.agentIsLoaded = true
        try StartupPolicy(home: home, commands: runner, uid: 501, environment: [:]).restartServer()
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker.path))
        XCTAssertTrue(runner.calls.contains("/bin/launchctl kickstart -k gui/501/app.botfleet.server"))
    }

    func testAFailedRestartRestoresTheDeliberateStop() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(at: home.appendingPathComponent("apps"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: home.appendingPathComponent(".botfleet"), withIntermediateDirectories: true)
        try Data("BF_CONTROL_STARTUP_POLICY_V1".utf8).write(to: home.appendingPathComponent("apps/botfleet-server-start.sh"))
        try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/app.botfleet.server.plist"))
        let marker = home.appendingPathComponent(".botfleet/stopped.marker")
        try Data().write(to: marker)
        let runner = MockCommands()
        runner.failKickstart = true
        XCTAssertThrowsError(try StartupPolicy(home: home, commands: runner, uid: 501, environment: [:]).restartServer())
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker.path))
    }

    private func mockSession() -> URLSession {
        MockURLProtocol.requests = []
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: configuration)
    }
}

private final class MockCommands: CommandRunning {
    /// A boot-session identifier made up for this run, so no real machine's is
    /// committed.  It is fixed for the life of the fake.
    let boot = UUID().uuidString
    var calls: [String] = []
    var failKickstart = false
    var launchctlListing = "PID\tStatus\tLabel\n-\t0\tother.service"
    /// True when launchctl already knows the agent.
    var agentIsLoaded = false
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append(([executable] + arguments).joined(separator: " "))
        if executable == "/usr/sbin/sysctl" { return boot }
        if arguments == ["list"] { return launchctlListing }
        if arguments.first == "print" {
            if agentIsLoaded { return "state = running" }
            throw StartupPolicyError.commandFailed
        }
        if failKickstart && arguments.first == "kickstart" { throw StartupPolicyError.commandFailed }
        return ""
    }
}

final class MockURLProtocol: URLProtocol {
    static var requests: [URLRequest] = []
    static var handler: ((URLRequest) -> (Int, Data)) = { _ in (500, Data()) }
    /// When this returns an error for a request, that request fails instead
    /// of answering.  A server that is alive but too slow to answer looks
    /// exactly like this to the client.
    static var failure: ((URLRequest) -> Error?)? = nil
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append(request)
        if let error = Self.failure?(request) {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        let (status, body) = Self.handler(request)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
