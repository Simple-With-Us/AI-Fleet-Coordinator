import Foundation

// One server unit's state,  read live through `server unit-status UNIT --plan --json`
// (`OpsAction.unitStatus`).
//
// The read changes nothing,  so it needs no sheet and no Touch ID,  and the helper takes it as a
// plan only.  The unit must be one the allowlist lists,  as a server unit or as a `watch` unit:
// `OpsUnitStatusReader` checks that against this Mac's copy before anything is sent,  and the
// server checks its own copy again.
//
// The helper's answer carries systemd's words (`activeState`,  `subState`,  `unitResult`) and
// three times.  Each word must be in systemd's own shape,  `[a-z][a-z0-9-]{0,31}`,  and anything
// else is not reported:  a state Muster cannot vouch for is never shown as one.
//
// Right after a `--yes` run of a server unit,  the app's controller reads the unit's state again
// (`OpsController.unitStateAfterRun`),  so the sheet can say "Now:  active (running),  checked
// 10:02 AM" under the result,  newer than the server's report it was planned from.

// MARK: - The state

/// One unit's systemd state,  as the server read it.
struct ServerUnitState: Equatable {
    let unit: String
    /// `user` or `system`,  when the answer said.
    let scope: String?
    /// systemd's ActiveState,  such as `active`,  `inactive` or `failed`.
    let activeState: String
    /// systemd's SubState,  such as `running`,  `waiting`,  `dead` or `exited`.
    let subState: String
    /// systemd's Result,  such as `success` or `exit-code`,  or nil when there is none yet.
    let unitResult: String?
    /// When a service last exited,  or nil when it has not.  A timer's is always nil.
    let lastExitAt: Date?
    /// True for a timer:  the answer carries `nextElapseAt`,  null included.
    let isTimer: Bool
    /// When a timer next elapses,  or nil for a service,  and for a timer on a monotonic
    /// schedule that has no wall-clock time to give.
    let nextElapseAt: Date?
    /// When the server read the unit.
    let checkedAt: Date

    init(unit: String, scope: String? = nil, activeState: String, subState: String,
         unitResult: String? = nil, lastExitAt: Date? = nil, isTimer: Bool = false,
         nextElapseAt: Date? = nil, checkedAt: Date) {
        self.unit = unit
        self.scope = scope
        self.activeState = activeState
        self.subState = subState
        self.unitResult = unitResult
        self.lastExitAt = lastExitAt
        self.isTimer = isTimer
        self.nextElapseAt = nextElapseAt
        self.checkedAt = checkedAt
    }

    /// The state in a helper's answer about `unit`,  or nil when the answer is about another
    /// unit,  lacks a state word or a check time,  or carries a word out of shape.
    ///
    /// The optional fields are lenient:  a result word out of shape,  or a time that will not
    /// parse,  is left out and the rest is kept.
    init?(answer: OpsResult, unit: String) {
        if let named = answer.unit, named != unit { return nil }
        guard
            let active = answer.activeState, Self.isStateWord(active),
            let sub = answer.subState, Self.isStateWord(sub),
            let checked = answer.checkedAt.flatMap(FleetTime.parse)
        else { return nil }
        let scope = answer.scope.flatMap { OpsAllowlist.serverScopes.contains($0) ? $0 : nil }
        self.init(
            unit: unit,
            scope: scope,
            activeState: active,
            subState: sub,
            unitResult: answer.unitResult.flatMap { Self.isStateWord($0) ? $0 : nil },
            lastExitAt: answer.lastExitAt.flatMap(FleetTime.parse),
            isTimer: answer.hasNextElapseAt,
            nextElapseAt: answer.nextElapseAt.flatMap(FleetTime.parse),
            checkedAt: checked
        )
    }

    /// systemd's state word shape:  a lower-case letter,  then up to 31 lower-case letters,
    /// digits and `-`.
    static func isStateWord(_ value: String) -> Bool {
        let bytes = Array(value.utf8)
        guard (1...32).contains(bytes.count), (97...122).contains(bytes[0]) else { return false }
        return bytes.dropFirst().allSatisfy { (97...122).contains($0) || (48...57).contains($0) || $0 == 45 }
    }

    /// `Now:  active (running), checked 10:02 AM`:  the state and when the server read it,  on
    /// Jay's clock,  with the date in front when it was not today.
    func nowText(now: Date = Date()) -> String {
        "Now:  \(activeState) (\(subState)), checked \(FleetTime.when(checkedAt, now: now))"
    }

    /// True when this read is newer than the report the screen was drawn from,  or there is no
    /// report time to compare.
    func isNewer(thanReport reportAt: Date?) -> Bool {
        guard let reportAt else { return true }
        return checkedAt > reportAt
    }

    /// The "Now:" line,  marked as newer than the server's report when it is.
    func nowLine(reportGeneratedAt: Date?, now: Date = Date()) -> String {
        let text = nowText(now: now)
        return isNewer(thanReport: reportGeneratedAt) ? "\(text), newer than the server's report" : text
    }

    /// The times,  in a sentence each:  when a timer runs next,  when a service last exited,
    /// and how it last ended.  Nil when the answer gave none of them.
    func timesText(now: Date = Date()) -> String? {
        var sentences: [String] = []
        if isTimer {
            if let next = nextElapseAt {
                sentences.append("Next run \(FleetTime.when(next, now: now)).")
            } else {
                sentences.append("No next run time:  it runs on a schedule counted from boot or its last run.")
            }
        }
        if let exited = lastExitAt {
            sentences.append("Last exited \(FleetTime.when(exited, now: now)).")
        }
        if let result = unitResult {
            sentences.append("Last result:  \(result).")
        }
        return sentences.isEmpty ? nil : sentences.joined(separator: ServerStatusCopy.sentenceGap)
    }

    /// The plain cause of a refused read,  from the helper's fixed code,  or from its words when
    /// it gave no code.
    static func cause(errorCode: String?, detail: String?) -> String {
        switch errorCode {
        case "read_only_required":
            return "The server reads a unit's state only as a plan, so nothing was read."
        case "unit_status_unavailable":
            return "The server could not ask systemd for this unit's state, so it is not known."
        case "unit_unavailable":
            return "systemd on the server does not have this unit loaded, so it has no state to report."
        case "unit_status_invalid":
            return "systemd's answer was not in the shape the server accepts, so no state is reported."
        default:
            break
        }
        switch detail?.trimmingCharacters(in: .whitespacesAndNewlines) {
        case "the unit is not in the allowlist":
            return "This unit is not on the server's list, so the server does not read its state."
        case "the unit is not a valid unit name":
            return "This is not a unit name the server reads."
        default:
            return "The server did not report this unit's state."
        }
    }
}

// MARK: - The reading

/// What one read of a unit's state came to.
enum ServerUnitStatusReading: Equatable {
    case state(ServerUnitState)
    /// Nothing is known,  and why,  in a plain sentence.
    case unavailable(String)

    /// The state,  when there is one.
    var state: ServerUnitState? {
        if case .state(let state) = self { return state }
        return nil
    }

    /// What the outcome of a `.unitStatus` plan came to.
    static func from(_ outcome: OpsOutcome) -> ServerUnitStatusReading {
        if outcome.isSuccess(for: .plan) {
            guard let state = outcome.unitState else {
                return .unavailable("The server answered without a state Muster could read, so none is shown.")
            }
            return .state(state)
        }
        switch outcome.kind {
        case .refused, .unavailable:
            return .unavailable(ServerUnitState.cause(errorCode: outcome.errorCode, detail: outcome.detail))
        case .planned, .submitted, .done, .failed, .unknown:
            return .unavailable(outcome.detail.isEmpty
                ? "The server did not report this unit's state." : outcome.detail)
        }
    }
}

/// The unit state read right after a run of that unit ended.
struct ServerUnitStateAfterRun: Equatable {
    let unit: String
    /// Nil while the read is in flight.
    let reading: ServerUnitStatusReading?

    /// True while the read is in flight.
    var isReading: Bool { reading == nil }

    /// The line under the run's result:  "Now:  active (running), checked 10:02 AM, newer than
    /// the server's report",  a plain sentence when nothing could be read,  or nil while reading.
    func line(reportGeneratedAt: Date?, now: Date = Date()) -> String? {
        switch reading {
        case .none: return nil
        case .some(.state(let state)): return state.nowLine(reportGeneratedAt: reportGeneratedAt, now: now)
        case .some(.unavailable(let cause)): return "Now:  not known.\(ServerStatusCopy.sentenceGap)\(cause)"
        }
    }
}

// MARK: - The reader

extension OpsAllowlist {
    /// True when `server unit-status` may be asked about `unit`:  the allowlist lists it as a
    /// server unit or as a `watch` unit.
    func allowsUnitStatus(_ unit: String) -> Bool {
        serverUnits.contains { $0.name == unit } || watchUnits.contains(unit)
    }
}

/// Reads one unit's state over the fixed,  read-only ssh route.
///
/// It bypasses the confirmation sheet because `unit-status --plan` cannot change anything.  A
/// unit this Mac's allowlist does not list,  or an allowlist that could not be read,  is refused
/// before anything is sent:  a gate that cannot be read does not open.
struct OpsUnitStatusReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read(unit: String, allowlist: OpsAllowlist?) async -> ServerUnitStatusReading {
        guard let allowlist else {
            return .unavailable("This Mac could not read the allowlist, so it asked the server nothing.")
        }
        guard allowlist.allowsUnitStatus(unit) else {
            return .unavailable("This unit is not on this Mac's allowlist, so it asked the server nothing.")
        }
        let action = OpsAction.unitStatus(unit: unit)
        do {
            let invocation = try makeBuilder().invocation(for: action, mode: .plan)
            let process = try await runner.run(invocation)
            let outcome = OpsResultParser.parse(process, mode: .plan, action: action, timeout: invocation.timeout)
            return ServerUnitStatusReading.from(outcome)
        } catch {
            return .unavailable(error.localizedDescription)
        }
    }
}
