import XCTest

/// The newer halves of the server's report:  the host's numbers,  the fleet's units and the
/// file summary.
///
/// The rule under test is that every new half is optional:  a report from a collector that
/// predates them reads exactly as it did,  and a half that is malformed costs that half alone.
final class ServerStatusExtensionTests: XCTestCase {
    private var generated: Date { FleetTime.parse("2026-10-28T15:00:00Z")! }

    /// Write a fixture to a file and read it back,  some minutes after it was written.
    private func readFixture(_ name: String, minutesAfter minutes: Double = 2) throws -> ServerStatusReading {
        let json = try fixture(name, "json")
        return try readText(json, minutesAfter: minutes)
    }

    private func readText(_ json: String, minutesAfter minutes: Double = 2) throws -> ServerStatusReading {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-server-ext-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("server-status.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return ServerStatusReportReader.read(at: url, now: generated.addingTimeInterval(minutes * 60))
    }

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    // MARK: - The current shape

    func test_the_current_report_carries_the_host_numbers() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        let metrics = try XCTUnwrap(reading.hostMetrics)
        XCTAssertEqual(metrics.cpuCount, 4)
        XCTAssertEqual(metrics.loadText, "1.50 1.25 1.00")
        XCTAssertEqual(try XCTUnwrap(metrics.loadFraction), 0.375, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(metrics.memory?.usedFraction), 0.5, accuracy: 0.0001)
        XCTAssertEqual(try XCTUnwrap(metrics.swap?.usedFraction), 0.25, accuracy: 0.0001)
        XCTAssertEqual(metrics.uptimeText, "2d")
        XCTAssertEqual(metrics.disks.map(\.mount), ["/", "/mnt/example"])
        XCTAssertEqual(try XCTUnwrap(metrics.disks.first?.usedFraction), 0.9, accuracy: 0.0001)
        XCTAssertEqual(metrics.disks.first?.state, .drift)
        XCTAssertEqual(metrics.disks.last?.state, .passed)
        // The fullest disk is the worst number on the host.
        XCTAssertEqual(reading.hostState, .drift)
    }

    func test_the_current_report_carries_the_services() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertEqual(reading.services.count, 2)
        let timer = try XCTUnwrap(reading.services.first)
        XCTAssertEqual(timer.unit, "fleet-server-status.timer")
        XCTAssertEqual(timer.scope, "user")
        XCTAssertEqual(timer.id, "user/fleet-server-status.timer")
        XCTAssertTrue(timer.controllable)
        XCTAssertEqual(timer.state, .passed)
        XCTAssertEqual(timer.stateText, "active (waiting)")
        XCTAssertEqual(timer.enabled, "enabled")
        XCTAssertNotNil(timer.sinceText)
        let failed = try XCTUnwrap(reading.services.last)
        XCTAssertFalse(failed.controllable)
        XCTAssertEqual(failed.state, .missing)
        XCTAssertEqual(reading.servicesState, .missing)
        // A failed unit is the report's worst half.
        XCTAssertEqual(reading.state, .missing)
    }

    func test_the_current_report_carries_the_file_summary_and_info_files() throws {
        let reading = try readFixture("server_status_current")
        XCTAssertTrue(reading.filesTruncated)
        XCTAssertEqual(reading.filesSummary?.found, 812)
        XCTAssertEqual(reading.filesSummary?.limit, 500)
        // A README change is information:  it is listed,  and it is not drift.
        XCTAssertEqual(reading.noticedFiles.map(\.fileName), ["README.md"])
        XCTAssertTrue(reading.driftedFiles.isEmpty)
        XCTAssertEqual(reading.noticedFiles.first?.fileDrift, .info)
        XCTAssertEqual(reading.noticedFiles.first?.state, .passed)
        XCTAssertEqual(reading.configState, .passed)
    }

    // MARK: - The old shape

    func test_an_old_report_without_the_new_halves_reads_as_before() throws {
        let reading = try readFixture("server_status_old_shape")
        XCTAssertNil(reading.problem)
        XCTAssertNil(reading.hostMetrics)
        XCTAssertTrue(reading.services.isEmpty)
        XCTAssertNil(reading.filesSummary)
        XCTAssertFalse(reading.filesTruncated)
        XCTAssertEqual(reading.state, .passed)
        // The halves it does not carry prove nothing,  and they do not pull the report down.
        XCTAssertEqual(reading.hostState, .unproven)
        XCTAssertEqual(reading.servicesState, .unproven)
    }

    // MARK: - The malformed shape

    func test_a_malformed_new_half_costs_that_half_alone() throws {
        let reading = try readFixture("server_status_malformed_halves")
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.coolify.count, 1)
        XCTAssertNil(reading.hostMetrics)
        XCTAssertTrue(reading.services.isEmpty)
        XCTAssertNil(reading.filesSummary)
        // Coolify passes,  and the empty halves are unproven rather than a pass.
        XCTAssertEqual(reading.state, .unproven)
    }

    func test_a_service_with_missing_fields_is_unproven_and_not_controllable() throws {
        let service = try decode(ServerService.self, #"{"unit": "example.service", "controllable": "yes", "enabled": true}"#)
        XCTAssertFalse(service.controllable)
        XCTAssertEqual(service.state, .unproven)
        XCTAssertEqual(service.id, "system/example.service")
        XCTAssertEqual(service.enabled, "enabled")
        XCTAssertEqual(service.stateText, "unknown")
        XCTAssertNil(service.sinceText)
    }

    func test_a_stopped_unit_reads_as_drift() throws {
        let service = try decode(ServerService.self, #"{"unit": "example.timer", "activeState": "inactive", "subState": "dead"}"#)
        XCTAssertEqual(service.state, .drift)
        XCTAssertEqual(service.stateText, "inactive (dead)")
    }

    func test_a_disk_reads_by_how_full_it_is() throws {
        let full = try decode(HostDisk.self, #"{"mount": "/", "usedBytes": 96, "freeBytes": 4}"#)
        XCTAssertEqual(full.state, .missing)
        let roomy = try decode(HostDisk.self, #"{"mount": "/", "usedBytes": 20, "freeBytes": 80, "totalBytes": 100}"#)
        XCTAssertEqual(roomy.state, .passed)
        let unread = try decode(HostDisk.self, #"{"mount": "/mnt/x"}"#)
        XCTAssertNil(unread.usedFraction)
        XCTAssertEqual(unread.state, .unproven)
    }

    func test_a_host_without_swap_has_no_swap_share() throws {
        let swap = try decode(HostSwap.self, #"{"totalBytes": 0, "freeBytes": 0}"#)
        XCTAssertNil(swap.usedFraction)
    }

    func test_host_numbers_of_the_wrong_type_are_left_out() throws {
        let metrics = try decode(
            HostMetrics.self,
            #"{"loadAverage": "high", "cpuCount": "four", "uptimeSeconds": 60, "disks": [{"mount": "/", "usedBytes": 1, "freeBytes": 1}]}"#
        )
        XCTAssertTrue(metrics.loadAverage.isEmpty)
        XCTAssertNil(metrics.cpuCount)
        XCTAssertNil(metrics.loadFraction)
        XCTAssertEqual(metrics.uptimeText, "1m")
        XCTAssertEqual(metrics.disks.count, 1)
    }

    // MARK: - The stale shape

    func test_a_stale_report_shows_no_host_or_service_state() throws {
        let reading = try readFixture("server_status_current", minutesAfter: 46)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertEqual(reading.hostState, .unproven)
        XCTAssertEqual(reading.servicesState, .unproven)
    }

    // MARK: - Redeploy

    /// Only the rows the server would redeploy are offered:  an application whose uuid came
    /// from a Coolify label,  the rule `uuid_from_label` applies in fleet-ops-control.py.
    func test_only_a_labelled_application_is_offered_a_redeploy() throws {
        let reading = try readFixture("server_status_coolify_rows")
        XCTAssertNil(reading.problem)
        let offered = reading.coolify.filter { reading.offersRedeploy($0) }.map(\.name)
        XCTAssertEqual(offered, ["labelled-app", "plain-app", "gone-app"])
    }

    func test_a_service_or_database_row_is_not_redeployable() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let service = try XCTUnwrap(reading.coolify.first { $0.name == "a-service" })
        XCTAssertFalse(service.isRedeployable)
        let database = try XCTUnwrap(reading.coolify.first { $0.name == "a-database" })
        XCTAssertFalse(database.isRedeployable)
        let standalone = try XCTUnwrap(reading.coolify.first { $0.name == "standalone-box" })
        XCTAssertFalse(standalone.isRedeployable)
    }

    func test_a_row_whose_uuid_is_its_name_is_not_redeployable() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let named = try XCTUnwrap(reading.coolify.first { $0.name == "named-app" })
        XCTAssertFalse(named.isRedeployable)
        let suffixed = try XCTUnwrap(reading.coolify.first { $0.name == "suffixed (service)" })
        XCTAssertFalse(suffixed.isRedeployable)
    }

    func test_a_row_that_says_where_its_uuid_came_from_is_believed_only_for_a_label() throws {
        let reading = try readFixture("server_status_coolify_rows")
        let labelled = try XCTUnwrap(reading.coolify.first { $0.name == "labelled-app" })
        XCTAssertTrue(labelled.isRedeployable)
        let fromName = try XCTUnwrap(reading.coolify.first { $0.name == "source-name" })
        XCTAssertFalse(fromName.isRedeployable)
        // A source that is not text still says something,  and it does not say `label`.
        let odd = try XCTUnwrap(reading.coolify.first { $0.name == "source-odd" })
        XCTAssertTrue(odd.declaresUUIDSource)
        XCTAssertNil(odd.uuidSource)
        XCTAssertFalse(odd.isRedeployable)
    }

    func test_the_first_row_with_a_uuid_decides_like_the_server() throws {
        let reading = try readFixture("server_status_coolify_rows")
        // The server redeploys the first row whose uuid matches,  and that row is a service.
        let app = try XCTUnwrap(reading.coolify.first { $0.name == "shared-app" })
        XCTAssertTrue(app.isRedeployable)
        XCTAssertFalse(reading.offersRedeploy(app))
    }

    func test_an_info_drift_is_a_known_drift() {
        XCTAssertEqual(ServerStatusDrift(reportValue: "info"), .info)
        XCTAssertEqual(ServerStatusDrift(reportValue: "wobble"), .unknown)
    }
}
