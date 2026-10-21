import SwiftUI

/// The artifact a cell is about,  shown read only below the grid.
///
/// The popup says what the engine measured,  and this says what is actually on the
/// disk.  An operator who is told a file matches a hash still wants to read the file:
/// the hash says the copy is the expected one,  and the text says whether the
/// expected one is right.  The panel never edits,  because a fleet surface is not the
/// app's to change,  and it never reads a credential:  a credential rule shows the
/// key names and the presence,  which is the information the rule is about.
struct ConfigFilePanel: View {
    let cell: GridCell
    let artifacts: FleetStore.CellArtifacts

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if artifacts.rules.isEmpty {
                Text(artifacts.coverReason ?? "No rule covers this seat and kind.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        ForEach(Array(artifacts.rules.enumerated()), id: \.offset) { index, rule in
                            ruleBlock(rule: rule, view: artifacts.views.indices.contains(index) ? artifacts.views[index] : nil)
                        }
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(cell.seat)
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
            Text("/")
                .foregroundStyle(.secondary)
            Text(cell.kind.title)
                .font(.headline)
            Text("Read only")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.secondary.opacity(0.12), in: Capsule())
            Spacer()
            Text("What this cell refers to,  as it is on this Mac.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

    @ViewBuilder
    private func ruleBlock(rule: CheckResult, view: ConfigFileView?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(rule.ruleId)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                Text(rule.checkType)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(rule.state)
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundStyle(rule.checkState == .passed ? Color.green : Color.orange)
            }
            Text(rule.reason)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(alignment: .top, spacing: 18) {
                ValueTable(title: "Expected", values: rule.expected)
                ValueTable(title: "Observed", values: rule.observed)
            }

            if let view {
                Divider()
                artifactView(view)
            }
        }
    }

    @ViewBuilder
    private func artifactView(_ artifact: ConfigFileView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(artifact.path)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .textSelection(.enabled)
                Text(artifact.summary)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            switch artifact {
            case .file(_, let text, let truncated, _):
                if truncated {
                    Text("The file is longer than the panel reads, so this is its first part.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                ScrollView(.horizontal, showsIndicators: true) {
                    Text(text.isEmpty ? "(the file is empty)" : text)
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 7))
            case .directory(_, let entries):
                if entries.isEmpty {
                    Text("The directory is empty.")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                } else {
                    Text(entries.joined(separator: "\n"))
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 7))
                }
            case .missing:
                Text("This path is not on this Mac,  so there is no file to show.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            case .refused(_, let reason):
                Text(reason)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            case .unresolved(let reason):
                Text(reason)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
