import SwiftUI

@main
struct MusterApp: App {
    @StateObject private var store = FleetStore()

    init() {
        // The owner is asked once,  here,  whether this app may notify them.  An
        // apply that asked on its own path would hold its result until the
        // prompt was answered.
        SystemApplyNotifier.requestPermissionOnce()
    }

    var body: some Scene {
        WindowGroup {
            FleetOverviewView()
                .environmentObject(store)
        }
        .defaultSize(width: 1080, height: 820)
        .windowResizability(.contentMinSize)
    }
}
