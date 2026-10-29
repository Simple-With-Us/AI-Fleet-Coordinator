import SwiftUI

/// The window's root:  the MiniMax panel, the fleet grid and the fleet view, over one
/// shared run.
struct FleetOverviewView: View {
    @EnvironmentObject private var store: FleetStore
    @State private var theme: ThemeChoice = .system

    var body: some View {
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
            TabView {
                MiniMaxDetailView()
                    .tabItem { Label("MiniMax", systemImage: "bolt.shield") }
                FleetGridView()
                    .tabItem { Label("Fleet Grid", systemImage: "square.grid.3x3") }
                FleetView()
                    .tabItem { Label("Fleet", systemImage: "list.bullet.rectangle") }
                FleetHostsView()
                    .tabItem { Label("Fleet Hosts", systemImage: "server.rack") }
                ServerStatusPanel()
                    .tabItem { Label("Server Status", systemImage: "cpu") }
                OperationsView()
                    .tabItem { Label("Operations", systemImage: "gearshape.2") }
                ControlsView()
                    .tabItem { Label("Controls", systemImage: "slider.horizontal.3") }
                WakeSettingsView()
                    .tabItem { Label("Wake Settings", systemImage: "bell.badge") }
                SeatActivityView()
                    .tabItem { Label("Seat Activity", systemImage: "person.2.badge.gearshape") }
                DriftHistoryView()
                    .tabItem { Label("Drift History", systemImage: "exclamationmark.triangle") }
            }
            Divider()
            ApplyActivityView()
            footer
        }
        .preferredColorScheme(theme.colorScheme)
        // The plan sheet hangs off the window root,  so the apply flow can present a
        // plan from any view that holds a red cell.
        .applyPlanSheet()
        // The operations sheet hangs off the root the same way,  so the Server Status,
        // Operations and Controls tabs share one plan,  one Touch ID question and one
        // action in flight.
        .opsActionSheet()
        .toolbar {
            ToolbarItemGroup {
                Picker("Theme", selection: $theme) {
                    ForEach(ThemeChoice.allCases) { choice in
                        Text(choice.title).tag(choice)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 210)

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
        return "read at " + generatedAt.formatted(date: .omitted, time: .standard)
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

/// The theme the window follows.
enum ThemeChoice: String, CaseIterable, Identifiable {
    case system
    case light
    case dark

    var id: String { rawValue }

    var title: String {
        switch self {
        case .system: return "System"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }

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
