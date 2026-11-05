import XCTest

final class FleetServicesPanelRoutingTests: XCTestCase {
    func test_launch_agents_use_admin_when_allowlist_is_missing_or_empty() {
        let serviceID = "com.jay.unlisted-agent"

        XCTAssertTrue(FleetServiceActionRouting.usesFleetAdmin(
            serviceID: serviceID, label: serviceID, allowlist: nil
        ))
        XCTAssertTrue(FleetServiceActionRouting.usesFleetAdmin(
            serviceID: serviceID, label: serviceID, allowlist: OpsAllowlist()
        ))
    }

    func test_listed_launch_agent_uses_controller_and_server_keeps_admin_route() {
        let label = "com.jay.allowed-agent"
        let allowlist = OpsAllowlist(macAgents: [
            OpsAllowlist.Entry(name: label, actions: [OpsVerb.start.rawValue])
        ])

        XCTAssertFalse(FleetServiceActionRouting.usesFleetAdmin(
            serviceID: label, label: label, allowlist: allowlist
        ))
        XCTAssertTrue(FleetServiceActionRouting.usesFleetAdmin(
            serviceID: FleetServiceRegistry.server.id,
            label: FleetServiceRegistry.server.id,
            allowlist: allowlist
        ))
    }
}

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
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "fleet-ops-ctl", "--",
    ]

    /// A connection that dies after it opened is noticed within a minute,  and a server run
    /// waits minutes and not a quarter hour.  A run on the Mac keeps the longer wait.
    func test_a_server_run_waits_minutes_and_a_dead_link_is_noticed() throws {
        XCTAssertTrue(OpsControlRoute.sshOptions.contains("ServerAliveInterval=15"))
        XCTAssertTrue(OpsControlRoute.sshOptions.contains("ServerAliveCountMax=3"))
        for tag in OpsActionCase.allCases where tag != .resourceStatus {
            let action = tag.sample
            let run = try exampleBuilder().invocation(for: action, mode: .yes)
            XCTAssertEqual(run.timeout, action.runsOnServer ? 180 : 900, "\(tag)")
            XCTAssertEqual(OpsControlRoute.runWait(for: action), run.timeout, "\(tag)")
            XCTAssertEqual(try exampleBuilder().invocation(for: action, mode: .plan).timeout, 90, "\(tag)")
        }
    }

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

    func test_a_pm2_verb_runs_the_helper_on_this_mac() throws {
        let invocation = try exampleBuilder().invocation(
            for: .pm2App(verb: .restart, name: "shellular"), mode: .plan
        )
        XCTAssertEqual(invocation.executable, "/usr/bin/python3")
        XCTAssertEqual(invocation.arguments, [
            "-s", "/tmp/example-repo/scripts/fleet-ops-control.py",
            "pm2", "restart", "shellular", "--plan", "--json",
        ])
        XCTAssertFalse(OpsAction.pm2App(verb: .restart, name: "shellular").runsOnServer)
        XCTAssertEqual(OpsAction.pm2App(verb: .stop, name: "shellular").auditVerb, "pm2 stop")
        XCTAssertEqual(OpsAction.pm2App(verb: .stop, name: "shellular").auditTarget, "shellular")
    }

    func test_a_logs_verb_goes_over_ssh_to_the_alias() throws {
        let invocation = try exampleBuilder().invocation(
            for: .logs(unit: "fleet-server-status.timer"), mode: .yes
        )
        XCTAssertEqual(invocation.executable, "/usr/bin/ssh")
        XCTAssertEqual(
            invocation.arguments,
            sshPrefix + ["server", "logs", "fleet-server-status.timer", "--yes", "--json"]
        )
        XCTAssertTrue(OpsAction.logs(unit: "fleet-server-status.timer").runsOnServer)
        XCTAssertEqual(OpsAction.logs(unit: "fleet-server-status.timer").auditVerb, "server logs")
    }

    func test_resource_policy_write_and_status_use_the_fixed_ssh_routes() throws {
        let revision = String(repeating: "a", count: 64)
        let action = OpsAction.serverResource(field: "cpu-cores", value: "8", revision: revision)
        let write = try exampleBuilder().invocation(for: action, mode: .plan)
        XCTAssertEqual(write.executable, "/usr/bin/ssh")
        XCTAssertEqual(write.arguments, sshPrefix + [
            "server", "resource-policy", "cpu-cores.8.\(revision)", "--plan", "--json",
        ])
        XCTAssertTrue(action.runsOnServer)
        XCTAssertEqual(action.auditVerb, "server resource policy")
        XCTAssertEqual(action.auditTarget, "coding.cpu-cores")

        let status = try exampleBuilder().invocation(for: .resourceStatus, mode: .plan)
        XCTAssertEqual(status.executable, "/usr/bin/ssh")
        XCTAssertEqual(status.arguments, sshPrefix + ["server", "resource-status", "coding", "--plan", "--json"])
        XCTAssertThrowsError(try exampleBuilder().invocation(for: .resourceStatus, mode: .yes))
    }

    func test_resource_policy_action_rejects_unlisted_fields_values_and_revisions() {
        let invalid: [OpsAction] = [
            .serverResource(field: "shell", value: "8", revision: String(repeating: "a", count: 64)),
            .serverResource(field: "cpu-cores", value: "0", revision: String(repeating: "a", count: 64)),
            .serverResource(field: "cpu-weight", value: "10001", revision: String(repeating: "a", count: 64)),
            .serverResource(field: "ram-budget-gib", value: "2.5", revision: String(repeating: "a", count: 64)),
            .serverResource(field: "borrow-idle", value: "true", revision: String(repeating: "a", count: 64)),
            .serverResource(field: "borrow-idle", value: "2", revision: String(repeating: "a", count: 64)),
            .serverResource(field: "cpu-cores", value: "8", revision: String(repeating: "A", count: 64)),
            .serverResource(field: "cpu-cores", value: "8", revision: "short"),
        ]
        for action in invalid {
            XCTAssertThrowsError(try exampleBuilder().invocation(for: action, mode: .plan), "\(action)")
        }
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
        XCTAssertEqual(invocation.timeout, OpsControlRoute.serverRunTimeout)
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
            "-s", "-m", "fleet_knobs", "set", "--plan", "--json", "--", "FLEET_AUTOPUSH_MODE", "report",
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
            "-s", "-m", "fleet_knobs", "set", "--yes", "--json", "--", "FLEET_AUTOPUSH_REPOS", "",
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
            (.pm2App(verb: .start, name: "shellular;id"), .invalidArgument("pm2 name")),
            (.pm2App(verb: .start, name: "-oProxyCommand=x"), .invalidArgument("pm2 name")),
            (.logs(unit: "a b.service"), .invalidArgument("unit name")),
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
        let invocation = try exampleBuilder().invocation(for: .knobSet(key: "FLEET_X", value: "-1"), mode: .plan)
        // The value comes after `--`,  so the knob engine's parser takes it as the value.
        XCTAssertEqual(Array(invocation.arguments.suffix(3)), ["--", "FLEET_X", "-1"])
    }

    func test_values_the_helper_would_refuse_are_refused_here_first() throws {
        // Each of these passes the loose token check but fails the helper's own
        // pattern for its verb, so building the invocation has to refuse it here
        // rather than after Touch ID.
        let refused: [(OpsAction, OpsControlError)] = [
            (.pm2App(verb: .start, name: "Shellular"), .invalidArgument("pm2 name")),
            (.pm2App(verb: .start, name: "app@1"), .invalidArgument("pm2 name")),
            (.pm2App(verb: .start, name: String(repeating: "a", count: 65)), .invalidArgument("pm2 name")),
            (.logs(unit: "fleet-worker@CLAUDE.service"), .invalidArgument("unit name")),
            (.logs(unit: "no-suffix"), .invalidArgument("unit name")),
            (.serverUnit(verb: .stop, unit: "fleet-worker@CLAUDE.service"), .invalidArgument("unit name")),
            (.serverUnit(verb: .stop, unit: "a.servicex"), .invalidArgument("unit name")),
            (.macAgent(verb: .start, label: "com.jay.x@y"), .invalidArgument("agent label")),
            (.macAgent(verb: .start, label: String(repeating: "a", count: 128)), .invalidArgument("agent label")),
            (.serverUpdate(name: "Fleet-CLI"), .invalidArgument("update name")),
            (.coolifyRedeploy(uuid: "ab-cd1234", name: "x"), .invalidArgument("resource id")),
            (.coolifyRedeploy(uuid: "abc", name: "x"), .invalidArgument("resource id")),
        ]
        for (action, expected) in refused {
            XCTAssertThrowsError(try exampleBuilder().invocation(for: action, mode: .plan), "\(action)") { error in
                XCTAssertEqual(error as? OpsControlError, expected, "\(action)")
            }
        }
        // And the shapes the helper accepts still build.
        XCTAssertNoThrow(try exampleBuilder().invocation(
            for: .pm2App(verb: .start, name: "botfleet-mcp"), mode: .plan))
        XCTAssertNoThrow(try exampleBuilder().invocation(
            for: .macAgent(verb: .start, label: "app.botfleet.companion"), mode: .plan))
        XCTAssertNoThrow(try exampleBuilder().invocation(
            for: .coolifyRedeploy(uuid: "exampleuuid1", name: "x"), mode: .plan))
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
        XCTAssertThrowsError(try builder.checkInstalled(for: .pm2App(verb: .start, name: "shellular")))
        XCTAssertNoThrow(try builder.checkInstalled(for: .serverUpdate(name: "fleet-cli")))
        XCTAssertNoThrow(try builder.checkInstalled(for: .logs(unit: "fleet-server-status.timer")))
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

    func test_a_resource_status_answer_carries_its_typed_receipt() throws {
        let revision = String(repeating: "a", count: 64)
        let policy = #"{"schemaVersion":1,"generatedAt":"2026-11-03T12:30:00Z","policyRevision":"\#(revision)","desired":{"cpuCores":8,"cpuWeight":100,"ramPressureGiB":8,"ramCriticalGiB":4,"reserveGiB":4,"allowBorrowing":false},"observed":{"cpuOnline":16,"cpuCores":8,"cpuWeight":50,"cpuGroups":4,"cpuMismatches":0,"ramTotalBytes":17179869184,"ramCurrentBytes":5368709120,"ramTargetBytes":8589934592,"mode":"normal","codingGroups":4,"productionGroups":2,"controlWriteFailures":0,"controlSuspended":0,"policyStale":false,"actualHighMismatches":0,"updatedAt":"2026-11-03T12:25:00Z"},"status":"applied"}"#
        let stdout = #"{"ok":true,"verb":"server resource status","target":"coding","commands":[["server","resource-status","coding"]],"result":"planned","resourcePolicy":\#(policy)}"#
        let outcome = OpsResultParser.parse(
            result(stdout), mode: .plan, action: .resourceStatus, timeout: 90
        )
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertEqual(outcome.resourcePolicy?.policyRevision, revision)
        XCTAssertEqual(outcome.resourcePolicy?.desired.allowBorrowing, false)
    }

    func test_a_run_reads_as_done() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_done", "json")), mode: .yes, action: unit, timeout: 900
        )
        XCTAssertEqual(outcome.kind, .done)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(outcome.detail, "Done.")
    }

    func test_async_acceptance_never_claims_completion_even_from_legacy_helpers() {
        for action in [OpsAction.serverUpdate(name: "fleet-cli"), .coolifyRedeploy(uuid: "exampleuuid1", name: "example")] {
            for json in [
                #"{"ok":true,"result":"done","detail":"done"}"#,
                #"{"ok":true,"result":"submitted","executionState":"submitted","verified":false}"#,
                #"{"ok":true,"result":"done","executionState":"submitted","verified":true}"#,
                #"{"ok":true,"result":"done","executionState":"completed","verified":false}"#,
                #"{"ok":true,"result":"done","executionState":"completed","verified":"true"}"#,
            ] {
                let outcome = OpsResultParser.parse(result(json), mode: .yes, action: action, timeout: 900)
                XCTAssertEqual(outcome.kind, .submitted)
                XCTAssertFalse(outcome.isSuccess(for: .yes))
            }
        }
    }

    func test_update_completion_requires_a_verified_invocation_receipt() {
        let action = OpsAction.serverUpdate(name: "fleet-cli")
        let receipt = String(repeating: "a", count: 32)
        let json = #"{"ok":true,"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let completed = OpsResultParser.parse(result(json), mode: .yes, action: action, timeout: 900)
        XCTAssertTrue(completed.isSuccess(for: .yes))
        XCTAssertEqual(completed.invocationID, receipt)
        let invalid = json.replacingOccurrences(of: receipt, with: "not-a-receipt")
        XCTAssertEqual(OpsResultParser.parse(result(invalid), mode: .yes, action: action, timeout: 900).kind, .submitted)
        XCTAssertNil(OpsResultParser.parse(result(invalid), mode: .yes, action: action, timeout: 900).invocationID)
        XCTAssertEqual(OpsResultParser.parse(result(json), mode: .plan, action: action, timeout: 90).kind, .failed)
    }

    func test_submitted_is_not_success_when_the_helper_exits_with_failure() {
        let json = #"{"ok":true,"result":"submitted"}"#
        let outcome = OpsResultParser.parse(result(json, exit: 1), mode: .yes, action: unit, timeout: 900)
        XCTAssertEqual(outcome.kind, .unknown)
        XCTAssertTrue(outcome.runNotKnown)
        XCTAssertEqual(outcome.detail, "The helper said submitted, but it exited with code 1.")
        XCTAssertFalse(outcome.isSuccess(for: .yes))
        let audit = OpsAuditEntry(time: nil, host: "server", verb: "server update", target: "fleet-cli", result: "submitted")
        XCTAssertEqual(audit.state, .unproven)
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
        // It said it did the work and did not stand behind that,  so it may have done it.
        XCTAssertEqual(outcome.kind, .unknown)
        XCTAssertTrue(outcome.runNotKnown)
        // The answer said ok,  so the words name the exit code.
        XCTAssertEqual(outcome.detail, "The helper said done, but it exited with code 1.")
        // The helper's own words stay,  after the words that say what disagrees.
        let worded = OpsResultParser.parse(
            result(#"{"ok":true,"result":"done","detail":"ran"}"#, exit: 1), mode: .yes, action: unit, timeout: 900
        )
        XCTAssertEqual(worded.detail, ApplyCopy.join("The helper said done, but it exited with code 1.", "ran"))
    }

    func test_a_done_answer_that_is_not_ok_is_not_known() {
        let outcome = OpsResultParser.parse(
            result(#"{"ok":false,"result":"done","detail":"ran"}"#), mode: .yes, action: unit, timeout: 900
        )
        XCTAssertEqual(outcome.kind, .unknown)
        XCTAssertTrue(outcome.runNotKnown)
        XCTAssertEqual(outcome.detail, ApplyCopy.join("The helper said done, but its answer was not ok (exit 0).", "ran"))
    }

    /// The helper's own word for a failure stands:  it knows how it ended.
    func test_a_run_the_helper_says_failed_is_still_failed() {
        let outcome = OpsResultParser.parse(
            result(#"{"ok":false,"result":"failed","detail":"no"}"#, exit: 1), mode: .yes, action: unit, timeout: 900
        )
        XCTAssertEqual(outcome.kind, .failed)
        XCTAssertFalse(outcome.runNotKnown)
    }

    /// A helper that gave no answer may have got anywhere before it stopped,  whichever
    /// machine it ran on and however it ended.
    func test_a_run_with_no_answer_is_not_known() {
        let macAgent = OpsAction.macAgent(verb: .restart, label: "com.jay.x")
        for action in [macAgent, unit] {
            let traceback = OpsResultParser.parse(
                result("Traceback (most recent call last):", exit: 1, stderr: "ValueError: boom"),
                mode: .yes, action: action, timeout: 900
            )
            XCTAssertEqual(traceback.kind, .unknown)
            XCTAssertTrue(traceback.runNotKnown)
            XCTAssertTrue(traceback.detail.contains("exit 1"))
            XCTAssertTrue(traceback.detail.contains("ValueError: boom"))
            for code: Int32 in [0, 9, 15] {
                let silent = OpsResultParser.parse(result("", exit: code), mode: .yes, action: action, timeout: 900)
                XCTAssertEqual(silent.kind, .unknown, "exit \(code)")
                XCTAssertTrue(silent.runNotKnown, "exit \(code)")
            }
        }
    }

    /// A shell that could not find or start the helper ran nothing.
    func test_a_helper_that_could_not_be_started_failed() {
        let macAgent = OpsAction.macAgent(verb: .restart, label: "com.jay.x")
        for action in [macAgent, unit] {
            for code: Int32 in [126, 127] {
                let outcome = OpsResultParser.parse(result("", exit: code), mode: .yes, action: action, timeout: 900)
                XCTAssertEqual(outcome.kind, .failed, "exit \(code)")
                XCTAssertFalse(outcome.runNotKnown, "exit \(code)")
            }
        }
    }

    /// A read changes nothing,  so a read that went wrong plainly did not work.
    func test_a_logs_run_that_went_wrong_failed() {
        let logs = OpsAction.logs(unit: "fleet-server-status.timer")
        let wrong: [(OpsProcessResult, String)] = [
            (result("", exit: 15, timedOut: true), "The helper did not finish within 900 seconds."),
            (result("", exit: 255), "The server did not answer, so nothing came back."),
            (result("", exit: 1), "The helper printed no answer (exit 1)."),
            (result(#"{"ok":true,"result":"done"}"#, exit: 1), "The helper said done, but it exited with code 1."),
        ]
        for (answer, words) in wrong {
            let outcome = OpsResultParser.parse(answer, mode: .yes, action: logs, timeout: 900)
            XCTAssertEqual(outcome.kind, .failed, words)
            XCTAssertFalse(outcome.runNotKnown, words)
            XCTAssertEqual(outcome.detail, words)
        }
        let wordless = OpsResultParser.parse(result(#"{"ok":true,"result":"maybe"}"#), mode: .yes, action: logs, timeout: 900)
        XCTAssertEqual(wordless.kind, .unknown)
        XCTAssertFalse(wordless.runNotKnown)
    }

    /// The block of commands is called what is known about them.
    func test_the_commands_heading_never_says_what_ran_when_that_is_not_known() {
        let commands = [["x"]]
        func outcome(_ kind: OpsResultKind, notKnown: Bool = false) -> OpsOutcome {
            OpsOutcome(kind: kind, ok: false, commands: commands, detail: "", exitCode: 0, runNotKnown: notKnown)
        }
        XCTAssertEqual(outcome(.planned).commandsHeading, "The Plan")
        // A refusal ran nothing,  and a failure stopped part way:  neither is "what ran".
        XCTAssertEqual(outcome(.refused).commandsHeading, "Not Run")
        XCTAssertEqual(outcome(.unknown, notKnown: true).commandsHeading, "What Was Asked To Run")
        XCTAssertEqual(outcome(.done).commandsHeading, "What Ran")
        XCTAssertEqual(outcome(.submitted).commandsHeading, "What Ran")
        XCTAssertEqual(outcome(.failed).commandsHeading, "What Was Tried")
        // A wrong-machine answer,  as the parser makes it,  on a plan and on a run.
        let json = #"{"ok":true,"result":"planned","commands":[["x"]],"host":"mac"}"#
        XCTAssertEqual(OpsResultParser.parse(result(json), mode: .plan, action: unit, timeout: 90).commandsHeading,
                       "The Plan")
        let done = #"{"ok":true,"result":"done","commands":[["x"]],"host":"mac"}"#
        XCTAssertEqual(OpsResultParser.parse(result(done), mode: .yes, action: unit, timeout: 900).commandsHeading,
                       "What Was Asked To Run")
        // A plan ran nothing,  however it came out.
        let wordless = #"{"ok":true,"result":"maybe","commands":[["x"]]}"#
        for answer in [done.replacingOccurrences(of: #","host":"mac""#, with: ""), wordless] {
            let plan = OpsResultParser.parse(result(answer), mode: .plan, action: unit, timeout: 90)
            XCTAssertFalse(plan.isSuccess(for: .plan))
            XCTAssertEqual(plan.commandsHeading, "The Plan", answer)
        }
        // A run the helper refused after it planned again lists its commands and ran none.
        let refusedRun = #"{"ok":false,"result":"refused","commands":[["x"]],"errorCode":"update_busy"}"#
        XCTAssertEqual(OpsResultParser.parse(result(refusedRun, exit: 2), mode: .yes, action: unit, timeout: 180)
            .commandsHeading, "Not Run")
        XCTAssertEqual(OpsResultParser.parse(result("", exit: 1), mode: .plan, action: unit, timeout: 90).isPlan, true)
        XCTAssertEqual(OpsResultParser.parse(result("", exit: 15, timedOut: true), mode: .plan, action: unit, timeout: 90).isPlan,
                       true)
        XCTAssertEqual(OpsResultParser.parse(result(wordless), mode: .yes, action: unit, timeout: 900).isPlan, false)
    }

    func test_only_a_read_changes_nothing() {
        for tag in OpsActionCase.allCases {
            XCTAssertEqual(tag.sample.changesNothing, tag == .logs || tag == .resourceStatus, "\(tag)")
        }
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

    func test_a_logs_run_surfaces_the_log_text_and_a_plan_never_does() {
        let logs = OpsAction.logs(unit: "fleet-server-status.timer")
        let done = #"{"ok": true, "verb": "server logs", "target": "fleet-server-status.timer", "commands": [["journalctl", "--user", "-u", "fleet-server-status.timer", "-n", "200", "--no-pager"]], "result": "done", "detail": "2 journal lines", "output": "line one\nline two\n", "lines": 2}"#
        let outcome = OpsResultParser.parse(result(done), mode: .yes, action: logs, timeout: 900)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(outcome.logText, "line one\nline two\n")
        XCTAssertEqual(outcome.logLines, 2)
        // The same answer read as a plan surfaces no log text: a plan fetched nothing.
        let asPlan = OpsResultParser.parse(result(done), mode: .plan, action: logs, timeout: 90)
        XCTAssertNil(asPlan.logText)
        // And output on an answer for any other action is never surfaced.
        let other = OpsResultParser.parse(result(done), mode: .yes, action: unit, timeout: 900)
        XCTAssertNil(other.logText)
    }

    func test_a_logs_run_strips_terminal_escapes_before_the_text_is_shown() {
        let logs = OpsAction.logs(unit: "fleet-server-status.timer")
        let done = #"{"ok": true, "verb": "server logs", "target": "fleet-server-status.timer", "commands": [], "result": "done", "detail": "2 journal lines", "output": "\u001b[31mred\u001b[0m\u0007\nnext\tline", "lines": 2}"#
        let outcome = OpsResultParser.parse(result(done), mode: .yes, action: logs, timeout: 900)
        XCTAssertEqual(outcome.logText, "red\nnext\tline")
    }

    func test_a_server_answer_has_no_knob_change() throws {
        let outcome = OpsResultParser.parse(
            result(try fixture("ops_result_planned", "json")), mode: .plan, action: unit, timeout: 90
        )
        XCTAssertNil(outcome.knobChange)
    }

    func test_ssh_errors_are_put_in_fixed_words() {
        let unresolved = OpsResultParser.parse(
            result("", exit: 255, stderr: "ssh: Could not resolve hostname server.test: nodename nor servname provided"),
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

    /// ssh ends with the same line for a key it has never seen and for one that changed.
    /// Only a changed one is a warning,  and neither tells the owner to accept a key.
    func test_a_changed_host_key_is_told_apart_from_an_unknown_one() {
        let unknown = OpsResultParser.parse(
            result("", exit: 255, stderr: "Host key verification failed."),
            mode: .yes, action: unit, timeout: 180
        )
        XCTAssertEqual(unknown.kind, .failed)
        XCTAssertFalse(unknown.runNotKnown)
        XCTAssertEqual(unknown.detail, "This Mac has no record of the server's identity, so Muster sent nothing.")
        let banner = [
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
            "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @",
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@",
            "Host key for server.example has changed and you have requested strict checking.",
            "Host key verification failed.",
        ].joined(separator: "\n")
        let changed = OpsResultParser.parse(result("", exit: 255, stderr: banner), mode: .plan, action: unit, timeout: 90)
        XCTAssertEqual(changed.kind, .failed)
        XCTAssertTrue(changed.detail.hasPrefix("The server's identity does not match what this Mac has on record"))
        XCTAssertTrue(changed.detail.contains("Do not accept a new key"))
        XCTAssertFalse(changed.detail.contains("server.example"))
        XCTAssertTrue(changed.detail.contains("\u{00A0} This can mean"))
    }

    /// The helper's code,  row id,  row target and progress are read,  each only in its own
    /// fixed shape.
    func test_the_helpers_code_row_and_progress_are_read_in_their_own_shape() {
        let json = #"{"ok":false,"result":"failed","commands":[["a"],["b"],["c"]],"detail":"b exited 5","errorCode":"update_busy","auditID":"0123456789ab","auditTarget":"fleet\u0007-recall","completedCommands":1}"#
        let failed = OpsResultParser.parse(result(json, exit: 1), mode: .yes, action: unit, timeout: 180)
        XCTAssertEqual(failed.kind, .failed)
        XCTAssertEqual(failed.errorCode, "update_busy")
        XCTAssertEqual(failed.serverAuditID, "0123456789ab")
        XCTAssertEqual(failed.serverAuditTarget, "fleet-recall")
        XCTAssertEqual(failed.completedCommands, 1)
        XCTAssertEqual(failed.progressNote, "1 of 3 commands succeeded, the next one did not, and none after it ran.")
        let bad = #"{"ok":false,"result":"failed","commands":[["a"]],"errorCode":"Not A Code","auditID":"--yes","completedCommands":4}"#
        let refusedShape = OpsResultParser.parse(result(bad, exit: 1), mode: .yes, action: unit, timeout: 180)
        XCTAssertNil(refusedShape.errorCode)
        XCTAssertNil(refusedShape.serverAuditID)
        XCTAssertNil(refusedShape.completedCommands)
        XCTAssertNil(refusedShape.progressNote)
        // A plan names no row:  it wrote none.
        let plan = OpsResultParser.parse(result(json, exit: 1), mode: .plan, action: unit, timeout: 90)
        XCTAssertNil(plan.serverAuditID)
        XCTAssertNil(plan.progressNote)
    }

    func test_how_far_a_chain_got_is_said_plainly() {
        func failed(_ commands: Int, finished: Int?) -> OpsOutcome {
            OpsOutcome(kind: .failed, ok: false, commands: Array(repeating: ["x"], count: commands),
                       detail: "", exitCode: 1, completedCommands: finished)
        }
        XCTAssertEqual(failed(1, finished: 0).progressNote, "The command ran and did not succeed.")
        XCTAssertEqual(failed(2, finished: 0).progressNote, "The first command did not succeed, so none after it ran.")
        XCTAssertNil(failed(2, finished: nil).progressNote)
        XCTAssertNil(failed(2, finished: 2).progressNote)
    }

    /// The first line of a result says whether anything ran,  in the owner's words,  and
    /// names the machine that answered.
    func test_the_result_title_says_whether_anything_ran() {
        let mac = OpsAction.macAgent(verb: .restart, label: "com.jay.x")
        func titled(_ json: String, exit: Int32 = 0, mode: OpsMode, action: OpsAction) -> String {
            OpsResultParser.parse(result(json, exit: exit), mode: mode, action: action, timeout: 90).resultTitle(for: action)
        }
        let refused = #"{"ok":false,"result":"refused","detail":"no"}"#
        XCTAssertEqual(titled(refused, exit: 2, mode: .plan, action: unit), "The server refused this, so nothing ran.")
        XCTAssertEqual(titled(refused, exit: 2, mode: .yes, action: mac), "This Mac refused this, so nothing ran.")
        XCTAssertEqual(titled("", exit: 255, mode: .plan, action: unit), "The plan could not be read, so nothing ran.")
        XCTAssertEqual(titled(#"{"ok":true,"result":"done","commands":[]}"#, mode: .yes, action: unit), "Done.")
        XCTAssertEqual(titled(#"{"ok":false,"result":"failed"}"#, exit: 1, mode: .yes, action: unit), "This did not work.")
        XCTAssertEqual(titled(#"{"ok":true,"result":"submitted"}"#, mode: .yes, action: .serverUpdate(name: "fleet-cli")),
                       "Sent.\u{00A0} Muster has not seen it finish yet.")
        // A run that lost its answer is never called a failure.
        let lost = OpsResultParser.parse(result("", exit: 255), mode: .yes, action: unit, timeout: 180)
        XCTAssertEqual(lost.resultTitle(for: unit),
                       "This may or may not have run on the server.\u{00A0} Check its state before you run it again.")
    }

    func test_a_known_error_code_gets_plain_advice() {
        func advice(_ code: String?, _ action: OpsAction) -> String? {
            OpsOutcome(kind: .refused, ok: false, commands: [], detail: "", exitCode: 2, errorCode: code).advice(for: action)
        }
        let update = OpsAction.serverUpdate(name: "fleet-cli")
        XCTAssertEqual(advice("update_busy", update), "An update is already running on the server.\u{00A0} Check again in a minute.")
        XCTAssertEqual(advice("action_busy", .macAgent(verb: .start, label: "com.jay.x")),
                       "Another action is still running on the Mac.\u{00A0} Wait for it to end, then try again.")
        XCTAssertNil(advice("something_new", update))
        XCTAssertNil(advice(nil, update))
    }

    func test_a_bare_done_is_not_repeated_under_the_title() {
        func shows(_ detail: String) -> Bool {
            OpsOutcome(kind: .done, ok: true, commands: [], detail: detail, exitCode: 0).showsDetailUnderTitle
        }
        XCTAssertFalse(shows("Done."))
        XCTAssertFalse(shows("done"))
        XCTAssertFalse(shows(""))
        XCTAssertTrue(shows("nothing to do"))
    }
}

// MARK: - The contract with the server

/// `Fixtures/ops_wire_golden.json` is shared with `scripts/test_fleet_ops_control.py`,
/// which feeds the same lines through the server's forced-command gate and checks the same
/// answers.  Either side changing its shape breaks one of the two suites.
final class OpsWireContractTests: XCTestCase {
    private func golden() throws -> [String: Any] {
        let text = try fixture("ops_wire_golden", "json")
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any])
    }

    private func action(_ entry: [String: String]) -> OpsAction? {
        guard let target = entry["target"] else { return nil }
        switch entry["action"] {
        case "serverUnit":
            guard let verb = entry["verb"].flatMap(OpsVerb.init(rawValue:)) else { return nil }
            return .serverUnit(verb: verb, unit: target)
        case "logs": return .logs(unit: target)
        case "serverUpdate": return .serverUpdate(name: target)
        case "coolifyRedeploy": return .coolifyRedeploy(uuid: target, name: "example-app")
        case "resourceStatus": return .resourceStatus
        default: return nil
        }
    }

    func test_muster_sends_each_line_the_server_accepts() throws {
        let accepted = try XCTUnwrap(golden()["accepted"] as? [[String: String]])
        XCTAssertFalse(accepted.isEmpty)
        for entry in accepted {
            let line = entry["line"] ?? ""
            let made = try XCTUnwrap(action(entry), line)
            let invocation = try exampleBuilder().invocation(for: made, mode: entry["mode"] == "yes" ? .yes : .plan)
            let arguments = invocation.arguments
            let end = try XCTUnwrap(arguments.firstIndex(of: "--"), line)
            XCTAssertEqual(arguments[end - 1], OpsControlRoute.serverAlias, line)
            XCTAssertEqual(arguments[(end + 1)...].joined(separator: " "), line)
        }
    }

    func test_muster_reads_the_answers_the_server_gives() throws {
        let answers = try XCTUnwrap(golden()["answers"] as? [String: Any])
        func parse(_ key: String, exit: Int32, mode: OpsMode, action: OpsAction) throws -> OpsOutcome {
            let object = try XCTUnwrap(answers[key], key)
            let text = String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
            return OpsResultParser.parse(OpsProcessResult(exitCode: exit, stdout: text, stderr: ""),
                                         mode: mode, action: action, timeout: 90)
        }
        let restart = OpsAction.serverUnit(verb: .restart, unit: "fleet-server-status.timer")
        let stop = OpsAction.serverUnit(verb: .stop, unit: "fleet-server-status.timer")
        let planned = try parse("planned", exit: 0, mode: .plan, action: restart)
        XCTAssertTrue(planned.isSuccess(for: .plan))
        XCTAssertEqual(planned.commandLines, ["/usr/bin/systemctl --user restart fleet-server-status.timer"])
        let notListed = try parse("notListed", exit: 2, mode: .plan, action: .serverUnit(verb: .restart, unit: "sshd.service"))
        XCTAssertEqual(notListed.kind, .refused)
        XCTAssertEqual(notListed.detail, "the unit is not in the allowlist")
        let busy = try parse("busy", exit: 2, mode: .yes, action: stop)
        XCTAssertEqual(busy.kind, .refused)
        XCTAssertEqual(busy.errorCode, "action_busy")
        XCTAssertNotNil(busy.advice(for: stop))
        XCTAssertEqual(busy.serverAuditID, "0123456789ab")
        let done = try parse("done", exit: 0, mode: .yes, action: stop)
        XCTAssertTrue(done.isSuccess(for: .yes))
        XCTAssertEqual(done.completedCommands, 1)
        XCTAssertEqual(done.serverAuditTarget, "fleet-server-status.timer")
        XCTAssertEqual(done.resultTitle(for: stop), "Done.")
    }
}

// MARK: - The words around a run

final class OpsSheetWordsTests: XCTestCase {
    /// Every action leads its plan with a plain sentence that names where it runs or what it
    /// changes,  and a read says it changes nothing.
    func test_every_action_has_a_plain_sentence_for_its_plan() {
        for tag in OpsActionCase.allCases {
            let action = tag.sample
            let sentence = action.effectSentence
            XCTAssertFalse(sentence.isEmpty, "\(tag)")
            XCTAssertTrue(sentence.hasSuffix("."), "\(tag)")
            if action.changes == .acrossFleet {
                XCTAssertTrue(sentence.contains("every machine"), "\(tag)")
            } else {
                XCTAssertTrue(sentence.contains(action.runsOn.inProse), "\(tag)")
            }
            XCTAssertEqual(sentence.contains("Nothing changes."), action.changesNothing, "\(tag)")
            XCTAssertFalse(sentence.contains("  "), "\(tag)")
        }
        XCTAssertEqual(OpsAction.serverUnit(verb: .restart, unit: OpsNames.reportTimerUnit).effectSentence,
                       "Stops Server Report Timer on the server and starts it again.")
    }

    /// The button names the approval,  which may be Touch ID or the login password.
    func test_the_run_button_names_the_approval_and_the_place() {
        for tag in OpsActionCase.allCases {
            let title = tag.sample.runButtonTitle
            XCTAssertTrue(title.hasPrefix("Approve And "), "\(tag)")
            XCTAssertFalse(title.contains("Touch ID"), "\(tag)")
        }
    }

    func test_a_length_of_time_reads_in_words() {
        XCTAssertEqual(OpsController.durationText(0), "0 seconds")
        XCTAssertEqual(OpsController.durationText(1.9), "1 second")
        XCTAssertEqual(OpsController.durationText(45), "45 seconds")
        XCTAssertEqual(OpsController.durationText(60), "1 minute")
        XCTAssertEqual(OpsController.durationText(65), "1 minute 5 seconds")
        XCTAssertEqual(OpsController.durationText(180), "3 minutes")
        XCTAssertEqual(OpsController.durationText(-4), "0 seconds")
    }

    /// While it runs,  the sheet says where,  for how long,  how long Muster waits,  and that
    /// closing it does not stop the run.  Before that,  it says nothing has run yet.
    func test_the_running_words_say_where_how_long_and_what_closing_does() {
        let unit = OpsAction.serverUnit(verb: .restart, unit: OpsNames.reportTimerUnit)
        XCTAssertEqual(OpsRunStage.checkingPlan.sentence(for: unit, elapsed: 3),
                       "Checking the plan again on the server, 3 seconds so far.\u{00A0} Nothing has run yet.")
        let running = OpsRunStage.running.sentence(for: unit, elapsed: 12)
        XCTAssertTrue(running.hasPrefix("Running on the server, 12 seconds so far."))
        XCTAssertTrue(running.contains("Muster waits up to 3 minutes for the answer."))
        XCTAssertTrue(running.contains("Closing this sheet does not stop the run"))
        let mac = OpsRunStage.running.sentence(for: .macAgent(verb: .start, label: "com.jay.x"), elapsed: 0)
        XCTAssertTrue(mac.contains("up to 15 minutes"))
    }
}

// MARK: - The allowlist and the audit log

final class OpsUpdateReceiptTests: XCTestCase {
    private let receipt = String(repeating: "a", count: 32)

    private func read(_ json: String, exit: Int32 = 0, timedOut: Bool = false) async -> (OpsOutcome, FakeOpsRunner) {
        let runner = FakeOpsRunner { _, _ in
            OpsProcessResult(exitCode: exit, stdout: json, stderr: "", timedOut: timedOut)
        }
        let reader = OpsUpdateReceiptReader(runner: runner, makeBuilder: { exampleBuilder() })
        return (await reader.read(name: "fleet-cli", invocationID: receipt, baselineStartMonotonic: 100), runner)
    }

    func test_verified_same_invocation_success_is_read_over_plan_only() async {
        let json = #"{"ok":true,"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let (outcome, runner) = await read(json)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(runner.recorded.first?.suffix(5).map { $0 }, [
            "server", "update-status", "fleet-cli.\(receipt).100", "--plan", "--json",
        ])
        XCTAssertFalse(runner.recorded.flatMap { $0 }.contains("--yes"))
    }

    func test_verified_same_invocation_failure_is_terminal() async {
        let json = #"{"ok":false,"result":"failed","executionState":"failed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":100,"detail":"The recorded update failed."}"#
        let (outcome, _) = await read(json, exit: 1)
        XCTAssertEqual(outcome.kind, .failed)
        XCTAssertEqual(outcome.detail, "The recorded update failed.")
    }

    func test_missing_replaced_or_unverified_receipts_never_complete_the_update() async {
        for json in [
            #"{"ok":true,"result":"done"}"#,
            #"{"ok":true,"result":"done","executionState":"completed","verified":true,"invocationID":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#,
            #"{"ok":true,"result":"submitted","executionState":"submitted","verified":false,"invocationID":"\#(receipt)","baselineStartMonotonic":100,"errorCode":"invocation_not_current"}"#,
        ] {
            let (outcome, _) = await read(json)
            XCTAssertEqual(outcome.kind, .submitted)
            XCTAssertFalse(outcome.isSuccess(for: .yes))
            XCTAssertEqual(outcome.invocationID, receipt)
        }
    }

    func test_a_timed_out_receipt_read_stays_unverified() async {
        let json = #"{"ok":true,"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let (outcome, _) = await read(json, timedOut: true)
        XCTAssertEqual(outcome.kind, .submitted)
    }

    func test_recheck_requires_the_original_exact_integer_baseline() async {
        for raw in ["true", "-1", "1.5", "18446744073709551616", "\"100\"", "101"] {
            let json = #"{"ok":true,"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":\#(raw)}"#
            let (outcome, _) = await read(json)
            XCTAssertEqual(outcome.kind, .submitted, raw)
        }
        let json = #"{"ok":true,"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)"}"#
        let (missing, _) = await read(json)
        XCTAssertEqual(missing.kind, .submitted)
        let maximum = OpsResultParser.decode(#"{"baselineStartMonotonic":18446744073709551615}"#)
        XCTAssertEqual(maximum?.baselineStartMonotonic, UInt64.max)
        let invocation = try? exampleBuilder().updateStatusInvocation(name: "fleet-cli", invocationID: receipt, baselineStartMonotonic: .max)
        XCTAssertTrue(invocation?.arguments.contains("fleet-cli.\(receipt).18446744073709551615") == true)
    }

    func test_an_expired_receipt_remains_unverified_and_cannot_be_rechecked() async {
        let json = #"{"ok":true,"result":"submitted","executionState":"expired","verified":false,"invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let (outcome, _) = await read(json)
        XCTAssertEqual(outcome.kind, .submitted)
        XCTAssertEqual(outcome.executionState, "expired")
        XCTAssertFalse(outcome.isSuccess(for: .yes))
    }

    func test_recheck_cannot_name_another_update_or_inject_an_invocation() {
        for (name, id) in [("other", receipt), ("fleet-cli", "--yes"), ("fleet-cli", receipt + ".extra")] {
            XCTAssertThrowsError(try exampleBuilder().updateStatusInvocation(name: name, invocationID: id, baselineStartMonotonic: 100))
        }
    }
}

final class OpsAllowlistAndAuditTests: XCTestCase {
    func test_the_allowlist_names_the_buttons() throws {
        let list = try XCTUnwrap(OpsAllowlist.parse(Data(try fixture("ops_allowlist", "json").utf8)))
        XCTAssertEqual(list.verbs(forMacAgent: "com.jay.fleet-tools-doctor"), [.start, .stop, .restart])
        XCTAssertEqual(list.verbs(forServerUnit: "fleet-server-status.timer"), [.restart])
        XCTAssertEqual(list.verbs(forServerUnit: "sshd.service"), [])
        XCTAssertEqual(list.verbs(forPM2App: "shellular"), [.start, .restart])
        XCTAssertEqual(list.verbs(forPM2App: "not-listed"), [])
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

    func test_a_log_one_byte_over_the_tail_keeps_its_first_row() throws {
        // 5 rows of 52,429 bytes are 262,145 bytes:  the tail plus one,  so the read
        // starts at the first byte of the file and every row is whole.
        let url = try writeLargeLog(rowLength: 52_429, rows: 5)
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        XCTAssertEqual(OpsAuditLog.maximumTailBytes + 1, 52_429 * 5)
        let reading = OpsAuditLog.read(at: url, limit: 10_000)
        XCTAssertEqual(reading.unreadable, 0)
        XCTAssertEqual(reading.entries.count, 5)
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

extension OpsController {
    /// What the sheet does after the owner approved:  confirm the plan on screen,  by its
    /// action and its exact outcome.  Outside a review it confirms a stand-in,  which the
    /// controller must refuse.
    func confirm() {
        if case .review(let action, let reviewed) = phase {
            confirm(action: action, reviewed: reviewed)
        } else {
            confirm(action: phase.action ?? .resourceStatus,
                    reviewed: OpsOutcome(kind: .planned, ok: true, commands: [], detail: "", exitCode: 0))
        }
    }
}

/// Holds every `--yes` call until released,  and answers it with `answer`.
private actor RunGate {
    var started = false
    private var continuation: CheckedContinuation<Void, Never>?
    func wait() async {
        started = true
        await withCheckedContinuation { continuation = $0 }
    }
    func release() {
        continuation?.resume()
        continuation = nil
    }
}

/// Holds every call,  plan or run,  until released.
private struct GatedPlanRunner: OpsRunning {
    let gate: RunGate
    let answer: String
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        await gate.wait()
        return OpsProcessResult(exitCode: 0, stdout: answer, stderr: "")
    }
}

private struct GatedRunRunner: OpsRunning {
    let base: FakeOpsRunner
    let gate: RunGate
    let answer: String
    var exitCode: Int32 = 0
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        if invocation.arguments.contains("--yes") {
            _ = try await base.run(invocation)
            await gate.wait()
            return OpsProcessResult(exitCode: exitCode, stdout: answer, stderr: "")
        }
        return try await base.run(invocation)
    }
}

private func isFinished(_ phase: OpsController.Phase) -> Bool {
    if case .finished = phase { return true }
    return false
}

private actor ReceiptReadGate {
    var started = false
    private var continuation: CheckedContinuation<Void, Never>?
    func wait() async {
        started = true
        await withCheckedContinuation { continuation = $0 }
    }
    func release() {
        continuation?.resume()
        continuation = nil
    }
}

private struct GatedReceiptRunner: OpsRunning {
    let base: FakeOpsRunner
    let gate: ReceiptReadGate
    let answer: String
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        if invocation.arguments.contains("update-status") {
            await gate.wait()
            return OpsProcessResult(exitCode: 0, stdout: answer, stderr: "")
        }
        return try await base.run(invocation)
    }
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

    func test_closing_a_pending_recheck_does_not_reopen_the_sheet_or_run_an_update() async throws {
        let receipt = String(repeating: "a", count: 32)
        let planned = #"{"ok":true,"commands":[],"result":"planned"}"#
        let submitted = #"{"ok":true,"commands":[],"result":"submitted","invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let completed = #"{"ok":true,"commands":[],"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let base = FakeOpsRunner { invocation, _ in
            OpsProcessResult(exitCode: 0, stdout: invocation.arguments.contains("--plan") ? planned : submitted, stderr: "")
        }
        let gate = ReceiptReadGate()
        let runner = GatedReceiptRunner(base: base, gate: gate, answer: completed)
        let ops = OpsController(runner: runner, makeBuilder: { exampleBuilder() }, auditURL: nil, checksInstall: false)
        ops.plan(.serverUpdate(name: "fleet-cli"))
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        XCTAssertTrue(ops.canRecheckUpdate)
        ops.recheckSubmittedUpdate()
        for _ in 0..<400 {
            if await gate.started { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        let started = await gate.started
        XCTAssertTrue(started)
        ops.dismiss()
        await gate.release()
        for _ in 0..<10 { await Task.yield() }
        XCTAssertEqual(ops.phase, .idle)
        XCTAssertFalse(ops.isRechecking)
        XCTAssertEqual(base.recorded.filter { $0.contains("--yes") }.count, 1)
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

    func test_a_resource_policy_plan_with_a_changed_revision_or_old_value_is_not_run() async throws {
        let revision = String(repeating: "a", count: 64)
        let action = OpsAction.serverResource(field: "cpu-cores", value: "9", revision: revision)
        let first = #"{"ok":true,"commands":[["resource-policy","coding","\#(revision)","cpu-cores","8","9"]],"result":"planned"}"#
        let changed = #"{"ok":true,"commands":[["resource-policy","coding","\#(revision)","cpu-cores","7","9"]],"result":"planned"}"#
        let fake = FakeOpsRunner { invocation, count in
            if invocation.arguments.contains("--plan") {
                return OpsProcessResult(exitCode: 0, stdout: count == 1 ? first : changed, stderr: "")
            }
            return OpsProcessResult(exitCode: 0, stdout: #"{"ok":true,"commands":[],"result":"done"}"#, stderr: "")
        }
        let ops = controller(fake)
        XCTAssertTrue(ops.plan(action))
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops) { phase in
            if case .review(_, let outcome) = phase {
                return outcome.commandLines == ["resource-policy coding \(revision) cpu-cores 7 9"]
            }
            return false
        }
        XCTAssertTrue(ops.notice?.contains("changed") == true)
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
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

    /// The started row goes in before the helper is launched,  so a helper that could not be
    /// launched closes it with a row that says nothing ran.
    func test_a_run_that_could_not_start_closes_its_row_as_not_run() async throws {
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
        let rows = OpsAuditLog.read(at: url).entries
        XCTAssertEqual(rows.map(\.result), ["not-run", "started"])
        XCTAssertEqual(rows.first?.state, .drift)
        XCTAssertEqual(ops.completedRuns, 0)
    }

    /// The helper ran and its answer could not be read,  so the Mac cannot say how far it got.
    func test_a_run_whose_answer_could_not_be_read_is_not_known() async throws {
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
        try await waitFor(ops, until: isFinished)
        guard case .finished(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertEqual(outcome.kind, .unknown)
        XCTAssertTrue(outcome.runNotKnown)
        XCTAssertEqual(outcome.detail, OpsControlError.outputTooLarge.errorDescription)
        XCTAssertEqual(OpsAuditLog.read(at: url).entries.first?.result, "unknown")
        XCTAssertEqual(ops.completedRuns, 1)
    }

    /// A read changes nothing,  so one whose answer could not be read failed.
    func test_a_logs_run_whose_answer_could_not_be_read_failed() async throws {
        let planned = try fixture("ops_result_planned", "json")
        let fake = ThrowingOpsRunner(planned: planned, throwFrom: 3, error: .outputTooLarge)
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: url, checksInstall: false)
        ops.plan(.logs(unit: "fleet-server-status.timer"))
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
        // One row before the run is sent and one after,  newest first.
        XCTAssertEqual(reading.entries.map(\.result), ["done", "started"])
        XCTAssertEqual(reading.entries.first?.target, "fleet-server-status.timer")
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

    // MARK: The approval is tied to the plan it was asked about

    func test_an_approval_runs_only_the_plan_it_was_asked_about() async throws {
        let fake = try runner()
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        guard case .review(let shown, let reviewed) = ops.phase else { return XCTFail("\(ops.phase)") }
        // Another action,  or another outcome for the same action,  is refused.
        XCTAssertFalse(ops.confirm(action: .serverUpdate(name: "fleet-cli"), reviewed: reviewed))
        var other = reviewed
        other.commands = [["systemctl", "--user", "restart", "something-else.timer"]]
        XCTAssertFalse(ops.confirm(action: shown, reviewed: other))
        XCTAssertTrue(isReview(ops.phase))
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        XCTAssertTrue(ops.confirm(action: shown, reviewed: reviewed))
        try await waitFor(ops, until: isFinished)
    }

    func test_a_stale_approval_never_runs_the_plan_now_on_screen() async throws {
        let changed = #"{"ok": true, "commands": [["systemctl", "--user", "restart", "something-else.timer"]], "result": "planned"}"#
        let fake = try runner(planAfterReview: changed)
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        let first = try XCTUnwrap(ops.beginApproval())
        // A second window cannot ask while the first is asking.
        XCTAssertNil(ops.beginApproval())
        XCTAssertTrue(ops.isAsking)
        ops.finishApproval(first, answer: .approved)
        XCTAssertFalse(ops.isAsking)
        // The plan changed after the approval,  so the new plan comes back for review.
        try await waitFor(ops) { phase in
            if case .review(_, let outcome) = phase { return outcome.commandLines.first?.contains("something-else") == true }
            return false
        }
        // The first approval,  answered late,  does not run the new plan.
        ops.finishApproval(first, answer: .approved)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(isReview(ops.phase))
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        XCTAssertTrue(ops.notice?.contains("changed while you were approving") == true)
    }

    /// A cancel is not an error:  the plan stays up,  says nothing ran,  and can be approved.
    func test_a_cancelled_approval_keeps_the_plan_and_can_be_approved_after_all() async throws {
        let fake = try runner()
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        let asked = try XCTUnwrap(ops.beginApproval())
        ops.finishApproval(asked, answer: .notApproved("You canceled the approval, so nothing ran."))
        XCTAssertTrue(isReview(ops.phase))
        XCTAssertEqual(ops.notice, "You canceled the approval, so nothing ran.")
        XCTAssertFalse(fake.recorded.contains { $0.contains("--yes") })
        let again = try XCTUnwrap(ops.beginApproval())
        XCTAssertNil(ops.notice)
        ops.finishApproval(again, answer: .approved)
        try await waitFor(ops, until: isFinished)
        XCTAssertEqual(fake.recorded.filter { $0.contains("--yes") }.count, 1)
    }

    func test_a_mac_that_cannot_ask_ends_the_sheet() async throws {
        let fake = try runner()
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        let asked = try XCTUnwrap(ops.beginApproval())
        ops.finishApproval(asked, answer: .unavailable("This Mac cannot ask for approval, so nothing ran."))
        guard case .failed(_, let reason) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertEqual(reason, "This Mac cannot ask for approval, so nothing ran.")
        XCTAssertFalse(ops.isAsking)
    }

    /// The sheet cannot be closed out from under the question.
    func test_the_sheet_stays_while_the_approval_is_asked() async throws {
        let ops = controller(try runner())
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        _ = try XCTUnwrap(ops.beginApproval())
        ops.dismiss()
        XCTAssertTrue(isReview(ops.phase))
    }

    // MARK: A run can be left to finish behind a closed sheet

    func test_closing_the_sheet_while_it_runs_hides_it_and_the_result_brings_it_back() async throws {
        let base = try runner()
        let gate = RunGate()
        let done = try fixture("ops_result_done", "json")
        let ops = OpsController(runner: GatedRunRunner(base: base, gate: gate, answer: done),
                                makeBuilder: { exampleBuilder() }, auditURL: nil, checksInstall: false)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        XCTAssertNil(ops.quitWarning, "nothing is sent while the plan is reviewed")
        ops.confirm()
        for _ in 0..<400 {
            if await gate.started { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        XCTAssertEqual(ops.runStage, .running)
        XCTAssertNotNil(ops.stageStartedAt)
        XCTAssertNotNil(ops.quitWarning)
        ops.dismiss()
        XCTAssertTrue(ops.isSheetHidden)
        XCTAssertFalse(ops.isSheetShown)
        XCTAssertEqual(ops.hiddenRun, action)
        XCTAssertTrue(ops.isPresenting, "the run still holds the one action slot")
        // Another action is refused,  and the run's sheet comes back to say why.
        XCTAssertFalse(ops.plan(.serverUpdate(name: "fleet-cli")))
        XCTAssertTrue(ops.isSheetShown)
        ops.dismiss()
        XCTAssertEqual(ops.hiddenRun, action)
        await gate.release()
        try await waitFor(ops, until: isFinished)
        XCTAssertFalse(ops.isSheetHidden)
        XCTAssertTrue(ops.isSheetShown)
        XCTAssertNil(ops.runStage)
        XCTAssertNil(ops.quitWarning)
        XCTAssertEqual(ops.completedRuns, 1)
    }

    func test_closing_the_sheet_while_it_plans_gives_the_plan_up() async throws {
        let gate = RunGate()
        let planned = try fixture("ops_result_planned", "json")
        let ops = OpsController(runner: GatedPlanRunner(gate: gate, answer: planned),
                                makeBuilder: { exampleBuilder() }, auditURL: nil, checksInstall: false)
        ops.plan(action)
        for _ in 0..<400 {
            if await gate.started { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        ops.dismiss()
        XCTAssertFalse(ops.isSheetShown)
        XCTAssertNil(ops.hiddenRun, "a plan is not a run")
        await gate.release()
        try await waitFor(ops) { $0 == .idle }
        XCTAssertFalse(ops.isSheetHidden)
        XCTAssertTrue(ops.plan(action), "a new plan is allowed once the old one is given up")
    }

    // MARK: What a run leaves behind

    private func rawRows(_ url: URL) throws -> [[String: String]] {
        let text = try String(contentsOf: url, encoding: .utf8)
        return try text.split(separator: "\n").map { line in
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: String])
        }
    }

    func test_the_macs_rows_name_the_run_and_the_servers_own_row() async throws {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let planned = try fixture("ops_result_planned", "json")
        let done = #"{"ok": true, "commands": [["systemctl", "--user", "restart", "fleet-server-status.timer"]], "result": "done", "detail": "done", "auditID": "0a1b2c3d4e5f", "auditTarget": "fleet-server-status.timer", "completedCommands": 1}"#
        let fake = FakeOpsRunner { invocation, _ in
            OpsProcessResult(exitCode: 0, stdout: invocation.arguments.contains("--plan") ? planned : done, stderr: "")
        }
        let ops = controller(fake, auditURL: url)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        let rows = try rawRows(url)
        XCTAssertEqual(rows.map { $0["result"] }, ["started", "done"])
        XCTAssertEqual(rows[0]["runID"], rows[1]["runID"])
        XCTAssertEqual(rows[0]["runID"]?.count, 12)
        XCTAssertNil(rows[0]["serverAuditID"])
        XCTAssertEqual(rows[1]["serverAuditID"], "0a1b2c3d4e5f")
        XCTAssertEqual(rows[1]["serverTarget"], "fleet-server-status.timer")
    }

    /// The helper plans again before it runs,  so the commands it reports can differ from the
    /// ones the owner approved.  That is flagged,  never passed off as the approved plan.
    func test_a_run_that_reports_other_commands_than_the_approved_plan_is_flagged() async throws {
        let planned = try fixture("ops_result_planned", "json")
        let other = #"{"ok": true, "commands": [["systemctl", "--user", "start", "fleet-server-status.timer"]], "result": "done"}"#
        let fake = FakeOpsRunner { invocation, _ in
            OpsProcessResult(exitCode: 0, stdout: invocation.arguments.contains("--plan") ? planned : other, stderr: "")
        }
        let ops = controller(fake)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        guard case .finished(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertTrue(outcome.differsFromReview)
        // The same commands are not flagged.
        let same = controller(try runner())
        same.plan(action)
        try await waitFor(same, until: isReview)
        same.confirm()
        try await waitFor(same, until: isFinished)
        guard case .finished(_, let sameOutcome) = same.phase else { return XCTFail("\(same.phase)") }
        XCTAssertFalse(sameOutcome.differsFromReview)
    }

    /// A run that lost its answer may have landed,  so planning it again says so first.
    func test_planning_again_after_a_lost_answer_warns_before_the_approval() async throws {
        let planned = try fixture("ops_result_planned", "json")
        let fake = FakeOpsRunner { invocation, _ in
            if invocation.arguments.contains("--plan") {
                return OpsProcessResult(exitCode: 0, stdout: planned, stderr: "")
            }
            return OpsProcessResult(exitCode: 255, stdout: "", stderr: "Connection to server closed by remote host.")
        }
        var clock = Date(timeIntervalSince1970: 1_800_000_000)
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: nil,
                                checksInstall: false, now: { clock })
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        guard case .finished(_, let lost) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertTrue(lost.runNotKnown)
        ops.dismiss()
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        XCTAssertTrue(ops.notice?.contains("may already have run") == true)
        // Another action is not warned about.
        ops.dismiss()
        ops.plan(.serverUnit(verb: .stop, unit: OpsNames.reportTimerUnit))
        try await waitFor(ops, until: isReview)
        XCTAssertNil(ops.notice)
        // After an hour the warning is gone.
        ops.dismiss()
        clock = clock.addingTimeInterval(OpsController.unresolvedMemory + 1)
        ops.plan(action)
        try await waitFor(ops, until: isReview)
        XCTAssertNil(ops.notice)
    }

    func test_a_recheck_that_settles_an_update_adds_a_row_and_counts_as_a_run() async throws {
        let receipt = String(repeating: "a", count: 32)
        let planned = #"{"ok":true,"commands":[["systemctl","start","--no-block","fleet-cli-git-update.service"]],"result":"planned"}"#
        let submitted = #"{"ok":true,"commands":[["systemctl","start","--no-block","fleet-cli-git-update.service"]],"result":"submitted","invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let completed = #"{"ok":true,"commands":[],"result":"done","executionState":"completed","verified":true,"invocationID":"\#(receipt)","baselineStartMonotonic":100}"#
        let fake = FakeOpsRunner { invocation, _ in
            if invocation.arguments.contains("update-status") {
                return OpsProcessResult(exitCode: 0, stdout: completed, stderr: "")
            }
            return OpsProcessResult(exitCode: 0, stdout: invocation.arguments.contains("--plan") ? planned : submitted, stderr: "")
        }
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-ops-flow-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("ops-audit.jsonl")
        let ops = controller(fake, auditURL: url)
        ops.plan(.serverUpdate(name: "fleet-cli"))
        try await waitFor(ops, until: isReview)
        ops.confirm()
        try await waitFor(ops, until: isFinished)
        XCTAssertEqual(ops.completedRuns, 1)
        ops.recheckSubmittedUpdate()
        for _ in 0..<400 {
            if !ops.isRechecking { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        XCTAssertEqual(ops.completedRuns, 2)
        let rows = try rawRows(url)
        XCTAssertEqual(rows.map { $0["result"] }, ["started", "submitted", "done"])
        XCTAssertEqual(rows[1]["invocationID"], receipt)
        XCTAssertEqual(rows[1]["baselineStartMonotonic"], "100")
        XCTAssertEqual(rows[2]["invocationID"], receipt)
        XCTAssertEqual(rows[2]["recheck"], "true")
        XCTAssertEqual(fake.recorded.filter { $0.contains("--yes") }.count, 1)
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
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("none"), "")
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("None (uses the built-in list)"), "")
        XCTAssertEqual(KnobRegistry.Knob.plainDefault("nonempty"), "nonempty")
    }
}
