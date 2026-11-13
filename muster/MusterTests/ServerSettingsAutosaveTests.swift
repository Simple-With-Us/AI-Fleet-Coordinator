import Combine
import XCTest

private let settingsRevisionA = String(repeating: "a", count: 64)
private let settingsRevisionB = String(repeating: "b", count: 64)
private let settingsRevisionC = String(repeating: "c", count: 64)
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

private func settingsEnvelope(_ settings: [String: ServerSettingValue] = settingsBaseline,
                              revision: String = settingsRevisionA, result: String = "read",
                              overrides: [String: ServerSettingValue] = [:], outerOK: Bool = true) -> String {
    var document: [String: ServerSettingValue] = [
        "schemaVersion": .int(1), "tool": .text("eavesdropper-settings"),
        "action": .text(result == "read" ? "get" : "save"), "ok": .bool(outerOK), "result": .text(result),
        "source": .text("infisical"), "revision": .text(revision), "baseSHA256": .text(revision),
        "persisted": .bool(true), "readback": .bool(true), "runtimeApplied": .bool(true),
        "autosaveEnabled": .bool(true), "settings": .table(settings),
        "channelNames": .list([.text("Work-In-Progress"), .text("Builds"), .text("Custom Team")]),
    ]
    document.merge(overrides, uniquingKeysWith: { _, new in new })
    return ServerSettingValue.table(["ok": .bool(outerOK), "result": .text(result),
                                      "eavesdropperSettings": .table(document)]).json ?? ""
}

private actor SettingsSaveRunner: OpsRunning {
    private var pending: [CheckedContinuation<OpsProcessResult, Never>] = []
    private var waiting: [(Int, CheckedContinuation<Void, Never>)] = []
    private var pauseRead = false
    private var pendingRead: CheckedContinuation<OpsProcessResult, Never>?
    private var readWaiters: [CheckedContinuation<Void, Never>] = []
    private(set) var invocations: [OpsInvocation] = []
    private(set) var writes: [OpsInvocation] = []
    var readAnswer = settingsEnvelope()

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        invocations.append(invocation)
        if invocation.arguments.contains("--plan") {
            if pauseRead {
                pauseRead = false
                return await withCheckedContinuation { continuation in
                    pendingRead = continuation
                    let ready = readWaiters; readWaiters = []
                    for waiter in ready { waiter.resume() }
                }
            }
            return OpsProcessResult(exitCode: 0, stdout: readAnswer, stderr: "")
        }
        return await withCheckedContinuation { continuation in
            pending.append(continuation)
            writes.append(invocation)
            let ready = waiting.filter { writes.count >= $0.0 }
            waiting.removeAll { writes.count >= $0.0 }
            for (_, waiter) in ready { waiter.resume() }
        }
    }

    func untilWrites(_ count: Int) async {
        if writes.count >= count { return }
        await withCheckedContinuation { waiting.append((count, $0)) }
    }

    func finish(_ answer: String, exit: Int32 = 0, timedOut: Bool = false) {
        pending.removeFirst().resume(returning: OpsProcessResult(exitCode: exit, stdout: answer, stderr: "", timedOut: timedOut))
    }

    func setRead(_ answer: String) { readAnswer = answer }
    func holdNextRead() { pauseRead = true }
    func untilReadWaiting() async {
        if pendingRead != nil { return }
        await withCheckedContinuation { readWaiters.append($0) }
    }
    func finishRead(_ answer: String) {
        readAnswer = answer
        pendingRead?.resume(returning: OpsProcessResult(exitCode: 0, stdout: answer, stderr: ""))
        pendingRead = nil
    }
}

private actor SettingsDebounceClock {
    private var sleepers: [CheckedContinuation<Void, Never>] = []
    private var waiting: [(Int, CheckedContinuation<Void, Never>)] = []
    private var count = 0

    func sleep() async {
        await withCheckedContinuation { continuation in
            sleepers.append(continuation)
            count += 1
            let ready = waiting.filter { count >= $0.0 }
            waiting.removeAll { count >= $0.0 }
            for (_, waiter) in ready { waiter.resume() }
        }
    }

    func untilSleeps(_ target: Int) async {
        if count >= target { return }
        await withCheckedContinuation { waiting.append((target, $0)) }
    }

    func advance() {
        let ready = sleepers
        sleepers = []
        for sleeper in ready { sleeper.resume() }
    }
}

final class ServerSettingsAutosaveReceiptTests: XCTestCase {
    func test_save_uses_existing_forced_command_and_revision_fenced_stdin() throws {
        let request = ServerSettingsSaveRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA)
        let invocation = try settingsBuilder().listenerSettingsSaveInvocation(request)
        XCTAssertEqual(invocation.executable, OpsControlRoute.ssh)
        XCTAssertEqual(Array(invocation.arguments.suffix(7)),
                       [OpsControlRoute.serverAlias, "--", "server", "eavesdropper-settings", "server", "--yes", "--json"])
        let text = String(decoding: try XCTUnwrap(invocation.standardInput), as: UTF8.self)
        let document = try XCTUnwrap(ServerSettingJSON.parse(text)?.tableEntries)
        XCTAssertEqual(document["action"], .text("save"))
        XCTAssertEqual(document["expectBaseSHA256"], .text(settingsRevisionA))
        XCTAssertEqual(document["set"], .table(request.set))
        XCTAssertFalse(invocation.arguments.contains { $0.contains(settingsRevisionA) })
    }

    func test_saved_requires_exact_readback_and_all_provider_runtime_receipts() {
        let request = ServerSettingsSaveRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA,
                                               expectedKeys: Set(settingsBaseline.keys))
        var applied = settingsBaseline
        applied["linear_projects.enabled"] = .bool(true)
        let good = OpsProcessResult(exitCode: 0, stdout: settingsEnvelope(applied, revision: settingsRevisionB, result: "saved"), stderr: "")
        guard case .saved = ServerListenerSettingsSaver.receipt(good, request: request) else { return XCTFail("No saved receipt") }
        let bad: [[String: ServerSettingValue]] = [
            ["persisted": .bool(false)], ["readback": .bool(false)], ["runtimeApplied": .bool(false)],
            ["persisted": .int(1)], ["revision": .text("invalid")], ["baseSHA256": .text(settingsRevisionC)],
            ["source": .text("runtime-copy")], ["action": .text("get")], ["schemaVersion": .int(2)],
            ["settings": .table(settingsBaseline)],
            ["settings": .table(["linear_projects.enabled": .bool(true)])],
        ]
        for override in bad {
            let process = OpsProcessResult(exitCode: 0,
                stdout: settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", overrides: override), stderr: "")
            guard case .failed = ServerListenerSettingsSaver.receipt(process, request: request) else {
                XCTFail("An incomplete or mismatched receipt passed: \(override.keys)")
                continue
            }
        }
        let outerFailure = OpsProcessResult(exitCode: 1,
            stdout: settingsEnvelope(applied, revision: settingsRevisionB, result: "saved", outerOK: false), stderr: "")
        guard case .failed = ServerListenerSettingsSaver.receipt(outerFailure, request: request) else { return XCTFail("Helper failure passed") }
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

    func test_exit_zero_or_timeout_without_readback_is_never_saved() {
        let request = ServerSettingsSaveRequest(set: ["linear_projects.enabled": .bool(true)], expectBaseSHA256: settingsRevisionA)
        for process in [OpsProcessResult(exitCode: 0, stdout: #"{"ok":true,"result":"done"}"#, stderr: ""),
                        OpsProcessResult(exitCode: 0, stdout: settingsEnvelope(result: "saved"), stderr: "", timedOut: true)] {
            guard case .failed(_, nil) = ServerListenerSettingsSaver.receipt(process, request: request) else {
                XCTFail("Process status substituted for a provider receipt")
                continue
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
        let request = ServerSettingsSaveRequest(set: typed.set, expectBaseSHA256: settingsRevisionA,
                                               expectedKeys: Set(before.keys), remove: typed.remove)
        let input = try XCTUnwrap(request.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries?["remove"],
                       .list([.text("seat.CODEX.budget.wakes_per_day")]))
        let unchanged = OpsProcessResult(exitCode: 0, stdout: settingsEnvelope(before, revision: settingsRevisionB, result: "saved"), stderr: "")
        guard case .failed = ServerListenerSettingsSaver.receipt(unchanged, request: request) else {
            return XCTFail("A still-present limit passed for an unset receipt")
        }
    }
}

@MainActor
final class ServerSettingsAutosaveStoreTests: XCTestCase {
    private func makeStore(_ runner: SettingsSaveRunner, _ clock: SettingsDebounceClock,
                       authorized: Bool = true, wired: Bool = true) throws -> ServerListenerSettingsStore {
        let schema = try settingsSchema()
        let saver = ServerListenerSettingsSaver(runner: runner, makeBuilder: { settingsBuilder() },
                                                authorize: { authorized ? .approved : .notApproved("Approval canceled.") })
        return ServerListenerSettingsStore(reader: ServerListenerSettingsReader(runner: runner, makeBuilder: { settingsBuilder() }),
            saver: wired ? saver : nil, waitForDebounce: { _ in await clock.sleep() }, readAllowlist: { schema })
    }

    func test_fast_edits_coalesce_before_one_actual_save() async throws {
        let runner = SettingsSaveRunner(), clock = SettingsDebounceClock()
        let store = try makeStore(runner, clock)
        await store.refresh()
        store.edit("daemon.coalesce_seconds", text: "21")
        await clock.untilSleeps(1)
        store.edit("daemon.coalesce_seconds", text: "22")
        await clock.untilSleeps(2)
        let before = await runner.writes
        XCTAssertTrue(before.isEmpty)
        let saved = expectation(description: "Verified autosave")
        let observation = store.$saveStates.first { $0["daemon.coalesce_seconds"] == .saved }.sink { _ in saved.fulfill() }
        await clock.advance()
        await runner.untilWrites(1)
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
        await runner.untilWrites(1)
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
        await runner.untilWrites(2)
        let writes = await runner.writes
        let input = try XCTUnwrap(writes.last?.standardInput)
        XCTAssertEqual(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries?["expectBaseSHA256"], .text(settingsRevisionB))
        current["daemon.coalesce_seconds"] = .int(20)
        await runner.finish(settingsEnvelope(current, revision: settingsRevisionC, result: "saved"))
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
        await runner.untilWrites(1)
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
        await runner.untilReadWaiting()
        store.edit("daemon.coalesce_seconds", text: "21")
        await store.savePending()
        let writesBeforeRead = await runner.writes
        XCTAssertTrue(writesBeforeRead.isEmpty)
        await runner.finishRead(settingsEnvelope())
        await read.value
        XCTAssertEqual(store.drafts["daemon.coalesce_seconds"], "21")
        XCTAssertEqual(store.saveStates["daemon.coalesce_seconds"], .saving)
        let save = Task { await store.savePending() }
        await runner.untilWrites(1)
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
        await runner.untilWrites(1)
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
        await runner.untilWrites(1)
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
        await runner.untilWrites(1)
        XCTAssertTrue(store.removalDrafts.contains("seat.CODEX.budget.wakes_per_day"))
        before["seat.CODEX.budget"] = .table(["board_per_day": .int(20)])
        await runner.finish(settingsEnvelope(before, revision: settingsRevisionB, result: "saved"))
        await unset.value
        XCTAssertTrue(store.removalDrafts.isEmpty)
        XCTAssertEqual(store.saveStates["seat.CODEX.budget.wakes_per_day"], .saved)
        store.edit("seat.CODEX.budget.wakes_per_day", text: "0")
        let zero = Task { await store.savePending() }
        await runner.untilWrites(2)
        let writes = await runner.writes
        let input = try XCTUnwrap(writes.last?.standardInput)
        let document = try XCTUnwrap(ServerSettingJSON.parse(String(decoding: input, as: UTF8.self))?.tableEntries)
        XCTAssertNil(document["remove"])
        XCTAssertEqual(document["set"]?.tableEntries?["seat.CODEX.budget"]?.tableEntries?["wakes_per_day"], .int(0))
        before["seat.CODEX.budget"] = .table(["board_per_day": .int(20), "wakes_per_day": .int(0)])
        await runner.finish(settingsEnvelope(before, revision: settingsRevisionC, result: "saved"))
        await zero.value
        XCTAssertEqual(store.saveStates["seat.CODEX.budget.wakes_per_day"], .saved)
        store.revert(); await clock.advance()
    }
}
