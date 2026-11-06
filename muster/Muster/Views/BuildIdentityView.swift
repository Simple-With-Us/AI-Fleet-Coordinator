import SwiftUI

/// The build and the checkout,  for the Settings window.
///
/// The words all come from `BuildIdentityCopy`,  so what this says is tested without a view.
struct BuildIdentitySettingsSection: View {
    @ObservedObject private var updater = AppUpdater.shared
    @State private var checkouts: [CheckoutRevision.Reading] = []
    private let identity = BuildIdentity.current

    var body: some View {
        Section("This Build") {
            LabeledContent("Version", value: BuildIdentityCopy.versionValue(identity))
            LabeledContent("Commit", value: BuildIdentityCopy.commitLine(identity))
            LabeledContent("Source", value: BuildIdentityCopy.source(identity))
            if let marker = identity.marker {
                Text(marker).font(.caption.weight(.semibold))
            }
            ForEach(checkouts, id: \.root) { reading in
                LabeledContent("Checkout", value: BuildIdentityCopy.checkoutLine(reading))
                Text(BuildIdentityCopy.comparison(app: identity, checkout: reading))
                    .font(.caption).foregroundStyle(.secondary)
            }
            LabeledContent("Automatic Updates", value: updater.availability.summary)
            if !updater.waitingFor.isEmpty {
                Text(UpdateGateCopy.waiting(updater.waitingFor))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("Check For Updates\u{2026}") { updater.checkForUpdates() }
                .disabled(!updater.canCheckForUpdates)
        }
        .onAppear { readCheckouts() }
    }

    private func readCheckouts() {
        checkouts = CheckoutRevision.musterCheckouts().map { CheckoutRevision.read(root: $0) }
    }
}

/// The foot of the menu bar menu:  which build is running,  and where its checkout is.
struct BuildIdentityMenuSection: View {
    @ObservedObject private var updater = AppUpdater.shared
    private let identity = BuildIdentity.current

    var body: some View {
        Section {
            Text(BuildIdentityCopy.versionLine(identity))
            if let marker = identity.marker {
                Text(marker)
            }
            // Read from disk at most every thirty seconds,  however often the body is built.
            ForEach(CheckoutReadingCache.shared.readings(), id: \.root) { reading in
                Text(BuildIdentityCopy.menuCheckoutLine(app: identity, checkout: reading))
            }
            if !updater.waitingFor.isEmpty {
                Text("Update Waiting")
            }
            if updater.availability.isEnabled {
                CheckForUpdatesButton()
            }
        }
    }
}

/// The Check For Updates command,  in the app menu and the menu bar menu.
struct CheckForUpdatesButton: View {
    @ObservedObject private var updater = AppUpdater.shared

    var body: some View {
        Button("Check For Updates\u{2026}") { updater.checkForUpdates() }
            .disabled(!updater.canCheckForUpdates)
    }
}
