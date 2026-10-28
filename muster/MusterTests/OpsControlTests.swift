import XCTest

// The operations control:  the argv Muster builds for each action,  how it reads a
// helper's answer,  and the plan,  confirm and run flow with an injected runner.
//
// No test here starts a process:  the runner is a fake,  and the builder is only asked for
// the invocation it would run.

private let exampleRoot = URL(fileURLWithPath: "/tmp/example-repo", isDirectory: true)
private let exampleHome = URL(fileURLWithPath: "/Users/example", isDirectory: true)

private func exampleBuilder(socket: String? = nil) -> OpsCommandBuilder {
    OpsCommandBuilder(repoRoot: exampleRoot, home: exampleHome, sshAuthSocket: socket)
}

// MARK: - Building the argv

final class OpsCommandBuilderTests: XCTestCase {
    private let sshPrefix = [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
        "fleet-ops-ctl", "--",
    ]

    func test_a_mac_verb_runs_the_helper_with_the_pinned_interpreter() throws {
        let invocation = try exampleBuilder().invocation(
            for: .macAgent(verb: .restart, label: "com.jay.fleet-tools-doctor"), mode: .plan
        )
        XCTAssertEqual(invocation.executable, "/usr/bin/python3")
        XCTAssertEqual(invocation.arguments, [
            "-s", "/tmp/example-repo/scripts/fleet-ops-control.py",
            "mac", "restart", "com.jay.fleet-tools-doctor", "--plan", "--json",
        ])
        XCTAssertEqual(invocation.workingDirectory, "/")
        XCTAssertEqual(invocation.timeout, OpsControlRoute.planTimeout)
    }

    func test_a_server_verb_goes_over_ssh_to_the_alias() throws {
        let invocation = try exampleBuilder().invocation(
            for: .serverUnit(verb: .start, unit: "fleet-server-status.timer"), mode: .yes
        )
        XCTAssertEqual(invocation.executable, "/usr/bin/ssh")
        XCTAssertEqual(
            invocation.arguments,
            sshPrefix + ["server", "start", "fleet-server-status.timer", "--yes", "--json"]
        )
        XCTAssertEqual(invocation.timeout, OpsControlRoute.runTimeout)
        XCTAssertEqual(Set(invocation.environment.keys), ["PATH", "HOME", "LANG"])
        XCTAssertEqual(invocation.environment["HOME"], "/Users/example")
    }

    func test_ssh_carries_the_agent_socket_when_there_is_one() throws {
        let invocation = try exampleBuilder(socket: "/private/tmp/agent.sock").invocation(
            for: .serverUpdate(name: "fleet-cli"), mode: .plan
        )
        XCTAssertEqual(invocation.environment["SSH_AUTH_SOCK"], "/private/tmp/agent.sock")
        XCTAssertEqual(invocation.arguments, sshPrefix + ["server", "update", "fleet-cli", "--plan", "--json"])
    }

    func test_a_redeploy_names_the_resource_by_its_uuid() throws {
        let invocation = try exampleBuilder().invocation(
            for: .coolifyRedeploy(uuid: "exampleuuid1", name: "example-app"), mode: .plan
        )
        XCTAssertEqual(invocation.arguments, sshPrefix + ["server", "redeploy", "exampleuuid1", "--plan", "--json"])
    }

    func test_a_knob_write_runs_the_knob_engine_from_the_scripts_directory() throws {
        let invocation = try exampleBuilder().invocation(
            for: .knobSet(key: "FLEET_AUTOPUSH_MODE", value: "report"), mode: .plan
        )
        XCTAssertEqual(invocation.executable, "/usr/bin/python3")
        XCTAssertEqual(invocation.arguments, [
            "-s", "-m", "fleet_knobs", "set", "FLEET_AUTOPUSH_MODE", "report", "--plan", "--json",
        ])
        XCTAssertEqual(invocation.workingDirectory, "/tmp/example-repo/scripts")
        XCTAssertEqual(invocation.environment["PYTHONPATH"], "/tmp/example-repo/scripts")
        XCTAssertEqual(invocation.environment["PATH"]?.contains("/opt/homebrew/bin"), true)
        // The environment is built,  never inherited:  a write token in the app's own
        // environment does not reach the child.
        XCTAssertNil(invocation.environment["FLEET_KNOBS_WRITE_TOKEN"])
        XCTAssertNil(invocation.environment["INFISICAL_TOKEN"])
    }

    func test_an_empty_knob_value_is_passed_as_one_empty_argument() throws {
        let invocation = try exampleBuilder().invocation(
            for: .knobSet(key: "FLEET_AUTOPUSH_REPOS", value: ""), mode: .yes
        )
        XCTAssertEqual(invocation.arguments, [
            "-s", "-m", "fleet_knobs", "set", "FLEET_AUTOPUSH_REPOS", "", "--yes", "--json",
        ])
    }

    func test_a_value_that_could_be_a_flag_or_a_shell_word_is_refused() {
        let refused: [(OpsAction, OpsControlError)] = [
            (.macAgent(verb: .start, label: "-oProxyCommand=x"), .invalidArgument("agent label")),
            (.macAgent(verb: .start, label: "../com.jay.x"), .invalidArgument("agent label")),
            (.serverUnit(verb: .stop, unit: "a b.service"), .invalidArgument("unit name")),
            (.serverUnit(verb: .stop, unit: "a;reboot"), .invalidArgument("unit name")),
            (.serverUnit(verb: .stop, unit: ""), .invalidArgument("unit name")),
            (.serverUpdate(name: "fleet-cli$(id)"), .invalidArgument("update name")),
            (.coolifyRedeploy(uuid: "x@y", name: "x"), .invalidArgument("resource id")),
            (.knobSet(key: "lower_case", value: "x"), .invalidArgument("knob name")),
            (.knobSet(key: "FLEET_X", value: "-rf"), .invalidArgument("knob value")),
            (.knobSet(key: "FLEET_X", value: "two\nlines"), .invalidArgument("knob value")),
            (.knobSet(key: "FLEET_X", value: String(repeating: "a", count: 1025)), .invalidArgument("knob value")),
        ]
        for (action, expected) in refused {
            XCTAssertThrowsError(try exampleBuilder().invocation(for: action, mode: .plan), "\(action)") { error in
                XCTAssertEqual(error as? OpsControlError, expected, "\(action)")
            }
        }
    }

    func test_a_negative_number_is_a_value_and_not_a_flag() throws {
        XCTAssertTrue(OpsCommandBuilder.isSafeKnobValue("-1"))
        XCTAssertNoThrow(try exampleBuilder().invocation(for: .knobSet(key: "FLEET_X", value: "-1"), mode: .plan))
    }

    func test_a_templated_unit_name_is_a_token() {
        XCTAssertTrue(OpsCommandBuilder.isSafeToken("fleet-worker@CLAUDE.service"))
        XCTAssertTrue(OpsCommandBuilder.isSafeToken("com.jay.fleet-tools-doctor"))
        XCTAssertFalse(OpsCommandBuilder.isSafeToken("ünit.service"))
    }

    func test_a_local_action_needs_its_helper_and_a_server_action_does_not() {
        let builder = OpsCommandBuilder(
            repoRoot: URL(fileURLWithPath: "/nonexistent-\(UUID().uuidString)", isDirectory: true)
        )
        XCTAssertThrowsError(try builder.checkInstalled(for: .macAgent(verb: .start, label: "com.jay.x"))) { error in
            guard case .helperNotFound(_)? = error as? OpsControlError else {
                return XCTFail("\(error)")
            }
        }
        XCTAssertThrowsError(try builder.checkInstalled(for: .knobSet(key: "FLEET_X", value: "1")))
        XCTAssertNoThrow(try builder.checkInstalled(for: .serverUpdate(name: "fleet-cli")))
    }

    func test_the_audit_names_of_an_action() {
        let redeploy = OpsAction.coolifyRedeploy(uuid: "exampleuuid1", name: "example-app")
        XCTAssertTrue(redeploy.runsOnServer)
        XCTAssertEqual(redeploy.auditVerb, "server redeploy")
        // The Mac's audit row names a resource by its name,  not its uuid.
        XCTAssertEqual(redeploy.auditTarget, "example-app")
        XCTAssertFalse(OpsAction.knobSet(key: "FLEET_X", value: "1").runsOnServer)
        XCTAssertEqual(OpsAction.macAgent(verb: .stop, label: "com.jay.x").auditVerb, "mac stop")
    }
}

// MARK: - Reading the answer

final class OpsResultParserTests: XCTestCase {
    private let unit = OpsAction.serverUnit(verb: .restart, unit: "fleet-server-status.timer")

    private func result(_ stdout: String, exit: Int32 = 0, stderr: String = "", timedOut: Bool = false) -> OpsProcessResult {
        OpsProcessResult(exitCode: exit, stdout: stdout, stderr: stderr, timedOut: timedOut)
    }

    func test_a_plan_reads_as_planned() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_planned", "json")), mode: .plan, action: unit, timeout: 90
        )
        XCTAssertEqual(outcome.kind, .planned)
        XCTAssertTrue(outcome.ok)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertFalse(outcome.isSuccess(for: .yes))
        XCTAssertEqual(outcome.commandLines, ["systemctl --user restart fleet-server-status.timer"])
        XCTAssertEqual(outcome.detail, "Nothing has run.")
    }

    func test_a_run_reads_as_done() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_done", "json")), mode: .yes, action: unit, timeout: 900
        )
        XCTAssertEqual(outcome.kind, .done)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(outcome.detail, "Done.")
    }

    func test_a_plan_that_answers_done_is_a_failure() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_done", "json")), mode: .plan, action: unit, timeout: 90
        )
        XCTAssertEqual(outcome.kind, .failed)
        XCTAssertFalse(outcome.isSuccess(for: .plan))
        XCTAssertTrue(outcome.detail.contains("\"done\""))
    }

    func test_a_refusal_reads_as_refused_with_the_helpers_words() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_refused", "json"), exit: 2), mode: .plan, action: unit, timeout: 90
        )
        XCTAssertEqual(outcome.kind, .refused)
        XCTAssertFalse(outcome.isSuccess(for: .plan))
        XCTAssertEqual(outcome.detail, "sshd.service is not in the allowlist.")
    }

    func test_an_ok_answer_with_a_failing_exit_is_not_a_success() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_done", "json"), exit: 1), mode: .yes, action: unit, timeout: 900
        )
        XCTAssertFalse(outcome.isSuccess(for: .yes))
    }

    func test_the_answer_is_found_after_other_output() throws {
        let planned = try fixture("ops_result_planned", "json")
        let stdout = "a warning line\n" + planned
        let outcome = OpsResultParser.parse(result(stdout), mode: .plan, action: unit, timeout: 90)
        XCTAssertEqual(outcome.kind, .planned)
    }

    func test_output_that_is_not_an_answer_is_a_failure() {
        let outcome = OpsResultParser.parse(
            result("Traceback (most recent call last):", exit: 1, stderr: "ValueError: boom"),
            mode: .plan,
            action: .macAgent(verb: .start, label: "com.jay.x"),
            timeout: 90
        )
        XCTAssertEqual(outcome.kind, .failed)
        XCTAssertFalse(outcome.ok)
        XCTAssertTrue(outcome.detail.contains("exit 1"))
        XCTAssertTrue(outcome.detail.contains("ValueError: boom"))
    }

    func test_an_answer_without_ok_is_not_ok() {
        let outcome = OpsResultParser.parse(
            result(#"{"result": "planned", "commands": [["true"]]}"#), mode: .plan, action: unit, timeout: 90
        )
        XCTAssertFalse(outcome.ok)
        XCTAssertFalse(outcome.isSuccess(for: .plan))
    }

    func test_a_run_that_timed_out_is_a_failure() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_planned", "json"), exit: 15, timedOut: true),
            mode: .plan, action: unit, timeout: 90
        )
        XCTAssertEqual(outcome.kind, .failed)
        XCTAssertTrue(outcome.detail.contains("90 seconds"))
    }

    func test_a_knob_plan_carries_the_old_and_the_new_value() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_knob_planned", "json")),
            mode: .plan, action: .knobSet(key: "FLEET_AUTOPUSH_MODE", value: "report"), timeout: 60
        )
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        let change = try XCTUnwrap(outcome.knobChange)
        XCTAssertEqual(change.old, "off")
        XCTAssertEqual(change.oldSource, "infisical")
        XCTAssertEqual(change.new, "report")
        XCTAssertEqual(change.oldText, "\"off\"")
        XCTAssertEqual(change.newText, "\"report\"")
        XCTAssertNil(change.lkgWarning)
    }

    func test_a_knob_whose_old_value_could_not_be_read_says_so() {
        let json = #"{"ok": true, "commands": [["infisical"]], "result": "planned", "old": null, "old_source": "unknown", "new": "report"}"#
        let outcome = OpsResultParser.parse(
            result(json), mode: .plan, action: .knobSet(key: "FLEET_AUTOPUSH_MODE", value: "report"), timeout: 60
        )
        let change = outcome.knobChange
        XCTAssertEqual(change?.oldText, "unknown")
        XCTAssertTrue(change?.sourceText.contains("could not be read") == true)
        let unset = OpsKnobChange(old: nil, oldSource: "default", new: "x", lkg: nil)
        XCTAssertEqual(unset.oldText, "unset")
    }

    func test_a_knob_write_that_left_the_copy_behind_warns() {
        let change = OpsKnobChange(old: "off", oldSource: "infisical", new: "report", lkg: "not-refreshed:missing")
        XCTAssertTrue(change.lkgWarning?.contains("(missing)") == true)
        XCTAssertNil(OpsKnobChange(old: nil, oldSource: nil, new: nil, lkg: "patched").lkgWarning)
    }

    func test_a_server_answer_has_no_knob_change() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_planned", "json")), mode: .plan, action: unit, timeout: 90
        )
        XCTAssertNil(outcome.knobChange)
    }

    func test_ssh_errors_are_put_in_fixed_words() {
        let unresolved = OpsResultParser.parse(
            result("", exit: 255, stderr: "ssh: Could not resolve hostname fleet-ops-ctl: nodename nor servname provided"),
            mode: .plan, action: unit, timeout: 90
        )
        XCTAssertEqual(unresolved.kind, .failed)
        XCTAssertTrue(unresolved.detail.contains("~/.ssh/config"))
        XCTAssertFalse(unresolved.detail.contains("nodename"))

        let refused = OpsResultParser.parse(
            result("", exit: 255, stderr: "someone@server.example: Permission denied (publickey)."),
            mode: .plan, action: unit, timeout: 90
        )
        XCTAssertTrue(refused.detail.contains("refused the fleet-ops-ctl key"))
        // The server's address in ssh's own message never reaches the screen.
        XCTAssertFalse(refused.detail.contains("server.example"))
    }
}

// MARK: - The allowlist and the audit log

final class OpsAllowlistAndAuditTests: XCTestCase {
    func test_the_allowlist_names_the_buttons() throws {
        let list = try XCTUnwrap(OpsAllowlist.parse(Data(try fixture("ops_allowlist", "json").utf8)))
        XCTAssertEqual(list.verbs(forMacAgent: "com.jay.fleet-tools-doctor"), [.start, .stop, .restart])
        XCTAssertEqual(list.verbs(forServerUnit: "fleet-server-status.timer"), [.restart])
        XCTAssertEqual(list.verbs(forServerUnit: "sshd.service"), [])
        XCTAssertEqual(list.updates.map(\.name), ["fleet-cli"])
        XCTAssertEqual(list.updates.first?.unit, "fleet-cli-git-update.service")
        XCTAssertTrue(list.coolifyRedeploy)
    }

    func test_an_allowlist_of_another_version_or_shape_is_not_read() throws {
        let other = try fixture("ops_allowlist", "json")
            .replacingOccurrences(of: "\"schemaVersion\": 1", with: "\"schemaVersion\": 2")
        XCTAssertNil(OpsAllowlist.parse(Data(other.utf8)))
        XCTAssertNil(OpsAllowlist.parse(Data("[]".utf8)))
        XCTAssertNil(OpsAllowlist.read(at: URL(fileURLWithPath: "/nonexistent-\(UUID().uuidString).json")))
    }

    func test_the_audit_log_reads_newest_first_and_counts_bad_lines() throws {
        let reading = OpsAuditLog.parse(try fixture("ops_audit_rows", "txt"))
        XCTAssertEqual(reading.entries.count, 3)
        XCTAssertEqual(reading.unreadable, 1)
        XCTAssertEqual(reading.entries.map { $0.target ?? "" }, [
            "fleet-cli", "fleet-server-status.timer", "com.jay.fleet-tools-doctor",
        ])
        XCTAssertEqual(reading.entries[0].state, .missing)
        XCTAssertEqual(reading.entries[1].state, .drift)
        XCTAssertEqual(reading.entries[1].via, "muster")
        XCTAssertEqual(reading.entries[2].state, .passed)
        XCTAssertNotNil(reading.entries[2].date)
        XCTAssertEqual(Set(reading.entries.map(\.id)).count, 3)
    }

    func test_the_audit_log_keeps_only_the_last_rows() throws {
        let reading = OpsAuditLog.parse(try fixture("ops_audit_rows", "txt"), limit: 2)
        XCTAssertEqual(reading.entries.map { $0.target ?? "" }, ["fleet-cli", "fleet-server-status.timer"])
    }

    /// A log of rows `length` bytes each,  newline included,  that is larger than the tail.
    private func writeLargeLog(rowLength length: Int, rows: Int) throws -> URL {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-audit-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let head = #"{"host":"server","result":"done","target":""#
        let rest = #"","time":"2026-10-28T15:00:00Z","verb":"server restart","via":"muster"}"#
        let pad = length - head.utf8.count - rest.utf8.count - 1
        XCTAssertGreaterThan(pad, 0)
        let line = head + String(repeating: "a", count: pad) + rest + "\n"
        XCTAssertEqual(line.utf8.count, length)
        try String(repeating: line, count: rows).write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    func test_a_large_log_does_not_count_its_cut_off_first_row() throws {
        let url = try writeLargeLog(rowLength: 300, rows: 1200)
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        let reading = OpsAuditLog.read(at: url, limit: 10_000)
        XCTAssertNil(reading.problem)
        XCTAssertEqual(reading.unreadable, 0)
        // Every whole row of the tail is read,  and the cut-off one is not.
        XCTAssertEqual(reading.entries.count, OpsAuditLog.maximumTailBytes / 300)
    }

    func test_a_tail_that_starts_exactly_at_a_row_keeps_that_row() throws {
        // 256 KiB is a whole number of 256-byte rows,  so the tail starts on a row.
        let url = try writeLargeLog(rowLength: 256, rows: 1100)
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        let reading = OpsAuditLog.read(at: url, limit: 10_000)
        XCTAssertEqual(reading.unreadable, 0)
        XCTAssertEqual(reading.entries.count, OpsAuditLog.maximumTailBytes / 256)
    }

    func test_a_log_that_is_not_there_is_empty_and_not_a_problem() {
        let reading = OpsAuditLog.read(at: URL(fileURLWithPath: "/nonexistent-\(UUID().uuidString).jsonl"))
        XCTAssertNil(reading.problem)
        XCTAssertTrue(reading.entries.isEmpty)
    }

    func test_a_row_is_appended_privately_and_reads_back() throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-audit-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("fleet-tools").appendingPathComponent("ops-audit.jsonl")
        let now = FleetTime.parse("2026-10-28T15:00:00Z")!
        let action = OpsAction.serverUnit(verb: .restart, unit: "fleet-server-status.timer")
        try OpsAuditLog.append(OpsAuditLog.entry(for: action, result: "done", now: now), to: url)
        try OpsAuditLog.append(OpsAuditLog.entry(for: action, result: "failed", now: now), to: url)

        let reading = OpsAuditLog.read(at: url)
        XCTAssertEqual(reading.entries.count, 2)
        XCTAssertEqual(reading.entries.first?.result, "failed")
        XCTAssertEqual(reading.entries.first?.host, "server")
        XCTAssertEqual(reading.entries.first?.verb, "server restart")
        XCTAssertEqual(reading.entries.first?.via, "muster")
        XCTAssertEqual(reading.entries.first?.time, "2026-10-28T15:00:00Z")

        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        let mode = (attributes[.posixPermissions] as? NSNumber)?.int16Value ?? 0
        XCTAssertEqual(mode & 0o777, 0o600)
    }
}

// MARK: - The flow

private final class FakeOpsRunner: OpsRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [OpsInvocation] = []
    private let handler: (OpsInvocation, Int) -> OpsProcessResult

    init(handler: @escaping (OpsInvocation, Int) -> OpsProcessResult) { self.handler = handler }

    var recorded: [[String]] { lock.lock(); defer { lock.unlock() }; return calls.map(\.arguments) }

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        lock.lock()
        calls.append(invocation)
        let count = calls.count
        lock.unlock()
        return handler(invocation, count)
    }
}

/// Answers plans with a fixture,  and throws from the `throwFrom`th call on.
private final class ThrowingOpsRunner: OpsRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [OpsInvocation] = []
    private let planned: String
    private let throwFrom: Int
    private let error: OpsControlError

    init(planned: String, throwFrom: Int, error: OpsControlError) {
        self.planned = planned
        self.throwFrom = throwFrom
        self.error = error
    }

    var recorded: [[String]] { lock.lock(); defer { lock.unlock() }; return calls.map(\.arguments) }

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        lock.lock()
        calls.append(invocation)
        let count = calls.count
        lock.unlock()
        if count >= throwFrom { throw error }
        return OpsProcessResult(exitCode: 0, stdout: planned, stderr: "")
    }
}

private func isReview(_ phase: OpsController.Phase) -> Bool {
    if case .review = phase { return true }
    return false
}

private func isFinished(_ phase: OpsController.Phase) -> Bool {
    if case .finished = phase { return true }
    return false
}

@MainActor
final class OpsControllerFlowTests: XCTestCase {
    private let action = OpsAction.serverUnit(verb: .restart, unit: "fleet-server-status.timer")

    private func controller(_ runner: FakeOpsRunner, auditURL: URL? = nil) -> OpsController {
        OpsController(
            runner: runner,
            makeBuilder: { exampleBuilder() },
            auditURL: auditURL,
            checksInstall: false
        )
    }

    /// A runner that plans with one fixture and runs with another.
    private func runner(planAfterReview: String? = nil) throws -> FakeOpsRunner {
        let planned = try fixture("ops_result_planned", "json")
        let done = try fixture("ops_result_done", "json")
        let lock = NSLock()
        var plans = 0
        return FakeOpsRunner { invocation, _ in
            if invocation.arguments.contains("--plan") {
                lock.lock(); plans += 1; let count = plans; lock.unlock()
                let text = (count > 1 ? planAfterReview : nil) ?? planned
                return OpsProcessResult(exitCode: 0, stdout: text, stderr: "")
            }
            return OpsProcessResult(exitCode: 0, stdout: done, stderr: "")
        }
    }

    private func waitFor(
        _ controller: OpsController,
        file: StaticString = #filePath, line: UInt = #line,
        until condition: (OpsController.Phase) -> Bool
    ) async throws {
        for _ in 0..<400 {
            if condition(controller.phase) { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("timed out waiting; phase is \(controller.phase)", file: file, line: line)
        throw CancellationError()
    }

    func test_plan_then_confirm_plans_again_then_runs() async throws {
        let fake = try runner()
        let ops = controller(fake)
        XCTAssertTrue(ops.plan(action))
        try await waitFor(ops, until: isReview)
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") }, "nothing may run before the confirmation")

        ops.confirm()
        try await waitFor(ops, until: isFinished)
        guard case .finished(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(fake.recorded.count, 3)
        XCTAssertEqual(fake.recorded.filter { $0.contains("--plan") }.count, 2)
        XCTAssertEqual(fake.recorded.last?.suffix(5).map { $0 }, [
            "server", "restart", "fleet-server-status.timer", "--yes", "--json",
        ])
        XCTAssertEqual(ops.completedRuns, 1)
        XCTAssertFalse(ops.isBusy)
    }

    func test_a_plan_that_changed_after_review_is_not_run() async throws {
        let changed = #"{"ok": true, "commands": [["systemctl", "--user", "restart", "something-else.timer"]], "result": "planned"}"#
        let fake = try runner(planAfterReview: changed)
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        // The controller shows the new plan,  so the review comes back after a run phase.
        try await waitFor(ops) { phase in
            if case .review(_, let outcome) = phase {
                return outcome.commandLines == ["systemctl --user restart something-else.timer"]
            }
            return false
        }
        XCTAssertNotNil(ops.notice)
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        XCTAssertEqual(ops.completedRuns, 0)
    }

    func test_a_second_plan_that_is_refused_shows_its_own_refusal() async throws {
        let refused = try fixture("ops_result_refused", "json")
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = controller(try runner(planAfterReview: refused), auditURL: url)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        guard case .finished(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertEqual(outcome.kind, .refused)
        XCTAssertEqual(outcome.detail, "sshd.service is not in the allowlist.")
        XCTAssertFalse(ops.notice?.contains("changed") ?? true)
        XCTAssertEqual(ops.completedRuns, 0)
        // Nothing was sent to run,  so the Mac's log has no row.
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func test_a_second_plan_that_throws_leaves_no_audit_row() async throws {
        let planned = try fixture("ops_result_planned", "json")
        let fake = ThrowingOpsRunner(planned: planned, throwFrom: 2, error: .outputTooLarge)
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: url, checksInstall: false)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops) { phase in
            if case .failed = phase { return true }
            return false
        }
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        XCTAssertEqual(ops.completedRuns, 0)
        XCTAssertNotNil(ops.notice)
    }

    func test_a_run_that_could_not_start_leaves_no_audit_row() async throws {
        let planned = try fixture("ops_result_planned", "json")
        let fake = ThrowingOpsRunner(planned: planned, throwFrom: 3, error: .launchFailed("no such file"))
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: url, checksInstall: false)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops) { phase in
            if case .failed = phase { return true }
            return false
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        XCTAssertEqual(ops.completedRuns, 0)
    }

    func test_a_run_that_failed_after_it_started_leaves_a_failed_row() async throws {
        let planned = try fixture("ops_result_planned", "json")
        let fake = ThrowingOpsRunner(planned: planned, throwFrom: 3, error: .outputTooLarge)
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: url, checksInstall: false)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops) { phase in
            if case .failed = phase { return true }
            return false
        }
        XCTAssertEqual(OpsAuditLog.read(at: url).entries.first?.result, "failed")
        XCTAssertEqual(ops.completedRuns, 1)
    }

    func test_a_knob_whose_old_value_changed_after_review_is_not_run() async throws {
        let first = try fixture("ops_result_knob_planned", "json")
        let second = first.replacingOccurrences(of: #""old": "off""#, with: #""old": "report-later""#)
        XCTAssertNotEqual(first, second)
        // The commands are the same both times:  only the value the write replaces moved.
        let fake = FakeOpsRunner { invocation, count in
            let text = count == 1 ? first : second
            return OpsProcessResult(exitCode: 0, stdout: invocation.arguments.contains("--plan") ? text : "", stderr: "")
        }
        let ops = controller(fake)
        ops.plan(.knobSet(key: "FLEET_AUTOPUSH_MODE", value: "report"))
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops) { phase in
            if case .review(_, let outcome) = phase { return outcome.knobChange?.old == "report-later" }
            return false
        }
        XCTAssertTrue(ops.notice?.contains("changed") == true)
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
    }

    func test_one_action_at_a_time() async throws {
        let fake = try runner()
        let ops = controller(fake)
        XCTAssertTrue(ops.plan(action))
        XCTAssertFalse(ops.plan(.serverUpdate(name: "fleet-cli")), "a second plan while one is planning")
        try await waitFor(ops, until: isReview)
        XCTAssertFalse(ops.plan(.serverUpdate(name: "fleet-cli")), "a second plan while one waits for review")
        XCTAssertNotNil(ops.notice)
        XCTAssertEqual(ops.phase.action, action)
    }

    func test_a_declined_touch_id_runs_nothing() async throws {
        let fake = try runner()
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.decline("Touch ID did not approve, so nothing ran.")
        guard case .failed(_, let reason) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertTrue(reason.contains("Touch ID"))
        // A confirm after a refusal does nothing:  the refusal is not retried.
        ops.confirm()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        ops.dismiss()
        XCTAssertEqual(ops.phase, .idle)
        XCTAssertTrue(ops.plan(action), "a new plan is allowed once the sheet is closed")
    }

    func test_a_refused_plan_is_shown_and_cannot_be_confirmed() async throws {
        let refused = try fixture("ops_result_refused", "json")
        let fake = FakeOpsRunner { _, _ in OpsProcessResult(exitCode: 2, stdout: refused, stderr: "") }
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isFinished)
        guard case .finished(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertEqual(outcome.kind, .refused)
        ops.confirm()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(fake.recorded.count, 1)
    }

    func test_a_server_action_leaves_a_row_in_the_macs_audit_log() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = controller(try runner(), auditURL: url)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        let reading = OpsAuditLog.read(at: url)
        XCTAssertEqual(reading.entries.count, 1)
        XCTAssertEqual(reading.entries.first?.target, "fleet-server-status.timer")
        XCTAssertEqual(reading.entries.first?.result, "done")
    }

    func test_a_local_action_leaves_its_row_to_the_helper() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = controller(try runner(), auditURL: url)
        ops.plan(.macAgent(verb: .restart, label: "com.jay.fleet-tools-doctor"))
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func test_an_invalid_value_fails_before_any_process() async throws {
        let fake = try runner()
        let ops = controller(fake)
        ops.plan(.knobSet(key: "FLEET_X", value: "-rf"))
        try await waitFor(ops) { phase in
            if case .failed = phase { return true }
            return false
        }
        XCTAssertTrue(fake.recorded.isEmpty)
    }
}

// MARK: - Editing a knob

final class KnobEditorTests: XCTestCase {
    func test_the_editor_follows_the_kind_column() {
        XCTAssertEqual(KnobEditorKind(kind: "bool"), .toggle)
        XCTAssertEqual(
            KnobEditorKind(kind: "enum: off < report < snapshot < full"),
            .choice(["off", "report", "snapshot", "full"])
        )
        XCTAssertEqual(KnobEditorKind(kind: "int 1 to 168"), .integer(min: 1, max: 168))
        XCTAssertEqual(KnobEditorKind(kind: "int 60000 to 21600000"), .integer(min: 60000, max: 21600000))
        XCTAssertEqual(KnobEditorKind(kind: "int"), .integer(min: nil, max: nil))
        guard case .text = KnobEditorKind(kind: "list") else { return XCTFail("list") }
        guard case .text = KnobEditorKind(kind: "caps") else { return XCTFail("caps") }
        guard case .text = KnobEditorKind(kind: "something new") else { return XCTFail("unknown") }
    }

    func test_the_editor_checks_a_value_before_the_engine_does() {
        XCTAssertNil(KnobEditorKind.toggle.problem(with: "true"))
        XCTAssertNotNil(KnobEditorKind.toggle.problem(with: "maybe"))
        let hours = KnobEditorKind.integer(min: 1, max: 168)
        XCTAssertNil(hours.problem(with: "12"))
        XCTAssertNotNil(hours.problem(with: "0"))
        XCTAssertNotNil(hours.problem(with: "169"))
        XCTAssertNotNil(hours.problem(with: "twelve"))
        let mode = KnobEditorKind.choice(["off", "report"])
        XCTAssertNil(mode.problem(with: "report"))
        XCTAssertNotNil(mode.problem(with: "full"))
        XCTAssertNil(KnobEditorKind.text(hint: "").problem(with: ""))
        XCTAssertNotNil(KnobEditorKind.text(hint: "").problem(with: "a\nb"))
    }

    private func knob(
        _ name: String, kind: String, defaultValue: String, secret: Bool,
        current: KnobRegistry.Knob.CurrentValue
    ) -> KnobRegistry.Knob {
        KnobRegistry.Knob(
            name: name, owner: "C1", kind: kind, defaultValue: defaultValue,
            isSecret: secret, secretColumn: secret ? "secret" : "no",
            localMay: "nothing", note: "", current: current
        )
    }

    func test_a_secret_knob_has_no_editor_and_no_value() {
        let secret = knob("FLEET_FACTS_SIGNING_KEY", kind: "str", defaultValue: "none, fails closed",
                          secret: true, current: .secretUnconfirmed)
        let mode = knob("FLEET_AUTOPUSH_MODE", kind: "enum: off < report", defaultValue: "off",
                        secret: false, current: .current("report"))
        let repos = knob("FLEET_AUTOPUSH_REPOS", kind: "list", defaultValue: "empty",
                         secret: false, current: .defaultValue("empty"))
        XCTAssertNil(secret.editor)
        XCTAssertEqual(mode.editValue, "report")
        XCTAssertEqual(repos.editValue, "")

        let registry = KnobRegistry(
            knobs: [secret, mode, repos], copyAge: "", copyIsStale: false,
            copyHeadline: "", unreadableRows: 0, problem: nil
        )
        let rows = registry.rows
        XCTAssertNil(rows[0].editor)
        XCTAssertEqual(rows[0].editValue, "")
        XCTAssertEqual(rows[1].editor, .choice(["off", "report"]))
        XCTAssertEqual(rows[1].editValue, "report")
        XCTAssertEqual(rows[2].editValue, "")
    }

    func test_the_words_for_nothing_start_the_editor_empty() {
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("empty"), "")
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("unset"), "")
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("none, fails closed"), "")
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("off"), "off")
    }
}
