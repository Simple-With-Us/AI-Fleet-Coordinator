import Combine
import Foundation

// Starts the Mac's services again when they stop,  inside Muster,  only while Muster runs.
//
// What it does is decided by `ServicePolicyTable` (one policy per service) and
// `ServiceReconcileEngine` (one decision at a time),  and this file is the loop around them:
// after every read of the fleet it looks at each service,  and starts the ones the policy says
// to.  It only ever starts.  It never stops or restarts a service,  never touches one the owner
// stopped on purpose,  waits longer after every try,  and gives up after a few.
//
// A start goes down the same road as the owner's own Start:  the route the router would pick,
// the same helper,  the same plan,  the same run and the same audit row.  The one difference is
// that nobody is at a sheet,  so there is no Touch ID.  Whatever it did is said on the service's
// row,  in the menu and in the Services panel.
//
// The whole of it can be turned off in Settings.

/// How one try to start a service ended.
enum ReconcileRun: Equatable {
    /// The start ran and said it worked.
    case started
    /// The start did not run or did not work,  with the reason in the owner's words.
    case notStarted(String)
    /// Something else is in the way,  so nothing was tried and no try is counted.
    case skipped
}

@MainActor
final class ServiceReconciler: ObservableObject {
    /// What Muster says about each service it acted on,  by service id.
    @Published private(set) var notes: [String: ReconcileNote] = [:]
    /// True while a pass is running.
    @Published private(set) var isRunning = false

    private let isEnabled: () -> Bool
    private let isBlocked: () -> Bool
    private let readEntries: () -> [FleetServiceEntry]
    private let isStoppedByOwner: (String) -> Bool
    private let startService: (FleetServiceEntry) async -> ReconcileRun
    private let publishNotes: ([String: ReconcileNote]) -> Void
    private let clock: () -> Date
    private let config: ServiceReconcileConfig

    /// What Muster remembers about each service it started,  for as long as it runs.
    private(set) var records: [String: ReconcileRecord] = [:]
    /// The services whose state has been read once since Muster came up.
    private var seenKnown: Set<String> = []

    /// - Parameters:
    ///   - enabled:  The setting that turns the whole thing off.  Read on every pass.
    ///   - blocked:  True when nothing may be started now:  an action of the owner's is in the
    ///     way,  or the record of the owner's stops cannot be read.
    ///   - entries:  The services as the last read found them.
    ///   - stoppedByOwner:  Whether the owner stopped one on purpose,  asked live and not taken
    ///     from `entries`,  so a stop made since the last read still counts.
    ///   - start:  Starts one service down the owner's own route.
    ///   - publish:  Hands the notes to whoever shows them.
    init(enabled: @escaping () -> Bool,
         blocked: @escaping () -> Bool,
         entries: @escaping () -> [FleetServiceEntry],
         stoppedByOwner: @escaping (String) -> Bool,
         start: @escaping (FleetServiceEntry) async -> ReconcileRun,
         publish: @escaping ([String: ReconcileNote]) -> Void,
         config: ServiceReconcileConfig = ServiceReconcileConfig(),
         now: @escaping () -> Date = Date.init) {
        self.isEnabled = enabled
        self.isBlocked = blocked
        self.readEntries = entries
        self.isStoppedByOwner = stoppedByOwner
        self.startService = start
        self.publishNotes = publish
        self.config = config
        self.clock = now
    }

    /// Looks at every service once,  and starts the ones the policy says to.
    ///
    /// A pass that is already running is not joined:  the next read starts another.  A pass
    /// stops at the first start that something else got in the way of,  and the next read tries
    /// again,  with no try counted.
    func pass() async {
        guard !isRunning else { return }
        isRunning = true
        defer { isRunning = false }
        // Nothing is looked at while something else owns the road,  or while a stop the owner
        // made could be missing from the record.
        guard !isBlocked() else { return }
        let enabled = isEnabled()
        var started: Set<String> = []
        for entry in readEntries() {
            let id = entry.id
            let policy = ServicePolicyTable.policy(for: id)
            // "At login" lasts until the first time the service's state is known.
            let firstLook = !seenKnown.contains(id)
            if entry.state != .unknown { seenKnown.insert(id) }
            let record = records[id] ?? ReconcileRecord()
            let now = clock()
            let decision = ServiceReconcileEngine.decide(
                state: entry.state,
                policy: policy,
                stoppedByOwner: entry.stoppedByOwner || isStoppedByOwner(id),
                enabled: enabled,
                record: record,
                now: now,
                firstLook: firstLook,
                config: config)
            switch decision {
            case .skip:
                store(ServiceReconcileEngine.settle(record, state: entry.state, decision: decision,
                                                    now: now, config: config), for: id)
            case .start:
                // The owner may have acted while an earlier start ran,  so the road is checked again.
                guard !isBlocked(), !isStoppedByOwner(id) else { return publish(startedNow: started) }
                let run = await startService(entry)
                let ended = clock()
                switch run {
                case .skipped:
                    return publish(startedNow: started)
                case .started:
                    store(ServiceReconcileEngine.recordTry(record, policy: policy, outcome: .started, at: ended), for: id)
                    started.insert(id)
                case .notStarted(let reason):
                    store(ServiceReconcileEngine.recordTry(record, policy: policy,
                                                           outcome: .notStarted(reason), at: ended), for: id)
                }
            }
        }
        publish(startedNow: started)
    }

    private func store(_ record: ReconcileRecord, for id: String) {
        if record.isBlank { records[id] = nil } else { records[id] = record }
    }

    /// Makes the notes from what Muster remembers and what the last read found.
    ///
    /// A service Muster has just started reads as running until the next read says otherwise,
    /// because the read this pass looked at was taken before the start.
    private func publish(startedNow: Set<String>) {
        var made: [String: ReconcileNote] = [:]
        for entry in readEntries() {
            guard let record = records[entry.id] else { continue }
            let state: ManagedServiceState = startedNow.contains(entry.id) ? .running : entry.state
            let policy = ServicePolicyTable.policy(for: entry.id)
            if let note = ReconcileNote.make(record: record, state: state, policy: policy, config: config) {
                made[entry.id] = note
            }
        }
        notes = made
        publishNotes(made)
    }
}

// MARK: - The live wiring

extension ServiceReconciler {
    /// The reconciler as the app runs it:  the owner's setting,  the admin's services and the
    /// router's own route.
    static func live(admin: FleetAdmin,
                     router: ServiceActionRouter,
                     controller: OpsController,
                     defaults: UserDefaults = .standard) -> ServiceReconciler {
        let reconciler = ServiceReconciler(
            enabled: { MusterSettings.keepsServicesRunning(defaults: defaults) },
            blocked: { router.isBusy || admin.ownerStopsUnreadable },
            entries: { admin.entries },
            stoppedByOwner: { admin.isStoppedByOwner($0) },
            start: { entry in
                guard !router.isBusy else { return .skipped }
                guard let route = router.route(forStarting: entry) else {
                    return .notStarted(MachineMenuCopy.noRouteSentence(allowlist: router.allowlist, someRouted: false))
                }
                switch route {
                case .ops(let action):
                    guard let outcome = await controller.runUnattended(action) else { return .skipped }
                    if outcome.isSuccess(for: .yes) { return .started }
                    return .notStarted(outcome.detail.isEmpty ? "the helper gave no reason." : outcome.detail)
                case .admin(let managed, let serviceID):
                    await admin.perform(managed, serviceID: serviceID)
                    guard let result = admin.lastResult, result.serviceID == serviceID else { return .skipped }
                    return result.succeeded ? .started : .notStarted(result.reason ?? "no reason was given.")
                }
            },
            publish: { admin.setReconcileNotes($0) })
        admin.onRead = { [weak reconciler] in
            guard let reconciler else { return }
            Task { await reconciler.pass() }
        }
        return reconciler
    }
}
