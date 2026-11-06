import Foundation

/// How the Control app supervises one fleet service.
public enum FleetServiceKind: Equatable {
    /// A pm2 application.
    case pm2(appName: String)
    /// A launchd agent.
    case launchAgent(label: String)

    /// The machine this kind of service runs on.
    ///
    /// It is an exhaustive switch with no default,  so a new kind of service cannot be
    /// added without saying which machine it runs on.
    public var host: FleetHost {
        switch self {
        case .pm2: return .mac
        case .launchAgent: return .mac
        }
    }
}

/// How the wanted state of a fleet service is decided.
///
/// The wanted state is a fact about what a service is for, shown in the row's words
/// ("stopped, should be running").  It does not start anything.  What Muster does about a
/// service that is not running is the policy in `ServicePolicyTable`.
///
/// The BotFleet MCP Server proxies every tool call to the BotFleet Server,
/// so it is wanted exactly while that server is running.
///
/// Every other fleet service reads from its own state:  one that is running or that
/// failed should be running, and one that is stopped is not asking to be anything else.
public enum FleetServiceWantedPolicy: Equatable {
    case whileServerRuns
    case alwaysOn
}

/// One fleet service the Control app knows about.
public struct FleetServiceDescriptor: Equatable {
    public let id: String
    public let title: String
    public let kind: FleetServiceKind
    public let policy: FleetServiceWantedPolicy

    public init(id: String, title: String, kind: FleetServiceKind, policy: FleetServiceWantedPolicy) {
        self.id = id
        self.title = title
        self.kind = kind
        self.policy = policy
    }

    /// The machine the service runs on.  It is computed and never stored,  so a
    /// descriptor cannot claim another machine.
    public var host: FleetHost { kind.host }
}

/// The fleet services the Control app watches on this Mac.
///
/// The BotFleet MCP Server comes first: it is the service the fleet is
/// judged by, and it is the only one with an authoritative health probe.
/// The pm2 applications are the ones the pm2 ecosystem file defines, and the
/// launchd agents are the BotFleet's own.
public enum FleetServiceRegistry {
    /// The BotFleet MCP Server, which is supervised through pm2 but observed
    /// through its own health endpoint.
    public static let mcpServer = FleetServiceDescriptor(
        id: ManagedServices.mcpServerID,
        title: ManagedServices.mcpServerTitle,
        kind: .pm2(appName: PM2Client.mcpAppName),
        policy: .whileServerRuns)

    /// The BotFleet Server, which is supervised by launchd.
    public static let server = FleetServiceDescriptor(
        id: "app.botfleet.server",
        title: "BotFleet Server",
        kind: .launchAgent(label: "app.botfleet.server"),
        policy: .alwaysOn)

    /// The BotFleet Companion, which is supervised by launchd.
    public static let companion = FleetServiceDescriptor(
        id: "app.botfleet.companion",
        title: "BotFleet Companion",
        kind: .launchAgent(label: "app.botfleet.companion"),
        policy: .alwaysOn)

    /// Every fleet service, in the order the menu shows them.
    public static let all: [FleetServiceDescriptor] = [
        mcpServer,
        server,
        companion,
        pm2("shellular", "Shellular"),
        pm2("code-main-keeper", "Code Main Keeper"),
        pm2("vision-worker", "Vision Worker"),
        pm2("xcode-health", "Xcode Health"),
        pm2("agy-acp", "Antigravity ACP"),
        pm2("grok-leader", "Grok Leader"),
        pm2("grok-acp", "Grok ACP"),
        pm2("clutch-web", "Clutch Web"),
        pm2("seat-mcp", "Seat MCP"),
    ]

    /// The descriptor with this id, when the registry knows it.
    public static func descriptor(for id: String) -> FleetServiceDescriptor? {
        all.first { $0.id == id }
    }

    /// A pm2 application that is wanted on its own.
    private static func pm2(_ appName: String, _ title: String) -> FleetServiceDescriptor {
        FleetServiceDescriptor(id: appName, title: title,
                               kind: .pm2(appName: appName), policy: .alwaysOn)
    }
}

/// The services the owner stopped on purpose, and how that record changes.
///
/// A stop the owner asked for always wins:  Muster never starts a service that is on this
/// record, until the owner starts it again.  The record is kept in a small file
/// (`OwnerStopFile`) when the app is given one, so a relaunch of Muster does not forget it.
public struct OwnerStops: Equatable {
    /// The services the owner stopped on purpose, by service id.
    public private(set) var ids: Set<String>
    /// When the owner stopped each one,  when that is known.
    public private(set) var stoppedAt: [String: Date]

    public init(ids: Set<String> = [], stoppedAt: [String: Date] = [:]) {
        self.ids = ids
        self.stoppedAt = stoppedAt.filter { ids.contains($0.key) }
    }

    /// Records what the owner asked for, before the change is made.
    ///
    /// The intent is recorded first, so nothing that reads the record while the change is
    /// running can start the service again.  A restart is a start as far as the record is
    /// concerned: the owner asked for the service to be running.
    public mutating func record(_ action: ManagedServiceAction, id: String, at date: Date = Date()) {
        switch action {
        case .stop:
            ids.insert(id)
            stoppedAt[id] = date
        case .start, .restart:
            ids.remove(id)
            stoppedAt[id] = nil
        case .none: break
        }
    }

    /// Puts the record back the way it was, for a change that was refused.
    ///
    /// A refused change is not a stop the owner asked for, so the record
    /// goes back to what it was rather than to what the action would have
    /// made of it.
    public mutating func restore(_ previous: Set<String>) {
        ids = previous
        stoppedAt = stoppedAt.filter { previous.contains($0.key) }
    }

    /// Puts the whole record back the way it was,  times included.
    public mutating func restore(_ previous: OwnerStops) {
        self = previous
    }
}

/// One fleet service as the Control app sees it right now.
public struct FleetServiceEntry: Equatable {
    public let descriptor: FleetServiceDescriptor
    /// The desired state.  It comes from the service's own policy and is
    /// never stored, so it cannot drift away from the truth.
    public let wanted: Bool?
    /// The observed state, probed rather than assumed.
    public let state: ManagedServiceState
    /// True when the owner stopped this service on purpose.
    public let stoppedByOwner: Bool
    /// When the owner stopped it,  when that is known.
    public let stoppedAt: Date?
    /// False when the service's start and restart would change a startup
    /// policy the installed wrapper does not carry.
    public let canChangeStartupPolicy: Bool
    /// What Muster did about this service when it was not running,  if it did anything.
    public let reconcileNote: ReconcileNote?

    public init(descriptor: FleetServiceDescriptor,
                wanted: Bool?,
                state: ManagedServiceState,
                stoppedByOwner: Bool = false,
                stoppedAt: Date? = nil,
                canChangeStartupPolicy: Bool = true,
                reconcileNote: ReconcileNote? = nil) {
        self.descriptor = descriptor
        self.wanted = wanted
        self.state = state
        self.stoppedByOwner = stoppedByOwner
        self.stoppedAt = stoppedAt
        self.canChangeStartupPolicy = canChangeStartupPolicy
        self.reconcileNote = reconcileNote
    }

    /// What Muster does when this service is not running.
    public var reconcilePolicy: ServiceReconcilePolicy { ServicePolicyTable.policy(for: id) }

    /// The one line that says what Muster does about this service,  for the Services panel.
    ///
    /// `keepServicesRunning` is the Settings switch.  When it is off Muster starts nothing,
    /// so a row must not promise a start.  A Leave Alone row keeps its reason,  which is
    /// true either way.
    public func reconcilePolicyLine(keepServicesRunning: Bool = true) -> String {
        switch reconcilePolicy {
        case .keepRunning, .startAtLoginOnly:
            if !keepServicesRunning {
                return "\(reconcilePolicy.title):  Turned off in Settings, so Muster does not start it."
            }
            return "\(reconcilePolicy.title):  \(reconcilePolicy.sentence)"
        case .leaveAlone:
            return "\(reconcilePolicy.title):  \(ServicePolicyTable.reason(for: id))"
        }
    }

    /// The line on a row the owner stopped on purpose,  or nil when the owner did not.
    ///
    /// A stop that did not read as landed is still on the record,  so the line says what the
    /// owner asked for and not what happened.
    public var ownerStopLine: String? {
        guard stoppedByOwner else { return nil }
        return FleetActionWords.ownerStopLine(for: state, at: stoppedAt)
    }

    /// What the row says about the service's state,  beyond the state itself:  the owner's
    /// stop first,  then what Muster did.  Nil when there is nothing to add.
    public var stateNote: String? {
        if let line = ownerStopLine { return line }
        return reconcileNote?.line
    }

    /// The same note in a few words,  for a menu row.
    public var menuNote: String? {
        if stoppedByOwner {
            switch state {
            case .running, .unknown: return "you asked for it to stop"
            case .stopped, .failed, .notInstalled, .notInPM2:
                return stoppedAt.map { "stopped by you at \(FleetTime.clock($0))" } ?? "stopped by you"
            }
        }
        return reconcileNote?.shortLine
    }

    /// True when the row is something to look at:  a failure,  a state nobody can read,  or
    /// a service Muster gave up on.
    public var needsAttention: Bool {
        if state == .failed || state == .unknown { return true }
        return reconcileNote?.isProblem == true
    }

    public var id: String { descriptor.id }
    public var title: String { descriptor.title }
    public var kind: FleetServiceKind { descriptor.kind }
    public var policy: FleetServiceWantedPolicy { descriptor.policy }
    /// The machine the service runs on.
    public var host: FleetHost { descriptor.host }
    /// The service's title as a menu row shows it.
    public var menuRowTitle: String { FleetServices.menuRowTitle(title: title, host: host) }

    /// Why the service offers nothing right now,  or nil when it offers something or
    /// when its state already says why.
    public var whyNoActions: String? {
        guard availableActions.isEmpty else { return nil }
        if state == .notInstalled { return nil }
        if state == .notInPM2 { return "pm2 on the Mac has no record of it, so it is not started from here" }
        if !canChangeStartupPolicy { return "BotFleet on the Mac is too old to start it from here" }
        if policy == .whileServerRuns { return "it starts when BotFleet Server is running" }
        return nil
    }

    /// A short description of the entry for the menu.
    public var summary: String {
        ManagedServices.summary(wanted: wanted, state: state)
    }

    /// The actions the owner may pick for this service right now.
    public var availableActions: [ManagedServiceAction] {
        let actions = FleetServices.availableActions(for: state,
                                                    wanted: wanted,
                                                    policy: policy,
                                                    stoppedByOwner: stoppedByOwner)
        // Only a start and a restart change the startup policy, and the
        // installed wrapper has to carry the contract for those.  A stop
        // asks the server itself, so it works with any wrapper and is
        // never taken away.
        guard canChangeStartupPolicy else {
            return actions.filter { $0 != .start && $0 != .restart }
        }
        return actions
    }
}

/// One row of the Fleet Services submenu.
public struct FleetServiceMenuItem: Equatable {
    public let id: String
    public let title: String
    /// The machine the service runs on.
    public let host: FleetHost
    /// What the Control app saw, and what it wants.
    public let summary: String
    /// The actions the owner may pick for this service.
    public let actions: [ManagedServiceAction]

    public init(id: String, title: String, host: FleetHost, summary: String, actions: [ManagedServiceAction]) {
        self.id = id
        self.title = title
        self.host = host
        self.summary = summary
        self.actions = actions
    }
}

/// The fleet services the Control app watches, and what it wants of them.
public struct FleetServices: Equatable {
    /// The BotFleet Server's own state.  It decides whether the BotFleet MCP
    /// Server is wanted, because that server has nothing to offer while the
    /// BotFleet Server is down.
    public let serverState: ManagedServices.ServerState
    /// The observed state of every service, by service id.
    public let states: [String: ManagedServiceState]
    /// The services the owner stopped on purpose, by service id.
    ///
    /// A stop the owner asked for is one Muster never undoes.
    public let stoppedByOwner: Set<String>
    /// When the owner stopped each of them,  when that is known.
    public let stoppedAt: [String: Date]
    /// True when the installed wrapper carries the startup policy contract.
    ///
    /// The BotFleet Server's start and restart change that policy, so an
    /// older wrapper is offered nothing at all rather than an action that
    /// would always fail.
    public let startupPolicySupported: Bool
    /// What Muster did about each service it started,  by service id.
    public let reconcileNotes: [String: ReconcileNote]

    public init(serverState: ManagedServices.ServerState,
                states: [String: ManagedServiceState],
                stoppedByOwner: Set<String> = [],
                stoppedAt: [String: Date] = [:],
                startupPolicySupported: Bool = true,
                reconcileNotes: [String: ReconcileNote] = [:]) {
        self.serverState = serverState
        self.states = states
        self.stoppedByOwner = stoppedByOwner
        self.stoppedAt = stoppedAt
        self.startupPolicySupported = startupPolicySupported
        self.reconcileNotes = reconcileNotes
    }

    /// The wanted state of one service.
    ///
    /// The rule comes from the service's own policy, so the rule that
    /// follows the BotFleet Server is never used for a service that is
    /// wanted on its own.
    public func wanted(for descriptor: FleetServiceDescriptor) -> Bool? {
        switch descriptor.policy {
        case .whileServerRuns:
            // A service the owner stopped on purpose is not wanted, whatever
            // the service it follows is doing.
            if stoppedByOwner.contains(descriptor.id) { return false }
            switch serverState {
            case .running: return true
            case .stopped: return false
            case .unknown: return nil
            }
        case .alwaysOn:
            switch states[descriptor.id] {
            case .some(.running): return true
            case .some(.failed): return true
            case .some(.stopped): return false
            case .some(.notInstalled): return false
            case .some(.notInPM2): return false
            default: return nil
            }
        }
    }

    /// Every fleet service, in the order the menu shows them.
    public var entries: [FleetServiceEntry] {
        FleetServiceRegistry.all.map { descriptor in
            let isServer = descriptor.id == FleetServiceRegistry.server.id
            return FleetServiceEntry(descriptor: descriptor,
                                     wanted: wanted(for: descriptor),
                                     state: states[descriptor.id] ?? .unknown,
                                     stoppedByOwner: stoppedByOwner.contains(descriptor.id),
                                     stoppedAt: stoppedAt[descriptor.id],
                                     canChangeStartupPolicy: startupPolicySupported || !isServer,
                                     reconcileNote: reconcileNotes[descriptor.id])
        }
    }

    /// The rows the Fleet Services submenu shows, one per service.
    ///
    /// Each row carries the service's state and the actions the owner may
    /// pick for it, so the menu itself only draws what the policy decided.
    public var menuItems: [FleetServiceMenuItem] {
        entries.map { entry in
            FleetServiceMenuItem(id: entry.id, title: entry.title, host: entry.host,
                                 summary: entry.summary, actions: entry.availableActions)
        }
    }

    /// The title of the submenu that holds every service.
    public var menuTitle: String {
        "Services \(FleetHost.mac.onTitle) (\(entries.count))"
    }

    /// A title as a menu row shows it.
    ///
    /// A service on the Mac whose own name says Server would read as the server,  so its
    /// row says which machine it is on.  The bare name stays everywhere else,  because it
    /// is the owner's name for it.
    public static func menuRowTitle(title: String, host: FleetHost) -> String {
        guard host == .mac,
              title.split(separator: " ").contains("Server") else { return title }
        return "\(title) (\(host.onTitle))"
    }

    /// True when pm2 answered the last read.
    ///
    /// Any pm2 application with a state of its own means pm2 answered,  and so does one pm2
    /// says it has no record of.  Every one of them unknown means it did not,  which says
    /// nothing about whether those applications run.
    public var pm2Answered: Bool {
        FleetServiceRegistry.all.contains { descriptor in
            guard case .pm2 = descriptor.kind, descriptor.policy == .alwaysOn else { return false }
            return (states[descriptor.id] ?? .unknown) != .unknown
        }
    }

    /// What stopping one fleet service does, for the confirmation.
    ///
    /// The copy has to promise only what the menu really offers.  On a
    /// wrapper that cannot carry the startup policy the row has no
    /// Restart, so the copy names the real recovery instead of a Restart
    /// the owner cannot pick.
    public static func stopBody(for service: FleetServiceMenuItem,
                                startupPolicySupported: Bool) -> String {
        stopBody(forServiceID: service.id, startupPolicySupported: startupPolicySupported)
    }

    /// What stopping one fleet service does,  from its id alone.
    public static func stopBody(forServiceID id: String,
                                startupPolicySupported: Bool) -> String {
        if id == FleetServiceRegistry.server.id {
            let common = "BotFleet Server stops itself on the Mac, and its Local VMs go down with it.\u{00A0} It refuses while work is in flight.\u{00A0} It stays down after a login until it is started again"
            guard startupPolicySupported else {
                return common + ".\u{00A0} This version of BotFleet cannot restart it from here, so to bring it back, delete the file ~/.botfleet/stopped.marker on the Mac."
            }
            return common + "; Restart brings it back."
        }
        if let descriptor = FleetServiceRegistry.descriptor(for: id),
           case .launchAgent = descriptor.kind {
            return launchAgentStopBody
        }
        return pm2StopBody
    }

    /// What restarting BotFleet Server does to the work it found in flight.
    public static func restartBody(work: String) -> String {
        "A restart of BotFleet Server on the Mac does not wait the way a stop does, so it can end this work.\u{00A0} Checked just now:  \(work) in flight."
    }

    /// What stopping a LaunchAgent does.
    public static let launchAgentStopBody =
        "It stops right away on the Mac.\u{00A0} It stays stopped until you start it here, and macOS may start it again the next time you log in."

    /// What stopping a pm2 application does.
    public static let pm2StopBody =
        "It stops right away on the Mac.\u{00A0} pm2 and Muster both leave it stopped until you start it here."

    /// The actions the owner may pick for a service in this state.
    ///
    /// An action is offered only when the observed state supports it: a
    /// service that is running can be stopped and restarted, one that is
    /// stopped can be started, one that is not installed cannot be
    /// started at all, and one pm2 has never heard of is not started from here,
    /// because a start could make a second copy of a job that belongs to another
    /// machine.  A service the Control app cannot see is never
    /// stopped, but it can be restarted, because a restart only asks for a
    /// fresh copy of it.
    ///
    /// A service that follows another one,  the BotFleet MCP Server,  has nothing to
    /// serve while the one it follows is down,  so a stopped one is not offered a start then.
    /// A running one can always be stopped.  A service the owner stopped on purpose can
    /// always be started again.  `FleetServiceEntry.whyNoActions` says why a row is empty.
    public static func availableActions(for state: ManagedServiceState,
                                        wanted: Bool?,
                                        policy: FleetServiceWantedPolicy,
                                        stoppedByOwner: Bool = false) -> [ManagedServiceAction] {
        if policy == .whileServerRuns {
            // A service the owner stopped on purpose can always be started
            // again, because that is how the owner says they want it.
            if stoppedByOwner {
                return state == .running ? [.stop, .restart] : [.start, .restart]
            }
            // The server it follows is down:  it can be stopped if it is up,  and there is
            // nothing to start it for.
            if wanted == false { return state == .running ? [.stop] : [] }
        }
        switch state {
        case .running: return [.stop, .restart]
        case .stopped, .failed: return [.start, .restart]
        case .notInstalled, .notInPM2: return []
        case .unknown: return [.restart]
        }
    }
}

/// The marker on a service row:  a word,  a symbol and a tone,  so the state is never carried
/// by colour alone.
public struct ServiceStateMarker: Equatable {
    public enum Tone: Equatable {
        case good
        case bad
        case warning
        case neutral
    }

    /// The word,  in Title Case.
    public let word: String
    /// The SF Symbol that goes with it.
    public let symbol: String
    public let tone: Tone

    /// The marker for a state.  A service the owner stopped on purpose says so in its word.
    public static func make(state: ManagedServiceState, stoppedByOwner: Bool = false) -> ServiceStateMarker {
        switch state {
        case .running:
            // A stop the owner asked for that does not read as landed is not a running service
            // to be pleased about.
            if stoppedByOwner {
                return ServiceStateMarker(word: "Running", symbol: "exclamationmark.triangle.fill", tone: .warning)
            }
            return ServiceStateMarker(word: "Running", symbol: "checkmark.circle.fill", tone: .good)
        case .stopped:
            return ServiceStateMarker(word: stoppedByOwner ? "Stopped By You" : "Stopped",
                                      symbol: "pause.circle.fill", tone: .neutral)
        case .failed:
            return ServiceStateMarker(word: "Failed", symbol: "xmark.octagon.fill", tone: .bad)
        case .notInstalled:
            return ServiceStateMarker(word: "Not Installed", symbol: "minus.circle", tone: .neutral)
        case .notInPM2:
            return ServiceStateMarker(word: "Not In pm2", symbol: "minus.circle", tone: .neutral)
        case .unknown:
            return ServiceStateMarker(word: "Not Known", symbol: "questionmark.circle.fill", tone: .warning)
        }
    }
}

extension FleetServiceEntry {
    /// The marker for this row.
    public var marker: ServiceStateMarker {
        ServiceStateMarker.make(state: state, stoppedByOwner: stoppedByOwner)
    }
}
