import XCTest

/// The hosts Muster administers, and the rule that keeps one host's rules off another
/// host's verdict.
final class FleetHostTests: XCTestCase {

    // MARK: - The verdict

    /// A host whose own rows are green reads as passing.
    func test_a_host_that_answers_reads_as_passing_when_its_own_rows_are_green() async throws {
        let host = FleetHost(
            id: "mac",
            label: "This Mac",
            kind: .mac,
            transport: LocalHostTransport(runner: FakeEngine(stdout: HostFixtures.envelope(passing: 1)))
        )
        let reading = await host.transport.status()
        let verdict = FleetHostVerdict(host: host, reading: reading)
        XCTAssertEqual(verdict.state, .passed)
        XCTAssertEqual(verdict.ownResults.count, 1)
        XCTAssertNil(verdict.reason)
    }

    /// The rule the whole host model rests on:  a Mac rule run against the server is
    /// not a server failure.
    func test_a_row_from_another_host_is_never_a_failure_on_this_host() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(stdout: HostFixtures.envelope(macRows: 3, macFailing: 3))
            )
        )
        let reading = await host.transport.status()
        let verdict = FleetHostVerdict(host: host, reading: reading)
        XCTAssertEqual(verdict.ownResults.count, 0)
        XCTAssertEqual(verdict.foreignResults.count, 3)
        XCTAssertTrue(verdict.failingResults.isEmpty)
        // The host has no rules of its own, so nothing was proved and nothing failed.
        XCTAssertEqual(verdict.state, .unproven)
        XCTAssertNotNil(verdict.reason)
    }

    /// A host's own failing row is a failure on that host.
    func test_a_failing_row_of_its_own_is_a_failure() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(stdout: HostFixtures.envelope(hetznerRows: 2, hetznerFailing: 1))
            )
        )
        let reading = await host.transport.status()
        let verdict = FleetHostVerdict(host: host, reading: reading)
        XCTAssertEqual(verdict.ownResults.count, 2)
        XCTAssertEqual(verdict.failingResults.count, 1)
        XCTAssertEqual(verdict.state, .missing)
    }

    /// A host that cannot be reached is unknown with the reason, never a healthy host.
    func test_a_host_that_cannot_be_reached_reads_as_unproven_with_the_reason() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(failure: HostProcessFailure.exit(255, "ssh: connect to host 167.233.254.55 port 22: Connection refused"))
            )
        )
        let reading = await host.transport.status()
        let verdict = FleetHostVerdict(host: host, reading: reading)
        XCTAssertEqual(verdict.state, .unproven)
        XCTAssertNotNil(verdict.reason)
    }

    /// ssh's own text can carry host names and addresses, so it is never copied.
    func test_ssh_failure_text_is_never_copied_into_the_reason() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(failure: HostProcessFailure.exit(255, "ssh: connect to host 167.233.254.55 port 22: Connection refused"))
            )
        )
        let reading = await host.transport.status()
        let reason = reading.reason ?? ""
        XCTAssertFalse(reason.contains("167.233.254.55"))
        XCTAssertFalse(reason.contains("Connection refused"))
    }

    /// A timeout is a host that could not be reached, not a host with nothing wrong.
    func test_a_timeout_reads_as_unreachable() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(failure: .timedOut)
            )
        )
        let reading = await host.transport.status()
        XCTAssertNil(reading.envelope)
        XCTAssertNotNil(reading.reason)
    }

    /// The engine exits 1 when the verdict is FAIL, and that is still an answer.
    func test_an_exit_of_one_with_a_document_is_still_an_answer() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(stdout: HostFixtures.envelope(hetznerRows: 1), exitCode: 1)
            )
        )
        let reading = await host.transport.status()
        XCTAssertNotNil(reading.envelope)
        XCTAssertNil(reading.reason)
    }

    /// An answer this app cannot read is unknown, never a partly-read pass.
    func test_a_malformed_answer_reads_as_unreachable() async throws {
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(
                hostID: "hetzner",
                alias: "muster-hetzner",
                process: FakeProcess(stdout: "{\"schemaVersion\": 99}")
            )
        )
        let reading = await host.transport.status()
        XCTAssertNil(reading.envelope)
        XCTAssertNotNil(reading.reason)
    }

    // MARK: - The route

    /// The route is an argument array, never a shell string.
    func test_the_ssh_route_runs_an_argument_array_and_never_a_shell() {
        let arguments = SSHHostTransport.arguments(alias: "muster-hetzner")
        XCTAssertEqual(arguments.first, "ssh")
        XCTAssertTrue(arguments.contains("muster-hetzner"))
        XCTAssertFalse(arguments.contains { $0.contains("muster-hetzner;") })
        XCTAssertTrue(arguments.contains("BatchMode=yes"))
    }

    /// An alias that could be read as a flag is refused before any process exists.
    func test_an_alias_that_could_be_read_as_a_flag_is_refused_before_any_process_runs() async throws {
        let process = FakeProcess(stdout: HostFixtures.envelope(hetznerRows: 1))
        let host = FleetHost(
            id: "hetzner",
            label: "Hetzner Server",
            kind: .hetzner,
            transport: SSHHostTransport(hostID: "hetzner", alias: "-oProxyCommand=touch /tmp/pwn", process: process)
        )
        let reading = await host.transport.status()
        XCTAssertNil(reading.envelope)
        XCTAssertNotNil(reading.reason)
        XCTAssertEqual(process.runs, 0)
    }

    // MARK: - The hosts

    /// The Mac is always the first host.
    func test_the_mac_is_the_first_host() {
        let hosts = FleetHostSet.hosts(localRunner: FakeEngine(stdout: HostFixtures.envelope(passing: 1)))
        XCTAssertEqual(hosts.first?.id, "mac")
        XCTAssertEqual(hosts.first?.kind, .mac)
    }

    /// The host kinds are the engine's collector words, so a rule and a host name the
    /// same thing the same way.
    func test_the_host_kinds_are_the_collector_words() {
        XCTAssertEqual(FleetHostKind.mac.rawValue, "mac")
        XCTAssertEqual(FleetHostKind.hetzner.rawValue, "hetzner")
        XCTAssertEqual(FleetHostKind.vm.rawValue, "vm")
        XCTAssertEqual(FleetHostKind.api.rawValue, "api")
    }
}

// MARK: - Fakes

private enum HostProcessFailure: Error {
    case exit(Int32, String)
    case timedOut
}

private final class FakeProcess: HostProcessRunning, @unchecked Sendable {
    private let stdout: String
    private let exitCode: Int32
    private let failure: HostProcessFailure?
    private(set) var runs = 0

    init(stdout: String = "", exitCode: Int32 = 0, failure: HostProcessFailure? = nil) {
        self.stdout = stdout
        self.exitCode = exitCode
        self.failure = failure
    }

    func run(_ arguments: [String], timeout: TimeInterval) async throws -> FleetConfigCLIResult {
        runs += 1
        if let failure {
            switch failure {
            case .exit(let code, let stderr):
                return FleetConfigCLIResult(exitCode: code, stdout: "", stderr: stderr, timedOut: false)
            case .timedOut:
                return FleetConfigCLIResult(exitCode: -1, stdout: "", stderr: "", timedOut: true)
            }
        }
        return FleetConfigCLIResult(exitCode: exitCode, stdout: stdout, stderr: "", timedOut: false)
    }
}

private final class FakeEngine: FleetConfigRunning, @unchecked Sendable {
    private let stdout: String

    init(stdout: String) {
        self.stdout = stdout
    }

    func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult {
        FleetConfigCLIResult(exitCode: 0, stdout: stdout, stderr: "", timedOut: false)
    }
}

// MARK: - Fixtures

private enum HostFixtures {
    /// An envelope with rows for one or more collectors.
    static func envelope(
        passing: Int = 0,
        macRows: Int = 0,
        macFailing: Int = 0,
        hetznerRows: Int = 0,
        hetznerFailing: Int = 0
    ) -> String {
        var rows: [String] = []
        for index in 0..<passing {
            rows.append(row("mac-rule-\(index)", collector: "mac", state: "PASS", failing: false))
        }
        for index in 0..<macRows {
            let failing = index < macFailing
            rows.append(row("mac-rule-\(index)", collector: "mac", state: failing ? "MISSING" : "PASS", failing: failing))
        }
        for index in 0..<hetznerRows {
            let failing = index < hetznerFailing
            rows.append(row("hetzner-rule-\(index)", collector: "hetzner", state: failing ? "MISSING" : "PASS", failing: failing))
        }
        let total = rows.count
        let failing = macFailing + hetznerFailing
        return """
        {
         "schemaVersion": 1,
         "tool": "fleet-config",
         "command": "status",
         "generatedAt": "2026-10-24T05:00:00Z",
         "summary": {
          "total": \(total),
          "byState": {"PASS": \(total - failing), "DRIFT": 0, "MISSING": \(failing), "UNPROVEN": 0, "UNSUPPORTED": 0, "UNKNOWN": 0},
          "overallVerdict": "\(failing == 0 ? "PASS" : "FAIL")",
          "failing": \(failing)
         },
         "results": [\(rows.joined(separator: ","))]
        }
        """
    }

    private static func row(_ id: String, collector: String, state: String, failing: Bool) -> String {
        """
        {
         "ruleId": "\(id)",
         "seat": "CLAUDE",
         "platform": "claude",
         "kind": "instructions",
         "state": "\(state)",
         "reason": "a synthetic row for the test",
         "reasonCode": "\(failing ? "missing" : "ok")",
         "expected": {"present": true},
         "observed": {"present": true},
         "proofLevelRequired": "L0",
         "proofLevelAchieved": "L0",
         "collector": "\(collector)",
         "checkType": "marked_block_hash",
         "checkedAt": "2026-10-24T05:00:00Z",
         "expiresAt": null,
         "ttlSeconds": 86400,
         "stale": false,
         "failing": \(failing)
        }
        """
    }
}
