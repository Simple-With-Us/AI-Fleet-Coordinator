import XCTest

/// The server's status report,  and what Muster can say about it.
///
/// The rule under test is the doctor's rule:  a report that cannot be trusted reads
/// as unknown,  never as a pass.  A missing file,  a malformed file,  a version this
/// app does not read and a report past forty-five minutes all read as unknown,  and a
/// measurement the collector could not take is never shown as a zero.
final class ServerStatusReportTests: XCTestCase {
    /// A directory the test can write a report into.
    private func makeDirectory() throws -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-server-status-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    /// Write a report and read it back.
    private func read(_ json: String, now: Date = Date()) throws -> ServerStatusReading {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("server-status.json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return ServerStatusReportReader.read(at: url, now: now)
    }

    /// One Coolify resource.
    private func resource(
        uuid: String = "uuid1",
        name: String = "Example",
        type: String = "application",
        status: String = "running",
        cpu: String? = "12.3",
        memory: String? = "123456"
    ) -> String {
        var fields: [String] = [
            "\"uuid\": \"\(uuid)\"",
            "\"name\": \"\(name)\"",
            "\"type\": \"\(type)\"",
            "\"status\": \"\(status)\"",
        ]
        if let cpu = cpu { fields.append("\"cpuPercent\": \(cpu)") }
        if let memory = memory { fields.append("\"memoryBytes\": \(memory)") }
        return "{" + fields.joined(separator: ", ") + "}"
    }

    /// One process.
    private func process(
        pid: Int = 123,
        owner: String = "root",
        name: String = "next-server",
        cpu: String? = "5.1",
        memory: String? = "123456"
    ) -> String {
        var fields: [String] = [
            "\"pid\": \(pid)",
            "\"owner\": \"\(owner)\"",
            "\"name\": \"\(name)\"",
        ]
        if let cpu = cpu { fields.append("\"cpuPercent\": \(cpu)") }
        if let memory = memory { fields.append("\"memoryBytes\": \(memory)") }
        return "{" + fields.joined(separator: ", ") + "}"
    }

    /// One watched file.
    private func file(
        path: String = "/srv/example-code/example-repo/AGENTS.md",
        bytes: String? = "5290",
        sha: String? = "ab12",
        drift: String = "unchanged"
    ) -> String {
        var fields: [String] = [
            "\"path\": \"\(path)\"",
            "\"drift\": \"\(drift)\"",
        ]
        if let bytes = bytes { fields.append("\"bytes\": \(bytes)") }
        if let sha = sha { fields.append("\"sha256\": \"\(sha)\"") }
        return "{" + fields.joined(separator: ", ") + "}"
    }

    /// The report,  written at a time.
    private func report(
        at date: Date,
        schemaVersion: Int = 1,
        tool: String = "fleet-server-status",
        host: String = "fleet-hetzner-nbg1",
        coolify: [String] = [],
        processes: [String] = [],
        files: [String] = []
    ) -> String {
        let stamp = ISO8601DateFormatter().string(from: date)
        return """
        {"schemaVersion": \(schemaVersion), "tool": "\(tool)", "generatedAt": "\(stamp)", "host": "\(host)", "coolify": [\(coolify.joined(separator: ","))], "processes": [\(processes.joined(separator: ","))], "files": [\(files.joined(separator: ","))]}
        """
    }

    // MARK: - A report that can be believed

    /// A fresh report reads as the host it was taken on,  with its rows.
    func test_a_good_report_reads_the_host_and_its_rows() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-120),
            coolify: [resource()],
            processes: [process()],
            files: [file()]
        )
        let reading = try read(json, now: now)
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.host, "fleet-hetzner-nbg1")
        XCTAssertEqual(reading.coolify.count, 1)
        XCTAssertEqual(reading.processes.count, 1)
        XCTAssertEqual(reading.files.count, 1)
        XCTAssertEqual(reading.state, .passed)
        XCTAssertEqual(reading.age, "2m")
    }

    /// The resource carries its own state,  and its measurements when they were taken.
    func test_a_resource_carries_its_state_and_its_measurements() throws {
        let now = Date()
        let json = report(at: now, coolify: [resource()])
        let reading = try read(json, now: now)
        let coolify = reading.coolify.first
        XCTAssertEqual(coolify?.resourceType, .application)
        XCTAssertEqual(coolify?.resourceStatus, .running)
        XCTAssertEqual(coolify?.state, .passed)
        XCTAssertEqual(coolify?.cpuText, "12.3%")
        XCTAssertEqual(coolify?.memoryText, "120.6 KB")
    }

    /// A resource that exited reads as a failure,  and one nobody could tell reads as
    /// unproven rather than as a pass.
    func test_a_resource_that_exited_reads_as_a_failure() throws {
        let now = Date()
        let json = report(
            at: now,
            coolify: [resource(status: "exited"), resource(uuid: "uuid2", status: "wobble")]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.coolify.first?.state, .missing)
        XCTAssertEqual(reading.coolify.last?.state, .unproven)
        XCTAssertEqual(reading.state, .missing)
    }

    // MARK: - The ways a report cannot be believed

    /// A report that is not there is unknown,  never a pass.
    func test_a_missing_report_reads_as_unknown() throws {
        let directory = try makeDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let reading = ServerStatusReportReader.read(
            at: directory.appendingPathComponent("server-status.json")
        )
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.coolify.isEmpty)
        XCTAssertTrue(reading.processes.isEmpty)
        XCTAssertTrue(reading.files.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertFalse(reading.isCurrent)
    }

    /// A file that is not JSON is unknown,  never a pass.
    func test_a_malformed_report_reads_as_unknown() throws {
        let reading = try read("{ this is not a report")
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.coolify.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    /// A report of a version this app does not read is unknown,  never a pass.
    func test_a_wrong_schema_version_reads_as_unknown() throws {
        let now = Date()
        let json = report(at: now, schemaVersion: 2, coolify: [resource()])
        let reading = try read(json, now: now)
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(
            reading.problem?.contains("2") ?? false,
            "the reason names the version it found: \(reading.problem ?? "")"
        )
        XCTAssertTrue(reading.coolify.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    /// A report from another tool is not this report.
    func test_a_report_from_another_tool_reads_as_unknown() throws {
        let now = Date()
        let json = report(at: now, tool: "something-else", coolify: [resource()])
        let reading = try read(json, now: now)
        XCTAssertNotNil(reading.problem)
        XCTAssertTrue(reading.coolify.isEmpty)
        XCTAssertEqual(reading.state, .unknown)
    }

    /// A report past the collector's window is unknown,  never a pass.
    func test_a_stale_report_reads_as_unknown() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-46 * 60),
            coolify: [resource(status: "running")],
            files: [file(drift: "unchanged")]
        )
        let reading = try read(json, now: now)
        XCTAssertNil(reading.problem)
        XCTAssertFalse(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
        // The half that could not be measured is unproven,  not a pass.
        XCTAssertEqual(reading.coolifyState, .unproven)
    }

    /// A report inside the window is current.
    func test_a_report_inside_the_window_is_current() throws {
        let now = Date()
        let json = report(
            at: now.addingTimeInterval(-44 * 60),
            coolify: [resource()],
            processes: [process()],
            files: [file()]
        )
        let reading = try read(json, now: now)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.state, .passed)
    }

    /// A report with no time on it is not a reading.
    func test_a_report_with_no_time_reads_as_unknown() throws {
        let json = """
        {"schemaVersion": 1, "tool": "fleet-server-status", "coolify": [], "processes": [], "files": []}
        """
        let reading = try read(json)
        XCTAssertNotNil(reading.problem)
        XCTAssertEqual(reading.state, .unknown)
    }

    /// A report that proves nothing at all reads as unknown.
    func test_an_empty_report_reads_as_unknown() throws {
        let now = Date()
        let json = report(at: now)
        let reading = try read(json, now: now)
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.isCurrent)
        XCTAssertEqual(reading.state, .unknown)
        XCTAssertEqual(reading.coolifyState, .unproven)
        XCTAssertEqual(reading.processState, .unproven)
        XCTAssertEqual(reading.configState, .unproven)
    }

    // MARK: - The drift

    /// A file that changed shows as drift,  and the panel lists it.
    func test_a_changed_file_shows_as_drift() throws {
        let now = Date()
        let json = report(
            at: now,
            files: [
                file(path: "/srv/example-code/example-repo/AGENTS.md", drift: "changed"),
                file(path: "/srv/example-code/HogHunter/README.md", drift: "unchanged"),
            ]
        )
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.driftedFiles.count, 1)
        XCTAssertEqual(reading.driftedFiles.first?.path, "/srv/example-code/example-repo/AGENTS.md")
        XCTAssertEqual(reading.driftedFiles.first?.fileDrift, .changed)
        XCTAssertEqual(reading.driftedFiles.first?.state, .drift)
        XCTAssertEqual(reading.configState, .drift)
        XCTAssertEqual(reading.state, .drift)
    }

    /// A file that is gone reads as missing,  and not as a zero byte file.
    func test_a_file_that_is_gone_reads_as_missing() throws {
        let now = Date()
        let json = report(
            at: now,
            files: [file(path: "/srv/example-code/example-repo/AGENTS.md", bytes: nil, sha: nil, drift: "missing")]
        )
        let reading = try read(json, now: now)
        let gone = reading.files.first
        XCTAssertEqual(gone?.fileDrift, .missing)
        XCTAssertEqual(gone?.state, .missing)
        XCTAssertNil(gone?.sizeText)
        XCTAssertEqual(reading.state, .missing)
    }

    /// A drift this app does not know is unproven,  never unchanged.
    func test_a_drift_the_app_does_not_know_is_not_unchanged() throws {
        let now = Date()
        let json = report(at: now, files: [file(drift: "wobble")])
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.files.first?.fileDrift, .unknown)
        XCTAssertEqual(reading.files.first?.state, .unproven)
        XCTAssertEqual(reading.configState, .unproven)
    }

    // MARK: - The measurements that were not taken

    /// A measurement the collector could not take is not shown as a zero.
    func test_a_measurement_that_was_not_taken_is_not_a_zero() throws {
        let now = Date()
        let json = report(
            at: now,
            coolify: [resource(cpu: nil, memory: nil)],
            processes: [process(cpu: nil, memory: nil)]
        )
        let reading = try read(json, now: now)
        XCTAssertNil(reading.coolify.first?.cpuText)
        XCTAssertNil(reading.coolify.first?.memoryText)
        XCTAssertNil(reading.processes.first?.cpuText)
        XCTAssertNil(reading.processes.first?.memoryText)
    }

    /// A process the collector could not name is still listed by its pid.
    func test_a_process_without_a_name_is_listed_by_its_pid() throws {
        let now = Date()
        let json = report(at: now, processes: [process(name: "")])
        let reading = try read(json, now: now)
        XCTAssertEqual(reading.processes.first?.displayName, "pid 123")
    }

    // MARK: - The contract

    /// The window and the version are the collector's own.
    func test_the_contract_is_the_collectors_own() {
        XCTAssertEqual(ServerStatusContract.maxAgeSeconds, 45 * 60)
        XCTAssertEqual(ServerStatusContract.supportedSchemaVersion, 1)
        XCTAssertEqual(ServerStatusContract.expectedTool, "fleet-server-status")
    }

    /// The report the collector writes lands where the panel reads it.
    func test_the_report_is_read_from_the_state_directory() {
        let home = URL(fileURLWithPath: "/Users/somebody")
        let url = ServerStatusReportReader.reportURL(home: home)
        XCTAssertEqual(
            url.path,
            "/Users/somebody/.local/state/fleet-tools/server-status.json"
        )
    }

    /// The sizes the panel shows are readable.
    func test_the_sizes_are_readable() {
        XCTAssertEqual(ServerStatusFormat.bytes(5290), "5.2 KB")
        XCTAssertEqual(ServerStatusFormat.bytes(123456), "120.6 KB")
        XCTAssertEqual(ServerStatusFormat.bytes(900), "900 B")
    }
}
