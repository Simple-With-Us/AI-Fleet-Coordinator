import Foundation

/// Whether this build can update itself,  decided from its own Info.plist before Sparkle is
/// touched.
///
/// Sparkle raises a modal "updater failed to start" alert at launch when the feed or the
/// signing key is missing.  The Debug configuration carries no feed,  and a build made before
/// Muster's update key existed carries no key,  so both are ruled out here rather than greeted
/// with that alert.  See muster/docs/AUTO-UPDATE.md.
enum AutoUpdateAvailability: Equatable {
    case enabled(feed: URL)
    case disabled(reason: String)

    static let feedKey = "SUFeedURL"
    static let publicKeyKey = "SUPublicEDKey"

    static func evaluate(info: [String: Any]?, bundleURL: URL) -> AutoUpdateAvailability {
        guard bundleURL.pathExtension == "app" else {
            return .disabled(reason: "Not running from an app bundle.")
        }
        guard let rawFeed = info?[feedKey] as? String,
              let feed = URL(string: rawFeed.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = feed.scheme?.lowercased(),
              let host = feed.host, !host.isEmpty else {
            return .disabled(reason: "This build has no update feed.")
        }
        // Plain HTTP only reaches this Mac itself,  which is how an update is rehearsed
        // locally.  Anything that leaves the machine must be HTTPS.
        guard scheme == "https" || (scheme == "http" && (host == "localhost" || host == "127.0.0.1")) else {
            return .disabled(reason: "The update feed is not HTTPS.")
        }
        guard let rawKey = info?[publicKeyKey] as? String,
              let key = Data(base64Encoded: rawKey.trimmingCharacters(in: .whitespacesAndNewlines)),
              key.count == 32 else {
            return .disabled(reason: "This build has no update signing key.")
        }
        return .enabled(feed: feed)
    }

    var isEnabled: Bool {
        if case .enabled = self { return true }
        return false
    }

    /// One line for Settings.
    var summary: String {
        switch self {
        case .enabled: return "On"
        case .disabled(let reason): return "Off \u{00B7} \(reason)"
        }
    }
}

/// Holds an update's install and relaunch until nothing Muster started is still running.
///
/// Sparkle hands over the install in two ways.  The automatic one arrives once an update has
/// downloaded on its own,  and it runs only when Muster is not the frontmost app and no work
/// is in flight.  The relaunch one arrives when the owner chose Install And Relaunch,  and it
/// waits only for work in flight:  the owner asked,  so being frontmost does not hold it.
///
/// Work in flight is what the probes report:  an apply on the app-lifetime
/// `ApplyCoordinator`,  an `OpsController` run,  and a `ServiceActionRouter` action on the
/// direct route.  A relaunch in the middle of any of them would cut a write short and lose
/// the result the owner is waiting for.
@MainActor
final class UpdateInstallGate {
    enum Kind: Equatable {
        case automatic
        case relaunch
    }

    /// One thing that can hold the install,  as a sentence,  or nil when it is idle.
    typealias Probe = @MainActor () -> String?

    private let appIsActive: @MainActor () -> Bool
    private let probes: [Probe]
    /// One slot per kind, so a relaunch that arrives while an automatic install waits does
    /// not drop it, and each runs once its own blockers clear.
    private var pending: [Kind: () -> Void] = [:]
    /// What held the install back the last time it was looked at.
    private(set) var heldBy: [String] = []

    init(appIsActive: @escaping @MainActor () -> Bool, probes: [Probe]) {
        self.appIsActive = appIsActive
        self.probes = probes
    }

    /// True while an update waits to be installed.
    var hasPendingInstall: Bool { !pending.isEmpty }

    /// Everything that stands in the way of an install of this kind,  in words.
    func blockers(for kind: Kind) -> [String] {
        var reasons = probes.compactMap { $0() }
        if kind == .automatic, appIsActive() {
            reasons.append(UpdateGateCopy.inUse)
        }
        return reasons
    }

    /// Sparkle's automatic install:  always taken over,  and run now or once idle.
    func holdAutomaticInstall(_ install: @escaping () -> Void) {
        pending[.automatic] = install
        reevaluate()
    }

    /// Sparkle's relaunch:  true when it has to wait,  and then it runs once idle.
    func shouldPostponeRelaunch(_ install: @escaping () -> Void) -> Bool {
        let reasons = blockers(for: .relaunch)
        guard !reasons.isEmpty else {
            heldBy = pending.isEmpty ? [] : heldBy
            return false
        }
        pending[.relaunch] = install
        heldBy = reasons
        return true
    }

    /// Looks again,  after something that could have been holding the install changed.
    ///
    /// The relaunch the owner asked for goes first,  because it waits for less.  Each
    /// handler runs once and leaves its slot before it runs.
    func reevaluate() {
        var stillHeldBy: [String] = []
        for kind in [Kind.relaunch, .automatic] {
            guard let install = pending[kind] else { continue }
            let reasons = blockers(for: kind)
            if reasons.isEmpty {
                pending[kind] = nil
                install()
            } else {
                for reason in reasons where !stillHeldBy.contains(reason) {
                    stillHeldBy.append(reason)
                }
            }
        }
        heldBy = stillHeldBy
    }

    /// The probes for Muster's own app-lifetime owners of work.
    static func musterWork(
        apply: ApplyCoordinator,
        ops: OpsController,
        router: ServiceActionRouter?
    ) -> [Probe] {
        [
            { apply.hasWorkInFlight ? UpdateGateCopy.applyRunning : nil },
            { ops.isBusy ? UpdateGateCopy.operationRunning : nil },
            { (router?.hasActionInFlight ?? false) ? UpdateGateCopy.serviceActionRunning : nil },
        ]
    }
}

/// The words for what holds an update.
enum UpdateGateCopy {
    static let applyRunning = "A fleet config apply is running."
    static let operationRunning = "An operations action is running."
    static let serviceActionRunning = "A service action is running."
    static let inUse = "Muster is the app in front."

    /// One line for Settings while an update waits.
    static func waiting(_ reasons: [String]) -> String {
        (["An update is ready and waits until nothing is running."] + reasons)
            .joined(separator: BuildIdentityCopy.gap)
    }
}
