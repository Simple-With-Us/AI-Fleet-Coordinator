import SwiftUI

/// The menu bar item:  both places,  in words,  so the owner can see them without opening the
/// window.
///
/// Plain weight and no colour.  Menu bar labels are generally drawn as template images,  so
/// colour may never appear,  and the words already say everything.
struct DriftMenuBarLabel: View {
    @EnvironmentObject private var monitor: DriftMonitor
    @EnvironmentObject private var admin: FleetAdmin
    @EnvironmentObject private var watch: PlaceWatch
    @EnvironmentObject private var router: ServiceActionRouter
    @AppStorage(MusterSettings.shortMenuBarTextDefaultsKey) private var shortText = false

    var body: some View {
        let menu = MachineMenu(MachineMenuInputs(admin: admin, monitor: monitor, watch: watch, router: router))
        HStack(spacing: 3) {
            Image(systemName: menu.barSymbol)
            Text(menu.barTitle(short: shortText))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(menu.accessibilityLabel)
    }
}

/// The menu behind the menu bar item:  The Mac and The Server,  with everything under the place
/// it runs on.
///
/// The words all come from `MachineMenu`,  and this only draws them,  so what the menu says is
/// tested without a view.
struct DriftMenuBarContent: View {
    @EnvironmentObject private var monitor: DriftMonitor
    @EnvironmentObject private var store: FleetStore
    @EnvironmentObject private var admin: FleetAdmin
    @EnvironmentObject private var watch: PlaceWatch
    @EnvironmentObject private var router: ServiceActionRouter
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let menu = MachineMenu(MachineMenuInputs(admin: admin, monitor: monitor, watch: watch, router: router))
        Section(FleetHost.mac.heading) {
            items(menu.mac.items)
        }
        Section(FleetHost.server.heading) {
            items(menu.server.items)
        }
        Section {
            Button("Open Muster") { perform(.openMuster) }
            Toggle("Launch Muster At Login", isOn: Binding(
                get: { admin.launchesAtLogin },
                set: { admin.setLaunchesAtLogin($0) }
            ))
            // The toggle goes back on its own when this fails,  so the reason has to be said.
            if let problem = admin.loginItemProblem {
                Button(MachineMenuCopy.oneRow(problem)) { perform(.openServices) }
            }
        }
    }

    /// The window's identifier.  `MusterWindow` opens it only when no window is on screen,
    /// because `openWindow` on a `WindowGroup` opens another copy each time.
    static let windowID = "muster-main"

    private func items(_ items: [MenuItem]) -> some View {
        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
            MachineMenuItemView(item: item, disabled: router.isBusy, start: start, perform: perform)
        }
    }

    /// Starts an action.  A plan needs a window for its sheet,  so one comes forward.
    private func start(_ action: MenuAction) {
        if router.start(action) == .planOpened {
            MusterWindow.show(openWindow: openWindow)
        }
    }

    private func perform(_ command: MenuCommand) {
        switch command {
        case .checkMac:
            if store.phase != .running { store.refresh() }
            router.reloadAllowlist()
            watch.readNow()
            Task { await admin.refresh() }
        case .openServerStatus:
            MusterWindow.show(.serverStatus, openWindow: openWindow)
        case .openServices:
            MusterWindow.show(.services, openWindow: openWindow)
        case .openComplianceGrid:
            MusterWindow.show(.complianceGrid, openWindow: openWindow)
        case .openMuster:
            MusterWindow.show(openWindow: openWindow)
        case .askServer:
            watch.askServer()
        case .fetchFromServer:
            let fetch = OpsAction.macAgent(verb: .restart, label: OpsNames.fetchAgentLabel)
            if router.plan(fetch) == .planOpened {
                MusterWindow.show(openWindow: openWindow)
            }
        }
    }
}

/// One item of the menu.  A submenu holds more of these,  so this view holds itself.
private struct MachineMenuItemView: View {
    let item: MenuItem
    let disabled: Bool
    let start: (MenuAction) -> Void
    let perform: (MenuCommand) -> Void

    var body: some View {
        switch item {
        case .line(let text):
            Text(text)
        case .row(let row):
            if row.actions.isEmpty {
                Text(row.title)
            } else {
                Menu(row.title) {
                    ForEach(row.actions, id: \.title) { action in
                        Button(action.title) { start(action) }
                            .disabled(disabled)
                    }
                }
            }
        case .group(let title, let items):
            Menu(title) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, child in
                    MachineMenuItemView(item: child, disabled: disabled, start: start, perform: perform)
                }
            }
        case .command(let title, let command):
            // A fetch is an action on the Mac through the sheet,  so it waits like the rest.
            Button(title) { perform(command) }
                .disabled(disabled && command == .fetchFromServer)
        case .action(let action):
            Button(action.title) { start(action) }
                .disabled(disabled)
        }
    }
}
