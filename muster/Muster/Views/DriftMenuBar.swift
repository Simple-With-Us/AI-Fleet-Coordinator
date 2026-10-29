import SwiftUI

/// The menubar item:  the red count,  so the owner can see it without opening the
/// window.
///
/// The colour is the app's own:  a count above zero is red,  because a red count
/// is a real failure,  and no other colour is introduced here.
struct DriftMenuBarLabel: View {
    @EnvironmentObject private var monitor: DriftMonitor

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: "shield.lefthalf.filled")
            Text(countText)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
        }
        .foregroundStyle(isRed ? Color.red : Color.secondary)
    }

    /// The count,  or a dash when the app does not know it.
    private var countText: String {
        guard let count = monitor.redCount else { return "—" }
        return "\(count)"
    }

    /// Whether the count is a failure.
    private var isRed: Bool {
        (monitor.redCount ?? 0) > 0
    }
}

/// The menu behind the menubar item.
///
/// It says how many cells are red,  which ones they are,  what turned red most
/// recently and what the owner can do about it.
struct DriftMenuBarContent: View {
    @EnvironmentObject private var monitor: DriftMonitor
    @EnvironmentObject private var store: FleetStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text(headline)
        if !monitor.redCells.isEmpty {
            Divider()
            ForEach(monitor.redCells.prefix(8)) { cell in
                Text("\(cell.seat) / \(cell.kindTitle):  \(cell.ruleID ?? cell.state.shortLabel)")
            }
            if monitor.redCells.count > 8 {
                Text("\(monitor.redCells.count - 8) more red cell(s)")
            }
        }
        if !monitor.transitions.isEmpty {
            Divider()
            Text("Turned red most recently")
            ForEach(monitor.transitions.prefix(3)) { transition in
                Text("\(transition.timeText)  \(transition.seat) / \(transition.kindTitle)")
            }
        }
        FleetAdminMenu()
        Divider()
        Button("Refresh Now") {
            store.refresh()
        }
        .disabled(store.phase == .running)
        Button("Open Muster") {
            openWindow(id: DriftMenuBarContent.windowID)
        }
    }

    /// The window's identifier,  so the menu can open the window that is already
    /// open instead of a second copy of it.
    static let windowID = "muster-main"

    /// The first line of the menu.
    private var headline: String {
        guard let count = monitor.redCount else {
            return "The last run could not be trusted."
        }
        if count == 0 { return "No red cells." }
        return "\(count) red cell(s)."
    }
}
