import SwiftUI

/// The server status panel:  what the server's own report says.
///
/// The panel reads one file and asks nothing of the network:  the collector's report
/// at `~/.local/state/fleet-tools/server-status.json`.  It shows how Coolify is
/// working,  the busiest processes on the host,  and the watched config files whose
/// drift is not `unchanged`.
///
/// A report that cannot be trusted reads as unknown,  which is information and not a
/// fault,  so a collector that could not run shows as an unknown panel and not as a
/// broken server.
///
/// The panel is read only.  It never runs the collector and never writes the report.
struct ServerStatusPanel: View {
    @State private var reading: ServerStatusReading?
    @State private var loading = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading && reading == nil {
                    Text("Reading the server's report…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let reading {
                    reportLine(reading)
                    if let problem = reading.problem {
                        problemBox(problem)
                    } else if !reading.isCurrent {
                        problemBox("This report is stale. Refresh after the collector writes a new report; old rows are not current health evidence.")
                    } else {
                        coolifySection(reading)
                        processesSection(reading)
                        configSection(reading)
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
                Text("Server Status")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("What the server's own report says about Coolify, its processes, and the fleet's config files.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// The report itself:  how old it is,  and whether it can be believed.
    ///
    /// A report that is out of its window gets no badge of its own:  the rows carry
    /// the state,  and a red badge beside "it may not be current" would be alarm the
    /// panel has no reason to raise.
    private func reportLine(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                if reading.problem == nil && reading.isCurrent {
                    FleetToolsStateBadge(state: reading.state, width: 92)
                }
                if !reading.host.isEmpty {
                    Text(reading.host)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
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

    // MARK: The sections

    /// The title of a section,  with the state of that half of the report.
    private func sectionHeader(_ title: String, _ state: CellState, _ detail: String) -> some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.headline)
            FleetToolsStateBadge(state: state, width: 92)
            Spacer()
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func coolifySection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Coolify", reading.coolifyState, "\(reading.coolify.count) \(reading.coolify.count == 1 ? "resource" : "resources")")
            if reading.coolify.isEmpty {
                Text("The report names no Coolify resources.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.coolify) { resource in
                    coolifyRow(resource)
                }
            }
        }
    }

    private func coolifyRow(_ resource: CoolifyResource) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(resource.name.isEmpty ? "unnamed" : resource.name)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                HStack(spacing: 6) {
                    Text(resource.resourceType.rawValue)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.12), in: Capsule())
                    if let image = resource.image, !image.isEmpty {
                        Text(image)
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
            }
            Spacer(minLength: 0)
            if let cpu = resource.cpuText {
                Text("cpu \(cpu)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let memory = resource.memoryText {
                Text("mem \(memory)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let updated = resource.updatedText {
                Text(updated)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            FleetToolsStateBadge(state: resource.state, width: 92)
        }
        .padding(.vertical, 2)
    }

    private func processesSection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Top Processes", reading.processState, "\(reading.processes.count) \(reading.processes.count == 1 ? "process" : "processes")")
            if reading.processes.isEmpty {
                Text("The report names no processes.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.processes) { process in
                    processRow(process)
                }
            }
        }
    }

    private func processRow(_ process: ServerProcess) -> some View {
        HStack(spacing: 8) {
            Text("\(process.pid)")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 70, alignment: .leading)
            Text(process.displayName)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
            Text(process.owner ?? "unknown")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            if let cpu = process.cpuText {
                Text("cpu \(cpu)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let memory = process.memoryText {
                Text("mem \(memory)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    private func configSection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Config Drift",
                reading.configState,
                "\(reading.driftedFiles.count) of \(reading.files.count) \(reading.files.count == 1 ? "file" : "files") drifted"
            )
            if reading.files.isEmpty {
                Text("The report watches no files.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if reading.driftedFiles.isEmpty {
                Text("Every watched file is unchanged.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.driftedFiles) { file in
                    fileRow(file)
                }
            }
        }
    }

    private func fileRow(_ file: WatchedFile) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(file.fileName)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                Text(file.path)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 0)
            if let size = file.sizeText {
                Text(size)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Text(file.fileDrift.rawValue)
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(tint(for: file.state))
        }
        .padding(.vertical, 2)
    }

    /// The colour a row's state carries,  from the app's own palette.
    private func tint(for state: CellState) -> Color {
        switch state {
        case .missing: return .red
        case .drift: return .orange
        case .unproven, .unknown: return .indigo
        case .passed: return .green
        case .unsupported: return .secondary
        case .covered: return .secondary
        }
    }

    // MARK: The read

    /// Read the report off the main thread.
    private func load() async {
        loading = true
        defer { loading = false }
        let url = ServerStatusReportReader.reportURL()
        let result = await Task.detached(priority: .userInitiated) {
            ServerStatusReportReader.read(at: url)
        }.value
        reading = result
    }
}

#Preview {
    ServerStatusPanel()
}
