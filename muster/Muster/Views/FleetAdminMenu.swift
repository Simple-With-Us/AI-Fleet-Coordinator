import AppKit
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
        Button("Refresh Fleet Services") {
            Task { await admin.refresh() }
        }
        .disabled(admin.reading)
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
                            if action.action == .stop,
                               !confirmFleetStop(title: entry.title,
                                                 message: admin.stopConfirmation(for: entry.id)) {
                                return
                            }
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

/// Asks the owner to confirm a stop,  with the service's own warning.
///
/// A stop takes a service down at once,  and for the BotFleet Server its Local VMs
/// with it,  so the warning is shown before the stop and not after.
@MainActor
func confirmFleetStop(title: String, message: String) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "Stop \(title)?"
    alert.informativeText = message
    alert.addButton(withTitle: "Stop")
    alert.addButton(withTitle: "Cancel")
    NSApp.activate(ignoringOtherApps: true)
    return alert.runModal() == .alertFirstButtonReturn
}
