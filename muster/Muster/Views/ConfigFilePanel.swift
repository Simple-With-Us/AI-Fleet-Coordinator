import SwiftUI

/// The detail for one cell,  shown read only below the grid.
///
/// This is the only place a cell is explained.  It carries what the engine measured
/// and what is actually on the disk,  because an operator who is told a file matches
/// a hash still wants to read the file:  the hash says the copy is the expected one,
/// and the text says whether the expected one is right.
///
/// The panel never edits,  because a fleet surface is not the app's to change,  and it
/// never reads a credential:  a credential rule shows the key names and the presence,
/// which is the information the rule is about.
///
/// **This is an architectural exception,  not a rule.**  The grid proper reads the
/// engine's envelope and nothing else,  and the app does not parse a seat's config.
/// This panel does read a file,  because an operator who is told a file matches a
/// hash still wants to read the file.  The project's point of contact has said the
/// better shape is one engine-owned,  typed,  allowlisted and redacted artifact
/// projection,  and that is the shape to build when the engine boundary is next
/// worked on.  Until then this stands as an exception,  on the record,  and not as a
/// precedent that raw config content belongs in a manifest or in a view.
struct ConfigFilePanel: View {
    let cell: GridCell
    let artifacts: FleetStore.CellArtifacts
    /// The citation each rule carries in the manifest,  by rule id.
    let sources: [String: String]
    /// When the run this cell came from checked it (`GridPresentation.asOfLine`).
    let asOf: String
    /// Called when the operator closes the panel.
    let onClose: () -> Void

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
                            RuleArtifactView(
                                rule: rule,
                                view: artifacts.views.indices.contains(index) ? artifacts.views[index] : nil,
                                source: sources[rule.ruleId]
                            )
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
            HStack(spacing: 5) {
                Image(systemName: cell.gridSymbol)
                    .font(.system(size: 10, weight: .semibold))
                    .accessibilityHidden(true)
                // The grid's one spelling of the state, the same word the badge shows.
                Text(cell.gridWord)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
            }
            .foregroundStyle(cell.state.tint)
            Text("Read only")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color.secondary.opacity(0.12), in: Capsule())
            Spacer()
            // Each rule block says where its evidence is read, so the header says only
            // which run this is.
            Text(asOf)
                .font(.caption)
                .foregroundStyle(.secondary)
                .help("When the check this panel shows was run.\u{00A0} The panel reads the cell again when the next check lands.")
            Button {
                onClose()
            } label: {
                Label("Close", systemImage: "xmark.circle.fill")
                    .labelStyle(.iconOnly)
                    .font(.system(size: 14))
            }
            .buttonStyle(.plain)
            .help("Close the detail panel")
            .accessibilityLabel("Close the detail panel")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 12)
    }

}

/// One rule of a cell, with the facts the engine measured, where its evidence is read (a
/// file on the Mac or the secret store), and the artifact it checks.
///
/// The grid's detail panel and the Seat Setup panel both show a rule this way, so a rule
/// reads the same wherever it is opened.  The same read-only and no-credential rules apply:
/// nothing here edits, and a credential rule shows key names and presence only.
struct RuleArtifactView: View {
    let rule: CheckResult
    let view: ConfigFileView?
    /// The citation the manifest carries for the rule, when it has one.
    let source: String?

    var body: some View {
        ruleBlock(rule: rule, view: view)
    }

    /// Where this rule's evidence is read:  a file on the Mac, or the secret store.
    private var location: GridPresentation.RuleLocation {
        GridPresentation.RuleLocation.of(checkType: rule.checkType)
    }

    /// The colour of the rule's own state, so a MISSING rule is never orange under a red
    /// header.
    private var stateTint: Color {
        rule.isNotApplicable ? Color.secondary : CellState(checkState: rule.checkState).tint
    }

    /// Plain words first:  the state and what it found, where it was read, then the
    /// values.  The ids, the collector and the codes are under Technical Details.
    @ViewBuilder
    private func ruleBlock(rule: CheckResult, view: ConfigFileView?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(GridPresentation.ruleWord(rule))
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .foregroundStyle(stateTint)
                if let scope = rule.guardScope {
                    // What this rule asks the guard to cover,  so a PASS here reads as
                    // exactly that and not as a guard that covers everything.
                    Text(scope)
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                }
                Spacer()
                // Where the evidence is read, on every rule block:  the Permissions key
                // rules ask the secret store, not a file on the Mac.
                Label(location.label, systemImage: location == .secretStore ? "key" : FleetHost.mac.systemImage)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            Text(rule.reason)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)

            // The citation the manifest carries,  which is the directive or the message
            // that asked for the rule.  A rule that cites none shows no line at all.
            if let cited = GridPresentation.shownSource(source) {
                ProofLine(label: "Source", value: cited)
            }

            HStack(alignment: .top, spacing: 18) {
                ValueTable(title: "Expected", values: rule.expected)
                ValueTable(title: "Observed", values: rule.observed)
            }

            ProofLine(label: "Proof", value: rule.proofText)

            // The ids, the collector and the codes are for whoever debugs the engine,
            // so they wait under a disclosure rather than lead the block.
            DisclosureGroup("Technical Details") {
                VStack(alignment: .leading, spacing: 4) {
                    ProofLine(label: "Rule", value: rule.ruleId)
                    ProofLine(label: "Platform", value: rule.platform)
                    ProofLine(label: "Check type", value: rule.checkType)
                    ProofLine(label: "Collector", value: rule.collector)
                    ProofLine(label: "Reason code", value: rule.reasonCode)
                    ProofLine(label: "Checked at", value: rule.checkedAt)
                    ProofLine(label: "Expires at", value: rule.expiresAt ?? "—")
                    ProofLine(label: "Time to live", value: "\(rule.ttlSeconds)s")
                    ProofLine(label: "Stale", value: rule.stale ? "yes" : "no")
                    ProofLine(label: "Counts as failing", value: rule.failing ? "yes" : "no")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
            }
            .font(.caption)

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
                // A store folder such as /zulip is not a path on the Mac, so the line
                // names which of the two it is.
                if case .unresolved = artifact {
                    EmptyView()
                } else {
                    Text("\(location.pathLabel):")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
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
