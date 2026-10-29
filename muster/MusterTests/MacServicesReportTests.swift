import XCTest

/// The Mac's LaunchAgents,  as the collector reported them.
final class MacServicesReportTests: XCTestCase {
    private var generated: Date { FleetTime.parse("2026-10-28T15:00:00Z")! }

    private func readText(_ json: String, minutesAfter minutes: Double = 2) throws -> MacServicesReading {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-mac-services-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("mac-services.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return MacServicesReportReader.read(at: url, now: generated.addingTimeInterval(minutes * 60))
    }

    func test_a_current_file_reads_every_agent() throws {
        let reading = try readText(try fixture("mac_services_current", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.agents.count, 5)
        XCTAssertEqual(reading.loadedCount, 3)

        let doctor = reading.agents[0]
        XCTAssertTrue(doctor.controllable)
        XCTAssertEqual(doctor.state, .passed)
        XCTAssertEqual(doctor.statusText, "loaded, last exit 0")

        let health = reading.agents[1]
        XCTAssertEqual(health.state, .passed)
        XCTAssertEqual(health.statusText, "running, pid 4242")

        let services = reading.agents[2]
        XCTAssertEqual(services.state, .drift)
        XCTAssertEqual(services.statusText, "installed, not loaded")

        let missing = reading.agents[3]
        XCTAssertFalse(missing.plistInRepo)
        XCTAssertEqual(missing.state, .unproven)
        XCTAssertEqual(missing.statusText, "not installed")

        let failing = reading.agents[4]
        XCTAssertEqual(failing.state, .drift)
        // A flag that is not a JSON true is not a permission.
        XCTAssertFalse(failing.controllable)

        XCTAssertEqual(reading.state, .drift)
    }

    func test_a_stale_file_reads_as_unknown() throws {
        let reading = try readText(try fixture("mac_services_current", "json"), minutesAfter: 16)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_a_malformed_file_reads_as_unknown() throws {
        let reading = try readText("[1, 2, 3]")
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.agents.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_another_version_or_tool_reads_as_unknown() throws {
        let base = try fixture("mac_services_current", "json")
        let version = try readText(base.replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 3"))
        XCTAssertNotNil(version.problem)
        let tool = try readText(base.replacingOccurrences(of: "fleet-mac-services", with: "fleet-other"))
        XCTAssertNotNil(tool.problem)
    }

    func test_launchctl_unavailable_reads_as_unknown_with_a_problem() throws {
        let reading = try readText(try fixture("mac_services_launchctl_unavailable", "json"))
        // The rows are the collector's guesses,  so none is shown as a reading.
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.problem?.contains("launchctl could not be run") == true)
        XCTAssertTrue(reading.agents.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_an_agent_that_timed_out_is_unproven() throws {
        let reading = try readText(try fixture("mac_services_agent_timeout", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.agents.count, 2)
        let running = reading.agents[0]
        XCTAssertTrue(running.isObserved)
        XCTAssertEqual(running.state, .passed)
        let timedOut = reading.agents[1]
        XCTAssertFalse(timedOut.isObserved)
        XCTAssertEqual(timedOut.error, "timeout")
        // Installed and not loaded would read as drift;  a guess reads as unproven.
        XCTAssertEqual(timedOut.state, .unproven)
        XCTAssertEqual(timedOut.statusText, "launchctl timed out")
        XCTAssertEqual(reading.loadedCount, 1)
        XCTAssertEqual(reading.unreadCount, 1)
        XCTAssertEqual(reading.state, .unproven)
    }

    func test_an_agent_error_that_is_not_text_still_marks_it_unread() throws {
        let agent = try JSONDecoder().decode(
            MacAgent.self,
            from: Data(#"{"label": "com.jay.x", "installed": true, "loaded": false, "error": 7}"#.utf8)
        )
        XCTAssertEqual(agent.error, "unreadable")
        XCTAssertEqual(agent.state, .unproven)
        let clean = try JSONDecoder().decode(
            MacAgent.self,
            from: Data(#"{"label": "com.jay.x", "installed": true, "loaded": false, "error": null}"#.utf8)
        )
        XCTAssertNil(clean.error)
        XCTAssertEqual(clean.state, .drift)
    }

    func test_an_empty_list_proves_nothing() throws {
        let json = #"{"schemaVersion": 1, "tool": "fleet-mac-services", "generatedAt": "2026-10-28T15:00:00Z", "host": "mac", "agents": []}"#
        let reading = try readText(json)
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.state, .unknown)
    }
}
