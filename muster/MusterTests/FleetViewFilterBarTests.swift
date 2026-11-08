import AppKit
import SwiftUI
import XCTest

/// Apps & Work's filter row (owner's screenshot, November 7, 2026:  the App Kind control's first
/// segment was cut at the pane's left edge, reading "ps", and its last segment ran under the
/// filter field).  The row is hosted at the narrowest detail pane the window allows and at a
/// wide one, and every control must sit inside the row without overlapping another.
///
/// Every run prints `PROBE filter-bar` lines with the frames behind each assertion.
@MainActor
final class FleetViewFilterBarTests: XCTestCase {
    /// The narrowest detail pane:  the window's minimum width, less the widest sidebar and the
    /// page's 24 pt padding on each side.
    static let narrowestRow = MusterWindowLayout.minWidth - MusterWindowLayout.sidebarMaxWidth - 48
    /// A full-screen window on a large display, with the sidebar at its ideal width.
    static let wideRow: CGFloat = 1600 - MusterWindowLayout.sidebarIdealWidth - 48

    private struct Frames {
        let row: NSRect
        let segmented: NSRect
        let field: NSRect
        let checkbox: NSRect
    }

    private func fmt(_ r: NSRect) -> String {
        String(format: "x=%.1f y=%.1f w=%.1f h=%.1f", Double(r.minX), Double(r.minY), Double(r.width), Double(r.height))
    }

    private func descendants(of view: NSView) -> [NSView] {
        view.subviews.flatMap { [$0] + descendants(of: $0) }
    }

    /// Hosts the row `width` wide, lays it out and returns each control's frame in the row's
    /// own coordinates.
    private func layOut(width: CGFloat) throws -> Frames {
        _ = NSApplication.shared
        var filter = FleetViewFilter()
        let binding = Binding(get: { filter }, set: { filter = $0 })
        let hosting = NSHostingView(rootView: FleetViewFilterBar(filter: binding).frame(width: width))
        let height = max(hosting.fittingSize.height, 30)
        let window = NSWindow(contentRect: NSRect(x: 20, y: 20, width: width, height: height),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer { window.orderOut(nil); window.close() }
        window.contentView = hosting
        hosting.frame = NSRect(x: 0, y: 0, width: width, height: height)
        window.orderFrontRegardless()
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        window.layoutIfNeeded()

        let all = descendants(of: hosting)
        let segmented = try XCTUnwrap(all.first { $0 is NSSegmentedControl }, "no segmented control at \(width) pt")
        let field = try XCTUnwrap(all.first { $0 is NSTextField && ($0 as? NSTextField)?.isEditable == true },
                                  "no filter field at \(width) pt")
        // The row's only button is the checkbox;  the segmented control is not an NSButton.
        let checkbox = try XCTUnwrap(all.first { $0 is NSButton }, "no Needs Attention Only checkbox at \(width) pt")
        let frames = Frames(row: hosting.bounds,
                            segmented: segmented.convert(segmented.bounds, to: hosting),
                            field: field.convert(field.bounds, to: hosting),
                            checkbox: checkbox.convert(checkbox.bounds, to: hosting))
        print("PROBE filter-bar width=\(width) row=[\(fmt(frames.row))] segmented=[\(fmt(frames.segmented))] "
              + "field=[\(fmt(frames.field))] checkbox=[\(fmt(frames.checkbox))]")
        return frames
    }

    private func assertInsideAndApart(_ f: Frames, width: CGFloat, file: StaticString = #filePath, line: UInt = #line) {
        let row = f.row.insetBy(dx: -0.5, dy: -0.5)
        for (name, frame) in [("App Kind", f.segmented), ("filter field", f.field), ("checkbox", f.checkbox)] {
            XCTAssertTrue(row.contains(frame), "\(name) [\(fmt(frame))] leaves the \(width) pt row", file: file, line: line)
        }
        let pairs = [("App Kind", f.segmented, "filter field", f.field),
                     ("App Kind", f.segmented, "checkbox", f.checkbox),
                     ("filter field", f.field, "checkbox", f.checkbox)]
        for (a, fa, b, fb) in pairs {
            let overlap = fa.insetBy(dx: 0.5, dy: 0.5).intersection(fb.insetBy(dx: 0.5, dy: 0.5))
            XCTAssertTrue(overlap.isNull || overlap.isEmpty,
                          "\(a) overlaps \(b) at \(width) pt by [\(fmt(overlap))]", file: file, line: line)
        }
        XCTAssertGreaterThanOrEqual(f.field.width, FleetViewFilterBar.fieldMinWidth - 1,
                                    "the filter field is squeezed below its minimum at \(width) pt", file: file, line: line)
    }

    func testTheRowFitsTheNarrowestPane() throws {
        assertInsideAndApart(try layOut(width: Self.narrowestRow), width: Self.narrowestRow)
    }

    func testTheRowFitsAWidePane() throws {
        assertInsideAndApart(try layOut(width: Self.wideRow), width: Self.wideRow)
    }

    /// FleetView is not in the test bundle, so the order of its reads is pinned by its source:
    /// the slow work read starts first, through the app's `FleetViewStore`, and the apps are
    /// listed from the local registry before the page waits on it (owner's report, November 7,
    /// 2026:  the page sat on a spinner).  The read itself, and the rebuild of the rows before
    /// the envelope is set, are the store's, and its source is pinned below.
    func testTheAppsListBeforeTheWorkReadReturns() throws {
        let muster = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Muster")
        let source = try String(contentsOf: muster.appendingPathComponent("FleetView.swift"), encoding: .utf8)
        func find(_ needle: String, in text: String, file: String) throws -> String.Index {
            try XCTUnwrap(text.range(of: needle)?.lowerBound, "\(file) no longer contains \(needle)")
        }
        func position(_ needle: String) throws -> String.Index {
            try find(needle, in: source, file: "FleetView.swift")
        }
        // A visit reads only when the reading on hand is old;  the Refresh button always reads.
        XCTAssertTrue(source.contains(".task { await load(force: false) }"))
        XCTAssertTrue(source.contains("Task { await load(force: true) }"))
        XCTAssertTrue(source.contains("@ObservedObject private var reading = FleetViewStore.shared"))
        // The view never runs the work read or rebuilds the rows itself.
        XCTAssertFalse(source.contains("FleetViewService.load()"), "the view runs fleet-view itself")
        XCTAssertFalse(source.contains("viewOutput: loaded"), "the view rebuilds the rows itself")

        let startWork = try position("await reading.refreshIfOlder(than: Self.visitFreshness, engineRoot: named)")
        let forceWork = try position("await reading.refresh(engineRoot: named)")
        let readTools = try position("FleetToolsReportReader.readDefault()")
        let listApps = try position("FleetRegistryReader.read(repoRoot: root, viewOutput: nil, toolsReading: tools)")
        let awaitWork = try position("await work.value")
        XCTAssertLessThan(startWork, readTools, "the work read must start before the local reads")
        XCTAssertLessThan(forceWork, readTools, "the forced work read must start before the local reads")
        XCTAssertLessThan(readTools, listApps)
        XCTAssertLessThan(listApps, awaitWork, "the apps must be listed before the page waits on the work read")
        // The early listing is shown only while the counts are read;  the store's rows take over
        // when its read lands, so a failed read still clears the page.
        XCTAssertTrue(source.contains("if countsPending, let earlyRegistry { return earlyRegistry }\n        return reading.registrySummary"))
        XCTAssertTrue(source.contains("private var countsPending: Bool { loading && envelope == nil }"))
        // Another checkout's reading never stands in for this one (Kody, PR 1481):  a visit after
        // the checkout changed reads even inside the window, and an earlier load's listing is
        // dropped before this load lists its apps.
        XCTAssertTrue(source.contains("let mustRead = force || checkoutChanged"))
        XCTAssertTrue(source.contains("if mustRead {\n                await reading.refresh(engineRoot: named)"))
        let dropListing = try position("if reading.envelope == nil || checkoutChanged {\n            earlyRegistry = nil")
        XCTAssertLessThan(dropListing, readTools, "the old listing must go before this load lists its apps")
        XCTAssertTrue(source.contains("if !FleetViewReadingOrigin.checkoutChanged(to: named) {\n                earlyRegistry = listed"))

        // The rows are rebuilt with the work read before `envelope` is set, so pending never
        // ends while the rows still hold counts that were not read (Sentry, PR 1450).
        let store = try String(contentsOf: muster.appendingPathComponent("Model/FleetViewStore.swift"), encoding: .utf8)
        let storeFile = "FleetViewStore.swift"
        let loadView = try find("let loaded = try await loadView()", in: store, file: storeFile)
        let rebuild = try find("readRegistry(root, loaded, tools)", in: store, file: storeFile)
        let setEnvelope = try find("envelope = loaded", in: store, file: storeFile)
        let setRows = try find("registrySummary = summary", in: store, file: storeFile)
        XCTAssertLessThan(loadView, rebuild)
        XCTAssertLessThan(rebuild, setEnvelope, "the rows must be rebuilt before the envelope goes on screen")
        XCTAssertLessThan(setEnvelope, setRows)
        // Envelope and rows are set in one step, with no suspension between them.
        let between = store[setEnvelope..<setRows]
        XCTAssertFalse(between.contains("await"), "the store suspends between setting the envelope and the rows")

        // A pending count is a dash, never a zero.
        XCTAssertTrue(source.contains("if countsPending {\n                pendingCell(width: 120)"))
    }

    /// The App Kind control is never narrower than its labels, so no segment is cut.
    func testTheAppKindControlGetsItsNaturalWidth() throws {
        let frames = try layOut(width: Self.narrowestRow)
        let natural = NSSegmentedControl(labels: FleetAppKindFilter.allCases.map(\.rawValue),
                                         trackingMode: .selectOne, target: nil, action: nil).intrinsicContentSize.width
        print("PROBE filter-bar natural-segmented=\(natural)")
        XCTAssertGreaterThanOrEqual(frames.segmented.width + 1, natural,
                                    "the App Kind control is narrower than its labels need")
    }
}
