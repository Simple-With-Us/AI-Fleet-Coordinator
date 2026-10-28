import SwiftUI

/// One action the owner can take on a fleet service,  with a title the menu can show.
struct FleetAdminAction: Identifiable {
    /// The action itself.
    let action: ManagedServiceAction

    var id: String { title }

    /// The title the menu and the panel show.
    var title: String {
        switch action {
        case .start: return "Start"
        case .stop: return "Stop"
        case .restart: return "Restart"
        case .none: return "No Action Is Available"
        }
    }
}

/// The actions that make sense for one service's state.
///
/// The list is the service's own:  a service that is running can be stopped and
/// restarted,  a service that is stopped can be started,  and a service that is not
/// installed has no action at all.
func fleetAdminActions(for entry: FleetServiceEntry) -> [FleetAdminAction] {
    entry.availableActions
        .filter { $0 != .none }
        .map { FleetAdminAction(action: $0) }
}

/// The fleet admin in the menubar:  the fleet's own services,  and what the owner can
/// do about them without opening the window.
///
/// The menu names every service with its state,  and the actions that make sense for
/// that state are one click away.  A service that is busy says so,  and a refusal is
/// shown in the owner's words rather than in a code.
///
/// This is the control the standalone BotFleet Control app used to carry,  moved into
/// Muster:  the app is about the fleet now,  and not about BotFleet alone.
struct FleetAdminMenu: View {
    @EnvironmentObject private var admin: FleetAdmin

    var body: some View {
        Divider()
        Text("Fleet Admin")
        if admin.reading {
            Text("Reading the fleet services…")
        }
        if let problem = admin.problem {
            Text(problem)
        }
        if admin.entries.isEmpty {
            Text("No fleet service has been read yet.")
        } else {
            ForEach(admin.entries, id: \.id) { entry in
                Menu {
                    ForEach(fleetAdminActions(for: entry)) { action in
                        Button(action.title) {
                            Task { await admin.perform(action.action, serviceID: entry.id) }
                        }
                        .disabled(admin.busyServiceID != nil)
                    }
                } label: {
                    Text("\(entry.title):  \(entry.summary)")
                }
            }
        }
        Toggle("Launch Muster At Login", isOn: Binding(
            get: { admin.launchesAtLogin },
            set: { admin.setLaunchesAtLogin($0) }
        ))
    }
}
