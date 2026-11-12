import XCTest

// The server Eavesdropper's settings (AFC-362):  what Muster offers, what one Show Plan sends to
// `server eavesdropper-settings server`, and how it reads the editor's answers.
//
// No test here starts a process:  the builder is only asked for the invocation it would run.

private let exampleRoot = URL(fileURLWithPath: "/tmp/example-repo", isDirectory: true)
private let exampleHome = URL(fileURLWithPath: "/Users/example", isDirectory: true)
private let planHash = String(repeating: "a", count: 64)

private func builder() -> OpsCommandBuilder {
    OpsCommandBuilder(repoRoot: exampleRoot, home: exampleHome, sshAuthSocket: nil)
}

/// A small allowlist copy, in the editor's own shape, with keys that must never be offered.
private let allowlistJSON = """
{
  "schemaVersion": 1,
  "applyEnabled": false,
  "seat": {
    "enabled": {"type": "bool"},
    "wake": {"type": "enum", "values": ["inbox", "http"]},
    "creds": {"type": "string", "pattern": "[a-z]+", "maxLength": 8},
    "routine": {"type": "table", "keys": {
      "url_env": {"type": "seat-env"},
      "method": {"type": "enum", "values": ["POST", "PUT"]},
      "timeout_seconds": {"type": "number", "min": 1, "max": 60}
    }},
    "budget": {"type": "table", "keys": {
      "wakes_per_day": {"type": "int", "min": 0, "max": 100000},
      "wakes_per_hour": {"type": "int", "min": 0, "max": 10000},
      "usd_per_day": {"type": "number", "min": 0, "max": 100}
    }}
  },
  "tables": {
    "daemon": {
      "coalesce_seconds": {"type": "number", "min": 0, "max": 3600},
      "held_back_catch_up": {"type": "bool"},
      "owner_user_id": {"type": "int", "min": 0, "max": 10},
      "presence_topics": {"type": "pairs"}
    },
    "linear_comments": {"enabled": {"type": "bool"}},
    "webhook": {"callback_url": {"type": "string", "pattern": ".*", "maxLength": 64}}
  }
}
"""

/// A get, as the helper answers it over ssh.
private let getAnswer = """
{"ok": true, "verb": "server eavesdropper-settings", "result": "read", "detail": "6 allowlisted settings",
 "eavesdropperSettings": {"schemaVersion": 1, "tool": "eavesdropper-settings", "action": "get", "ok": true,
  "result": "read", "applyEnabled": true, "baseSHA256": "\(String(repeating: "c", count: 64))",
  "settings": {
   "daemon.coalesce_seconds": 30.0,
   "daemon.held_back_catch_up": false,
   "linear_comments.enabled": true,
   "seat.CLAUDE.budget": {"wakes_per_day": 40, "wakes_per_hour": 6, "usd_per_day": 5.0},
   "seat.CLAUDE.enabled": true,
   "seat.CLAUDE.mystery": 3
  }}}
"""

final class ServerListenerSettingsTests: XCTestCase {
    private func allowlist() throws -> ServerSettingsAllowlist {
        try XCTUnwrap(ServerSettingsAllowlist.parse(Data(allowlistJSON.utf8)))
    }

    private func answer() throws -> ServerSettingsAnswer {
        try XCTUnwrap(ServerSettingsAnswer(stdout: getAnswer))
    }

    private func rows() throws -> [ServerSettingRow] {
        ServerSettingsBuilder.groups(settings: try answer().settings, allowlist: try allowlist()).flatMap(\.rows)
    }

    private func row(_ id: String) throws -> ServerSettingRow {
        try XCTUnwrap(try rows().first { $0.id == id }, id)
    }

    // MARK: What is offered

    func test_the_allowlist_copy_never_keeps_a_pin_or_a_name_that_reads_as_a_secret() throws {
        let list = try allowlist()
        XCTAssertNil(list.seat["creds"])
        XCTAssertNil(list.tables["daemon"]?["owner_user_id"])
        XCTAssertNil(list.tables["webhook"], "a table of only secret-shaped names is dropped")
        XCTAssertEqual(list.seat["wake"], .choice(["inbox", "http"]))
        XCTAssertNotNil(list.tables["daemon"]?["coalesce_seconds"])
    }

    func test_the_checkouts_own_allowlist_copy_reads() throws {
        // muster/MusterTests/<this file> -> the repo root is two folders up from MusterTests.
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let list = try XCTUnwrap(ServerSettingsAllowlist.read(at: ServerSettingsAllowlist.url(repoRoot: root)))
        XCTAssertNotNil(list.seat["budget"])
        XCTAssertNotNil(list.tables["daemon"]?["held_back_catch_up"])
        for name in ServerSettingsAllowlist.neverOfferedSeatKeys { XCTAssertNil(list.seat[name], name) }
        for name in ServerSettingsAllowlist.neverOfferedDaemonKeys { XCTAssertNil(list.tables["daemon"]?[name], name) }
    }

    func test_a_key_outside_the_allowlist_is_not_offered_and_says_why() throws {
        // The server answered a key this checkout's copy does not list.
        let mystery = try row("seat.CLAUDE.mystery")
        XCTAssertFalse(mystery.isEditable)
        XCTAssertEqual(mystery.readOnlyReason, ServerSettingsCopy.notOnThisCopy)
        // A pin is shown, read only, with its reason, and never with a value.
        let creds = try row("seat.CLAUDE.creds")
        XCTAssertFalse(creds.isEditable)
        XCTAssertNil(creds.current)
        XCTAssertEqual(creds.readOnlyReason, ServerSettingsCopy.neverOffered)
        let owner = try row("daemon.owner_user_id")
        XCTAssertFalse(owner.isEditable)
        XCTAssertEqual(owner.readOnlyReason, ServerSettingsCopy.neverOffered)
        // The cap that is never enforced, and a list of pairs, are read only too.
        XCTAssertEqual(try row("seat.CLAUDE.budget.usd_per_day").readOnlyReason, ServerSettingsCopy.notEnforced)
        XCTAssertEqual(try row("daemon.presence_topics").readOnlyReason, ServerSettingsCopy.pairs)
        // A seat with no routine table is never given one.
        XCTAssertEqual(try row("seat.CLAUDE.routine.timeout_seconds").readOnlyReason, ServerSettingsCopy.noRoutine)
        XCTAssertEqual(try row("seat.CLAUDE.routine.url_env").readOnlyReason, ServerSettingsCopy.variableName)
        // Every read-only row says why.
        for row in try rows() where !row.isEditable {
            XCTAssertNotNil(row.readOnlyReason, row.id)
        }
    }

    func test_an_edit_of_a_read_only_row_plans_nothing() throws {
        let rows = try rows()
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.CLAUDE.mystery": "4"], rows: rows),
                       .failure(.readOnly(try row("seat.CLAUDE.mystery").title)))
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.CLAUDE.creds": "x"], rows: rows),
                       .failure(.readOnly(try row("seat.CLAUDE.creds").title)))
        // A key Muster may never send is refused at the request, whatever built it.
        XCTAssertFalse(ServerListenerSettingsRequest.isOfferableKey("seat.CLAUDE.creds"))
        XCTAssertFalse(ServerListenerSettingsRequest.isOfferableKey("daemon.owner_user_id"))
        XCTAssertFalse(ServerListenerSettingsRequest.isOfferableKey("webhook.callback_url"))
        XCTAssertFalse(ServerListenerSettingsRequest(set: ["seat.CLAUDE.creds": .text("x")]).isSendable)
        XCTAssertThrowsError(try builder().invocation(
            for: .serverListenerSettings(ServerListenerSettingsRequest(set: ["daemon.owner_user_id": .int(1)])),
            mode: .plan))
    }

    func test_a_value_out_of_range_plans_nothing() throws {
        let result = ServerSettingsBuilder.request(edits: ["seat.CLAUDE.budget.wakes_per_day": "-1"], rows: try rows())
        guard case .failure(.notAValue) = result else { return XCTFail("\(result)") }
    }

    // MARK: What one Show Plan sends

    func test_a_server_seat_edit_produces_the_editors_plan_command() throws {
        let request = try ServerSettingsBuilder.request(
            edits: ["seat.CLAUDE.budget.wakes_per_day": "50", "daemon.held_back_catch_up": "On"],
            rows: try rows()
        ).get()
        // The budget goes whole, every sub-key the server holds kept, so nothing is dropped.
        XCTAssertEqual(request.set["seat.CLAUDE.budget"],
                       .table(["wakes_per_day": .int(50), "wakes_per_hour": .int(6), "usd_per_day": .number(5.0)]))
        XCTAssertEqual(request.set["daemon.held_back_catch_up"], .bool(true))
        XCTAssertNil(request.expectPlanSHA256)

        let invocation = try builder().invocation(for: .serverListenerSettings(request), mode: .plan)
        XCTAssertEqual(invocation.executable, OpsControlRoute.ssh)
        XCTAssertEqual(Array(invocation.arguments.suffix(7)),
                       ["fleet-ops-ctl", "--", "server", "eavesdropper-settings", "server", "--plan", "--json"])
        // The change goes on standard input, never into a word of the command.
        XCTAssertFalse(invocation.arguments.contains { $0.contains("wakes_per_day") })
        let stdin = String(decoding: try XCTUnwrap(invocation.standardInput), as: UTF8.self)
        XCTAssertEqual(stdin, #"{"set":{"daemon.held_back_catch_up":true,"seat.CLAUDE.budget":{"usd_per_day":5.0,"wakes_per_day":50,"wakes_per_hour":6}}}"#)
    }

    func test_an_unchanged_edit_plans_nothing() throws {
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["daemon.coalesce_seconds": "30"], rows: try rows()),
                       .failure(.nothingChanged))
    }

    func test_an_apply_carries_the_approved_hash_or_is_never_built() throws {
        let request = ServerListenerSettingsRequest(set: ["daemon.held_back_catch_up": .bool(true)])
        XCTAssertThrowsError(try builder().invocation(for: .serverListenerSettings(request), mode: .yes))
        var approved = request
        approved.expectPlanSHA256 = planHash
        let run = try builder().invocation(for: .serverListenerSettings(approved), mode: .yes)
        XCTAssertEqual(Array(run.arguments.suffix(3)), ["server", "--yes", "--json"])
        let stdin = String(decoding: try XCTUnwrap(run.standardInput), as: UTF8.self)
        XCTAssertEqual(stdin, #"{"expectPlanSHA256":"\#(planHash)","set":{"daemon.held_back_catch_up":true}}"#)
        // A hash that is not one is never sent.
        approved.expectPlanSHA256 = "not-a-hash"
        XCTAssertThrowsError(try builder().invocation(for: .serverListenerSettings(approved), mode: .yes))
    }

    func test_the_read_is_a_get_with_nothing_on_standard_input() {
        let read = builder().listenerSettingsReadInvocation()
        XCTAssertNil(read.standardInput)
        XCTAssertEqual(Array(read.arguments.suffix(7)),
                       ["fleet-ops-ctl", "--", "server", "eavesdropper-settings", "server", "--plan", "--json"])
    }

    func test_the_action_asks_for_touch_id_like_every_write() {
        let action = OpsAction.serverListenerSettings(ServerListenerSettingsRequest(set: ["daemon.held_back_catch_up": .bool(true)]))
        XCTAssertFalse(action.changesNothing)
        XCTAssertTrue(action.runsOnServer)
    }

    // MARK: Reading the editor's answers

    func test_a_get_reads_with_whole_numbers_and_floats_kept_apart() throws {
        let answer = try answer()
        XCTAssertEqual(answer.result, "read")
        XCTAssertEqual(answer.applyEnabled, true)
        XCTAssertEqual(answer.settings["daemon.coalesce_seconds"], .number(30.0))
        XCTAssertEqual(answer.settings["seat.CLAUDE.enabled"], .bool(true))
        guard case .read = ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 0, stdout: getAnswer, stderr: "")) else {
            return XCTFail("a get should read")
        }
    }

    func test_a_plan_the_eavesdropper_would_refuse_cannot_be_approved() throws {
        let refused = ServerSettingsAnswer(document: [
            "result": .text("planned"), "ok": .bool(true), "planSHA256": .text(planHash),
            "check": .table(["ok": .bool(false), "problems": .list([.text("bad cap")])]),
        ])
        XCTAssertEqual(ServerSettingsAnswer.outcome(refused, helperKind: .planned, mode: .plan, detail: "").0, .refused)
        let noHash = ServerSettingsAnswer(document: [
            "result": .text("planned"), "ok": .bool(true),
            "check": .table(["ok": .bool(true)]),
        ])
        XCTAssertEqual(ServerSettingsAnswer.outcome(noHash, helperKind: .planned, mode: .plan, detail: "").0, .failed)
        let good = ServerSettingsAnswer(document: [
            "result": .text("planned"), "ok": .bool(true), "planSHA256": .text(planHash),
            "check": .table(["ok": .bool(true)]),
        ])
        XCTAssertEqual(ServerSettingsAnswer.outcome(good, helperKind: .planned, mode: .plan, detail: "").0, .planned)
        XCTAssertEqual(ServerSettingsAnswer.outcome(nil, helperKind: .planned, mode: .plan, detail: "").0, .failed)
    }

    func test_a_failed_apply_that_was_not_rolled_back_is_unknown() {
        let failed = ServerSettingsAnswer(document: ["result": .text("failed"), "ok": .bool(false)])
        XCTAssertEqual(ServerSettingsAnswer.outcome(failed, helperKind: .failed, mode: .yes, detail: "").0, .unknown)
        let rolledBack = ServerSettingsAnswer(document: ["result": .text("failed"), "ok": .bool(false), "rolledBack": .bool(true)])
        XCTAssertEqual(ServerSettingsAnswer.outcome(rolledBack, helperKind: .failed, mode: .yes, detail: "").0, .failed)
    }

    func test_the_value_writer_matches_json() {
        XCTAssertEqual(ServerSettingValue.number(30).json, "30.0")
        XCTAssertEqual(ServerSettingValue.int(30).json, "30")
        XCTAssertEqual(ServerSettingValue.text("a\"b").json, #""a\"b""#)
        XCTAssertEqual(ServerSettingJSON.parse("[1, 2.5, \"x\", null]"),
                       .list([.int(1), .number(2.5), .text("x"), .null]))
        XCTAssertNil(ServerSettingJSON.parse("{} trailing"))
    }
}
