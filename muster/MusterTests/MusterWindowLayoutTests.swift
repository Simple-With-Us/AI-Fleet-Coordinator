import AppKit
import SwiftUI
import XCTest

/// The main window's layout:  a regression test for the owner's November 6, 2026 report
/// (AFC-616), and the stale saved split-view frames the app forgets.
///
/// The report:  both columns came up shifted up by about 100 to 140 pt, their first rows under
/// the title bar, where no scrolling reached.  PR 1442 (an experiment on the CI runner, never
/// merged) found the trigger:  wrapping text with a vertical `fixedSize` outside the panel's
/// ScrollView, such as the place strip's source line.  Measured at a zero-width proposal it
/// wraps about one character per line, so the strip's minimum height was 1032 pt for Seat
/// Setup's line.  The split view took that minimum (1085 pt in all), and the old root centred
/// it over the window, so its top ran above the title bar.  The column cap
/// (`musterColumnHeight`) keeps each column's minimum at zero, and that is the fix.  The
/// top-aligned root alone is not:  without the cap the split view still runs off the bottom.
///
/// The harness hosts a copy of the window's root in a plain window at the app's default size
/// (1160 x 760), with stand-in panels and the strip as it was before this fix.  It is not the
/// app's `WindowGroup` scene, which no test here runs.  Three roots:
///
/// - `original`:  the root the bug was reported on, the split view in an overlay of a sized
///   `Color.clear`, with no column cap.  It must overflow:  this is the control that shows the
///   harness reproduces the bug.
/// - `production`:  the root `FleetOverviewView` uses, `musterWindowSize` with each column
///   capped by `musterColumnHeight`.  It must keep both columns inside the window.
/// - `uncapped`:  `musterWindowSize` without the cap.  It must overflow off the bottom, which
///   shows the cap is load-bearing.
///
/// Each run prints its measurements (`[window layout]` in the test log).
@MainActor
final class MusterWindowLayoutTests: XCTestCase {
    private enum Root: String {
        case original
        case production
        case uncapped
    }

    private enum Strip: String {
        /// The strip before this fix:  the bug's trigger.
        case old
        /// The app's own `PlaceStrip` (`Views/PlaceBadge.swift`, compiled into this bundle).
        case real
    }

    /// The app's default window size (`MusterApp`, `.defaultSize`).
    private static let windowSize = NSSize(width: 1160, height: 760)

    /// The panel the owner reported on, with its real source line.
    private let destination = MusterDestination.seatDetail

    /// `PlaceStrip` as it was before this fix, frame for frame.  It keeps the vertical
    /// `fixedSize` on purpose, because it is the bug fixture;  the app's strip now holds its
    /// line to three lines instead.
    private struct OldPlaceStrip: View {
        let place: FleetPlace
        let source: String

        var body: some View {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                PlaceBadge(place: place)
                Text(source)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 8)
            .accessibilityElement(children: .combine)
        }
    }

    private struct Sidebar: View {
        var body: some View {
            List {
                Section {
                    Label("Overview", systemImage: "square.grid.2x2")
                }
                Section("The Mac") {
                    ForEach(1..<10, id: \.self) { index in
                        Label("Mac Place \(index)", systemImage: "desktopcomputer")
                    }
                }
                Section("The Server") {
                    ForEach(10..<15, id: \.self) { index in
                        Label("Server Place \(index)", systemImage: "server.rack")
                    }
                }
            }
            .listStyle(.sidebar)
        }
    }

    /// The detail column's shape:  the strip, then the panel's ScrollView, far taller than
    /// the window.
    private struct Detail: View {
        let strip: Strip
        let place: FleetPlace
        let source: String

        var body: some View {
            VStack(spacing: 0) {
                switch strip {
                case .old:
                    OldPlaceStrip(place: place, source: source)
                case .real:
                    PlaceStrip(place: place, source: source)
                }
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("First")
                        Color.gray.frame(height: 2000)
                    }
                    .padding(.horizontal, 22)
                    .padding(.vertical, 18)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private struct Harness: View {
        let root: Root
        let strip: Strip
        let place: FleetPlace
        let source: String

        var body: some View {
            switch root {
            case .original:
                // FleetOverviewView's root before PR 1433, frame for frame.
                Color.clear
                    .frame(minWidth: MusterWindowLayout.minWidth, idealWidth: MusterWindowLayout.minWidth,
                           maxWidth: .infinity,
                           minHeight: MusterWindowLayout.minHeight, idealHeight: MusterWindowLayout.idealHeight,
                           maxHeight: .infinity)
                    .overlay { split(capped: false) }
            case .production:
                split(capped: true)
                    .musterWindowSize()
            case .uncapped:
                split(capped: false)
                    .musterWindowSize()
            }
        }

        private func split(capped: Bool) -> some View {
            NavigationSplitView {
                Sidebar()
                    .modifier(Cap(on: capped))
                    .navigationTitle("Muster")
                    .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 300)
            } detail: {
                Detail(strip: strip, place: place, source: source)
                    .modifier(Cap(on: capped))
                    .navigationTitle("Seat Setup")
                    .navigationSubtitle("On The Mac")
                    .toolbar {
                        ToolbarItem {
                            Button {} label: { Label("Check Seat Setup", systemImage: "checkmark.shield") }
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

    /// A window the window server does not shrink to the runner's screen (1024 x 768, with
    /// 674 pt visible), so it keeps the app's 760-pt default height.
    final class UnconstrainedWindow: NSWindow {
        override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
            frameRect
        }
    }

    // MARK: - Measuring (ported from PR 1442's probe)

    /// What one hosted root measured, in window coordinates (y grows upward).
    private struct Measured {
        let content: NSRect
        /// The content area below the title bar.
        let layout: NSRect
        let split: NSRect
        let detail: NSRect
        /// The top of the panel's document, the panel's first row.
        let detailTop: CGFloat
        let detailDocumentHeight: CGFloat
        let detailVisibleHeight: CGFloat
        /// The top of the sidebar's document, its first row.
        let sidebarTop: CGFloat
        let hostMinimum: NSSize

        var splitAboveTop: Bool { split.maxY > content.height + 1 }
        var splitBelowBottom: Bool { split.minY < -1 }
        var detailTopUnderTitleBar: Bool { detailTop > layout.maxY + 1 }
        var sidebarTopUnderTitleBar: Bool { sidebarTop > layout.maxY + 1 }
    }

    private func fmt(_ value: CGFloat) -> String { String(format: "%.1f", Double(value)) }
    private func fmt(_ size: NSSize) -> String { "\(fmt(size.width))x\(fmt(size.height))" }
    private func fmt(_ rect: NSRect) -> String {
        "(\(fmt(rect.minX)),\(fmt(rect.minY)),\(fmt(rect.width))x\(fmt(rect.height)))"
    }

    private func descendants(_ view: NSView) -> [NSView] {
        var found: [NSView] = []
        for sub in view.subviews {
            found.append(sub)
            found += descendants(sub)
        }
        return found
    }

    /// The visible scroll views under `column` that are not inside another scroll view there.
    private func outermostScrolls(in column: NSView) -> [NSScrollView] {
        let all = descendants(column).compactMap { $0 as? NSScrollView }.filter { !$0.isHidden }
        return all.filter { scroll in
            var parent = scroll.superview
            while let view = parent, view !== column {
                if view is NSScrollView { return false }
                parent = view.superview
            }
            return true
        }
    }

    private func windowRect(_ view: NSView) -> NSRect {
        view.convert(view.bounds, to: nil)
    }

    private func documentTop(_ scroll: NSScrollView) throws -> CGFloat {
        let doc = try XCTUnwrap(scroll.documentView, "a scroll view has no document")
        return doc.convert(NSPoint(x: 0, y: doc.isFlipped ? doc.bounds.minY : doc.bounds.maxY), to: nil).y
    }

    /// Hosts `root` with `strip` in a 1160 x 760 window, measures it, and closes the window.
    private func host(_ root: Root, _ strip: Strip) throws -> Measured {
        _ = NSApplication.shared
        let place = try XCTUnwrap(destination.place)
        let source = try XCTUnwrap(destination.sourceLine(PlaceSourceInputs()))
        let window = UnconstrainedWindow(contentRect: NSRect(origin: NSPoint(x: 20, y: 20), size: Self.windowSize),
                                         styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                                     .fullSizeContentView],
                                         backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer {
            window.orderOut(nil)
            window.close()
        }
        let hosting = NSHostingController(rootView: Harness(root: root, strip: strip, place: place, source: source))
        // Chosen to match the app's window, which opens at its default size and keeps it, and
        // not verified against the scene's window:  the hosting controller enforces the root's
        // minimum and does not resize the window to its ideal.
        hosting.sizingOptions = [.minSize]
        window.contentViewController = hosting
        window.setContentSize(Self.windowSize)
        window.orderFrontRegardless()
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.6))
        window.layoutIfNeeded()
        attachScreenshot(window, "\(root.rawValue)-\(strip.rawValue)")

        let content = try XCTUnwrap(window.contentView)
        let split = try XCTUnwrap(descendants(content).compactMap { $0 as? NSSplitView }.first,
                                  "no NSSplitView found; the harness no longer sees the split view")
        let columns = split.arrangedSubviews
        XCTAssertGreaterThanOrEqual(columns.count, 2, "the split view has fewer than two columns")
        // On macOS 26 the detail column runs under the floating sidebar (both start at x 0), so
        // a scroll view's column comes from the split view's arranged subviews, not its x.
        let sidebarColumn = try XCTUnwrap(columns.first)
        let detailColumn = try XCTUnwrap(columns.last)
        let detail = try XCTUnwrap(outermostScrolls(in: detailColumn)
            .max { ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0) },
            "no scroll view in the detail column")
        let sidebar = try XCTUnwrap(outermostScrolls(in: sidebarColumn)
            .max { windowRect($0).height < windowRect($1).height },
            "no scroll view in the sidebar column")
        let detailTop = try documentTop(detail)
        let sidebarTop = try documentTop(sidebar)
        let measured = Measured(
            content: content.bounds,
            layout: window.contentLayoutRect,
            split: windowRect(split),
            detail: windowRect(detail),
            detailTop: detailTop,
            detailDocumentHeight: detail.documentView?.frame.height ?? 0,
            detailVisibleHeight: detail.contentView.bounds.height,
            sidebarTop: sidebarTop,
            hostMinimum: hosting.sizeThatFits(in: .zero))
        var reasons: [String] = []
        if measured.splitAboveTop { reasons.append("splitAboveTop") }
        if measured.splitBelowBottom { reasons.append("splitBelowBottom") }
        if measured.detailTopUnderTitleBar { reasons.append("panelTopUnderTitleBar") }
        if measured.sidebarTopUnderTitleBar { reasons.append("sidebarTopUnderTitleBar") }
        let overflow: String = reasons.isEmpty ? "no" : "yes(\(reasons.joined(separator: "+")))"
        let parts: [String] = [
            "[window layout] root=\(root.rawValue) strip=\(strip.rawValue)",
            "content=\(fmt(measured.content.size))",
            "layoutTop=\(fmt(measured.layout.maxY))",
            "split=\(fmt(measured.split))",
            "detail=\(fmt(measured.detail))",
            "panelTop=\(fmt(measured.detailTop))",
            "docH=\(fmt(measured.detailDocumentHeight))",
            "visH=\(fmt(measured.detailVisibleHeight))",
            "sidebarTop=\(fmt(measured.sidebarTop))",
            "hostMin=\(fmt(measured.hostMinimum))",
            "overflow=\(overflow)",
        ]
        print(parts.joined(separator: " "))
        return measured
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

    // MARK: - The bug, the fix and the guard

    /// Control:  the root the bug was reported on, with the strip as it was, overflows the way
    /// the owner saw it.  PR 1442 measured the panel's top 104 pt above the title bar's edge in
    /// this window.  If this stops failing to fit, the harness no longer reproduces the bug and
    /// the tests below prove nothing.
    func testTheOldRootWithTheOldStripOverflowsUnderTheTitleBar() throws {
        let measured = try host(.original, .old)
        XCTAssertGreaterThan(measured.split.height, Self.windowSize.height + 100,
                             "the old strip no longer makes the split view taller than the window")
        XCTAssertTrue(measured.splitAboveTop, "the split view did not run above the window's top")
        XCTAssertTrue(measured.detailTopUnderTitleBar, "the panel's first row is not under the title bar")
        XCTAssertTrue(measured.sidebarTopUnderTitleBar, "the sidebar's first row is not under the title bar")
    }

    /// The fix:  the window's root, with the same old strip, keeps both columns inside the
    /// window, their first rows below the title bar, and the panel scrolls.
    func testTheProductionRootKeepsBothColumnsInsideTheWindow() throws {
        let measured = try host(.production, .old)
        assertInside(measured)
    }

    /// Guard:  the production root's frame without the column cap still overflows, off the
    /// bottom (PR 1442 measured 225 to 385 pt below it).  The top alignment keeps the top in
    /// place, but the panel's bottom and its last rows are out of reach.  The cap is the fix:
    /// do not remove it.
    func testWithoutTheColumnCapTheSplitViewRunsOffTheBottom() throws {
        let measured = try host(.uncapped, .old)
        XCTAssertGreaterThan(measured.split.height, Self.windowSize.height + 100,
                             "the old strip no longer makes the split view taller than the window")
        XCTAssertTrue(measured.splitBelowBottom, "without the cap the split view no longer runs off the bottom")
        XCTAssertFalse(measured.splitAboveTop, "the top-aligned root let the split view run above the top")
    }

    /// The app's own strip holds its line to three lines, so it no longer claims a huge
    /// minimum, and even the old root stays inside the window with it (PR 1442, variant
    /// stripSeatSetupLimited).  This is defence in depth;  the cap is still the fix.
    func testTheRealStripStaysInsideEvenTheOldRoot() throws {
        let original = try host(.original, .real)
        assertInside(original)
        let production = try host(.production, .real)
        assertInside(production)
    }

    private func assertInside(_ measured: Measured, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertFalse(measured.splitAboveTop, "the split view runs above the window's top", file: file, line: line)
        XCTAssertFalse(measured.splitBelowBottom, "the split view runs below the window's bottom", file: file, line: line)
        XCTAssertGreaterThanOrEqual(measured.detail.minY, -1, "the panel spills past the window's bottom",
                                    file: file, line: line)
        XCTAssertLessThanOrEqual(measured.detail.maxY, measured.content.maxY + 1, "the panel spills past the window's top",
                                 file: file, line: line)
        XCTAssertLessThanOrEqual(measured.detailTop, measured.layout.maxY + 1, "the panel's first row is under the title bar",
                                 file: file, line: line)
        XCTAssertGreaterThan(measured.detailTop, measured.layout.minY, file: file, line: line)
        XCTAssertGreaterThan(measured.detailDocumentHeight, measured.detailVisibleHeight + 100, "the panel cannot scroll",
                             file: file, line: line)
        XCTAssertLessThanOrEqual(measured.sidebarTop, measured.layout.maxY + 1,
                                 "the sidebar's first row is under the title bar", file: file, line: line)
        XCTAssertGreaterThan(measured.sidebarTop, measured.layout.minY, file: file, line: line)
    }

    /// The trigger itself, with no window:  at a zero-width proposal the old strip's line wraps
    /// about one character per line (PR 1442:  1032 pt), and the app's strip stays small.
    func testTheOldStripClaimsAHugeMinimumAndTheRealOneDoesNot() throws {
        let place = try XCTUnwrap(destination.place)
        let source = try XCTUnwrap(destination.sourceLine(PlaceSourceInputs()))
        let old = NSHostingController(rootView: OldPlaceStrip(place: place, source: source)).sizeThatFits(in: .zero)
        let real = NSHostingController(rootView: PlaceStrip(place: place, source: source)).sizeThatFits(in: .zero)
        print("[window layout] strip minimum at zero width: old=\(fmt(old)) real=\(fmt(real))")
        XCTAssertGreaterThan(old.height, 500, "the old strip no longer claims a huge minimum; the fixture is stale")
        XCTAssertLessThan(real.height, 80, "the app's strip claims more than three caption lines")
    }

    /// Every panel's source line fits the strip at the narrowest detail column (the window's
    /// 1160-pt minimum less the sidebar's 300-pt maximum) without being cut:  the app's strip,
    /// held to three lines, is as tall there as the old strip, which never cut a line.
    func testEverySourceLineFitsTheStripAtTheNarrowestDetail() {
        let inputs = PlaceSourceInputs(servicesReadAt: Date(), serverStatus: nil, seatCheckAt: Date())
        let proposal = NSSize(width: MusterWindowLayout.minWidth - 300, height: 2000)
        for destination in MusterDestination.allCases {
            guard let place = destination.place, let source = destination.sourceLine(inputs) else { continue }
            let old = NSHostingController(rootView: OldPlaceStrip(place: place, source: source))
                .sizeThatFits(in: proposal)
            let real = NSHostingController(rootView: PlaceStrip(place: place, source: source))
                .sizeThatFits(in: proposal)
            XCTAssertEqual(real.height, old.height, accuracy: 1,
                           "\(destination.rawValue)'s source line is cut at the narrowest detail column")
        }
    }

    /// The root frame sets the window's minimum, whatever the panel inside asks for.
    func testRootFrameSetsTheWindowMinimum() throws {
        let measured = try host(.production, .real)
        XCTAssertEqual(measured.content.height, Self.windowSize.height, accuracy: 1,
                       "the window did not keep the size it was given")
        // The content view runs under the title bar, so the root's minimum is measured with it.
        let titleBar = measured.content.height - measured.layout.height
        XCTAssertEqual(measured.hostMinimum.height, MusterWindowLayout.minHeight + titleBar, accuracy: 2,
                       "the root's minimum height is not the window's own")
        XCTAssertEqual(measured.hostMinimum.width, MusterWindowLayout.minWidth, accuracy: 1)
    }

    // MARK: - Stale saved split-view frames

    // The owner's Mac had its split view's columns saved at 1085 pt for a 760-pt window.  That
    // is the height the overflowing split view took (1032 pt of strip and about 52 more), so
    // those frames were most likely saved while the window was overflowing:  a consequence of
    // the bug, not its cause (PR 1442 seeded them in a harness and nothing changed).  The app
    // still forgets such frames at launch, which only clears what older builds saved.

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
