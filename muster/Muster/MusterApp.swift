import SwiftUI

@main
struct MusterApp: App {
    @NSApplicationDelegateAdaptor(AppIconFollower.self) private var appIconFollower
    @StateObject private var store: FleetStore
    @StateObject private var monitor: DriftMonitor
    @StateObject private var admin: FleetAdmin
    @StateObject private var router: ServiceActionRouter
    @StateObject private var reconciler: ServiceReconciler
    @StateObject private var watch: PlaceWatch

    @MainActor
    init() {
        // The owner is asked once,  here,  whether this app may notify them.  An
        // apply that asked on its own path would hold its result until the
        // prompt was answered.
        SystemApplyNotifier.requestPermissionOnce()
        let store = FleetStore()
        // The monitor starts here and not with a window:  the menu bar item has to know the
        // Mac before any window has appeared,  and the store's first run is still in flight
        // at this point,  so the monitor hears it land.
        let monitor = DriftMonitor(grid: store)
        monitor.start()
        // The owner's stops are kept in a file,  so Muster never starts a service the owner
        // stopped,  even after Muster quits and comes back.
        let admin = FleetAdmin(ownerStopStore: OwnerStopFile())
        _store = StateObject(wrappedValue: store)
        _monitor = StateObject(wrappedValue: monitor)
        _admin = StateObject(wrappedValue: admin)
        let router = ServiceActionRouter(admin: admin, confirm: { request in
            confirmFleetAction(request)
        })
        _router = StateObject(wrappedValue: router)
        // Updates arrive through Sparkle,  and the install waits while any action runs.
        AppUpdater.shared.start(router: router, admin: admin)
        // Muster starts the Mac's services again when they stop,  only while it runs,  and the
        // setting above turns it off.
        let reconciler = ServiceReconciler.live(admin: admin, router: router, controller: OpsController.shared)
        _reconciler = StateObject(wrappedValue: reconciler)
        _watch = StateObject(wrappedValue: PlaceWatch(onWake: {
            Task { await admin.refresh() }
        }))
    }

    var body: some Scene {
        WindowGroup(id: DriftMenuBarContent.windowID) {
            FleetOverviewView()
                .environmentObject(store)
                .environmentObject(monitor)
                .environmentObject(admin)
                .environmentObject(router)
                // The window reads the same places the menu does,  so the two cannot disagree.
                .environmentObject(watch)
        }
        .defaultSize(width: 1080, height: 820)
        // One window:  the menu brings it forward,  and a second copy would show every
        // plan sheet twice.
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .appInfo) { CheckForUpdatesButton() }
        }

        Settings {
            MusterSettingsView()
                .environmentObject(store)
        }
        .windowResizability(.contentMinSize)

        // The menu bar item names both places,  so the owner sees what needs attention and
        // where without opening the window.
        MenuBarExtra {
            DriftMenuBarContent()
                .environmentObject(store)
                .environmentObject(monitor)
                .environmentObject(admin)
                .environmentObject(watch)
                .environmentObject(router)
        } label: {
            DriftMenuBarLabel()
                .environmentObject(monitor)
                .environmentObject(admin)
                .environmentObject(watch)
                .environmentObject(router)
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
    @AppStorage(MusterSettings.shortMenuBarTextDefaultsKey) private var shortMenuBarText = false
    @AppStorage(MusterSettings.keepServicesRunningDefaultsKey) private var keepServicesRunning = true
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
            Section("Services") {
                Toggle("Keep Services Running", isOn: $keepServicesRunning)
                Text("While Muster runs, it starts a service on the Mac again when it stops or fails, waits longer after each try, and stops trying after 3.\u{00A0} It never stops a service, and it never starts one you stopped from Muster.\u{00A0} The Services panel says what it does for each service.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Appearance") {
                Picker("Theme", selection: $theme) {
                    ForEach(ThemeChoice.allCases) { choice in Text(choice.title).tag(choice) }
                }
                .pickerStyle(.segmented)
                Toggle("Short Menu Bar Text", isOn: $shortMenuBarText)
                Text("Reads M OK \u{00B7} S ? in place of Mac OK \u{00B7} Server ?, for a crowded menu bar.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            BuildIdentitySettingsSection()
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
