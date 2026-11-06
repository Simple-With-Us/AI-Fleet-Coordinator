import XCTest

private struct InventoryRunner: OpsRunning {
    let result: OpsProcessResult
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult { result }
}

final class SeatInventoryTests: XCTestCase {
    private let sample = """
    {"schemaVersion": 1, "tool": "fleet-seat-inventory", "ok": true, "host": "mac", "platform": "claude",
     "generatedAt": "2026-11-06T21:56:04Z", "detail": "",
     "sections": [
       {"title": "Hooks", "source": "~/.claude/settings.json", "note": null,
        "items": [{"name": "PreToolUse", "detail": "2 entries"}, {"name": "Stop", "detail": "1 entry"}]},
       {"title": "Sub-Agents", "source": "~/.claude/agents", "items": [], "note": "The folder is not on this host."},
       {"title": "Skills", "source": "~/.claude/skills", "note": "1 backup copy is not listed.",
        "items": [{"name": "board-ops", "detail": null}]}
     ]}
    """

    private func data(_ text: String) -> Data { Data(text.utf8) }

    // MARK: - the answer

    func test_a_good_answer_loads_with_its_sections() throws {
        guard case .loaded(let inventory) = SeatInventoryDecoder.decode(data(sample)) else {
            return XCTFail("the sample did not load")
        }
        XCTAssertEqual(inventory.platform, "claude")
        XCTAssertEqual(inventory.host, "mac")
        XCTAssertEqual(inventory.sections.map(\.title), ["Hooks", "Sub-Agents", "Skills"])
        XCTAssertEqual(inventory.sections[0].items.map(\.name), ["PreToolUse", "Stop"])
        XCTAssertEqual(inventory.sections[0].items.first?.detail, "2 entries")
        XCTAssertNil(inventory.sections[2].items.first?.detail)
    }

    func test_an_answer_that_is_not_this_tools_or_not_this_version_is_refused() {
        let otherTool = sample.replacingOccurrences(of: "fleet-seat-inventory", with: "something-else")
        XCTAssertEqual(
            SeatInventoryDecoder.decode(data(otherTool)),
            .unavailable("The answer is from something-else, not the seat inventory.")
        )
        let newer = sample.replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 2")
        XCTAssertEqual(
            SeatInventoryDecoder.decode(data(newer)),
            .unavailable("The answer is schema version 2, which this app does not read.")
        )
    }

    func test_an_answer_that_says_it_is_not_ok_gives_its_own_reason() {
        let refused = sample
            .replacingOccurrences(of: "\"ok\": true", with: "\"ok\": false")
            .replacingOccurrences(of: "\"detail\": \"\"", with: "\"detail\": \"This reader has no sources for that platform.\"")
        XCTAssertEqual(
            SeatInventoryDecoder.decode(data(refused)),
            .unavailable("This reader has no sources for that platform.")
        )
        let silent = sample.replacingOccurrences(of: "\"ok\": true", with: "\"ok\": false")
        XCTAssertEqual(
            SeatInventoryDecoder.decode(data(silent)),
            .unavailable("The reader could not answer for this platform.")
        )
    }

    func test_text_that_is_not_an_inventory_is_never_drawn_as_one() {
        for text in ["", "not json", "{}", "[1, 2]", "{\"tool\": \"fleet-seat-inventory\"}"] {
            XCTAssertEqual(
                SeatInventoryDecoder.decode(data(text)),
                .unavailable("The answer could not be read as a seat inventory."),
                text
            )
        }
    }

    func test_only_a_plain_platform_id_is_accepted() {
        for good in ["claude", "muse-code", "zcode", "a1"] {
            XCTAssertTrue(SeatInventoryDecoder.isPlatformId(good), good)
        }
        for bad in ["", "Claude", "../etc", "a b", "-x", "1a", "claude;ls", String(repeating: "a", count: 26)] {
            XCTAssertFalse(SeatInventoryDecoder.isPlatformId(bad), bad)
        }
    }

    // MARK: - one line per section

    func test_a_section_says_how_many_it_lists_and_any_note() throws {
        guard case .loaded(let inventory) = SeatInventoryDecoder.decode(data(sample)) else {
            return XCTFail("the sample did not load")
        }
        XCTAssertEqual(inventory.sections[0].summary, "2 listed")
        // An empty section gives its reason and never reads as a list of none.
        XCTAssertEqual(inventory.sections[1].summary, "The folder is not on this host.")
        XCTAssertEqual(inventory.sections[2].summary, "1 listed.\(ServerStatusCopy.sentenceGap)1 backup copy is not listed.")
    }

    func test_an_empty_section_with_no_note_still_says_nothing_was_listed() {
        let section = SeatInventory.Section(title: "T", source: "~/x", items: [], note: nil)
        XCTAssertEqual(section.summary, "Nothing listed.")
    }

    // MARK: - rows with the same name

    /// Two entries in one section can share a name (a skill folder and a `.md` file of the same
    /// stem both read as one name), so the rows are told apart by position and not by name.
    func test_rows_are_told_apart_by_position_because_names_can_repeat() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(
            contentsOf: root.appendingPathComponent("Muster/Views/SeatSetupView.swift"), encoding: .utf8
        )
        XCTAssertTrue(source.contains("ForEach(Array(section.items.enumerated()), id: \\.offset)"))
        XCTAssertFalse(source.contains("ForEach(section.items)"))
    }

    // MARK: - reading on the Mac

    private func withReader(_ result: OpsProcessResult?, _ body: (MacSeatInventoryReader) async throws -> Void) async throws {
        let root = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
        let scripts = root.appendingPathComponent("scripts")
        try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: root.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: scripts.path)
        defer { try? FileManager.default.removeItem(at: root) }
        let script = scripts.appendingPathComponent("fleet_seat_inventory.py")
        try Data("# test fixture\n".utf8).write(to: script)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: script.path)
        var reader = MacSeatInventoryReader(repoRoot: root, home: root.appendingPathComponent("home"))
        if let result { reader.runner = InventoryRunner(result: result) }
        try await body(reader)
    }

    func test_the_reader_runs_the_checkouts_script_isolated_with_one_platform() async throws {
        try await withReader(nil) { reader in
            let invocation = try reader.invocation(platform: "codex")
            XCTAssertEqual(invocation.executable, "/usr/bin/python3")
            XCTAssertEqual(invocation.arguments, [
                "-I", URL(fileURLWithPath: invocation.workingDirectory).appendingPathComponent("scripts/fleet_seat_inventory.py").path,
                "--platform", "codex", "--home", reader.home.path, "--json",
            ])
            XCTAssertEqual(invocation.environment, ["HOME": reader.home.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"])
        }
    }

    func test_the_reader_refuses_a_platform_name_that_is_not_plain_before_launching() async throws {
        try await withReader(nil) { reader in
            XCTAssertThrowsError(try reader.invocation(platform: "../../bin/sh"))
            XCTAssertThrowsError(try reader.invocation(platform: "claude; rm"))
        }
    }

    func test_the_reader_refuses_a_missing_or_writable_script() async throws {
        let missing = MacSeatInventoryReader(repoRoot: URL(fileURLWithPath: "/nonexistent-\(UUID().uuidString)"))
        XCTAssertThrowsError(try missing.invocation(platform: "claude"))
        try await withReader(nil) { reader in
            let script = reader.repoRoot.appendingPathComponent("scripts/fleet_seat_inventory.py")
            try FileManager.default.setAttributes([.posixPermissions: 0o666], ofItemAtPath: script.path)
            XCTAssertThrowsError(try reader.invocation(platform: "claude"))
        }
    }

    func test_a_good_run_loads_and_each_kind_of_failure_says_why() async throws {
        try await withReader(OpsProcessResult(exitCode: 0, stdout: sample, stderr: "")) { reader in
            guard case .loaded(let inventory) = await reader.read(platform: "claude") else {
                return XCTFail("a good run did not load")
            }
            XCTAssertEqual(inventory.sections.count, 3)
        }
        try await withReader(OpsProcessResult(exitCode: 0, stdout: "", stderr: "", timedOut: true)) { reader in
            let reading = await reader.read(platform: "claude")
            XCTAssertEqual(reading, .unavailable("The seat inventory reader timed out."))
        }
        try await withReader(OpsProcessResult(exitCode: 1, stdout: "", stderr: "boom")) { reader in
            let reading = await reader.read(platform: "claude")
            XCTAssertEqual(reading, .unavailable("The seat inventory reader failed (exit 1)."))
        }
        let refusal = "{\"ok\": false, \"detail\": \"This reader has no sources for that platform.\"}"
        try await withReader(OpsProcessResult(exitCode: 2, stdout: refusal, stderr: "")) { reader in
            let reading = await reader.read(platform: "nope")
            XCTAssertEqual(reading, .unavailable("This reader has no sources for that platform."))
        }
        try await withReader(OpsProcessResult(exitCode: 0, stdout: "garbage", stderr: "")) { reader in
            let reading = await reader.read(platform: "claude")
            XCTAssertEqual(reading, .unavailable("The answer could not be read as a seat inventory."))
        }
    }
}
