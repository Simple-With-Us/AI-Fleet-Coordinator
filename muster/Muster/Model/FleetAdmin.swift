import Combine
import Foundation
import ServiceManagement

/// How long the admin waits for the BotFleet Server's port to close after a stop.
///
/// The server's own stop is asynchronous,  so the wait is a budget rather than a
/// promise:  a server that is still there when the budget runs out is left as it is,
/// and the read that follows reports it as unknown rather than as stopped.
struct ServerStopWait: Equatable {
    /// How many times the port is looked at.
    let attempts: Int
    /// How long the admin waits between two looks.
    let interval: TimeInterval

    init(attempts: Int = 8, interval: TimeInterval = 0.5) {
        self.attempts = max(1, attempts)
        self.interval = max(0, interval)
    }
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
/// The fleet is read as the admin comes up,  so the menu bar carries the services before
/// the owner opens the window.  The read itself is off the main thread,  so the menu
/// never waits for pm2 and launchctl to answer,  and only the newest read may write the
/// states,  so a slower older read does not leave the menu showing what was true before
/// the action the owner took.
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
    /// Whether this Mac's fleet can manage the server's startup policy.
    @Published private(set) var startupPolicySupported = false
    /// Whether the server starts at login today.
    @Published private(set) var startsAtLogin = false
    /// Whether the BotFleet Server is running now.
    @Published private(set) var serverRunning = false
    /// Whether Muster comes back at login.
    ///
    /// The menu bar item has to be there when the owner logs in,  so the app carries the
    /// login item the way the BotFleet Control app did.
    @Published private(set) var launchesAtLogin = false

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
    /// How long a stop waits for the server's port to close.
    private let stopWait: ServerStopWait

    init(control: FleetServiceControl? = nil,
         startup: StartupPolicy = StartupPolicy(),
         presence: ServerPresenceProbe = ServerPresenceProbe(),
         api: ControlAPI = ControlAPI(),
         stopWait: ServerStopWait = ServerStopWait()) {
        let mcp = try? McpServerControl()
        self.mcpServer = mcp
        self.startup = startup
        self.presence = presence
        self.api = api
        self.stopWait = stopWait
        self.control = control ?? FleetServiceControl(
            pm2: PM2Client(),
            startup: startup,
            mcpServer: mcp,
            stopServer: { [api] in try await api.stopServer() }
        )
        self.launchesAtLogin = SMAppService.mainApp.status == .enabled
        // The fleet is read as the admin comes up,  so the menu bar carries the
        // services before the owner opens the window.
        Task { await self.refresh() }
    }

    // MARK: Reading

    /// Reads every service,  and the server's own state.
    ///
    /// The read is off the main thread,  so the menu never waits for pm2 and launchctl.
    func refresh() async {
        reading = true
        defer { reading = false }
        await readServerAPI()
        let generation = sequencer.begin()
        let control = self.control
        let startup = self.startup
        let presence = self.presence
        let apiAvailable = self.apiAvailable
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
        self.states = snapshot.states
        publish(snapshot)
    }

    /// Whether the server's own API answers,  which is one of the three ways the server
    /// can be known to be up.
    private func readServerAPI() async {
        do {
            _ = try await api.status()
            apiAvailable = true
        } catch {
            apiAvailable = false
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
        serverRunning = snapshot.serverState == .running
    }

    // MARK: Changing

    /// Changes one fleet service,  and keeps what the admin can see afterwards.
    ///
    /// The intent is recorded before the change,  because the reconciler runs as soon as
    /// the change is done,  and it goes back exactly as it was when the manager refuses
    /// the change.
    func perform(_ action: ManagedServiceAction, serviceID: String) async {
        guard busyServiceID == nil else { return }
        guard let service = services?.menuItems.first(where: { $0.id == serviceID }) else { return }
        busyServiceID = serviceID
        problem = nil
        defer { busyServiceID = nil }
        let previous = ownerStops.ids
        ownerStops.record(action, id: serviceID)
        do {
            let observed = try await control.perform(action, serviceID: serviceID)
            if serviceID == FleetServiceRegistry.server.id {
                // The BotFleet Server's own agent is a wrapper that exits as soon
                // as the server is healthy,  so launchctl cannot say whether the
                // server is still serving:  it says stopped while the server is up.
                // The port can say it,  so a stop waits for the port to close,  and
                // the read that follows reports the server as it really is.
                if action == .stop {
                    await Self.waitForTheServerToGo(presence: presence, wait: stopWait)
                }
                // The state the manager reported is the launchd view of a wrapper,
                // so it is not what the owner is shown.
            } else {
                states[serviceID] = observed
            }
        } catch {
            ownerStops.restore(previous)
            problem = "\(service.title):  \(error.localizedDescription)"
        }
        await refresh()
    }

    /// Waits for the BotFleet Server's port to close.
    ///
    /// The server's own stop is asynchronous,  and launchd does not own the process,
    /// so the port is the only honest witness that the server has gone.  A server
    /// that is still there when the budget runs out is left as it is:  the read that
    /// follows reports it as unknown rather than as stopped.
    nonisolated static func waitForTheServerToGo(presence: ServerPresenceProbe,
                                                 wait: ServerStopWait) async {
        for _ in 0..<wait.attempts {
            if presence.presence() == .absent { return }
            try? await Task.sleep(nanoseconds: UInt64(wait.interval * 1_000_000_000))
        }
    }

    /// Starts the BotFleet Server through its own installed wrapper.
    func startServer() async {
        problem = nil
        let startup = self.startup
        do {
            // launchctl blocks,  so the start does not happen on the main thread.
            try await Task.detached(priority: .userInitiated) { try startup.startServer() }.value
        } catch {
            problem = "The server did not start:  \(error.localizedDescription)"
        }
        await refresh()
    }

    /// Stops the BotFleet Server by asking the server itself.
    ///
    /// launchd does not always own the process,  so booting the agent out can leave a
    /// server that is still serving.
    func stopServer() async {
        problem = nil
        do {
            try await api.stopServer()
        } catch {
            problem = "The server did not stop:  \(error.localizedDescription)"
        }
        await refresh()
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
        }
        launchesAtLogin = SMAppService.mainApp.status == .enabled
    }
}
