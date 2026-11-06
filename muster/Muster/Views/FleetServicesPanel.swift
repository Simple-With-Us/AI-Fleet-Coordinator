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
    @AppStorage(MusterSettings.keepServicesRunningDefaultsKey) private var keepServicesRunning = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                serverCard
                reconcilerLine
                if let problem = admin.ownerStopProblem {
                    problemBox(problem, heading: "Muster Cannot Keep Your Stops")
                }
                if let problem = lastProblem {
                    problemBox(problem, heading: FleetActionWords.panelHeading(actionState: router.actionState,
                                                                               adminProblem: admin.problem,
                                                                               adminProblemIsNotKnown: admin.problemIsNotKnown))
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
        FleetActionWords.panelProblem(actionState: router.actionState, adminProblem: admin.problem)
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

    // MARK: What Muster does about a stopped service

    /// Says whether Muster starts services again on its own,  and where to turn that off.
    private var reconcilerLine: some View {
        Text(keepServicesRunning
             ? "Muster starts a service again when its row says Keep Running and it stops, waits longer after each try, and stops trying after 3.\u{00A0} It never stops a service, and it never starts one you stopped.\u{00A0} Turn this off in Settings."
             : "Muster does not start services on its own, because Keep Services Running is turned off in Settings.")
            .font(.caption)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
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
                if admin.services != nil {
                    ServiceStateBadge(marker: ServiceStateMarker.make(state: admin.localServiceState.managedState))
                }
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
                if let line = pm2Line {
                    Text(line)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(rowsProblemsFirst, id: \.id) { entry in
                    serviceRow(entry)
                }
            }
        }
    }

    /// The rows,  with the ones that need a look first.  The order inside each half is the
    /// registry's own.
    private var rowsProblemsFirst: [FleetServiceEntry] {
        let entries = admin.entries
        return entries.filter { $0.needsAttention } + entries.filter { !$0.needsAttention }
    }

    /// True for a pm2 application whose state could not be read because pm2 did not answer.
    private func pm2Unread(_ entry: FleetServiceEntry) -> Bool {
        guard admin.pm2Answered == false, case .pm2 = entry.kind else { return false }
        return entry.policy == .alwaysOn
    }

    /// One line above the rows when pm2 did not answer,  saying whether it is there at all,
    /// so nine rows do not each repeat one fact.
    private var pm2Line: String? {
        guard admin.pm2Answered == false else { return nil }
        let count = admin.entries.filter { pm2Unread($0) }.count
        let services = count == 1 ? "1 service" : "\(count) services"
        if admin.pm2Installed {
            return "pm2 did not answer, so \(services) cannot be read or changed."
        }
        return "pm2 is not installed on the Mac, so \(services) cannot be read or changed."
    }

    /// One service,  with the actions that make sense for its state.
    private func serviceRow(_ entry: FleetServiceEntry) -> some View {
        let unread = pm2Unread(entry)
        // pm2 did not answer,  so a button here could only fail:  the line above the rows says so.
        let actions: [FleetAdminAction] = unread ? [] : fleetAdminActions(for: entry)
        return HStack(alignment: .firstTextBaseline, spacing: 12) {
            ServiceStateBadge(marker: entry.marker)
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.title)
                    .font(.body.weight(.medium))
                Text(entry.summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let note = entry.stateNote {
                    Text(note)
                        .font(.caption)
                        .foregroundStyle(entry.reconcileNote?.isProblem == true && !entry.stoppedByOwner ? Color.orange : Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text(entry.reconcilePolicyLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !unread, let why = entry.whyNoActions {
                    Text("No action:  \(why).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !unread, let why = whyActionsAreMissing(entry) {
                    Text(why)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            HStack(spacing: 8) {
                ForEach(actions) { action in
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

    /// Why a row shows fewer buttons than its state offers,  or nil when it shows them all.
    private func whyActionsAreMissing(_ entry: FleetServiceEntry) -> String? {
        let routes = fleetAdminActions(for: entry).map { router.action($0.action, on: entry) }
        guard routes.contains(where: { $0 == nil }) else { return nil }
        return MachineMenuCopy.noRouteSentence(allowlist: router.allowlist,
                                               someRouted: routes.contains(where: { $0 != nil }))
    }

    // MARK: A refusal

    private func problemBox(_ problem: String, heading: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(heading)
                .font(.callout.weight(.semibold))
            Text(problem)
                .font(.callout)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background((heading == FleetActionWords.notKnownHeading ? Color.orange : Color.red).opacity(0.08))
        .cornerRadius(8)
    }
}

/// A service's state as a word,  a symbol and a colour,  so it reads at a glance and never by
/// colour alone.
struct ServiceStateBadge: View {
    let marker: ServiceStateMarker

    private var tint: Color {
        switch marker.tone {
        case .good: return .green
        case .bad: return .red
        case .warning: return .orange
        case .neutral: return .secondary
        }
    }

    var body: some View {
        Label(marker.word, systemImage: marker.symbol)
            .font(.caption.weight(.semibold))
            .foregroundStyle(tint)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
            .frame(minWidth: 118, alignment: .leading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(marker.word)
    }
}
