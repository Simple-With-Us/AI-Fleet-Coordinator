import AppKit
import SwiftUI
import XCTest

/// A badged sidebar row still selects.
///
/// The Services row carries a badge and the rows beside it do not.  The sidebar builds every
/// row as one expression with the tag outermost, and this test holds that shape in a hosted
/// sidebar-styled list:  the row's selection must reach the binding by its tag.
///
/// It mirrors the shape of `sidebarRow`, not the app's own view, and it drives the table's
/// selection the way the list reads it, not a pointer click:  synthetic clicks did not move a
/// SwiftUI list's selection in this harness.  Before the change the badged row was built as
/// the tagged label wrapped in `.badge`, and its binding stayed nil in the same fixture.
@MainActor
final class ServicesRowSelectionTests: XCTestCase {
    private final class SelectionBox {
        var selection: String?
    }

    private struct SidebarFixture: View {
        let box: SelectionBox

        var body: some View {
            List(selection: Binding(get: { box.selection }, set: { box.selection = $0 })) {
                Section("The Mac") {
                    Label("Overview", systemImage: "square.grid.2x2").tag("overview")
                    Label("Services", systemImage: "gearshape.2")
                        .badge(Text("1"))
                        .help("1 service needs attention")
                        .accessibilityValue(Text("1 service needs attention"))
                        .tag("services")
                    Label("Compliance Grid", systemImage: "square.grid.3x3").tag("grid")
                }
            }
            .listStyle(.sidebar)
        }
    }

    private func findTable(_ view: NSView) -> NSTableView? {
        if let table = view as? NSTableView { return table }
        for sub in view.subviews {
            if let found = findTable(sub) { return found }
        }
        return nil
    }

    func test_badged_services_row_selects_by_its_tag() throws {
        let box = SelectionBox()
        let window = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 280, height: 420),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentViewController = NSHostingController(rootView: SidebarFixture(box: box))
        window.makeKeyAndOrderFront(nil)
        defer { window.orderOut(nil) }
        RunLoop.current.run(until: Date().addingTimeInterval(0.8))

        let content = try XCTUnwrap(window.contentView)
        let table = try XCTUnwrap(findTable(content))
        // The section's header, then Overview, Services and Compliance Grid.
        XCTAssertEqual(table.numberOfRows, 4)

        func select(_ row: Int) -> String? {
            box.selection = nil
            table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            table.delegate?.tableViewSelectionDidChange?(
                Notification(name: NSTableView.selectionDidChangeNotification, object: table))
            RunLoop.current.run(until: Date().addingTimeInterval(0.4))
            return box.selection
        }

        XCTAssertEqual(select(1), "overview")
        XCTAssertEqual(select(2), "services")
        XCTAssertEqual(select(3), "grid")
    }
}
