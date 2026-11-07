import AppKit
import SwiftUI
import XCTest

/// The main window's layout:  a panel taller than the window scrolls inside its column, and the
/// tops of both columns stay reachable.
///
/// The owner's report (November 6, 2026):  both the sidebar and the detail were pushed up off
/// the window's top, under the title bar, and neither could be scrolled back to its first rows.
/// The harness hosts the window root's shape (a sidebar List, a detail column with a strip
/// over a ScrollView far taller than the window) in a real window, in two layouts:
///
/// - `original`:  the root the bug was reported on, the split view in an overlay of a sized
///   `Color.clear`, with no column cap.  This is the control:  it must show the overflow, or
///   the fixed run proves nothing.
/// - `fixed`:  the root `FleetOverviewView` uses now, the split view itself sized by
///   `musterWindowSize`, each column capped by `musterColumnHeight`.
///
/// Each run prints its measurements (`[window layout]` in the test log), and attaches a
/// screenshot to the result bundle, which CI uploads.
@MainActor
final class MusterWindowLayoutTests: XCTestCase {
    private enum Layout: String {
        case original
        case fixed
    }

    /// The window root's shape:  a sidebar List and a detail column with a strip above a
    /// ScrollView far taller than the window.
    private struct Harness: View {
        let layout: Layout

        var body: some View {
            switch layout {
            case .original:
                // FleetOverviewView's root before this fix, frame for frame.
                Color.clear
                    .frame(minWidth: MusterWindowLayout.minWidth, idealWidth: MusterWindowLayout.minWidth,
                           maxWidth: .infinity,
                           minHeight: MusterWindowLayout.minHeight, idealHeight: MusterWindowLayout.idealHeight,
                           maxHeight: .infinity)
                    .overlay { split(capped: false) }
            case .fixed:
                split(capped: true)
                    .musterWindowSize()
            }
        }

        private func split(capped: Bool) -> some View {
            NavigationSplitView {
                List {
                    Section { Text("Overview") }
                    Section("The Mac") { ForEach(0..<20, id: \.self) { Text("Row \($0)") } }
                }
                .modifier(Cap(on: capped))
                .navigationTitle("Harness")
                .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 300)
            } detail: {
                VStack(spacing: 0) {
                    Text("Strip").frame(height: 33)
                    Divider()
                    ScrollView {
                        VStack(spacing: 0) {
                            Text("First")
                            Color.gray.frame(height: 2000)
                        }.frame(maxWidth: .infinity)
                    }
                }
                .modifier(Cap(on: capped))
                .navigationTitle("Seat Setup")
                .navigationSubtitle("On The Mac")
                .toolbar {
                    ToolbarItem {
                        Button {} label: { Label("Check", systemImage: "checkmark.shield") }
                    }
                }
            }
        }
    }

    private struct Cap: ViewModifier {
        let on: Bool
        @ViewBuilder func body(content: Content) -> some View {
            if on { content.musterColumnHeight() } else { content }
        }
    }

    /// A window height that fits the runner's screen, so the window server does not shrink it.
    private var height: CGFloat {
        let screen = NSScreen.main?.visibleFrame.height ?? 700
        return max(MusterWindowLayout.minHeight + 60, min(700, screen - 40)).rounded(.down)
    }

    private func host(_ layout: Layout) -> (NSWindow, NSHostingController<Harness>) {
        _ = NSApplication.shared
        let size = NSSize(width: MusterWindowLayout.minWidth, height: height)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let hosting = NSHostingController(rootView: Harness(layout: layout))
        // Like the app's window, which opens at its default size and keeps it:  the hosting
        // controller enforces the root's minimum and does not resize the window to its ideal.
        hosting.sizingOptions = [.minSize]
        window.contentViewController = hosting
        window.setContentSize(size)
        window.orderFrontRegardless()
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        window.layoutIfNeeded()
        report(layout, window)
        return (window, hosting)
    }

    private func scrollViews(in view: NSView) -> [NSScrollView] {
        var found: [NSScrollView] = []
        for sub in view.subviews {
            if let sv = sub as? NSScrollView, !sv.isHidden { found.append(sv) }
            found += scrollViews(in: sub)
        }
        return found
    }

    /// The tallest document is the detail's ScrollView.
    private func detailScroll(_ window: NSWindow) throws -> NSScrollView {
        let all = scrollViews(in: try XCTUnwrap(window.contentView))
        return try XCTUnwrap(all.max { ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0) },
                             "no NSScrollView found; the harness no longer sees SwiftUI's scroll view")
    }

    /// Where the top of a document sits, in window coordinates.
    private func documentTop(_ sv: NSScrollView) throws -> CGFloat {
        let doc = try XCTUnwrap(sv.documentView)
        return doc.convert(NSPoint(x: 0, y: doc.isFlipped ? 0 : doc.bounds.maxY), to: nil).y
    }

    /// The measurements behind every assertion, so a failed run says what it saw.
    private func report(_ layout: Layout, _ window: NSWindow) {
        guard let content = window.contentView else { return }
        var lines = ["[window layout] \(layout.rawValue): content \(content.bounds.size), "
                     + "layout rect \(window.contentLayoutRect), min \(window.contentMinSize)"]
        for sv in scrollViews(in: content) {
            let frame = sv.convert(sv.bounds, to: content)
            let top = (try? documentTop(sv)).map { "\($0)" } ?? "none"
            lines.append("[window layout] \(layout.rawValue): scroll view \(frame), "
                         + "document \(sv.documentView?.frame.size ?? .zero), document top \(top), "
                         + "insets \(sv.contentInsets.top)")
        }
        print(lines.joined(separator: "\n"))
    }

    private func attachScreenshot(_ window: NSWindow, _ name: String) {
        guard let view = window.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return }
        let attachment = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testWindowTakesItsOwnMinimumNotAPanelsHeight() throws {
        let (window, hosting) = host(.fixed)
        defer { window.close() }
        let content = try XCTUnwrap(window.contentView)
        XCTAssertEqual(content.bounds.height, height, accuracy: 1, "the window did not keep the size it was given")
        // The content view runs under the title bar, so the root's minimum is measured with it.
        let titleBar = content.bounds.height - window.contentLayoutRect.height
        let minimum = hosting.sizeThatFits(in: .zero)
        XCTAssertEqual(minimum.height, MusterWindowLayout.minHeight + titleBar, accuracy: 2,
                       "the root's minimum height is not the window's own, whatever the panel's height")
        XCTAssertEqual(minimum.width, MusterWindowLayout.minWidth, accuracy: 1)
    }

    func testFixedColumnsStayInsideTheWindowAndScroll() throws {
        let (window, _) = host(.fixed)
        defer { window.close() }
        attachScreenshot(window, "fixed")
        let content = try XCTUnwrap(window.contentView)
        for sv in scrollViews(in: content) {
            let frame = sv.convert(sv.bounds, to: content)
            XCTAssertGreaterThanOrEqual(frame.minY, -1, "a column spills past the window's bottom")
            XCTAssertLessThanOrEqual(frame.maxY, content.bounds.maxY + 1, "a column spills past the window's top")
        }
        let detail = try detailScroll(window)
        XCTAssertGreaterThan(try XCTUnwrap(detail.documentView).frame.height,
                             detail.contentView.bounds.height + 100, "the detail cannot scroll")
        let top = try documentTop(detail)
        XCTAssertLessThanOrEqual(top, window.contentLayoutRect.maxY + 1, "the panel's top is under the title bar")
        XCTAssertGreaterThan(top, window.contentLayoutRect.minY)
    }

    /// Control:  the root the bug was reported on must show it.  If this fails, the harness does
    /// not reproduce the bug and the test above proves nothing.
    func testOriginalRootReproducesTheOverflow() throws {
        let (window, _) = host(.original)
        defer { window.close() }
        attachScreenshot(window, "original")
        let top = try documentTop(try detailScroll(window))
        XCTAssertGreaterThan(top, window.contentLayoutRect.maxY + 1,
                             "the original root did not overflow; the harness does not reproduce the bug")
    }
}
