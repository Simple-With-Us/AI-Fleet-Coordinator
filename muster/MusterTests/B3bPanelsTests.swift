import Darwin
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
    var environments: [[String: String]] = []
    var timeouts: [TimeInterval] = []
    var replies: [String: String] = [:]
    var failure: Error?
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append((executable, arguments))
        if let failure { throw failure }
        let verb = arguments.first == "-I" ? arguments.dropFirst(2).first : arguments.first
        return replies[verb ?? ""] ?? ""
    }
    func run(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) throws -> String {
        environments.append(environment)
        timeouts.append(timeout)
        return try run(executable, arguments)
    }
}

final class SecretNamesTests: XCTestCase {
    func test_direct_reader_uses_engine_and_only_explicit_read_bindings() throws {
        let fake = B3bCommands(); fake.replies["get"] = "synthetic-return-value"
        let secrets = FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot,
                                   interpreter: URL(fileURLWithPath: "/test/python3"),
                                   infisicalBindings: ["INFISICAL_TOKEN": "explicit-test-binding",
                                                       "FLEET_SECRETS_API": "https://infisical.example.test/api",
                                                       "INFISICAL_API_URL": "https://unused.example.test/api",
                                                       "INFISICAL_WRITE_TOKEN": "must-not-pass",
                                                       "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN": "must-not-borrow"])
        XCTAssertEqual(try secrets.secret(named: "A_KEY"), "synthetic-return-value")
        XCTAssertEqual(fake.calls.first?.0, "/test/python3")
        XCTAssertEqual(fake.calls.first?.1, ["-I", "/tmp/example-repo/scripts/fleet_infisical_cli.py", "get", "A_KEY"])
        XCTAssertNil(fake.environments.first?["PYTHONPATH"])
        XCTAssertEqual(fake.environments.first?["INFISICAL_TOKEN"], "explicit-test-binding")
        XCTAssertEqual(fake.environments.first?["FLEET_SECRETS_API"], "https://infisical.example.test/api")
        XCTAssertNil(fake.environments.first?["INFISICAL_API_URL"])
        XCTAssertNil(fake.environments.first?["INFISICAL_WRITE_TOKEN"])
        XCTAssertNil(fake.environments.first?["SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN"])
        XCTAssertEqual(fake.timeouts, [20])
    }

    func test_unbound_reader_does_not_borrow_or_create_a_token_binding() throws {
        let fake = B3bCommands(); fake.replies["names"] = "A_KEY\n"
        _ = try FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot)
            .secretNames(path: "/")
        XCTAssertNil(fake.environments.first?["INFISICAL_TOKEN"])
        XCTAssertNil(fake.environments.first?["INFISICAL_AI_FLEET_COORDINATOR_TOKEN"])
        XCTAssertNil(fake.environments.first?["FLEET_SECRETS_API"])
        XCTAssertNil(fake.environments.first?["INFISICAL_API_URL"])
    }

    func test_reader_does_not_inherit_an_ambient_api_override() throws {
        let name = "FLEET_SECRETS_API"
        let previous = ProcessInfo.processInfo.environment[name]
        XCTAssertEqual(setenv(name, "https://ambient.example.test/api", 1), 0)
        defer {
            if let previous { setenv(name, previous, 1) }
            else { unsetenv(name) }
        }
        let fake = B3bCommands(); fake.replies["names"] = "A_KEY\n"
        _ = try FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot)
            .secretNames(path: "/")
        XCTAssertNil(fake.environments.first?[name])
    }
    func test_names_parse_grouped_and_fail_closed_on_value_shaped_line() throws {
        let grouped = try FleetSecrets.parseNamesByPath([("/", "B_KEY\nA_KEY\n"), ("/linear", "CACHE_X\n")])
        XCTAssertEqual(grouped.map(\.path), ["/", "/linear"])
        XCTAssertEqual(grouped[0].names, ["A_KEY", "B_KEY"])
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY\nnot a name\n")]))
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY=somevalue\n")]))
    }
    func test_names_parse_accepts_mixed_case_names_but_still_refuses_value_shapes() throws {
        // Live root data carries a legacy mixed-case key name;  listing it is
        // metadata only, so the parse accepts it while value-shaped lines
        // keep failing closed (AFC-592).
        let grouped = try FleetSecrets.parseNamesByPath(
            [("/", "MAYBE_OLD_not_sure_SLACK_MCP_XOXB_TOKEN\nA_KEY\n")])
        XCTAssertEqual(grouped[0].names, ["A_KEY", "MAYBE_OLD_not_sure_SLACK_MCP_XOXB_TOKEN"])
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY\n-sneaky\n")]))
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY\n1LEADING_DIGIT\n")]))
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY\nHAS SPACE\n")]))
        XCTAssertThrowsError(try FleetSecrets.parseNamesByPath([("/", "A_KEY\nKEY=value\n")]))
        // The command-argument check stays strict:  a mixed-case name is
        // listable metadata, never something `get` will be called with.
        XCTAssertFalse(FleetSecrets.isValidName("MAYBE_OLD_not_sure_SLACK_MCP_XOXB_TOKEN"))
    }
    func test_names_uses_names_verb_only_never_get() throws {
        let fake = B3bCommands(); fake.replies["names"] = "A_KEY\n"
        let secrets = FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot)
        XCTAssertEqual(try secrets.secretNames(path: "/"), ["A_KEY"])
        XCTAssertEqual(fake.calls.first?.1, ["-I", "/tmp/example-repo/scripts/fleet_infisical_cli.py", "names", "--path", "/"])
        XCTAssertFalse(fake.calls.flatMap(\.1).contains("get"))
    }
    func test_names_exit_failure_is_refused_not_a_timeout() {
        // AFC-592: a non-zero exit (here 5, an unexpanded reference elsewhere
        // at the path) is not a timeout and must not be reported as one.
        let fake = B3bCommands(); fake.failure = CommandExitError(output: "", status: 5)
        let secrets = FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot)
        XCTAssertThrowsError(try secrets.secretNames(path: "/")) { error in
            XCTAssertEqual(error as? FleetSecretError, .refused("/"))
            XCTAssertFalse(FleetSecretError.refused("/").localizedDescription.contains("in time"))
        }
    }
    func test_names_timeout_stays_unavailable() {
        let fake = B3bCommands(); fake.failure = StartupPolicyError.commandFailed
        let secrets = FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot)
        XCTAssertThrowsError(try secrets.secretNames(path: "/")) { error in
            XCTAssertEqual(error as? FleetSecretError, .unavailable("/"))
        }
    }
    func test_get_exit_3_is_missing_and_other_exits_are_refused() {
        let absent = B3bCommands(); absent.failure = CommandExitError(output: "", status: 3)
        XCTAssertThrowsError(try FleetSecrets(commands: absent, home: b3bHome, engineRoot: b3bRoot).secret(named: "A_KEY")) { error in
            XCTAssertEqual(error as? FleetSecretError, .missing("A_KEY"))
        }
        let refused = B3bCommands(); refused.failure = CommandExitError(output: "", status: 5)
        XCTAssertThrowsError(try FleetSecrets(commands: refused, home: b3bHome, engineRoot: b3bRoot).secret(named: "A_KEY")) { error in
            XCTAssertEqual(error as? FleetSecretError, .refused("A_KEY"))
        }
    }
    func test_names_and_doctor_exits_are_never_missing() {
        // Names and doctor have no key of their own, so even exit 3 is a
        // refusal, never "absent" (Kody finding on PR 1249).
        let names = B3bCommands(); names.failure = CommandExitError(output: "", status: 3)
        XCTAssertThrowsError(try FleetSecrets(commands: names, home: b3bHome, engineRoot: b3bRoot).secretNames(path: "/")) { error in
            XCTAssertEqual(error as? FleetSecretError, .refused("/"))
        }
        let doctor = B3bCommands(); doctor.failure = CommandExitError(output: "", status: 3)
        XCTAssertThrowsError(try FleetSecrets(commands: doctor, home: b3bHome, engineRoot: b3bRoot).doctorReport()) { error in
            XCTAssertEqual(error as? FleetSecretError, .refused("doctor"))
        }
    }
    func test_doctor_parse_keeps_only_booleans_counts_and_short_tokens() throws {
        let json = #"{"ok":true,"cli_present":true,"names_count":3,"auth_mode":"machine","failure_reason":null,"cli_version":"1.2.3","secret_value":"should-not-pass"}"#
        let report = try FleetSecrets.parseDoctor(json)
        XCTAssertEqual(report["ok"], "true")
        XCTAssertEqual(report["names_count"], "3")
        XCTAssertNil(report["secret_value"])
    }
    func test_doctor_selects_only_the_existing_fleet_ops_root_reader() throws {
        let fake = B3bCommands()
        fake.replies["doctor"] = #"{"ok":true,"reader_configured":true,"names_count":2}"#
        let binding = UUID().uuidString
        let secrets = FleetSecrets(commands: fake, home: b3bHome, engineRoot: b3bRoot,
                                   infisicalBindings: ["INFISICAL_TOKEN": binding])
        XCTAssertEqual(try secrets.doctorReport()["ok"], "true")
        XCTAssertEqual(fake.calls.first?.1,
                       ["-I", "/tmp/example-repo/scripts/fleet_infisical_cli.py", "doctor", "--json",
                        "--project", "fleet-ops", "--path", "/"])
        XCTAssertEqual(fake.environments.first?["INFISICAL_TOKEN"], binding)
        XCTAssertNil(fake.environments.first?["INFISICAL_AI_FLEET_COORDINATOR_TOKEN"])
        XCTAssertEqual(fake.timeouts, [20])
    }
}

final class RecallSettingsTests: XCTestCase {
    func test_request_builder_uses_bearer_without_embedding_token_in_url() throws {
        let config = RecallSettingsConfig(baseURLString: "https://recall.example.test", tokenKeyName: "RECALL_ADMIN_TOKEN")
        // A fresh synthetic token exercises the real bearer header contract.
        let token = UUID().uuidString
        let request = try RecallSettingsClient.buildRequest(config: config, token: token)
        XCTAssertEqual(request.url?.absoluteString, "https://recall.example.test/admin/settings")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer \(token)")
        XCTAssertFalse(request.url?.absoluteString.contains(token) ?? true)
    }
    func test_request_builder_rejects_bad_config() {
        XCTAssertThrowsError(try RecallSettingsClient.buildRequest(
            config: RecallSettingsConfig(baseURLString: "not a url", tokenKeyName: "RECALL_ADMIN_TOKEN"), token: "x"))
        XCTAssertThrowsError(try RecallSettingsClient.buildRequest(
            config: RecallSettingsConfig(baseURLString: "https://recall.example.test", tokenKeyName: "lower"), token: "x"))
        XCTAssertThrowsError(try RecallSettingsClient.buildRequest(
            config: RecallSettingsConfig(baseURLString: "http://recall.example.test", tokenKeyName: "RECALL_ADMIN_TOKEN"), token: "x"))
    }
    func test_bearer_only_rides_the_redirect_blocking_session() {
        // The admin token must never be forwarded by a 3xx: the default
        // session is ephemeral and its delegate refuses every redirect.
        XCTAssertTrue(RecallSettingsClient.blockingSession.delegate is RecallRedirectBlocker)
        XCTAssertEqual(RecallSettingsClient.blockingSession.configuration.identifier, nil)
        XCTAssertNil(RecallSettingsClient.blockingSession.configuration.urlCache)
    }
    func test_masking_never_displays_a_raw_value() throws {
        let json = #"{"ok":true,"settings":{"RECALL_ADMIN_TOKEN":{"set":true,"value":"set"},"PLAIN_KEY":{"set":true,"value":"raw-value-must-not-show"},"EMPTY_KEY":{"set":false,"value":"empty"}}}"#
        let rows = try RecallSettingsClient.parseSettings(Data(json.utf8))
        XCTAssertEqual(rows.map(\.name), ["EMPTY_KEY", "PLAIN_KEY", "RECALL_ADMIN_TOKEN"])
        XCTAssertTrue(rows.allSatisfy { $0.display == "set" || $0.display == "empty" })
        XCTAssertFalse(rows.map(\.display).joined().contains("raw-value"))
    }
}
