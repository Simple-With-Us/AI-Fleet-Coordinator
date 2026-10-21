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
        .task { load() }
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
                    Text("Local copy \(registry.copyAge) old.")
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
            }
        }
    }

    private func table(_ registry: KnobRegistry) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            GridRowHeader(labels: ["Knob", "Owner", "Kind", "Now", "Local files may", "What it does"])
            ForEach(registry.knobs) { knob in
                GridRow(cells: [
                    GridCellText(value: knob.name),
                    GridCellText(value: knob.owner),
                    GridCellText(value: knob.kind),
                    GridCellText(
                        value: knob.current.displayText,
                        emphasised: knob.isSecret
                    ),
                    GridCellText(value: knob.localMay),
                    GridCellText(value: knob.note),
                ])
            }
        }
    }

    private func load() {
        guard let root = store.engineRoot else {
            reason = "No engine checkout was found,  so the registry was not read."
            return
        }
        let loaded = KnobReader.read(
            repoRoot: root,
            home: FileManager.default.homeDirectoryForCurrentUser
        )
        registry = loaded
        reason = loaded.knobs.isEmpty
            ? "The registry block was not found in INFISICAL.md."
            : nil
    }
}
