import Combine
import Foundation

// The one way in for an action on a service,  from the menu or the panel.
//
// The router decides the route,  holds the allowlist,  and owns what the panel used to keep
// in view state:  the owner's stop is recorded and settled here,  and the fleet is read again
// after a run here,  so an action started from the menu settles with no panel on screen.
//
// There are two routes:
//
//   * `.ops`:  the plan sheet and Touch ID,  through `OpsController`.  Every pm2 row,  every
//     LaunchAgent the allowlist names,  and everything on the server
//   * `.admin`:  `FleetAdmin` directly,  for BotFleet Server and for a LaunchAgent the
//     allowlist does not name.  Their start and stop are not something an allowlist argv
//     can say,  so they have no plan,  no Touch ID and no audit row.  Stop still asks,  and
//     BotFleet Server's Restart asks when a fresh read finds work in flight

// MARK: - The route

/// Where one action goes.
enum MenuRoute: Equatable {
    case ops(OpsAction)
    case admin(ManagedServiceAction, serviceID: String)

    /// The machine the action runs on,  from the value that decides the route.
    var runsOn: FleetHost {
        switch self {
        case .ops(let action): return action.runsOn
        case .admin(_, let serviceID): return FleetServiceRegistry.descriptor(for: serviceID)?.host ?? .mac
        }
    }

    /// True when a sheet or an alert opens before anything runs.
    var asksFirst: Bool {
        switch self {
        case .ops: return true
        case .admin(let action, _): return action == .stop
        }
    }

    /// The route for one action on one service,  or nil when it has none.
    ///
    /// An allowlist that could not be read offers nothing on the direct route rather than
    /// falling back to it:  a gate that cannot be read does not open.  pm2 rows still plan,
    /// because the helper checks its own copy and the plan says what it finds.
    static func forService(_ action: ManagedServiceAction,
                           entry: FleetServiceEntry,
                           allowlist: OpsAllowlist?) -> MenuRoute? {
        let verb: OpsVerb
        switch action {
        case .start: verb = .start
        case .stop: verb = .stop
        case .restart: verb = .restart
        case .none: return nil
        }
        switch entry.kind {
        case .pm2(let appName):
            if let allowlist, !allowlist.verbs(forPM2App: appName).contains(verb) { return nil }
            return .ops(.pm2App(verb: verb, name: appName))
        case .launchAgent(let label):
            if entry.id == FleetServiceRegistry.server.id { return .admin(action, serviceID: entry.id) }
            guard let allowlist else { return nil }
            if FleetServiceActionRouting.usesFleetAdmin(serviceID: entry.id, label: label, allowlist: allowlist) {
                return .admin(action, serviceID: entry.id)
            }
            return allowlist.verbs(forMacAgent: label).contains(verb)
                ? .ops(.macAgent(verb: verb, label: label))
                : nil
        }
    }
}

// MARK: - What an action is called

/// What an action does,  for the words about it.
enum FleetActionVerb: Equatable {
    case start
    case stop
    case restart
    case readLogs
    /// An action with no single verb,  named by its own title.
    case other
}

/// The thing an action is about.
struct ServiceActionSubject: Equatable {
    let title: String
    let host: FleetHost
    let verb: FleetActionVerb

    /// The subject of an action on the plan path.
    init(action: OpsAction) {
        switch action {
        case .macAgent(let verb, _), .serverUnit(let verb, _), .pm2App(let verb, _):
            title = action.subjectTitle
            self.verb = FleetActionVerb(verb)
        case .logs:
            title = action.subjectTitle
            self.verb = .readLogs
        case .serverUpdate, .coolifyRedeploy, .knobSet, .knobUnset, .serverResource, .resourceStatus:
            title = action.title
            self.verb = .other
        }
        host = action.runsOn
    }

    /// The subject of an action on a service.
    init(title: String, host: FleetHost, action: ManagedServiceAction) {
        self.title = title
        self.host = host
        switch action {
        case .start: verb = .start
        case .stop: verb = .stop
        case .restart: verb = .restart
        case .none: verb = .other
        }
    }
}

extension FleetActionVerb {
    init(_ verb: OpsVerb) {
        switch verb {
        case .start: self = .start
        case .stop: self = .stop
        case .restart: self = .restart
        }
    }
}

/// Where the last action got to.
enum ServiceActionState: Equatable {
    case waiting(ServiceActionSubject)
    case running(ServiceActionSubject)
    case done(ServiceActionSubject, at: Date)
    case failed(ServiceActionSubject, reason: String)

    var subject: ServiceActionSubject {
        switch self {
        case .waiting(let subject), .running(let subject): return subject
        case .done(let subject, _): return subject
        case .failed(let subject, _): return subject
        }
    }
}

/// The words for an action's state,  one place for the menu,  the panel and the notice.
enum FleetActionWords {
    /// `Shellular did not restart on the Mac`
    static func didNot(_ action: ManagedServiceAction, title: String, host: FleetHost) -> String {
        let base: String
        switch action {
        case .start: base = "start"
        case .stop: base = "stop"
        case .restart: base = "restart"
        case .none: base = "change"
        }
        return "\(title) did not \(base) \(host.inProse)"
    }

    /// One sentence for an action's state.
    static func line(for state: ServiceActionState) -> String {
        let subject = state.subject
        let place = subject.host.inProse
        switch state {
        case .waiting:
            // The answer is always given on the Mac,  wherever the action will run.
            return "\(subject.title):  waiting for your answer \(FleetHost.mac.inProse)"
        case .running:
            switch subject.verb {
            case .start: return "Starting \(subject.title) \(place)\u{2026}"
            case .stop: return "Stopping \(subject.title) \(place)\u{2026}"
            case .restart: return "Restarting \(subject.title) \(place)\u{2026}"
            case .readLogs: return "Reading the logs of \(subject.title) \(place)\u{2026}"
            case .other: return "\(subject.title)\u{2026}"
            }
        case .done(_, let at):
            let time = FleetTime.clock(at)
            switch subject.verb {
            case .start: return "\(subject.title) started \(place) at \(time)."
            case .stop: return "\(subject.title) stopped \(place) at \(time)."
            case .restart: return "\(subject.title) restarted \(place) at \(time)."
            case .readLogs: return "The logs of \(subject.title) were read \(place) at \(time)."
            case .other: return "\(subject.title):  done at \(time)."
            }
        case .failed(_, let reason):
            switch subject.verb {
            case .start: return "\(subject.title) did not start \(place):  \(reason)"
            case .stop: return "\(subject.title) did not stop \(place):  \(reason)"
            case .restart: return "\(subject.title) did not restart \(place):  \(reason)"
            case .readLogs: return "The logs of \(subject.title) were not read \(place):  \(reason)"
            case .other: return "\(subject.title):  did not finish:  \(reason)"
            }
        }
    }

    /// The whole of what the Services panel shows under The Last Change Did Not Work,  so a
    /// reason the menu cut short can always be read there.
    ///
    /// There are two records.  The router holds the last action,  and the admin holds its own
    /// direct action and the two login toggles.  A direct action writes the same sentence to
    /// both,  which reads once.  Neither says which came last,  so different ones both show,
    /// and neither hides the other.
    static func panelProblem(actionState: ServiceActionState?, adminProblem: String?) -> String? {
        var parts: [String] = []
        if case .some(.failed) = actionState, let actionState {
            parts.append(line(for: actionState))
        }
        if let adminProblem, !parts.contains(adminProblem) {
            parts.append(adminProblem)
        }
        return parts.isEmpty ? nil : parts.joined(separator: "\n\n")
    }
}

// MARK: - The router

/// One action,  with the words on its button and the route that decides where it goes.
struct MenuAction: Equatable {
    let title: String
    let route: MenuRoute
    let subject: ServiceActionSubject
    /// The service row this action belongs to,  whose owner stop the router settles.
    let serviceID: String?
    /// What the action asks of that service.
    let managedAction: ManagedServiceAction?

    /// An action on one service row,  or nil when it has no route.
    static func forService(_ action: ManagedServiceAction,
                           entry: FleetServiceEntry,
                           allowlist: OpsAllowlist?) -> MenuAction? {
        guard let route = MenuRoute.forService(action, entry: entry, allowlist: allowlist) else { return nil }
        let word: String
        switch action {
        case .start: word = "Start"
        case .stop: word = "Stop"
        case .restart: word = "Restart"
        case .none: return nil
        }
        return MenuAction(title: titled(word, route: route),
                          route: route,
                          subject: ServiceActionSubject(title: entry.title, host: route.runsOn, action: action),
                          serviceID: entry.id,
                          managedAction: action)
    }

    /// An action on the plan path that belongs to no service row.
    static func plan(_ action: OpsAction, word: String) -> MenuAction {
        let route = MenuRoute.ops(action)
        return MenuAction(title: titled(word, route: route),
                          route: route,
                          subject: ServiceActionSubject(action: action),
                          serviceID: nil,
                          managedAction: nil)
    }

    /// `Restart On The Mac`,  with an ellipsis when a sheet or an alert opens first.
    private static func titled(_ word: String, route: MenuRoute) -> String {
        "\(word) \(route.runsOn.onTitle)\(route.asksFirst ? "\u{2026}" : "")"
    }
}

/// Decides each action's route and settles it,  whichever surface started it.
@MainActor
final class ServiceActionRouter: ObservableObject {
    /// How an action began.
    enum Started: Equatable {
        /// The plan sheet is up,  so a window has to be on screen for it.
        case planOpened
        /// The direct route took it.
        case direct
        /// Another action is in the way,  so nothing happened.
        case refused
    }

    /// What the alert on the direct route has to say.
    struct ConfirmRequest: Equatable {
        /// `Stop` or `Restart`,  for the alert's title and its button.
        let word: String
        let title: String
        let host: FleetHost
        let message: String
    }

    /// Where the last action got to.  A finished one stays until the next action starts.
    @Published private(set) var actionState: ServiceActionState?
    /// The allowlist as the Mac's checkout carries it,  or nil when it could not be read.
    @Published private(set) var allowlist: OpsAllowlist?

    private let admin: FleetAdmin
    private let controller: OpsController
    private let readAllowlist: () -> OpsAllowlist?
    private let confirm: @MainActor (ConfirmRequest) -> Bool
    private let notifier: ApplyNotifying
    private let settleDelay: TimeInterval
    private var cancellables: Set<AnyCancellable> = []
    /// The action the sheet is showing.
    private var subject: ServiceActionSubject?
    /// The service whose owner stop is waiting for the sheet's action to end.
    private var pendingServiceID: String?
    /// True from a direct action's click to its result,  which covers its alert too.
    private var directInFlight = false

    /// - Parameter controller:  The app's one controller unless a test gives its own.
    init(admin: FleetAdmin,
         controller: OpsController? = nil,
         readAllowlist: @escaping () -> OpsAllowlist? = {
             OpsAllowlist.read(at: OpsAllowlist.url(repoRoot: FleetConfigLocation.resolve().repoRoot))
         },
         confirm: @escaping @MainActor (ConfirmRequest) -> Bool,
         notifier: ApplyNotifying = SystemApplyNotifier(),
         settleDelay: TimeInterval = 2) {
        self.admin = admin
        self.controller = controller ?? OpsController.shared
        self.readAllowlist = readAllowlist
        self.confirm = confirm
        self.notifier = notifier
        self.settleDelay = settleDelay
        self.allowlist = readAllowlist()
        self.controller.$phase
            .sink { [weak self] phase in self?.phaseChanged(phase) }
            .store(in: &cancellables)
        self.controller.$completedRuns
            .dropFirst()
            .sink { [weak self] _ in self?.runCompleted() }
            .store(in: &cancellables)
    }

    /// True while any action,  on either route,  stands in the way of another.
    var isBusy: Bool {
        directInFlight || admin.busyServiceID != nil || controller.isPresenting
    }

    /// Reads the allowlist again.
    func reloadAllowlist() {
        allowlist = readAllowlist()
    }

    /// The action for one service row and one verb,  on the route the router would use.
    func action(_ action: ManagedServiceAction, on entry: FleetServiceEntry) -> MenuAction? {
        MenuAction.forService(action, entry: entry, allowlist: allowlist)
    }

    /// Starts an action.  The caller brings a window up when this says a plan opened.
    @discardableResult
    func start(_ action: MenuAction) -> Started {
        guard !isBusy else { return .refused }
        switch action.route {
        case .ops(let opsAction):
            guard controller.plan(opsAction) else { return .refused }
            if let managed = action.managedAction, let serviceID = action.serviceID {
                admin.beginControllerAction(managed, serviceID: serviceID)
                pendingServiceID = serviceID
            }
            subject = action.subject
            actionState = .waiting(action.subject)
            return .planOpened
        case .admin(let managed, let serviceID):
            objectWillChange.send()
            directInFlight = true
            actionState = nil
            Task { [weak self] in
                await self?.runDirect(action, managed: managed, serviceID: serviceID)
            }
            return .direct
        }
    }

    /// Plans an action that belongs to no service row.
    @discardableResult
    func plan(_ action: OpsAction) -> Started {
        start(MenuAction.plan(action, word: action.title))
    }

    // MARK: The direct route

    private func runDirect(_ action: MenuAction, managed: ManagedServiceAction, serviceID: String) async {
        defer {
            objectWillChange.send()
            directInFlight = false
        }
        // A stop always asks.  A restart runs at once unless a fresh read says it could end
        // work in flight:  BotFleet Server refuses a stop for that work,  and a restart does
        // not ask it.
        var message: String?
        var word = "Stop"
        if managed == .stop {
            message = await admin.stopConfirmation(for: serviceID)
        } else if managed == .restart {
            message = await admin.restartWarning(for: serviceID)
            word = "Restart"
        }
        if let message {
            let request = ConfirmRequest(word: word, title: action.subject.title,
                                         host: action.subject.host, message: message)
            guard confirm(request) else { return }
        }
        actionState = .running(action.subject)
        await admin.perform(managed, serviceID: serviceID)
        guard let result = admin.lastResult, result.serviceID == serviceID else {
            actionState = nil
            return
        }
        let state: ServiceActionState = result.succeeded
            ? .done(action.subject, at: result.at)
            : .failed(action.subject, reason: result.reason ?? "no reason was given.")
        actionState = state
        // The menu closed on the click,  so a notice is how the owner hears how it went.
        let notifier = self.notifier
        let body = FleetActionWords.line(for: state)
        let title = "Action \(action.subject.host.onTitle)"
        Task { await notifier.post(title: title, body: body) }
    }

    // MARK: The plan route

    /// Follows the sheet's action to its end.
    ///
    /// The value comes from the publisher:  `@Published` calls this before the property
    /// changes,  so reading the controller here would see the phase before this one.
    private func phaseChanged(_ phase: OpsController.Phase) {
        objectWillChange.send()
        switch phase {
        case .idle:
            // The sheet closed.  A run that ended keeps its result,  and a plan that was
            // only closed says nothing:  nothing happened.
            settle(succeeded: false)
            subject = nil
            switch actionState {
            case .some(.waiting): actionState = nil
            case .some(.running): if !directInFlight { actionState = nil }
            case .some(.done), .some(.failed), .none: break
            }
        case .planning(let action), .review(let action, _):
            actionState = .waiting(sheetSubject(for: action))
        case .running(let action, _):
            actionState = .running(sheetSubject(for: action))
        case .finished(let action, let outcome):
            let succeeded = outcome.isSuccess(for: .yes)
            settle(succeeded: succeeded)
            let made = sheetSubject(for: action)
            if succeeded {
                actionState = .done(made, at: Date())
            } else if outcome.kind == .submitted, outcome.executionState != "expired" {
                // Accepted and not yet verified,  which is not a failure.  The sheet says so,
                // and this line goes when the sheet closes.
                actionState = .running(made)
            } else {
                actionState = .failed(made, reason: outcome.detail)
            }
        case .failed(let action, let reason):
            settle(succeeded: false)
            actionState = .failed(sheetSubject(for: action), reason: reason)
        }
    }

    /// The subject of the sheet's action,  including one started from another panel.
    private func sheetSubject(for action: OpsAction) -> ServiceActionSubject {
        if let subject { return subject }
        let made = ServiceActionSubject(action: action)
        subject = made
        return made
    }

    /// Settles the owner's stop:  a run that did what it was asked keeps the record,  and
    /// anything else,  the sheet closed without a run included,  restores it.
    private func settle(succeeded: Bool) {
        guard let serviceID = pendingServiceID else { return }
        admin.endControllerAction(serviceID: serviceID, succeeded: succeeded)
        pendingServiceID = nil
    }

    /// A run ended,  so the services are read again:  once now,  and once more after a
    /// moment,  because a service that just started may not answer its health probe on the
    /// first read after the helper exits.
    private func runCompleted() {
        let delay = UInt64(max(settleDelay, 0) * 1_000_000_000)
        Task { [weak self] in
            await self?.admin.refresh()
            try? await Task.sleep(nanoseconds: delay)
            await self?.admin.refresh()
        }
    }
}
