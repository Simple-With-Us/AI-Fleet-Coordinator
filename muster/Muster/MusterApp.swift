import SwiftUI

@main
struct MusterApp: App {
    var body: some Scene {
        WindowGroup {
            FleetOverviewView()
                .preferredColorScheme(.light)
        }
        .defaultSize(width: 900, height: 620)
        .windowResizability(.contentMinSize)
    }
}
