import SwiftUI

@main
struct MusterApp: App {
    var body: some Scene {
        WindowGroup {
            TabView {
                FleetOverviewView()
                    .tabItem { Label("Overview", systemImage: "square.stack.3d.up") }
                FleetView()
                    .tabItem { Label("Fleet", systemImage: "list.bullet.rectangle") }
            }
            .preferredColorScheme(.light)
        }
        .defaultSize(width: 900, height: 620)
        .windowResizability(.contentMinSize)
    }
}
