import XCTest

/// What Hog Hunter reports about this Mac,  as Muster reads it.
final class HogHunterMacReportTests: XCTestCase {
    private var generated: Date { FleetTime.parse("2026-11-04T21:00:00Z")! }

    private func read(
        _ json: String, minutesAfter minutes: Double = 1, mode: mode_t = 0o600
    ) throws -> HogHunterMacReading {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-hh-mac-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("hoghunter-mac.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        chmod(url.path, mode)
        return HogHunterMacReportReader.read(at: url, now: generated.addingTimeInterval(minutes * 60))
    }

    func test_a_current_report_reads_every_part() throws {
        let reading = try read(try fixture("hoghunter_mac_current", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.appVersion, "1.0.9")
        XCTAssertEqual(reading.hostMetrics?.cpuText, "41%")
        XCTAssertEqual(reading.hostMetrics?.disks.count, 2)
        XCTAssertEqual(reading.processes.map(\.displayName), ["Xcode", "kernel_task"])
        XCTAssertEqual(reading.lock, .held)
        XCTAssertEqual(reading.maintain?.state, .passed)
        XCTAssertEqual(reading.serverSnapshotAgeSeconds, 120)
        // Load of 2.5 on ten cores is fine;  the external disk at 96 percent is not.
        XCTAssertEqual(reading.state, .missing)
        let rows = Dictionary(uniqueKeysWithValues: reading.rows.map { ($0.label, $0) })
        XCTAssertEqual(rows["Load"]?.value, "2.50 2.00 1.50 on 10 cores")
        XCTAssertEqual(rows["Memory"]?.value.hasPrefix("63% used"), true)
        XCTAssertEqual(rows["Swap"]?.value.hasPrefix("25% used"), true)
        XCTAssertEqual(rows["Disk /Volumes/External"]?.state, .missing)
        XCTAssertEqual(rows["Disk /"]?.state, .passed)
        XCTAssertEqual(rows["Housekeeper lock"]?.value, "Held by a running cleanup")
        XCTAssertEqual(rows["Cleaning regimen"]?.value.hasPrefix("On,"), true)
        XCTAssertEqual(rows["Server snapshot"]?.value, "Fetched 2m ago")
        XCTAssertEqual(rows["Hog Hunter"]?.value, "Version 1.0.9")
    }

    func test_a_report_older_than_five_minutes_reads_unknown_and_says_hog_hunter_is_not_reporting() throws {
        let reading = try read(try fixture("hoghunter_mac_current", "json"), minutesAfter: 6)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertEqual(reading.problem, HogHunterMacReading.notReportingText)
        XCTAssertTrue(reading.problem?.contains("not running or has not reported") == true)
        XCTAssertTrue(reading.rows.isEmpty)
        XCTAssertTrue(reading.hogs.isEmpty)
    }

    func test_a_report_just_inside_the_window_is_current() throws {
        XCTAssertTrue(try read(try fixture("hoghunter_mac_current", "json"), minutesAfter: 4.9).isCurrent)
    }

    func test_a_missing_file_reads_as_hog_hunter_closed() {
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("nope-\(UUID().uuidString).json")
        let reading = HogHunterMacReportReader.read(at: url, now: generated)
        XCTAssertEqual(reading.problem, HogHunterMacReading.notReportingText)
        XCTAssertEqual(reading.state, .unknown)
    }

    func test_the_wrong_schema_version_or_tool_reads_unknown() throws {
        let current = try fixture("hoghunter_mac_current", "json")
        let version = try read(current.replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 2"))
        XCTAssertEqual(version.state, .unknown)
        XCTAssertTrue(version.problem?.contains("version 2") == true)
        let tool = try read(current.replacingOccurrences(of: "hoghunter-mac-status", with: "fleet-server-status"))
        XCTAssertEqual(tool.state, .unknown)
        XCTAssertTrue(tool.problem?.contains("fleet-server-status") == true)
    }

    func test_a_malformed_file_or_missing_time_reads_unknown() throws {
        XCTAssertNotNil(try read("{not json").problem)
        XCTAssertNotNil(try read("{\"schemaVersion\":1,\"tool\":\"hoghunter-mac-status\"}").problem)
        let future = try read(try fixture("hoghunter_mac_current", "json"), minutesAfter: -10)
        XCTAssertEqual(future.state, .unknown)
        XCTAssertNotNil(future.problem)
    }

    func test_missing_and_wrong_typed_fields_cost_only_those_fields() throws {
        let reading = try read(try fixture("hoghunter_mac_partial", "json"))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.hostMetrics?.cpuCount, 8)
        XCTAssertEqual(reading.hostMetrics?.loadAverage, [])
        XCTAssertNil(reading.hostMetrics?.memory?.totalBytes)
        XCTAssertTrue(reading.processes.isEmpty)
        XCTAssertNil(reading.maintain?.health)
        XCTAssertEqual(reading.maintain?.lastRunOutcome, "partial")
        XCTAssertEqual(reading.lock, .unknown)
        XCTAssertNil(reading.regimen)
        // Nothing was proven about the host's health,  so the report does not read as a pass.
        XCTAssertEqual(reading.state, .unknown)
        let rows = Dictionary(uniqueKeysWithValues: reading.rows.map { ($0.label, $0.value) })
        XCTAssertEqual(rows["Memory"], "Not reported")
        XCTAssertEqual(rows["CPU"], "Not reported")
        XCTAssertEqual(rows["Cleaning regimen"], "Not reported")
        XCTAssertEqual(rows["Housekeeper lock"], "Not reported")
    }

    func test_an_unknown_lock_word_and_a_stale_lock() throws {
        let current = try fixture("hoghunter_mac_current", "json")
        let stale = try read(current.replacingOccurrences(of: "\"held\"", with: "\"stale\""))
        XCTAssertEqual(stale.lock, .stale)
        XCTAssertTrue(stale.lockText.hasPrefix("Stale"))
        XCTAssertEqual(try read(current.replacingOccurrences(of: "\"held\"", with: "\"banana\"")).lock, .unknown)
    }

    // MARK: - The file is the authentication

    func test_a_group_or_world_writable_file_is_not_read() throws {
        for mode: mode_t in [0o620, 0o602, 0o666] {
            let reading = try read(try fixture("hoghunter_mac_current", "json"), mode: mode)
            XCTAssertEqual(reading.state, .unknown)
            XCTAssertTrue(reading.problem?.contains("writable by other users") == true, "mode \(String(mode, radix: 8))")
            XCTAssertTrue(reading.rows.isEmpty)
        }
        // Read-only for others is fine.
        XCTAssertTrue(try read(try fixture("hoghunter_mac_current", "json"), mode: 0o644).isCurrent)
    }

    func test_a_file_owned_by_someone_else_is_not_read() throws {
        var info = stat()
        info.st_mode = S_IFREG | 0o600
        info.st_uid = getuid() &+ 1
        XCTAssertEqual(HogHunterMacReportReader.trustProblem(info), "is not owned by this user")
        info.st_uid = getuid()
        XCTAssertNil(HogHunterMacReportReader.trustProblem(info))
        info.st_mode = S_IFDIR | 0o700
        XCTAssertEqual(HogHunterMacReportReader.trustProblem(info), "is not a regular file")
    }

    func test_the_owner_check_is_applied_to_a_real_file_with_another_expected_user() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-hh-mac-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("hoghunter-mac.json")
        try fixture("hoghunter_mac_current", "json").write(to: url, atomically: true, encoding: .utf8)
        chmod(url.path, 0o600)
        let reading = HogHunterMacReportReader.read(at: url, now: generated, uid: getuid() &+ 1)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertTrue(reading.problem?.contains("not owned by this user") == true)
    }

    func test_a_symlink_is_not_followed() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-hh-mac-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let real = directory.appendingPathComponent("real.json")
        try fixture("hoghunter_mac_current", "json").write(to: real, atomically: true, encoding: .utf8)
        chmod(real.path, 0o600)
        let link = directory.appendingPathComponent("hoghunter-mac.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real)
        let reading = HogHunterMacReportReader.read(at: link, now: generated)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertTrue(reading.problem?.contains("link") == true)
    }

    func test_an_oversized_file_is_not_read() throws {
        let big = "{\"schemaVersion\":1,\"tool\":\"hoghunter-mac-status\",\"pad\":\"" + String(repeating: "x", count: 300_000) + "\"}"
        let reading = try read(big)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertTrue(reading.problem?.contains("larger") == true)
    }

    // MARK: - No control

    func test_the_report_path_is_the_one_local_file() {
        let url = HogHunterMacReportReader.reportURL(home: URL(fileURLWithPath: "/Users/someone"))
        XCTAssertEqual(url.path, "/Users/someone/.local/state/fleet-tools/hoghunter-mac.json")
    }
}
