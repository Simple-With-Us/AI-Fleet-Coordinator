import AppKit
import SwiftUI
import XCTest

/// The main window's layout:  a panel taller than the window scrolls inside its column, and the
/// tops of both columns stay reachable.
///
/// The owner's report (November 6, 2026):  both the sidebar and the detail were pushed up off
/// the window's top, under the title bar, and neither could be scrolled back to its first rows.
/// The harness hosts the window root's shape (a sidebar List, a detail column with a strip
/// over a ScrollView far taller than the window) in a real window, once with the column cap
/// (`musterColumnHeight`) and once without it.  The uncapped run is the control:  it must show
/// the overflow, or the capped run proves nothing.
@MainActor
final class MusterWindowLayoutTests: XCTestCase {
    private let size = NSSize(width: 1160, height: 700)

    private struct Cap: ViewModifier {
        let on: Bool
        @ViewBuilder func body(content: Content) -> some View {
            if on { content.musterColumnHeight() } else { content }
        }
    }

    /// The window root's shape:  a sidebar List and a detail column with a strip above a
    /// ScrollView far taller than the window.
    private struct Harness: View {
        let capped: Bool
        var body: some View {
            NavigationSplitView {
                List { ForEach(0..<20, id: \.self) { Text("Row \($0)") } }
                    .modifier(Cap(on: capped))
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
                .navigationTitle("Harness")
            }
            .musterWindowSize()
        }
    }

    private func host(capped: Bool) -> (NSWindow, NSHostingController<Harness>) {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let hosting = NSHostingController(rootView: Harness(capped: capped))
        window.contentViewController = hosting
        window.setContentSize(size)
        window.orderFrontRegardless()
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        window.layoutIfNeeded()
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

    func testWindowTakesItsOwnMinimumNotAPanelsHeight() {
        let (window, hosting) = host(capped: true)
        defer { window.close() }
        XCTAssertEqual(window.contentView?.bounds.height ?? 0, size.height, accuracy: 1)
        // The root's own minimum, whatever the panels inside it want.
        let minimum = hosting.sizeThatFits(in: .zero)
        XCTAssertEqual(minimum.height, MusterWindowLayout.minHeight, accuracy: 1)
        XCTAssertEqual(minimum.width, MusterWindowLayout.minWidth, accuracy: 1)
        // A plain window may report no minimum of its own;  when it reports one, it is the root's.
        if window.contentMinSize.height > 0 {
            XCTAssertLessThanOrEqual(window.contentMinSize.height, MusterWindowLayout.minHeight + 1)
        }
    }

    func testCappedColumnsStayInsideTheWindowAndScroll() throws {
        let (window, _) = host(capped: true)
        defer { window.close() }
        attachScreenshot(window, "capped")
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

    /// Control:  without the cap, the harness must show the reported bug.  If this fails, the
    /// harness does not reproduce the bug and the test above proves nothing.
    func testUncappedColumnsReproduceTheOverflow() throws {
        let (window, _) = host(capped: false)
        defer { window.close() }
        attachScreenshot(window, "uncapped")
        let top = try documentTop(try detailScroll(window))
        XCTAssertGreaterThan(top, window.contentLayoutRect.maxY + 1,
                             "uncapped detail did not overflow; the root cause is not what the fix assumes")
    }
}
