import Foundation

// What Muster does about a service on the Mac that is not running.
//
// The rule is small and written down in one place:  each service has one of three
// policies,  and the table below says which and why.  Muster only acts while it is
// running,  only ever starts a service (never stops or restarts one),  and never undoes a
// stop the owner asked for.  `ServiceReconcileEngine` decides one service at a time and
// does no work of its own,  so a test can read every decision.

// MARK: - The three policies

/// What Muster does when a service is not running.
public enum ServiceReconcilePolicy: String, Equatable, CaseIterable {
    /// Start it again whenever it is found stopped or failed,  with a backoff and a cap.
    case keepRunning
    /// Start it once, at the first look after Muster starts,  and never again until Muster is
    /// started again.  That is the moment Muster comes up,  which is at login only when the
    /// owner turned on its login item,  so the policy is not tied to a login.
    case startAtLoginOnly
    /// Never start it.
    case leaveAlone

    /// The policy as a settings row or a table names it.
    public var title: String {
        switch self {
        case .keepRunning: return "Keep Running"
        case .startAtLoginOnly: return "Start Once Per Launch"
        case .leaveAlone: return "Leave Alone"
        }
    }

    /// What Muster does,  as a sentence for a service row.
    public var sentence: String {
        switch self {
        case .keepRunning: return "Muster starts it again when it stops."
        case .startAtLoginOnly: return "Muster starts it once when Muster starts, and never again after that."
        case .leaveAlone: return "Muster never starts it."
        }
    }
}

// MARK: - The table

/// Where a row of the table comes from.
public enum ServicePolicySource: Equatable {
    /// A row of the Services list (`FleetServiceRegistry`).
    case servicesList
    /// A LaunchAgent the allowlist (`scripts/fleet-ops-control.json`) names and the Services list does not.
    case allowlistedAgent
}

/// One row of the table:  a service,  its policy and the reason in one line.
public struct ServicePolicyRow: Equatable {
    public let id: String
    public let title: String
    public let source: ServicePolicySource
    public let policy: ServiceReconcilePolicy
    public let reason: String
}

/// Every service Muster manages,  with its default policy and a one-line reason.
///
/// Most services keep running.  Anything whose restart could do harm,  or that is stopped on
/// purpose today,  is left alone.  A service that stops itself on purpose to avoid a restart
/// storm is started once per launch,  so Muster cannot fight whatever holds its lock or port.
public enum ServicePolicyTable {
    private static func row(_ id: String, _ policy: ServiceReconcilePolicy,
                            _ reason: String) -> (String, ServiceReconcilePolicy, String) {
        (id, policy, reason)
    }

    /// The Services list's rows,  by id.  Every service in `FleetServiceRegistry.all` has one,
    /// and a test fails when a new service is added without one.
    private static let servicesList: [(String, ServiceReconcilePolicy, String)] = [
        row("botfleet-mcp", .leaveAlone,
            "It only proxies BotFleet Server, and its pm2 record can be stale, so Muster does not guess and never starts it."),
        row("app.botfleet.server", .leaveAlone,
            "Starting it clears BotFleet's deliberate-stop marker, so only the owner starts it."),
        row("app.botfleet.companion", .leaveAlone,
            "It only serves BotFleet Server, which Muster leaves alone, so Muster leaves it alone too."),
        row("com.jay.operator", .leaveAlone,
            "launchd keeps the Mac's eavesdropper alive itself, so a stop is deliberate and Muster never starts it on its own."),
        row("shellular", .keepRunning,
            "A long-running job in the pm2 ecosystem file, and starting it again is safe."),
        row("code-main-keeper", .keepRunning,
            "A long-running job in the pm2 ecosystem file that keeps the Mac's copy of main current, and starting it again is safe."),
        row("vision-worker", .keepRunning,
            "A long-running worker in the pm2 ecosystem file, and starting it again is safe."),
        row("xcode-health", .keepRunning,
            "A small health server in the pm2 ecosystem file, and starting it again is safe."),
        row("agy-acp", .keepRunning,
            "A long-running bridge in the pm2 ecosystem file, and starting it again is safe."),
        row("grok-leader", .startAtLoginOnly,
            "It exits on purpose when another leader holds its lock, so Muster starts it once when Muster starts and never fights the lock."),
        row("grok-acp", .keepRunning,
            "A long-running bridge in the pm2 ecosystem file, and starting it again is safe."),
        row("clutch-web", .startAtLoginOnly,
            "It exits on purpose when something else holds its port, so Muster starts it once when Muster starts and never fights the port."),
        row("seat-mcp", .keepRunning,
            "A long-running job in the pm2 ecosystem file, and starting it again is safe."),
    ]

    /// The reason every LaunchAgent that is only on the allowlist is left alone.
    private static let scheduledReason =
        "launchd runs it on its own schedule and it exits between runs, so no process is its normal state."
    /// The reason for an allowlisted agent that launchd keeps alive itself.
    private static let keepAliveReason =
        "launchd keeps it alive itself, so a stop from Muster is deliberate and stays."
    /// The reason for an allowlisted agent nobody wrote a schedule down for.
    private static let handReason =
        "It is not in the Services list and its schedule is not written down, so Muster does not read its state and never starts it."

    /// The allowlisted LaunchAgents the Services list does not show,  by label.  Muster
    /// leaves every one of them alone.
    private static let agents: [(String, String)] = [
        ("com.jay.botfleet-server", handReason),
        ("com.jay.fleet-gdrive-backup", scheduledReason),
        ("com.jay.fleet-mac-services", scheduledReason),
        ("com.jay.fleet-rag-mirror", scheduledReason),
        ("com.jay.fleet-tools-doctor", scheduledReason),
        ("com.jay.fleet-work-backup", scheduledReason),
        ("com.jay.fleet-worker-health", scheduledReason),
        ("com.jay.github-outbox-bridge", scheduledReason),
        ("com.jay.grok-idle-unload", scheduledReason),
        ("com.jay.ios-testflight-freshness", scheduledReason),
        ("com.jay.mac-cleanup", scheduledReason),
        ("com.jay.mac-facts-publish", scheduledReason),
        ("com.jay.mac-process-watch", scheduledReason),
        ("com.jay.mac-resource-watch", scheduledReason),
        ("com.jay.mac-seat-watch", handReason),
        ("com.jay.needs-owner-notify", scheduledReason),
        ("com.jay.operator-notes-runner", keepAliveReason),
        ("com.jay.operator-wake-alerter", handReason),
        ("com.jay.sim-idle-reaper", scheduledReason),
        ("com.jay.simple-tools-runtime-sync", handReason),
        ("com.jays.weekly-fleet-mode-digest", scheduledReason),
    ]

    /// The labels of the allowlisted agents the table names,  for a test that checks them
    /// against the allowlist file.
    public static var agentLabels: [String] { agents.map { $0.0 } }

    /// Every row:  the Services list first,  in the order the menu shows it,  then the agents.
    ///
    /// Built once:  a Services panel render and every reconcile pass look rows up,  and
    /// none of them should rebuild the table to do it.
    public static let rows: [ServicePolicyRow] = {
        var out: [ServicePolicyRow] = []
        for descriptor in FleetServiceRegistry.all {
            let found = ServicePolicyTable.servicesList.first { $0.0 == descriptor.id }
            out.append(ServicePolicyRow(id: descriptor.id,
                                        title: descriptor.title,
                                        source: .servicesList,
                                        policy: found?.1 ?? .leaveAlone,
                                        reason: found?.2 ?? ServicePolicyTable.unlistedReason))
        }
        for agent in ServicePolicyTable.agents {
            out.append(ServicePolicyRow(id: agent.0,
                                        title: OpsNames.agentTitle(agent.0),
                                        source: .allowlistedAgent,
                                        policy: .leaveAlone,
                                        reason: agent.1))
        }
        return out
    }()

    /// The rows by id,  for a lookup.
    private static let rowsByID: [String: ServicePolicyRow] =
        Dictionary(rows.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })

    /// The ids the Services list names that have an explicit row in the table.
    public static var listedServiceIDs: [String] { servicesList.map { $0.0 } }

    /// What a service nobody wrote a row for gets.
    public static let unlistedReason = "Nobody has said Muster should keep it running."

    /// The table's row for one service,  or nil when nobody wrote one.
    public static func row(for id: String) -> ServicePolicyRow? {
        rowsByID[id]
    }

    /// The policy for one service.  A service with no row is left alone.
    public static func policy(for id: String) -> ServiceReconcilePolicy {
        row(for: id)?.policy ?? .leaveAlone
    }

    /// The one-line reason for one service's policy.
    public static func reason(for id: String) -> String {
        row(for: id)?.reason ?? unlistedReason
    }
}

// MARK: - What was tried

/// How the last try to start one service ended.
public enum ReconcileOutcome: Equatable {
    /// The start ran and said it worked.
    case started
    /// The start did not run or did not work,  with the reason in the owner's words.
    case notStarted(String)
}

/// What Muster remembers about one service it started,  for as long as Muster runs.
public struct ReconcileRecord: Equatable {
    /// How many tries Muster made since the service last stayed up.
    public var attempts = 0
    /// When the last try began.
    public var lastAttemptAt: Date?
    /// How the last try ended.
    public var lastOutcome: ReconcileOutcome?
    /// True once Muster stopped trying.
    public var gaveUp = false
    /// True once Muster used its one start at login for this service.
    public var loginStartUsed = false
    /// When Muster last started this service and the start worked.  It outlives the count,
    /// so a row can go on saying "Restarted by Muster at 2:46 PM" once the service is stable.
    public var lastSuccessAt: Date?

    public init() {}

    /// True when Muster did nothing about this service so far.
    public var isBlank: Bool { attempts == 0 && !gaveUp && !loginStartUsed && lastSuccessAt == nil }

    /// The record with the count started again,  keeping what is about the whole launch.
    public func startedAgain() -> ReconcileRecord {
        var fresh = ReconcileRecord()
        fresh.loginStartUsed = loginStartUsed
        fresh.lastSuccessAt = lastSuccessAt
        return fresh
    }
}

/// The numbers behind the backoff and the cap.
public struct ServiceReconcileConfig: Equatable {
    /// How many times Muster starts a service that keeps stopping before it gives up.
    public var maxTries = 3
    /// The wait after the first try,  in seconds.  Each later wait doubles.
    public var firstDelay: TimeInterval = 60
    /// How long a service must have been up after a try before the count starts again.
    public var stableAfter: TimeInterval = 600

    public init() {}

    public init(maxTries: Int, firstDelay: TimeInterval, stableAfter: TimeInterval) {
        self.maxTries = max(1, maxTries)
        self.firstDelay = max(0, firstDelay)
        self.stableAfter = max(0, stableAfter)
    }

    /// How long Muster waits after its `attempts`th try before it looks at the service again.
    public func delay(afterAttempts attempts: Int) -> TimeInterval {
        guard attempts > 0 else { return 0 }
        return firstDelay * pow(2, Double(min(attempts, 20) - 1))
    }
}

// MARK: - A decision

/// Why Muster did nothing about one service.
public enum ReconcileSkip: Equatable {
    /// The owner turned the whole thing off in Settings.
    case turnedOff
    /// The policy says leave it alone.
    case leaveAlone
    /// The owner stopped it on purpose,  and has not started it again.
    case stoppedByOwner
    /// It is running,  or it is in a state Muster does not act on.
    case nothingToDo
    /// The one start at login was used.
    case loginStartUsed
    /// The one start at login is for the first look after Muster comes up,  and this is not it.
    case notAtLogin
    /// A try is too recent,  and the next one is not due until then.
    case waiting(until: Date)
    /// Muster tried the most it will,  and the service is still not running.
    case gaveUp(tries: Int)
}

/// What Muster does about one service on one look.
public enum ReconcileDecision: Equatable {
    /// Start it:  this is the `try`th try since it last stayed up.
    case start(attempt: Int)
    case skip(ReconcileSkip)
}

// MARK: - The engine

/// Decides what Muster does about one service,  from facts alone.
///
/// The order of the checks is the safety argument:  the setting and the owner's stop come
/// before anything else,  so nothing below them can start a service the owner stopped.
public enum ServiceReconcileEngine {
    /// The states Muster will start a service from.  A service that is running is fine,  one
    /// that is not known is never acted on,  one that is not installed or that pm2 has never
    /// heard of is not something a start can fix.
    public static func isStartable(_ state: ManagedServiceState) -> Bool {
        switch state {
        case .stopped, .failed: return true
        case .running, .notInstalled, .notInPM2, .unknown: return false
        }
    }

    /// Decides one service.
    ///
    /// - Parameters:
    ///   - firstLook: True only for the first look after Muster comes up.
    public static func decide(state: ManagedServiceState,
                              policy: ServiceReconcilePolicy,
                              stoppedByOwner: Bool,
                              enabled: Bool,
                              record: ReconcileRecord,
                              now: Date,
                              firstLook: Bool,
                              config: ServiceReconcileConfig = ServiceReconcileConfig()) -> ReconcileDecision {
        if !enabled { return .skip(.turnedOff) }
        if stoppedByOwner { return .skip(.stoppedByOwner) }
        if policy == .leaveAlone { return .skip(.leaveAlone) }
        guard isStartable(state) else { return .skip(.nothingToDo) }
        if policy == .startAtLoginOnly {
            if record.loginStartUsed { return .skip(.loginStartUsed) }
            if !firstLook { return .skip(.notAtLogin) }
            return .start(attempt: 1)
        }
        if record.gaveUp { return .skip(.gaveUp(tries: record.attempts)) }
        if let last = record.lastAttemptAt, record.attempts > 0 {
            let due = last.addingTimeInterval(config.delay(afterAttempts: record.attempts))
            if now < due { return .skip(.waiting(until: due)) }
            if record.attempts >= config.maxTries { return .skip(.gaveUp(tries: record.attempts)) }
        }
        return .start(attempt: record.attempts + 1)
    }

    /// The record after a look at a service that Muster did not start on this look.
    ///
    /// A service that is up again and has stayed up since the last try starts its count
    /// again,  and so does one that was given up on and is running now,  because somebody
    /// started it.  A service that is still not running after the last allowed try is given
    /// up on.
    public static func settle(_ record: ReconcileRecord,
                              state: ManagedServiceState,
                              decision: ReconcileDecision,
                              now: Date,
                              config: ServiceReconcileConfig = ServiceReconcileConfig()) -> ReconcileRecord {
        var next = record
        // The owner's stop starts the count again:  whatever Muster tried before is over.
        if case .skip(.stoppedByOwner) = decision { return record.startedAgain() }
        if case .skip(.gaveUp(let tries)) = decision {
            next.gaveUp = true
            next.attempts = max(next.attempts, tries)
        }
        if state == .running {
            let stable = record.lastAttemptAt.map { now.timeIntervalSince($0) >= config.stableAfter } ?? true
            // The login start stays used:  Muster starts a service at login once per launch.
            if record.gaveUp || stable { return record.startedAgain() }
        }
        return next
    }

    /// The record after a try.
    public static func recordTry(_ record: ReconcileRecord,
                                 policy: ServiceReconcilePolicy,
                                 outcome: ReconcileOutcome,
                                 at: Date) -> ReconcileRecord {
        var next = record
        next.attempts += 1
        next.lastAttemptAt = at
        next.lastOutcome = outcome
        if case .started = outcome { next.lastSuccessAt = at }
        if policy == .startAtLoginOnly { next.loginStartUsed = true }
        return next
    }
}

// MARK: - What the owner reads

/// What Muster says about one service it acted on,  for the menu and the Services panel.
public struct ReconcileNote: Equatable {
    public enum Kind: Equatable {
        /// Muster started it,  and the last look found it running.
        case restarted(at: Date)
        /// Muster started it,  and it is not running again.
        case stoppedAgain(tries: Int, at: Date, next: Date?)
        /// The start did not work,  with the reason.
        case couldNotStart(reason: String, at: Date, next: Date?)
        /// Muster stopped trying.
        case gaveUp(tries: Int)
        /// Muster started it,  and the last look could not say whether it is running.
        case startedStateNotKnown(at: Date)
    }

    public let kind: Kind

    public init(_ kind: Kind) { self.kind = kind }

    /// The line for a row.
    public var line: String {
        switch kind {
        case .restarted(let at):
            return "Restarted by Muster at \(FleetTime.clock(at))"
        case .stoppedAgain(let tries, let at, let next):
            return "Restarted by Muster at \(FleetTime.clock(at)), and it stopped again (try \(tries)).\(Self.nextText(next))"
        case .couldNotStart(let reason, let at, let next):
            let ended = Self.endsASentence(reason) ? reason : "\(reason)."
            return "Muster could not restart it at \(FleetTime.clock(at)):  \(next == nil ? reason : ended)\(Self.nextText(next))"
        case .gaveUp(let tries):
            return "Muster stopped retrying after \(tries) \(tries == 1 ? "try" : "tries")"
        case .startedStateNotKnown(let at):
            return "Muster started it at \(FleetTime.clock(at)), and its state is not known."
        }
    }

    /// The same note in a few words,  for a menu row.  The Services panel has the whole line.
    public var shortLine: String {
        switch kind {
        case .restarted(let at):
            return "Restarted by Muster at \(FleetTime.clock(at))"
        case .stoppedAgain(_, let at, _):
            return "Restarted by Muster at \(FleetTime.clock(at)), and it stopped again"
        case .couldNotStart(_, let at, _):
            return "Muster could not restart it at \(FleetTime.clock(at))"
        case .gaveUp(let tries):
            return "Muster stopped retrying after \(tries) \(tries == 1 ? "try" : "tries")"
        case .startedStateNotKnown(let at):
            return "Muster started it at \(FleetTime.clock(at)), state not known"
        }
    }

    private static func endsASentence(_ text: String) -> Bool {
        guard let last = text.last else { return true }
        return last == "." || last == "!" || last == "?"
    }

    private static func nextText(_ next: Date?) -> String {
        guard let next else { return "" }
        return "\u{00A0} Next try at \(FleetTime.clock(next))."
    }

    /// True when the note says something went wrong.
    public var isProblem: Bool {
        switch kind {
        case .restarted: return false
        case .stoppedAgain, .couldNotStart, .gaveUp, .startedStateNotKnown: return true
        }
    }

    /// The note for one service,  from what Muster remembers and what the last look found.
    public static func make(record: ReconcileRecord,
                            state: ManagedServiceState,
                            policy: ServiceReconcilePolicy = .keepRunning,
                            config: ServiceReconcileConfig = ServiceReconcileConfig()) -> ReconcileNote? {
        guard let at = record.lastAttemptAt, record.attempts > 0 else {
            // The count is over,  and the last start that worked is still worth saying.
            if let worked = record.lastSuccessAt, state == .running {
                return ReconcileNote(.restarted(at: worked))
            }
            return nil
        }
        if record.gaveUp { return ReconcileNote(.gaveUp(tries: record.attempts)) }
        // A service started at login only has no next try.
        let next: Date? = (record.attempts >= config.maxTries || policy != .keepRunning)
            ? nil
            : at.addingTimeInterval(config.delay(afterAttempts: record.attempts))
        switch record.lastOutcome {
        case .some(.notStarted(let reason)):
            return ReconcileNote(.couldNotStart(reason: reason, at: at, next: next))
        case .some(.started), .none:
            if state == .running { return ReconcileNote(.restarted(at: at)) }
            if ServiceReconcileEngine.isStartable(state) {
                return ReconcileNote(.stoppedAgain(tries: record.attempts, at: at, next: next))
            }
            // Not installed,  not in pm2 or not known:  the start worked,  but nobody can see
            // the service now,  so the note must not read as a success.
            return ReconcileNote(.startedStateNotKnown(at: at))
        }
    }
}
