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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if let registry, !registry.knobs.isEmpty {
                    table(registry)
                } else {
                    Text(reason ?? "The knob registry could not be read.")
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
        guard let root = store.engineRoot else {
            reason = "No engine checkout was found,  so the registry was not read."
            return
        }
        let location = store.engineLocation
        let home = FileManager.default.homeDirectoryForCurrentUser
        let loaded = await Task.detached(priority: .userInitiated) {
            // The engine is asked which project its copy belongs to,  because the app
            // does not know and does not guess:  a private identifier does not belong
            // in the app's source,  and a copy whose project cannot be confirmed is
            // not this fleet's copy.
            let identity = location.flatMap { EngineIdentity.read(location: $0, home: home) }
            return KnobReader.read(repoRoot: root, home: home, identity: identity)
        }.value
        registry = loaded
        reason = loaded.problem ?? (loaded.knobs.isEmpty
            ? "The registry block was not found in INFISICAL.md."
            : nil)
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
