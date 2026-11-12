import XCTest

// The server Eavesdropper's settings (AFC-362):  what Muster offers, what one Show Plan sends to
// `server eavesdropper-settings server`, and how it reads the editor's answers.
//
// No test here starts a process:  the builder is only asked for the invocation it would run.

private let exampleRoot = URL(fileURLWithPath: "/tmp/example-repo", isDirectory: true)
private let exampleHome = URL(fileURLWithPath: "/Users/example", isDirectory: true)
private let planHash = String(repeating: "a", count: 64)
private let backupName = "listener.toml.bak-settings-20261112T173000Z"

private func builder() -> OpsCommandBuilder {
    OpsCommandBuilder(repoRoot: exampleRoot, home: exampleHome, sshAuthSocket: nil)
}

/// This checkout's root:  muster/MusterTests/<this file> is two folders below it.
private func checkoutRoot() -> URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
}

/// A value of the type a spec names, for a settings map that holds every allowlisted key.
private func sampleValue(_ spec: ServerSettingSpec) -> ServerSettingValue {
    switch spec {
    case .bool: return .bool(true)
    case .whole(let low, _): return .int(Int(low))
    case .number(let low, _): return .number(low)
    case .choice(let values): return values.first.map(ServerSettingValue.text) ?? .null
    case .text: return .text("x")
    case .pairs: return .list([.list([.text("fleet"), .text("presence")])])
    case .seatEnv: return .text("CLAUDE_ROUTINE_URL")
    case .table(let keys): return .table(keys.mapValues(sampleValue))
    }
}

/// A small allowlist copy, in the editor's own shape, with keys that must never be offered.  The
/// routine's `retries` is not in the real copy:  it is here so a routine sub-key that has no
/// reason of its own shows the no-routine reason on a seat with no routine table.
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
      "timeout_seconds": {"type": "number", "min": 1, "max": 60},
      "retries": {"type": "int", "min": 0, "max": 5}
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
   "seat.CLAUDE.routine": {"url_env": "CLAUDE_ROUTINE_URL", "method": "POST", "timeout_seconds": 15},
   "seat.JET.enabled": true,
   "seat.CLAUDE.mystery": 3
  }}}
"""

/// A get in the shape the real server answers (Nov 12, 2026, keys and types only):  the helper's
/// own top-level keys (`commands`, `detail`, `output`, `target`, `verb`) beside the editor's
/// document, which carries `file`;  only five `[daemon]` keys, whole-number timings and a list of
/// pairs;  a seat with no `enabled` line;  and a `budget` table with two of its ten sub-keys and
/// no `live` table.
private let realShapeGet = #"""
{"commands": [], "detail": "14 allowlisted settings",
 "eavesdropperSettings": {"action": "get", "applyEnabled": false,
  "baseSHA256": "\#(String(repeating: "c", count: 64))", "file": "listener.toml", "ok": true,
  "result": "read", "schemaVersion": 1,
  "settings": {
   "daemon.coalesce_max_seconds": 120,
   "daemon.coalesce_seconds": 30,
   "daemon.owner_coalesce_seconds": 5,
   "daemon.presence_topics": [["fleet", "presence"], ["Work-In-Progress", "fleet"]],
   "daemon.stale_after_minutes": 30,
   "linear_comments.enabled": true,
   "linear_projects.enabled": false,
   "listener_api.enabled": true,
   "notes_queue.enabled": false,
   "seat.CLAUDE.budget": {"board_per_day": 20, "usd_per_day": 2.5},
   "seat.CLAUDE.enabled": true,
   "seat.CLAUDE.routine": {"auth": "bearer", "header": "Authorization", "key_env": "CLAUDE_ROUTINE_KEY",
                           "method": "POST", "timeout_seconds": 20, "url_env": "CLAUDE_ROUTINE_URL"},
   "seat.CLAUDE.wake": "http",
   "seat.JET.wake": "inbox"
  },
  "tool": "eavesdropper-settings"},
 "ok": true, "output": "daemon.coalesce_seconds = 30", "result": "read", "target": "server",
 "verb": "server eavesdropper-settings"}
"""#

/// The helper's answer around one editor document, as `fleet-ops-control.py` wraps it.
private func helperAnswer(_ editor: String, ok: Bool, result: String, errorCode: String? = nil, detail: String) -> String {
    let code = errorCode.map { #","errorCode":"\#($0)""# } ?? ""
    return #"{"ok":\#(ok),"verb":"server eavesdropper-settings","target":"server","commands":[],"result":"\#(result)"\#(code),"detail":"\#(detail)","eavesdropperSettings":\#(editor)}"#
}

/// The editor's refusal (`run_request`, `except Refusal`).
private func editorRefusal(action: String, code: String, detail: String) -> String {
    #"{"schemaVersion":1,"tool":"eavesdropper-settings","action":"\#(action)","file":"listener.toml","ok":false,"result":"refused","errorCode":"\#(code)","detail":"\#(detail)"}"#
}

/// The editor's failed apply (`run_request`, `except StepFailed` and `except OSError`).
private func editorFailure(code: String, detail: String, rolledBack: Bool, replaced: Bool, backup: String?) -> String {
    let named = backup.map { #","backup":"\#($0)""# } ?? ""
    return #"{"schemaVersion":1,"tool":"eavesdropper-settings","action":"apply","file":"listener.toml","ok":false,"result":"failed","errorCode":"\#(code)","detail":"\#(detail)","rolledBack":\#(rolledBack),"replaced":\#(replaced)\#(named)}"#
}

/// The editor's applied answer, with the reload's outcome and the backup it made.
private let editorApplied = #"""
{"schemaVersion":1,"tool":"eavesdropper-settings","action":"apply","file":"listener.toml","ok":true,"result":"applied",
 "baseSHA256":"\#(String(repeating: "c", count: 64))","newSHA256":"\#(String(repeating: "d", count: 64))",
 "planSHA256":"\#(String(repeating: "a", count: 64))",
 "changes":[{"key":"seat.CLAUDE.enabled","set":true,"from":true,"to":false,"removes":[]}],
 "diff":["--- listener.toml","+++ listener.toml","@@ -3 +3 @@","-enabled = true","+enabled = false"],
 "check":{"ok":true,"problems":[],"warnings":0},
 "backup":"listener.toml.bak-settings-20261112T173000Z",
 "reload":{"ok":true,"confirmed":true,"code":"reloaded"}}
"""#

/// The editor's plan of two changes:  a whole routine table that drops a sub-key (`removes`), and a
/// `wake` line the file did not have (`from` null), with the Eavesdropper's own check refusing it.
private let editorPlanRefusedByCheck = #"""
{"schemaVersion":1,"tool":"eavesdropper-settings","action":"plan","file":"listener.toml","ok":true,"result":"planned",
 "baseSHA256":"\#(String(repeating: "c", count: 64))","newSHA256":"\#(String(repeating: "d", count: 64))",
 "planSHA256":"\#(String(repeating: "a", count: 64))",
 "changes":[
  {"key":"seat.CLAUDE.routine","set":true,
   "from":{"cost_usd":0.1,"method":"POST","timeout_seconds":15,"url_env":"CLAUDE_ROUTINE_URL"},
   "to":{"method":"POST","timeout_seconds":15,"url_env":"CLAUDE_ROUTINE_URL"},
   "removes":["seat.CLAUDE.routine.cost_usd"]},
  {"key":"seat.JET.wake","set":false,"from":null,"to":"http","removes":[]}
 ],
 "diff":["--- listener.toml","+++ listener.toml","@@ -9 +8,0 @@","-cost_usd = 0.1","@@ -14,0 +14 @@","+wake = \"http\""],
 "check":{"ok":false,"problems":["seat.JET.routine must be a table (url_env, key_env, method, header, auth) when wake = \"http\""],"warnings":0}}
"""#

final class ServerListenerSettingsTests: XCTestCase {
    private func allowlist() throws -> ServerSettingsAllowlist {
        try XCTUnwrap(ServerSettingsAllowlist.parse(Data(allowlistJSON.utf8)))
    }

    private func realAllowlist() throws -> ServerSettingsAllowlist {
        try XCTUnwrap(ServerSettingsAllowlist.read(at: ServerSettingsAllowlist.url(repoRoot: checkoutRoot())))
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
        let list = try realAllowlist()
        // The only keys Muster may edit are there, with the types the daemon reads.
        XCTAssertEqual(list.seat["enabled"], .bool)
        XCTAssertEqual(list.seat["wake"], .choice(["inbox", "http"]))
        XCTAssertNotNil(list.seat["routine"])
        XCTAssertNotNil(list.seat["budget"])
        XCTAssertNotNil(list.tables["daemon"]?["held_back_catch_up"])
        for name in ServerSettingsAllowlist.neverOfferedSeatKeys { XCTAssertNil(list.seat[name], name) }
        for name in ServerSettingsAllowlist.neverOfferedDaemonKeys { XCTAssertNil(list.tables["daemon"]?[name], name) }
    }

    /// Pinned from the checkout's real allowlist copy, with every key it lists given a value:  only
    /// each seat's `enabled` and `wake` are editable, so a key added to the allowlist later never
    /// becomes editable by accident.
    func test_the_real_allowlist_makes_only_enabled_and_wake_editable() throws {
        let list = try realAllowlist()
        var settings: [String: ServerSettingValue] = [:]
        for seat in ["CLAUDE", "JET"] {
            for (name, spec) in list.seat { settings["seat.\(seat).\(name)"] = sampleValue(spec) }
        }
        for (table, specs) in list.tables {
            for (name, spec) in specs { settings["\(table).\(name)"] = sampleValue(spec) }
        }
        XCTAssertNotNil(settings["seat.CLAUDE.routine"]?.tableEntries, "the seats have a routine table")
        let rows = ServerSettingsBuilder.groups(settings: settings, allowlist: list).flatMap(\.rows)
        let editable = Set(rows.filter(\.isEditable).map(\.id))
        XCTAssertEqual(editable, ["seat.CLAUDE.enabled", "seat.CLAUDE.wake", "seat.JET.enabled", "seat.JET.wake"])
        // A routine's timing and cost are knobs, even on a seat that has a routine.
        for id in ["seat.CLAUDE.routine.timeout_seconds", "seat.CLAUDE.routine.cost_usd"] {
            let knob = try XCTUnwrap(rows.first { $0.id == id }, id)
            XCTAssertEqual(knob.readOnlyReason, ServerSettingsCopy.knobHome, id)
        }
        for row in rows where !row.isEditable {
            XCTAssertNotNil(row.readOnlyReason, row.id)
        }
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
        // A routine's timing is a knob whether or not the seat has a routine.
        XCTAssertEqual(try row("seat.JET.routine.timeout_seconds").readOnlyReason, ServerSettingsCopy.knobHome)
        XCTAssertEqual(try row("seat.CLAUDE.routine.timeout_seconds").readOnlyReason, ServerSettingsCopy.knobHome)
        // A seat with no routine table is never given one.
        XCTAssertEqual(try row("seat.JET.routine.retries").readOnlyReason, ServerSettingsCopy.noRoutine)
        XCTAssertEqual(try row("seat.CLAUDE.routine.url_env").readOnlyReason, ServerSettingsCopy.variableName)
        XCTAssertEqual(try row("seat.CLAUDE.routine.method").readOnlyReason, ServerSettingsCopy.routerContract)
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

    /// Owner's rule (Nov 11, 2026):  a knob's home is Infisical, so Muster never writes one into the
    /// server's listener file.  Only a seat's `enabled` and `wake` are editable here.
    func test_a_knob_is_shown_and_never_written_through_the_settings_editor() throws {
        for id in ["daemon.coalesce_seconds", "daemon.held_back_catch_up", "linear_comments.enabled",
                   "seat.CLAUDE.budget.wakes_per_day", "seat.CLAUDE.budget.wakes_per_hour",
                   "seat.CLAUDE.routine.timeout_seconds"] {
            let knob = try row(id)
            XCTAssertFalse(knob.isEditable, id)
            XCTAssertEqual(knob.readOnlyReason, ServerSettingsCopy.knobHome, id)
            XCTAssertNotNil(knob.current, id)  // still shown
        }
        XCTAssertEqual(try row("seat.CLAUDE.routine.timeout_seconds").current, .int(15))
        // A more exact reason still wins, and the routing structure stays editable.
        XCTAssertEqual(try row("seat.CLAUDE.budget.usd_per_day").readOnlyReason, ServerSettingsCopy.notEnforced)
        XCTAssertTrue(try row("seat.CLAUDE.enabled").isEditable)
        XCTAssertEqual(try row("seat.CLAUDE.enabled").control, .toggle)
        XCTAssertTrue(try row("seat.CLAUDE.wake").isEditable)
        XCTAssertEqual(try row("seat.CLAUDE.wake").control, .choice(["inbox", "http"]))
        for edits in [["daemon.held_back_catch_up": "On"], ["seat.CLAUDE.budget.wakes_per_day": "50"],
                      ["seat.CLAUDE.routine.timeout_seconds": "20"]] {
            guard case .failure(.readOnly) = ServerSettingsBuilder.request(edits: edits, rows: try rows()) else {
                return XCTFail("a knob edit was planned:  \(edits)")
            }
        }
        XCTAssertEqual(ServerSettingsBuilder.seatStructureKeys, ["enabled", "wake"])
    }

    /// The request gate refuses every key but a seat's `enabled` and `wake`, whatever built the
    /// request, so no knob reaches the server even past the rows.
    func test_only_a_seats_enabled_and_wake_are_sendable_and_every_knob_key_is_refused() throws {
        let refused: [String: ServerSettingValue] = [
            "daemon.coalesce_seconds": .number(30),
            "daemon.held_back_catch_up": .bool(true),
            "linear_comments.enabled": .bool(false),
            "listener_api.enabled": .bool(true),
            "seat.CLAUDE.budget": .table(["wakes_per_day": .int(10)]),
            "seat.CLAUDE.live": .table(["per_hour": .int(1)]),
            "seat.CLAUDE.routine": .table(["timeout_seconds": .int(20)]),
            "seat.CLAUDE.routine.timeout_seconds": .int(20),
            "seat.claude.enabled": .bool(false),
        ]
        for (key, value) in refused {
            XCTAssertFalse(ServerListenerSettingsRequest.isOfferableKey(key), key)
            let request = ServerListenerSettingsRequest(set: [key: value])
            XCTAssertFalse(request.isSendable, key)
            XCTAssertThrowsError(try builder().invocation(for: .serverListenerSettings(request), mode: .plan), key)
            var approved = request
            approved.expectPlanSHA256 = planHash
            XCTAssertThrowsError(try builder().invocation(for: .serverListenerSettings(approved), mode: .yes), key)
        }
        // One refused key sinks a request that also holds an allowed one.
        XCTAssertFalse(ServerListenerSettingsRequest(
            set: ["seat.CLAUDE.enabled": .bool(false), "daemon.coalesce_seconds": .number(30)]).isSendable)
        let accepted: [String: ServerSettingValue] = [
            "seat.CLAUDE.enabled": .bool(false),
            "seat.CLAUDE.wake": .text("http"),
            "seat.BF-HOUSEKEEPER.wake": .text("inbox"),
        ]
        for (key, value) in accepted {
            XCTAssertTrue(ServerListenerSettingsRequest.isOfferableKey(key), key)
            let request = ServerListenerSettingsRequest(set: [key: value])
            XCTAssertTrue(request.isSendable, key)
            XCTAssertNoThrow(try builder().invocation(for: .serverListenerSettings(request), mode: .plan), key)
        }
    }

    func test_row_labels_are_title_case() throws {
        XCTAssertEqual(try row("seat.CLAUDE.budget.wakes_per_day").title, "Budget:  Wakes Per Day")
        XCTAssertEqual(try row("seat.CLAUDE.budget.usd_per_day").title, "Budget:  USD Per Day")
        XCTAssertEqual(try row("daemon.coalesce_seconds").title, "Coalesce Seconds")
        XCTAssertEqual(ServerSettingsCopy.rowTitle("url_env"), "URL Env")
    }

    func test_a_value_the_allowlist_does_not_take_plans_nothing() throws {
        let rows = try rows()
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.CLAUDE.wake": "pager"], rows: rows),
                       .failure(.notAValue("Wake must be one of inbox, http.")))
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.CLAUDE.enabled": "maybe"], rows: rows),
                       .failure(.notAValue("Enabled must be on or off.")))
        // One bad value sinks the whole request, so nothing is half planned.
        let mixed = ServerSettingsBuilder.request(edits: ["seat.CLAUDE.enabled": "Off", "seat.CLAUDE.wake": "pager"], rows: rows)
        guard case .failure(.notAValue) = mixed else { return XCTFail("\(mixed)") }
    }

    /// A seat with no `enabled` or `wake` line is on and woken by inbox, as the daemon reads it
    /// (`scripts/simple_tools/config.py`), so the panel draws that and plans against it.
    func test_a_seat_with_no_enabled_or_wake_line_shows_the_daemons_default_and_plans_against_it() throws {
        let settings: [String: ServerSettingValue] = ["seat.AMP.budget": .table(["wakes_per_day": .int(5)])]
        let rows = ServerSettingsBuilder.groups(settings: settings, allowlist: try allowlist()).flatMap(\.rows)
        let enabled = try XCTUnwrap(rows.first { $0.id == "seat.AMP.enabled" })
        XCTAssertNil(enabled.current, "the file sets no value, so a plan's old value reads as not set")
        XCTAssertEqual(enabled.defaultValue, .bool(true))
        XCTAssertEqual(enabled.effective, .bool(true))
        XCTAssertEqual(enabled.currentText, "On")
        XCTAssertTrue(enabled.showsDefault)
        XCTAssertEqual(enabled.defaultNote, ServerSettingsCopy.daemonDefault)
        XCTAssertTrue(enabled.isEditable)
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.AMP.enabled": "On"], rows: rows), .failure(.nothingChanged))
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.AMP.enabled": "Off"], rows: rows),
                       .success(ServerListenerSettingsRequest(set: ["seat.AMP.enabled": .bool(false)])))

        let wake = try XCTUnwrap(rows.first { $0.id == "seat.AMP.wake" })
        XCTAssertNil(wake.current)
        XCTAssertEqual(wake.effective, .text("inbox"))
        XCTAssertEqual(wake.currentText, "inbox")
        XCTAssertEqual(wake.defaultNote, ServerSettingsCopy.daemonDefault)
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.AMP.wake": "inbox"], rows: rows), .failure(.nothingChanged))
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.AMP.wake": "http"], rows: rows),
                       .success(ServerListenerSettingsRequest(set: ["seat.AMP.wake": .text("http")])))

        // A value the file sets is shown as it is, with no note, and a knob has no default.
        let set = try row("seat.CLAUDE.enabled")
        XCTAssertFalse(set.showsDefault)
        XCTAssertNil(set.defaultNote)
        let budget = try XCTUnwrap(rows.first { $0.id == "seat.AMP.budget.wakes_per_day" })
        XCTAssertNil(budget.defaultValue)
        XCTAssertNil(budget.defaultNote)
        let unread = try XCTUnwrap(rows.first { $0.id == "seat.AMP.budget.wakes_per_hour" })
        XCTAssertEqual(unread.currentText, "Not read")
    }

    // MARK: What one Show Plan sends

    func test_a_server_seat_edit_produces_the_editors_plan_command() throws {
        let request = try ServerSettingsBuilder.request(
            edits: ["seat.CLAUDE.wake": "http", "seat.CLAUDE.enabled": "Off"],
            rows: try rows()
        ).get()
        XCTAssertEqual(request.set, ["seat.CLAUDE.enabled": .bool(false), "seat.CLAUDE.wake": .text("http")])
        XCTAssertNil(request.expectPlanSHA256)

        let invocation = try builder().invocation(for: .serverListenerSettings(request), mode: .plan)
        XCTAssertEqual(invocation.executable, OpsControlRoute.ssh)
        XCTAssertEqual(Array(invocation.arguments.suffix(7)),
                       ["fleet-ops-ctl", "--", "server", "eavesdropper-settings", "server", "--plan", "--json"])
        // The change goes on standard input, never into a word of the command.
        XCTAssertFalse(invocation.arguments.contains { $0.contains("seat.CLAUDE") })
        let stdin = String(decoding: try XCTUnwrap(invocation.standardInput), as: UTF8.self)
        XCTAssertEqual(stdin, #"{"set":{"seat.CLAUDE.enabled":false,"seat.CLAUDE.wake":"http"}}"#)
    }

    func test_an_unchanged_edit_plans_nothing() throws {
        XCTAssertEqual(ServerSettingsBuilder.request(edits: ["seat.CLAUDE.enabled": "On"], rows: try rows()),
                       .failure(.nothingChanged))
    }

    func test_an_apply_carries_the_approved_hash_or_is_never_built() throws {
        let request = ServerListenerSettingsRequest(set: ["seat.CLAUDE.enabled": .bool(false)])
        XCTAssertThrowsError(try builder().invocation(for: .serverListenerSettings(request), mode: .yes))
        var approved = request
        approved.expectPlanSHA256 = planHash
        let run = try builder().invocation(for: .serverListenerSettings(approved), mode: .yes)
        XCTAssertEqual(Array(run.arguments.suffix(3)), ["server", "--yes", "--json"])
        XCTAssertEqual(run.timeout, OpsControlRoute.serverListenerSettingsRunTimeout)
        let stdin = String(decoding: try XCTUnwrap(run.standardInput), as: UTF8.self)
        XCTAssertEqual(stdin, #"{"expectPlanSHA256":"\#(planHash)","set":{"seat.CLAUDE.enabled":false}}"#)
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

    /// The action is a write that runs on the server.  Touch ID itself is asked in the action sheet,
    /// which no test here drives.
    func test_the_settings_action_is_a_write_that_runs_on_the_server() {
        let action = OpsAction.serverListenerSettings(ServerListenerSettingsRequest(set: ["seat.CLAUDE.enabled": .bool(false)]))
        XCTAssertFalse(action.changesNothing)
        XCTAssertTrue(action.runsOnServer)
    }

    // MARK: Reading the editor's answers

    func test_a_get_reads_with_whole_numbers_and_floats_kept_apart() throws {
        let answer = try answer()
        XCTAssertEqual(answer.result, "read")
        XCTAssertEqual(answer.applyEnabled, true)
        XCTAssertEqual(answer.settings["daemon.coalesce_seconds"], .number(30.0))
        XCTAssertEqual(answer.settings["seat.CLAUDE.budget"]?.tableEntries?["wakes_per_day"], .int(40))
        XCTAssertEqual(answer.settings["seat.CLAUDE.budget"]?.tableEntries?["usd_per_day"], .number(5.0))
        XCTAssertEqual(answer.settings["seat.CLAUDE.routine"]?.tableEntries?["timeout_seconds"], .int(15))
        XCTAssertEqual(answer.settings["seat.CLAUDE.mystery"], .int(3))
        XCTAssertEqual(answer.settings["seat.CLAUDE.enabled"], .bool(true))
        guard case .read = ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 0, stdout: getAnswer, stderr: "")) else {
            return XCTFail("a get should read")
        }
    }

    /// The three shapes the real server's answer has that a hand-made fixture may not:  top-level
    /// keys the parser does not know, keys the allowlist offers that the server did not send, and a
    /// `budget` table that holds two of its ten sub-keys.
    func test_a_get_in_the_real_servers_shape_reads_and_draws() throws {
        XCTAssertEqual(OpsResultParser.decode(realShapeGet)?.verb, "server eavesdropper-settings")
        guard case .read(let answer) = ServerListenerSettingsReader.reading(
            OpsProcessResult(exitCode: 0, stdout: realShapeGet, stderr: "")) else {
            return XCTFail("a get in the real shape should read")
        }
        XCTAssertEqual(answer.applyEnabled, false)
        XCTAssertEqual(answer.baseSHA256, String(repeating: "c", count: 64))
        XCTAssertEqual(answer.settings.count, 14)
        // The daemon's timings arrive as whole numbers and stay whole.
        for key in ["daemon.coalesce_seconds", "daemon.coalesce_max_seconds", "daemon.owner_coalesce_seconds",
                    "daemon.stale_after_minutes"] {
            guard case .int(_)? = answer.settings[key] else {
                return XCTFail("\(key) is \(String(describing: answer.settings[key]))")
            }
        }
        XCTAssertEqual(answer.settings["daemon.coalesce_seconds"], .int(30))
        XCTAssertEqual(answer.settings["daemon.presence_topics"],
                       .list([.list([.text("fleet"), .text("presence")]), .list([.text("Work-In-Progress"), .text("fleet")])]))

        let rows = ServerSettingsBuilder.groups(settings: answer.settings, allowlist: try realAllowlist()).flatMap(\.rows)
        func shown(_ id: String) throws -> ServerSettingRow { try XCTUnwrap(rows.first { $0.id == id }, id) }
        // A key the allowlist offers and the server did not send:  a row with no value, read only.
        for id in ["daemon.held_alert_minutes", "daemon.held_back_catch_up", "daemon.host_wake_load_per_core",
                   "daemon.peer_broadcast_reminder_minutes"] {
            let unsent = try shown(id)
            XCTAssertNil(unsent.current, id)
            XCTAssertEqual(unsent.currentText, "Not read", id)
            XCTAssertFalse(unsent.isEditable, id)
        }
        XCTAssertEqual(try shown("daemon.presence_topics").currentText, "[fleet, presence], [Work-In-Progress, fleet]")
        // A budget with two of its ten sub-keys:  those two are shown, the rest read as not read.
        XCTAssertEqual(rows.filter { $0.settingKey == "seat.CLAUDE.budget" }.count, 10)
        XCTAssertEqual(try shown("seat.CLAUDE.budget.board_per_day").current, .int(20))
        XCTAssertEqual(try shown("seat.CLAUDE.budget.usd_per_day").current, .number(2.5))
        XCTAssertNil(try shown("seat.CLAUDE.budget.wakes_per_hour").current)
        // No seat sends `live`:  its rows are there, with no value.
        let live = rows.filter { $0.settingKey == "seat.CLAUDE.live" }
        XCTAssertEqual(live.count, 5)
        XCTAssertTrue(live.allSatisfy { $0.current == nil && !$0.isEditable })
        // A routine's sub-keys the server did not send (cost_usd, signature_prefix) are there too.
        XCTAssertNil(try shown("seat.CLAUDE.routine.cost_usd").current)
        XCTAssertNil(try shown("seat.CLAUDE.routine.signature_prefix").current)
        XCTAssertEqual(try shown("seat.CLAUDE.routine.signature_prefix").readOnlyReason, ServerSettingsCopy.routerContract)
        XCTAssertEqual(try shown("seat.CLAUDE.routine.timeout_seconds").current, .int(20))
        // A seat with no `enabled` line is drawn on;  its `wake` is what the server sent.
        XCTAssertNil(try shown("seat.JET.enabled").current)
        XCTAssertEqual(try shown("seat.JET.enabled").currentText, "On")
        XCTAssertEqual(try shown("seat.JET.wake").current, .text("inbox"))
        XCTAssertEqual(try shown("seat.CLAUDE.wake").current, .text("http"))
        XCTAssertEqual(Set(rows.filter(\.isEditable).map(\.id)),
                       ["seat.CLAUDE.enabled", "seat.CLAUDE.wake", "seat.JET.enabled", "seat.JET.wake"])
    }

    func test_a_failed_read_is_unavailable_and_says_why() {
        XCTAssertEqual(ServerListenerSettingsReader.reading(
            OpsProcessResult(exitCode: 15, stdout: "", stderr: "", timedOut: true)),
            .unavailable("The server did not answer in time, so its settings are not known."))
        XCTAssertEqual(ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 255, stdout: "", stderr: "")),
                       .unavailable("The server could not be reached over the fleet-ops-ctl route."))
        // The helper refused before the editor ran, so there is no settings document.
        let helperOnly = #"{"ok":false,"verb":"server eavesdropper-settings","result":"refused","detail":"eavesdropper-settings is not offered on this route"}"#
        XCTAssertEqual(ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 2, stdout: helperOnly, stderr: "")),
                       .unavailable("The server gave no settings:  eavesdropper-settings is not offered on this route"))
        // The editor answered, and not with a read.
        let detail = "the live file is not UTF-8"
        let refused = helperAnswer(editorRefusal(action: "get", code: "file_not_toml", detail: detail),
                                   ok: false, result: "refused", errorCode: "file_not_toml", detail: detail)
        XCTAssertEqual(ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 2, stdout: refused, stderr: "")),
                       .unavailable("The server refused the read:  \(detail)"))
        let planned = helperAnswer(#"{"ok":true,"result":"planned","planSHA256":"\#(planHash)"}"#,
                                   ok: true, result: "planned", detail: "1 setting(s) would change")
        XCTAssertEqual(ServerListenerSettingsReader.reading(OpsProcessResult(exitCode: 0, stdout: planned, stderr: "")),
                       .unavailable("The server refused the read:  planned"))
    }

    func test_a_full_plan_document_reads_its_changes_diff_and_check() throws {
        let stdout = helperAnswer(editorPlanRefusedByCheck, ok: true, result: "planned",
                                  detail: "2 setting(s) would change; the Eavesdropper would NOT take the result")
        let plan = try XCTUnwrap(ServerSettingsAnswer(stdout: stdout))
        XCTAssertEqual(plan.result, "planned")
        XCTAssertEqual(plan.planSHA256, planHash)
        XCTAssertEqual(plan.changes.map(\.key), ["seat.CLAUDE.routine", "seat.JET.wake"])
        XCTAssertEqual(plan.changes[0].removes, ["seat.CLAUDE.routine.cost_usd"])
        XCTAssertEqual(plan.changes[0].from?.tableEntries?["cost_usd"], .number(0.1))
        XCTAssertEqual(plan.changes[0].to.tableEntries?["timeout_seconds"], .int(15))
        XCTAssertEqual(plan.changes[1].removes, [])
        XCTAssertNil(plan.changes[1].from, "a null from is a setting the file did not have")
        XCTAssertEqual(plan.changes[1].line, "seat.JET.wake:  not set \u{2192} http")
        XCTAssertEqual(plan.diff, ["--- listener.toml", "+++ listener.toml", "@@ -9 +8,0 @@", "-cost_usd = 0.1",
                                   "@@ -14,0 +14 @@", "+wake = \"http\""])
        XCTAssertEqual(plan.checkOK, false)
        let problem = "seat.JET.routine must be a table (url_env, key_env, method, header, auth) when wake = \"http\""
        XCTAssertEqual(plan.checkProblems, [problem])
        XCTAssertNil(plan.reloadCode)
        let outcome = ServerSettingsAnswer.outcome(plan, helperKind: .planned, mode: .plan, detail: "")
        XCTAssertEqual(outcome.0, .refused)
        XCTAssertTrue(outcome.1.hasSuffix(problem), outcome.1)

        // An apply's answer carries the reload's code and the backup it made.
        let applied = try XCTUnwrap(ServerSettingsAnswer(stdout: helperAnswer(
            editorApplied, ok: true, result: "applied", detail: "1 setting(s) changed; reloaded")))
        XCTAssertEqual(applied.reloadCode, "reloaded")
        XCTAssertEqual(applied.backup, backupName)
        XCTAssertNil(applied.replaced)
        XCTAssertEqual(applied.changes.first?.line, "seat.CLAUDE.enabled:  On \u{2192} Off")
    }

    func test_a_failed_applys_backup_and_replaced_flag_are_read_and_a_strange_backup_name_is_dropped() throws {
        func failed(backup: String?, replaced: Bool) throws -> ServerSettingsAnswer {
            try XCTUnwrap(ServerSettingsAnswer(stdout: helperAnswer(
                editorFailure(code: "reload_failed", detail: "the Eavesdropper did not take the reload",
                              rolledBack: !replaced, replaced: replaced, backup: backup),
                ok: false, result: "failed", errorCode: "reload_failed", detail: "the Eavesdropper did not take the reload")))
        }
        let kept = try failed(backup: backupName, replaced: true)
        XCTAssertEqual(kept.backup, backupName)
        XCTAssertEqual(kept.replaced, true)
        XCTAssertEqual(kept.rolledBack, false)
        XCTAssertEqual(try failed(backup: backupName + "-2", replaced: false).backup, backupName + "-2")
        XCTAssertEqual(try failed(backup: backupName, replaced: false).replaced, false)
        // A name with a slash, another file's name, or the bare prefix is never shown.
        for name in ["listener.toml.bak-settings-x/../../etc/passwd", "/data/" + backupName,
                     "other.toml.bak-settings-20261112T173000Z", ServerSettingsAnswer.backupPrefix,
                     backupName + " and more", backupName + String(repeating: "9", count: 60)] {
            XCTAssertFalse(ServerSettingsAnswer.isBackupName(name), name)
            XCTAssertNil(try failed(backup: name, replaced: true).backup, name)
        }
        XCTAssertTrue(ServerSettingsAnswer.isBackupName(backupName))
        // An answer that names no backup and says nothing of replacing carries neither.
        let bare = ServerSettingsAnswer(document: ["result": .text("failed"), "ok": .bool(false)])
        XCTAssertNil(bare.backup)
        XCTAssertNil(bare.replaced)
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

    /// Every branch of `ServerSettingsAnswer.outcome`, each fed the editor's own answer shape
    /// (`eavesdropper_settings_edit.py`, `run_request`), so the key names are pinned to the editor.
    func test_each_editor_answer_comes_to_its_own_result() throws {
        func outcome(_ stdout: String, mode: OpsMode, helperKind: OpsResultKind = .unknown) throws -> (OpsResultKind, String) {
            ServerSettingsAnswer.outcome(try XCTUnwrap(ServerSettingsAnswer(stdout: stdout)),
                                         helperKind: helperKind, mode: mode, detail: "the helper's words")
        }
        // Planned is covered above;  unchanged is nothing to approve.
        let unchanged = helperAnswer(
            #"{"schemaVersion":1,"tool":"eavesdropper-settings","action":"plan","file":"listener.toml","ok":true,"result":"unchanged","planSHA256":"\#(planHash)","changes":[],"diff":[],"check":{"ok":true,"problems":[],"warnings":0}}"#,
            ok: true, result: "unchanged", detail: "every setting is already as asked")
        let same = try outcome(unchanged, mode: .plan)
        XCTAssertEqual(same.0, .refused)
        XCTAssertEqual(same.1, "Every setting is already as asked, so there is nothing to apply.")

        // Applied, and the editor stands behind it.
        let applied = helperAnswer(editorApplied, ok: true, result: "applied", detail: "1 setting(s) changed; reloaded")
        XCTAssertEqual(try outcome(applied, mode: .yes).0, .done)
        // Applied, and the editor does not say ok:  not known.
        let unbacked = helperAnswer(editorApplied.replacingOccurrences(of: #""ok":true,"result":"applied""#,
                                                                       with: #""ok":false,"result":"applied""#),
                                    ok: false, result: "applied", detail: "1 setting(s) changed; reloaded")
        XCTAssertEqual(try XCTUnwrap(ServerSettingsAnswer(stdout: unbacked)).ok, false)
        XCTAssertEqual(try outcome(unbacked, mode: .yes).0, .unknown)
        // "applied" to a plan is not an answer to it.
        XCTAssertEqual(try outcome(applied, mode: .plan).0, .failed)

        // Refused, in the editor's words.
        let off = "apply is off:  the allowlist's applyEnabled is false until the owner turns it on"
        let refusal = helperAnswer(editorRefusal(action: "apply", code: "apply_disabled", detail: off),
                                   ok: false, result: "refused", errorCode: "apply_disabled", detail: off)
        XCTAssertEqual(try outcome(refusal, mode: .yes).0, .refused)
        XCTAssertEqual(try outcome(refusal, mode: .yes).1, off)

        // Failed before the live file was replaced:  nothing changed.
        let before = helperAnswer(editorFailure(code: "io_error",
                                                detail: "a file step failed before the live file was replaced;  it is unchanged",
                                                rolledBack: false, replaced: false, backup: nil),
                                  ok: false, result: "failed", errorCode: "io_error", detail: "failed")
        XCTAssertEqual(try outcome(before, mode: .yes).0, .failed)
        XCTAssertEqual(try outcome(before, mode: .yes).1,
                       "a file step failed before the live file was replaced;  it is unchanged")
        // Failed after the replace, and the old file is back:  failed, with the backup named.
        let back = helperAnswer(editorFailure(code: "reload_failed",
                                              detail: "the Eavesdropper did not take the reload;  the old file is back",
                                              rolledBack: true, replaced: false, backup: backupName),
                                ok: false, result: "failed", errorCode: "reload_failed", detail: "failed")
        XCTAssertEqual(try outcome(back, mode: .yes).0, .failed)
        XCTAssertEqual(try outcome(back, mode: .yes).1,
                       "the Eavesdropper did not take the reload;  the old file is back (a copy of the old file is kept as \(backupName))")
        // Failed after the replace, and the old file could not be put back:  not known.
        let stuck = helperAnswer(editorFailure(code: "reload_failed",
                                               detail: "the Eavesdropper did not take the reload;  the old bytes could NOT be put back",
                                               rolledBack: false, replaced: true, backup: backupName),
                                 ok: false, result: "failed", errorCode: "reload_failed", detail: "failed")
        XCTAssertEqual(try outcome(stuck, mode: .yes).0, .unknown)
        XCTAssertTrue(try outcome(stuck, mode: .yes).1.hasSuffix("(a copy of the old file is kept as \(backupName))"))

        // A result word the editor never gives.
        let strange = helperAnswer(#"{"ok":true,"result":"maybe"}"#, ok: true, result: "maybe", detail: "maybe")
        let lost = try outcome(strange, mode: .yes)
        XCTAssertEqual(lost.0, .unknown)
        XCTAssertEqual(lost.1, "The server answered \"maybe\", which is not an answer to this request.")
        XCTAssertEqual(try outcome(strange, mode: .plan).0, .failed)
    }

    /// The full parse of a refused or failed apply, so the sheet's advice for the editor's codes
    /// and the unknown state of a file left replaced are what the owner sees.
    func test_a_refused_or_failed_apply_reaches_the_sheet_with_its_advice() throws {
        let action = OpsAction.serverListenerSettings(
            ServerListenerSettingsRequest(set: ["seat.CLAUDE.enabled": .bool(false)], expectPlanSHA256: planHash))
        func parse(_ stdout: String, exit: Int32) -> OpsOutcome {
            OpsResultParser.parse(OpsProcessResult(exitCode: exit, stdout: stdout, stderr: ""),
                                  mode: .yes, action: action, timeout: OpsControlRoute.serverListenerSettingsRunTimeout)
        }
        let gap = ServerStatusCopy.sentenceGap
        let cases: [(code: String, detail: String, advice: String)] = [
            ("apply_disabled", "apply is off:  the allowlist's applyEnabled is false until the owner turns it on",
             ServerSettingsCopy.applyOff),
            ("plan_mismatch", "the file or the request is not what the plan was made from;  plan again",
             "The server's config changed after the plan you approved, so nothing was applied.\(gap)Show the plan again."),
            ("changed_underneath", "the live file changed while this ran;  nothing was written",
             "The server's config changed after the plan you approved, so nothing was applied.\(gap)Show the plan again."),
        ]
        for item in cases {
            let stdout = helperAnswer(editorRefusal(action: "apply", code: item.code, detail: item.detail),
                                      ok: false, result: "refused", errorCode: item.code, detail: item.detail)
            let outcome = parse(stdout, exit: 2)
            XCTAssertEqual(outcome.kind, .refused, item.code)
            XCTAssertFalse(outcome.isSuccess(for: .yes), item.code)
            XCTAssertFalse(outcome.runNotKnown, item.code)
            XCTAssertEqual(outcome.errorCode, item.code)
            XCTAssertEqual(outcome.listenerSettings?.errorCode, item.code)
            XCTAssertEqual(outcome.detail, item.detail)
            XCTAssertEqual(outcome.advice(for: action), item.advice, item.code)
        }

        let stuck = parse(helperAnswer(editorFailure(code: "reload_failed", detail: "the old bytes could NOT be put back",
                                                     rolledBack: false, replaced: true, backup: backupName),
                                       ok: false, result: "failed", errorCode: "reload_failed", detail: "failed"), exit: 1)
        XCTAssertEqual(stuck.kind, .unknown)
        XCTAssertTrue(stuck.runNotKnown)
        XCTAssertEqual(stuck.listenerSettings?.backup, backupName)
        let unchanged = parse(helperAnswer(editorFailure(code: "io_error", detail: "a file step failed before the live file was replaced;  it is unchanged",
                                                         rolledBack: false, replaced: false, backup: nil),
                                           ok: false, result: "failed", errorCode: "io_error", detail: "failed"), exit: 1)
        XCTAssertEqual(unchanged.kind, .failed)
        XCTAssertFalse(unchanged.runNotKnown)

        let done = parse(helperAnswer(editorApplied, ok: true, result: "applied", detail: "1 setting(s) changed; reloaded"), exit: 0)
        XCTAssertEqual(done.kind, .done)
        XCTAssertTrue(done.isSuccess(for: .yes))
        XCTAssertEqual(done.listenerSettings?.reloadCode, "reloaded")
    }

    func test_the_value_writer_matches_json() {
        XCTAssertEqual(ServerSettingValue.number(30).json, "30.0")
        XCTAssertEqual(ServerSettingValue.int(30).json, "30")
        XCTAssertEqual(ServerSettingValue.text("a\"b").json, #""a\"b""#)
        XCTAssertEqual(ServerSettingJSON.parse("[1, 2.5, \"x\", null]"),
                       .list([.int(1), .number(2.5), .text("x"), .null]))
        XCTAssertNil(ServerSettingJSON.parse("{} trailing"))
    }

    func test_a_list_of_pairs_is_shown_with_each_pair_bracketed_and_a_flat_list_is_not() {
        let pairs = ServerSettingValue.list([.list([.text("a"), .text("b")]), .list([.text("c"), .text("d")])])
        XCTAssertEqual(pairs.display, "[a, b], [c, d]")
        XCTAssertEqual(ServerSettingValue.list([.text("a"), .text("b")]).display, "a, b")
        XCTAssertEqual(ServerSettingValue.list([]).display, "")
        let topics = ServerSettingJSON.parse(#"[["fleet", "presence"], ["Work-In-Progress", "fleet"]]"#)
        XCTAssertEqual(topics?.display, "[fleet, presence], [Work-In-Progress, fleet]")
    }
}
