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
/// The panel never runs the collector and never writes the report.  Its buttons start an
/// action through `OpsController`,  which plans it,  shows the plan and asks Touch ID before
/// anything runs on the server:  a unit the report marks `controllable` gets Start,  Stop and
/// Restart,  and a Coolify application whose uuid came from a Coolify label gets Redeploy when
/// the allowlist allows redeploys (the same rule the server applies before it plans one).
struct ServerStatusPanel: View {
    @State private var reading: ServerStatusReading?
    @State private var loading = false
    /// The allowlist in this Mac's checkout,  which decides the buttons.
    @State private var allowlist: OpsAllowlist?
    @ObservedObject private var controller = OpsController.shared

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
                        if reading.hostMetrics != nil {
                            hostSection(reading)
                        }
                        if !reading.services.isEmpty {
                            servicesSection(reading)
                        }
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
        // A finished action changes what the server reports,  so the report is read again.
        .onChange(of: controller.completedRuns) { _, _ in
            Task { await load() }
        }
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
            Text("What the server's own report says about the host, its services, Coolify, its processes, and the fleet's config files.")
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

    private func hostSection(_ reading: ServerStatusReading) -> some View {
        let metrics = reading.hostMetrics
        let uptime = metrics?.uptimeText.map { "up \($0)" } ?? "uptime not read"
        return VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Host", reading.hostState, uptime)
            if let metrics {
                if let load = metrics.loadText {
                    let cpus = metrics.cpuCount.map { " on \($0) \($0 == 1 ? "cpu" : "cpus")" } ?? ""
                    metricRow("Load", "\(load)\(cpus)", fraction: metrics.loadFraction)
                }
                if let memory = metrics.memory, let total = memory.totalBytes {
                    let available = memory.availableBytes.map { ServerStatusFormat.bytes($0) } ?? "not read"
                    metricRow(
                        "Memory",
                        "\(available) free of \(ServerStatusFormat.bytes(total))",
                        fraction: memory.usedFraction
                    )
                }
                if let swap = metrics.swap, let total = swap.totalBytes, total > 0 {
                    let free = swap.freeBytes.map { ServerStatusFormat.bytes($0) } ?? "not read"
                    metricRow(
                        "Swap",
                        "\(free) free of \(ServerStatusFormat.bytes(total))",
                        fraction: swap.usedFraction
                    )
                }
                ForEach(metrics.disks) { disk in
                    metricRow(
                        disk.mount.isEmpty ? "disk" : disk.mount,
                        diskText(disk),
                        fraction: disk.usedFraction
                    )
                }
                if metrics.loadText == nil && metrics.memory == nil && metrics.disks.isEmpty {
                    Text("The report carries no host numbers.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func diskText(_ disk: HostDisk) -> String {
        guard let free = disk.freeBytes else { return "not read" }
        if let total = disk.totalBytes {
            return "\(ServerStatusFormat.bytes(free)) free of \(ServerStatusFormat.bytes(total))"
        }
        return "\(ServerStatusFormat.bytes(free)) free"
    }

    /// One measured number,  with a bar when there is a share to draw.
    ///
    /// The bar is amber from 85 percent and red from 95,  and a load bar is drawn against
    /// one per cpu,  so a machine that is fully busy fills it.
    private func metricRow(_ label: String, _ text: String, fraction: Double?) -> some View {
        HStack(spacing: 10) {
            Text(label)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 140, alignment: .leading)
            usageBar(fraction)
            if let fraction {
                Text(String(format: "%.0f%%", fraction * 100))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(width: 44, alignment: .trailing)
            }
            Text(text)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 1)
    }

    private func usageBar(_ fraction: Double?) -> some View {
        let width: CGFloat = 160
        let share = min(max(fraction ?? 0, 0), 1)
        let tint: Color = {
            guard let fraction else { return .secondary }
            if fraction >= 0.95 { return .red }
            if fraction >= 0.85 { return .orange }
            return .green
        }()
        return ZStack(alignment: .leading) {
            Capsule()
                .fill(Color.secondary.opacity(0.15))
            Capsule()
                .fill(tint)
                .frame(width: fraction == nil ? 0 : max(2, width * share))
        }
        .frame(width: width, height: 6)
        .accessibilityHidden(true)
    }

    private func servicesSection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Services",
                reading.servicesState,
                "\(reading.services.count) \(reading.services.count == 1 ? "unit" : "units")"
            )
            ForEach(reading.services) { service in
                serviceRow(service)
            }
        }
    }

    private func serviceRow(_ service: ServerService) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(service.unit)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(service.scope ?? "system")
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.12), in: Capsule())
                }
                if let description = service.description, !description.isEmpty {
                    Text(description)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            Text(service.stateText)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            if let since = service.sinceText {
                Text("for \(since)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let enabled = service.enabled {
                Text(enabled)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if service.controllable {
                ForEach(verbs(for: service), id: \.self) { verb in
                    Button(verb.title) {
                        controller.plan(.serverUnit(verb: verb, unit: service.unit))
                    }
                    .controlSize(.small)
                    .disabled(controller.isPresenting)
                }
            }
            FleetToolsStateBadge(state: service.state, width: 92)
        }
        .padding(.vertical, 2)
    }

    /// The verbs a controllable unit gets:  the ones this Mac's allowlist names for it,  or
    /// all three when this checkout has no entry for it.  The server checks its own
    /// allowlist again either way.
    private func verbs(for service: ServerService) -> [OpsVerb] {
        let named = allowlist?.verbs(forServerUnit: service.unit) ?? []
        return named.isEmpty ? OpsVerb.allCases : named
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
                    coolifyRow(resource, offersRedeploy: reading.offersRedeploy(resource))
                }
            }
        }
    }

    private func coolifyRow(_ resource: CoolifyResource, offersRedeploy: Bool) -> some View {
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
            // Only an application whose uuid came from a Coolify label is offered:  the
            // server refuses every other row,  so a button there would only lead to a
            // refused plan.
            if allowlist?.coolifyRedeploy == true, offersRedeploy {
                Button("Redeploy") {
                    controller.plan(.coolifyRedeploy(
                        uuid: resource.uuid,
                        name: resource.name.isEmpty ? "this resource" : resource.name
                    ))
                }
                .controlSize(.small)
                .disabled(controller.isPresenting)
                .help("Plan a Coolify redeploy of this resource.")
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
            if reading.filesTruncated {
                Text(partialListText(reading.filesSummary))
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if reading.files.isEmpty {
                Text("The report watches no files.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if reading.driftedFiles.isEmpty {
                Text(reading.noticedFiles.isEmpty
                    ? "Every watched file is unchanged."
                    : "No watched file has drifted.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.driftedFiles) { file in
                    fileRow(file)
                }
            }
            if !reading.noticedFiles.isEmpty {
                Text("Changed, for information only")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
                ForEach(reading.noticedFiles) { file in
                    fileRow(file)
                }
            }
        }
    }

    /// The note for a file list the collector cut short.
    private func partialListText(_ summary: ServerFilesSummary?) -> String {
        if let found = summary?.found, let limit = summary?.limit {
            return "Partial list: the collector found \(found) files and read the first \(limit)."
        }
        return "Partial list: the collector stopped at its limit."
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
        let allowlistURL = OpsAllowlist.url(repoRoot: FleetConfigLocation.resolve().repoRoot)
        let result = await Task.detached(priority: .userInitiated) {
            (ServerStatusReportReader.read(at: url), OpsAllowlist.read(at: allowlistURL))
        }.value
        reading = result.0
        allowlist = result.1
    }
}

#Preview {
    ServerStatusPanel()
}
