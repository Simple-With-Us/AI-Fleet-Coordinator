import AppKit
import SwiftUI
import XCTest

/// EXPERIMENT, never merged (draft PR "Experiment: Muster Window Overflow Probe").
///
/// Measures which construct makes the main window's NavigationSplitView overflow the window
/// under the original overlay root, and whether PR 1433's column cap keeps it inside.  Every
/// test only prints greppable `PROBE` / `PROBEMIN` / `PROBEENV` lines;  nothing here asserts,
/// so the job always finishes and logs everything.
///
/// Roots:
/// - `A`:  main before PR 1433, a sized `Color.clear` with the split view in an overlay.
/// - `B`:  PR 1433, the split view sized by `musterWindowSize`, each column `musterColumnHeight`.
/// - `C`:  the overlay root of `A` with PR 1433's column cap (the cap alone).
/// - `W`:  PR 1433's root frame without the column cap (the root change alone).
/// - `U`:  the bare split view, uncapped (the root before PR 1414).
@MainActor
final class MusterOverflowProbeTests: XCTestCase {
    enum Root: String, CaseIterable {
        case A, B, C, W, U
    }

    enum Kind: String, CaseIterable {
        /// The PR 1433 harness's shape:  a fixed-height strip over a tall plain ScrollView.
        case baseline
        /// The real PlaceStrip with Seat Setup's source line, over a tall ScrollView.
        case stripSeatSetup
        /// The real PlaceStrip with Tools And Worker Health's (longest) source line.
        case stripTools
        /// Candidate product fix:  the strip's line with lineLimit(1...3) instead of fixedSize.
        case stripSeatSetupLimited
        /// A FailureBanner with a long reason (vertical fixedSize) above the ScrollView.
        case banner
        /// Apps & Work's shape:  a VStack with a fixedSize problem label over a List.
        case appsAndWork
        /// The root-level bottom safeAreaInset that opsActionSheet adds.
        case bottomInset
        /// Content that grows after the first layout.
        case grow
        /// An expanded DisclosureGroup inside the ScrollView.
        case disclosure
        /// TextField, menu Picker, fixedSize segmented Picker and ProgressView inside the ScrollView.
        case controls
        /// A horizontal ScrollView of selectable monospaced text inside the vertical one.
        case nestedHorizontal
        /// Seat Setup's real nesting:  strip, ScrollView, chooser, cards, disclosure, embedded section.
        case seatSetupComposite
        /// The baseline in a window with an NSToolbar and toolbar and title bridging.
        case chromeBaseline
        /// The Seat Setup strip in a window with an NSToolbar and bridging.
        case chromeStripSeatSetup
        /// Seat Setup composite, banner, bottom inset, sheets and chrome together.
        case everything

        var wantsChrome: Bool {
            switch self {
            case .chromeBaseline, .chromeStripSeatSetup, .everything: return true
            default: return false
            }
        }

        var wantsBottomInset: Bool { self == .bottomInset || self == .everything }
        var wantsSheets: Bool { self == .everything }
    }

    enum Lines {
        static let seatSetup = "One seat's setup on the Mac, for any platform:  the engine's answers and the files behind them."
        static let tools = "The doctor's report on the Mac, which covers the Mac and the server.\u{00A0} Worker Health is the Worker's own file, read on the Mac."
        static let paragraph = String(repeating: "A paragraph that wraps at the column's width. ", count: 8)
        static let reason = String(repeating: "engine: could not read the manifest at the seat's path. ", count: 6)
    }

    // MARK: - the views

    struct ProbeCap: ViewModifier {
        let on: Bool
        @ViewBuilder func body(content: Content) -> some View {
            if on { content.musterColumnHeight() } else { content }
        }
    }

    struct ProbeBottomInset: ViewModifier {
        let on: Bool
        @ViewBuilder func body(content: Content) -> some View {
            if on {
                content.safeAreaInset(edge: .bottom, spacing: 0) {
                    Text(String(repeating: "A hidden run is still going. ", count: 12))
                        .lineLimit(2)
                        .padding(8)
                        .frame(maxWidth: .infinity)
                        .background(.bar)
                }
            } else {
                content
            }
        }
    }

    struct ProbeSheets: ViewModifier {
        let on: Bool
        @ViewBuilder func body(content: Content) -> some View {
            if on {
                content
                    .sheet(isPresented: .constant(false)) { Text("Plan") }
                    .sheet(isPresented: .constant(false)) { Text("Action") }
            } else {
                content
            }
        }
    }

    struct ProbeSidebar: View {
        var body: some View {
            List {
                Section {
                    Label("Overview", systemImage: "square.grid.2x2")
                }
                Section("The Mac") {
                    ForEach(1..<10, id: \.self) { index in
                        Label("Mac Place \(index)", systemImage: "desktopcomputer")
                            .badge(index == 2 ? 3 : 0)
                    }
                }
                Section("The Server") {
                    ForEach(10..<15, id: \.self) { index in
                        Label("Server Place \(index)", systemImage: "server.rack")
                    }
                }
                Section("Across The Fleet") {
                    ForEach(15..<18, id: \.self) { index in
                        Label("Fleet Place \(index)", systemImage: "globe")
                    }
                }
            }
            .listStyle(.sidebar)
        }
    }

    /// PlaceStrip, frame for frame.
    struct ProbeStrip: View {
        let source: String
        let fixed: Bool

        var body: some View {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Label("The Mac", systemImage: "desktopcomputer")
                    .font(.callout.weight(.semibold))
                if fixed {
                    Text(source)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                } else {
                    Text(source)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1...3)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 8)
            .accessibilityElement(children: .combine)
        }
    }

    /// FailureBanner as it is on main, with the full reason.
    struct ProbeBanner: View {
        var body: some View {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                VStack(alignment: .leading, spacing: 2) {
                    Text("The engine did not answer, so every cell reads unknown.")
                        .font(.callout.weight(.medium))
                    Text(Lines.reason)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 10)
            .background(Color.red.opacity(0.10))
        }
    }

    struct PlainStrip: View {
        var body: some View {
            Text("Strip").frame(height: 33)
        }
    }

    struct TallScroll: View {
        var body: some View {
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

    struct GrowScroll: View {
        @State private var tall = false

        var body: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text("First")
                    ForEach(0..<(tall ? 12 : 1), id: \.self) { _ in
                        Text(Lines.paragraph)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Color.gray.frame(height: tall ? 2000 : 100)
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .task {
                try? await Task.sleep(nanoseconds: 200_000_000)
                tall = true
            }
        }
    }

    struct DisclosureScroll: View {
        var body: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    DisclosureGroup("Rules", isExpanded: .constant(true)) {
                        Color.gray.frame(height: 1500)
                    }
                    Color.gray.frame(height: 500)
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    struct ControlsScroll: View {
        @State private var text = ""
        @State private var seat = 0
        @State private var host = 0

        var body: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    TextField("Find", text: $text)
                    Picker("Seat", selection: $seat) {
                        Text("CLAUDE").tag(0)
                        Text("MM").tag(1)
                    }
                    .frame(maxWidth: 220)
                    Picker("Host", selection: $host) {
                        Text("The Mac").tag(0)
                        Text("The Server").tag(1)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                    ProgressView()
                    Color.gray.frame(height: 2000)
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    struct NestedHorizontalScroll: View {
        var body: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    GroupBox {
                        ScrollView(.horizontal) {
                            Text(String(repeating: "a line of a config file, long enough to scroll sideways, and then some more\n", count: 300))
                                .font(.system(size: 11, design: .monospaced))
                                .textSelection(.enabled)
                        }
                    }
                    Color.gray.frame(height: 500)
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    struct AppsAndWorkLike: View {
        @State private var filter = ""
        @State private var scope = 0

        var body: some View {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Text("Apps & Work").font(.title2)
                    Spacer()
                    TextField("Filter", text: $filter).frame(width: 200)
                }
                Picker("", selection: $scope) {
                    Text("All").tag(0)
                    Text("Mine").tag(1)
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 460)
                Label(String(repeating: "The registry could not be read. ", count: 6), systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                List {
                    ForEach(0..<80, id: \.self) { Text("Row \($0)") }
                }
                .listStyle(.inset)
            }
            .padding(24)
        }
    }

    /// SectionPanel, frame for frame.
    struct ProbePanel<Content: View>: View {
        let title: String
        let subtitle: String
        let content: Content

        init(title: String, subtitle: String, @ViewBuilder content: () -> Content) {
            self.title = title
            self.subtitle = subtitle
            self.content = content()
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary)
                }
                content
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12).strokeBorder(.quaternary, lineWidth: 1)
            }
        }
    }

    struct SeatSetupLike: View {
        @State private var seat = "CLAUDE"
        @State private var host = 0

        var body: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Picker("Seat", selection: $seat) {
                            ForEach(["CLAUDE", "MM", "CODEX"], id: \.self) { tag in
                                Text(tag).tag(tag)
                            }
                        }
                        .frame(maxWidth: 220)
                        Picker("Host", selection: $host) {
                            Text("The Mac").tag(0)
                            Text("The Server").tag(1)
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .fixedSize()
                        Text("runs on Claude Code")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Button("Run The Engine Again") {}
                    }
                    ForEach(0..<6, id: \.self) { index in
                        ProbePanel(title: "Kind \(index)", subtitle: "Seat CLAUDE, on the Mac") {
                            Text(Lines.paragraph)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                            DisclosureGroup("Rules", isExpanded: .constant(index == 0)) {
                                ForEach(0..<8, id: \.self) { row in
                                    Text("Rule \(row)")
                                }
                            }
                        }
                    }
                    ProbePanel(title: "MiniMax", subtitle: "Embedded, without its own ScrollView") {
                        ForEach(0..<10, id: \.self) { _ in
                            Text(Lines.paragraph)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.horizontal, 22)
                .padding(.vertical, 18)
            }
        }
    }

    struct ProbeDetail: View {
        let kind: Kind

        var body: some View {
            VStack(spacing: 0) {
                switch kind {
                case .baseline, .chromeBaseline, .bottomInset:
                    PlainStrip()
                    Divider()
                    TallScroll()
                case .stripSeatSetup, .chromeStripSeatSetup:
                    ProbeStrip(source: Lines.seatSetup, fixed: true)
                    Divider()
                    TallScroll()
                case .stripTools:
                    ProbeStrip(source: Lines.tools, fixed: true)
                    Divider()
                    TallScroll()
                case .stripSeatSetupLimited:
                    ProbeStrip(source: Lines.seatSetup, fixed: false)
                    Divider()
                    TallScroll()
                case .banner:
                    PlainStrip()
                    Divider()
                    ProbeBanner()
                    TallScroll()
                case .appsAndWork:
                    PlainStrip()
                    Divider()
                    AppsAndWorkLike()
                case .grow:
                    PlainStrip()
                    Divider()
                    GrowScroll()
                case .disclosure:
                    PlainStrip()
                    Divider()
                    DisclosureScroll()
                case .controls:
                    PlainStrip()
                    Divider()
                    ControlsScroll()
                case .nestedHorizontal:
                    PlainStrip()
                    Divider()
                    NestedHorizontalScroll()
                case .seatSetupComposite:
                    ProbeStrip(source: Lines.seatSetup, fixed: true)
                    Divider()
                    SeatSetupLike()
                case .everything:
                    ProbeStrip(source: Lines.seatSetup, fixed: true)
                    Divider()
                    ProbeBanner()
                    SeatSetupLike()
                }
            }
        }
    }

    struct ProbeRoot: View {
        let root: Root
        let kind: Kind

        var body: some View {
            switch root {
            case .A:
                Color.clear
                    .frame(minWidth: MusterWindowLayout.minWidth, idealWidth: MusterWindowLayout.minWidth,
                           maxWidth: .infinity,
                           minHeight: MusterWindowLayout.minHeight, idealHeight: MusterWindowLayout.idealHeight,
                           maxHeight: .infinity)
                    .overlay { split(capped: false) }
            case .C:
                Color.clear
                    .frame(minWidth: MusterWindowLayout.minWidth, idealWidth: MusterWindowLayout.minWidth,
                           maxWidth: .infinity,
                           minHeight: MusterWindowLayout.minHeight, idealHeight: MusterWindowLayout.idealHeight,
                           maxHeight: .infinity)
                    .overlay { split(capped: true) }
            case .B:
                split(capped: true)
                    .musterWindowSize()
            case .W:
                split(capped: false)
                    .musterWindowSize()
            case .U:
                split(capped: false)
            }
        }

        private func split(capped: Bool) -> some View {
            NavigationSplitView {
                ProbeSidebar()
                    .modifier(ProbeCap(on: capped))
                    .navigationTitle("Muster")
                    .navigationSplitViewColumnWidth(min: 200, ideal: 230, max: 300)
            } detail: {
                ProbeDetail(kind: kind)
                    .modifier(ProbeCap(on: capped))
                    .navigationTitle("Seat Setup")
                    .navigationSubtitle("On The Mac")
                    .toolbar {
                        ToolbarItem {
                            Button {} label: { Label("Check Seat Setup", systemImage: "checkmark.shield") }
                        }
                    }
            }
            .modifier(ProbeBottomInset(on: kind.wantsBottomInset))
            .modifier(ProbeSheets(on: kind.wantsSheets))
        }
    }

    // MARK: - measuring

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

    private func windowRect(_ view: NSView) -> NSRect {
        view.convert(view.bounds, to: nil)
    }

    private func documentTop(_ scroll: NSScrollView) -> CGFloat? {
        guard let doc = scroll.documentView else { return nil }
        return doc.convert(NSPoint(x: 0, y: doc.isFlipped ? doc.bounds.minY : doc.bounds.maxY), to: nil).y
    }

    private func environmentLine(_ name: String) {
        let screen = NSScreen.main
        let frame: String = screen.map { fmt($0.frame) } ?? "none"
        let visible: String = screen.map { fmt($0.visibleFrame) } ?? "none"
        let os = ProcessInfo.processInfo.operatingSystemVersionString.replacingOccurrences(of: " ", with: "_")
        print("PROBEENV test=\(name) screen=\(frame) visible=\(visible) os=\(os)")
    }

    private func probe(_ kind: Kind, _ root: Root, height: CGFloat) {
        _ = NSApplication.shared
        let size = NSSize(width: MusterWindowLayout.minWidth, height: height)
        let window = NSWindow(contentRect: NSRect(origin: NSPoint(x: 20, y: 20), size: size),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let hosting = NSHostingController(rootView: ProbeRoot(root: root, kind: kind))
        hosting.sizingOptions = [.minSize]
        if kind.wantsChrome {
            hosting.sceneBridgingOptions = [.toolbars, .title]
            window.toolbar = NSToolbar(identifier: "probe.\(kind.rawValue).\(root.rawValue).\(Int(height))")
            window.toolbarStyle = .unified
        }
        window.contentViewController = hosting
        window.setContentSize(size)
        window.orderFrontRegardless()
        window.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(kind == .grow ? 1.2 : 0.6))
        window.layoutIfNeeded()
        measure(kind, root, requested: height, window, hosting)
        window.orderOut(nil)
        window.close()
    }

    private func measure(_ kind: Kind, _ root: Root, requested: CGFloat, _ window: NSWindow,
                         _ hosting: NSHostingController<ProbeRoot>) {
        let head = "PROBE variant=\(kind.rawValue) root=\(root.rawValue) req=\(Int(requested))"
        guard let content = window.contentView else {
            print("\(head) content=none")
            return
        }
        let contentHeight = content.bounds.height
        let layoutTop = window.contentLayoutRect.maxY
        let views = descendants(content)
        let scrolls = views.compactMap { $0 as? NSScrollView }.filter { !$0.isHidden }
        let detail = scrolls
            .filter { windowRect($0).minX >= 150 }
            .max { ($0.documentView?.frame.height ?? 0) < ($1.documentView?.frame.height ?? 0) }
        let sidebar = scrolls
            .filter { windowRect($0).minX < 150 }
            .max { windowRect($0).height < windowRect($1).height }
        let split = views.compactMap { $0 as? NSSplitView }.first
        let splitRect = split.map { windowRect($0) }
        var columns = "none"
        if let split {
            var described: [String] = []
            for column in split.arrangedSubviews {
                let rect: NSRect = windowRect(column)
                let fit: NSSize = column.fittingSize
                described.append("\(fmt(rect))fit\(fmt(fit))")
            }
            columns = described.joined(separator: ";")
        }

        var reasons: [String] = []
        if let splitRect {
            if splitRect.maxY > contentHeight + 1 { reasons.append("splitAboveTop") }
            if splitRect.minY < -1 { reasons.append("splitBelowBottom") }
        }
        var detailText = "scrollFrame=none docTop=none docH=none visH=none inset=none scrollable=none"
        if let detail {
            let frame = windowRect(detail)
            let top = documentTop(detail)
            let docHeight = detail.documentView?.frame.height ?? 0
            let visible = detail.contentView.bounds.height
            if let top, top > layoutTop + 1 { reasons.append("docTopAboveLayoutTop") }
            if frame.maxY > contentHeight + 1 || frame.minY < -1 { reasons.append("scrollOutsideWindow") }
            let topText: String = top.map { fmt($0) } ?? "none"
            let scrollable: String = docHeight > visible + 1 ? "yes" : "no"
            let parts: [String] = [
                "scrollFrame=\(fmt(frame))",
                "docTop=\(topText)",
                "docH=\(fmt(docHeight))",
                "visH=\(fmt(visible))",
                "inset=\(fmt(detail.contentInsets.top))",
                "scrollable=\(scrollable)",
            ]
            detailText = parts.joined(separator: " ")
        }
        var sidebarText = "sbFrame=none sbRow0=none"
        if let sidebar {
            var row0 = "none"
            let tables: [NSTableView] = descendants(sidebar).compactMap { $0 as? NSTableView }
            if let table = tables.first, table.numberOfRows > 0 {
                let rect = table.convert(table.rect(ofRow: 0), to: nil)
                row0 = fmt(rect)
                if rect.maxY > layoutTop + 1 { reasons.append("sidebarRow0AboveLayoutTop") }
            }
            let sbTop: String = documentTop(sidebar).map { fmt($0) } ?? "none"
            sidebarText = "sbFrame=\(fmt(windowRect(sidebar))) sbDocTop=\(sbTop) sbRow0=\(row0)"
        }
        let overflow: String = reasons.isEmpty ? "no" : "yes(\(reasons.joined(separator: "+")))"
        let fitting = hosting.view.fittingSize
        let minimum = hosting.sizeThatFits(in: .zero)
        let splitText: String = splitRect.map { fmt($0) } ?? "none"
        let parts: [String] = [
            head,
            "content=\(fmt(content.bounds.size))",
            "layoutTop=\(fmt(layoutTop))",
            "minSize=\(fmt(window.contentMinSize))",
            "fitting=\(fmt(fitting))",
            "hostMin=\(fmt(minimum))",
            "split=\(splitText)",
            "cols=\(columns)",
            detailText,
            sidebarText,
            "overflow=\(overflow)",
        ]
        print(parts.joined(separator: " "))
    }

    // MARK: - the probes

    /// Each detail's and each root's own minimum, with no window.
    func test1_IsolatedMinimums() {
        environmentLine("isolated")
        for kind in Kind.allCases {
            let host = NSHostingController(rootView: ProbeDetail(kind: kind))
            let zero = host.sizeThatFits(in: .zero)
            let narrow = host.sizeThatFits(in: NSSize(width: 900, height: 0))
            let fitting = host.view.fittingSize
            print("PROBEMIN variant=\(kind.rawValue) part=detail zero=\(fmt(zero)) w900=\(fmt(narrow)) fitting=\(fmt(fitting))")
        }
        let strips: [(String, String)] = [("seatSetup", Lines.seatSetup), ("tools", Lines.tools)]
        for (name, line) in strips {
            for fixed in [true, false] {
                let host = NSHostingController(rootView: ProbeStrip(source: line, fixed: fixed))
                let label = fixed ? "fixed" : "limited"
                let zero = host.sizeThatFits(in: .zero)
                let narrow = host.sizeThatFits(in: NSSize(width: 900, height: 0))
                let fitting = host.view.fittingSize
                print("PROBEMIN variant=strip-\(name)-\(label) part=strip zero=\(fmt(zero)) w900=\(fmt(narrow)) fitting=\(fmt(fitting))")
            }
        }
        let rootKinds: [Kind] = [.baseline, .stripSeatSetup, .stripTools, .seatSetupComposite]
        for root in Root.allCases {
            for kind in rootKinds {
                let host = NSHostingController(rootView: ProbeRoot(root: root, kind: kind))
                let zero = host.sizeThatFits(in: .zero)
                let fitting = host.view.fittingSize
                print("PROBEMIN variant=\(kind.rawValue) part=root root=\(root.rawValue) zero=\(fmt(zero)) fitting=\(fmt(fitting))")
            }
        }
    }

    /// Every variant under every root, in a 1160 x 700 window.
    func test2_AllVariantsAt700() {
        environmentLine("all700")
        for kind in Kind.allCases {
            for root in Root.allCases {
                probe(kind, root, height: 700)
            }
        }
    }

    /// The strip and Seat Setup variants in taller windows, near the owner's screen and above it.
    func test3_WorstVariantsTall() {
        environmentLine("tall")
        let kinds: [Kind] = [.baseline, .stripSeatSetup, .stripTools, .seatSetupComposite, .everything]
        for height in [CGFloat(880), CGFloat(1300)] {
            for kind in kinds {
                for root in [Root.A, .B, .C, .W] {
                    probe(kind, root, height: height)
                }
            }
        }
    }
}
