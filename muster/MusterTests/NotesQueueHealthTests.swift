import XCTest

// The Notes queue's own state:  what the settings say it is configured to do, and what the
// doctor's report says it is doing.  The report is the shape `fleet-tools-doctor` writes.

private let testNow = FleetTime.parse("2026-11-26T18:00:00Z")!

private func report(rows: String, generatedAt: String = "2026-11-26T17:58:00Z") -> String {
    """
    {"schemaVersion": 1, "tool": "fleet-tools-doctor", "generatedAt": "\(generatedAt)",
     "hosts": [{"host": "server", "reachable": true, "generatedAt": "\(generatedAt)",
                "checks": [\(rows)]}]}
    """
}

private func reading(_ text: String, at date: Date = testNow) throws -> FleetToolsReading {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
    try Data(text.utf8).write(to: url)
    defer { try? FileManager.default.removeItem(at: url) }
    return FleetToolsReportReader.read(at: url, now: date)
}

final class NotesQueueHealthTests: XCTestCase {
    private let settings: [String: ServerSettingValue] = [
        "notes_queue.enabled": .bool(true),
        "listener_api.enabled": .bool(true),
        "notes_queue.worker_seats": .list([.text("JET"), .text("CLAUDE"), .text("ZC")]),
        "notes_queue.server_seats": .list([.text("CLAUDE"), .text("CODEX")]),
        "seat.CLAUDE.enabled": .bool(true),
        "seat.CODEX.enabled": .bool(true),
        "seat.ZC.enabled": .bool(true),
        "seat.JET.enabled": .bool(true),
    ]

    func test_the_queue_reads_its_configured_state_and_the_doctors_rows() throws {
        let doctor = try reading(report(rows: """
        {"check": "notes:queue", "status": "OK", "state": "current", "category": "service", "tool": "notes-queue", "detail": "0 queued, 0 running"},
        {"check": "notes:runner", "status": "WARN", "state": "stale", "category": "service", "tool": "notes-runner", "detail": "last poll 5m ago"}
        """))
        let health = NotesQueueHealth.make(settings: settings, doctor: doctor)
        XCTAssertTrue(health.lines.contains("Queue:  On"))
        XCTAssertTrue(health.lines.contains("Listener API:  On"))
        XCTAssertTrue(health.lines.contains("Seats:  3 through the Worker key, 2 through the Server key, 1 in both."))
        XCTAssertTrue(health.lines.contains("Server queue:  0 queued, 0 running"))
        XCTAssertTrue(health.lines.contains("Mac runner:  last poll 5m ago"))
        XCTAssertTrue(health.warnings.contains { $0.contains("Mac runner reads WARN") })
    }

    func test_a_queue_without_the_listener_api_warns_before_anything_is_sent() {
        var edited = settings
        edited["listener_api.enabled"] = .bool(false)
        let health = NotesQueueHealth.make(settings: edited, doctor: nil)
        XCTAssertTrue(health.warnings.contains("The Listener API is off, so the queue cannot serve a request until it is on."))
        XCTAssertTrue(health.lines.contains("Queue:  On"))
        XCTAssertTrue(health.lines.contains("Listener API:  Off"))
    }

    func test_a_missing_or_stale_report_is_not_read_as_a_pass() throws {
        let missing = NotesQueueHealth.make(settings: settings, doctor: nil)
        XCTAssertTrue(missing.lines.contains("The doctor's report holds no Notes row, so queue health is not read."))
        let stale = try reading(report(rows: "", generatedAt: "2026-11-26T16:00:00Z"))
        let health = NotesQueueHealth.make(settings: settings, doctor: stale)
        XCTAssertEqual(health.doctorProblem, "The doctor's report is not current, so queue health is not read.")
        XCTAssertNil(health.queue)
    }

    func test_empty_lists_and_operator_are_named() {
        var edited = settings
        edited["notes_queue.worker_seats"] = .list([])
        edited["notes_queue.server_seats"] = .list([])
        let health = NotesQueueHealth.make(settings: edited, doctor: nil)
        XCTAssertTrue(health.warnings.contains("Both seat lists are empty, so no seat may enqueue a request."))
        edited["notes_queue.worker_seats"] = .list([.text("OPERATOR")])
        let operatorHealth = NotesQueueHealth.make(settings: edited, doctor: nil)
        XCTAssertTrue(operatorHealth.warnings.contains("OPERATOR is a service, not a seat the queue can act for."))
    }
}