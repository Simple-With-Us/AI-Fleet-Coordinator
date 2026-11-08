import AppKit
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
        // Before the window opens:  forget any split-view column frame saved taller than its
        // window.  Older builds most likely saved such frames while the window overflowed
        // (AFC-616);  they were most likely a consequence, not the cause, and this only
        // clears them
        // (MusterWindowLayout.oversizedSplitViewFrameKeys).
        MusterWindowLayout.forgetOversizedSplitViewFrames(
            in: .standard, screenHeight: NSScreen.screens.map(\.frame.height).max())
        // The owner is asked once,  here,  whether this app may notify them.  An
        // apply that asked on its own path would hold its result until the
        // prompt was answered.
        SystemApplyNotifier.requestPermissionOnce()
        // The Seats panel reads Zulip only as the Operator (AFC-654):  a seat saved by an
        // older build is not read,  and is removed so it cannot look like a choice.
        MusterSettings.removeRetiredZulipSeat()
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
            // The seat counts are from before the sleep,  and the menu reads them as not known
            // once they are older than their window.  The engine runs again once the network has
            // had a moment to come back,  so the counts are current again without waiting for
            // the safety net.  A run in flight is shared,  never doubled.
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: MusterWake.engineDelayNanoseconds)
                if !store.isRunning { store.refresh() }
            }
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
        .defaultSize(width: 1160, height: 760)
        // The window may be as small as the root's minimum and no smaller.  This is a
        // WindowGroup's default, written out so it is visible.  The root's own min/max frame
        // (musterWindowSize) sets that minimum, whatever the panel inside it asks for.
        .windowResizability(.contentMinSize)
        // One window:  the menu brings it forward,  and a second copy would show every
        // plan sheet twice.
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .appInfo) { CheckForUpdatesButton() }
        }

        // Settings keeps Apple's default, `.contentSize`, so the window should be the size of
        // its fixed-size panes and not resizable (not measured on a real Mac;  a source
        // guard in MusterSettingsLayoutTests keeps this scene free of `windowResizability`).
        Settings {
            MusterSettingsView()
                .environmentObject(store)
        }

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

/// What the app does when the Mac wakes,  beyond `PlaceWatch`'s own reads.
enum MusterWake {
    /// How long after a wake the engine runs again:  long enough for the network to return
    /// for the checks that read the secret store,  short enough that the counts are not left
    /// not known for long.
    static let engineDelayNanoseconds: UInt64 = 10_000_000_000
}

/// The app's Settings scene:  four panes of one fixed size,  so the window (which Settings
/// sizes to its content) fits a short screen and nothing hides below an unseen scroller.
/// Zulip has no seat to choose:  the Seats panel reads only as the Operator (AFC-654).
struct MusterSettingsView: View {
    @AppStorage(MusterSettings.themeDefaultsKey) private var theme: ThemeChoice = .system
    @AppStorage(MusterSettings.shortMenuBarTextDefaultsKey) private var shortMenuBarText = false
    @AppStorage(MusterSettings.keepServicesRunningDefaultsKey) private var keepServicesRunning = true

    var body: some View {
        TabView {
            MusterFoldersTab()
                .tabItem { Label("Folders", systemImage: "folder") }
            MusterZulipServicesPane(keepServicesRunning: $keepServicesRunning)
                .tabItem { Label("Zulip And Services", systemImage: "bubble.left.and.text.bubble.right") }
            MusterAppearancePane(theme: $theme, shortMenuBarText: $shortMenuBarText)
                .tabItem { Label("Appearance", systemImage: "paintbrush") }
            MusterSettingsPane { BuildIdentitySettingsSection() }
                .tabItem { Label("This Build", systemImage: "info.circle") }
        }
        // The Settings window follows the same stored theme as the main window (AFC-592).
        .preferredColorScheme(theme.colorScheme)
    }
}

/// The Folders tab.  It is its own view, with no store, so a store refresh never runs the
/// folder checks again:  they run when the tab is built and when a folder setting changes.
/// The checks are a few file-existence stats on the local disk.
struct MusterFoldersTab: View {
    @AppStorage(MusterSettings.repoRootDefaultsKey) private var repoRoot = ""
    @AppStorage(MusterSettings.scriptsDirectoryDefaultsKey) private var scriptsDir = ""

    var body: some View {
        // Read here so the tab is rebuilt when a folder is chosen or reset:  the resolvers
        // below read the same defaults these properties write.
        let _ = (repoRoot, scriptsDir)
        MusterFoldersPane(
            checkout: SettingsCopy.checkoutRow(FleetConfigLocation.resolveWithSource(),
                                               engineRoot: EngineLocator.locate()?.root),
            scripts: SettingsCopy.scriptsRow(FleetViewService.resolveScripts()),
            choose: { folder, path in
                switch folder {
                case .checkout: repoRoot = path
                case .scripts: scriptsDir = path
                }
            },
            useDefault: { folder in
                switch folder {
                case .checkout: repoRoot = ""
                case .scripts: scriptsDir = ""
                }
            })
    }
}
