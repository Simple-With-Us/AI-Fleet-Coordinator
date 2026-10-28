import Combine
import Foundation

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
/// The read is off the main thread,  so the menu never waits for pm2 and launchctl to
/// answer,  and only the newest read may write the states,  so a slower older read does
/// not leave the menu showing what was true before the action the owner took.
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

    init(control: FleetServiceControl? = nil,
         startup: StartupPolicy = StartupPolicy(),
         presence: ServerPresenceProbe = ServerPresenceProbe(),
         api: ControlAPI = ControlAPI()) {
        let mcp = try? McpServerControl()
        self.mcpServer = mcp
        self.startup = startup
        self.presence = presence
        self.api = api
        self.control = control ?? FleetServiceControl(
            pm2: PM2Client(),
            startup: startup,
            mcpServer: mcp,
            stopServer: { [api] in try await api.stopServer() }
        )
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
        let serverState = self.serverState()
        let control = self.control
        let states = await Task.detached(priority: .userInitiated) {
            await control.states(serverState: serverState)
        }.value
        guard sequencer.isCurrent(generation) else { return }
        self.states = states
        publish()
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

    /// What the admin knows about the BotFleet Server itself.
    private func serverState() -> ManagedServices.ServerState {
        ManagedServices.serverState(statusAvailable: apiAvailable,
                                    installedServerState: startup.installedServerState,
                                    serverPresence: presence.presence())
    }

    /// Publishes the last read.
    private func publish() {
        let serverState = serverState()
        services = FleetServices(serverState: serverState,
                                states: states,
                                stoppedByOwner: ownerStops.ids,
                                startupPolicySupported: startup.supportsPolicy)
        startupPolicySupported = startup.supportsPolicy
        startsAtLogin = startup.startsAtLogin
        serverRunning = serverState == .running
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
            states[serviceID] = observed
        } catch {
            ownerStops.restore(previous)
            problem = "\(service.title):  \(error.localizedDescription)"
        }
        await refresh()
    }

    /// Starts the BotFleet Server through its own installed wrapper.
    func startServer() async {
        problem = nil
        do {
            try startup.startServer()
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
        do {
            try startup.setStartsAtLogin(enabled)
        } catch {
            problem = "The startup policy did not change:  \(error.localizedDescription)"
        }
        await refresh()
    }

    /// The entries,  as the menu and the panel show them.
    var entries: [FleetServiceEntry] {
        services?.entries ?? []
    }
}
