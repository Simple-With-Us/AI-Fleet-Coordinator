import AppKit
import Combine
import SwiftUI
import XCTest

/// Every panel of the window, rendered to a PNG (AFC-707).
///
/// The window's real root, `FleetOverviewView`, is hosted in an `NSHostingView` on each
/// `MusterDestination`, at the window's default size (1160 x 760) and at its minimum
/// (`MusterWindowLayout.minWidth` x `MusterWindowLayout.minHeight`), in light and in dark.
/// The root reads its place and its theme from the defaults, so the test writes both keys
/// in this test process's own defaults (never the app's) and puts them back afterwards.
///
/// The five environment stores are fixtures:  the engine answers with a recorded
/// `status --json`, the server report is a recorded one, the services read answers from a
/// fake command runner, and every Ops run fails at once without starting a process.  No
/// store here starts a timer, a watcher or a run of its own.
///
/// What the fixtures cannot reach:  a few panels read through app singletons
/// (`OpsController.shared`, `FleetViewStore.shared`, `SeatActivityStore.shared`,
/// `WakeSettingsStore.shared`, `ServerEavesdropperStatusStore.shared`) from a `.task` when
/// they appear.  Those reads are the app's own read-only readers and run as they would in
/// the app;  the PNG is taken before most of them land, so those panels show their loading
/// or not-read state.  Giving them fixtures needs an injection point in the view, which this
/// harness leaves to a follow-up rather than changing a view.
///
/// This is report only:  no pixel baselines.  The checks are that every PNG was written,
/// that it is not one flat colour, and that the split view stays inside the window (the
/// AFC-616 overflow).  The PNGs go to `MUSTER_PANEL_SNAPSHOT_DIR` when it is set
/// (`script/render_panels.sh` sets it through `TEST_RUNNER_`), else to a temporary folder.
@MainActor
final class PanelSnapshotTests: XCTestCase {
    enum Theme: String, CaseIterable {
        case light, dark

        var choice: ThemeChoice { self == .light ? .light : .dark }
        var appearance: NSAppearance? { NSAppearance(named: self == .light ? .aqua : .darkAqua) }
    }

    struct Size {
        let name: String
        let width: CGFloat
        let height: CGFloat
    }

    static let sizes = [
        Size(name: "default", width: 1160, height: 760),
        Size(name: "minimum", width: MusterWindowLayout.minWidth, height: MusterWindowLayout.minHeight),
    ]

    /// Every panel, or only those named in `MUSTER_PANEL_ONLY` (comma separated raw values),
    /// for a quick local look.  The count checks below still demand every panel, so a filtered
    /// run fails them on purpose and can never stand in for the whole set.
    static var destinations: [MusterDestination] {
        guard let only = ProcessInfo.processInfo.environment["MUSTER_PANEL_ONLY"], !only.isEmpty else {
            return MusterDestination.allCases
        }
        let names = Set(only.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) })
        return MusterDestination.allCases.filter { names.contains($0.rawValue) }
    }

    /// How long each panel gets to lay out and draw before it is captured.
    static let settle: TimeInterval = 0.6
    /// Far off every screen, so the host's window is never seen.
    static let offscreen: CGFloat = -30000
    /// How much taller than asked the window may come out at the minimum size.
    static let toolbarAllowance: CGFloat = 52

    private var savedDestination: Any?
    private var savedTheme: Any?
    private var scratch: URL!

    override func setUpWithError() throws {
        // The test host never comes forward:  an accessory app has no Dock icon and is never
        // made active by this test, and every window below is borderless (it cannot become
        // key), fully transparent, ignores the mouse, sits far off every screen and is only
        // ever ordered to the back.  Nothing here calls activate or orderFront.
        NSApplication.shared.setActivationPolicy(.accessory)
        let defaults = UserDefaults.standard
        savedDestination = defaults.object(forKey: MusterSettings.sidebarSelectionDefaultsKey)
        savedTheme = defaults.object(forKey: MusterSettings.themeDefaultsKey)
        scratch = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-panel-fixtures-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        let defaults = UserDefaults.standard
        for (key, saved) in [(MusterSettings.sidebarSelectionDefaultsKey, savedDestination),
                             (MusterSettings.themeDefaultsKey, savedTheme)] {
            if let saved { defaults.set(saved, forKey: key) } else { defaults.removeObject(forKey: key) }
        }
        if let scratch { try? FileManager.default.removeItem(at: scratch) }
    }

    // MARK: The test

    func testEveryPanelRendersInBothThemesAtBothSizes() throws {
        let output = try Self.outputDirectory()
        let stores = try makeStores()
        var written: [String: [URL]] = [:]

        for size in Self.sizes {
            let folder = output.appendingPathComponent(size.name, isDirectory: true)
            try? FileManager.default.removeItem(at: folder)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            for theme in Theme.allCases {
                for destination in Self.destinations {
                    let url = folder.appendingPathComponent("\(destination.rawValue)-\(theme.rawValue).png")
                    try render(destination, theme: theme, size: size, stores: stores, to: url)
                    written[size.name, default: []].append(url)
                }
            }
        }

        // One PNG per panel per theme, at each size, and nothing else in the folder.
        for size in Self.sizes {
            let folder = output.appendingPathComponent(size.name, isDirectory: true)
            let pngs = try FileManager.default.contentsOfDirectory(atPath: folder.path)
                .filter { $0.hasSuffix(".png") }
            XCTAssertEqual(pngs.count, MusterDestination.allCases.count * Theme.allCases.count,
                           "\(size.name):  one PNG per MusterDestination per theme")
            XCTAssertEqual(written[size.name]?.count, MusterDestination.allCases.count * 2)
        }
        print("PROBE panel-snapshots dir=\(output.path) count=\(written.values.map(\.count).reduce(0, +))")
    }

    // MARK: Rendering

    private func render(_ destination: MusterDestination, theme: Theme, size: Size,
                        stores: Stores, to url: URL) throws {
        let started = Date()
        let defaults = UserDefaults.standard
        defaults.set(destination.rawValue, forKey: MusterSettings.sidebarSelectionDefaultsKey)
        defaults.set(theme.choice.rawValue, forKey: MusterSettings.themeDefaultsKey)

        let root = FleetOverviewView()
            .environmentObject(stores.store)
            .environmentObject(stores.monitor)
            .environmentObject(stores.admin)
            .environmentObject(stores.router)
            .environmentObject(stores.watch)
        let hosting = NSHostingView(rootView: root)
        let frame = NSRect(x: 0, y: 0, width: size.width, height: size.height)
        let window = NSWindow(contentRect: frame.offsetBy(dx: Self.offscreen, dy: Self.offscreen),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = theme.appearance
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.hasShadow = false
        window.collectionBehavior = [.transient, .ignoresCycle, .stationary]
        defer {
            // Take the root out of the window before the next panel is built:  a root left alive
            // still follows the saved place (`.onChange(of: storedDestination)`) and writes its
            // own back, which showed one panel's content in another panel's PNG.
            window.contentView = nil
            window.orderOut(nil)
            window.close()
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        window.contentView = hosting
        window.setContentSize(frame.size)
        hosting.frame = frame
        // Ordered in behind every other window, off screen and transparent, so SwiftUI draws
        // it;  ordering to the back never activates the host or takes focus.
        window.orderBack(nil)
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(Self.settle))
        window.layoutIfNeeded()
        hosting.layoutSubtreeIfNeeded()
        hosting.displayIfNeeded()

        let label = "\(destination.rawValue) \(theme.rawValue) \(size.name)"
        let bounds = hosting.bounds
        XCTAssertEqual(bounds.width, size.width, accuracy: 1, "\(label):  the window took another width")
        // At the minimum height the root's own minimum plus the split view's toolbar can ask for
        // a little more than the window's minimum, and the window grows to it, as the app's
        // window does (`.contentMinSize`).  The PNG is taken at the height actually granted.
        XCTAssertGreaterThanOrEqual(bounds.height, size.height - 1, "\(label):  the window is shorter than asked")
        XCTAssertLessThanOrEqual(bounds.height, size.height + Self.toolbarAllowance,
                                 "\(label):  the content grew the window by more than a toolbar")

        // The AFC-616 overflow:  the split view, and so both columns, inside the window.
        let splits = Self.descendants(of: hosting).compactMap { $0 as? NSSplitView }
        XCTAssertFalse(splits.isEmpty, "\(label):  no split view was laid out")
        for split in splits {
            let inHost = split.convert(split.bounds, to: hosting)
            XCTAssertTrue(bounds.insetBy(dx: -1, dy: -1).contains(inHost),
                          "\(label):  the split view [\(inHost)] leaves the window [\(bounds)]")
        }

        let rep = try XCTUnwrap(Self.capture(hosting, in: window, appearance: theme.appearance),
                                "\(label):  no bitmap")
        XCTAssertGreaterThan(rep.pixelsWide, 0, "\(label):  empty render")
        XCTAssertGreaterThan(rep.pixelsHigh, 0, "\(label):  empty render")
        XCTAssertFalse(Self.isOneColour(rep), "\(label):  the render is one flat colour")
        let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]), "\(label):  no PNG")
        try png.write(to: url)
        print("PROBE panel-snapshot \(label) px=\(rep.pixelsWide)x\(rep.pixelsHigh) bytes=\(png.count) "
              + "seconds=\(String(format: "%.1f", Date().timeIntervalSince(started))) "
              + "splits=\(splits.map { NSStringFromRect($0.convert($0.bounds, to: hosting)) })")
    }

    /// The host drawn by `cacheDisplay` in the theme's appearance, over the theme's window
    /// background.  The window's background belongs to the window's frame, not its content
    /// view, so `cacheDisplay` alone leaves it transparent:  dark text then sits on a white
    /// page in a viewer.  Drawing the layer tree instead (`CALayer.render`) gave tiles out of
    /// order and no sidebar, so it is not used.  Known gap:  sidebar vibrancy is not drawn by
    /// `cacheDisplay`, so the sidebar's text reads dimmer in these PNGs than in the window.
    private static func capture(_ hosting: NSView, in window: NSWindow,
                                appearance: NSAppearance?) -> NSBitmapImageRep? {
        let bounds = hosting.bounds
        let drawing = appearance ?? NSAppearance.currentDrawing()
        guard let content = hosting.bitmapImageRepForCachingDisplay(in: bounds) else { return nil }
        drawing.performAsCurrentDrawingAppearance {
            hosting.cacheDisplay(in: bounds, to: content)
        }
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
                                         pixelsWide: content.pixelsWide, pixelsHigh: content.pixelsHigh,
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                         isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0) else { return content }
        // The size in points first, so the context draws in points at the backing scale.
        rep.size = bounds.size
        guard let context = NSGraphicsContext(bitmapImageRep: rep) else { return content }
        NSGraphicsContext.saveGraphicsState()
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSGraphicsContext.current = context
        drawing.performAsCurrentDrawingAppearance {
            NSColor.windowBackgroundColor.setFill()
            bounds.fill()
        }
        content.draw(in: bounds, from: .zero, operation: .sourceOver, fraction: 1,
                     respectFlipped: false, hints: nil)
        context.flushGraphics()
        return rep
    }

    private static func descendants(of view: NSView) -> [NSView] {
        view.subviews.flatMap { [$0] + descendants(of: $0) }
    }

    /// True when every sampled pixel has the same colour:  a blank window, not a panel.
    private static func isOneColour(_ rep: NSBitmapImageRep) -> Bool {
        let step = max(1, min(rep.pixelsWide, rep.pixelsHigh) / 40)
        var first: NSColor?
        for y in stride(from: 0, to: rep.pixelsHigh, by: step) {
            for x in stride(from: 0, to: rep.pixelsWide, by: step) {
                guard let colour = rep.colorAt(x: x, y: y) else { continue }
                if let first {
                    if first != colour { return false }
                } else {
                    first = colour
                }
            }
        }
        return true
    }

    static func outputDirectory() throws -> URL {
        let environment = ProcessInfo.processInfo.environment
        let url: URL
        if let path = environment["MUSTER_PANEL_SNAPSHOT_DIR"], !path.isEmpty {
            url = URL(fileURLWithPath: path, isDirectory: true)
        } else {
            url = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("muster-panel-snapshots", isDirectory: true)
        }
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    // MARK: Fixture stores

    struct Stores {
        let store: FleetStore
        let monitor: DriftMonitor
        let admin: FleetAdmin
        let router: ServiceActionRouter
        let watch: PlaceWatch
    }

    private func makeStores() throws -> Stores {
        // The engine reads this checkout's own manifest and registry, read only, and answers
        // with the recorded two-seat status.
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let envelope = try EnvelopeDecoder.decode(try Self.fixtureData("status_two_seats_live", "json"))
        let store = FleetStore(
            location: EngineLocation(root: repoRoot, interpreter: URL(fileURLWithPath: "/usr/bin/python3")),
            runEngine: { _, completion in
                completion(EngineRun(envelope: envelope, exitCode: 0, standardError: "",
                                     failure: nil, timedOut: false))
            },
            refreshOnStart: false
        )
        store.refresh()

        let monitor = DriftMonitor(
            grid: store,
            notifier: QuietNotifier(),
            history: DriftHistoryStore(url: scratch.appendingPathComponent("drift-history.json")),
            watcher: StillWatcher(),
            scheduler: NeverScheduler(),
            ticker: StillTicker(),
            wake: StillWake(),
            home: scratch
        )

        let commands = FixtureCommands()
        let startup = StartupPolicy(home: scratch, commands: commands, uid: 501, environment: [:])
        let api = ControlAPI(baseURL: URL(string: "http://127.0.0.1:9")!)
        let control = FleetServiceControl(
            pm2: PM2Client(commands: commands, nodePath: "/usr/bin/node", pm2Path: "/usr/bin/pm2"),
            startup: startup,
            mcpServer: nil,
            stopServer: {}
        )
        let controller = OpsController(
            runner: FailingOpsRunner(),
            makeBuilder: { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/example/repo"),
                                             home: URL(fileURLWithPath: "/example/home"),
                                             sshAuthSocket: nil) },
            auditURL: nil,
            checksInstall: false
        )
        let admin = FleetAdmin(
            control: control,
            startup: startup,
            presence: ServerPresenceProbe(port: 9, connect: { _ in .absent }),
            api: api,
            stopWait: ServerPortWait(attempts: 1, interval: 0),
            startWait: ServerPortWait(attempts: 1, interval: 0),
            refreshInterval: nil,
            runsCompleted: Empty<Int, Never>().eraseToAnyPublisher()
        )
        let allowlist = try Self.fixtureURL("ops_allowlist", "json")
        let router = ServiceActionRouter(
            admin: admin,
            controller: controller,
            readAllowlist: { OpsAllowlist.read(at: allowlist) },
            confirm: { _ in false },
            notifier: QuietNotifier(),
            settleDelay: 0
        )
        let serverReport = try Self.fixtureURL("server_status_current", "json")
        let watch = PlaceWatch(
            readStatus: { ServerStatusReportReader.read(at: serverReport, now: Date()) },
            readTools: { FleetToolsReading() },
            runner: FailingOpsRunner(),
            makeBuilder: { OpsCommandBuilder(repoRoot: URL(fileURLWithPath: "/example/repo"),
                                             home: URL(fileURLWithPath: "/example/home"),
                                             sshAuthSocket: nil) },
            interval: nil,
            controller: controller
        )
        watch.readNow()
        // The engine's answer and the server report land on the main queue.
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        return Stores(store: store, monitor: monitor, admin: admin, router: router, watch: watch)
    }

    private final class Locator {}

    static func fixtureURL(_ name: String, _ ext: String) throws -> URL {
        try XCTUnwrap(Bundle(for: Locator.self).url(forResource: name, withExtension: ext),
                      "missing fixture \(name).\(ext)")
    }

    static func fixtureData(_ name: String, _ ext: String) throws -> Data {
        try Data(contentsOf: try fixtureURL(name, ext))
    }
}

// MARK: Fakes:  nothing here starts a process, a timer or a watcher.

private struct QuietNotifier: ApplyNotifying {
    func post(title: String, body: String) async {}
}

private struct PanelFixtureError: Error {}

/// Every Ops run fails at once, so no `ssh` or helper starts.
private struct FailingOpsRunner: OpsRunning {
    func run(_ invocation: OpsInvocation) async throws -> OpsProcessResult { throw PanelFixtureError() }
}

/// The Mac's service reads:  launchctl lists one service and pm2 lists none.
private final class FixtureCommands: CommandRunning {
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        if executable == "/bin/launchctl", arguments == ["list"] {
            return "PID\tStatus\tLabel\n-\t0\tother.service"
        }
        if executable == "/usr/sbin/sysctl" { return "panel-fixture-boot" }
        if arguments.contains("jlist") { return "[]" }
        return ""
    }
}

private final class StillWatcher: DriftWatching {
    func arm(_ watches: [DriftWatchList.Watch], onChange: @escaping ([URL]) -> Void) -> [DriftWatchList.Watch] { [] }
    func disarm(_ watches: [DriftWatchList.Watch]) {}
    var startedWatches: [DriftWatchList.Watch] { [] }
    func stop() {}
}

private final class NeverScheduler: DriftScheduling {
    func schedule(after delay: TimeInterval, work: @escaping () -> Void) -> DriftScheduledWork {
        NeverWork()
    }
}

private final class NeverWork: DriftScheduledWork {
    func cancel() {}
}

private final class StillTicker: DriftTicking {
    func start(interval: TimeInterval, work: @escaping () -> Void) {}
    func stop() {}
}

private final class StillWake: DriftWakeObserving {
    func start(onWake: @escaping () -> Void) {}
    func stop() {}
}
