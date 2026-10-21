import XCTest

// Fixtures in Fixtures/ are real engine output:  `status --json` and `apply SEAT [KIND] --plan`
// captured read-only from scripts/fleet_config, plus apply reports rendered by the engine's own
// `ApplyResult.render()` for constructed steps (no installer ran and nothing was written).

private final class FixtureLocator {}

func fixture(_ name: String, _ ext: String) throws -> String {
    let bundle = Bundle(for: FixtureLocator.self)
    guard let url = bundle.url(forResource: name, withExtension: ext) else {
        throw XCTSkip("missing fixture \(name).\(ext)")
    }
    return try String(contentsOf: url, encoding: .utf8)
}

final class EnvelopeDecodingTests: XCTestCase {
    func testDecodesLiveStatusEnvelope() throws {
        let envelope = try FleetConfigEnvelope.decode(try fixture("status_two_seats_live", "json"))
        XCTAssertEqual(envelope.schemaVersion, 1)
        XCTAssertFalse(envelope.results.isEmpty)
        let hooks = try XCTUnwrap(envelope.result(seat: "CLAUDE", ruleID: "claude-hooks-lane-guard"))
        XCTAssertEqual(hooks.kind, "hooks")
        XCTAssertEqual(hooks.state, .pass)
        XCTAssertTrue(hooks.isProven)
        let drift = envelope.results.first { $0.state == .drift }
        XCTAssertNotNil(drift)
        XCTAssertFalse(drift!.isProven)
    }

    func testDecodesMissingState() throws {
        let envelope = try FleetConfigEnvelope.decode(try fixture("status_claude_missing", "json"))
        let hooks = try XCTUnwrap(envelope.result(seat: "CLAUDE", ruleID: "claude-hooks-lane-guard"))
        XCTAssertEqual(hooks.state, .missing)
        XCTAssertEqual(hooks.failing, true)
        XCTAssertFalse(hooks.isProven)
    }

    func testToleratesUnknownFieldsAndStates() throws {
        let json = """
        {"schemaVersion":1,"extra":{"a":1},"summary":{"total":1,"overallVerdict":"UNKNOWN","novel":true},
         "results":[{"ruleId":"r","seat":"S","platform":"p","kind":"hooks","state":"EXOTIC",
                     "reason":"x","somethingNew":[1,2]}]}
        """
        let envelope = try FleetConfigEnvelope.decode(json)
        XCTAssertEqual(envelope.results[0].state, .unrecognized("EXOTIC"))
        XCTAssertFalse(envelope.results[0].isProven)
    }

    func testStaleOrFailingPassIsNotProven() throws {
        let json = """
        {"schemaVersion":1,"summary":{"total":1,"overallVerdict":"FAIL"},
         "results":[{"ruleId":"r","seat":"S","platform":"p","kind":"hooks","state":"PASS",
                     "reason":"x","stale":true}]}
        """
        XCTAssertFalse(try FleetConfigEnvelope.decode(json).results[0].isProven)
    }

    func testRejectsOtherSchemaVersionAndGarbage() {
        XCTAssertThrowsError(try FleetConfigEnvelope.decode(
            #"{"schemaVersion":2,"summary":{"total":0,"overallVerdict":"PASS"},"results":[]}"#
        )) { XCTAssertEqual($0 as? FleetConfigCLIError, .unsupportedSchema(2)) }
        XCTAssertThrowsError(try FleetConfigEnvelope.decode("not json"))
        XCTAssertThrowsError(try FleetConfigEnvelope.decode(""))
    }
}

final class InvocationTests: XCTestCase {
    func testStatusArguments() throws {
        XCTAssertEqual(
            try FleetConfigInvocation.status(seat: "CLAUDE", kind: "hooks", includeDoctors: false).arguments,
            ["status", "--json", "--seat", "CLAUDE", "--kind", "hooks", "--no-doctor"]
        )
    }

    func testPlanAndApplyArguments() throws {
        XCTAssertEqual(
            try FleetConfigInvocation.planApply(seat: "CLAUDE", kind: "hooks").arguments,
            ["apply", "CLAUDE", "hooks", "--plan"]
        )
        let apply = try FleetConfigInvocation.apply(seat: "CLAUDE", kind: "hooks")
        XCTAssertEqual(apply.arguments, ["apply", "CLAUDE", "hooks", "--yes"])
        for flag in ["--home", "--manifest", "--registry"] {
            XCTAssertFalse(apply.arguments.contains(flag))
        }
    }

    func testRefusesFlagLikeAndPathLikeTokens() {
        for bad in ["--home", "-x", "a b", "../x", "a;b", "", "$(x)"] {
            XCTAssertThrowsError(try FleetConfigInvocation.planApply(seat: bad), bad)
            XCTAssertThrowsError(try FleetConfigInvocation.apply(seat: "CLAUDE", kind: bad), bad)
        }
        XCTAssertNoThrow(try FleetConfigInvocation.planApply(seat: "GROK-WEB"))
    }

    func testProcessArgumentsAreAnArgvArrayForTheEngineModule() throws {
        let invocation = try FleetConfigInvocation.planApply(seat: "CLAUDE")
        XCTAssertEqual(
            FleetConfigCLI.processArguments(for: invocation),
            ["-m", "fleet_config", "apply", "CLAUDE", "--plan"]
        )
        XCTAssertEqual(FleetConfigCLI.interpreter, "/usr/bin/python3")
    }

    func testEnvironmentDropsHelperAndPythonOverrides() {
        let cleaned = FleetConfigCLI.sanitizedEnvironment([
            "HOME": "/Users/x", "PATH": "/usr/bin",
            "FLEET_CONFIG_TOUCHID_TEST_BYPASS": "1", "FLEET_CONFIG_HELPER": "/tmp/fake",
            "PYTHONPATH": "/tmp/evil", "PYTHONHOME": "/tmp/evil", "PYTHONSTARTUP": "/tmp/s.py",
        ])
        XCTAssertEqual(cleaned["HOME"], "/Users/x")
        XCTAssertNil(cleaned["FLEET_CONFIG_TOUCHID_TEST_BYPASS"])
        XCTAssertNil(cleaned["FLEET_CONFIG_HELPER"])
        XCTAssertNil(cleaned["PYTHONPATH"])
        XCTAssertNil(cleaned["PYTHONHOME"])
        XCTAssertNil(cleaned["PYTHONSTARTUP"])
        XCTAssertEqual(cleaned["PYTHONDONTWRITEBYTECODE"], "1")
    }

    func testLocationPrecedence() {
        let suite = UserDefaults(suiteName: "muster-test-\(UUID().uuidString)")!
        let home = URL(fileURLWithPath: "/Users/tester")
        XCTAssertEqual(
            FleetConfigLocation.resolve(defaults: suite, environment: [:], home: home).repoRoot.path,
            "/Users/tester/Code/AI-Fleet-Coordinator"
        )
        XCTAssertEqual(
            FleetConfigLocation.resolve(
                defaults: suite, environment: ["MUSTER_REPO_ROOT": "/env/repo"], home: home
            ).repoRoot.path, "/env/repo"
        )
        suite.set("/saved/repo", forKey: FleetConfigLocation.defaultsKey)
        XCTAssertEqual(
            FleetConfigLocation.resolve(
                defaults: suite, environment: ["MUSTER_REPO_ROOT": "/env/repo"], home: home
            ).repoRoot.path, "/saved/repo"
        )
    }

    func testMissingEngineIsReported() {
        let location = FleetConfigLocation(repoRoot: URL(fileURLWithPath: "/nonexistent-muster-repo"))
        XCTAssertThrowsError(try location.validate()) {
            guard case FleetConfigCLIError.engineNotFound = $0 else { return XCTFail("\($0)") }
        }
    }
}

final class PlanParsingTests: XCTestCase {
    func testNothingToChange() throws {
        let plan = try ApplyPlanParser.parse(try fixture("plan_nothing_to_change", "txt"))
        XCTAssertTrue(plan.entries.isEmpty)
        XCTAssertEqual(plan.scope, "CLAUDE (hooks)")
    }

    func testBlockedHooksEntryCarriesReasonAndFiles() throws {
        let plan = try ApplyPlanParser.parse(try fixture("plan_hooks_missing_blocked", "txt"))
        let entry = try XCTUnwrap(plan.entry(ruleID: "claude-hooks-lane-guard"))
        XCTAssertEqual(entry.state, .missing)
        XCTAssertEqual(entry.kind, "hooks")
        XCTAssertEqual(entry.files, [".claude/settings.json"])
        XCTAssertTrue(entry.isPersistenceClass)
        XCTAssertEqual(
            entry.autofix,
            .blocked(reason: "the manifest does not allow an automatic fix for this rule")
        )
        XCTAssertTrue(plan.autofixable.isEmpty)
    }

    func testAutofixEntriesAndTouchID() throws {
        let plan = try ApplyPlanParser.parse(try fixture("plan_autofix_with_touch_id", "txt"))
        XCTAssertEqual(plan.entries.count, 3)
        XCTAssertEqual(plan.autofixable.map(\.ruleID), ["claude-hooks-lane-guard", "claude-instructions-lane-map"])
        XCTAssertEqual(plan.entry(ruleID: "claude-hooks-lane-guard")?.autofix, .allowed(persistenceClass: true))
        XCTAssertEqual(plan.entry(ruleID: "claude-instructions-lane-map")?.autofix, .allowed(persistenceClass: false))
        XCTAssertTrue(plan.needsTouchID)
        XCTAssertEqual(plan.notes, ["a note here"])
    }

    func testEntryWithoutInstallerHasNoFiles() throws {
        let plan = try ApplyPlanParser.parse(try fixture("plan_report_only", "txt"))
        let entry = try XCTUnwrap(plan.entries.first)
        XCTAssertNil(entry.installer)
        XCTAssertTrue(entry.files.isEmpty)
        XCTAssertFalse(entry.isPersistenceClass)
    }

    func testWholeSeatPlan() throws {
        let plan = try ApplyPlanParser.parse(try fixture("plan_seat_all_blocked", "txt"))
        XCTAssertEqual(plan.entries.count, 5)
        XCTAssertEqual(plan.scope, "CODEX")
    }

    func testFingerprintChangesWhenPlanChanges() throws {
        let a = try ApplyPlanParser.parse(try fixture("plan_autofix_with_touch_id", "txt"))
        let b = try ApplyPlanParser.parse(try fixture("plan_hooks_missing_blocked", "txt"))
        XCTAssertEqual(a.fingerprint, a.fingerprint)
        XCTAssertNotEqual(a.fingerprint, b.fingerprint)
    }

    func testFailsClosedOnCountMismatchAndGarbage() throws {
        var text = try fixture("plan_autofix_with_touch_id", "txt")
        text = text.replacingOccurrences(of: "3 entries,  2 auto-fixable", with: "3 entries,  3 auto-fixable")
        XCTAssertThrowsError(try ApplyPlanParser.parse(text))
        XCTAssertThrowsError(try ApplyPlanParser.parse("fleet-config: unknown seat 'NOPE'"))
        XCTAssertThrowsError(try ApplyPlanParser.parse(""))
        XCTAssertThrowsError(try ApplyPlanParser.parse("Plan for X:  1 entry,  0 auto-fixable.\n  this is junk\n"))
    }

    func testCopyNormalisesEngineGaps() {
        XCTAssertEqual(ApplyCopy.normalize("a;  b.  C"), "a;\u{00A0} b.\u{00A0} C")
        XCTAssertEqual(ApplyCopy.normalize("a b"), "a b")
        XCTAssertEqual(ApplyCopy.join("One.", "Two."), "One.\u{00A0} Two.")
    }
}

final class OutcomeMappingTests: XCTestCase {
    private let rule = "claude-hooks-lane-guard"

    private func result(_ exit: Int32, _ stdout: String, stderr: String = "", timedOut: Bool = false) -> FleetConfigCLIResult {
        FleetConfigCLIResult(exitCode: exit, stdout: stdout, stderr: stderr, timedOut: timedOut)
    }

    private func result(state: FleetConfigState, stale: Bool = false) -> FleetConfigResult {
        FleetConfigResult(
            ruleId: rule, seat: "CLAUDE", platform: "claude", kind: "hooks", state: state,
            reason: "r", reasonCode: nil, failing: state != .pass, stale: stale, checkedAt: nil
        )
    }

    func testReportParsing() throws {
        let report = try XCTUnwrap(ApplyReport.parse(try fixture("apply_report_applied", "txt")))
        XCTAssertEqual(report.steps.count, 1)
        XCTAssertEqual(report.steps[0].outcome, "applied")
        XCTAssertEqual(report.steps[0].ruleID, rule)
        XCTAssertEqual(report.verdictAfter, "PASS")
        XCTAssertNil(report.refusalReason)
        XCTAssertNil(ApplyReport.parse("fleet-config: unknown seat"))
    }

    func testAppliedAndReprovedPassIsGreen() throws {
        let outcome = ApplyOutcome.classify(
            applyResult: result(0, try fixture("apply_report_applied", "txt")),
            targetRuleID: rule, reproved: result(state: .pass)
        )
        XCTAssertTrue(outcome.isGreen)
    }

    func testAppliedButStillMissingIsNotGreen() throws {
        let outcome = ApplyOutcome.classify(
            applyResult: result(1, try fixture("apply_report_applied", "txt")),
            targetRuleID: rule, reproved: result(state: .missing)
        )
        XCTAssertEqual(outcome, .stillFailing(result(state: .missing)))
    }

    func testStaleProofIsNeverGreen() throws {
        let outcome = ApplyOutcome.classify(
            applyResult: result(0, try fixture("apply_report_applied", "txt")),
            targetRuleID: rule, reproved: result(state: .pass, stale: true)
        )
        XCTAssertFalse(outcome.isGreen)
    }

    func testMissingReproofIsNotGreen() throws {
        let outcome = ApplyOutcome.classify(
            applyResult: result(0, try fixture("apply_report_applied", "txt")),
            targetRuleID: rule, reproved: nil
        )
        XCTAssertEqual(outcome, .stillFailing(nil))
    }

    func testTouchIDRefusals() throws {
        let cases: [(String, ApplyRefusalKind)] = [
            ("apply_report_refused_declined", .touchIDDeclined),
            ("apply_report_refused_unavailable", .touchIDUnavailable),
            ("apply_report_refused_helper_missing", .touchIDHelperProblem),
        ]
        for (name, kind) in cases {
            let outcome = ApplyOutcome.classify(
                applyResult: result(2, try fixture(name, "txt")),
                targetRuleID: rule, reproved: result(state: .missing)
            )
            guard case .refused(let got, let message) = outcome else { return XCTFail("\(name): \(outcome)") }
            XCTAssertEqual(got, kind, name)
            XCTAssertFalse(message.isEmpty)
            XCTAssertFalse(outcome.isGreen)
        }
    }

    func testRefusalWinsEvenIfReproofHappensToPass() throws {
        let outcome = ApplyOutcome.classify(
            applyResult: result(2, try fixture("apply_report_refused_declined", "txt")),
            targetRuleID: rule, reproved: result(state: .pass)
        )
        guard case .refused = outcome else { return XCTFail("\(outcome)") }
    }

    func testInstallerFailure() throws {
        let outcome = ApplyOutcome.classify(
            applyResult: result(1, try fixture("apply_report_failed", "txt")),
            targetRuleID: rule, reproved: result(state: .missing)
        )
        XCTAssertEqual(outcome, .failed("the installer exited 1"))
    }

    func testEngineLevelRefusalWithoutReport() {
        let outcome = ApplyOutcome.classify(
            applyResult: result(2, "", stderr: "fleet-config: the manifest is invalid\n"),
            targetRuleID: rule, reproved: nil
        )
        XCTAssertEqual(outcome, .refused(.engineRefused, "fleet-config: the manifest is invalid"))
    }

    func testUsageErrorAndTimeoutAndUnknownExit() {
        XCTAssertEqual(
            ApplyOutcome.classify(
                applyResult: result(64, "", stderr: "fleet-config: unknown seat 'X'\n"),
                targetRuleID: rule, reproved: nil
            ),
            .refused(.usage, "fleet-config: unknown seat 'X'")
        )
        guard case .failed = ApplyOutcome.classify(
            applyResult: result(143, "", timedOut: true), targetRuleID: rule, reproved: nil
        ) else { return XCTFail("timeout") }
        guard case .failed = ApplyOutcome.classify(
            applyResult: result(9, ""), targetRuleID: rule, reproved: nil
        ) else { return XCTFail("exit 9") }
        guard case .failed = ApplyOutcome.classify(
            applyResult: result(0, "garbage"), targetRuleID: rule, reproved: result(state: .pass)
        ) else { return XCTFail("unreadable output must not read as success") }
    }
}

// MARK: - Coordinator flow with a fake engine

private final class FakeEngine: FleetConfigRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [[String]] = []
    var handler: (FleetConfigInvocation) -> FleetConfigCLIResult

    init(handler: @escaping (FleetConfigInvocation) -> FleetConfigCLIResult) { self.handler = handler }

    var recorded: [[String]] { lock.lock(); defer { lock.unlock() }; return calls }

    func run(_ invocation: FleetConfigInvocation) async throws -> FleetConfigCLIResult {
        lock.lock(); calls.append(invocation.arguments); lock.unlock()
        return handler(invocation)
    }
}

private final class RecordingNotifier: ApplyNotifying, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [(String, String)] = []
    var posted: [(String, String)] { lock.lock(); defer { lock.unlock() }; return items }
    func post(title: String, body: String) async { lock.lock(); items.append((title, body)); lock.unlock() }
}

@MainActor
final class CoordinatorFlowTests: XCTestCase {
    private let rule = "claude-hooks-lane-guard"

    private func engine(
        plan: String, statusAfter: String, apply: FleetConfigCLIResult,
        planAfterReview: String? = nil
    ) -> FakeEngine {
        var planCalls = 0
        let lock = NSLock()
        return FakeEngine { invocation in
            let args = invocation.arguments
            if args.contains("--plan") {
                lock.lock(); planCalls += 1; let n = planCalls; lock.unlock()
                let text = (n > 1 ? planAfterReview : nil) ?? plan
                return FleetConfigCLIResult(exitCode: 0, stdout: text, stderr: "")
            }
            if args.first == "status" {
                return FleetConfigCLIResult(exitCode: 0, stdout: statusAfter, stderr: "")
            }
            return apply
        }
    }

    private func waitFor(
        _ coordinator: ApplyCoordinator, _ id: UUID,
        until condition: (ApplySession) -> Bool,
        file: StaticString = #filePath, line: UInt = #line
    ) async throws -> ApplySession {
        for _ in 0..<400 {
            if let session = coordinator.session(id: id), condition(session) { return session }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("timed out waiting for the session", file: file, line: line)
        throw CancellationError()
    }

    func testPlanThenConfirmThenGreen() async throws {
        let planText = try fixture("plan_autofix_with_touch_id", "txt")
        let fake = engine(
            plan: planText,
            statusAfter: try fixture("status_two_seats_live", "json"),
            apply: FleetConfigCLIResult(exitCode: 0, stdout: try fixture("apply_report_applied", "txt"), stderr: "")
        )
        let notifier = RecordingNotifier()
        let coordinator = ApplyCoordinator(runner: fake, notifier: notifier)
        let id = coordinator.beginPlan(seat: "CLAUDE", ruleID: rule, kind: nil)
        let reviewed = try await waitFor(coordinator, id) { if case .review = $0.phase { return true }; return false }
        XCTAssertEqual(reviewed.kind, "hooks")
        XCTAssertEqual(fake.recorded.first, ["apply", "CLAUDE", "--plan"])
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") }, "nothing may be applied before the confirmation")

        coordinator.confirmApply(sessionID: id)
        let done = try await waitFor(coordinator, id) { $0.isFinished }
        guard case .finished(let outcome) = done.phase else { return XCTFail("\(done.phase)") }
        XCTAssertTrue(outcome.isGreen)
        XCTAssertEqual(fake.recorded.filter { $0.contains("--yes") }, [["apply", "CLAUDE", "hooks", "--yes"]])
        XCTAssertEqual(coordinator.latestResult(seat: "CLAUDE", ruleID: rule)?.state, .pass)
        XCTAssertEqual(notifier.posted.count, 1)
        XCTAssertEqual(notifier.posted.first?.0, "Apply Finished")
        XCTAssertFalse(coordinator.hasWorkInFlight)
    }

    func testClosingTheSheetDoesNotCancelTheApply() async throws {
        let fake = engine(
            plan: try fixture("plan_autofix_with_touch_id", "txt"),
            statusAfter: try fixture("status_two_seats_live", "json"),
            apply: FleetConfigCLIResult(exitCode: 0, stdout: try fixture("apply_report_applied", "txt"), stderr: "")
        )
        let coordinator = ApplyCoordinator(runner: fake, notifier: RecordingNotifier())
        let id = coordinator.beginPlan(seat: "CLAUDE", ruleID: rule, kind: "hooks")
        _ = try await waitFor(coordinator, id) { if case .review = $0.phase { return true }; return false }
        coordinator.confirmApply(sessionID: id)
        coordinator.dismiss(sessionID: id)   // the owner closes the sheet immediately
        XCTAssertNil(coordinator.presentedSessionID)
        let done = try await waitFor(coordinator, id) { $0.isFinished }
        guard case .finished(let outcome) = done.phase else { return XCTFail("\(done.phase)") }
        XCTAssertTrue(outcome.isGreen)
    }

    func testTouchIDRefusalLeavesCellRedAndIsNotRetried() async throws {
        let notifier = RecordingNotifier()
        let fake = engine(
            plan: try fixture("plan_autofix_with_touch_id", "txt"),
            statusAfter: try fixture("status_claude_missing", "json"),
            apply: FleetConfigCLIResult(exitCode: 2, stdout: try fixture("apply_report_refused_declined", "txt"), stderr: "")
        )
        let coordinator = ApplyCoordinator(runner: fake, notifier: notifier)
        let id = coordinator.beginPlan(seat: "CLAUDE", ruleID: rule, kind: "hooks")
        _ = try await waitFor(coordinator, id) { if case .review = $0.phase { return true }; return false }
        coordinator.confirmApply(sessionID: id)
        let done = try await waitFor(coordinator, id) { $0.isFinished }
        guard case .finished(.refused(let kind, _)) = done.phase else { return XCTFail("\(done.phase)") }
        XCTAssertEqual(kind, .touchIDDeclined)
        XCTAssertEqual(fake.recorded.filter { $0.contains("--yes") }.count, 1, "a refusal is never retried")
        XCTAssertNotEqual(coordinator.latestResult(seat: "CLAUDE", ruleID: rule)?.state, .pass)
        XCTAssertEqual(notifier.posted.first?.0, "Apply Needs Attention")
    }

    func testBlockedTargetCannotBeApplied() async throws {
        let fake = engine(
            plan: try fixture("plan_hooks_missing_blocked", "txt"),
            statusAfter: try fixture("status_claude_missing", "json"),
            apply: FleetConfigCLIResult(exitCode: 0, stdout: "", stderr: "")
        )
        let coordinator = ApplyCoordinator(runner: fake, notifier: RecordingNotifier())
        let id = coordinator.beginPlan(seat: "CLAUDE", ruleID: rule, kind: "hooks")
        _ = try await waitFor(coordinator, id) { if case .review = $0.phase { return true }; return false }
        coordinator.confirmApply(sessionID: id)
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        XCTAssertEqual(fake.recorded.count, 1)
    }

    func testPlanThatChangesAfterReviewIsNotApplied() async throws {
        let fake = engine(
            plan: try fixture("plan_autofix_with_touch_id", "txt"),
            statusAfter: try fixture("status_claude_missing", "json"),
            apply: FleetConfigCLIResult(exitCode: 0, stdout: try fixture("apply_report_applied", "txt"), stderr: ""),
            planAfterReview: try fixture("plan_hooks_missing_blocked", "txt")
        )
        let coordinator = ApplyCoordinator(runner: fake, notifier: RecordingNotifier())
        let id = coordinator.beginPlan(seat: "CLAUDE", ruleID: rule, kind: "hooks")
        _ = try await waitFor(coordinator, id) { if case .review = $0.phase { return true }; return false }
        coordinator.confirmApply(sessionID: id)
        let back = try await waitFor(coordinator, id) { $0.notice != nil }
        guard case .review = back.phase else { return XCTFail("\(back.phase)") }
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
    }

    func testUnreadablePlanFailsClosed() async throws {
        let fake = FakeEngine { _ in FleetConfigCLIResult(exitCode: 0, stdout: "Plan for X:  1 entry,  1 auto-fixable.\n", stderr: "") }
        let coordinator = ApplyCoordinator(runner: fake, notifier: RecordingNotifier())
        let id = coordinator.beginPlan(seat: "CLAUDE", ruleID: rule, kind: "hooks")
        let done = try await waitFor(coordinator, id) { $0.isFinished }
        guard case .failedToPlan = done.phase else { return XCTFail("\(done.phase)") }
    }
}

// Runs the real engine through the real Process wrapper.  Read-only verbs only:  `status --json`
// and `apply SEAT --plan`.  It never runs an apply.  Skipped when the repository is not found.
final class LiveEngineReadOnlyTests: XCTestCase {
    private var location: FleetConfigLocation {
        // .../muster/MusterTests/ApplyPlanTests.swift -> repo root is three levels up
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return FleetConfigLocation(repoRoot: root)
    }

    func testStatusAndPlanThroughTheRealWrapper() async throws {
        try XCTSkipIf(ProcessInfo.processInfo.environment["CI"] != nil, "hosted CI has no fleet secret store")
        let location = self.location
        try XCTSkipUnless(
            FileManager.default.fileExists(atPath: location.engineEntryPoint.path), "no engine checkout"
        )
        let cli = FleetConfigCLI(location: { location })
        let envelope = try await cli.status(seat: "OPENCODE", includeDoctors: false)
        XCTAssertEqual(envelope.schemaVersion, 1)
        XCTAssertFalse(envelope.results.isEmpty)
        XCTAssertTrue(envelope.results.allSatisfy { $0.seat == "OPENCODE" })

        let plan = try await cli.run(FleetConfigInvocation.planApply(seat: "OPENCODE"))
        XCTAssertEqual(plan.exitCode, 0, plan.stderr)
        let parsed = try ApplyPlanParser.parse(plan.stdout)
        XCTAssertEqual(parsed.entries.count, parsed.declaredEntryCount)

        let usage = try await cli.run(FleetConfigInvocation.planApply(seat: "NOPE"))
        XCTAssertEqual(usage.exitCode, 64)
        XCTAssertTrue(usage.stderrTail.contains("unknown seat"))
    }
}
