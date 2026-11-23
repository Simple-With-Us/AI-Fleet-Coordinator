import XCTest

/// The Mac poll's bearer status file:  a file that cannot be trusted reads as not current, never as healthy.
final class BearerStatusReportTests: XCTestCase {
    private let now = FleetTime.parse("2026-11-23T16:10:00Z")!

    private func parse(_ json: String, at date: Date? = nil) -> BearerStatusReading {
        BearerStatusReader.parse(Data(json.utf8), now: date ?? now)
    }

    private let good = """
    {"schemaVersion":1,"tool":"fleet-bearer-status","generatedAt":"2026-11-23T16:07:00Z","ok":true,
     "rows":[{"identity":"WISE","kind":"hosted","problem":"ok","active":true,"expired":false,"expiresAt":"2026-12-23T00:00:00Z","mcpAccept":true,"paused":false,"missingRecords":null,"mismatchCount":0},
             {"identity":"ZC","kind":"bearer_only","problem":"credentials_missing","active":false,"expired":false,"missingRecords":["ZC_ZULIP_KEY"]}],
     "alarms":[{"key":"identity:ZC","summary":"ZC credentials missing","since":"2026-11-23T16:00:00Z"}],"recovered":[]}
    """

    func test_a_current_file_reads_rows_and_alarms() {
        let reading = parse(good)
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.rows.map(\.identity), ["WISE", "ZC"])
        XCTAssertTrue(reading.rows[0].isHealthy)
        XCTAssertEqual(reading.rows[0].verdict, "live")
        XCTAssertFalse(reading.rows[1].isHealthy)
        XCTAssertEqual(reading.rows[1].verdict, "credentials missing")
        XCTAssertEqual(reading.rows[1].missingRecords, ["ZC_ZULIP_KEY"])
        XCTAssertEqual(reading.alarms.map(\.key), ["identity:ZC"])
    }

    func test_an_old_file_is_not_current() {
        XCTAssertFalse(parse(good, at: now.addingTimeInterval(20 * 60)).isCurrent)
    }

    func test_another_version_and_bad_json_are_not_read() {
        XCTAssertNotNil(parse(#"{"schemaVersion":2,"rows":[]}"#).problem)
        XCTAssertTrue(parse(#"{"schemaVersion":2,"rows":[]}"#).rows.isEmpty)
        XCTAssertNotNil(parse("not json").problem)
    }

    func test_a_poll_that_could_not_reach_the_worker_carries_its_reason() {
        let reading = parse(#"{"schemaVersion":1,"generatedAt":"2026-11-23T16:08:00Z","ok":false,"rows":[],"error":"access_login_needed"}"#)
        XCTAssertFalse(reading.pollOk)
        XCTAssertEqual(reading.problem, "access login needed")
    }

    func test_a_missing_file_reads_as_a_problem() {
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("no-\(UUID().uuidString).json")
        XCTAssertNotNil(BearerStatusReader.read(at: url, now: now).problem)
    }
}
