import SwiftUI

/// The window's root:  the sidebar's places in one detail column, over one shared run.
///
/// The sidebar names every panel the window holds, which the tab bar it replaced did
/// not:  at the window's usual width the tabs past the first few folded into an
/// overflow menu, and a panel inside it was a panel the owner had to know to look for.
struct FleetOverviewView: View {
    @EnvironmentObject private var store: FleetStore
    @EnvironmentObject private var monitor: DriftMonitor
    @AppStorage(MusterSettings.themeDefaultsKey) private var theme: ThemeChoice = .system
    @AppStorage(MusterSettings.sidebarSelectionDefaultsKey) private var storedDestination = MusterDestination.overview.rawValue
    @State private var destination: MusterDestination

    init() {
        let saved = UserDefaults.standard.string(forKey: MusterSettings.sidebarSelectionDefaultsKey)
        let restored = MusterDestination.restored(from: saved)
        if let saved, MusterDestination(rawValue: saved) == nil {
            // Heal a spelling this app no longer names, so the fallback runs once
            // instead of on every launch.
            UserDefaults.standard.set(restored.rawValue, forKey: MusterSettings.sidebarSelectionDefaultsKey)
        }
        _destination = State(initialValue: restored)
    }

    var body: some View {
        NavigationSplitView {
            List(selection: $destination) {
                Section {
                    sidebarRow(.overview)
                }
                Section("Fleet") {
                    ForEach(MusterDestination.fleet) { sidebarRow($0) }
                }
                Section("This Mac") {
                    ForEach(MusterDestination.thisMac) { sidebarRow($0) }
                }
                Section("Server") {
                    ForEach(MusterDestination.server) { sidebarRow($0) }
                }
            }
            .navigationTitle("Muster")
            .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 300)
        } detail: {
            VStack(spacing: 0) {
                if case .failed(let reason) = store.phase {
                    FailureBanner(reason: reason)
                }
                if let problem = store.manifestProblem {
                    FailureBanner(
                        reason: problem,
                        title: "The manifest could not be read,  so the roster is not the manifest's."
                    )
                }
                if let problem = store.registryProblem {
                    FailureBanner(
                        reason: problem,
                        title: "The registry could not be read,  so the grid has no rows of its own."
                    )
                }
                detail
                // The footer and the fix activity describe the run and its cells, so
                // they sit under the two places that read those cells and nowhere
                // else:  under every panel they read as belonging to it, including
                // panels an engine failure says nothing about.
                if destination == .overview || destination == .complianceGrid {
                    Divider()
                    ApplyActivityView()
                    footer
                }
            }
            .navigationTitle(destination.title)
            .toolbar {
                ToolbarItem {
                    // The one refresh the window keeps is the engine's.  Panels with a
                    // source of their own keep their own refresh beside their rows,
                    // which names what it rereads;  this one ran the engine when it
                    // stood over the tabs and still does.
                    Button {
                        store.refresh()
                    } label: {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                    .disabled(store.phase == .running)
                    .help("Run the engine again")
                }
            }
        }
        .preferredColorScheme(theme.colorScheme)
        // The plan sheet hangs off the window root,  so the apply flow can present a
        // plan from any view that holds a red cell.
        .applyPlanSheet()
        // The operations sheet hangs off the root the same way,  so the Server Status,
        // Operations and Controls panels share one plan,  one Touch ID question and one
        // action in flight.
        .opsActionSheet()
        .onChange(of: destination) { _, chosen in
            storedDestination = chosen.rawValue
        }
        // The menu chooses a place by writing the saved one,  so an open window follows it.
        .onChange(of: storedDestination) { _, saved in
            let chosen = MusterDestination.restored(from: saved)
            if chosen != destination { destination = chosen }
        }
        .background(MusterWindowTracker())
    }

    /// One place in the sidebar.  The two compliance places carry the drift count,
    /// which is the monitor's own number:  nil reads unknown, never nothing.
    @ViewBuilder
    private func sidebarRow(_ place: MusterDestination) -> some View {
        let label = Label(place.title, systemImage: place.systemImage).tag(place)
        if place == .complianceGrid || place == .driftHistory {
            if let redCount = monitor.redCount, redCount > 0 {
                label.badge(redCount)
            } else if monitor.redCount == nil {
                label.badge(Text("?"))
            } else {
                label
            }
        } else {
            label
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch destination {
        case .overview:
            OverviewView(selection: $destination)
        case .complianceGrid:
            FleetGridView()
        case .appsAndWork:
            FleetView()
        case .seats:
            SeatActivityView()
        case .driftHistory:
            DriftHistoryView()
        case .services:
            FleetServicesPanel()
        case .wakeAndListener:
            WakeSettingsView()
        case .controls:
            ControlsView()
        case .seatDetail:
            MiniMaxDetailView()
        case .serverStatus:
            ServerStatusPanel()
        case .serverResourceLimits:
            ResourceLimitsView(
                readPolicy: { await OpsResourcePolicyReader().read() },
                onReviewChange: { field, value, revision in
                    OpsController.shared.plan(.serverResource(field: field, value: value, revision: revision))
                }
            )
        case .hosts:
            FleetHostsView()
        case .virtualMachines:
            VirtualMachinesView()
        case .operations:
            OperationsView()
        }
    }

    /// The bar under the panels:  the verdict, the command the app ran, and the clock.
    private var footer: some View {
        HStack(spacing: 14) {
            verdict
            Divider().frame(height: 14)
            Text(store.commandLine.isEmpty ? "no engine located" : store.commandLine)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
                .help(store.commandLine)
            Spacer(minLength: 8)
            Text(generatedText)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 9)
        .background(.bar)
    }

    private var verdict: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(verdictColor)
                .frame(width: 8, height: 8)
            Text(verdictText)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
            if let summary = store.summary {
                Text("\(summary.failing) failing of \(summary.total)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .foregroundStyle(verdictColor)
        // The footer's verdict is the grid's PASS,  and the tooltip says what that is.
        .help(
            store.phase == .loaded
                ? "\(PassMeaning.verdict(store.summary?.overallVerdict))\u{00A0} \(PassMeaning.everywhere)"
                : ""
        )
    }

    private var verdictText: String {
        switch store.phase {
        case .idle: return "IDLE"
        case .running: return "RUNNING"
        case .loaded: return store.summary?.overallVerdict ?? "UNKNOWN"
        case .failed: return "UNKNOWN"
        }
    }

    private var verdictColor: Color {
        switch store.phase {
        case .idle: return .secondary
        case .running: return .secondary
        case .loaded:
            switch store.summary?.overallVerdict {
            case "PASS": return .green
            case "FAIL": return .red
            default: return .orange
            }
        case .failed: return .red
        }
    }

    private var generatedText: String {
        guard let generatedAt = store.generatedAt else { return "—" }
        return "read at " + FleetTime.clock(generatedAt)
    }
}

/// The banner that says why the last run cannot be trusted.
struct FailureBanner: View {
    let reason: String
    var title: String = "The engine did not answer, so every cell reads unknown."

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.callout.weight(.medium))
                Text(reason)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(Color.red.opacity(0.10))
    }
}

extension ThemeChoice {
    /// The scheme the window asks for;  nil leaves the system in charge.
    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }
}

#Preview {
    FleetOverviewView()
        .environmentObject(FleetStore())
        .environmentObject(DriftMonitor(grid: PreviewGrid.shared))
}
