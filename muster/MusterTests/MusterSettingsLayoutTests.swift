import AppKit
import SwiftUI
import XCTest

/// The worst-case content the Settings panes can show, shared by the tests below.
private enum SettingsFixture {
    /// Synthetic paths, as long as the real ones.
    static let checkoutPath = "/tmp/fixture-home/Code/fixture-repository"
    static let longPath = "/tmp/fixture-home/apps/lanes/fixture-repository/seat-a-long-lane-slug-for-the-worst-case"

    /// Every optional line shown at once, with the long path wherever a path wraps.
    static let worstCheckout = FolderRowModel(
        path: longPath, sourceLine: SettingsCopy.chosen, problem: SettingsCopy.checkoutProblem,
        note: SettingsCopy.checksElsewhere(longPath), hasChoice: true)
    static let worstScripts = FolderRowModel(
        path: nil, sourceLine: SettingsCopy.join(SettingsCopy.openedSkipped, SettingsCopy.chosenMissing),
        problem: SettingsCopy.scriptsProblem, note: nil, hasChoice: true)
}

/// BuildIdentitySettingsSection needs Sparkle, which the test bundle does not compile, so this
/// is its worst case row for row:  the marker, two checkouts with three-sentence comparisons,
/// and the waiting line.
private struct WorstCaseBuildSection: View {
    static let comparison = [
        "The app was built from abc1234, and the checkout it runs Python from is at def5678.",
        "They update separately, so the checkout can be ahead of the app or behind it.",
        "A report the checkout writes in a newer format waits for the app to catch up.",
    ].joined(separator: "\u{00A0} ")

    var body: some View {
        Section("This Build") {
            LabeledContent("Version", value: "0.1.0, Build 202611070142")
            LabeledContent("Commit", value: "abc1234, committed Nov 7, 2026 at 1:42 AM")
            LabeledContent("Source", value: "Not From Main:  built from a lane or with uncommitted changes")
            Text("Built from a lane with uncommitted changes").font(.caption.weight(.semibold))
            ForEach([SettingsFixture.checkoutPath, SettingsFixture.longPath], id: \.self) { root in
                LabeledContent("Checkout", value: "\(root) at def5678 on claude/a-long-branch-name")
                Text(Self.comparison).font(.caption).foregroundStyle(.secondary)
            }
            LabeledContent("Automatic Updates", value: "On, from the Muster feed")
            Text("An update is ready, and waits for 2 running actions to finish.")
                .font(.caption).foregroundStyle(.secondary)
            Button("Check For Updates\u{2026}") {}
        }
    }
}

/// The old Settings form (MusterApp.swift at 3c4d023fa), section for section, with the
/// worst-case build section, so its heights are an upper bound.  The control.
private struct LegacySettingsForm: View {
    var body: some View {
        Form {
            Section("Fleet-Config Repository") {
                HStack {
                    TextField("Repository Root", text: .constant("")).font(.system(size: 11, design: .monospaced))
                    Button("Choose\u{2026}") {}
                }
                Text("Saved as fleetConfigRepoRoot, the key FleetConfigLocation reads.\u{00A0} The MUSTER_REPO_ROOT environment variable is read only when nothing is saved.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Scripts Directory") {
                HStack {
                    TextField("Scripts Directory", text: .constant("")).font(.system(size: 11, design: .monospaced))
                    Button("Choose\u{2026}") {}
                }
                Text("Saved as musterScriptsDir.\u{00A0} MUSTER_SCRIPTS_DIR, when set, still wins over the saved folder.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Zulip") {
                Picker("Seat To Read As", selection: .constant("CLAUDE")) { Text("CLAUDE").tag("CLAUDE") }
                Text("Saved as musterZulipSeat.\u{00A0} MUSTER_ZULIP_SEAT, when set, still wins over the saved seat.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Services") {
                Toggle("Keep Services Running", isOn: .constant(true))
                Text(SettingsCopy.servicesHelper).font(.caption).foregroundStyle(.secondary)
            }
            Section("Appearance") {
                Picker("Theme", selection: .constant(ThemeChoice.system)) {
                    ForEach(ThemeChoice.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                Toggle("Short Menu Bar Text", isOn: .constant(false))
                Text(SettingsCopy.shortMenuBarHelper).font(.caption).foregroundStyle(.secondary)
            }
            WorstCaseBuildSection()
        }
        .formStyle(.grouped)
        .frame(width: 520)
    }
}

/// The Settings window (owner's report, November 7, 2026:  only three sections showed, with no
/// scroller).  Each pane is hosted in a plain window, not the app's Settings scene, which no
/// test runs, with the worst-case content.  A source guard keeps the scene itself as the fix
/// left it.  Two controls host the old single Form and must fail the same checks:  they are
/// strict expected failures, so if the old Form passes, CI says so.  They show the old form
/// is taller than an assumed laptop screen;  neither reproduces the three-section screenshot.
///
/// Every run prints `PROBE settings` lines with the measurements behind each assertion.
@MainActor
final class MusterSettingsLayoutTests: XCTestCase {
    /// An assumed laptop screen less the menu bar.  Not measured on the owner's Mac.
    private static let assumedScreen: CGFloat = 923
    /// A plain title bar;  today's single Form has no tab toolbar.
    private static let titleBar: CGFloat = 28

    /// Kept at the size the test gives it, even when that is taller than the runner's screen.
    final class UnconstrainedWindow: NSWindow {
        override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
    }

    // MARK: Measuring

    private struct Measured {
        let name: String
        /// The size a `.contentSize` window would take:  the content's ideal size.
        let fitting: NSSize
        let content: NSSize
        let documentHeight: CGFloat
        let visibleHeight: CGFloat
        var hidden: CGFloat { max(0, documentHeight - visibleHeight) }
    }

    private func fmt(_ v: CGFloat) -> String { String(format: "%.1f", Double(v)) }
    private func fmt(_ s: NSSize) -> String { "\(fmt(s.width))x\(fmt(s.height))" }

    private func scrollViews(in view: NSView) -> [NSScrollView] {
        var found: [NSScrollView] = []
        for sub in view.subviews {
            if let sv = sub as? NSScrollView, !sv.isHidden { found.append(sv) }
            found += scrollViews(in: sub)
        }
        return found
    }

    /// Hosts `view` in a window `contentHeight` tall (its fitting height when nil), measures the
    /// Form's scroll view, prints a PROBE line and closes the window.
    private func layOut<V: View>(_ name: String, _ view: V, contentHeight: CGFloat? = nil) throws -> Measured {
        _ = NSApplication.shared
        // The ideal size, from a controller with the default sizing options.
        let fitting = NSHostingController(rootView: view).view.fittingSize
        let hosting = NSHostingController(rootView: view)
        // The test sets the window's size, as a restored or content-sized window has it.
        hosting.sizingOptions = []
        let size = NSSize(width: MusterSettingsLayout.paneWidth, height: contentHeight ?? fitting.height)
        let window = UnconstrainedWindow(contentRect: NSRect(origin: NSPoint(x: 20, y: 20), size: size),
                                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer { window.orderOut(nil); window.close() }
        window.contentViewController = hosting
        window.setContentSize(size)
        window.orderFrontRegardless()
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.6))
        window.layoutIfNeeded()
        attachScreenshot(window, name)
        let content = try XCTUnwrap(window.contentView)
        let all = scrollViews(in: content)
        for sv in all {
            print("PROBE settings-scroll name=\(name) frame=\(fmt(sv.frame.size)) "
                  + "doc=\(fmt(sv.documentView?.frame.size ?? .zero)) visible=\(fmt(sv.documentVisibleRect.size))")
        }
        let scroll = try XCTUnwrap(
            all.max { ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0) },
            "no NSScrollView in \(name); the harness no longer sees the Form's scroll view")
        let measured = Measured(name: name, fitting: fitting, content: content.bounds.size,
                                documentHeight: scroll.documentView?.frame.height ?? 0,
                                visibleHeight: scroll.documentVisibleRect.height)
        print("PROBE settings name=\(name) fitting=\(fmt(fitting)) zero=\(fmt(hosting.sizeThatFits(in: .zero))) "
              + "content=\(fmt(measured.content)) doc=\(fmt(measured.documentHeight)) "
              + "visible=\(fmt(measured.visibleHeight)) hidden=\(fmt(measured.hidden))")
        return measured
    }

    private func attachScreenshot(_ window: NSWindow, _ name: String) {
        guard let view = window.contentView, let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return }
        let attachment = XCTAttachment(data: png, uniformTypeIdentifier: "public.png")
        attachment.name = "settings-\(name)"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// Nothing sits below the fold, where macOS shows no scroller until you scroll.
    private func assertNothingHidden(_ m: Measured, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertLessThanOrEqual(m.documentHeight, m.visibleHeight + 2,
                                 "\(m.name) hides \(fmt(m.hidden)) pt below the fold", file: file, line: line)
    }

    // MARK: The fix

    func testThePanesAndTheirChromeFitTheShortestWindow() {
        XCTAssertLessThanOrEqual(MusterSettingsLayout.paneHeight + MusterSettingsLayout.chromeAllowance,
                                 MusterSettingsLayout.shortestWindow)
    }

    func testTheFoldersPaneShowsEveryLineWithoutScrolling() throws {
        let m = try layOut("folders", MusterFoldersPane(checkout: SettingsFixture.worstCheckout,
                                                        scripts: SettingsFixture.worstScripts,
                                                        choose: { _, _ in }, useDefault: { _ in }))
        XCTAssertEqual(m.fitting.height, MusterSettingsLayout.paneHeight, accuracy: 1, "the pane is not its fixed size")
        assertNothingHidden(m)
    }

    func testTheZulipAndServicesPaneShowsEveryLineWithoutScrolling() throws {
        let m = try layOut("zulip-services", MusterZulipServicesPane(
            seat: .constant("CLAUDE"), seatChoices: ["CLAUDE", "MM"], environmentSeat: "MM",
            keepServicesRunning: .constant(true)))
        XCTAssertEqual(m.fitting.height, MusterSettingsLayout.paneHeight, accuracy: 1, "the pane is not its fixed size")
        assertNothingHidden(m)
    }

    func testTheAppearancePaneShowsEveryLineWithoutScrolling() throws {
        let m = try layOut("appearance", MusterAppearancePane(theme: .constant(.system),
                                                              shortMenuBarText: .constant(false)))
        XCTAssertEqual(m.fitting.height, MusterSettingsLayout.paneHeight, accuracy: 1, "the pane is not its fixed size")
        assertNothingHidden(m)
    }

    func testTheBuildPaneShowsEveryLineWithoutScrolling() throws {
        let m = try layOut("build", MusterSettingsPane { WorstCaseBuildSection() })
        XCTAssertEqual(m.fitting.height, MusterSettingsLayout.paneHeight, accuracy: 1, "the pane is not its fixed size")
        assertNothingHidden(m)
    }

    // MARK: The scene itself

    /// No test runs the Settings scene, so its source is checked:  the scene carries no
    /// `windowResizability`, and its root is the TabView of the four fixed-size panes, not a Form.
    func testTheSettingsSceneKeepsItsFixedSizeTabs() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Muster/MusterApp.swift")
        let source = try String(contentsOf: url, encoding: .utf8)
        let sceneStart = try XCTUnwrap(source.range(of: "\n        Settings {"), "no Settings scene in MusterApp.swift")
        let sceneEnd = try XCTUnwrap(source.range(of: "MenuBarExtra", range: sceneStart.upperBound..<source.endIndex))
        let scene = source[sceneStart.lowerBound..<sceneEnd.lowerBound]
        XCTAssertTrue(scene.contains("MusterSettingsView()"), String(scene))
        XCTAssertFalse(scene.contains("windowResizability"), "the Settings scene sets windowResizability again")
        let viewStart = try XCTUnwrap(source.range(of: "struct MusterSettingsView: View {"))
        let viewEnd = try XCTUnwrap(source.range(of: "struct MusterFoldersTab: View {",
                                                 range: viewStart.upperBound..<source.endIndex))
        let view = source[viewStart.upperBound..<viewEnd.lowerBound]
        XCTAssertTrue(view.contains("TabView {"), "the Settings root is no longer a TabView")
        XCTAssertFalse(view.contains("Form {"), "the Settings root is a single Form again")
        for pane in ["MusterFoldersTab()", "MusterZulipServicesPane(", "MusterAppearancePane(",
                     "MusterSettingsPane { BuildIdentitySettingsSection() }"] {
            XCTAssertTrue(view.contains(pane), "the Settings TabView lost \(pane)")
        }
    }

    // MARK: Controls:  the old form must fail the same checks

    /// Reading (a):  sized to its content (Settings' own `.contentSize`), the old form with a
    /// worst-case build section is taller than the assumed screen.
    func testTheOldFormIsTallerThanAnAssumedScreen() throws {
        let m = try layOut("legacy-fitting", LegacySettingsForm())
        XCTExpectFailure("The old Settings form does not fit an assumed 923-pt screen.", strict: true) {
            XCTAssertLessThanOrEqual(m.fitting.height + Self.titleBar, Self.assumedScreen,
                                     "the old form fits the assumed screen")
        }
    }

    /// Reading (b):  in a window as tall as the assumed screen, which `.contentMinSize` allowed
    /// and a saved frame restores, the old form hides content below an unseen scroller.  This
    /// window shows more sections than the screenshot did, so it proves overflow, not a match.
    func testTheOldFormHidesContentInAWindowTheScreenHolds() throws {
        let m = try layOut("legacy-screen", LegacySettingsForm(), contentHeight: Self.assumedScreen - Self.titleBar)
        XCTExpectFailure("The old Settings form hides content in a window as tall as an assumed screen.",
                         strict: true) {
            assertNothingHidden(m)
        }
    }
}
