import SwiftUI

@main
struct MusterApp: App {
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
