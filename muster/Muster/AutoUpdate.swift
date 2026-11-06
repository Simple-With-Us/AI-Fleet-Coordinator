import AppKit
import Combine
import Sparkle

/// Owns Sparkle for the life of the app.
///
/// Updates are checked hourly,  downloaded in the background,  and installed without a
/// prompt.  The install and its relaunch go through `UpdateInstallGate`,  so Muster is never
/// relaunched while an apply,  an operations run or a service action is still running,  and
/// never while the owner is using it.  The gate looks again whenever one of those owners of
/// work changes,  and when Muster stops being the app in front.
///
/// The pattern is the fleet's (CodeCaps' docs/AUTO-UPDATE.md is canonical,  Hog Hunter's
/// Sources/App/AutoUpdate.swift is the copy this follows);  muster/docs/AUTO-UPDATE.md says
/// what Muster adds.
@MainActor
final class AppUpdater: NSObject, ObservableObject, SPUUpdaterDelegate {
    static let shared = AppUpdater()

    let availability: AutoUpdateAvailability
    @Published private(set) var canCheckForUpdates = false
    /// What holds a downloaded update,  empty when nothing is waiting.
    @Published private(set) var waitingFor: [String] = []

    private var controller: SPUStandardUpdaterController?
    private var gate: UpdateInstallGate?
    private var canCheckObservation: NSKeyValueObservation?
    private var cancellables: Set<AnyCancellable> = []

    private override init() {
        availability = AutoUpdateAvailability.evaluate(info: Bundle.main.infoDictionary,
                                                       bundleURL: Bundle.main.bundleURL)
        super.init()
    }

    /// Starts Sparkle,  once,  when this build carries a feed and a key.
    func start(router: ServiceActionRouter, admin: FleetAdmin) {
        guard availability.isEnabled, controller == nil else { return }
        let gate = UpdateInstallGate(
            appIsActive: { NSApp?.isActive ?? true },
            probes: UpdateInstallGate.musterWork(apply: .shared, ops: .shared, router: router)
        )
        self.gate = gate
        let changes: [AnyPublisher<Void, Never>] = [
            NotificationCenter.default.publisher(for: NSApplication.didResignActiveNotification)
                .map { _ in () }.eraseToAnyPublisher(),
            ApplyCoordinator.shared.$sessions.map { _ in () }.eraseToAnyPublisher(),
            OpsController.shared.$phase.map { _ in () }.eraseToAnyPublisher(),
            OpsController.shared.$isRechecking.map { _ in () }.eraseToAnyPublisher(),
            router.objectWillChange.map { _ in () }.eraseToAnyPublisher(),
            admin.objectWillChange.map { _ in () }.eraseToAnyPublisher(),
        ]
        // `@Published` and `objectWillChange` fire before the value changes,  so the gate
        // looks on the next turn of the run loop,  when the change has landed.
        Publishers.MergeMany(changes)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.reevaluate() }
            .store(in: &cancellables)
        let controller = SPUStandardUpdaterController(startingUpdater: true,
                                                      updaterDelegate: self,
                                                      userDriverDelegate: nil)
        self.controller = controller
        canCheckObservation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
            let canCheck = updater.canCheckForUpdates
            Task { @MainActor in self?.canCheckForUpdates = canCheck }
        }
    }

    /// The Check For Updates command.  Sparkle's window opens in front.
    func checkForUpdates() {
        guard let controller else { return }
        NSApp.activate(ignoringOtherApps: true)
        controller.checkForUpdates(nil)
    }

    private func reevaluate() {
        gate?.reevaluate()
        waitingFor = (gate?.hasPendingInstall ?? false) ? (gate?.heldBy ?? []) : []
    }

    // MARK: - SPUUpdaterDelegate

    /// An update downloaded on its own.  Muster takes the install over and runs it once
    /// nothing is running and Muster is not in front.
    func updater(_ updater: SPUUpdater,
                 willInstallUpdateOnQuit item: SUAppcastItem,
                 immediateInstallationBlock immediateInstallHandler: @escaping () -> Void) -> Bool {
        guard let gate else { return false }
        gate.holdAutomaticInstall(immediateInstallHandler)
        waitingFor = gate.hasPendingInstall ? gate.heldBy : []
        return true
    }

    /// Any relaunch,  including one the owner asked for:  it waits while work is running.
    func updater(_ updater: SPUUpdater,
                 shouldPostponeRelaunchForUpdate item: SUAppcastItem,
                 untilInvokingBlock installHandler: @escaping () -> Void) -> Bool {
        guard let gate else { return false }
        let postponed = gate.shouldPostponeRelaunch(installHandler)
        // An automatic install can still be waiting when this relaunch goes ahead.
        waitingFor = gate.hasPendingInstall ? gate.heldBy : []
        return postponed
    }
}
