import SwiftUI

@main
struct MusterApp: App {
    @StateObject private var store = FleetStore()

    var body: some Scene {
        WindowGroup {
            FleetOverviewView()
                .environmentObject(store)
        }
        .defaultSize(width: 1080, height: 820)
        .windowResizability(.contentMinSize)
    }
}
