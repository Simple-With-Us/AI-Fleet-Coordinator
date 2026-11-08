import XCTest

// One server unit's state read live (`server unit-status UNIT --plan --json`),  the unit a logs
// answer read (`logsUnit`),  the resource a redeploy plan names (`resourceName`),  and the Mac
// agents' role and stop consequence.
//
// No test here starts a process or reaches the network:  the runner is a fake,  and the answers
// are fixed text in the shapes `fleet-ops-control.py` gives (the helper's `unit_status`,  Nov 8,
// 2026).  At 4:02 PM UTC on Nov 8, 2026,  Chicago is on standard time,  so it is 10:02 AM there.

/// Answers each call from a handler,  and records the argv it was asked to run.
private final class UnitRunner: OpsRunning, @unchecked Sendable {
    private let lock = NSLock()
    private var calls: [[String]] = []
    private let handler: ([String]) -> OpsProcessResult

    init(_ handler: @escaping ([String]) -> OpsProcessResult) { self.handler = handler }

    convenience init(answer: String, exit: Int32 = 0) {
        self.init { _ in OpsProcessResult(exitCode: exit, stdout: answer, stderr: "") }
    }

    var recorded: [[String]] { lock.lock(); defer { lock.unlock() }; return calls }

    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult {
        lock.lock()
        calls.append(invocation.arguments)
        lock.unlock()
        return handler(invocation.arguments)
    }
}

private func exampleBuilder() -> OpsCommandBuilder {
    OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/tmp/example-repo", isDirectory: true),
                      home: URL(fileURLWithPath: "/Users/example", isDirectory: true),
                      sshAuthSocket: nil)
}

private func date(_ text: String) throws -> Date {
    try XCTUnwrap(FleetTime.parse(text), text)
}

/// A planned `server unit-status` answer about `unit`,  with `fields` (JSON members) added.
private func unitAnswer(_ unit: String, _ fields: String) -> String {
    #"{"ok":true,"verb":"server unit-status","target":""# + unit
        + #"","commands":[],"result":"planned","detail":"read-only status of a listed unit","unit":""# + unit
        + #"","# + fields + "}"
}

/// fleet-server-status.timer:  a realtime timer,  so a next run time,  and no exit time.
private let timerAnswer = unitAnswer(
    "fleet-server-status.timer",
    #""scope":"user","activeState":"active","subState":"waiting","unitResult":"success","lastExitAt":null,"nextElapseAt":"2026-11-08T16:15:00Z","checkedAt":"2026-11-08T16:02:00Z""#
)

/// docker.service:  a `watch` service,  with an exit time and no `nextElapseAt` key at all.
private let serviceAnswer = unitAnswer(
    "docker.service",
    #""scope":"system","activeState":"active","subState":"running","unitResult":"success","lastExitAt":"2026-11-08T15:40:00Z","checkedAt":"2026-11-08T16:02:00Z""#
)

/// code-main-sync.timer:  a monotonic timer,  whose next run has no wall-clock time.
private let monotonicAnswer = unitAnswer(
    "code-main-sync.timer",
    #""scope":"system","activeState":"active","subState":"waiting","unitResult":"success","lastExitAt":null,"nextElapseAt":null,"checkedAt":"2026-11-08T16:02:00Z""#
)

/// A refusal of `server unit-status`,  with or without a code.
private func refusal(_ unit: String, detail: String, code: String?) -> String {
    let quote = "\""
    let coded = code.map { #","errorCode":""# + $0 + quote } ?? ""
    return #"{"ok":false,"verb":"server unit-status","target":""# + unit
        + #"","commands":[],"result":"refused","detail":""# + detail + quote + coded + "}"
}

private func parsePlan(_ stdout: String, _ action: OpsAction, exit: Int32 = 0) -> OpsOutcome {
    OpsResultParser.parse(OpsProcessResult(exitCode: exit, stdout: stdout, stderr: ""),
                          mode: .plan, action: action, timeout: 90)
}

private func parseRun(_ stdout: String, _ action: OpsAction, exit: Int32 = 0) -> OpsOutcome {
    OpsResultParser.parse(OpsProcessResult(exitCode: exit, stdout: stdout, stderr: ""),
                          mode: .yes, action: action, timeout: 180)
}

/// The allowlist's server units and `watch` units,  as the checkout carries them.
private let allowlist = OpsAllowlist(
    serverUnits: [
        OpsAllowlist.Entry(name: "fleet-server-status.timer", actions: ["start", "stop", "restart"], scope: "user"),
        OpsAllowlist.Entry(name: "fleet-cli-git-update.timer", actions: ["start", "stop", "restart"], scope: "system"),
    ],
    watchUnits: ["code-main-sync.timer", "docker.service"]
)

// MARK: - The route

final class ServerUnitStatusRouteTests: XCTestCase {
    private let sshPrefix = [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "fleet-ops-ctl", "--",
    ]

    func test_the_read_is_one_fixed_plan_over_the_ssh_route() throws {
        let action = OpsAction.unitStatus(unit: "fleet-server-status.timer")
        let invocation = try exampleBuilder().invocation(for: action, mode: .plan)
        XCTAssertEqual(invocation.executable, "/usr/bin/ssh")
        XCTAssertEqual(invocation.arguments, sshPrefix + [
            "server", "unit-status", "fleet-server-status.timer", "--plan", "--json",
        ])
        XCTAssertEqual(invocation.timeout, OpsControlRoute.planTimeout)
        XCTAssertTrue(action.runsOnServer)
        XCTAssertTrue(action.changesNothing)
        XCTAssertNil(exampleBuilder().requiredFile(for: action))
        XCTAssertEqual(action.caseTag, .unitStatus)
        XCTAssertTrue(OpsActionCase.unitStatus.isPlanOnly)
        XCTAssertEqual(action.auditVerb, "server unit status")
        XCTAssertEqual(action.auditTarget, "fleet-server-status.timer")
        XCTAssertEqual(action.title, "Read the state of Server Report Timer on the server")
        XCTAssertEqual(action.effectSentence,
                       "Reads the state of Server Report Timer on the server.\u{00A0} Nothing changes.")
        XCTAssertNil(action.stopWarning)
    }

    /// The helper refuses `--yes` for this verb,  and Muster never builds one.
    func test_a_run_of_the_read_is_never_built() {
        XCTAssertThrowsError(try exampleBuilder().invocation(for: .unitStatus(unit: "docker.service"), mode: .yes)) { error in
            XCTAssertEqual(error as? OpsControlError, .invalidArgument("read-only unit status mode"))
        }
    }

    /// Only a plain `.service` or `.timer` name is passed on.
    func test_a_unit_that_is_not_a_unit_name_is_refused_before_anything_runs() {
        for unit in ["sshd", "a/b.service", "-x.service", "fleet-worker@CLAUDE.service", "x.socket", "", "x;reboot.timer"] {
            XCTAssertThrowsError(try exampleBuilder().invocation(for: .unitStatus(unit: unit), mode: .plan), unit) { error in
                XCTAssertEqual(error as? OpsControlError, .invalidArgument("unit name"), unit)
            }
        }
    }

    /// Muster never takes this read on its own as a start.
    @MainActor
    func test_the_unattended_route_never_runs_the_read() async {
        let runner = UnitRunner(answer: timerAnswer)
        let controller = OpsController(runner: runner, makeBuilder: { exampleBuilder() },
                                       auditURL: nil, checksInstall: false)
        let outcome = await controller.runUnattended(.unitStatus(unit: "fleet-server-status.timer"))
        XCTAssertNil(outcome)
        XCTAssertTrue(runner.recorded.isEmpty)
    }

    /// The allowlist's `watch` units are read,  and a malformed one is skipped without
    /// refusing the file.
    func test_the_allowlist_reads_the_watch_units() throws {
        let json = #"""
        {"schemaVersion":1,"mac":{},"server":{"units":[{"unit":"fleet-server-status.timer","scope":"user","actions":["restart"]}],
         "watch":[{"unit":"code-main-sync.timer","scope":"system"},{"unit":"docker.service","scope":"system"},
                  {"unit":"../etc.service","scope":"system"},{"scope":"system"},"nope"],
         "coolify":{"redeploy":true}}}
        """#
        let list = try XCTUnwrap(OpsAllowlist.parse(Data(json.utf8)))
        XCTAssertEqual(list.watchUnits, ["code-main-sync.timer", "docker.service"])
        XCTAssertTrue(list.allowsUnitStatus("fleet-server-status.timer"))
        XCTAssertTrue(list.allowsUnitStatus("docker.service"))
        XCTAssertFalse(list.allowsUnitStatus("sshd.service"))
        // A watch unit grants no verb.
        XCTAssertEqual(list.verbs(forServerUnit: "docker.service"), [])
    }
}

// MARK: - The answer

final class ServerUnitStateDecodingTests: XCTestCase {
    func test_a_realtime_timer_has_a_next_run_and_no_exit_time() throws {
        let action = OpsAction.unitStatus(unit: "fleet-server-status.timer")
        let outcome = parsePlan(timerAnswer, action)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        let state = try XCTUnwrap(outcome.unitState)
        XCTAssertEqual(state.unit, "fleet-server-status.timer")
        XCTAssertEqual(state.scope, "user")
        XCTAssertEqual(state.activeState, "active")
        XCTAssertEqual(state.subState, "waiting")
        XCTAssertEqual(state.unitResult, "success")
        XCTAssertNil(state.lastExitAt)
        XCTAssertTrue(state.isTimer)
        XCTAssertEqual(state.nextElapseAt, try date("2026-11-08T16:15:00Z"))
        XCTAssertEqual(state.checkedAt, try date("2026-11-08T16:02:00Z"))
        XCTAssertEqual(ServerUnitStatusReading.from(outcome), .state(state))
        let now = try date("2026-11-08T16:05:00Z")
        XCTAssertEqual(state.timesText(now: now), "Next run 10:15 AM.\u{00A0} Last result:  success.")
    }

    func test_a_service_has_an_exit_time_and_no_next_run() throws {
        let outcome = parsePlan(serviceAnswer, .unitStatus(unit: "docker.service"))
        let state = try XCTUnwrap(outcome.unitState)
        XCTAssertEqual(state.subState, "running")
        XCTAssertFalse(state.isTimer)
        XCTAssertNil(state.nextElapseAt)
        XCTAssertEqual(state.lastExitAt, try date("2026-11-08T15:40:00Z"))
        XCTAssertEqual(state.timesText(now: try date("2026-11-08T16:05:00Z")),
                       "Last exited 9:40 AM.\u{00A0} Last result:  success.")
    }

    func test_a_monotonic_timer_has_no_next_run_time_and_says_why() throws {
        let outcome = parsePlan(monotonicAnswer, .unitStatus(unit: "code-main-sync.timer"))
        let state = try XCTUnwrap(outcome.unitState)
        XCTAssertTrue(state.isTimer)
        XCTAssertNil(state.nextElapseAt)
        XCTAssertNil(state.lastExitAt)
        XCTAssertTrue(state.timesText(now: try date("2026-11-08T16:05:00Z"))?
            .hasPrefix("No next run time:") == true)
    }

    /// A timer's `lastTriggerAt` is when it last fired,  said on Jay's clock before the next run,
    /// even when the next run has no wall-clock time.
    func test_a_timer_says_when_it_last_ran() throws {
        let answer = unitAnswer(
            "code-main-sync.timer",
            #""scope":"system","activeState":"active","subState":"waiting","unitResult":"success","lastExitAt":null,"nextElapseAt":null,"lastTriggerAt":"2026-11-08T15:50:00Z","checkedAt":"2026-11-08T16:02:00Z""#
        )
        let state = try XCTUnwrap(parsePlan(answer, .unitStatus(unit: "code-main-sync.timer")).unitState)
        XCTAssertTrue(state.isTimer)
        XCTAssertNil(state.nextElapseAt)
        XCTAssertEqual(state.lastTriggerAt, try date("2026-11-08T15:50:00Z"))
        XCTAssertEqual(state.timesText(now: try date("2026-11-08T16:05:00Z")),
                       "Last ran 9:50 AM.\u{00A0} No next run time:  it runs on a schedule counted from boot or its last run.\u{00A0} Last result:  success.")

        // With a next run too,  the last run comes first.
        let both = unitAnswer(
            "fleet-server-status.timer",
            #""scope":"user","activeState":"active","subState":"waiting","unitResult":"success","lastExitAt":null,"nextElapseAt":"2026-11-08T16:15:00Z","lastTriggerAt":"2026-11-08T16:00:00Z","checkedAt":"2026-11-08T16:02:00Z""#
        )
        let timer = try XCTUnwrap(parsePlan(both, .unitStatus(unit: "fleet-server-status.timer")).unitState)
        XCTAssertEqual(timer.timesText(now: try date("2026-11-08T16:05:00Z")),
                       "Last ran 10:00 AM.\u{00A0} Next run 10:15 AM.\u{00A0} Last result:  success.")
    }

    /// A timer that has not fired since boot gives `lastTriggerAt` as null,  and one that will not
    /// parse is left out:  neither says when it last ran.
    func test_a_timer_with_no_last_run_time_says_nothing_about_it() throws {
        for value in ["null", #""yesterday""#, "7"] {
            let answer = unitAnswer(
                "fleet-server-status.timer",
                #""scope":"user","activeState":"active","subState":"waiting","unitResult":"success","lastExitAt":null,"nextElapseAt":"2026-11-08T16:15:00Z","lastTriggerAt":"#
                    + value + #","checkedAt":"2026-11-08T16:02:00Z""#
            )
            let state = try XCTUnwrap(parsePlan(answer, .unitStatus(unit: "fleet-server-status.timer")).unitState, value)
            XCTAssertNil(state.lastTriggerAt, value)
            XCTAssertEqual(state.timesText(now: try date("2026-11-08T16:05:00Z")),
                           "Next run 10:15 AM.\u{00A0} Last result:  success.", value)
        }
    }

    /// A service carries no `lastTriggerAt` key,  and a stray one on a service is not read.
    func test_a_service_has_no_last_run_time() throws {
        let state = try XCTUnwrap(parsePlan(serviceAnswer, .unitStatus(unit: "docker.service")).unitState)
        XCTAssertFalse(state.isTimer)
        XCTAssertNil(state.lastTriggerAt)

        let stray = unitAnswer(
            "docker.service",
            #""scope":"system","activeState":"active","subState":"running","unitResult":"success","lastExitAt":"2026-11-08T15:40:00Z","lastTriggerAt":"2026-11-08T15:50:00Z","checkedAt":"2026-11-08T16:02:00Z""#
        )
        let service = try XCTUnwrap(parsePlan(stray, .unitStatus(unit: "docker.service")).unitState)
        XCTAssertNil(service.lastTriggerAt)
        XCTAssertEqual(service.timesText(now: try date("2026-11-08T16:05:00Z")),
                       "Last exited 9:40 AM.\u{00A0} Last result:  success.")
    }

    /// A null result is no result,  not a word.
    func test_a_unit_with_no_result_yet_keeps_its_state() throws {
        let answer = unitAnswer(
            "docker.service",
            #""scope":"system","activeState":"activating","subState":"start","unitResult":null,"lastExitAt":null,"checkedAt":"2026-11-08T16:02:00Z""#
        )
        let state = try XCTUnwrap(parsePlan(answer, .unitStatus(unit: "docker.service")).unitState)
        XCTAssertNil(state.unitResult)
        XCTAssertEqual(state.activeState, "activating")
        XCTAssertNil(state.timesText())
    }

    /// A state word out of systemd's shape is not reported,  and neither is anything about
    /// another unit,  another verb,  or an answer with no check time.
    func test_a_state_muster_cannot_vouch_for_is_not_reported() {
        let action = OpsAction.unitStatus(unit: "docker.service")
        let checked = #""checkedAt":"2026-11-08T16:02:00Z""#
        let bad: [String] = [
            unitAnswer("docker.service", #""activeState":"Active","subState":"running","# + checked),
            unitAnswer("docker.service", #""activeState":"active","subState":"run ning","# + checked),
            unitAnswer("docker.service", #""activeState":"active","subState":"\u001b[31m","# + checked),
            unitAnswer("docker.service", #""activeState":"1active","subState":"running","# + checked),
            unitAnswer("docker.service", #""activeState":"active","subState":""# + String(repeating: "a", count: 33) + #"","# + checked),
            unitAnswer("docker.service", #""activeState":"active","subState":"running","checkedAt":"yesterday""#),
            unitAnswer("docker.service", #""activeState":"active","subState":"running""#),
            unitAnswer("docker.service", #""activeState":7,"subState":"running","# + checked),
            unitAnswer("tailscaled.service", #""activeState":"active","subState":"running","# + checked),
            #"{"ok":true,"verb":"server logs","result":"planned","commands":[],"unit":"docker.service","activeState":"active","subState":"running","# + checked + "}",
        ]
        for answer in bad {
            let outcome = parsePlan(answer, action)
            XCTAssertNil(outcome.unitState, answer)
            if outcome.isSuccess(for: .plan) {
                guard case .unavailable = ServerUnitStatusReading.from(outcome) else {
                    return XCTFail(answer)
                }
            }
        }
        // A result word out of shape is left out,  and the state is kept.
        let oddResult = unitAnswer("docker.service", #""activeState":"active","subState":"running","unitResult":"Exit Code","# + checked)
        XCTAssertNil(parsePlan(oddResult, action).unitState?.unitResult)
        XCTAssertEqual(parsePlan(oddResult, action).unitState?.subState, "running")
        // The state is a plan's:  the same answer read as a run surfaces nothing.
        XCTAssertNil(parseRun(serviceAnswer, action).unitState)
        // And it is only ever surfaced for this action.
        XCTAssertNil(parsePlan(serviceAnswer, .resourceStatus).unitState)
    }

    func test_the_state_word_shape() {
        for word in ["active", "a", "dead", "exit-code", "start-pre", "auto-restart", String(repeating: "a", count: 32)] {
            XCTAssertTrue(ServerUnitState.isStateWord(word), word)
        }
        for word in ["", "Active", "-x", "9x", "a b", "a_b", "é", String(repeating: "a", count: 33)] {
            XCTAssertFalse(ServerUnitState.isStateWord(word), word)
        }
    }

    /// Each refusal says why in a plain sentence,  and never shows a state.
    func test_each_refusal_has_a_plain_cause() {
        let unit = "fleet-server-status.timer"
        let action = OpsAction.unitStatus(unit: unit)
        let cases: [(String, String?, String)] = [
            ("unit status is read only; pass --plan", "read_only_required",
             "The server reads a unit's state only as a plan, so nothing was read."),
            ("the unit status is unavailable", "unit_status_unavailable",
             "The server could not ask systemd for this unit's state, so it is not known."),
            ("the unit is missing or unavailable", "unit_unavailable",
             "systemd on the server does not have this unit loaded, so it has no state to report."),
            ("the unit status is invalid", "unit_status_invalid",
             "systemd's answer was not in the shape the server accepts, so no state is reported."),
            ("the unit is not in the allowlist", nil,
             "This unit is not on the server's list, so the server does not read its state."),
            ("the unit is not a valid unit name", nil, "This is not a unit name the server reads."),
            ("something new", nil, "The server did not report this unit's state."),
        ]
        for (detail, code, cause) in cases {
            let outcome = parsePlan(refusal(unit, detail: detail, code: code), action, exit: 2)
            XCTAssertEqual(outcome.kind, .refused, detail)
            XCTAssertEqual(outcome.errorCode, code, detail)
            XCTAssertNil(outcome.unitState, detail)
            XCTAssertEqual(ServerUnitStatusReading.from(outcome), .unavailable(cause), detail)
            XCTAssertEqual(outcome.resultTitle(for: action), "The server refused this, so nothing ran.")
        }
        // The codes get the sheet's advice too.
        let invalid = parsePlan(refusal(unit, detail: "x", code: "unit_status_invalid"), action, exit: 2)
        XCTAssertEqual(invalid.advice(for: action),
                       "systemd's answer was not in the shape the server accepts, so no state is reported.")
        let readOnly = parsePlan(refusal(unit, detail: "x", code: "read_only_required"), action, exit: 2)
        XCTAssertEqual(readOnly.advice(for: action),
                       "This only reads, so the server takes it as a plan and never as a run.")
    }

    /// A read that timed out or gave no answer is a plain sentence,  never a state.
    func test_a_lost_read_is_not_a_state() {
        let action = OpsAction.unitStatus(unit: "docker.service")
        let lost = OpsResultParser.parse(OpsProcessResult(exitCode: 255, stdout: "", stderr: ""),
                                         mode: .plan, action: action, timeout: 90)
        guard case .unavailable(let cause) = ServerUnitStatusReading.from(lost) else {
            return XCTFail("\(lost)")
        }
        XCTAssertFalse(cause.isEmpty)
    }
}

// MARK: - The "Now:" line

final class ServerUnitStateNowLineTests: XCTestCase {
    private func state() throws -> ServerUnitState {
        try XCTUnwrap(parsePlan(timerAnswer, .unitStatus(unit: "fleet-server-status.timer")).unitState)
    }

    func test_the_now_line_names_the_state_and_the_time_on_jays_clock() throws {
        let now = try date("2026-11-08T16:05:00Z")
        XCTAssertEqual(try state().nowText(now: now), "Now:  active (waiting), checked 10:02 AM")
    }

    func test_the_now_line_is_marked_newer_than_an_older_report() throws {
        let now = try date("2026-11-08T16:05:00Z")
        let state = try state()
        XCTAssertEqual(state.nowLine(reportGeneratedAt: try date("2026-11-08T15:50:00Z"), now: now),
                       "Now:  active (waiting), checked 10:02 AM, newer than the server's report")
        XCTAssertEqual(state.nowLine(reportGeneratedAt: nil, now: now),
                       "Now:  active (waiting), checked 10:02 AM, newer than the server's report")
        // A report written after the read is not older,  so the line is not marked.
        XCTAssertEqual(state.nowLine(reportGeneratedAt: try date("2026-11-08T16:03:00Z"), now: now),
                       "Now:  active (waiting), checked 10:02 AM")
        XCTAssertFalse(state.isNewer(thanReport: try date("2026-11-08T16:02:00Z")))
    }

    /// A read from another day names the day,  so it never reads as today.
    func test_a_read_from_another_day_names_the_day() throws {
        let tomorrow = try date("2026-11-09T18:00:00Z")
        XCTAssertEqual(try state().nowText(now: tomorrow), "Now:  active (waiting), checked Sun, Nov 8, 10:02 AM")
    }

    func test_the_line_after_a_run() throws {
        let now = try date("2026-11-08T16:05:00Z")
        let report = try date("2026-11-08T15:50:00Z")
        XCTAssertNil(ServerUnitStateAfterRun(unit: "x.timer", reading: nil).line(reportGeneratedAt: report, now: now))
        XCTAssertTrue(ServerUnitStateAfterRun(unit: "x.timer", reading: nil).isReading)
        XCTAssertEqual(ServerUnitStateAfterRun(unit: "fleet-server-status.timer", reading: .state(try state()))
                        .line(reportGeneratedAt: report, now: now),
                       "Now:  active (waiting), checked 10:02 AM, newer than the server's report")
        XCTAssertEqual(ServerUnitStateAfterRun(unit: "x.timer", reading: .unavailable("Why."))
                        .line(reportGeneratedAt: report, now: now),
                       "Now:  not known.\u{00A0} Why.")
    }
}

// MARK: - The reader and the controller

final class ServerUnitStatusReaderTests: XCTestCase {
    private func reader(_ runner: UnitRunner) -> OpsUnitStatusReader {
        OpsUnitStatusReader(runner: runner, makeBuilder: { exampleBuilder() })
    }

    func test_a_listed_unit_is_read_over_one_plan() async throws {
        let runner = UnitRunner(answer: timerAnswer)
        let reading = await reader(runner).read(unit: "fleet-server-status.timer", allowlist: allowlist)
        XCTAssertEqual(reading.state?.subState, "waiting")
        XCTAssertEqual(runner.recorded.count, 1)
        XCTAssertEqual(Array(runner.recorded[0].suffix(5)),
                       ["server", "unit-status", "fleet-server-status.timer", "--plan", "--json"])
        XCTAssertFalse(runner.recorded[0].contains("--yes"))
    }

    func test_a_watch_unit_is_read() async {
        let runner = UnitRunner(answer: serviceAnswer)
        let reading = await reader(runner).read(unit: "docker.service", allowlist: allowlist)
        XCTAssertEqual(reading.state?.activeState, "active")
    }

    /// A unit this Mac's allowlist does not list,  or an allowlist that could not be read,
    /// sends nothing.
    func test_an_unlisted_unit_or_an_unread_allowlist_sends_nothing() async {
        let runner = UnitRunner(answer: serviceAnswer)
        let unlisted = await reader(runner).read(unit: "sshd.service", allowlist: allowlist)
        let unread = await reader(runner).read(unit: "docker.service", allowlist: nil)
        guard case .unavailable = unlisted, case .unavailable = unread else {
            return XCTFail("\(unlisted) \(unread)")
        }
        XCTAssertTrue(runner.recorded.isEmpty)
    }
}

@MainActor
final class OpsControllerUnitStateTests: XCTestCase {
    private let restart = OpsAction.serverUnit(verb: .restart, unit: "fleet-server-status.timer")
    private let planned = #"{"ok":true,"commands":[["/usr/bin/systemctl","--user","restart","fleet-server-status.timer"]],"result":"planned","detail":"user unit"}"#
    private let done = #"{"ok":true,"commands":[["/usr/bin/systemctl","--user","restart","fleet-server-status.timer"]],"result":"done","detail":"done","completedCommands":1}"#

    private func runner() -> UnitRunner {
        let planned = self.planned
        let done = self.done
        return UnitRunner { argv in
            if argv.contains("unit-status") { return OpsProcessResult(exitCode: 0, stdout: timerAnswer, stderr: "") }
            return OpsProcessResult(exitCode: 0, stdout: argv.contains("--plan") ? planned : done, stderr: "")
        }
    }

    private func waitUntil(_ condition: () -> Bool, file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("timed out", file: file, line: line)
        throw CancellationError()
    }

    private func reviewing(_ ops: OpsController) -> Bool {
        if case .review = ops.phase { return true }
        return false
    }

    private func finished(_ ops: OpsController) -> Bool {
        if case .finished = ops.phase { return true }
        return false
    }

    /// The app's controller reads the unit again right after a run of it ends.
    func test_the_unit_is_read_again_after_its_run() async throws {
        let fake = runner()
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: nil,
                                checksInstall: false, readsUnitStateAfterRun: true,
                                readAllowlist: { allowlist })
        XCTAssertTrue(ops.plan(restart))
        try await waitUntil { reviewing(ops) }
        XCTAssertNil(ops.unitStateAfterRun)
        ops.confirm()
        try await waitUntil { finished(ops) }
        try await waitUntil { ops.unitStateAfterRun?.reading != nil }
        XCTAssertEqual(ops.unitStateAfterRun?.unit, "fleet-server-status.timer")
        XCTAssertEqual(ops.unitStateAfterRun?.reading?.state?.subState, "waiting")
        XCTAssertEqual(fake.recorded.count, 4)
        XCTAssertEqual(Array(fake.recorded[3].suffix(5)),
                       ["server", "unit-status", "fleet-server-status.timer", "--plan", "--json"])
        XCTAssertEqual(fake.recorded.filter { $0.contains("--yes") }.count, 1)
        XCTAssertFalse(ops.isBusy)
        // A new plan forgets it.
        ops.dismiss()
        XCTAssertTrue(ops.plan(restart))
        XCTAssertNil(ops.unitStateAfterRun)
    }

    /// A controller that was not asked to read sends nothing more than the run.
    func test_a_controller_that_does_not_read_sends_nothing_more() async throws {
        let fake = runner()
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: nil,
                                checksInstall: false, readAllowlist: { allowlist })
        ops.plan(restart)
        try await waitUntil { reviewing(ops) }
        ops.confirm()
        try await waitUntil { finished(ops) }
        for _ in 0..<10 { await Task.yield() }
        XCTAssertNil(ops.unitStateAfterRun)
        XCTAssertEqual(fake.recorded.count, 3)
    }

    /// The sheet can ask for a read itself.
    func test_the_sheet_can_ask_for_a_read() async throws {
        let fake = runner()
        let ops = OpsController(runner: fake, makeBuilder: { exampleBuilder() }, auditURL: nil,
                                checksInstall: false, readAllowlist: { allowlist })
        ops.readUnitState("docker.service")
        XCTAssertEqual(ops.unitStateAfterRun, ServerUnitStateAfterRun(unit: "docker.service", reading: nil))
        try await waitUntil { ops.unitStateAfterRun?.reading != nil }
        // The fake answers with the timer's answer,  which is about another unit.
        guard case .some(.unavailable) = ops.unitStateAfterRun?.reading else {
            return XCTFail("\(String(describing: ops.unitStateAfterRun))")
        }
        XCTAssertFalse(ops.isBusy)
    }
}

// MARK: - Logs:  the unit read

final class OpsLogsUnitTests: XCTestCase {
    private let logs = OpsAction.logs(unit: "fleet-server-status.timer")

    func test_a_timers_logs_name_the_service_read() {
        let done = #"{"ok":true,"verb":"server logs","target":"fleet-server-status.timer","commands":[["journalctl","--user","-u","fleet-server-status.service","-n","200","--no-pager"]],"result":"done","detail":"2 journal lines","output":"a\nb\n","lines":2,"logsUnit":"fleet-server-status.service"}"#
        let outcome = parseRun(done, logs)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        XCTAssertEqual(outcome.logsUnit, "fleet-server-status.service")
        XCTAssertEqual(outcome.logsHeading, "Logs of fleet-server-status.service")
        XCTAssertEqual(logs.title(plan: outcome), "Read the last logs of fleet-server-status.service on the server")
        // The plan carries it too.
        let plan = parsePlan(done.replacingOccurrences(of: #""result":"done""#, with: #""result":"planned""#), logs)
        XCTAssertEqual(plan.logsUnit, "fleet-server-status.service")
        // A failed run keeps it,  so its words name the same unit.
        let failed = parseRun(#"{"ok":false,"result":"failed","commands":[],"logsUnit":"fleet-server-status.service"}"#, logs, exit: 1)
        XCTAssertEqual(failed.logsHeading, "Logs of fleet-server-status.service")
    }

    func test_a_service_that_reads_its_own_journal_keeps_its_title() {
        let action = OpsAction.logs(unit: "docker.service")
        let outcome = parseRun(#"{"ok":true,"result":"done","commands":[],"logsUnit":"docker.service"}"#, action)
        XCTAssertEqual(action.title(plan: outcome), action.title)
        XCTAssertEqual(outcome.logsHeading, "Logs of docker.service")
    }

    func test_a_logs_unit_out_of_shape_is_not_shown() {
        for bad in ["../etc/passwd", "x", "a b.service", "fleet-worker@X.service"] {
            let outcome = parseRun(#"{"ok":true,"result":"done","commands":[],"logsUnit":""# + bad + #""}"#, logs)
            XCTAssertNil(outcome.logsUnit, bad)
            XCTAssertNil(outcome.logsHeading, bad)
            XCTAssertEqual(logs.title(plan: outcome), logs.title, bad)
        }
        // And it is never read off another action's answer.
        let other = parseRun(#"{"ok":true,"result":"done","commands":[],"logsUnit":"docker.service"}"#,
                             .serverUnit(verb: .restart, unit: "fleet-server-status.timer"))
        XCTAssertNil(other.logsUnit)
    }
}

// MARK: - Redeploy:  the server's resource name

final class OpsRedeployNameTests: XCTestCase {
    private func plan(_ name: String) -> String {
        #"{"ok":true,"verb":"server redeploy","target":"abc123def456","commands":[["coolify","GET","/api/v1/deploy?uuid=abc123def456"]],"result":"planned","detail":"redeploy","resourceName":""# + name + #""}"#
    }

    @MainActor
    func test_the_servers_name_is_preferred_once_a_plan_gave_one() {
        let action = OpsAction.coolifyRedeploy(uuid: "abc123def456", name: "muster-web (app)")
        let outcome = parsePlan(plan("muster-web"), action)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertFalse(outcome.stoppedOnMac)
        XCTAssertEqual(outcome.resourceName, "muster-web")
        XCTAssertEqual(action.title, "Redeploy muster-web (app) on the server")
        XCTAssertEqual(action.title(plan: outcome), "Redeploy muster-web on the server")
        XCTAssertEqual(action.title(plan: nil), action.title)
        XCTAssertEqual(OpsApprovalRequest(action: action, reviewed: outcome).reason, "Redeploy muster-web on the server")
        XCTAssertEqual(OpsController.Phase.review(action, outcome).title, "Redeploy muster-web on the server")
        XCTAssertEqual(OpsController.Phase.planning(action).title, action.title)
        XCTAssertNil(OpsController.Phase.idle.title)
        // The run's answer repeats it.
        let done = parseRun(#"{"ok":true,"result":"done","verified":true,"executionState":"completed","commands":[],"resourceName":"muster-web"}"#, action)
        XCTAssertEqual(done.resourceName, "muster-web")
    }

    /// The server spells a name in printable ASCII,  so this Mac's name is put in that shape
    /// before the two are compared.
    func test_a_name_in_the_servers_spelling_is_the_same_name() {
        XCTAssertTrue(OpsRedeployName.matches(muster: "Caf\u{00E9} App", server: "Caf? App"))
        XCTAssertTrue(OpsRedeployName.matches(muster: "muster-web", server: "muster-web"))
        XCTAssertTrue(OpsRedeployName.matches(muster: "muster-web (app)", server: "muster-web"))
        XCTAssertTrue(OpsRedeployName.matches(muster: String(repeating: "n", count: 130), server: String(repeating: "n", count: 120)))
        // This Mac had no name,  so there is nothing to compare.
        XCTAssertTrue(OpsRedeployName.matches(muster: "this resource", server: "coolify resource"))
        XCTAssertTrue(OpsRedeployName.matches(muster: "", server: "anything"))
        XCTAssertFalse(OpsRedeployName.matches(muster: "muster-web", server: "other-app"))
        XCTAssertFalse(OpsRedeployName.matches(muster: "muster-web", server: "coolify resource"))
        XCTAssertEqual(OpsRedeployName.displayed("Caf\u{00E9}\u{0007} App"), "Caf?? App")
        XCTAssertNil(OpsRedeployName.displayed("   "))
    }

    /// The server's stand-in for a row with no name never replaces this Mac's words.
    func test_the_servers_stand_in_name_keeps_this_macs_words() {
        let action = OpsAction.coolifyRedeploy(uuid: "abc123def456", name: "this resource")
        let outcome = parsePlan(plan("coolify resource"), action)
        XCTAssertTrue(outcome.isSuccess(for: .plan))
        XCTAssertEqual(action.title(plan: outcome), "Redeploy this resource on the server")
    }

    /// A plan that names another resource is stopped before the yes step,  and says both names.
    func test_a_plan_that_names_another_resource_is_stopped() {
        let action = OpsAction.coolifyRedeploy(uuid: "abc123def456", name: "muster-web")
        let outcome = parsePlan(plan("other-app"), action)
        XCTAssertFalse(outcome.isSuccess(for: .plan))
        XCTAssertEqual(outcome.kind, .refused)
        XCTAssertTrue(outcome.stoppedOnMac)
        XCTAssertEqual(outcome.detail,
                       "Muster planned a redeploy of \"muster-web\", but the server's plan names \"other-app\", so nothing will run.\u{00A0} Read the server's report again, then plan the redeploy again.")
        XCTAssertEqual(outcome.resultTitle(for: action), "Muster stopped this before it ran, so nothing ran.")
    }

    @MainActor
    func test_a_mismatched_plan_never_reaches_the_yes_step() async throws {
        let runner = UnitRunner(answer: plan("other-app"))
        let ops = OpsController(runner: runner, makeBuilder: { exampleBuilder() }, auditURL: nil, checksInstall: false)
        let action = OpsAction.coolifyRedeploy(uuid: "abc123def456", name: "muster-web")
        XCTAssertTrue(ops.plan(action))
        for _ in 0..<400 {
            if case .finished = ops.phase { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        guard case .finished(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertTrue(outcome.stoppedOnMac)
        XCTAssertNil(ops.beginApproval())
        XCTAssertFalse(runner.recorded.contains { $0.contains("--yes") })
    }

    /// A resource renamed between the review and the approval is shown again,  not run.
    @MainActor
    func test_a_resource_renamed_after_review_is_not_run() async throws {
        let lock = NSLock()
        var plans = 0
        let first = plan("muster-web")
        let second = plan("muster-web-2")
        let runner = UnitRunner { _ in
            lock.lock(); plans += 1; let count = plans; lock.unlock()
            return OpsProcessResult(exitCode: 0, stdout: count == 1 ? first : second, stderr: "")
        }
        let ops = OpsController(runner: runner, makeBuilder: { exampleBuilder() }, auditURL: nil, checksInstall: false)
        // This Mac had no name,  so either name passes the first check;  only the change stops it.
        ops.plan(.coolifyRedeploy(uuid: "abc123def456", name: "this resource"))
        for _ in 0..<400 {
            if case .review = ops.phase { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        ops.confirm()
        for _ in 0..<400 {
            if case .review(_, let outcome) = ops.phase, outcome.resourceName == "muster-web-2" { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        guard case .review(_, let outcome) = ops.phase else { return XCTFail("\(ops.phase)") }
        XCTAssertEqual(outcome.resourceName, "muster-web-2")
        XCTAssertNotNil(ops.notice)
        XCTAssertFalse(runner.recorded.contains { $0.contains("--yes") })
    }
}

// MARK: - The Mac agents' role and stop consequence

final class MacAgentNotesTests: XCTestCase {
    private func agents(_ rows: String) throws -> [MacAgent] {
        let json = #"{"schemaVersion":1,"tool":"fleet-mac-services","generatedAt":"2026-11-08T16:00:00Z","host":"mac","agents":["# + rows + "]}"
        return try JSONDecoder().decode(MacServicesReport.self, from: Data(json.utf8)).agents
    }

    func test_role_and_stop_consequence_are_read() throws {
        let rows = try agents(#"""
        {"label":"com.jay.fleet-tools-doctor","plistInRepo":true,"installed":true,"loaded":true,"pid":1,"lastExitStatus":0,"controllable":true,"role":"reporting","stopConsequence":"Stopping this stops the Mac fetching the server's report."},
        {"label":"com.jay.simple-tools-runtime-sync","plistInRepo":true,"installed":true,"loaded":true,"pid":null,"lastExitStatus":0,"controllable":true,"role":"updater"},
        {"label":"com.jay.operator-notes-runner","plistInRepo":true,"installed":true,"loaded":true,"pid":null,"lastExitStatus":0,"controllable":true,"stopConsequence":"Owner notes wait until it starts again."},
        {"label":"com.jay.mac-cleanup","plistInRepo":true,"installed":true,"loaded":true,"pid":null,"lastExitStatus":0,"controllable":true}
        """#)
        XCTAssertEqual(rows[0].role, "reporting")
        XCTAssertEqual(rows[0].stopConsequence, "Stopping this stops the Mac fetching the server's report.")
        XCTAssertEqual(rows[1].role, "updater")
        XCTAssertNil(rows[1].stopConsequence)
        XCTAssertNil(rows[2].role)
        XCTAssertEqual(rows[2].stopConsequence, "Owner notes wait until it starts again.")
        XCTAssertNil(rows[3].role)
        XCTAssertNil(rows[3].stopConsequence)
    }

    func test_a_note_out_of_shape_is_not_reported() throws {
        let long = String(repeating: "x", count: 201)
        let rows = try agents(#"""
        {"label":"a","role":"boss","stopConsequence":""},
        {"label":"b","role":3,"stopConsequence":"two\nlines"},
        {"label":"c","role":null,"stopConsequence":"Café"},
        {"label":"d","stopConsequence":"\#(long)"},
        {"label":"e","stopConsequence":["x"]}
        """#)
        for row in rows {
            XCTAssertNil(row.role, row.label)
            XCTAssertNil(row.stopConsequence, row.label)
        }
        XCTAssertTrue(MacAgent.isStopConsequence(String(repeating: "y", count: 200)))
    }

    /// The plan of a stop says what depends on the agent and what stopping it costs,  from that
    /// agent's own row,  and says nothing for any other action.
    func test_the_plan_of_a_stop_carries_the_agents_note() throws {
        let rows = try agents(#"""
        {"label":"com.jay.fleet-tools-doctor","controllable":true,"role":"reporting","stopConsequence":"Stopping this stops the fleet tools report."},
        {"label":"com.jay.simple-tools-runtime-sync","controllable":true,"role":"updater"},
        {"label":"com.jay.operator-notes-runner","controllable":true,"stopConsequence":"Owner notes wait until it starts again."},
        {"label":"com.jay.mac-cleanup","controllable":true}
        """#)
        XCTAssertEqual(rows[0].roleSentence, "This agent writes a report Muster reads.")
        XCTAssertEqual(rows[1].roleSentence, "This agent keeps the fleet tools on the Mac up to date.")
        XCTAssertNil(rows[2].roleSentence)

        let stop = { (label: String) in OpsAction.macAgent(verb: .stop, label: label) }
        XCTAssertEqual(MacAgent.stopNote(for: stop("com.jay.fleet-tools-doctor"), agents: rows),
                       "This agent writes a report Muster reads.\u{00A0} Stopping this stops the fleet tools report.")
        XCTAssertEqual(MacAgent.stopNote(for: stop("com.jay.simple-tools-runtime-sync"), agents: rows),
                       "This agent keeps the fleet tools on the Mac up to date.")
        XCTAssertEqual(MacAgent.stopNote(for: stop("com.jay.operator-notes-runner"), agents: rows),
                       "Owner notes wait until it starts again.")
        // An agent with neither,  or one the file does not list,  has no note:  the general
        // stop warning stands alone.
        XCTAssertNil(MacAgent.stopNote(for: stop("com.jay.mac-cleanup"), agents: rows))
        XCTAssertNil(MacAgent.stopNote(for: stop("com.jay.unknown"), agents: rows))
        XCTAssertNil(MacAgent.stopNote(for: stop("com.jay.fleet-tools-doctor"), agents: []))
        // Only a stop of a LaunchAgent carries it.
        XCTAssertNil(MacAgent.stopNote(for: .macAgent(verb: .restart, label: "com.jay.fleet-tools-doctor"), agents: rows))
        XCTAssertNil(MacAgent.stopNote(for: .macAgent(verb: .start, label: "com.jay.fleet-tools-doctor"), agents: rows))
        XCTAssertNil(MacAgent.stopNote(for: .serverUnit(verb: .stop, unit: "com.jay.fleet-tools-doctor"), agents: rows))
        XCTAssertNil(MacAgent.stopNote(for: .pm2App(verb: .stop, name: "com.jay.fleet-tools-doctor"), agents: rows))
        // The sheet reads the agents only for such a stop.
        XCTAssertTrue(MacAgent.mayHaveStopNote(stop("com.jay.fleet-tools-doctor")))
        XCTAssertFalse(MacAgent.mayHaveStopNote(.macAgent(verb: .restart, label: "com.jay.fleet-tools-doctor")))
        XCTAssertFalse(MacAgent.mayHaveStopNote(.serverUnit(verb: .stop, unit: "fleet-server-status.timer")))
        XCTAssertFalse(MacAgent.mayHaveStopNote(nil))
        // The general warning is still the plan's,  beside the note.
        XCTAssertEqual(stop("com.jay.fleet-tools-doctor").stopWarning, FleetServices.launchAgentStopBody)
    }
}

// MARK: - Review findings on PR 1533

final class UnitStatusReviewFindingTests: XCTestCase {
    /// A timer that is not active has no next run because it is not scheduled,  not because
    /// its schedule is counted from boot (Sentry,  PR 1533).
    func test_a_timer_that_is_not_active_says_it_is_not_scheduled() throws {
        let now = try date("2026-11-08T16:05:00Z")
        for (active, sub) in [("inactive", "dead"), ("failed", "failed")] {
            let answer = unitAnswer(
                "code-main-sync.timer",
                #""scope":"system","activeState":""# + active + #"","subState":""# + sub
                    + #"","unitResult":"success","lastExitAt":null,"nextElapseAt":null,"checkedAt":"2026-11-08T16:02:00Z""#
            )
            let state = try XCTUnwrap(parsePlan(answer, .unitStatus(unit: "code-main-sync.timer")).unitState, active)
            XCTAssertTrue(state.isTimer, active)
            XCTAssertNil(state.nextElapseAt, active)
            XCTAssertEqual(state.timesText(now: now),
                           "No next run:  the timer is not scheduled while it is \(active).\u{00A0} Last result:  success.",
                           active)
        }
        // An active timer with no wall-clock time still says its schedule is counted from boot.
        let active = try XCTUnwrap(parsePlan(monotonicAnswer, .unitStatus(unit: "code-main-sync.timer")).unitState)
        XCTAssertEqual(active.timesText(now: now),
                       "No next run time:  it runs on a schedule counted from boot or its last run.\u{00A0} Last result:  success.")
    }

    /// A long name with a service suffix still matches:  the suffix comes off the whole name
    /// before it is cut to 120 characters (Kody,  PR 1533).
    func test_a_long_name_with_a_service_suffix_matches_the_servers_name() {
        let stem = String(repeating: "a", count: 118)
        let muster = stem + " (my-service)"
        XCTAssertGreaterThan(muster.count, OpsRedeployName.maximumLength)
        // Cut first,  the name would end in " (" and the suffix could not be found.
        XCTAssertEqual(OpsRedeployName.displayed(muster), stem + " (")
        XCTAssertTrue(OpsRedeployName.matches(muster: muster, server: stem))
        XCTAssertFalse(OpsRedeployName.matches(muster: muster, server: String(repeating: "b", count: 118)))
        // A stem longer than the limit is cut to it,  as the server cuts it.
        let longStem = String(repeating: "c", count: 125)
        XCTAssertTrue(OpsRedeployName.matches(muster: longStem + " (svc)", server: String(repeating: "c", count: 120)))
        // Non-ASCII in a long name is shaped before the suffix is taken off.
        let shaped = String(repeating: "d", count: 117) + "\u{00E9}" + " (svc)"
        XCTAssertTrue(OpsRedeployName.matches(muster: shaped, server: String(repeating: "d", count: 117) + "?"))
    }

    /// A logs answer that names another verb names no journal (Kody,  PR 1533).
    func test_a_logs_unit_from_an_answer_for_another_verb_is_ignored() {
        let logs = OpsAction.logs(unit: "fleet-server-status.timer")
        func run(verb: String?) -> OpsOutcome {
            let named = verb.map { #""verb":""# + $0 + #"","# } ?? ""
            return parseRun(#"{"ok":true,"# + named + #""result":"done","commands":[],"output":"a\n","lines":1,"logsUnit":"other.service"}"#, logs)
        }
        XCTAssertEqual(OpsAction.logsVerb, "server logs")
        XCTAssertEqual(run(verb: "server logs").logsUnit, "other.service")
        XCTAssertEqual(run(verb: nil).logsUnit, "other.service")
        for other in ["server restart", "server unit-status", "server eavesdropper-status", ""] {
            let outcome = run(verb: other)
            XCTAssertNil(outcome.logsUnit, other)
            XCTAssertNil(outcome.logsHeading, other)
            XCTAssertEqual(logs.title(plan: outcome), logs.title, other)
        }
    }

    /// The Touch ID question and the sheet's title are the plan-aware title (Kody,  PR 1533).
    @MainActor
    func test_the_approval_question_and_the_sheet_title_name_the_unit_read() {
        let logs = OpsAction.logs(unit: "fleet-server-status.timer")
        let plan = parsePlan(#"{"ok":true,"verb":"server logs","result":"planned","commands":[],"logsUnit":"fleet-server-status.service"}"#, logs)
        let expected = "Read the last logs of fleet-server-status.service on the server"
        XCTAssertEqual(OpsApprovalRequest(action: logs, reviewed: plan).reason, expected)
        XCTAssertEqual(OpsController.Phase.review(logs, plan).title, expected)
        XCTAssertEqual(OpsController.Phase.running(logs, plan).title, expected)
        XCTAssertEqual(OpsController.Phase.finished(logs, plan).title, expected)
        XCTAssertEqual(OpsController.Phase.failed(logs, "x").title, logs.title)
    }

    /// The journal block is headed with the unit read when the answer named it (Kody,  PR 1533).
    func test_the_journal_block_names_the_unit_read() {
        let logs = OpsAction.logs(unit: "fleet-server-status.timer")
        let named = parseRun(#"{"ok":true,"verb":"server logs","result":"done","commands":[],"output":"a\nb\n","lines":2,"logsUnit":"fleet-server-status.service"}"#, logs)
        XCTAssertEqual(named.journalHeading, "Logs of fleet-server-status.service")
        XCTAssertEqual(named.journalCountLine, "The last 2 journal lines.")
        let one = parseRun(#"{"ok":true,"verb":"server logs","result":"done","commands":[],"output":"a\n","lines":1,"logsUnit":"fleet-server-status.service"}"#, logs)
        XCTAssertEqual(one.journalCountLine, "The last journal line.")
        // With no unit named,  the heading says how many lines were read,  as it did before.
        let unnamed = parseRun(#"{"ok":true,"result":"done","commands":[],"output":"a\nb\n","lines":2}"#, logs)
        XCTAssertEqual(unnamed.journalHeading, "The Last 2 Journal Lines")
        XCTAssertNil(unnamed.journalCountLine)
        let bare = parseRun(#"{"ok":true,"result":"done","commands":[],"output":"a\n"}"#, logs)
        XCTAssertEqual(bare.journalHeading, "The Last Journal Lines")
        XCTAssertNil(bare.journalCountLine)
    }

    /// The "Now:" line goes under the result of the run of that unit,  and nowhere else (Kody,
    /// PR 1533).
    func test_the_now_line_is_shown_under_the_run_of_its_unit() throws {
        let unit = "fleet-server-status.timer"
        let ran = OpsAction.serverUnit(verb: .restart, unit: unit)
        let now = try date("2026-11-08T16:05:00Z")
        let report = try date("2026-11-08T15:50:00Z")
        let state = try XCTUnwrap(parsePlan(timerAnswer, .unitStatus(unit: unit)).unitState)
        let read = ServerUnitStateAfterRun(unit: unit, reading: .state(state))

        XCTAssertTrue(read.belongs(to: ran))
        XCTAssertEqual(ServerUnitStateAfterRun.sheetLine(read, for: ran, reportGeneratedAt: report, now: now),
                       "Now:  active (waiting), checked 10:02 AM, newer than the server's report")
        // With no report time reachable,  the read is still shown.
        XCTAssertEqual(ServerUnitStateAfterRun.sheetLine(read, for: ran, reportGeneratedAt: nil, now: now),
                       "Now:  active (waiting), checked 10:02 AM, newer than the server's report")
        // While the read is in flight,  the sheet says so.
        XCTAssertEqual(ServerUnitStateAfterRun.sheetLine(ServerUnitStateAfterRun(unit: unit, reading: nil),
                                                         for: ran, reportGeneratedAt: report, now: now),
                       "Now:  reading its state on the server\u{2026}")
        // A read that came to nothing says why.
        XCTAssertEqual(ServerUnitStateAfterRun.sheetLine(ServerUnitStateAfterRun(unit: unit, reading: .unavailable("Why.")),
                                                         for: ran, reportGeneratedAt: report, now: now),
                       "Now:  not known.\u{00A0} Why.")
        // No read,  a read of another unit,  or another action shows nothing.
        XCTAssertNil(ServerUnitStateAfterRun.sheetLine(nil, for: ran, reportGeneratedAt: report, now: now))
        XCTAssertNil(ServerUnitStateAfterRun.sheetLine(read, for: .serverUnit(verb: .stop, unit: "fleet-cli-git-update.timer"),
                                                       reportGeneratedAt: report, now: now))
        XCTAssertNil(ServerUnitStateAfterRun.sheetLine(read, for: .logs(unit: unit), reportGeneratedAt: report, now: now))
        XCTAssertNil(ServerUnitStateAfterRun.sheetLine(read, for: .unitStatus(unit: unit), reportGeneratedAt: report, now: now))
        XCTAssertFalse(read.belongs(to: .macAgent(verb: .restart, label: unit)))
    }
}
