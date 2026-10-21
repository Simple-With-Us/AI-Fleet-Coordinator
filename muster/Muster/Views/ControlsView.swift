import SwiftUI

/// The controls surface:  every variable that governs a script or a background process.
///
/// This is the read side.  It shows what the knob is,  who owns it,  what it may be,
/// which way it is allowed to move,  and what it holds now.  It does not write:
/// a write needs the owner-only identity that AFC-393 is building,  and a panel that
/// could write a fleet's control surface before that identity exists is the thing the
/// fleet must not have.
struct ControlsView: View {
    @EnvironmentObject private var store: FleetStore
    @State private var registry: KnobRegistry?
    @State private var reason: String?
    /// True until the first read finishes.
    ///
    /// Without it the tab would say the registry could not be read for as long as
    /// the read takes,  which is a false report about a read that is still running.
    @State private var loading = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading {
                    Text("Reading the registry…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let registry, !registry.knobs.isEmpty {
                    table(registry)
                    if let reason {
                        Text(reason)
                            .font(.callout)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else {
                    Text(reason ?? "The registry block has no readable rows.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Controls")
                .font(.largeTitle.weight(.semibold))
            Text("Every variable that governs a script or a background process across the fleet.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let registry {
                HStack(spacing: 6) {
                    Text(registry.copyHeadline)
                        .font(.caption)
                        .foregroundStyle(registry.copyIsStale ? Color.orange : Color.secondary)
                    if registry.copyIsStale {
                        Text("It may not be current.")
                            .font(.caption)
                            .foregroundStyle(Color.orange)
                    }
                    Text("Read only:  writing needs the owner identity.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if registry.unreadableRows > 0 {
                    Text("\(registry.unreadableRows) row(s) of the registry could not be read.")
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                }
            }
        }
    }

    private func table(_ registry: KnobRegistry) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            GridRowHeader(labels: ["Knob", "Owner", "Kind", "Now", "Local files may"])
            ForEach(registry.rows) { row in
                KnobRowView(row: row)
            }
        }
    }

    /// Read the registry,  off the main actor.
    ///
    /// The registry file and the copy are both read from disk,  and the app's other
    /// engine work stays off the main thread,  so this does too.
    private func load() async {
        defer { loading = false }
        guard let root = store.engineRoot else {
            reason = "No engine checkout was found,  so the registry was not read."
            return
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        // The engine is asked which project its copy belongs to,  because the app
        // does not know and does not guess:  a private identifier does not belong
        // in the app's source,  and a copy whose project cannot be confirmed is
        // not this fleet's copy.  The store keeps the answer,  so a second visit
        // to this tab does not start another interpreter.
        let answer = await store.engineAnswer()
        let loaded = await Task.detached(priority: .userInitiated) {
            KnobReader.read(repoRoot: root, home: home, identity: answer.identity)
        }.value
        registry = loaded
        // Two things can fail on their own:  the engine may not answer,  and the
        // registry file may not be readable.  Both are said,  so the operator is
        // not sent to the wrong cause.
        let combined = [answer.reason, loaded.problem]
            .compactMap { $0 }
            .joined(separator: "  ")
        if combined.isEmpty {
            reason = loaded.knobs.isEmpty ? "The registry block has no readable rows." : nil
        } else {
            reason = combined
        }
    }
}

/// One knob,  with what it does on a line of its own.
///
/// The shared grid row truncates every cell to two lines,  and the registry notes run
/// far longer than a column of a table can hold,  so the controls tab does not use
/// it.  The note goes under the row at the full width,  where it can be read.
private struct KnobRowView: View {
    let row: KnobRegistry.Row

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 12) {
                GridCellText(value: row.name)
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(value: row.owner)
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(value: row.kind)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(row.kind)
                // The "Now" cell of a secret row is marked,  and never carries a
                // tooltip,  so a secret's value cannot be read off a secret row.
                GridCellText(value: row.now, emphasised: row.nowIsSecret)
                    .frame(maxWidth: .infinity, alignment: .leading)
                GridCellText(value: row.localMay)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.vertical, 2)
            if !row.note.isEmpty {
                Text(row.note)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 6)
            }
        }
    }
}
