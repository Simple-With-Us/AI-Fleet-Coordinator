import XCTest

// B3b: knob unset argv, secret names/doctor parsing, recall settings masking.
// Fakes only; no process, no network, no real secret value anywhere.

private let b3bRoot = URL(fileURLWithPath: "/tmp/example-repo", isDirectory: true)
private let b3bHome = URL(fileURLWithPath: "/Users/example", isDirectory: true)

final class KnobUnsetBuilderTests: XCTestCase {
    func test_unset_argv_is_argv_array_with_flags_before_separator() throws {
        let b = OpsCommandBuilder(repoRoot: b3bRoot, home: b3bHome, sshAuthSocket: nil)
        let inv = try b.invocation(for: .knobUnset(key: "FLEET_AUTOPUSH_MODE"), mode: .plan)
        XCTAssertEqual(inv.executable, "/usr/bin/python3")
        XCTAssertEqual(inv.arguments, ["-s", "-m", "fleet_knobs", "unset", "--plan", "--json", "--", "FLEET_AUTOPUSH_MODE"])
        XCTAssertEqual(inv.workingDirectory, "/tmp/example-repo/scripts")
        let yes = try b.invocation(for: .knobUnset(key: "FLEET_AUTOPUSH_MODE"), mode: .yes)
        XCTAssertEqual(yes.arguments, ["-s", "-m", "fleet_knobs", "unset", "--yes", "--json", "--", "FLEET_AUTOPUSH_MODE"])
    }
    func test_unset_rejects_bad_key_and_names_audit() {
        XCTAssertThrowsError(try OpsCommandBuilder(repoRoot: b3bRoot, home: b3bHome, sshAuthSocket: nil)
            .invocation(for: .knobUnset(key: "lower_case"), mode: .plan))
        let a = OpsAction.knobUnset(key: "FLEET_X")
        XCTAssertFalse(a.runsOnServer)
        XCTAssertEqual(a.auditVerb, "knob unset")
        XCTAssertEqual(a.auditTarget, "FLEET_X")
    }
    func test_unset_plan_parses_as_removal() {
        let json = #"{"ok":true,"verb":"unset","target":"FLEET_X","commands":[["infisical"]],"result":"planned","old":"off","old_source":"infisical","new":null}"#
        let out = OpsResultParser.parse(OpsProcessResult(exitCode: 0, stdout: json, stderr: ""),
                                        mode: .plan, action: .knobUnset(key: "FLEET_X"), timeout: 90)
        XCTAssertTrue(out.isSuccess(for: .plan))
        XCTAssertEqual(out.knobChange?.oldText, "\"off\"")
        XCTAssertEqual(out.knobChange?.newText, "unset")
    }
}

private final class B3bCommands: CommandRunning {
    var calls: [(String, [String])] = []
    var replies: [String: String] = [:]
    var failure: Error?
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append((executable, arguments))
        if let failure { throw failure }
        return replies[arguments.first ?? ""] ?? ""
    }
}

final class SecretNamesTests: XCTestCase {
    func test_names_parse_grouped_and_fail_closed_on_value_shaped_line() throws {
        let grouped = try FleetSecrets.parseNamesByPath([("/", "B_KEY\nA_KEY\n"), ("/linear", "CACHE_X\n")])
        XCTAssertEqual(grouped.map(\.path), ["/", "/linear"])
        XCTAssertEqual(grouped[0].names, ["A_KEY", "B_KEY"])
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY\nnot a name\n")]))
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY=somevalue\n")]))
    }
    func test_names_uses_names_verb_only_never_get() throws {
        let fake = B3bCommands(); fake.replies["names"] = "A_KEY\n"
        let secrets = FleetSecrets(commands: fake, home: b3bHome)
        XCTAssertEqual(try secrets.secretNames(path: "/"), ["A_KEY"])
        XCTAssertEqual(fake.calls.first?.1.first, "names")
        XCTAssertFalse(fake.calls.flatMap(\.1).contains("get"))
    }
    func test_doctor_parse_keeps_only_booleans_counts_and_short_tokens() throws {
        let json = #"{"ok":true,"cli_present":true,"names_count":3,"auth_mode":"machine","failure_reason":null,"cli_version":"1.2.3","secret_value":"should-not-pass"}"#
        let report = try FleetSecrets.parseDoctor(json)
        XCTAssertEqual(report["ok"], "true")
        XCTAssertEqual(report["names_count"], "3")
        XCTAssertNil(report["secret_value"])
    }
}

final class RecallSettingsTests: XCTestCase {
    func test_request_builder_uses_bearer_without_embedding_token_in_url() throws {
        let config = RecallSettingsConfig(baseURLString: "https://recall.example.test", tokenKeyName: "RECALL_ADMIN_TOKEN")
        let request = try RecallSettingsClient.buildRequest(config: config, token: "fake-token-for-test")
        XCTAssertEqual(request.url?.absoluteString, "https://recall.example.test/admin/settings")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fake-token-for-test")
        XCTAssertFalse(request.url?.absoluteString.contains("fake-token-for-test") ?? true)
    }
    func test_request_builder_rejects_bad_config() {
        XCTAssertThrowsError(try RecallSettingsClient.buildRequest(
            config: RecallSettingsConfig(baseURLString: "not a url", tokenKeyName: "RECALL_ADMIN_TOKEN"), token: "x"))
        XCTAssertThrowsError(try RecallSettingsClient.buildRequest(
            config: RecallSettingsConfig(baseURLString: "https://recall.example.test", tokenKeyName: "lower"), token: "x"))
        XCTAssertThrowsError(try RecallSettingsClient.buildRequest(
            config: RecallSettingsConfig(baseURLString: "http://recall.example.test", tokenKeyName: "RECALL_ADMIN_TOKEN"), token: "x"))
    }
    func test_masking_never_displays_a_raw_value() throws {
        let json = #"{"ok":true,"settings":{"RECALL_ADMIN_TOKEN":{"set":true,"value":"set"},"PLAIN_KEY":{"set":true,"value":"raw-value-must-not-show"},"EMPTY_KEY":{"set":false,"value":"empty"}}}"#
        let rows = try RecallSettingsClient.parseSettings(Data(json.utf8))
        XCTAssertEqual(rows.map(\.name), ["EMPTY_KEY", "PLAIN_KEY", "RECALL_ADMIN_TOKEN"])
        XCTAssertTrue(rows.allSatisfy { $0.display == "set" || $0.display == "empty" })
        XCTAssertFalse(rows.map(\.display).joined().contains("raw-value"))
    }
}
