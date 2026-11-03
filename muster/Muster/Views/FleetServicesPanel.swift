import SwiftUI

/// The Fleet Services panel:  every long-lived process the fleet runs on this Mac,  and
/// what the owner can do about each one.
///
/// The panel is the wide version of the menubar's fleet admin:  the same services,  the
/// same states and the same actions,  with room for the BotFleet Server's own controls
/// and for the reason a service cannot be changed.
///
/// The states are probed and never assumed:  a pm2 application is what pm2 reports,  a
/// launchd agent is what launchctl reports,  and the BotFleet MCP Server is what its
/// own health endpoint reports.
struct FleetServicesPanel: View {
    @EnvironmentObject private var admin: FleetAdmin
    @ObservedObject private var controller = OpsController.shared
    /// The allowlist as this Mac's checkout carries it, read when the panel opens.
    @State private var allowlist: OpsAllowlist?
    /// The service whose sheet action this panel started, until that action settles.
    @State private var pendingServiceID: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                serverCard
                if let problem = admin.problem {
                    problemBox(problem)
                }
                servicesSection
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task {
            allowlist = OpsAllowlist.read(
                at: OpsAllowlist.url(repoRoot: FleetConfigLocation.resolve().repoRoot))
            await admin.refresh()
        }
        .onChange(of: controller.phase) { _, _ in
            settlePendingAction()
        }
        .onChange(of: controller.completedRuns) { _, _ in
            settlePendingAction()
            Task {
                await admin.refresh()
                // A service that just started may not answer its health probe on
                // the first read after the helper exits, so read once more after
                // a moment rather than leaving that first reading on screen.
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await admin.refresh()
            }
        }
    }

    // MARK: The header

    private var header: some View {
        HStack {
            Text("Fleet Services")
                .font(.largeTitle.weight(.semibold))
            Spacer()
            Button {
                Task { await admin.refresh() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(admin.reading)
        }
    }

    // MARK: The server's own card

    /// The BotFleet Server's own controls.
    ///
    /// The server is stopped by asking the server itself,  because launchd does not
    /// always own the process:  the installed wrapper exits when the harness is already
    /// healthy,  and the desktop app forks its own harness when nothing answers on the
    /// port,  so booting the agent out can leave a server that is still serving.
    private var serverCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("BotFleet Server")
                .font(.headline)
            Text(admin.serverRunning ? "Running." : "Not running.")
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack(spacing: 12) {
                Button("Start Server") {
                    Task { await admin.startServer() }
                }
                .disabled(admin.serverRunning || admin.busyServiceID != nil)
                Button("Stop Server") {
                    guard confirmFleetStop(title: "BotFleet Server",
                                           message: admin.stopConfirmation(for: FleetServiceRegistry.server.id))
                    else { return }
                    Task { await admin.stopServer() }
                }
                .disabled(!admin.serverRunning || admin.busyServiceID != nil)
                Toggle("Start Server At Login", isOn: Binding(
                    get: { admin.startsAtLogin },
                    set: { wanted in Task { await admin.setStartsAtLogin(wanted) } }
                ))
                .toggleStyle(.switch)
                .disabled(!admin.startupPolicySupported)
            }
            if !admin.startupPolicySupported {
                Text("This Mac's BotFleet is too old to manage the server's startup policy.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Divider()
            Toggle("Launch Muster At Login", isOn: Binding(
                get: { admin.launchesAtLogin },
                set: { admin.setLaunchesAtLogin($0) }
            ))
            .toggleStyle(.switch)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08))
        .cornerRadius(10)
    }

    // MARK: The services

    private var servicesSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("The fleet's own services")
                .font(.headline)
            if admin.entries.isEmpty {
                Text(admin.reading
                     ? "Reading the fleet services…"
                     : "No fleet service has been read yet.  Refresh to read them.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(admin.entries, id: \.id) { entry in
                    serviceRow(entry)
                }
            }
        }
    }

    /// One service,  with the actions that make sense for its state.
    private func serviceRow(_ entry: FleetServiceEntry) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.title)
                    .font(.body.weight(.medium))
                Text(entry.summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if entry.stoppedByOwner {
                    Text("Stopped on purpose;  it stays stopped until it is started again.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            HStack(spacing: 8) {
                ForEach(fleetAdminActions(for: entry)) { action in
                    Button(action.title) {
                        actuate(action.action, for: entry)
                    }
                    .disabled(admin.busyServiceID != nil || controller.isPresenting)
                }
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 10)
        .background(Color.secondary.opacity(0.05))
        .cornerRadius(8)
    }

    /// One row's action,  on the one control path.
    ///
    /// A pm2 row and a LaunchAgent row go through `OpsController`:  the helper plans
    /// the change,  the sheet shows the plan and asks Touch ID,  and only then does
    /// anything run,  against the allowlist the helper checks again itself.  The
    /// BotFleet Server's own row stays with the fleet admin:  its start clears the
    /// deliberate-stop marker through the installed wrapper and its stop asks the
    /// server itself,  neither of which an allowlist argv can say,  so it keeps its
    /// stop confirmation here.  A LaunchAgent row the allowlist does not name yet
    /// keeps that same fleet admin path, because the helper could only refuse it.
    private func actuate(_ action: ManagedServiceAction, for entry: FleetServiceEntry) {
        let verb: OpsVerb?
        switch action {
        case .start: verb = .start
        case .stop: verb = .stop
        case .restart: verb = .restart
        case .none: verb = nil
        }
        guard let verb else { return }
        switch entry.kind {
        case .pm2(let appName):
            planThroughController(.pm2App(verb: verb, name: appName),
                                  action: action, entry: entry)
        case .launchAgent(let label):
            if entry.id == FleetServiceRegistry.server.id {
                performThroughAdmin(action, entry: entry)
            } else if let allowlist, allowlist.verbs(forMacAgent: label).isEmpty {
                // The helper fails closed on a label its allowlist does not name,
                // so a row with no entry keeps the fleet admin path the menubar
                // still uses until the owner adds that label to the allowlist.
                performThroughAdmin(action, entry: entry)
            } else {
                planThroughController(.macAgent(verb: verb, label: label),
                                      action: action, entry: entry)
            }
        }
    }

    /// Plans one action on the controller path and records the owner's intent
    /// behind it, so a stop made here is a stop the fleet admin knows about.
    private func planThroughController(_ opsAction: OpsAction,
                                       action: ManagedServiceAction,
                                       entry: FleetServiceEntry) {
        guard controller.plan(opsAction) else { return }
        admin.beginControllerAction(action, serviceID: entry.id)
        pendingServiceID = entry.id
    }

    /// Runs one action through the fleet admin, with the stop confirmation that
    /// path has always asked for.
    private func performThroughAdmin(_ action: ManagedServiceAction, entry: FleetServiceEntry) {
        if action == .stop,
           !confirmFleetStop(title: entry.title,
                             message: admin.stopConfirmation(for: entry.id)) {
            return
        }
        Task { await admin.perform(action, serviceID: entry.id) }
    }

    /// Settles the intent recorded for this panel's sheet action once that action
    /// reaches an end:  a run that did what it was asked keeps the record, and
    /// anything else, the sheet closed without a run included, restores it.
    private func settlePendingAction() {
        guard let serviceID = pendingServiceID else { return }
        switch controller.phase {
        case .finished(_, let outcome):
            admin.endControllerAction(serviceID: serviceID,
                                      succeeded: outcome.isSuccess(for: .yes))
            pendingServiceID = nil
        case .failed, .idle:
            admin.endControllerAction(serviceID: serviceID, succeeded: false)
            pendingServiceID = nil
        case .planning, .review, .running:
            break
        }
    }

    // MARK: A refusal

    private func problemBox(_ problem: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("The last change was refused")
                .font(.callout.weight(.semibold))
            Text(problem)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.08))
        .cornerRadius(8)
    }
}
