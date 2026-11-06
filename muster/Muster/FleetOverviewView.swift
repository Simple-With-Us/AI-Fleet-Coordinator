import SwiftUI

/// The window's root:  the sidebar's places in one detail column, over one shared run.
///
/// The sidebar names every panel the window holds, which the tab bar it replaced did
/// not:  at the window's usual width the tabs past the first few folded into an
/// overflow menu, and a panel inside it was a panel the owner had to know to look for.
///
/// The sidebar files each panel under the place its data and actions live, The Mac, The
/// Server or Across The Fleet, and the files come from `MusterDestination.place` alone,
/// so a heading can never hold a panel that belongs to another machine.
struct FleetOverviewView: View {
    @EnvironmentObject private var store: FleetStore
    @EnvironmentObject private var monitor: DriftMonitor
    @EnvironmentObject private var admin: FleetAdmin
    @EnvironmentObject private var watch: PlaceWatch
    @EnvironmentObject private var router: ServiceActionRouter
    @AppStorage(MusterSettings.themeDefaultsKey) private var theme: ThemeChoice = .system
    @AppStorage(MusterSettings.sidebarSelectionDefaultsKey) private var storedDestination = MusterDestination.overview.rawValue
    @State private var destination: MusterDestination
    /// The filter an Overview card asked the grid to open on,  held by this window only.
    @State private var gridRequest: GridStateFilter?

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
        let menu = MachineMenu(MachineMenuInputs(admin: admin, monitor: monitor, watch: watch, router: router))
        NavigationSplitView {
            List(selection: $destination) {
                Section {
                    sidebarRow(.overview, menu: menu)
                }
                ForEach(MusterDestination.sidebarPlaces, id: \.sectionHeading) { place in
                    Section(place.sectionHeading) {
                        ForEach(MusterDestination.listed(under: place)) { sidebarRow($0, menu: menu) }
                    }
                }
            }
            .navigationTitle("Muster")
            .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 300)
        } detail: {
            VStack(spacing: 0) {
                if let place = destination.place {
                    PlaceStrip(place: place, source: destination.sourceLine(sourceInputs))
                    Divider()
                }
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
                // The fix activity describes the run and its cells, so it sits under the
                // two places that read those cells and nowhere else.  The footer, with
                // the command the app ran, is the grid's alone:  the Overview shows the
                // verdict once, in its own words.
                if destination == .overview || destination == .complianceGrid {
                    Divider()
                    ApplyActivityView()
                }
                if destination == .complianceGrid {
                    footer
                }
            }
            .navigationTitle(destination.title)
            .navigationSubtitle(destination.subtitle)
            .toolbar {
                ToolbarItem {
                    // The engine's run is the Overview's and the grid's.  Every other panel
                    // keeps its own button, named for what it rereads, so the toolbar never
                    // offers a refresh that leaves the panel in view unchanged.
                    if destination == .overview || destination == .complianceGrid {
                        Button {
                            store.refresh()
                        } label: {
                            Label("Check Seat Setup", systemImage: "checkmark.shield")
                        }
                        .disabled(store.phase == .running)
                        .help("Checks the seats' setup on the Mac again.")
                    }
                }
            }
        }
        .preferredColorScheme(theme.colorScheme)
        // The plan sheet hangs off the window root,  so the apply flow can present a
        // plan from any view that holds a red cell.
        .applyPlanSheet()
        // The operations sheet hangs off the root the same way,  so every panel that starts
        // an action shares one plan,  one Touch ID question and one action in flight.
        .opsActionSheet()
        .onChange(of: destination) { _, chosen in
            storedDestination = chosen.rawValue
            // A card's request is for the visit it started,  and no later one.
            if chosen != .complianceGrid { gridRequest = nil }
        }
        // The menu chooses a place by writing the saved one,  so an open window follows it.
        .onChange(of: storedDestination) { _, saved in
            let chosen = MusterDestination.restored(from: saved)
            if chosen != destination { destination = chosen }
        }
        .background(MusterWindowTracker())
    }

    /// What the window knows about each panel's source,  for the line under its badge.
    private var sourceInputs: PlaceSourceInputs {
        PlaceSourceInputs(
            servicesReadAt: admin.lastReadAt,
            serverStatus: watch.status,
            seatCheckAt: store.generatedAt
        )
    }

    /// One place in the sidebar,  with the badge its own reader gives,  or none.
    @ViewBuilder
    private func sidebarRow(_ place: MusterDestination, menu: MachineMenu) -> some View {
        let label = Label(place.title, systemImage: place.systemImage).tag(place)
        if let badge = sidebarBadge(place, menu: menu) {
            label.badge(Text(badge))
        } else {
            label
        }
    }

    /// The badge a row carries.  Each one is its own reader's number:  nil reads unknown,
    /// never nothing.
    ///
    /// Drift History carries none:  the current red count belongs to the grid, and the
    /// history's own number is on the Overview's card.
    private func sidebarBadge(_ place: MusterDestination, menu: MachineMenu) -> String? {
        switch place {
        case .complianceGrid:
            guard let redCount = monitor.redCount else { return "?" }
            return redCount > 0 ? "\(redCount)" : nil
        case .services:
            // Measured against the watch's tick,  the clock the Mac card and the menu use,  so
            // the window and the menu never disagree about one read.  The tick is a minute at
            // most behind,  and the menu's own window allows for it.
            return SidebarBadges.services(admin.entries, pm2Answered: admin.pm2Answered ?? true,
                                          readAt: admin.lastReadAt, now: watch.now)
        case .serverStatus:
            return PlaceHealthWords.badge(menu.server.health)
        case .overview, .macAgents, .driftHistory, .wakeAndListener, .seatDetail,
             .serverResourceLimits, .serverUpdates, .appsAndWork, .seats, .controls,
             .hosts, .virtualMachines, .actionHistory:
            return nil
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch destination {
        case .overview:
            OverviewView(selection: $destination, gridRequest: $gridRequest)
        case .complianceGrid:
            FleetGridView(request: $gridRequest)
        case .appsAndWork:
            FleetView()
        case .seats:
            SeatActivityView()
        case .driftHistory:
            DriftHistoryView()
        case .services:
            FleetServicesPanel()
        case .macAgents:
            OperationsView(part: .agents)
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
        case .serverUpdates:
            OperationsView(part: .updates)
        case .hosts:
            FleetHostsView()
        case .virtualMachines:
            VirtualMachinesView()
        case .actionHistory:
            OperationsView(part: .history)
        }
    }

    /// The bar under the grid:  the verdict, the command the app ran, and the clock.
    private var footer: some View {
        let verdict = RunVerdict.make(phase: store.phase, verdict: store.summary?.overallVerdict)
        return VStack(spacing: 0) {
            Divider()
            HStack(spacing: 14) {
                RunVerdictLabel(verdict: verdict, font: .system(size: 11, weight: .semibold))
                    // The footer's verdict is the grid's PASS,  and the tooltip says what that is.
                    // Only a verdict the engine returned has a meaning to state,  as the overview says.
                    .help(RunVerdict.meaning(phase: store.phase, verdict: store.summary?.overallVerdict) ?? "")
                if let failing = RunVerdict.failingText(store.summary) {
                    Text(failing)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                }
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
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 9)
            .background(.bar)
        }
    }

    private var generatedText: String {
        guard let generatedAt = store.generatedAt else { return "\u{2014}" }
        return "Checked at " + FleetTime.clock(generatedAt)
    }
}

/// The run's verdict:  a symbol and the words, in the tone's colour.  The words carry it.
struct RunVerdictLabel: View {
    let verdict: RunVerdict
    var font: Font = .title2.weight(.semibold)

    var body: some View {
        Label(verdict.words, systemImage: symbol)
            .font(font)
            .foregroundStyle(color)
    }

    private var symbol: String {
        switch verdict.tone {
        case .neutral: return "circle.dashed"
        case .pass: return "checkmark.circle.fill"
        case .fail: return "xmark.octagon.fill"
        case .notKnown: return "questionmark.circle.fill"
        }
    }

    private var color: Color {
        switch verdict.tone {
        case .neutral: return .secondary
        case .pass: return .green
        case .fail: return .red
        case .notKnown: return .orange
        }
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
