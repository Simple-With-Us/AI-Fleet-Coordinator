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
struct FleetHostsView: View {
    @State private var reading: FleetToolsReading?
    /// The Worker's own health file,  read beside the doctor's report.
    @State private var workerHealth: WorkerHealthReading?
    @State private var loading = false
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
                    Text("Reading the doctor's report…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let reading {
                    reportLine(reading)
                    if let problem = reading.problem {
                        problemBox(problem)
                    } else {
                        hostsSection(reading)
                    }
                    if let workerHealth {
                        workerHealthCard(workerHealth)
                    }
                } else {
                    Text("No report yet.  Refresh to read it.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load() }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Fleet Hosts")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("What the doctor's report says about each host's tools.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// The report itself:  how old it is,  and whether it can be believed.
    ///
    /// A report that is out of its window gets no badge of its own:  the hosts carry
    /// the state,  and a red badge beside "it may not be current" would be alarm the
    /// panel has no reason to raise.
    private func reportLine(_ reading: FleetToolsReading) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                if reading.problem == nil && reading.isCurrent {
                    FleetToolsStateBadge(state: reading.verdict, width: 92)
                }
                if let age = reading.age {
                    Text("Report read \(age) ago")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(reading.isCurrent ? Color.secondary : Color.orange)
                }
                if !reading.isCurrent && reading.problem == nil {
                    Text("It may not be current.")
                        .font(.caption)
                        .foregroundStyle(Color.orange)
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

                    FleetToolsStateBadge(state: host.state, width: stateWidth)
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

    /// The tip the doctor compared against,  and the commit it ran from.
    private func tipLine(_ host: FleetToolsHostVerdict) -> some View {
        HStack(spacing: 10) {
            if let age = host.age {
                Text("read \(age) ago")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
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
                    VStack(alignment: .leading, spacing: 1) {
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
                            if let installed = row.installed, !installed.isEmpty {
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
                            Spacer(minLength: 0)
                        }
                        Text(row.summary)
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.vertical, 1)
                }
            }
        }
        .padding(.leading, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: The Worker

    /// The Worker's Linear token health,  one row per seat.
    ///
    /// A file out of its window,  or a Worker the collector could not reach,  shows its
    /// reason and no seat rows:  rows from an old file are not current health evidence.
    private func workerHealthCard(_ health: WorkerHealthReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text("Worker Health")
                    .font(.headline)
                if health.problem == nil && health.isCurrent {
                    FleetToolsStateBadge(state: health.state, width: 92)
                }
                if let age = health.age {
                    Text("read \(age) ago")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(health.isCurrent ? Color.secondary : Color.orange)
                }
                Spacer()
                if let bundle = health.bundle, health.reachable {
                    Text("bundle \(bundle)")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(bundle == "ok" ? Color.secondary : Color.red)
                }
            }
            Text("The Worker's Linear token for each hosted seat, from its signed health route.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if let problem = health.problem {
                problemBox(problem)
            } else if !health.isCurrent {
                problemBox("This file is stale.\u{00A0} The collector writes it every fifteen minutes; old rows are not current health evidence.")
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

    /// Read the report off the main thread.
    private func load() async {
        loading = true
        defer { loading = false }
        let url = FleetToolsReportReader.reportURL()
        let healthURL = WorkerHealthReportReader.reportURL()
        let (result, health) = await Task.detached(priority: .userInitiated) {
            (FleetToolsReportReader.read(at: url), WorkerHealthReportReader.read(at: healthURL))
        }.value
        reading = result
        workerHealth = health
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
    }
}

#Preview {
    FleetHostsView()
}
