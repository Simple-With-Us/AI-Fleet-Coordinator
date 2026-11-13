import Combine
import XCTest

private let settingsRevisionA = String(repeating: "a", count: 64)
private let settingsRevisionB = String(repeating: "b", count: 64)
private let settingsRevisionC = String(repeating: "c", count: 64)
private func settingsAttempt(_ number: Int = 1) -> ServerSettingsAttempt {
    let suffix = String(number)
    return ServerSettingsAttempt(requestID: "00000000-0000-4000-8000-" + String(repeating: "0", count: 12 - suffix.count) + suffix,
                                 requestCreatedAt: 1_794_590_000_000 + number)
}
private func settingsRequest(set: [String: ServerSettingValue], expectBaseSHA256: String,
                            expectedKeys: Set<String> = [], remove: [String] = [],
                            referenceNames: [String: Set<String>] = [:],
                            attempt: ServerSettingsAttempt = settingsAttempt()) -> ServerSettingsSaveRequest {
    ServerSettingsSaveRequest(set: set, expectBaseSHA256: expectBaseSHA256, expectedKeys: expectedKeys,
                              remove: remove, referenceNames: referenceNames, attempt: attempt)
}
private let settingsBaseline: [String: ServerSettingValue] = [
    "daemon.coalesce_seconds": .int(20),
    "daemon.presence_topics": .list([.list([.text("Work-In-Progress"), .text("fleet")])]),
    "linear_projects.enabled": .bool(false),
    "seat.CODEX.enabled": .bool(true),
    "seat.CODEX.routine": .table(["url_env": .text("CODEX_ROUTINE_URL"), "key_env": .text("CODEX_ROUTINE_KEY"),
                                  "method": .text("POST"), "auth": .text("bearer"), "header": .text("Authorization")]),
]

private func settingsBuilder() -> OpsCommandBuilder {
    OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/tmp/operator-settings-fixture"),
                      home: URL(fileURLWithPath: "/Users/example"), sshAuthSocket: nil)
}

private func settingsSchema() throws -> ServerSettingsAllowlist {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    return try XCTUnwrap(ServerSettingsAllowlist.read(at: root.appendingPathComponent(ServerSettingsAllowlist.relativePath)))
}

private let generatedSettingsCases: [String: ServerSettingValue] = {
    let path = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .appendingPathComponent("Fixtures/operator-settings-wire.json")
    guard let data = try? Data(contentsOf: path),
          let root = ServerSettingJSON.parse(String(decoding: data, as: UTF8.self))?.tableEntries,
          root["synthetic"] == .bool(true), let cases = root["cases"]?.tableEntries,
          ["get", "save1", "save2", "runtime_pending", "reconcile_unknown", "reconcile_saved",
           "superseded", "known_ok_read_failed", "cancelled", "initialized", "unactivated_get"].allSatisfy({ cases[$0]?.tableEntries != nil }) else {
        preconditionFailure("The generated Operator settings wire fixture is missing or malformed")
    }
    return cases
}()

private struct GeneratedSettingsCase {
    let request: [String: ServerSettingValue]?
    let response: [String: ServerSettingValue]
    let exitCode: Int32
    var stdout: String {
        guard let text = ServerSettingValue.table(response).json else {
            preconditionFailure("The generated Operator response cannot be serialized")
        }
        return text
    }
}

private func generatedSettingsCase(_ name: String) -> GeneratedSettingsCase {
    guard let entry = generatedSettingsCases[name]?.tableEntries,
          let response = entry["response"]?.tableEntries,
          response["eavesdropperSettings"]?.tableEntries != nil,
          case .int(let code)? = entry["exitCode"], let exitCode = Int32(exactly: code) else {
        preconditionFailure("The generated Operator settings wire case has invalid fields: \(name)")
    }
    return GeneratedSettingsCase(request: entry["request"]?.tableEntries, response: response, exitCode: exitCode)
}

private func generatedSaveRequest(_ name: String) throws -> ServerSettingsSaveRequest {
    let document = try XCTUnwrap(generatedSettingsCase(name).request)
    let set = try XCTUnwrap(document["set"]?.tableEntries)
    guard case .text(let base)? = document["expectBaseSHA256"],
          case .text(let id)? = document["requestID"], case .int(let created)? = document["requestCreatedAt"] else {
        throw SettingsTestWait.timedOut
    }
    let get = try XCTUnwrap(ServerSettingsAnswer(stdout: generatedSettingsCase("get").stdout))
    return settingsRequest(set: set, expectBaseSHA256: base, expectedKeys: Set(get.settings.keys),
                           attempt: ServerSettingsAttempt(requestID: id, requestCreatedAt: created))
}

private func settingsEnvelope(_ settings: [String: ServerSettingValue] = settingsBaseline,
                              revision: String = settingsRevisionA, result: String = "read",
                              attempt: ServerSettingsAttempt = settingsAttempt(), reconciling: Bool = false,
                              overrides: [String: ServerSettingValue] = [:], outerOK: Bool = true) -> String {
    // Receipt flags come from the real helper/editor transaction, never a permissive injection.
    let template = generatedSettingsCase(result == "read" ? "get" : (result == "saved" ? "save1" : "runtime_pending"))
    guard var document = template.response["eavesdropperSettings"]?.tableEntries else {
        preconditionFailure("The generated Operator response has no settings envelope")
    }
    document["ok"] = .bool(outerOK)
    document["result"] = .text(result)
    document["revision"] = .text(revision)
    document["baseSHA256"] = .text(revision)
    document["settings"] = .table(settings)
    document["channelNames"] = .list([.text("Work-In-Progress"), .text("Builds"), .text("Custom Team")])
    if result != "read" {
        document["action"] = .text(reconciling ? "reconcile" : "save")
        document["requestID"] = .text(attempt.requestID)
        document["outcome"] = .text(result == "saved" ? "ok" : "unknown")
        if reconciling { document["operation"] = .text("save") }
    }
    document.merge(overrides, uniquingKeysWith: { _, new in new })
    return ServerSettingValue.table(["ok": .bool(outerOK), "result": .text(result),
                                      "eavesdropperSettings": .table(document)]).json ?? ""
}

private enum SettingsTestWait: Error { case timedOut }

private actor SettingsSaveRunner: OpsRunning {
    private var pending: [CheckedContinuation<OpsProcessResult, Never>] = []
    private var pauseRead = false
    private var pendingRead: CheckedContinuation<OpsProcessResult, Never>?
    private(set) var invocations: [OpsInvocation] = []
    private(set) var writes: [OpsInvocation] = []
    private(set) var reconciles: [OpsInvocation] = []
    var readAnswer = settingsEnvelope()
    private var reconcileAnswer = ""

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        invocations.append(invocation)
        if let data = invocation.standardInput,
           ServerSettingJSON.parse(String(decoding: data, as: UTF8.self))?.tableEntries?["action"] == .text("reconcile") {
            reconciles.append(invocation)
            return OpsProcessResult(exitCode: 0, stdout: reconcileAnswer, stderr: "")
        }
        if invocation.arguments.contains("--plan") {
            if pauseRead {
                pauseRead = false
                return await withCheckedContinuation { continuation in
                    pendingRead = continuation
                }
            }
            return OpsProcessResult(exitCode: 0, stdout: readAnswer, stderr: "")
        }
        return await withCheckedContinuation { continuation in
            pending.append(continuation)
            writes.append(invocation)
        }
    }

    func untilWrites(_ count: Int) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        while writes.count < count {
            guard ProcessInfo.processInfo.systemUptime < deadline else { throw SettingsTestWait.timedOut }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
    }

    func finish(_ answer: String, exit: Int32 = 0, timedOut: Bool = false) {
        pending.removeFirst().resume(returning: OpsProcessResult(exitCode: exit, stdout: answer, stderr: "", timedOut: timedOut))
    }

    func setRead(_ answer: String) { readAnswer = answer }
    func setReconcile(_ answer: String) { reconcileAnswer = answer }
    func holdNextRead() { pauseRead = true }
    func untilReadWaiting() async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        while pendingRead == nil {
            guard ProcessInfo.processInfo.systemUptime < deadline else { throw SettingsTestWait.timedOut }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
    }
    func finishRead(_ answer: String) {
        readAnswer = answer
        pendingRead?.resume(returning: OpsProcessResult(exitCode: 0, stdout: answer, stderr: ""))
        pendingRead = nil
    }
}

private actor SettingsDebounceClock {
    private var sleepers: [CheckedContinuation<Void, Never>] = []
    private var count = 0

    func sleep() async {
        guard !Task.isCancelled else { return }
        await withCheckedContinuation { continuation in
            sleepers.append(continuation)
            count += 1
        }
    }

    func untilSleeps(_ target: Int) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        while count < target {
            guard ProcessInfo.processInfo.systemUptime < deadline else { throw SettingsTestWait.timedOut }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
    }

    func advance() {
        let ready = sleepers
        sleepers = []
        for sleeper in ready { sleeper.resume() }
    }
}

final class ServerSettingsAutosaveReceiptTests: XCTestCase {
    func test_save_uses_existing_forced_command_and_revision_fenced_stdin() throws {
        let request = settingsRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA)
        let invocation = try settingsBuilder().listenerSettingsSaveInvocation(request)
        XCTAssertEqual(invocation.executable, OpsControlRoute.ssh)
        XCTAssertEqual(Array(invocation.arguments.suffix(7)),
                       [OpsControlRoute.serverAlias, "--", "server", "eavesdropper-settings", "server", "--yes", "--json"])
        let text = String(decoding: try XCTUnwrap(invocation.standardInput), as: UTF8.self)
        let document = try XCTUnwrap(ServerSettingJSON.parse(text)?.tableEntries)
        XCTAssertEqual(document["action"], .text("save"))
        XCTAssertEqual(document["expectBaseSHA256"], .text(settingsRevisionA))
        XCTAssertEqual(document["set"], .table(request.set))
        XCTAssertEqual(document["requestID"], .text(request.attempt.requestID))
        XCTAssertEqual(document["requestCreatedAt"], .int(request.attempt.requestCreatedAt))
        XCTAssertFalse(invocation.arguments.contains { $0.contains(settingsRevisionA) })
    }

    func test_reconcile_uses_the_same_route_with_only_the_original_identity() throws {
        let attempt = settingsAttempt()
        let invocation = try settingsBuilder().listenerSettingsReconcileInvocation(attempt)
        XCTAssertEqual(invocation.executable, OpsControlRoute.ssh)
        XCTAssertEqual(Array(invocation.arguments.suffix(7)),
                       [OpsControlRoute.serverAlias, "--", "server", "eavesdropper-settings", "server", "--yes", "--json"])
        let input = try XCTUnwrap(invocation.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self)), .table([
            "action": .text("reconcile"), "requestID": .text(attempt.requestID),
            "requestCreatedAt": .int(attempt.requestCreatedAt),
        ]))
        XCTAssertThrowsError(try settingsBuilder().listenerSettingsReconcileInvocation(
            ServerSettingsAttempt(requestID: "invalid", requestCreatedAt: 1)))
    }

    func test_saved_requires_exact_readback_and_all_provider_runtime_receipts() {
        let request = settingsRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA,
                                               expectedKeys: Set(settingsBaseline.keys))
        var applied = settingsBaseline
        applied["linear_projects.enabled"] = .bool(true)
        XCTAssertNil(generatedSettingsCase("save1").response["eavesdropperSettings"]?.tableEntries?["schemaVersion"],
                     "The current shared receipt intentionally has no required schemaVersion field")
        let good = OpsProcessResult(exitCode: 0, stdout: settingsEnvelope(applied, revision: settingsRevisionB, result: "saved"), stderr: "")
        guard case .saved = ServerListenerSettingsSaver.receipt(good, request: request) else { return XCTFail("No saved receipt") }
        let bad: [[String: ServerSettingValue]] = [
            ["persisted": .bool(false)], ["readback": .bool(false)], ["runtimeApplied": .bool(false)],
            ["autosaveEnabled": .bool(false)],
            ["persisted": .int(1)], ["revision": .text("invalid")], ["baseSHA256": .text(settingsRevisionC)],
            ["source": .text("runtime-copy")], ["action": .text("get")], ["schemaVersion": .int(2)],
            ["schemaVersion": .text("1")], ["schemaVersion": .bool(true)],
            ["requestID": .text(settingsAttempt(2).requestID)], ["outcome": .text("unknown")],
            ["operation": .text("initialize")],
            ["settings": .table(settingsBaseline)],
            ["settings": .table(["linear_projects.enabled": .bool(true)])],
        ]
        for override in bad {
            let process = OpsProcessResult(exitCode: 0,
                stdout: settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", overrides: override), stderr: "")
            if case .saved = ServerListenerSettingsSaver.receipt(process, request: request) {
                XCTFail("An incomplete or mismatched receipt passed: \(override.keys)")
            }
        }
        let outerFailure = OpsProcessResult(exitCode: 1,
            stdout: settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", outerOK: false), stderr: "")
        if case .saved = ServerListenerSettingsSaver.receipt(outerFailure, request: request) { XCTFail("Helper failure passed") }
    }

    func test_reconciliation_requires_original_target_and_distinguishes_terminal_from_unknown() {
        let request = settingsRequest(set: ["daemon.coalesce_seconds": .int(21)], expectBaseSHA256: settingsRevisionA,
                                      expectedKeys: Set(settingsBaseline.keys))
        var applied = settingsBaseline; applied["daemon.coalesce_seconds"] = .int(21)
        let good = settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", reconciling: true)
        guard case .saved = ServerListenerSettingsSaver.receipt(
            OpsProcessResult(exitCode: 0, stdout: good, stderr: ""), request: request, reconciling: true) else {
            return XCTFail("A complete original-operation reconciliation did not pass")
        }
        let wrongTarget = settingsEnvelope(result: "saved", reconciling: true)
        guard case .outcomeUnknown = ServerListenerSettingsSaver.receipt(
            OpsProcessResult(exitCode: 0, stdout: wrongTarget, stderr: ""), request: request, reconciling: true) else {
            return XCTFail("A different target settled the original write")
        }
        for code in ["outcome_unknown", "request_cancelled", "request_refused", "request_superseded", "prior_result_unavailable"] {
            let cancelled = code == "request_cancelled"
            let knownOK = ["request_superseded", "prior_result_unavailable"].contains(code)
            let body = settingsEnvelope(applied, revision: settingsRevisionB, result: "failed", reconciling: true,
                overrides: ["outcome": .text(code == "outcome_unknown" ? "unknown" : (knownOK ? "ok" : "refused")),
                            "operation": .text(cancelled ? "cancelled" : "save"),
                            "cancelled": .bool(cancelled), "errorCode": .text(code)], outerOK: false)
            let result = ServerListenerSettingsSaver.receipt(OpsProcessResult(exitCode: 1, stdout: body, stderr: ""),
                                                             request: request, reconciling: true)
            if code == "outcome_unknown" {
                guard case .outcomeUnknown = result else { return XCTFail("An unknown operation was released") }
            } else {
                guard case .failed = result else { return XCTFail("A terminal operation was held or shown Saved: \(code)") }
            }
        }
    }

    func test_actual_backend_reconciliation_and_terminal_wire_envelopes_are_not_saved_by_value_alone() throws {
        let pending = try generatedSaveRequest("runtime_pending")
        for name in ["reconcile_unknown", "reconcile_saved"] {
            let wire = generatedSettingsCase(name)
            let result = ServerListenerSettingsSaver.receipt(
                OpsProcessResult(exitCode: wire.exitCode, stdout: wire.stdout, stderr: ""), request: pending, reconciling: true)
            if name == "reconcile_saved" {
                guard case .saved = result else { return XCTFail("The actual full reconciliation was refused") }
            } else {
                guard case .outcomeUnknown = result else { return XCTFail("Actual runtime uncertainty was settled by a value match") }
            }
            let invocation = try settingsBuilder().listenerSettingsReconcileInvocation(pending.attempt)
            let input = try XCTUnwrap(invocation.standardInput)
            XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self)), .table(try XCTUnwrap(wire.request)))
        }
        for name in ["superseded", "known_ok_read_failed"] {
            let known = generatedSettingsCase(name)
            guard case .failed = ServerListenerSettingsSaver.receipt(
                OpsProcessResult(exitCode: known.exitCode, stdout: known.stdout, stderr: ""),
                request: try generatedSaveRequest("save1")) else {
                return XCTFail("A terminal known success became unknown or current Saved: \(name)")
            }
        }
        let cancelled = generatedSettingsCase("cancelled")
        let document = try XCTUnwrap(cancelled.request)
        guard case .text(let id)? = document["requestID"], case .int(let created)? = document["requestCreatedAt"] else {
            return XCTFail("The actual cancellation fixture lost its identity")
        }
        var original = try generatedSaveRequest("save1")
        original.attempt = ServerSettingsAttempt(requestID: id, requestCreatedAt: created)
        guard case .failed = ServerListenerSettingsSaver.receipt(
            OpsProcessResult(exitCode: cancelled.exitCode, stdout: cancelled.stdout, stderr: ""),
            request: original, reconciling: true) else {
            return XCTFail("Durable cancellation did not settle the old request without Saved")
        }
    }

    func test_missing_save_availability_is_a_protocol_failure_instead_of_first_saved() throws {
        var applied = settingsBaseline
        applied["linear_projects.enabled"] = .bool(true)
        var outer = try XCTUnwrap(ServerSettingJSON.parse(settingsEnvelope(applied, revision: settingsRevisionB, result: "saved"))?.tableEntries)
        var document = try XCTUnwrap(outer["eavesdropperSettings"]?.tableEntries)
        document.removeValue(forKey: "autosaveEnabled")
        outer["eavesdropperSettings"] = .table(document)
        let body = try XCTUnwrap(ServerSettingValue.table(outer).json)
        let request = settingsRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA)
        let process = OpsProcessResult(exitCode: 0, stdout: body, stderr: "")
        guard case .outcomeUnknown = ServerListenerSettingsSaver.receipt(process, request: request) else {
            return XCTFail("A save receipt without next-edit availability appeared saved")
        }
    }

    func test_typed_controls_cover_all_exposed_config_and_preserve_presence_case() throws {
        let schema = try settingsSchema()
        let groups = ServerSettingsBuilder.groups(settings: settingsBaseline, allowlist: schema, editable: true,
                                                  channelNames: ["Work-In-Progress", "Builds", "Custom Team"])
        let rows = groups.flatMap(\.rows)
        XCTAssertTrue(rows.allSatisfy(\.isEditable))
        for key in ["daemon.presence_topics", "daemon.coalesce_seconds", "linear_projects.enabled",
                    "seat.CODEX.routine.url_env", "seat.CODEX.routine.key_env", "seat.CODEX.routine.auth",
                    "seat.CODEX.routine.method", "seat.CODEX.routine.header", "seat.CODEX.routine.signature_prefix",
                    "seat.CODEX.budget.usd_per_day", "seat.CODEX.live.loop_turns"] {
            XCTAssertNotNil(rows.first { $0.id == key && $0.isEditable }, key)
        }
        XCTAssertEqual(groups.first { $0.id == "linear_projects" }?.title, "Linear Project Label Sync")
        let dollars = try XCTUnwrap(rows.first { $0.id == "seat.CODEX.budget.usd_per_day" })
        XCTAssertTrue(dollars.isEditable)
        XCTAssertEqual(dollars.information, ServerSettingsCopy.usdRecordedOnly)
        let presence = try XCTUnwrap(rows.first { $0.id == "daemon.presence_topics" })
        let text = #"[["work-in-progress","Topic Case"],["custom team","Keep This"],["Unknown MiXeD","Custom Topic"]]"#
        let value = try ServerSettingsBuilder.value(text, for: presence).get()
        XCTAssertEqual(value, .list([.list([.text("Work-In-Progress"), .text("Topic Case")]),
                                    .list([.text("Custom Team"), .text("Keep This")]),
                                    .list([.text("Unknown MiXeD"), .text("Custom Topic")])]))
        XCTAssertThrowsError(try ServerSettingsBuilder.value(#"[["Builds"]]"#, for: presence).get())
        XCTAssertThrowsError(try ServerSettingsBuilder.value(#"[["","topic"]]"#, for: presence).get())
        let reference = try XCTUnwrap(rows.first { $0.id == "seat.CODEX.routine.key_env" })
        XCTAssertThrowsError(try ServerSettingsBuilder.value("OTHER_ROUTINE_KEY", for: reference).get())
        let request = try ServerSettingsBuilder.request(edits: ["seat.CODEX.routine.method": "PUT"], rows: rows, forAutosave: true).get()
        XCTAssertEqual(request.set["seat.CODEX.routine"]?.tableEntries?["auth"], .text("bearer"))
    }

    func test_partial_channel_metadata_keeps_verified_spellings_and_unknown_case() {
        let pairs: ServerSettingValue = .list([
            .list([.text("work-in-progress"), .text("Mixed Topic")]),
            .list([.text("builds"), .text("gates")]),
            .list([.text("alerts"), .text("*")]),
            .list([.text("Custom MiXeD"), .text("Keep Topic")]),
        ])
        XCTAssertEqual(ServerPresenceTopics.canonical(pairs, names: ["Policy"]), .list([
            .list([.text("Work-In-Progress"), .text("Mixed Topic")]),
            .list([.text("Builds"), .text("gates")]),
            .list([.text("Alerts"), .text("*")]),
            .list([.text("Custom MiXeD"), .text("Keep Topic")]),
        ]))
        XCTAssertEqual(ServerPresenceTopics.canonical(.list([.list([.text("builds"), .text("T")])]), names: ["BUILDS"]),
                       .list([.list([.text("BUILDS"), .text("T")])]))
    }

    func test_typed_reference_names_survive_scanning_only_in_their_own_fields() throws {
        let schema = try settingsSchema()
        for seat in ["JET", "CLAUDE", "GROK-WEB"] {
            let path = "seat.\(seat).routine"
            let code = seat.replacingOccurrences(of: "-", with: "_")
            var settings = settingsBaseline
            settings[path] = .table(["url_env": .text("ZULIP_ALERT_\(code)_ENDPOINT"),
                                     "key_env": .text("EXISTING_\(code)_ROUTINE_KEY"), "method": .text("POST")])
            let rows = ServerSettingsBuilder.groups(settings: settings, allowlist: schema, editable: true).flatMap(\.rows)
            let url = try XCTUnwrap(rows.first { $0.id == path + ".url_env" })
            XCTAssertEqual(try ServerSettingsBuilder.value("ZULIP_ALERT_\(code)_ENDPOINT", for: url).get(),
                           .text("ZULIP_ALERT_\(code)_ENDPOINT"))
            let typed = try ServerSettingsBuilder.request(edits: [path + ".method": "PUT"], rows: rows, forAutosave: true).get()
            let request = settingsRequest(set: typed.set, expectBaseSHA256: settingsRevisionA,
                                                    referenceNames: ServerSettingsBuilder.referenceNames(rows: rows))
            XCTAssertNil(request.validationProblem)
            XCTAssertNotNil(request.standardInput)
        }
        let raw = String(repeating: "A1", count: 20)
        let request = settingsRequest(set: ["seat.CLAUDE.routine": .table(["key_env": .text(raw)])],
                                               expectBaseSHA256: settingsRevisionA)
        XCTAssertEqual(request.validationProblem?.code, "secret_refused")
        XCTAssertEqual(request.validationProblem?.field, "seat.CLAUDE.routine.key_env")
        XCTAssertFalse(request.validationProblem?.sentence.contains(raw) == true)
        let wrongField = settingsRequest(set: ["seat.CLAUDE.routine": .table(["header": .text("ZULIP_ALERT_CLAUDE_ENDPOINT")])],
                                                   expectBaseSHA256: settingsRevisionA)
        XCTAssertEqual(wrongField.validationProblem?.code, "secret_refused")
    }

    func test_compact_presence_json_is_validated_as_typed_names_and_reports_bad_fields() throws {
        let rows = ServerSettingsBuilder.groups(settings: settingsBaseline, allowlist: try settingsSchema(), editable: true).flatMap(\.rows)
        let presence = try XCTUnwrap(rows.first { $0.id == "daemon.presence_topics" })
        XCTAssertEqual(try ServerSettingsBuilder.value(#"[["Builds","gates"]]"#, for: presence).get(),
                       .list([.list([.text("Builds"), .text("gates")])]))
        let coalesce = try XCTUnwrap(rows.first { $0.id == "daemon.coalesce_seconds" })
        guard case .failure(let problem) = ServerSettingsBuilder.value("999999", for: coalesce) else {
            return XCTFail("Out-of-range field was accepted")
        }
        XCTAssertTrue(problem.sentence.hasPrefix("invalid_settings: daemon.coalesce_seconds"))
        let malformed = settingsEnvelope(overrides: ["revision": .int(1)])
        XCTAssertEqual(ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 0, stdout: malformed, stderr: "")),
                       .unavailable("invalid_receipt: revision has an invalid shape."))
    }

    @MainActor
    func test_refused_fields_are_named_without_authorization_or_transport() async {
        let runner = SettingsSaveRunner()
        var authorizations = 0
        let saver = ServerListenerSettingsSaver(runner: runner, makeBuilder: { settingsBuilder() }, authorize: {
            authorizations += 1
            return .approved
        })
        let secret = String(repeating: "x9Y", count: 12)
        let request = settingsRequest(set: ["seat.CODEX.routine": .table(["header": .text(secret)])],
                                               expectBaseSHA256: settingsRevisionA)
        guard case .failed(let why, nil) = await saver.save(request) else { return XCTFail("Credential was not refused") }
        XCTAssertTrue(why.hasPrefix("secret_refused: seat.CODEX.routine.header"))
        for length in 4...secret.count { XCTAssertFalse(why.contains(String(secret.prefix(length)))) }
        XCTAssertEqual(authorizations, 0)
        let invocations = await runner.invocations
        XCTAssertTrue(invocations.isEmpty)
        let invalid = settingsRequest(set: ["daemon.coalesce_seconds": .null], expectBaseSHA256: settingsRevisionA)
        XCTAssertEqual(invalid.validationProblem?.field, "daemon.coalesce_seconds")
        XCTAssertEqual(invalid.validationProblem?.code, "invalid_settings")
    }

    func test_exit_zero_or_timeout_without_readback_is_never_saved() {
        let request = settingsRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA)
        for process in [OpsProcessResult(exitCode: 0, stdout: #"{"ok":true,"result":"done"}"#, stderr: ""),
                        OpsProcessResult(exitCode: 0, stdout: settingsEnvelope(result: "saved"), stderr: "", timedOut: true)] {
            if case .saved = ServerListenerSettingsSaver.receipt(process, request: request) {
                XCTFail("Process status substituted for a provider receipt")
            }
        }
    }

    func test_explicit_subkey_removal_preserves_other_caps_and_requires_absence() throws {
        let schema = try settingsSchema()
        var before = settingsBaseline
        before["seat.CODEX.budget"] = .table(["wakes_per_day": .int(40), "board_per_day": .int(20)])
        let rows = ServerSettingsBuilder.groups(settings: before, allowlist: schema, editable: true).flatMap(\.rows)
        let typed = try ServerSettingsBuilder.request(edits: [:], rows: rows, forAutosave: true,
                                                      removing: ["seat.CODEX.budget.wakes_per_day"]).get()
        XCTAssertEqual(typed.remove, ["seat.CODEX.budget.wakes_per_day"])
        XCTAssertEqual(typed.set["seat.CODEX.budget"], .table(["board_per_day": .int(20)]))
        let request = settingsRequest(set: typed.set, expectBaseSHA256: settingsRevisionA,
                                               expectedKeys: Set(before.keys), remove: typed.remove)
        let input = try XCTUnwrap(request.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries?["remove"],
                       .list([.text("seat.CODEX.budget.wakes_per_day")]))
        let unchanged = OpsProcessResult(exitCode: 0, stdout: settingsEnvelope(before, revision: settingsRevisionB, result: "saved"), stderr: "")
        guard case .outcomeUnknown = ServerListenerSettingsSaver.receipt(unchanged, request: request) else {
            return XCTFail("A still-present limit passed for an unset receipt")
        }
    }
}

@MainActor
final class ServerSettingsAutosaveStoreTests: XCTestCase {
    private func makeStore(_ runner: SettingsSaveRunner, _ clock: SettingsDebounceClock,
                       authorized: Bool = true, wired: Bool = true,
                       attempts: [ServerSettingsAttempt]? = nil) throws -> ServerListenerSettingsStore {
        let schema = try settingsSchema()
        let saver = ServerListenerSettingsSaver(runner: runner, makeBuilder: { settingsBuilder() },
                                                authorize: { authorized ? .approved : .notApproved("Approval canceled.") })
        var attemptIndex = 0
        return ServerListenerSettingsStore(reader: ServerListenerSettingsReader(runner: runner, makeBuilder: { settingsBuilder() }),
            saver: wired ? saver : nil, waitForDebounce: { _ in await clock.sleep() }, makeAttempt: {
                defer { attemptIndex += 1 }
                if let attempts, attemptIndex < attempts.count { return attempts[attemptIndex] }
                return settingsAttempt(attemptIndex + 1)
            }, readAllowlist: { schema })
    }

    private func afterUnconfirmedWrite(stdout: String = "", timedOut: Bool = true) async throws
        -> (ServerListenerSettingsStore, SettingsSaveRunner, SettingsDebounceClock) {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "21")
        let save = Task { await store.savePending() }
        try await runner.untilWrites(1)
        await runner.finish(stdout, timedOut: timedOut)
        await save.value
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else {
            XCTFail("Unconfirmed write did not retain a failed edit")
            throw SettingsTestWait.timedOut
        }
        return (store, runner, clock)
    }

    func test_uncertain_or_malformed_response_reconciles_original_before_a_new_restore_generation() async throws {
        var applied = settingsBaseline
        applied["daemon.coalesce_seconds"] = .int(21)
        let malformed = settingsEnvelope(applied, revision: settingsRevisionB, result: "saved",
                                         overrides: ["persisted": .text("invalid")])
        let wrongAction = settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", overrides: ["action": .text("get")])
        let failedOuter = settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", outerOK: false)
        for (stdout, timedOut) in [("", true), (malformed, false), (wrongAction, false), (failedOuter, false)] {
            for desired in [20, 21] {
                let (store, runner, clock) = try await afterUnconfirmedWrite(stdout: stdout, timedOut: timedOut)
                await runner.setRead(settingsEnvelope(applied, revision: settingsRevisionB))
                await runner.setReconcile(settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", reconciling: true))
                store.edit("daemon.coalesce_seconds", text: String(desired))
                XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], String(desired))
                let save = Task { await store.savePending() }
                try await runner.untilWrites(2)
                let writes = await runner.writes
                let sent = try XCTUnwrap(writes.last?.standardInput)
                let request = try XCTUnwrap(ServerSettingJSON.parse(String(decoding: sent, as: UTF8.self))?.tableEntries)
                XCTAssertEqual(request["expectBaseSHA256"], .text(settingsRevisionB))
                XCTAssertEqual(request["set"]?.tableEntries?["daemon.coalesce_seconds"], .int(desired))
                XCTAssertEqual(request["requestID"], .text(settingsAttempt(2).requestID))
                XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saving)
                XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], String(desired))
                let reconciles = await runner.reconciles
                let reconcileInput = try XCTUnwrap(reconciles.first?.standardInput)
                XCTAssertEqual(ServerSettingJSON.parse(String(decoding: reconcileInput, as: UTF8.self)),
                               ServerSettingJSON.parse(String(decoding: try XCTUnwrap(settingsAttempt().reconcileInput), as: UTF8.self)))
                var confirmed = applied; confirmed["daemon.coalesce_seconds"] = .int(desired)
                await runner.finish(settingsEnvelope(confirmed, revision: settingsRevisionC, result: "saved", attempt: settingsAttempt(2)))
                await save.value
                XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saved)
                store.revert(); await clock.advance()
            }
        }
    }

    func test_new_field_never_rebatches_an_older_failed_field() async throws {
        let (store, runner, clock) = try await afterUnconfirmedWrite()
        var actual = settingsBaseline
        await runner.setRead(settingsEnvelope(actual, revision: settingsRevisionB))
        await runner.setReconcile(settingsEnvelope(actual, revision: settingsRevisionB, result: "failed", reconciling: true,
            overrides: ["outcome": .text("refused"), "errorCode": .text("request_refused")], outerOK: false))
        store.edit("linear_projects.enabled", text: "On")
        let save = Task { await store.savePending() }
        try await runner.untilWrites(2)
        let writes = await runner.writes
        let sent = try XCTUnwrap(writes.last?.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: sent, as: UTF8.self))?.tableEntries?["set"],
                       .table(["linear_projects.enabled": .bool(true)]))
        actual["linear_projects.enabled"] = .bool(true)
        await runner.finish(settingsEnvelope(actual, revision: settingsRevisionC, result: "saved", attempt: settingsAttempt(2)))
        await save.value
        XCTAssertEqual(store.saveStates["linear_projects.enabled"], .saved)
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "21")
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else { return XCTFail("Older failed field was cleared or replayed") }
        store.revert(); await clock.advance()
    }

    func test_matching_get_cannot_release_unknown_and_no_retry_loops_without_an_edit() async throws {
        let (store, runner, clock) = try await afterUnconfirmedWrite()
        var actual = settingsBaseline; actual["daemon.coalesce_seconds"] = .int(21)
        await runner.setRead(settingsEnvelope(actual, revision: settingsRevisionB))
        await store.refresh()
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "21")
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else { return XCTFail("A GET appeared Saved") }
        store.edit("daemon.coalesce_seconds", text: "20")
        await store.savePending()
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "20")
        XCTAssertNotNil(store.groups.flatMap(\.rows).first { $0.id == "daemon.coalesce_seconds" })
        let before = await runner.invocations.count
        await clock.advance()
        await store.savePending()
        let after = await runner.invocations.count
        let writes = await runner.writes
        XCTAssertEqual(writes.count, 1)
        let reconciles = await runner.reconciles
        XCTAssertEqual(reconciles.count, 1)
        XCTAssertEqual(after, before)
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else { return XCTFail("Failed reconciliation discarded the status") }
        store.revert(); await clock.advance()
    }

    func test_cancelled_absent_original_requires_fresh_get_and_preserves_drafts_when_it_fails() async throws {
        let (store, runner, clock) = try await afterUnconfirmedWrite()
        await runner.setReconcile(settingsEnvelope(result: "failed", reconciling: true,
            overrides: ["operation": .text("cancelled"), "outcome": .text("refused"),
                        "errorCode": .text("request_cancelled"), "cancelled": .bool(true),
                        "persisted": .bool(false), "readback": .bool(false)], outerOK: false))
        await runner.setRead(#"{"ok":false,"result":"failed"}"#)
        store.edit("daemon.coalesce_seconds", text: "20")
        store.edit("linear_projects.enabled", text: "On")
        await store.savePending()
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "20")
        XCTAssertEqual(store.drafts["linear_projects.enabled"], "On")
        XCTAssertNotNil(store.refreshProblem)
        let writes = await runner.writes
        XCTAssertEqual(writes.count, 1)
        let calls = await runner.invocations.count
        await clock.advance(); await store.savePending()
        let after = await runner.invocations.count
        XCTAssertEqual(calls, after)
        store.revert(); await clock.advance()
    }

    func test_limit_removal_can_be_cancelled_before_the_write() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        var actual = settingsBaseline
        actual["seat.CODEX.budget"] = .table(["wakes_per_day": .int(40)])
        await runner.setRead(settingsEnvelope(actual))
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.removeOverride("seat.CODEX.budget.wakes_per_day")
        store.cancelRemoval("seat.CODEX.budget.wakes_per_day")
        await store.savePending()
        let writes = await runner.writes
        XCTAssertTrue(writes.isEmpty)
        XCTAssertTrue(store.removalDrafts.isEmpty)
        XCTAssertNil(store.drafts["seat.CODEX.budget.wakes_per_day"])
        store.revert(); await clock.advance()
    }

    func test_keep_limit_restores_intent_across_a_late_success_or_partial_unset() async throws {
        for partial in [false, true] {
            let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
            var actual = settingsBaseline
            actual["seat.CODEX.budget"] = .table(["wakes_per_day": .int(40), "board_per_day": .int(20)])
            await runner.setRead(settingsEnvelope(actual))
            let store = try makeStore(runner, clock)
            await store.refresh()
            store.removeOverride("seat.CODEX.budget.wakes_per_day")
            let unset = Task { await store.savePending() }
            try await runner.untilWrites(1)
            if !partial { store.cancelRemoval("seat.CODEX.budget.wakes_per_day") }
            actual["seat.CODEX.budget"] = .table(["board_per_day": .int(20)])
            await runner.finish(settingsEnvelope(actual, revision: settingsRevisionB, result: partial ? "failed" : "saved", outerOK: !partial), exit: partial ? 1 : 0)
            await unset.value
            if partial { store.cancelRemoval("seat.CODEX.budget.wakes_per_day") }
            await runner.setRead(settingsEnvelope(actual, revision: settingsRevisionB))
            if partial {
                await runner.setReconcile(settingsEnvelope(actual, revision: settingsRevisionB, result: "saved", reconciling: true))
            }
            XCTAssertEqual(store.drafts["seat.CODEX.budget.wakes_per_day"], "40")
            let restore = Task { await store.savePending() }
            try await runner.untilWrites(2)
            let writes = await runner.writes
            let sent = try XCTUnwrap(writes.last?.standardInput)
            let request = try XCTUnwrap(ServerSettingJSON.parse(String(decoding: sent, as: UTF8.self))?.tableEntries)
            XCTAssertEqual(request["expectBaseSHA256"], .text(settingsRevisionB))
            XCTAssertEqual(request["set"]?.tableEntries?["seat.CODEX.budget"],
                           .table(["wakes_per_day": .int(40), "board_per_day": .int(20)]))
            actual["seat.CODEX.budget"] = .table(["wakes_per_day": .int(40), "board_per_day": .int(20)])
            await runner.finish(settingsEnvelope(actual, revision: settingsRevisionC, result: "saved", attempt: settingsAttempt(2)))
            await restore.value
            XCTAssertEqual(store.saveStates["seat.CODEX.budget.wakes_per_day"], .saved)
            XCTAssertTrue(store.removalDrafts.isEmpty)
            store.revert(); await clock.advance()
        }
    }

    func test_actual_backend_wire_supports_consecutive_saves_and_retains_partial_failure() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        await runner.setRead(generatedSettingsCase("get").stdout)
        let attempts = try ["save1", "save2", "runtime_pending"].map { name -> ServerSettingsAttempt in
            let request = try XCTUnwrap(generatedSettingsCase(name).request)
            guard case .text(let requestID)? = request["requestID"], case .int(let created)? = request["requestCreatedAt"] else {
                throw SettingsTestWait.timedOut
            }
            return ServerSettingsAttempt(requestID: requestID, requestCreatedAt: created)
        }
        let store = try makeStore(runner, clock, attempts: attempts)
        await store.refresh()
        for (index, name) in ["save1", "save2", "runtime_pending"].enumerated() {
            let actual = generatedSettingsCase(name)
            let request = try XCTUnwrap(actual.request)
            let set = try XCTUnwrap(request["set"]?.tableEntries)
            for (key, value) in set { store.edit(key, text: try XCTUnwrap(value.json)) }
            let save = Task { await store.savePending() }
            try await runner.untilWrites(index + 1)
            let writes = await runner.writes
            let sent = try XCTUnwrap(writes.last?.standardInput)
            XCTAssertEqual(ServerSettingJSON.parse(String(decoding: sent, as: UTF8.self)), .table(request))
            await runner.finish(actual.stdout, exit: actual.exitCode)
            await save.value
            for key in set.keys {
                if name == "runtime_pending" {
                    guard case .failed? = store.saveStates[key] else { return XCTFail("Actual partial response appeared saved") }
                    XCTAssertNotNil(store.drafts[key])
                } else {
                    XCTAssertEqual(store.saveStates[key], .saved)
                    XCTAssertNil(store.drafts[key])
                }
            }
        }
        await clock.advance()
        await store.savePending()
        let writes = await runner.writes
        XCTAssertEqual(writes.count, 3)
        store.revert(); await clock.advance()
    }

    func test_actual_unactivated_and_initialize_receipts_never_enable_a_save() async throws {
        for name in ["unactivated_get", "initialized"] {
            let actual = generatedSettingsCase(name)
            let answer = try XCTUnwrap(ServerSettingsAnswer(stdout: actual.stdout))
            XCTAssertEqual(answer.autosaveEnabled, false)
            let request = settingsRequest(set: ["daemon.coalesce_seconds": .number(2.0)], expectBaseSHA256: settingsRevisionA)
            if case .saved = ServerListenerSettingsSaver.receipt(
                OpsProcessResult(exitCode: actual.exitCode, stdout: actual.stdout, stderr: ""), request: request) {
                XCTFail("Inactive initialization was treated as an applied save")
            }
        }
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        await runner.setRead(generatedSettingsCase("unactivated_get").stdout)
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "2.0")
        await store.savePending()
        let writes = await runner.writes
        XCTAssertTrue(writes.isEmpty)
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else { return XCTFail("Inactive source enabled autosave") }
        store.revert(); await clock.advance()
    }

    func test_fast_edits_coalesce_before_one_actual_save() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "21")
        try await clock.untilSleeps(1)
        store.edit("daemon.coalesce_seconds", text: "22")
        try await clock.untilSleeps(2)
        let before = await runner.writes
        XCTAssertTrue(before.isEmpty)
        let saved = expectation(description: "Verified autosave")
        let observation = store.$saveStates.first { $0["daemon.coalesce_seconds"] == .saved }.sink { _ in saved.fulfill() }
        await clock.advance()
        try await runner.untilWrites(1)
        let writes = await runner.writes
        let input = try XCTUnwrap(writes.first?.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries?["set"]?.tableEntries?["daemon.coalesce_seconds"], .int(22))
        var current = settingsBaseline; current["daemon.coalesce_seconds"] = .int(22)
        await runner.finish(settingsEnvelope(current, revision: settingsRevisionB, result: "saved"))
        await fulfillment(of: [saved], timeout: 5)
        observation.cancel()
        XCTAssertNil(store.drafts["daemon.coalesce_seconds"])
        store.revert(); await clock.advance()
    }

    func test_restore_of_baseline_survives_late_success_and_uses_new_revision() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "21")
        let first = Task { await store.savePending() }
        try await runner.untilWrites(1)
        store.edit("daemon.coalesce_seconds", text: "20")
        await store.savePending()
        let firstWriteCount = await runner.writes.count
        XCTAssertEqual(firstWriteCount, 1)
        var current = settingsBaseline; current["daemon.coalesce_seconds"] = .int(21)
        await runner.finish(settingsEnvelope(current, revision: settingsRevisionB, result: "saved"))
        await first.value
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "20")
        XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saving)
        let second = Task { await store.savePending() }
        try await runner.untilWrites(2)
        let writes = await runner.writes
        let input = try XCTUnwrap(writes.last?.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries?["expectBaseSHA256"], .text(settingsRevisionB))
        current["daemon.coalesce_seconds"] = .int(20)
        await runner.finish(settingsEnvelope(current, revision: settingsRevisionC, result: "saved", attempt: settingsAttempt(2)))
        await second.value
        XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saved)
        store.revert(); await clock.advance()
    }

    func test_runtime_pending_retains_draft_and_never_blindly_replays() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "21")
        let save = Task { await store.savePending() }
        try await runner.untilWrites(1)
        store.edit("daemon.coalesce_seconds", text: "22")
        var current = settingsBaseline; current["daemon.coalesce_seconds"] = .int(21)
        await runner.finish(settingsEnvelope(current, revision: settingsRevisionB, result: "failed",
            overrides: ["runtimeApplied": .bool(false), "errorCode": .text("runtime_pending")], outerOK: false), exit: 1)
        await save.value
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "22")
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else { return XCTFail("Partial save appeared successful") }
        await clock.advance()
        await store.savePending()
        let failedWriteCount = await runner.writes.count
        XCTAssertEqual(failedWriteCount, 1)
        store.revert(); await clock.advance()
    }

    func test_auth_denied_and_unwired_instances_keep_drafts_without_a_write() async throws {
        for wired in [false, true] {
            let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
            let store = try makeStore(runner, clock, authorized: false, wired: wired)
            await store.refresh()
            store.edit("linear_projects.enabled", text: "On")
            await store.savePending()
            XCTAssertEqual(store.drafts["linear_projects.enabled"], "On")
            guard case .failed? = store.saveStates["linear_projects.enabled"] else { return XCTFail("Missing failed status") }
            let writes = await runner.writes
            XCTAssertTrue(writes.isEmpty)
            store.revert(); await clock.advance()
        }
    }

    func test_edit_during_late_read_is_retained_and_no_write_overlaps_the_read() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        await runner.holdNextRead()
        let read = Task { await store.refresh() }
        try await runner.untilReadWaiting()
        store.edit("daemon.coalesce_seconds", text: "21")
        await store.savePending()
        let writesBeforeRead = await runner.writes
        XCTAssertTrue(writesBeforeRead.isEmpty)
        await runner.finishRead(settingsEnvelope())
        await read.value
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "21")
        XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saving)
        let save = Task { await store.savePending() }
        try await runner.untilWrites(1)
        var current = settingsBaseline; current["daemon.coalesce_seconds"] = .int(21)
        await runner.finish(settingsEnvelope(current, revision: settingsRevisionB, result: "saved"))
        await save.value
        XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saved)
        store.revert(); await clock.advance()
    }

    func test_failed_queued_read_keeps_rows_and_pending_drafts_visible() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "21")
        let save = Task { await store.savePending() }
        try await runner.untilWrites(1)
        await store.refresh()
        await runner.setRead(#"{"ok":false,"result":"failed","detail":"Read unavailable"}"#)
        await runner.finish("", exit: 124, timedOut: true)
        await save.value
        XCTAssertNotNil(store.refreshProblem)
        XCTAssertNotNil(store.groups.flatMap(\.rows).first { $0.id == "daemon.coalesce_seconds" })
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "21")
        guard case .failed? = store.saveStates["daemon.coalesce_seconds"] else { return XCTFail("Uncertain write was not failed") }
        await store.savePending()
        let writes = await runner.writes
        XCTAssertEqual(writes.count, 1)
        store.revert(); await clock.advance()
    }

    func test_runtime_pending_retains_removal_intent_without_replay() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        var before = settingsBaseline
        before["seat.CODEX.budget"] = .table(["wakes_per_day": .int(40), "board_per_day": .int(20)])
        await runner.setRead(settingsEnvelope(before))
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.removeOverride("seat.CODEX.budget.wakes_per_day")
        let save = Task { await store.savePending() }
        try await runner.untilWrites(1)
        before["seat.CODEX.budget"] = .table(["board_per_day": .int(20)])
        await runner.finish(settingsEnvelope(before, revision: settingsRevisionB, result: "failed",
            overrides: ["runtimeApplied": .bool(false), "errorCode": .text("runtime_pending")], outerOK: false), exit: 1)
        await save.value
        XCTAssertTrue(store.removalDrafts.contains("seat.CODEX.budget.wakes_per_day"))
        guard case .failed? = store.saveStates["seat.CODEX.budget.wakes_per_day"] else {
            return XCTFail("A partially applied unset appeared successful")
        }
        await clock.advance()
        await store.savePending()
        let writes = await runner.writes
        XCTAssertEqual(writes.count, 1)
        XCTAssertNotNil(store.groups.flatMap(\.rows).first { $0.id == "seat.CODEX.budget.wakes_per_day" })
        store.revert(); await clock.advance()
    }

    func test_unset_is_distinct_from_zero_and_receipts_clear_only_confirmed_intent() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        var before = settingsBaseline
        before["seat.CODEX.budget"] = .table(["wakes_per_day": .int(40), "board_per_day": .int(20)])
        await runner.setRead(settingsEnvelope(before))
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.removeOverride("seat.CODEX.budget.wakes_per_day")
        let unset = Task { await store.savePending() }
        try await runner.untilWrites(1)
        XCTAssertTrue(store.removalDrafts.contains("seat.CODEX.budget.wakes_per_day"))
        before["seat.CODEX.budget"] = .table(["board_per_day": .int(20)])
        await runner.finish(settingsEnvelope(before, revision: settingsRevisionB, result: "saved"))
        await unset.value
        XCTAssertTrue(store.removalDrafts.isEmpty)
        XCTAssertEqual(store.saveStates["seat.CODEX.budget.wakes_per_day"], .saved)
        store.edit("seat.CODEX.budget.wakes_per_day", text: "0")
        let zero = Task { await store.savePending() }
        try await runner.untilWrites(2)
        let writes = await runner.writes
        let input = try XCTUnwrap(writes.last?.standardInput)
        let document = try XCTUnwrap(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries)
        XCTAssertNil(document["remove"])
        XCTAssertEqual(document["set"]?.tableEntries?["seat.CODEX.budget"]?.tableEntries?["wakes_per_day"], .int(0))
        before["seat.CODEX.budget"] = .table(["board_per_day": .int(20), "wakes_per_day": .int(0)])
        await runner.finish(settingsEnvelope(before, revision: settingsRevisionC, result: "saved", attempt: settingsAttempt(2)))
        await zero.value
        XCTAssertEqual(store.saveStates["seat.CODEX.budget.wakes_per_day"], .saved)
        store.revert(); await clock.advance()
    }
}
