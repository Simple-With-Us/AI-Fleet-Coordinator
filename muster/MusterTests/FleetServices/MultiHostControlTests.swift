import XCTest

final class MultiHostControlTests: XCTestCase {
    private var tempDir: URL!

    override func setUpWithError() throws {
        try super.setUpWithError()
        tempDir = FileManager.default.temporaryDirectory.appendingPathComponent("ControlMultiHostTests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        MockURLProtocol.requests = []
        MockURLProtocol.handler = { _ in (500, Data()) }
        MockURLProtocol.failure = nil
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tempDir)
        try super.tearDownWithError()
    }

    func testServerHostStoreSeedsDefaultsAndEnforcesPermissions() throws {
        let storeFile = tempDir.appendingPathComponent("hosts.json")
        let store = ServerHostStore(fileURL: storeFile)

        // Initial load seeds Local Mac and Hetzner
        let hosts = store.loadHosts()
        XCTAssertEqual(hosts.count, 2)
        XCTAssertEqual(hosts[0].id, "local")
        XCTAssertTrue(hosts[0].isLocal)
        XCTAssertEqual(hosts[0].baseURL.absoluteString, "http://127.0.0.1:8799")

        XCTAssertEqual(hosts[1].id, "hetzner")
        XCTAssertFalse(hosts[1].isLocal)
        XCTAssertEqual(hosts[1].baseURL.absoluteString, "https://hetzner.botfleet.app")

        // File permissions are 0600
        let attrs = try FileManager.default.attributesOfItem(atPath: storeFile.path)
        let perms = attrs[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.intValue, 0o600)
    }

    func testServerHostStoreUpsertAndRemove() throws {
        let storeFile = tempDir.appendingPathComponent("hosts.json")
        let store = ServerHostStore(fileURL: storeFile)

        let customHost = ServerHost(
            id: "work-vps",
            name: "Work VPS",
            baseURL: URL(string: "https://vps.example.test:8799")!,
            authTokenSecret: "WORK_VPS_TOKEN",
            isLocal: false
        )

        try store.upsertHost(customHost)
        var hosts = store.loadHosts()
        XCTAssertEqual(hosts.count, 3)
        XCTAssertTrue(hosts.contains(where: { $0.id == "work-vps" && $0.authTokenSecret == "WORK_VPS_TOKEN" }))

        // Refuses to remove the local Mac anchor host
        XCTAssertFalse(try store.removeHost(id: "local"))
        XCTAssertEqual(store.loadHosts().count, 3)

        // Removes remote host
        XCTAssertTrue(try store.removeHost(id: "work-vps"))
        hosts = store.loadHosts()
        XCTAssertEqual(hosts.count, 2)
        XCTAssertFalse(hosts.contains(where: { $0.id == "work-vps" }))
    }

    func testRemoteHostControlAPIInjectsBearerAuth() async throws {
        let host = ServerHost(
            id: "hetzner",
            name: "Hetzner Server",
            baseURL: URL(string: "https://hetzner.botfleet.app")!,
            authTokenSecret: "BOTFLEET_REMOTE_TOKEN",
            isLocal: false
        )

        MockURLProtocol.handler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fleet-remote-bearer-key")
            if request.url!.path == "/api/health" {
                return (200, Data(#"{"app":"botfleet","pid":999,"ready":true}"#.utf8))
            }
            if request.url!.path == "/api/system/control" {
                return (200, Data(#"""
                {
                    "activeBotCount": 2,
                    "persistOnExit": true,
                    "localVms": [
                        { "key": "shared", "name": "Shared Cloud VM", "running": true, "state": "running" }
                    ]
                }
                """#.utf8))
            }
            return (404, Data("{}".utf8))
        }

        let secrets = StubSecrets(values: ["BOTFLEET_REMOTE_TOKEN": "fleet-remote-bearer-key"])
        let api = ControlAPI(session: mockSession(), host: host, secrets: secrets)
        let status = try await api.status()
        XCTAssertEqual(secrets.asked.first, "BOTFLEET_REMOTE_TOKEN")
        XCTAssertEqual(status.activeBotCount, 2)
        XCTAssertTrue(status.isSharedVMRunning)
        XCTAssertFalse(status.canStartSharedVM)
        XCTAssertTrue(status.canStopSharedVM)
    }

    func testSharedVMLifecycleStateTransitions() async throws {
        let host = ServerHost(
            id: "hetzner",
            name: "Hetzner Server",
            baseURL: URL(string: "https://hetzner.botfleet.app")!,
            isLocal: false
        )

        // 1. Shared VM is stopped / missing
        MockURLProtocol.handler = { request in
            if request.url!.path == "/api/health" {
                return (200, Data(#"{"app":"botfleet","pid":123,"ready":true}"#.utf8))
            }
            if request.url!.path == "/api/system/control" {
                return (200, Data(#"""
                {
                    "activeBotCount": 0,
                    "persistOnExit": true,
                    "localVms": [
                        { "key": "shared", "name": "Shared Cloud VM", "running": false, "state": "missing" }
                    ]
                }
                """#.utf8))
            }
            if request.url!.path == "/api/system/control/vms/start" {
                return (200, Data(#"{"ok":true}"#.utf8))
            }
            if request.url!.path == "/api/system/control/vms/stop" {
                return (200, Data(#"{"ok":true}"#.utf8))
            }
            return (404, Data("{}".utf8))
        }

        let api = ControlAPI(session: mockSession(), host: host)
        let offStatus = try await api.status()
        XCTAssertFalse(offStatus.isSharedVMRunning)
        XCTAssertTrue(offStatus.canStartSharedVM)
        XCTAssertFalse(offStatus.canStopSharedVM)

        // Start shared VM
        try await api.startSharedVM()
        XCTAssertTrue(MockURLProtocol.requests.contains { $0.url!.path == "/api/system/control/vms/start" })

        // Stop shared VM
        try await api.stopSharedVM()
        XCTAssertTrue(MockURLProtocol.requests.contains { $0.url!.path == "/api/system/control/vms/stop" })
    }

    func testTheHostsFileKeepsTheSecretNameAndNeverAToken() throws {
        let storeFile = tempDir.appendingPathComponent("hosts.json")
        // A file from an older build may still carry a token value.  It is
        // ignored when read, and the next save drops it.
        try #"""
        [{"id":"local","name":"Local Mac","baseURL":"http://127.0.0.1:8799","isLocal":true},
         {"id":"old","name":"Old","baseURL":"https://old.example.test","isLocal":false,"authToken":"legacy-value"}]
        """#.write(to: storeFile, atomically: true, encoding: .utf8)
        let store = ServerHostStore(fileURL: storeFile)
        try store.upsertHost(ServerHost(id: "work-vps", name: "Work VPS",
                                        baseURL: URL(string: "https://vps.example.test")!,
                                        authTokenSecret: "WORK_VPS_TOKEN"))
        let raw = try String(contentsOf: storeFile, encoding: .utf8)
        XCTAssertTrue(raw.contains("WORK_VPS_TOKEN"))
        XCTAssertFalse(raw.contains("legacy-value"))
        XCTAssertFalse(raw.contains("\"authToken\""))
        let perms = try FileManager.default.attributesOfItem(atPath: storeFile.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(perms?.intValue, 0o600)
    }

    func testAMissingSecretIsRefusedByNameAndSendsNoRequest() async {
        let host = ServerHost(id: "hetzner", name: "Hetzner Server",
                              baseURL: URL(string: "https://hetzner.botfleet.app")!,
                              authTokenSecret: "BOTFLEET_REMOTE_TOKEN", isLocal: false)
        let api = ControlAPI(session: mockSession(), host: host, secrets: StubSecrets(values: [:]))
        do {
            _ = try await api.status()
            XCTFail("Expected a refusal")
        } catch {
            XCTAssertEqual(error as? ControlAPIError,
                           .refused(FleetSecretError.missing("BOTFLEET_REMOTE_TOKEN").localizedDescription))
        }
        XCTAssertTrue(MockURLProtocol.requests.isEmpty)
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

    func testServerHostStorePreservesCorruptFileAndRefusesOverwrite() throws {
        let storeFile = tempDir.appendingPathComponent("hosts.json")
        try "{ not valid json ...".write(to: storeFile, atomically: true, encoding: .utf8)

        let store = ServerHostStore(fileURL: storeFile)
        let loaded = store.loadHosts()
        XCTAssertEqual(loaded.count, 2)

        // Verify a .corrupt backup file was created
        let files = try FileManager.default.contentsOfDirectory(atPath: tempDir.path)
        XCTAssertTrue(files.contains { $0.contains("corrupt") })

        // Re-corrupt the file
        try "corrupt content".write(to: storeFile, atomically: true, encoding: .utf8)
        let hostToUpsert = ServerHost(id: "h1", name: "H1", baseURL: URL(string: "https://example.com")!)
        XCTAssertThrowsError(try store.upsertHost(hostToUpsert)) { error in
            XCTAssertEqual(error as? ServerHostStoreError, .unreadable(storeFile.path))
        }
        XCTAssertThrowsError(try store.removeHost(id: "hetzner"))

        // Refused to overwrite with defaults, original corrupt content is preserved
        let raw = try String(contentsOf: storeFile, encoding: .utf8)
        XCTAssertEqual(raw, "corrupt content")
    }

    func testAHostSaveThatFailsIsReportedAndNotLost() throws {
        // The folder the list lives in is a file, so nothing can be saved there.
        let blocker = tempDir.appendingPathComponent("not-a-folder")
        try Data().write(to: blocker)
        let store = ServerHostStore(fileURL: blocker.appendingPathComponent("hosts.json"))
        XCTAssertThrowsError(try store.saveHosts([ServerHost.defaultLocal])) { error in
            guard case .writeFailed = error as? ServerHostStoreError else {
                return XCTFail("Expected writeFailed, got \(error)")
            }
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

    func testAnAnswerNestedTooDeepIsRefused() {
        let deep = String(repeating: #"{"a":"#, count: HetznerCloudVMProvider.maxAnswerDepth + 1) + "1"
            + String(repeating: "}", count: HetznerCloudVMProvider.maxAnswerDepth + 1)
        XCTAssertNil(HetznerCloudVMProvider.boundedObject(Data(deep.utf8)))
        let wide = "{\"a\":[" + Array(repeating: "1", count: HetznerCloudVMProvider.maxAnswerNodes).joined(separator: ",") + "]}"
        XCTAssertNil(HetznerCloudVMProvider.boundedObject(Data(wide.utf8)))
        XCTAssertNotNil(HetznerCloudVMProvider.boundedObject(Data(#"{"server":{"status":"running"}}"#.utf8)))
    }

    private func mockSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: config)
    }
}

/// Answers secrets from a table, and records which names were asked for.
final class StubSecrets: SecretResolving {
    private let values: [String: String]
    private(set) var asked: [String] = []

    init(values: [String: String]) { self.values = values }

    func secret(named name: String) throws -> String {
        asked.append(name)
        guard let value = values[name] else { throw FleetSecretError.missing(name) }
        return value
    }
}
