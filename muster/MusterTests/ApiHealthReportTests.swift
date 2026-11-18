import XCTest

/// Socratic Trade and Congress.Trade health,  and what Muster can say about it (AFC-398).
///
/// The rule under test is the doctor's rule:  a file that cannot be trusted reads as
/// unknown,  never as a pass.  The mapping itself is pinned on the collector side by
/// `scripts/test_fleet_api_health.py`;  these tests pin the reader:  the senate_efd
/// fixture shows CT degraded with ingestion alive,  not failed,  and a timed-out
/// collector's rows read as unproven,  never as healthy.
final class ApiHealthReportTests: XCTestCase {
    private var generated: Date { FleetTime.parse("2026-10-28T15:00:00Z")! }

    private func readText(_ json: String?, minutesAfter minutes: Double = 2) throws -> ApiHealthReading {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-api-health-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("api-health.json")
        if let json {
            try json.write(to: url, atomically: true, encoding: .utf8)
        }
        return ApiHealthReportReader.read(at: url, now: generated.addingTimeInterval(minutes * 60))
    }

    func test_a_current_file_reads_both_apps() throws {
        let reading = try readText(try fixture("api_health_current", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.worst, .warn)
        XCTAssertEqual(reading.apps.map(\.app), ["ST", "CT"])
        let st = try XCTUnwrap(reading.apps.first)
        XCTAssertEqual(st.healthState, .ok)
        XCTAssertEqual(st.cellState, .passed)
        XCTAssertEqual(st.mangoReachable, true)
        XCTAssertEqual(st.observationAgeSeconds, 30)
        let ct = try XCTUnwrap(reading.apps.last)
        // senate_efd is down and ingestion is alive:  degraded,  not failed.
        XCTAssertEqual(ct.healthState, .warn)
        XCTAssertEqual(ct.cellState, .drift)
        XCTAssertEqual(ct.ingestionAlive, true)
        XCTAssertEqual(ct.upstreamStatus, "degraded")
        let senate = try XCTUnwrap(ct.components.first { $0.name == "senate_efd" })
        XCTAssertEqual(senate.healthState, .fail)
        XCTAssertEqual(reading.state, .drift)
        XCTAssertEqual(reading.age, "2m")
    }

    func test_a_timed_out_collector_reads_as_unproven_never_healthy() throws {
        let reading = try readText(try fixture("api_health_unreachable", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.worst, .unknown)
        for app in reading.apps {
            XCTAssertEqual(app.healthState, .unknown)
            XCTAssertEqual(app.cellState, .unproven)
            XCTAssertEqual(app.error, "timeout")
        }
        XCTAssertEqual(reading.state, .unproven)
    }

    func test_another_version_reads_as_unknown() throws {
        let json = try fixture("api_health_current", "json")
            .replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 2")
        let reading = try readText(json)
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.apps.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_another_tool_reads_as_unknown() throws {
        let json = try fixture("api_health_current", "json")
            .replacingOccurrences(of: "fleet-api-health", with: "something-else")
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
        let reading = try readText(try fixture("api_health_current", "json"), minutesAfter: 46)
        XCTAssertNil(reading.problem)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertTrue(reading.apps.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_the_file_is_read_from_the_state_directory() {
        let url = ApiHealthReportReader.reportURL(home: URL(fileURLWithPath: "/Users/example"))
        XCTAssertEqual(url.path, "/Users/example/.local/state/fleet-tools/api-health.json")
        XCTAssertEqual(ApiHealthContract.maxAgeSeconds, 45 * 60)
        XCTAssertEqual(ApiHealthContract.expectedCollector, "api")
    }
}
