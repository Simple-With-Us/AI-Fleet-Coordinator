import Combine
import Foundation
import ServiceManagement

/// How long the admin watches the BotFleet Server's port after a change.
///
/// The server moves on its own time,  so the wait is a budget rather than a promise:  a
/// server that has not got there when the budget runs out is left as it is,  and the
/// read that follows reports what it finds.
struct ServerPortWait: Equatable, Sendable {
    /// How many times the port is looked at.
    let attempts: Int
    /// How long the admin waits between two looks.
    let interval: TimeInterval

    init(attempts: Int, interval: TimeInterval) {
        self.attempts = max(1, attempts)
        self.interval = max(0, interval)
    }

    /// The port closes soon after a stop.
    static let stop = ServerPortWait(attempts: 8, interval: 0.5)
    /// The server takes longer to come up than to go,  so a start or a restart gets the
    /// twenty seconds any other service gets.
    static let start = ServerPortWait(attempts: PM2Client.settleAttempts,
                                      interval: FleetServiceControl.settleInterval)
}

/// How one action on the direct route ended.
struct FleetActionResult: Equatable {
    /// The service the action was on.
    let serviceID: String
    /// The service's title.
    let title: String
    /// What was asked.
    let action: ManagedServiceAction
    /// Whether the service ended where the action asked it to.
    let succeeded: Bool
    /// Why it did not,  in the owner's words.
    let reason: String?
    /// True when the Mac could not tell where the service ended,  which is not the same as
    /// the action not landing.
    let notKnown: Bool
    /// When it ended.
    let at: Date
}

/// What the server's own status said on one read.
private enum ServerStatusRead {
    /// It answered,  with the work in flight or nil when there is none.
    case answered(String?)
    /// It did not answer.
    case noAnswer
    /// The read was called off,  which says nothing about the server.
    case calledOff
}

/// What one read of the fleet found.
private struct FleetAdminSnapshot {
    /// What the BotFleet Server itself is doing.
    let serverState: ManagedServices.ServerState
    /// The state of every service,  by id.
    let states: [String: ManagedServiceState]
    /// Whether this Mac's fleet can manage the server's startup policy.
    let startupPolicySupported: Bool
    /// Whether the server starts at login today.
    let startsAtLogin: Bool
}

/// The fleet admin:  what the fleet's own services are doing on this Mac,  and what the
/// owner can do about them.
///
/// This is the control the standalone BotFleet Control menu bar app used to carry,  moved
/// into Muster:  the app is about the fleet now,  and not about BotFleet alone.
///
/// The wanted state of a service is derived and never stored here:  the BotFleet MCP
/// Server is wanted exactly while the BotFleet Server runs,  and every other service is
/// wanted on its own.  A stop the owner asked for is remembered until the owner starts
/// that service again,  so the reconciler does not take it back.
///
/// The fleet is read as the admin comes up and every two minutes after that,  so the menu
/// bar carries the services before the owner opens the window and does not go on showing
/// what was true at launch.  The read itself is off the main thread,  so the menu never
/// waits for pm2 and launchctl to answer,  and only the newest read may write the states,
/// so a slower older read does not leave the menu showing what was true before the action
/// the owner took.
@MainActor
final class FleetAdmin: ObservableObject {
    /// The services,  as the last read found them.
    @Published private(set) var services: FleetServices?
    /// The service an action is running against,  so the menu can say that it is busy.
    @Published private(set) var busyServiceID: String?
    /// The last refusal,  in the owner's words.
    @Published private(set) var problem: String?
    /// Whether a read is in flight.
    @Published private(set) var reading = false
    /// When the last read landed,  or nil before the first one.
    @Published private(set) var lastReadAt: Date?
    /// How the last action on the direct route ended,  until the next one starts.
    @Published private(set) var lastResult: FleetActionResult?
    /// Whether this Mac's fleet can manage the server's startup policy.
    @Published private(set) var startupPolicySupported = false
    /// Whether the server starts at login today.
    @Published private(set) var startsAtLogin = false
    /// What is known about BotFleet Server on the Mac.  It is three states and not a yes
    /// or a no,  because a server that did not answer is not a server that stopped.
    @Published private(set) var localServiceState: ManagedServices.ServerState = .unknown
    /// Whether Muster comes back at login.
    ///
    /// The menu bar item has to be there when the owner logs in,  so the app carries the
    /// login item the way the BotFleet Control app did.
    @Published private(set) var launchesAtLogin = false
    /// Why the login item did not change,  which the menu's own toggle has to be able to say.
    @Published private(set) var loginItemProblem: String?
    /// The work in flight that would block a stop,  as the server's last status
    /// said it,  or nil when nothing is in flight or the server did not answer.
    @Published private(set) var blockingWorkSummary: String?

    /// The fleet services the admin watches and changes.
    private let control: FleetServiceControl
    /// The launchd policy of the BotFleet Server's own agent.
    private let startup: StartupPolicy
    /// Whether the server is answering on its port.
    private let presence: ServerPresenceProbe
    /// The server's own control API.
    private let api: ControlAPI
    /// The MCP server's own health endpoint,  when it could be reached.
    private let mcpServer: McpServerControl?
    /// The state of every service,  by id.
    private var states: [String: ManagedServiceState] = [:]
    /// The services the owner stopped on purpose,  and how that record changes.
    private var ownerStops = OwnerStops()
    /// Which read may write the states.
    private let sequencer = FleetSnapshotSequencer()
    /// Whether the server's own API answered.
    private var apiAvailable = false
    /// How many reads are in flight,  so an older read ending does not say none is.
    private var readsInFlight = 0
    /// How long a stop waits for the server's port to close.
    private let stopWait: ServerPortWait
    /// How long a start or a restart waits for the server's port to answer.
    private let startWait: ServerPortWait
    /// The repeating read.
    private var refreshLoop: Task<Void, Never>?

    /// How often the fleet is read again.
    nonisolated static let refreshInterval: TimeInterval = 120
    /// How long a routine read waits for BotFleet Server's own status,  so a wedged server
    /// does not hold up every other row.
    static let statusTimeout: TimeInterval = 3

    init(control: FleetServiceControl? = nil,
         startup: StartupPolicy = StartupPolicy(),
         presence: ServerPresenceProbe = ServerPresenceProbe(),
         api: ControlAPI = ControlAPI(),
         stopWait: ServerPortWait = .stop,
         startWait: ServerPortWait = .start,
         refreshInterval: TimeInterval? = FleetAdmin.refreshInterval) {
        let mcp = try? McpServerControl()
        self.mcpServer = mcp
        self.startup = startup
        self.presence = presence
        self.api = api
        self.stopWait = stopWait
        self.startWait = startWait
        self.control = control ?? FleetServiceControl(
            pm2: PM2Client(),
            startup: startup,
            mcpServer: mcp,
            stopServer: { [api] in try await api.stopServer() },
            // The server is observed the way the admin reads it:  its API,
            // its installed wrapper and its port, never launchd's view of a
            // wrapper that exits once the server is healthy.
            observeServer: { [api, startup, presence] in
                let answered = (try? await api.status(timeout: 3)) != nil
                return ManagedServices.serverState(statusAvailable: answered,
                                                   installedServerState: startup.installedServerState,
                                                   serverPresence: presence.presence()).managedState
            }
        )
        self.launchesAtLogin = SMAppService.mainApp.status == .enabled
        // The fleet is read as the admin comes up,  so the menu bar carries the
        // services before the owner opens the window.
        Task { await self.refresh() }
        if let refreshInterval { startRefreshLoop(every: refreshInterval) }
    }

    deinit {
        refreshLoop?.cancel()
    }

    /// Reads the fleet again on a timer,  and leaves it alone while an action is running,
    /// because that action reads the fleet itself when it ends.
    private func startRefreshLoop(every interval: TimeInterval) {
        let nanoseconds = UInt64(max(interval, 0.01) * 1_000_000_000)
        refreshLoop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: nanoseconds)
                guard !Task.isCancelled, let self else { return }
                if self.busyServiceID == nil { await self.refresh() }
            }
        }
    }

    // MARK: Reading

    /// Reads every service,  and the server's own state.
    ///
    /// The read is off the main thread,  so the menu never waits for pm2 and launchctl.
    ///
    /// The generation is taken first and the server's answer travels with the read,  so
    /// a read that began before an action and finished after it cannot write what it saw.
    func refresh() async {
        readsInFlight += 1
        reading = true
        defer {
            readsInFlight -= 1
            reading = readsInFlight > 0
        }
        let generation = sequencer.begin()
        let statusRead = await readServerAPI()
        let apiAvailable: Bool
        switch statusRead {
        case .answered: apiAvailable = true
        case .noAnswer: apiAvailable = false
        case .calledOff: apiAvailable = self.apiAvailable
        }
        let control = self.control
        let startup = self.startup
        let presence = self.presence
        let snapshot = await Task.detached(priority: .userInitiated) {
            // launchctl and the socket probe block,  so they do not belong on
            // the main thread:  the menu has to stay alive while they answer.
            let serverState = ManagedServices.serverState(
                statusAvailable: apiAvailable,
                installedServerState: startup.installedServerState,
                serverPresence: presence.presence())
            return FleetAdminSnapshot(serverState: serverState,
                                      states: await control.states(serverState: serverState),
                                      startupPolicySupported: startup.supportsPolicy,
                                      startsAtLogin: startup.startsAtLogin)
        }.value
        guard sequencer.isCurrent(generation) else { return }
        self.apiAvailable = apiAvailable
        switch statusRead {
        case .answered(let work): blockingWorkSummary = work
        case .noAnswer: blockingWorkSummary = nil
        case .calledOff: break
        }
        self.states = snapshot.states
        lastReadAt = Date()
        publish(snapshot)
    }

    /// Whether the server's own API answers,  which is one of the three ways the server
    /// can be known to be up.
    private func readServerAPI() async -> ServerStatusRead {
        do {
            let status = try await api.status(timeout: Self.statusTimeout)
            return .answered(status.blockingWorkSummary)
        } catch is CancellationError {
            // A read that was called off says nothing about the server, so the
            // last answer stands.
            return .calledOff
        } catch {
            return .noAnswer
        }
    }

    /// Publishes the last read.
    private func publish(_ snapshot: FleetAdminSnapshot) {
        services = FleetServices(serverState: snapshot.serverState,
                                states: snapshot.states,
                                stoppedByOwner: ownerStops.ids,
                                startupPolicySupported: snapshot.startupPolicySupported)
        startupPolicySupported = snapshot.startupPolicySupported
        startsAtLogin = snapshot.startsAtLogin
        localServiceState = snapshot.serverState
    }

    /// True when pm2 answered the last read.  Nil before the first read.
    var pm2Answered: Bool? { services?.pm2Answered }

    // MARK: Changing

    /// Changes one fleet service,  and keeps what the admin can see afterwards.
    ///
    /// The intent is recorded before the change,  because the reconciler runs as soon as
    /// the change is done,  and it goes back exactly as it was when the manager refuses
    /// the change.  A change that did not throw and did not land is treated the same way:
    /// the owner is told,  and a stop that did not happen is not recorded as one.
    func perform(_ action: ManagedServiceAction, serviceID: String) async {
        guard busyServiceID == nil else { return }
        guard let service = services?.menuItems.first(where: { $0.id == serviceID }) else { return }
        busyServiceID = serviceID
        problem = nil
        lastResult = nil
        defer { busyServiceID = nil }
        let previous = ownerStops.ids
        ownerStops.record(action, id: serviceID)
        var reason: String?
        var notKnown = false
        let portUnread = "its port on the Mac could not be read"
        do {
            let observed = try await control.perform(action, serviceID: serviceID)
            if serviceID == FleetServiceRegistry.server.id {
                // The BotFleet Server's own agent is a wrapper that exits as soon
                // as the server is healthy,  so launchctl cannot say whether the
                // server is still serving:  it says stopped while the server is up.
                // The port can say it,  so a stop waits for the port to close,  a start
                // or a restart waits for it to answer,  and the read that follows reports
                // the server as it really is.
                // A probe that could not tell is not evidence either way.
                if action == .stop {
                    switch await Self.watchThePort(until: .absent, presence: presence, wait: stopWait) {
                    case .absent: break
                    case .present: reason = "its port on the Mac is still answering"
                    case .unknown:
                        reason = portUnread
                        notKnown = true
                    }
                } else if action == .start || action == .restart {
                    switch await Self.watchThePort(until: .present, presence: presence, wait: startWait) {
                    case .present: break
                    case .absent: reason = "nothing answers on its port on the Mac yet"
                    case .unknown:
                        reason = portUnread
                        notKnown = true
                    }
                }
                // The state the manager reported is the launchd view of a wrapper,
                // so it is not what the owner is shown.
            } else {
                states[serviceID] = observed
                if let wanted = Self.expectedState(after: action), observed != wanted {
                    reason = "it still reads \(observed.label)"
                }
            }
        } catch {
            reason = error.localizedDescription
        }
        if let reason {
            ownerStops.restore(previous)
            if notKnown {
                let subject = ServiceActionSubject(title: service.title, host: service.host, action: action)
                problem = FleetActionWords.line(for: .notKnown(subject, reason: reason))
            } else {
                problem = "\(FleetActionWords.didNot(action, title: service.title, host: service.host)):  \(reason)"
            }
        }
        lastResult = FleetActionResult(serviceID: serviceID, title: service.title, action: action,
                                       succeeded: reason == nil, reason: reason, notKnown: notKnown, at: Date())
        await refresh()
    }

    /// The state an action should leave a service in.
    private static func expectedState(after action: ManagedServiceAction) -> ManagedServiceState? {
        switch action {
        case .start, .restart: return .running
        case .stop: return .stopped
        case .none: return nil
        }
    }

    /// The owner-stop record as it was before a controller action, and which service
    /// that action belongs to.
    private var controllerActionPrevious: (serviceID: String, previous: Set<String>)?

    /// Records the intent behind an action the router runs through the ops controller.
    ///
    /// The run belongs to the controller rather than to `perform`, so the intent is
    /// recorded here, before anything can run, exactly as `perform` records it.  One
    /// controller action at a time, like the sheet itself.
    func beginControllerAction(_ action: ManagedServiceAction, serviceID: String) {
        guard controllerActionPrevious == nil else { return }
        // A new action on either route clears the last one's problem,  as `perform` does.
        problem = nil
        controllerActionPrevious = (serviceID, ownerStops.ids)
        ownerStops.record(action, id: serviceID)
    }

    /// Settles a controller action:  a run that did not succeed, including one the
    /// owner declined at the sheet, puts the record back as it was.
    func endControllerAction(serviceID: String, succeeded: Bool) {
        guard let pending = controllerActionPrevious, pending.serviceID == serviceID else { return }
        controllerActionPrevious = nil
        guard !succeeded else { return }
        ownerStops.restore(pending.previous)
        // A read may have landed while the sheet was up,  so what it published goes back too.
        if let services {
            self.services = FleetServices(serverState: services.serverState,
                                          states: services.states,
                                          stoppedByOwner: ownerStops.ids,
                                          startupPolicySupported: services.startupPolicySupported)
        }
    }

    /// Watches the BotFleet Server's port until it reads as wanted,  or the budget runs out,
    /// and says what the last look found.
    ///
    /// The server's own stop and start are asynchronous,  and launchd does not own the
    /// process,  so the port is the only honest witness.  A server that has not got there
    /// when the budget runs out is left as it is,  and the read that follows reports it.
    nonisolated static func watchThePort(until wanted: ServerPresence,
                                         presence: ServerPresenceProbe,
                                         wait: ServerPortWait) async -> ServerPresence {
        for _ in 0..<wait.attempts {
            if presence.presence() == wanted { return wanted }
            try? await Task.sleep(nanoseconds: UInt64(wait.interval * 1_000_000_000))
        }
        return presence.presence()
    }

    /// Records whether the server should start at login.
    func setStartsAtLogin(_ enabled: Bool) async {
        problem = nil
        let startup = self.startup
        do {
            // launchctl blocks,  so the policy does not change on the main thread.
            try await Task.detached(priority: .userInitiated) {
                try startup.setStartsAtLogin(enabled)
            }.value
        } catch {
            problem = "The startup policy did not change:  \(error.localizedDescription)"
        }
        await refresh()
    }

    /// What the owner is told before a stop,  and asked to confirm.
    ///
    /// The words are the service's own stop warning,  and for the BotFleet Server the
    /// work in flight that the server would refuse the stop for,  read now and not taken
    /// from the last read,  so the dialog and the refusal agree.  An answer that did not
    /// come says so:  it is not the same as nothing being in flight.
    func stopConfirmation(for serviceID: String) async -> String {
        let body = FleetServices.stopBody(forServiceID: serviceID,
                                          startupPolicySupported: startupPolicySupported)
        guard serviceID == FleetServiceRegistry.server.id else { return body }
        switch await readServerAPI() {
        case .answered(.some(let work)):
            return "\(body)\n\nChecked just now:  \(work) in flight."
        case .answered(.none):
            return "\(body)\n\nChecked just now:  nothing is in flight."
        case .noAnswer, .calledOff:
            return "\(body)\n\nCould not check what is in flight."
        }
    }

    /// What the owner is told before a restart,  or nil when it runs at once.
    ///
    /// A restart replaces the process and does not ask BotFleet Server first,  so it can end
    /// work the server would have refused a stop for.  Only an answer that names work asks:
    /// a server that does not answer is one a restart is there to repair.
    func restartWarning(for serviceID: String) async -> String? {
        guard serviceID == FleetServiceRegistry.server.id else { return nil }
        guard case .answered(.some(let work)) = await readServerAPI() else { return nil }
        return FleetServices.restartBody(work: work)
    }

    /// The entries,  as the menu and the panel show them.
    var entries: [FleetServiceEntry] {
        services?.entries ?? []
    }

    /// Records whether Muster comes back at login.
    ///
    /// The menu bar item has to be there when the owner logs in,  so the app carries the
    /// login item the way the BotFleet Control app did:  the owner may quit Muster,  and
    /// the login item brings it back.
    func setLaunchesAtLogin(_ enabled: Bool) {
        problem = nil
        loginItemProblem = nil
        do {
            if enabled {
                if SMAppService.mainApp.status != .enabled {
                    try SMAppService.mainApp.register()
                }
            } else {
                if SMAppService.mainApp.status == .enabled {
                    try SMAppService.mainApp.unregister()
                }
            }
        } catch {
            problem = "The login item did not change:  \(error.localizedDescription)"
            loginItemProblem = problem
        }
        launchesAtLogin = SMAppService.mainApp.status == .enabled
    }
}
