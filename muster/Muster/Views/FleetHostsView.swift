import SwiftUI

/// The fleet hosts panel:  what the doctor's report says about each host.
///
/// The panel reads one file and asks nothing of the network:  the doctor's own report
/// at `~/.local/state/fleet-tools/report.json`.  A host the doctor could not reach
/// reads as unknown,  which is information and not a fault,  so the server shows as a
/// second host and not as a gap in the fleet.
///
/// The panel is read only.  It never runs the doctor and never writes the report.
///
/// Under the hosts sits the Worker Health card:  the Worker's Linear token health,  from
/// `~/.local/state/fleet-tools/worker-health.json`,  under the same forty-five minute rule.
///
/// Both files are read again every minute and when the Mac wakes,  and the page works their
/// age and verdict out again every thirty seconds,  so a report left open goes stale when it
/// crosses its window instead of keeping the answer it had when the page opened.
struct FleetHostsView: View {
    /// The last read of the doctor's report,  as the reader gave it.  It is aged on every tick.
    @State private var reading: FleetToolsReading?
    /// The Worker's own health file,  read beside the doctor's report,  and aged the same way.
    @State private var workerHealth: WorkerHealthReading?
    @State private var loading = false
    /// When the two files were last read.
    @State private var readAt: Date?
    /// When the Mac last woke,  so a read from before the sleep is not shown as current.
    @State private var wokeAt: Date?
    /// The host whose rows are open.
    @State private var expandedHost: String?
    /// Which category of checks to display inside an expanded host.
    @State private var categoryFilter: FleetHostCategoryFilter = .notable
    /// Search text to filter check rows inside an expanded host.
    @State private var searchText: String = ""

    private let hostWidth: CGFloat = 108
    private let reachWidth: CGFloat = 92
    private let countWidth: CGFloat = 74
    private let stateWidth: CGFloat = 108

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading && reading == nil {
                    Text("Reading the doctor's report\u{2026}")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let reading {
                    // The age and the verdict are worked out again on every tick from the clock,
                    // so a report that crosses its window between two reads stops reading as
                    // current at once.
                    TimelineView(.periodic(from: .now, by: ReportFreshness.tickInterval)) { context in
                        report(reading, health: workerHealth, now: context.date)
                    }
                } else {
                    Text("No report has been read yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .rereadsOnTickAndWake(onWake: { wokeAt = Date() }) {
            await load()
        }
    }

    /// The doctor's report and the Worker card at a moment.
    private func report(_ raw: FleetToolsReading, health: WorkerHealthReading?, now: Date) -> some View {
        let freshness = raw.freshness(now: now, readAt: readAt, wokeAt: wokeAt)
        let reading = raw.aged(by: freshness)
        return VStack(alignment: .leading, spacing: 16) {
            reportLine(reading, freshness: freshness)
            if let problem = reading.problem {
                problemBox(problem)
            } else {
                // Past its window,  or read before a wake,  every host reads unproven:  the
                // rows stay in view as what the report said,  and the note above says why.
                hostsSection(reading)
            }
            if let health {
                workerHealthCard(health, now: now)
            }
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(MusterDestination.hosts.title)
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Read The Mac's Reports Again", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
                .help("Reads the doctor's report and the Worker health file this Mac holds.\(ServerStatusCopy.sentenceGap)It runs no doctor and asks no host anything.")
            }
            Text("What the doctor's report says about each host's tools.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// The report itself:  who wrote it and how old it is at this moment,  and,  when it is
    /// not current,  what that means and what to do.
    ///
    /// A report that is out of its window gets no badge of its own:  the hosts carry
    /// the state,  and a red badge beside the note would be alarm the panel has no
    /// reason to raise.
    private func reportLine(_ reading: FleetToolsReading, freshness: ReportFreshness) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .top, spacing: 8) {
                if reading.problem == nil && reading.isCurrent {
                    FleetToolsStateBadge(state: reading.verdict, width: 92, checked: PassMeaning.toolsHost)
                }
                // The doctor writes this report on this Mac,  so its age is said as that,
                // never as when the page read it.  A report with a problem says it below.
                if reading.problem == nil {
                    ReportAgeText(origin: .doctorOnMac, freshness: freshness)
                }
                Spacer(minLength: 0)
            }
            Text(reading.path)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    /// Why the report could not be read at all.
    private func problemBox(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    // MARK: The hosts

    private func hostsSection(_ reading: FleetToolsReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Text("Hosts")
                    .font(.headline)
                Picker("Category", selection: $categoryFilter) {
                    ForEach(FleetHostCategoryFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .frame(maxWidth: 260)
                TextField("Filter checks by tool, category, or detail…", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: 260)
                Spacer()
            }
            if reading.hosts.isEmpty {
                Text("The report names no hosts.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                tableHeader
                ForEach(reading.hosts) { host in
                    hostRow(host)
                }
            }
        }
    }

    private var tableHeader: some View {
        HStack(spacing: 8) {
            Text("Host")
                .frame(width: hostWidth, alignment: .leading)
            Text("Reachable")
                .frame(width: reachWidth, alignment: .leading)
            Text("Failing")
                .frame(width: countWidth, alignment: .leading)
            Text("Unknown")
                .frame(width: countWidth, alignment: .leading)
            Text("Stale")
                .frame(width: countWidth, alignment: .leading)
            Text("Current")
                .frame(width: countWidth, alignment: .leading)
            Text("State")
                .frame(width: stateWidth, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.bottom, 2)
    }

    /// One host:  what it reads as,  and the rows behind it.
    private func hostRow(_ host: FleetToolsHostVerdict) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                expandedHost = expandedHost == host.host ? nil : host.host
            } label: {
                HStack(spacing: 8) {
                    HStack(spacing: 4) {
                        Image(systemName: expandedHost == host.host ? "chevron.down" : "chevron.right")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Text(host.host)
                            .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    }
                    .frame(width: hostWidth, alignment: .leading)

                    Text(host.reachable ? "yes" : "no")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(host.reachable ? Color.secondary : Color.indigo)
                        .frame(width: reachWidth, alignment: .leading)

                    Text(host.reachable ? "\(host.failing)" : "—")
                        .frame(width: countWidth, alignment: .leading)
                    Text(host.reachable ? "\(host.unknown)" : "—")
                        .frame(width: countWidth, alignment: .leading)
                    Text(host.reachable ? "\(host.stale)" : "—")
                        .frame(width: countWidth, alignment: .leading)
                    Text(host.reachable ? "\(host.current)" : "—")
                        .frame(width: countWidth, alignment: .leading)

                    FleetToolsStateBadge(state: host.state, width: stateWidth, checked: PassMeaning.toolsHost)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(host.reachable ? "Open the rows behind this host." : "Open what the doctor said about this host.")

            if let reason = host.reason {
                Text("The doctor could not read this host:  \(reason)")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            tipLine(host)
            if expandedHost == host.host {
                rows(host)
            }
        }
        .padding(.vertical, 3)
    }

    /// When this host was checked,  the tip the doctor compared against,  and the commit it
    /// ran from.
    private func tipLine(_ host: FleetToolsHostVerdict) -> some View {
        HStack(spacing: 10) {
            // The age of this host's own half of the report,  worked out at the tick:  when
            // the host's doctor checked it,  not when this page read the file.
            if let age = host.age {
                Text("checked \(age) ago")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .help("When this host's own half of the doctor's report was written.")
            }
            if let tip = host.tip {
                Text("tip \(tip)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                if let source = host.tipSource {
                    Text("read with \(source)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            if let doctor = host.doctor {
                Text("doctor \(doctor)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// The rows behind the counts,  filtered by category and search,  worst first.
    private func rows(_ host: FleetToolsHostVerdict) -> some View {
        let displayed = host.rows(matching: categoryFilter, search: searchText)
        return VStack(alignment: .leading, spacing: 3) {
            if displayed.isEmpty {
                Text(
                    host.reachable
                        ? (categoryFilter == .notable && searchText.isEmpty
                            ? "Every tool row on this host is current."
                            : "No check rows match \(categoryFilter.rawValue).")
                        : "The doctor read no row on this host."
                )
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            } else {
                ForEach(Array(displayed.enumerated()), id: \.offset) { _, row in
                    toolRow(row)
                }
            }
        }
        .padding(.leading, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One check row:  its name and state,  the version columns when the doctor read any
    /// (AFC-461),  and the doctor's own line under them.
    private func toolRow(_ row: FleetToolsRow) -> some View {
        let columns = ToolVersionColumns.make(for: row)
        return VStack(alignment: .leading, spacing: 1) {
            if let columns {
                if columns.needsOwnLines {
                    // A value too long for a column:  each value on its own line under
                    // the tool name,  kept whole.
                    rowTitle(row, showsInstalled: false)
                    versionLines(columns)
                } else {
                    // Beside the tool name when the pane has room,  and under it when not,
                    // so no value is ever truncated.
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .top, spacing: 8) {
                            rowTitle(row, showsInstalled: false)
                            Spacer(minLength: 12)
                            versionColumns(columns)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            rowTitle(row, showsInstalled: false)
                            versionColumns(columns)
                        }
                    }
                }
                if let basis = columns.basis {
                    Text(basis)
                        .font(.system(size: 10))
                        .foregroundStyle(columns.explainsStale ? tint(for: row.outcome) : Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .help(columns.basisHelp ?? "")
                }
            } else {
                HStack(spacing: 8) {
                    rowTitle(row, showsInstalled: true)
                    Spacer(minLength: 0)
                }
            }
            Text(row.summary)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 1)
    }

    /// A row's name,  category,  state and verdict note.
    ///
    /// The old installed field shows only on a row without version columns:  on a registry
    /// row it holds the same version the Installed column shows.
    private func rowTitle(_ row: FleetToolsRow, showsInstalled: Bool) -> some View {
        HStack(spacing: 8) {
            Text(row.check)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
            if let category = row.category, !category.isEmpty {
                Text(category)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.secondary.opacity(0.12), in: Capsule())
            }
            if showsInstalled, let installed = row.installed, !installed.isEmpty {
                Text(installed.count > 12 ? String(installed.prefix(7)) : installed)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Text(row.rowState.rawValue)
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(tint(for: row.outcome))
            if !row.countsInVerdict {
                Text("excluded from host verdict")
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// The three values in fixed-width columns,  so they line up from row to row.
    private func versionColumns(_ columns: ToolVersionColumns) -> some View {
        HStack(alignment: .top, spacing: CGFloat(ToolVersionColumns.columnSpacing)) {
            ForEach(columns.cells, id: \.label) { cell in
                VStack(alignment: .leading, spacing: 0) {
                    Text(cell.label)
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                    versionValue(cell)
                }
                .frame(width: CGFloat(ToolVersionColumns.columnWidth), alignment: .leading)
            }
        }
    }

    /// The three values one to a line,  for a value too long for a column.
    private func versionLines(_ columns: ToolVersionColumns) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            ForEach(columns.cells, id: \.label) { cell in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(cell.label)
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                        .frame(width: 64, alignment: .leading)
                    versionValue(cell)
                }
            }
        }
    }

    /// One value:  the version,  or "Not known" in the secondary colour.  The words carry
    /// the meaning and the colour only repeats them.  It wraps rather than truncates.
    private func versionValue(_ cell: ToolVersionCell) -> some View {
        Text(cell.text)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(cell.isKnown ? Color.primary : Color.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .help(cell.help ?? "")
    }

    // MARK: The Worker

    /// The Worker's Linear token health,  one row per seat.
    ///
    /// A file out of its window,  or a Worker the collector could not reach,  shows its
    /// reason and no seat rows:  rows from an old file are not current health evidence.
    ///
    /// The file is aged at `now`,  so the card goes stale while it is on screen when the
    /// file crosses its window.
    private func workerHealthCard(_ raw: WorkerHealthReading, now: Date) -> some View {
        let freshness = raw.freshness(now: now, readAt: readAt, wokeAt: wokeAt)
        let health = raw.aged(by: freshness)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text("Worker Health")
                    .font(.headline)
                if health.problem == nil && health.isCurrent {
                    FleetToolsStateBadge(state: health.state, width: 92)
                }
                Spacer()
                if let bundle = health.bundle, health.reachable {
                    Text("bundle \(bundle)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(bundle == "ok" ? Color.secondary : Color.red)
                }
            }
            // Who wrote the file,  and how old it is at this tick.  What a stale file means is
            // said in the box below,  so it is not said twice.
            if health.problem == nil, let line = ReportFreshnessCopy.ageLine(workerHealthOrigin, freshness) {
                Text(line)
                    .font(.caption)
                    .foregroundStyle(freshness.verdict.isCurrent ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("The Worker's Linear token for each hosted seat, from its signed health route.\(ServerStatusCopy.sentenceGap)The Worker runs hosted, on neither machine, and the Mac checks it every fifteen minutes.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let problem = health.problem {
                problemBox(problem)
            } else if freshness.verdict == .checking {
                Text("Reading this file again, because the Mac woke since it was read\u{2026}")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if !health.isCurrent {
                problemBox(workerStaleNote(freshness))
            } else if !health.reachable {
                problemBox("The collector could not reach the Worker: \(health.unreachableReason ?? "it did not say why").")
            } else if health.seats.isEmpty {
                Text("The Worker named no seats.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(health.seats) { seat in
                    workerSeatRow(seat)
                }
            }
            Text(health.path)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .padding(.top, 8)
    }

    /// Who writes the Worker's health file:  the Mac's own collector,  not the Worker.
    private var workerHealthOrigin: ReportOrigin { .macCollector("the Worker health collector") }

    /// What a Worker health file past its window means,  and when a new one comes.
    private func workerStaleNote(_ freshness: ReportFreshness) -> String {
        let opening = ReportFreshnessCopy.verdictNote(workerHealthOrigin, freshness)
            ?? "This file is not current, so its rows are old evidence, not current health."
        return "\(opening)\(ServerStatusCopy.sentenceGap)The collector on this Mac writes a new one every fifteen minutes."
    }

    private func workerSeatRow(_ seat: WorkerHealthSeat) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 8) {
                Text(seat.seat.isEmpty ? "unnamed" : seat.seat)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .frame(width: hostWidth, alignment: .leading)
                Text(seat.tokenText)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                if let kind = seat.lastMintFailKind, !kind.isEmpty {
                    Text("last mint failed: \(kind)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.orange)
                }
                Spacer(minLength: 0)
                FleetToolsStateBadge(state: seat.cellState, width: stateWidth)
            }
            if let note = seat.note, !note.isEmpty {
                Text(note)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
    }

    /// The colour a row's state carries,  from the app's own palette.
    private func tint(for outcome: FleetToolsRowOutcome) -> Color {
        switch outcome {
        case .failing: return .red
        case .stale: return .orange
        case .unknown: return .indigo
        case .passing: return .green
        case .absent: return .secondary
        }
    }

    // MARK: The read

    /// Reads both files again,  off the main thread.  A read already in flight is not doubled:
    /// the tick,  the wake and the button all land here.
    private func load() async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        let url = FleetToolsReportReader.reportURL()
        let healthURL = WorkerHealthReportReader.reportURL()
        let (result, health) = await Task.detached(priority: .userInitiated) {
            (FleetToolsReportReader.read(at: url), WorkerHealthReportReader.read(at: healthURL))
        }.value
        reading = result
        workerHealth = health
        readAt = Date()
        if expandedHost == nil {
            expandedHost = result.hosts.first?.host
        }
    }
}

/// One host's state,  in the grid's own badge style.
///
/// The state is the app's own vocabulary,  so the badge reads like every other badge:
/// green for a pass,  amber for a host that is behind,  indigo for a host nobody could
/// prove,  and red only for a host with a real failure.
struct FleetToolsStateBadge: View {
    let state: CellState
    var width: CGFloat = 108
    /// What this badge's check covered,  said in the tooltip.
    ///
    /// PASS means every check the badge's own rule asks for was proven,  and a panel
    /// whose badge checks something narrower than a config rule says what,  so the word
    /// does not mean one thing here and another in the grid.
    var checked: String?

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(state.tint)
                .frame(width: 7, height: 7)
            Text(state.shortLabel)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 7)
        .frame(width: width, height: 30)
        .background(state.background, in: RoundedRectangle(cornerRadius: 7))
        .overlay {
            RoundedRectangle(cornerRadius: 7)
                .strokeBorder(state.border, lineWidth: 1)
        }
        .contentShape(Rectangle())
        .help(helpText)
    }

    /// The tooltip:  what the badge checked when its panel says,  and otherwise that PASS
    /// covers this panel's own check and nothing else,  so no PASS in the app is a bare
    /// word and none of them claims a config rule was proven.
    private var helpText: String {
        if let checked { return "\(state.rawValue):  \(checked)" }
        return state == .passed ? PassMeaning.panelRow : ""
    }
}

#Preview {
    FleetHostsView()
}
