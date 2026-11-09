import XCTest

/// The Worker's Linear token health,  and what Muster can say about it.
///
/// The rule under test is the doctor's rule:  a file that cannot be trusted reads as
/// unknown,  never as a pass,  and a Worker the collector could not reach shows its reason
/// and no seat rows.
final class WorkerHealthReportTests: XCTestCase {
    private var generated: Date { FleetTime.parse("2026-10-28T15:00:00Z")! }

    private func readText(_ json: String?, minutesAfter minutes: Double = 2) throws -> WorkerHealthReading {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-worker-health-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("worker-health.json")
        if let json {
            try json.write(to: url, atomically: true, encoding: .utf8)
        }
        return WorkerHealthReportReader.read(at: url, now: generated.addingTimeInterval(minutes * 60))
    }

    func test_a_current_file_reads_the_seats() throws {
        let reading = try readText(try fixture("worker_health_current", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertTrue(reading.reachable)
        XCTAssertEqual(reading.worst, .warn)
        XCTAssertEqual(reading.bundle, "ok")
        XCTAssertEqual(reading.seats.map(\.seat), ["CLAUDE", "CODEX"])
        let claude = try XCTUnwrap(reading.seats.first)
        XCTAssertEqual(claude.cellState, .passed)
        XCTAssertEqual(claude.mintCount, 4)
        XCTAssertEqual(claude.tokenText, "minted 3.5d ago, expires in 26.5d")
        XCTAssertNil(claude.lastMintFailKind)
        let codex = try XCTUnwrap(reading.seats.last)
        XCTAssertEqual(codex.healthState, .warn)
        XCTAssertEqual(codex.cellState, .drift)
        XCTAssertEqual(codex.lastMintFailKind, "http")
        XCTAssertEqual(codex.note, "the last mint failed, the cached token still works")
        XCTAssertEqual(reading.state, .drift)
        XCTAssertEqual(reading.age, "2m")
    }

    func test_an_unreachable_worker_shows_its_reason_and_no_seats() throws {
        let reading = try readText(try fixture("worker_health_unreachable", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertFalse(reading.reachable)
        XCTAssertTrue(reading.seats.isEmpty)
        XCTAssertEqual(reading.unreachableReason, "the signing key could not be read on this Mac")
        XCTAssertEqual(reading.state, .unproven)
    }

    func test_another_version_reads_as_unknown() throws {
        let json = try fixture("worker_health_current", "json")
            .replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 2")
        let reading = try readText(json)
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.seats.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_another_tool_reads_as_unknown() throws {
        let json = try fixture("worker_health_current", "json")
            .replacingOccurrences(of: "fleet-worker-health", with: "something-else")
        let reading = try readText(json)
        XCTAssertNotNil(reading.problem)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_a_malformed_file_reads_as_unknown() throws {
        let reading = try readText("{not json")
        XCTAssertNotNil(reading.problem)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_a_missing_file_reads_as_unknown() throws {
        let reading = try readText(nil)
        XCTAssertEqual(reading.problem?.contains("the file is not there"), true)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_a_stale_file_reads_as_unknown() throws {
        let reading = try readText(try fixture("worker_health_current", "json"), minutesAfter: 46)
        XCTAssertNil(reading.problem)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_a_file_inside_the_window_is_current() throws {
        let reading = try readText(try fixture("worker_health_current", "json"), minutesAfter: 44)
        XCTAssertTrue(reading.isCurrent)
    }

    func test_an_invalid_bundle_is_a_failure_whatever_the_seats_say() throws {
        let json = try fixture("worker_health_current", "json")
            .replacingOccurrences(of: "\"bundle\": \"ok\"", with: "\"bundle\": \"invalid\"")
        let reading = try readText(json)
        XCTAssertEqual(reading.state, .missing)
    }

    func test_a_state_the_app_does_not_know_is_not_ok() throws {
        let json = try fixture("worker_health_current", "json")
            .replacingOccurrences(of: "\"state\": \"ok\"", with: "\"state\": \"wobble\"")
        let reading = try readText(json)
        XCTAssertEqual(reading.seats.first?.healthState, .unknown)
        XCTAssertEqual(reading.seats.first?.cellState, .unproven)
    }

    func test_the_error_codes_read_as_words() {
        XCTAssertEqual(WorkerHealthReportReader.describe(errorCode: "http_503"), "the Worker answered HTTP 503")
        XCTAssertEqual(WorkerHealthReportReader.describe(errorCode: "no_answer"), "the Worker did not answer")
        XCTAssertEqual(WorkerHealthReportReader.describe(errorCode: nil), "the collector did not say why")
        XCTAssertEqual(WorkerHealthReportReader.describe(errorCode: "odd_code"), "odd_code")
    }

    func test_the_file_is_read_from_the_state_directory() {
        let url = WorkerHealthReportReader.reportURL(home: URL(fileURLWithPath: "/Users/example"))
        XCTAssertEqual(url.path, "/Users/example/.local/state/fleet-tools/worker-health.json")
        XCTAssertEqual(WorkerHealthContract.maxAgeSeconds, 45 * 60)
    }
}
