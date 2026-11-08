import XCTest

/// Process safety: a child that hangs, ignores SIGTERM, or floods a pipe must
/// never wedge the caller. Fake shell scripts only; no real engine is run.
final class ProcessSafetyTests: XCTestCase {
    private func makeScript(_ body: String) throws -> URL {
        let base = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-safety-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        let script = base.appendingPathComponent("fake.sh")
        try body.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return script
    }

    private func makeLocation(interpreter: URL) -> EngineLocation {
        EngineLocation(root: interpreter.deletingLastPathComponent(), interpreter: interpreter)
    }

    func test_engineRunner_timesOut_a_hanging_child() throws {
        let script = try makeScript("#!/bin/sh\nsleep 30\n")
        let runner = EngineRunner(location: makeLocation(interpreter: script), timeout: 0.5)
        let done = expectation(description: "run completes")
        var result: EngineRun?
        let start = Date()
        runner.run { run in result = run; done.fulfill() }
        wait(for: [done], timeout: 10)
        XCTAssertTrue(result?.timedOut == true)
        XCTAssertLessThan(Date().timeIntervalSince(start), 8)
    }

    func test_engineRunner_kills_a_child_that_ignores_SIGTERM() throws {
        let script = try makeScript("#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 1; done\n")
        let runner = EngineRunner(location: makeLocation(interpreter: script), timeout: 0.5)
        let done = expectation(description: "run completes")
        var result: EngineRun?
        let start = Date()
        runner.run { run in result = run; done.fulfill() }
        wait(for: [done], timeout: 12)
        XCTAssertTrue(result?.timedOut == true)
        XCTAssertLessThan(Date().timeIntervalSince(start), 10)
    }

    func test_engineRunner_drains_a_stderr_flood_without_deadlock() throws {
        let script = try makeScript("""
        #!/bin/sh
        i=0; while [ $i -lt 5000 ]; do echo "noise noise noise noise noise" >&2; i=$((i+1)); done
        echo "not an envelope"
        """)
        let runner = EngineRunner(location: makeLocation(interpreter: script), timeout: 20)
        let done = expectation(description: "run completes")
        var result: EngineRun?
        runner.run { run in result = run; done.fulfill() }
        wait(for: [done], timeout: 15)
        XCTAssertTrue(result?.timedOut == false)
        XCTAssertNotNil(result?.failure)
        XCTAssertLessThanOrEqual(result?.standardError.utf8.count ?? Int.max, EngineRunner.maximumOutputBytes + 64)
    }

    func test_fleetView_timesOut_a_hanging_child() throws {
        let script = try makeScript("#!/bin/sh\nsleep 30\n")
        let start = Date()
        XCTAssertThrowsError(try FleetViewService.fetch(
            executable: script, arguments: [], currentDirectory: nil,
            environment: [:], timeout: 0.5
        )) { error in
            guard case FleetViewService.ServiceError.timedOut = error else {
                return XCTFail("expected timedOut, got \(error)")
            }
        }
        XCTAssertLessThan(Date().timeIntervalSince(start), 8)
    }

    func test_fleetView_returns_when_a_grandchild_holds_the_pipe() throws {
        // The child exits at once but its background grandchild inherits
        // stdout, so EOF is 20s away; the bounded drain must not wait for it.
        let script = try makeScript("#!/bin/sh\n(sleep 20 &)\necho done\n")
        let start = Date()
        _ = try? FleetViewService.fetch(
            executable: script, arguments: [], currentDirectory: nil,
            environment: [:], timeout: 5
        )
        XCTAssertLessThan(Date().timeIntervalSince(start), 9)
    }

    func test_engineRunner_returns_when_a_grandchild_holds_the_pipe() throws {
        let script = try makeScript("#!/bin/sh\n(sleep 20 &)\necho done\n")
        let runner = EngineRunner(location: makeLocation(interpreter: script), timeout: 5)
        let done = expectation(description: "run completes")
        let start = Date()
        runner.run { _ in done.fulfill() }
        wait(for: [done], timeout: 10)
        XCTAssertLessThan(Date().timeIntervalSince(start), 9)
    }

    func test_fleetView_kills_a_child_that_ignores_SIGTERM() throws {
        let script = try makeScript("#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 1; done\n")
        let start = Date()
        XCTAssertThrowsError(try FleetViewService.fetch(
            executable: script, arguments: [], currentDirectory: nil,
            environment: [:], timeout: 0.5
        )) { error in
            guard case FleetViewService.ServiceError.timedOut = error else {
                return XCTFail("expected timedOut, got \(error)")
            }
        }
        XCTAssertLessThan(Date().timeIntervalSince(start), 10)
    }

    func test_fleetView_failure_carries_stderr_past_a_pipe_buffer() throws {
        let script = try makeScript("""
        #!/bin/sh
        i=0; while [ $i -lt 5000 ]; do echo "noise noise noise noise noise" >&2; i=$((i+1)); done
        echo "the real failure" >&2
        exit 3
        """)
        XCTAssertThrowsError(try FleetViewService.fetch(
            executable: script, arguments: [], currentDirectory: nil,
            environment: [:], timeout: 20
        )) { error in
            guard case FleetViewService.ServiceError.cliFailed(let text) = error else {
                return XCTFail("expected cliFailed, got \(error)")
            }
            XCTAssertTrue(text.contains("the real failure"), "stderr tail kept")
        }
    }

    // MARK: The knob reader and the simple-chat runner

    func test_knobStatus_kills_a_child_that_ignores_SIGTERM() async throws {
        // The child ignores SIGTERM and its own child holds the pipe open,  so only SIGKILL
        // and a bounded drain end the read.
        let script = try makeScript("#!/bin/sh\ntrap '' TERM\nsleep 15\n")
        let start = Date()
        let reading = await KnobStatusReader.read(location: makeLocation(interpreter: script), timeout: 1)
        guard case .failed(let reason) = reading else {
            return XCTFail("expected a failed reading, got \(reading)")
        }
        XCTAssertTrue(reason.contains("did not answer"), reason)
        XCTAssertLessThan(Date().timeIntervalSince(start), 10)
    }

    func test_knobStatus_reads_a_good_answer() async throws {
        let script = try makeScript("""
        #!/bin/sh
        echo '{"schema": 1, "fetch": "ok", "write_token": "missing", "knobs": []}'
        """)
        let reading = await KnobStatusReader.read(location: makeLocation(interpreter: script), timeout: 10)
        guard case .read(let status) = reading else {
            return XCTFail("expected a reading, got \(reading)")
        }
        XCTAssertTrue(status.fetchOK)
        XCTAssertEqual(status.writeToken, .missing)
    }

    func test_knobStatus_says_when_the_answer_was_too_large() {
        let outcome = FleetViewService.ProcessOutcome(
            status: 0, stdout: Data("{".utf8), stderr: Data(), timedOut: false, stdoutExceeded: true
        )
        guard case .failed(let reason) = KnobStatusReader.reading(from: .success(outcome), timeout: 40) else {
            return XCTFail("expected a failed reading")
        }
        XCTAssertTrue(reason.contains("larger than"), reason)
    }

    func test_knobStatus_says_when_the_engine_could_not_start() {
        let outcome: Result<FleetViewService.ProcessOutcome, Error> =
            .failure(FleetViewService.ServiceError.launchFailed("no such file"))
        guard case .failed(let reason) = KnobStatusReader.reading(from: outcome, timeout: 40) else {
            return XCTFail("expected a failed reading")
        }
        XCTAssertTrue(reason.contains("no such file"), reason)
    }

    func test_agentSync_kills_a_child_that_ignores_SIGTERM() async throws {
        let base = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-safety-\(UUID().uuidString)", isDirectory: true)
        let scripts = base.appendingPathComponent("scripts", isDirectory: true)
        try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
        let command = scripts.appendingPathComponent("simple-chat")
        try "#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 1; done\n"
            .write(to: command, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: command.path)
        let location = EngineLocation(root: base, interpreter: URL(fileURLWithPath: "/usr/bin/python3"))
        let runner = AgentSyncCLI(location: { location })

        let start = Date()
        let result = try await runner.run(AgentSyncInvocation(["status"], timeout: 1))

        XCTAssertTrue(result.timedOut)
        XCTAssertLessThan(Date().timeIntervalSince(start), 10)
    }

    func test_fleetView_decodes_a_valid_envelope() throws {
        let script = try makeScript("""
        #!/bin/sh
        cat <<'JSON'
        {"schemaVersion":1,"generatedAt":"2026-01-01T00:00:00Z","summary":{"lanes":0,"pullRequests":0,"claims":0,"overallVerdict":"OK"},"requestCounts":{"github":0,"linear":0,"githubMax":1,"linearMax":1},"sections":{"lanes":{"state":"OK","reason":"","items":[]},"pullRequests":{"state":"OK","reason":"","items":[]},"claims":{"state":"OK","reason":"","items":[]}},"notes":[]}
        JSON
        """)
        let envelope = try FleetViewService.fetch(
            executable: script, arguments: [], currentDirectory: nil,
            environment: [:], timeout: 10
        )
        XCTAssertEqual(envelope.schemaVersion, 1)
    }
}
