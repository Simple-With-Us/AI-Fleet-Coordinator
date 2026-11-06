import SwiftUI

/// Where something runs,  in a symbol and words.  The words carry the meaning:  the badge
/// reads the same with the symbol dropped or the tint gone,  and it is one spoken element.
struct PlaceBadge: View {
    let place: FleetPlace

    var body: some View {
        Label(place.title, systemImage: place.systemImage)
            .font(.callout.weight(.semibold))
            .accessibilityElement(children: .combine)
    }
}

/// The strip over every panel:  the one badge that says where the panel's data and actions
/// live,  and the line that says where its data came from.
struct PlaceStrip: View {
    let place: FleetPlace
    let source: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            PlaceBadge(place: place)
            if let source {
                Text(source)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 8)
        .accessibilityElement(children: .combine)
    }
}

/// A place's health,  in the menu's own words,  with a symbol beside them and never instead.
struct PlaceHealthLabel: View {
    let health: MachineHealth
    var font: Font = .headline

    var body: some View {
        Label(PlaceHealthWords.sentence(health), systemImage: PlaceHealthWords.systemImage(health))
            .font(font)
            .foregroundStyle(tint)
            .accessibilityElement(children: .combine)
    }

    private var tint: Color {
        switch health {
        case .ok: return .green
        case .attention: return .orange
        case .notKnown: return .secondary
        }
    }
}
