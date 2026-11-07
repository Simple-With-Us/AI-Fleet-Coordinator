import SwiftUI

/// The Seat Setup panel:  what one seat's setup looks like on the Mac, for any platform.
///
/// A seat is chosen at the top and the panel shows the same things for every one of them:
/// what needs a look, the engine's answer for each of the six config kinds it checks
/// (instructions, hooks, MCP, skills, sub-agents and permissions), and under each kind the
/// rules behind that answer with the file each rule is about, read as it is on this Mac.
/// Which file belongs to which seat comes from the engine's manifest, so a platform with a
/// different layout needs nothing here.
///
/// It covers the Mac only, it never edits, and a file that is absent or cannot be read says
/// so and never reads as empty.  MiniMax keeps the extra sections it already had (plugins,
/// config.yaml, channels and agents) under its own seat, because those are files only its
/// runtime has.
struct SeatSetupView: View {
    @EnvironmentObject private var store: FleetStore
    /// The seat chosen last, so the panel opens where it was left.
    @AppStorage("musterSeatSetupSeat") private var storedSeat = ""
    /// The artifacts each kind's rules are about, for the seat they were read for.
    @State private var artifacts: [ConfigKind: FleetStore.CellArtifacts] = [:]
    @State private var artifactsSeat: String?
    @State private var open: Set<ConfigKind> = []
    /// What the platform has installed, for the seat it was read for.
    @State private var inventory: SeatInventoryReading?
    @State private var inventorySeat: String?
    @State private var inventoryHost: SeatSetupHost?
    /// When the reading shown was read, when it is one that succeeded.
    @State private var inventoryReadAt: Date?
    /// The seat and host of each inventory read that is running, once per read.  A read the
    /// panel has moved on from still runs to its end, so this is a list and not one flag.
    @State private var inventoryReads: [SeatInventoryCache.Key] = []
    /// Whether the reader is running for the seat and host shown.
    private var readingInventory: Bool {
        guard let seat else { return false }
        return inventoryReads.contains(SeatInventoryCache.Key(seat: seat, host: host))
    }
    @State private var openInventory: Set<String> = []
    /// Which host's setup is shown.  The engine's answers are the Mac's only, so the server's
    /// side is what the platform has installed there, read over the restricted route.
    @State private var host: SeatSetupHost = .mac

    /// MiniMax's seat tag, the one seat with extra files of its own to show.
    private static let miniMaxSeat = "MM"

    private var seat: String? {
        SeatSetup.resolvedSeat(stored: storedSeat, roster: store.roster)
    }

    /// Whether the engine has finished a run.  Until it has, the grid's cells are empty UNKNOWN
    /// placeholders and say nothing about the seat.
    private var answered: Bool { store.phase == .loaded }

    private var cells: [GridCell] {
        guard let seat else { return [] }
        return ConfigKind.columnOrder.compactMap { store.cell(seat: seat, kind: $0) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                chooser
                if let seat {
                    switch host {
                    case .mac:
                        attention(seat)
                        answers(seat)
                        ForEach(ConfigKind.columnOrder, id: \.self) { kind in
                            kindPanel(kind, seat: seat)
                        }
                        if seat != Self.miniMaxSeat {
                            installed(seat)
                        }
                        if seat == Self.miniMaxSeat {
                            MiniMaxDetailView(embedded: true)
                        }
                    case .server:
                        SectionPanel(title: "The Server", subtitle: "Seat \(seat), on the server") {
                            Text("The engine checks the Mac only today, so it has no answers for the server.  This shows what the seat's platform has installed there, as names and counts.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        installed(seat)
                    }
                } else {
                    EmptyNote(text: "The engine has no seats to show yet.  Run it from the Compliance Grid or the Overview.")
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        // The files are read again when the seat or host changes and when an engine run lands,
        // so a panel opened before the engine answered fills in when it does.  The key is the
        // run's stamp and not its phase:  the phase moves twice in a run (running, then
        // loaded), and each move used to run the platform inventory again.  Whether the
        // inventory is read at all is SeatInventoryCache's call.
        .task(id: loadKey) { await loadArtifacts() }
    }

    /// What the panel's reading is keyed on:  the seat, the host and the engine run.
    private var loadKey: String {
        let run = store.generatedAt.map { String($0.timeIntervalSince1970) } ?? "none"
        return "\(seat ?? "")/\(host.rawValue)/\(run)"
    }

    // MARK: - the seat

    private var chooser: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Picker("Seat", selection: seatBinding) {
                ForEach(store.roster, id: \.self) { tag in
                    Text(tag).tag(tag)
                }
            }
            .frame(maxWidth: 220)
            .disabled(store.roster.isEmpty)
            Picker("Host", selection: $host) {
                ForEach(SeatSetupHost.allCases) { item in
                    Text(item.title).tag(item)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            if let platform = SeatSetup.platform(of: cells) {
                Text("runs on \(platform)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            Button("Run The Engine Again") { store.refresh() }
                .disabled(store.isRunning)
                .help("Checks every seat's setup on the Mac again.")
        }
    }

    private var seatBinding: Binding<String> {
        Binding(
            get: { seat ?? "" },
            set: { storedSeat = $0 }
        )
    }

    private var engineLine: String {
        switch store.phase {
        case .idle:
            return "The engine has not run yet."
        case .running:
            return "The engine is running."
        case .loaded:
            guard let at = store.generatedAt else { return "The engine answered." }
            return "The engine last answered \(FleetTime.ageText(since: at)) ago."
        case .failed(let reason):
            return "The last engine run failed:  \(reason)"
        }
    }

    // MARK: - what needs a look

    private func attention(_ seat: String) -> some View {
        let items = SeatSetup.attention(for: cells, answered: answered)
        return SectionPanel(title: "What Needs A Look", subtitle: "Seat \(seat), on the Mac") {
            VStack(alignment: .leading, spacing: 8) {
                if !answered && !cells.contains(where: { !$0.rules.isEmpty }) {
                    // Until a run finishes, the grid holds an empty UNKNOWN cell for every kind.
                    // That is silence and not a missing rule, so nothing is raised.  Cells that
                    // carry rules from an earlier run are still shown below.
                    Text("The engine has not answered for this seat yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if items.isEmpty {
                    Label {
                        Text("Nothing the engine checked on this seat is missing, drifting, unproven or unreadable.")
                            .font(.callout)
                    } icon: {
                        Image(systemName: "checkmark.circle")
                            .accessibilityHidden(true)
                    }
                } else {
                    ForEach(items, id: \.kind) { item in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                                .accessibilityHidden(true)
                            Text("\(item.kind.title), \(item.state):  \(item.text)")
                                .font(.callout)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                    }
                }
                Text(engineLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - the engine's answers

    private func answers(_ seat: String) -> some View {
        SectionPanel(title: "The Engine's Answers", subtitle: "Seat \(seat) on the Mac, one badge per config kind") {
            if cells.isEmpty {
                EmptyNote(text: "The engine has no answer for seat \(seat) yet.")
            } else {
                HStack(spacing: 10) {
                    ForEach(cells) { cell in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(cell.kind.title)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            CellBadge(cell: cell, width: 124)
                        }
                    }
                }
                .padding(.top, 4)
            }
        }
    }

    // MARK: - one kind, with its rules and files

    private func kindPanel(_ kind: ConfigKind, seat: String) -> some View {
        let cell = store.cell(seat: seat, kind: kind)
        return FoldedPanel(
            title: kind.title,
            summary: SeatSetup.summary(for: cell, answered: answered),
            expanded: openBinding(kind)
        ) {
            kindDetail(kind, seat: seat, cell: cell)
        }
    }

    @ViewBuilder
    private func kindDetail(_ kind: ConfigKind, seat: String, cell: GridCell?) -> some View {
        if cell == nil {
            EmptyNote(text: SeatSetup.summary(for: nil, answered: answered))
        } else if let found = artifacts[kind], artifactsSeat == seat {
            if found.rules.isEmpty {
                EmptyNote(text: answered
                    ? (found.coverReason ?? "No rule covers this seat and kind.")
                    : SeatSetup.waitingForEngine)
            } else {
                let sources = store.ruleSources(seat: seat, kind: kind)
                VStack(alignment: .leading, spacing: 20) {
                    ForEach(Array(found.rules.enumerated()), id: \.offset) { index, rule in
                        RuleArtifactView(
                            rule: rule,
                            view: found.views.indices.contains(index) ? found.views[index] : nil,
                            source: sources[rule.ruleId]
                        )
                        if index < found.rules.count - 1 { Divider() }
                    }
                }
            }
        } else {
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                Text("Reading the files on the Mac.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - what is installed

    /// What the seat's platform has installed:  MCP servers, hooks, plugins, skills and so on,
    /// as names and counts.  MiniMax's seat has its own, richer section below instead.
    @ViewBuilder
    private func installed(_ seat: String) -> some View {
        SectionPanel(title: "What Is Installed", subtitle: "Names and counts only, read \(host.inProse)") {
            if inventorySeat != seat || inventoryHost != host {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text("Reading what the platform has installed.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    switch inventory {
                    case .some(.loaded(let found)):
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(found.sections) { section in
                                inventorySection(section)
                            }
                        }
                    case .some(.unavailable(let why)):
                        EmptyNote(text: why)
                    case .none:
                        EmptyNote(text: "No platform is known for this seat, so nothing was read.")
                    }
                    if inventory != nil {
                        inventoryFooter
                    }
                }
            }
        }
    }

    /// When the reading was read, and the button that reads it now.
    private var inventoryFooter: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            if readingInventory {
                ProgressView()
                    .controlSize(.small)
                Text("Reading again.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if let readAt = inventoryReadAt {
                Text(SeatInventoryCache.readLine(readAt: readAt))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            Button("Read Again") {
                Task { await readInventoryNow() }
            }
            // While the engine runs, no platform is known for the seat, so nothing could be read.
            .disabled(readingInventory || store.isRunning)
            .help("Reads what the platform has installed \(host.inProse) now.")
        }
    }

    private func inventorySection(_ section: SeatInventory.Section) -> some View {
        let isOpen = Binding<Bool>(
            get: { openInventory.contains(section.id) },
            set: { shown in
                if shown { openInventory.insert(section.id) } else { openInventory.remove(section.id) }
            }
        )
        return DisclosureGroup(isExpanded: isOpen) {
            VStack(alignment: .leading, spacing: 3) {
                if section.items.isEmpty {
                    Text(section.note ?? "Nothing listed.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else {
                    // Rows are told apart by position:  two entries can share a name (a skill
                    // folder and a `.md` file of the same stem both read as one name).
                    ForEach(Array(section.items.enumerated()), id: \.offset) { _, item in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(item.name)
                                .font(.system(size: 12, design: .monospaced))
                                .textSelection(.enabled)
                            if let detail = item.detail {
                                Text(detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                        }
                    }
                    if let note = section.note {
                        Text(note)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.top, 4)
                    }
                }
                Text(section.source)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .padding(.top, 4)
            }
            .padding(.top, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(section.title)
                    .font(.subheadline.weight(.semibold))
                Text(section.summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func openBinding(_ kind: ConfigKind) -> Binding<Bool> {
        Binding(
            get: { open.contains(kind) },
            set: { shown in
                if shown { open.insert(kind) } else { open.remove(kind) }
            }
        )
    }

    // MARK: - reading

    private func loadArtifacts() async {
        guard let seat else {
            artifacts = [:]
            artifactsSeat = nil
            return
        }
        let fleetStore = store
        let wanted = ConfigKind.columnOrder.compactMap { fleetStore.cell(seat: seat, kind: $0) }
        let readHost = host
        let platform = SeatSetup.platform(of: wanted)

        // The server's side is the platform's inventory over the restricted read-only route.
        // The platform is the one the engine's rules name for this seat.
        if readHost == .server {
            await loadInventory(seat: seat, host: readHost, platform: platform, force: false)
            return
        }

        let loaded = await Task.detached(priority: .userInitiated) {
            var byKind: [ConfigKind: FleetStore.CellArtifacts] = [:]
            for cell in wanted { byKind[cell.kind] = fleetStore.artifacts(for: cell) }
            return byKind
        }.value
        guard !Task.isCancelled else { return }
        artifacts = loaded
        artifactsSeat = seat

        // What the platform has installed is read once the engine's files are shown, so the
        // slower read never holds the panel up.  MiniMax's seat has its own section.
        guard platform != "minimax" else {
            showInventory(nil, readAt: nil, seat: seat, host: readHost)
            return
        }
        await loadInventory(seat: seat, host: readHost, platform: platform, force: false)
    }

    /// The Read Again button:  reads the seat and host shown now, whatever is held.
    private func readInventoryNow() async {
        guard let seat else { return }
        let wanted = ConfigKind.columnOrder.compactMap { store.cell(seat: seat, kind: $0) }
        await loadInventory(seat: seat, host: host, platform: SeatSetup.platform(of: wanted), force: true)
    }

    /// What the platform has installed, from the reading held for this seat and host when it is
    /// still good (SeatInventoryCache.decide), and from the reader otherwise.
    private func loadInventory(seat: String, host readHost: SeatSetupHost, platform: String?, force: Bool) async {
        let cacheStore = SeatInventoryCacheStore.shared
        let held = cacheStore.cache.entry(seat: seat, host: readHost)

        // While an engine run is in flight the cells carry no rules, so no platform is known.
        // A reading already held for this seat and host stays on screen until the run lands.
        guard let platform else {
            if let held {
                showInventory(held.reading, readAt: held.readAt, seat: seat, host: readHost)
            } else if readHost == .server {
                showInventory(
                    .unavailable("No platform is known for this seat, so nothing was read on the server."),
                    readAt: nil, seat: seat, host: readHost
                )
            } else {
                showInventory(nil, readAt: nil, seat: seat, host: readHost)
            }
            return
        }

        let runStamp = store.generatedAt
        let decision = SeatInventoryCache.decide(
            held: held, host: readHost, platform: platform, runStamp: runStamp, now: Date(), force: force
        )
        if case .useHeld(let entry) = decision {
            showInventory(entry.reading, readAt: entry.readAt, seat: seat, host: readHost)
            return
        }

        let readKey = SeatInventoryCache.Key(seat: seat, host: readHost)
        inventoryReads.append(readKey)
        let reading: SeatInventoryReading
        switch readHost {
        case .server:
            reading = await ServerSeatInventoryReader().read(platform: platform)
        case .mac:
            let repoRoot = FleetConfigLocation.resolve().repoRoot
            reading = await MacSeatInventoryReader(repoRoot: repoRoot).read(platform: platform)
        }
        let readAt = Date()
        // A reading that came back is kept even when the panel has moved on, so it is not run
        // again on the way back.  A failed one is not kept.
        cacheStore.cache.record(
            reading, seat: seat, host: readHost, platform: platform, runStamp: runStamp, at: readAt
        )
        if let index = inventoryReads.firstIndex(of: readKey) {
            inventoryReads.remove(at: index)
        }
        guard !Task.isCancelled else { return }
        if case .loaded = reading {
            showInventory(reading, readAt: readAt, seat: seat, host: readHost)
        } else {
            showInventory(reading, readAt: nil, seat: seat, host: readHost)
        }
    }

    private func showInventory(_ reading: SeatInventoryReading?, readAt: Date?, seat: String, host readHost: SeatSetupHost) {
        inventory = reading
        inventoryReadAt = readAt
        inventorySeat = seat
        inventoryHost = readHost
    }
}
