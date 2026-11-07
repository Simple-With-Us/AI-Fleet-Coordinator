import AppKit
import SwiftUI
import XCTest

/// The main window's layout, and the stale saved split-view frames the app forgets.
///
/// The owner's report (November 6, 2026):  both the sidebar and the detail were pushed up off
/// the window's top, under the title bar, and neither could be scrolled back to its first rows.
/// The owner's Mac had the split view's columns saved at 1085 pt for a 760-pt window, and the
/// `oversizedSplitViewFrameKeys` tests below check that the app forgets exactly such frames.
/// Those are the only tests here that fail without the fix.
///
/// The layout tests host the window root's shape (a sidebar List, a detail column with a strip
/// over a ScrollView far taller than the window) in a plain window with stand-in panels, not
/// the app's `WindowGroup` window, and with no saved split-view frames, in two layouts:
///
/// - `original`:  the root the bug was reported on, the split view in an overlay of a sized
///   `Color.clear`, with no column cap.  This is the control.
/// - `fixed`:  the root `FleetOverviewView` uses now, the split view itself sized by
///   `musterWindowSize`, each column capped by `musterColumnHeight`.
///
/// On the CI runner (macOS 26) the two lay out exactly the same and neither overflows, so the
/// layout tests check that the new root is sound (its columns stay inside the window, scroll,
/// and start below the title bar), not that it fixes the bug.  They are not a regression guard
/// for it.  Each run prints its measurements (`[window layout]` in the test log), and attaches a
/// screenshot to the test results when a run keeps a result bundle.
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
                        // Like the panels:  wrapped paragraphs whose height depends on the
                        // column's width, then more content than any screen holds.
                        VStack(alignment: .leading, spacing: 12) {
                            Text("First")
                            ForEach(0..<12, id: \.self) { _ in
                                Text(String(repeating: "A paragraph that wraps at the column's width. ", count: 12))
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Color.gray.frame(height: 1400)
                        }
                        .padding(.horizontal, 22)
                        .padding(.vertical, 18)
                        .frame(maxWidth: .infinity, alignment: .leading)
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
        return min(700, screen - 40).rounded(.down)
    }

    private func host(_ layout: Layout) throws -> (NSWindow, NSHostingController<Harness>) {
        _ = NSApplication.shared
        // The window must fit above the root's minimum plus the title bar, or the window
        // server and the minimum disagree and every measurement is off.
        guard height >= MusterWindowLayout.minHeight + 60 else {
            throw XCTSkip("The runner's screen is too short (\(height) pt) for a window above the minimum.")
        }
        let size = NSSize(width: MusterWindowLayout.minWidth, height: height)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let hosting = NSHostingController(rootView: Harness(layout: layout))
        // Chosen to match the app's window, which opens at its default size and keeps it, and
        // not verified against the scene's window:  the hosting controller enforces the root's
        // minimum and does not resize the window to its ideal.
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

    /// The sidebar's scroll view is the narrowest one.
    private func sidebarScroll(_ window: NSWindow) throws -> NSScrollView {
        let content = try XCTUnwrap(window.contentView)
        return try XCTUnwrap(scrollViews(in: content).min { $0.frame.width < $1.frame.width },
                             "no NSScrollView found for the sidebar")
    }

    /// The root frame sets the window's minimum, with a 1400-pt stand-in panel inside.  The
    /// original root passes this too, so it says nothing about the owner's bug.
    func testRootFrameSetsTheWindowMinimum() throws {
        let (window, hosting) = try host(.fixed)
        defer { window.close() }
        let content = try XCTUnwrap(window.contentView)
        XCTAssertEqual(content.bounds.height, height, accuracy: 1, "the window did not keep the size it was given")
        // The content view runs under the title bar, so the root's minimum is measured with it.
        let titleBar = content.bounds.height - window.contentLayoutRect.height
        let minimum = hosting.sizeThatFits(in: .zero)
        XCTAssertEqual(minimum.height, MusterWindowLayout.minHeight + titleBar, accuracy: 2,
                       "the root's minimum height is not the window's own")
        XCTAssertEqual(minimum.width, MusterWindowLayout.minWidth, accuracy: 1)
    }

    func testFixedColumnsStayInsideTheWindowAndScroll() throws {
        let (window, _) = try host(.fixed)
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
        // The sidebar may run under the title bar, but its first row must start below it
        // (the document's top already counts the scroll view's top inset).
        let sidebarTop = try documentTop(try sidebarScroll(window))
        XCTAssertLessThanOrEqual(sidebarTop, window.contentLayoutRect.maxY + 1,
                                 "the sidebar's first row is under the title bar")
        XCTAssertGreaterThan(sidebarTop, window.contentLayoutRect.minY)
    }

    /// Control:  the root the bug was reported on, laid out the same way.  On the CI runner it
    /// does not overflow (see the type's comment), so the overflow assertion is an expected
    /// failure, and a strict one:  if a later runner or harness does reproduce the overflow,
    /// this test fails and says so, and the harness can then guard the fix.
    func testOriginalRootControl() throws {
        let (window, _) = try host(.original)
        defer { window.close() }
        attachScreenshot(window, "original")
        // Found before the expected failure, so a harness that finds no scroll view still fails.
        let top = try documentTop(try detailScroll(window))
        XCTExpectFailure("The harness does not reproduce the owner's overflow on the CI runner.", strict: true)
        XCTAssertGreaterThan(top, window.contentLayoutRect.maxY + 1,
                             "the original root did not overflow; the harness does not reproduce the bug")
    }

    // MARK: - Stale saved split-view frames

    private let splitKey = "NSSplitView Subview Frames muster-main-AppWindow-1, SidebarNavigationSplitView"
    private let windowKey = "NSWindow Frame muster-main-AppWindow-1"

    private func columns(height: Int) -> [String] {
        ["0.000000, 0.000000, 230.000000, \(height).000000, NO, NO",
         "230.000000, 0.000000, 930.000000, \(height).000000, NO, NO"]
    }

    /// The owner's Mac on November 6:  a 760-pt window with its columns saved at 1085 pt.
    func testAColumnFrameSavedTallerThanItsWindowIsStale() {
        let saved: [String: Any] = [windowKey: "176 102 1160 760 0 0 1512 944 ", splitKey: columns(height: 1085)]
        XCTAssertEqual(MusterWindowLayout.oversizedSplitViewFrameKeys(in: saved, screenHeight: 944), [splitKey])
    }

    func testAColumnFrameThatFitsItsWindowIsKept() {
        let saved: [String: Any] = [windowKey: "176 102 1160 760 0 0 1512 944 ", splitKey: columns(height: 760)]
        XCTAssertEqual(MusterWindowLayout.oversizedSplitViewFrameKeys(in: saved, screenHeight: 944), [])
    }

    func testWithNoSavedWindowTheTallestScreenIsTheLimit() {
        XCTAssertEqual(MusterWindowLayout.oversizedSplitViewFrameKeys(
            in: [splitKey: columns(height: 1085)], screenHeight: 944), [splitKey])
        XCTAssertEqual(MusterWindowLayout.oversizedSplitViewFrameKeys(
            in: [splitKey: columns(height: 900)], screenHeight: 944), [])
        XCTAssertEqual(MusterWindowLayout.oversizedSplitViewFrameKeys(
            in: [splitKey: columns(height: 1085)], screenHeight: nil), [])
    }

    func testValuesOfAnotherShapeAreLeftAlone() {
        let saved: [String: Any] = [windowKey: "not a frame", splitKey: ["garbage"],
                                    "NSSplitView Subview Frames other": 42]
        XCTAssertEqual(MusterWindowLayout.oversizedSplitViewFrameKeys(in: saved, screenHeight: 944), [])
    }

    func testForgettingRemovesOnlyTheStaleFrame() throws {
        let suite = "MusterWindowLayoutTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        // A window name of the test's own, and `contains`, not equality:  a suite's search list
        // also reaches domains this test does not own, which the test host may write to.
        let split = "NSSplitView Subview Frames layout-test-AppWindow-1, SidebarNavigationSplitView"
        let frame = "NSWindow Frame layout-test-AppWindow-1"
        defaults.set("176 102 1160 760 0 0 1512 944 ", forKey: frame)
        defaults.set(columns(height: 1085), forKey: split)
        defaults.set("overview", forKey: "layoutTestSelection")
        XCTAssertTrue(MusterWindowLayout.forgetOversizedSplitViewFrames(in: defaults, screenHeight: 944)
            .contains(split))
        XCTAssertNil(defaults.object(forKey: split))
        XCTAssertNotNil(defaults.object(forKey: frame))
        XCTAssertEqual(defaults.string(forKey: "layoutTestSelection"), "overview")
        XCTAssertFalse(MusterWindowLayout.forgetOversizedSplitViewFrames(in: defaults, screenHeight: 944)
            .contains(split))
    }
}
