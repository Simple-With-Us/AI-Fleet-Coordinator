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
