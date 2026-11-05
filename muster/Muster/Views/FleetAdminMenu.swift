import AppKit
import SwiftUI

/// One action the owner can take on a fleet service,  with a title the panel can show.
struct FleetAdminAction: Identifiable {
    /// The action itself.
    let action: ManagedServiceAction

    var id: String { title }

    /// The title the panel shows.  A panel row sits inside one place,  so it does not repeat
    /// the place on the button.  The menu's titles come from `MenuAction`,  which does.
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

/// Asks the owner to confirm an action on the direct route,  with the service's own warning.
///
/// A stop takes a service down at once,  and for BotFleet Server its Local VMs with it,  so
/// the warning is shown before the action and not after.  The alert names the machine,  and
/// Return means Cancel:  it activates the app and could otherwise catch a Return meant for
/// whatever the owner was typing in.
@MainActor
func confirmFleetAction(_ request: ServiceActionRouter.ConfirmRequest) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "\(request.word) \(request.title) \(request.host.onTitle)?"
    alert.informativeText = request.message
    let run = alert.addButton(withTitle: "\(request.word) \(request.host.onTitle)")
    run.keyEquivalent = ""
    run.hasDestructiveAction = true
    let cancel = alert.addButton(withTitle: "Cancel")
    cancel.keyEquivalent = "\r"
    NSApp.activate(ignoringOtherApps: true)
    return alert.runModal() == .alertFirstButtonReturn
}
