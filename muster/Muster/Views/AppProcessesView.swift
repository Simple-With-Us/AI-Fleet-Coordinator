import SwiftUI

/// The app processes panel:  each fleet app's processes on the Mac and on the server.
///
/// The panel reads `~/.local/state/fleet-tools/app-processes.json` and nothing else.  The
/// fleet tools doctor writes it every fifteen minutes:  it scans the Mac,  and it carries the
/// server's scan over its read-only route.  The panel runs no scan and asks nothing of the
/// network,  so opening it never starts a process listing.
///
/// Words come first and colour only repeats them.  An app with no process on a machine
/// that was read in full says No Process Seen,  which is information and not a fault.  A
/// machine the doctor could not reach,  a machine read only in part,  and a report past
/// forty-five minutes all say Not Known,  never that nothing runs.
///
/// The panel controls nothing:  starting and stopping services stays in Services and
/// Server Status.
struct AppProcessesView: View {
    @State private var reading: AppProcessesReading?
    @State private var loading = false
    @State private var showAppsWithoutProcesses = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading && reading == nil {
                    Text("Reading the app process report\u{2026}")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let reading {
                    content(reading)
                } else {
                    Text("No report read yet.\u{00A0} Refresh to read it.")
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
                Text(MusterDestination.appProcesses.title)
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Toggle("Show Apps With No Process", isOn: $showAppsWithoutProcesses)
                    .toggleStyle(.checkbox)
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("Each fleet app's processes on the Mac and on the server, matched by executable, launchd label, pm2 name or systemd unit.\u{00A0} A process's arguments and environment are never read.\u{00A0} Apps that run in Coolify containers are under Server Status.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let reading {
                HStack(spacing: 10) {
                    Text(reading.path)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let generated = reading.generatedAt, let age = reading.age {
                        Text("Written at \(FleetTime.clock(generated)), \(age) ago")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(reading.isCurrent ? Color.secondary : Color.orange)
                    }
                }
            }
        }
    }

    // MARK: The report

    @ViewBuilder
    private func content(_ reading: AppProcessesReading) -> some View {
        if let problem = reading.problem {
            problemBox("Not Known.\u{00A0} \(problem)")
        } else if reading.hosts.isEmpty {
            problemBox("Not Known.\u{00A0} The report names no machine.")
        } else {
            if !reading.isCurrent {
                problemBox("Not Known.\u{00A0} The report is older than 45 minutes, so nothing in it is current.\u{00A0} Every app reads Not Known until the doctor writes a fresh one.")
            }
            ForEach(reading.hosts) { host in
                hostCard(host)
            }
        }
    }

    private func hostCard(_ host: AppProcessesHostReading) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            hostHeader(host)
            switch host.state {
            case .notReached(let reason):
                problemBox("Not Known:  the doctor could not read \(host.heading.lowercased()).\u{00A0} \(reason)")
            case .partlyRead:
                problemBox("Partly Read:  part of this machine's read failed or was cut, so an app with no process listed here reads Not Known.")
            case .read, .reportOld:
                EmptyView()
            }
            ForEach(host.notes, id: \.self) { note in
                Text(note)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: 6) {
                ForEach(visibleApps(host)) { app in
                    appRow(app)
                }
            }
            let hidden = host.apps.count - visibleApps(host).count
            if hidden > 0 {
                Text(hidden == 1 ? "1 app with no process seen is hidden." : "\(hidden) apps with no process seen are hidden.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private func hostHeader(_ host: AppProcessesHostReading) -> some View {
        HStack(spacing: 10) {
            Text(host.heading)
                .font(.headline)
            Text(host.state.words)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .foregroundStyle(hostTint(host.state))
            if host.state.showsRows {
                Text(host.appsSeen == 1 ? "1 app with processes" : "\(host.appsSeen) apps with processes")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if let generated = host.generatedAt {
                Text("Scanned at \(FleetTime.clock(generated))")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// Only an app the full read found nothing for may be hidden:  a Not Known row stays.
    private func visibleApps(_ host: AppProcessesHostReading) -> [AppProcessesAppReading] {
        showAppsWithoutProcesses ? host.apps : host.apps.filter { $0.state != .noProcessSeen }
    }

    // MARK: One app

    private func appRow(_ app: AppProcessesAppReading) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 10) {
                Text(app.title)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                    .frame(width: 240, alignment: .leading)
                Text(app.state.words)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(appTint(app.state))
                Spacer(minLength: 0)
            }
            ForEach(Array(app.processes.enumerated()), id: \.offset) { _, process in
                processRow(process)
            }
        }
        .padding(.vertical, 2)
    }

    private func processRow(_ process: AppProcessRow) -> some View {
        HStack(spacing: 10) {
            Text(process.name)
                .font(.system(size: 11, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 240, alignment: .leading)
                .help(process.executable ?? process.name)
            Text(process.pid.map { "pid \($0)" } ?? "pid not read")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 90, alignment: .leading)
            Text(process.stateWords)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(process.isStoppedOrGone ? Color.orange : Color.secondary)
                .frame(width: 140, alignment: .leading)
            Text(process.uptimeText().map { "up \($0)" } ?? "start not read")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
            if let matchedBy = process.matchedBy {
                Text("by \(matchedBy)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, 16)
    }

    // MARK: The pieces

    private func hostTint(_ state: AppProcessesHostState) -> Color {
        switch state {
        case .read: return .green
        case .partlyRead: return .orange
        case .notReached, .reportOld: return .indigo
        }
    }

    private func appTint(_ state: AppProcessesAppState) -> Color {
        switch state {
        case .seen: return .green
        case .noProcessSeen: return .secondary
        case .notKnown: return .indigo
        }
    }

    /// Why the report,  or one machine in it,  cannot be believed.
    private func problemBox(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "questionmark.circle")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(text)
                .font(.system(size: 11))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }

    private func load() async {
        loading = true
        defer { loading = false }
        let url = AppProcessesReportReader.reportURL()
        reading = await Task.detached(priority: .userInitiated) {
            AppProcessesReportReader.read(at: url)
        }.value
    }
}

#Preview {
    AppProcessesView()
}
