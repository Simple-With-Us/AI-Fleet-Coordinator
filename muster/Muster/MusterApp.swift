import SwiftUI

@main
struct MusterApp: App {
    @NSApplicationDelegateAdaptor(AppIconFollower.self) private var appIconFollower
    @StateObject private var store: FleetStore
    @StateObject private var monitor: DriftMonitor
    @StateObject private var admin: FleetAdmin

    init() {
        // The owner is asked once,  here,  whether this app may notify them.  An
        // apply that asked on its own path would hold its result until the
        // prompt was answered.
        SystemApplyNotifier.requestPermissionOnce()
        let store = FleetStore()
        _store = StateObject(wrappedValue: store)
        _monitor = StateObject(wrappedValue: DriftMonitor(grid: store))
        _admin = StateObject(wrappedValue: FleetAdmin())
    }

    var body: some Scene {
        WindowGroup(id: DriftMenuBarContent.windowID) {
            FleetOverviewView()
                .environmentObject(store)
                .environmentObject(monitor)
                .environmentObject(admin)
                .task { monitor.start() }
        }
        .defaultSize(width: 1080, height: 820)

        Settings {
            MusterSettingsView()
                .environmentObject(store)
        }
        .windowResizability(.contentMinSize)

        // The menubar item carries the red count,  so the owner sees how many cells
        // are red without opening the window.
        MenuBarExtra {
            DriftMenuBarContent()
                .environmentObject(store)
                .environmentObject(monitor)
                .environmentObject(admin)
        } label: {
            DriftMenuBarLabel()
                .environmentObject(monitor)
        }
    }
}

/// The app's Settings scene: the places on disk and the seat it reads Zulip as.
struct MusterSettingsView: View {
    @EnvironmentObject private var store: FleetStore
    @AppStorage(MusterSettings.repoRootDefaultsKey) private var repoRoot = ""
    @AppStorage(MusterSettings.scriptsDirectoryDefaultsKey) private var scriptsDir = ""
    @AppStorage(MusterSettings.zulipSeatDefaultsKey) private var zulipSeat = MusterSettings.defaultZulipSeat
    @AppStorage(MusterSettings.themeDefaultsKey) private var theme: ThemeChoice = .system
    @State private var pickingRepoRoot = false
    @State private var pickingScriptsDir = false

    private var seatChoices: [String] {
        var seats = store.roster
        for seat in [zulipSeat, MusterSettings.defaultZulipSeat] where !seat.isEmpty && !seats.contains(seat) {
            seats.append(seat)
        }
        return seats.isEmpty ? [MusterSettings.defaultZulipSeat] : seats
    }

    var body: some View {
        Form {
            Section("Fleet-Config Repository") {
                HStack {
                    TextField("Repository Root", text: $repoRoot)
                        .font(.system(size: 11, design: .monospaced))
                    Button("Choose\u{2026}") { pickingRepoRoot = true }
                }
                Text("Saved as \(MusterSettings.repoRootDefaultsKey), the key FleetConfigLocation reads.\u{00A0} The MUSTER_REPO_ROOT environment variable is read only when nothing is saved.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Scripts Directory") {
                HStack {
                    TextField("Scripts Directory", text: $scriptsDir)
                        .font(.system(size: 11, design: .monospaced))
                    Button("Choose\u{2026}") { pickingScriptsDir = true }
                }
                Text("Saved as \(MusterSettings.scriptsDirectoryDefaultsKey).\u{00A0} MUSTER_SCRIPTS_DIR, when set, still wins over the saved folder.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Zulip") {
                Picker("Seat To Read As", selection: $zulipSeat) {
                    ForEach(seatChoices, id: \.self) { seat in Text(seat).tag(seat) }
                }
                Text("Saved as \(MusterSettings.zulipSeatDefaultsKey).\u{00A0} MUSTER_ZULIP_SEAT, when set, still wins over the saved seat.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Appearance") {
                Picker("Theme", selection: $theme) {
                    ForEach(ThemeChoice.allCases) { choice in Text(choice.title).tag(choice) }
                }
                .pickerStyle(.segmented)
            }
        }
        .formStyle(.grouped)
        // The Settings window follows the same stored theme as the main
        // window;  without this it stayed on the system scheme while Dark
        // was chosen (AFC-592).
        .preferredColorScheme(theme.colorScheme)
        .frame(width: 520)
        .fileImporter(isPresented: $pickingRepoRoot, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result { repoRoot = url.path }
        }
        .fileImporter(isPresented: $pickingScriptsDir, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result { scriptsDir = url.path }
        }
    }
}
