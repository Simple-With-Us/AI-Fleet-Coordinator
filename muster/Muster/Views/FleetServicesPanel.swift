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
        .task { await admin.refresh() }
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
                        Task { await admin.perform(action.action, serviceID: entry.id) }
                    }
                    .disabled(admin.busyServiceID != nil)
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
