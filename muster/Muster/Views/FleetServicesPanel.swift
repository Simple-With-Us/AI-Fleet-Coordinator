import SwiftUI

/// The Services panel:  every long-lived process the fleet runs on the Mac,  and what the
/// owner can do about each one.
///
/// The panel is the wide version of the menu's The Mac section:  the same services,  the
/// same states and the same actions on the same route,  with room for BotFleet Server's
/// own controls and for the reason a service cannot be changed.
///
/// The states are probed and never assumed:  a pm2 application is what pm2 reports,  a
/// launchd agent is what launchctl reports,  and the BotFleet MCP Server is what its
/// own health endpoint reports.
struct FleetServicesPanel: View {
    @EnvironmentObject private var admin: FleetAdmin
    @EnvironmentObject private var router: ServiceActionRouter

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                serverCard
                if let problem = lastProblem {
                    problemBox(problem)
                }
                servicesSection
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task {
            router.reloadAllowlist()
            await admin.refresh()
        }
    }

    /// The whole reason for the last thing that did not work.  The menu can only hold part
    /// of it,  so its row opens this panel.
    private var lastProblem: String? {
        if let problem = admin.problem { return problem }
        if case .some(.failed) = router.actionState, let state = router.actionState {
            return FleetActionWords.line(for: state)
        }
        return nil
    }

    // MARK: The header

    private var header: some View {
        HStack {
            Text("Services \(FleetHost.mac.onTitle)")
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

    /// BotFleet Server's own controls.  It runs on the Mac,  and its name says Server,  so
    /// the card says which machine at every turn.
    ///
    /// The server is stopped by asking the server itself,  because launchd does not
    /// always own the process:  the installed wrapper exits when the harness is already
    /// healthy,  and the desktop app forks its own harness when nothing answers on the
    /// port,  so booting the agent out can leave a server that is still serving.
    private var serverCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Text("BotFleet Server")
                    .font(.headline)
                PlaceBadge(place: .machine(.mac))
            }
            Text(serverStateText)
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack(spacing: 12) {
                // The card and the row are one service on one route.  A state that is not
                // known offers neither:  the owner refreshes instead.
                Button("Start \(FleetHost.mac.onTitle)") {
                    startOnServer(.start)
                }
                .disabled(admin.localServiceState != .stopped || router.isBusy)
                Button("Stop \(FleetHost.mac.onTitle)") {
                    startOnServer(.stop)
                }
                .disabled(admin.localServiceState != .running || router.isBusy)
                Toggle("Start At Mac Login", isOn: Binding(
                    get: { admin.startsAtLogin },
                    set: { wanted in Task { await admin.setStartsAtLogin(wanted) } }
                ))
                .toggleStyle(.switch)
                .disabled(!admin.startupPolicySupported)
            }
            if !admin.startupPolicySupported {
                Text("BotFleet on the Mac is too old to manage BotFleet Server's startup policy.")
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

    /// What is known about BotFleet Server,  in three states.
    private var serverStateText: String {
        switch admin.localServiceState {
        case .running: return "Running \(FleetHost.mac.inProse)."
        case .stopped: return "Not running \(FleetHost.mac.inProse)."
        case .unknown: return "State not known."
        }
    }

    /// Runs one of the card's two buttons on the same route as the service's row.
    private func startOnServer(_ action: ManagedServiceAction) {
        guard let entry = admin.entries.first(where: { $0.id == FleetServiceRegistry.server.id }),
              let menuAction = router.action(action, on: entry) else { return }
        router.start(menuAction)
    }

    // MARK: The services

    private var servicesSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Every Service \(FleetHost.mac.onTitle)")
                .font(.headline)
            if admin.entries.isEmpty {
                Text(admin.reading
                     ? "Reading the Mac\u{2026}"
                     : "No service on the Mac has been read yet.\u{00A0} Refresh to read them.")
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
                    if let menuAction = router.action(action.action, on: entry) {
                        Button(action.title) {
                            router.start(menuAction)
                        }
                        .disabled(router.isBusy)
                        .help("\(action.title) \(entry.title) \(entry.host.inProse)")
                        .accessibilityLabel("\(action.title) \(entry.title) \(entry.host.inProse)")
                    }
                }
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 10)
        .background(Color.secondary.opacity(0.05))
        .cornerRadius(8)
    }

    // MARK: A refusal

    private func problemBox(_ problem: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("The Last Change Did Not Work")
                .font(.callout.weight(.semibold))
            Text(problem)
                .font(.callout)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.red.opacity(0.08))
        .cornerRadius(8)
    }
}
