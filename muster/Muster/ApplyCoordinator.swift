import Foundation
import SwiftUI
import UserNotifications

// The app-lifetime owner of every plan and apply.  Views only render what is published here, so
// closing a window or a sheet never cancels an apply in flight.  Entry point for a red cell:
//
//     ApplyCoordinator.shared.beginPlan(seat: "CLAUDE", ruleID: "claude-hooks-lane-guard")
//
// The flow is plan, ask once, apply in the background, re-prove.  There is no URL scheme,
// AppleScript, App Intents or Shortcuts entry point, and nothing here retries a refusal.

// MARK: - Session

struct ApplySession: Identifiable, Equatable {
    enum Phase: Equatable {
        case planning
        case review(ApplyPlan, kind: String)
        case applying(ApplyPlan)
        case reproving
        case finished(ApplyOutcome)
        case failedToPlan(String)
    }

    let id: UUID
    let seat: String
    let ruleID: String
    var kind: String?
    var phase: Phase
    var notice: String?
    var startedAt: Date

    var isInFlight: Bool {
        switch phase {
        case .planning, .applying, .reproving: return true
        case .review, .finished, .failedToPlan: return false
        }
    }

    /// True while the engine may be writing, or the result is being re-proved.
    var isWriting: Bool {
        switch phase {
        case .applying, .reproving: return true
        default: return false
        }
    }

    var isFinished: Bool {
        switch phase {
        case .finished, .failedToPlan: return true
        default: return false
        }
    }

    var key: String { ApplyCoordinator.key(seat: seat, ruleID: ruleID) }
}

// MARK: - Notifications

protocol ApplyNotifying: Sendable {
    func post(title: String, body: String) async
}

struct SystemApplyNotifier: ApplyNotifying {
    /// Ask the owner once for permission to post.
    ///
    /// The question is asked when the app starts,  and never on an apply's path:
    /// a permission prompt is the owner's to answer,  and an apply that waits for
    /// the answer holds its own result hostage  (Kody review 2026-10-21).  The
    /// apply publishes its result before the notification goes out,  so the
    /// notification can never hold the result.
    static func requestPermissionOnce() {
        _ = Task {
            // A process without an app bundle (a test host) cannot use the
            // notification center.
            guard Bundle.main.bundleURL.pathExtension == "app" else { return }
            _ = try? await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound])
        }
    }

    func post(title: String, body: String) async {
        // A process without an app bundle (a test host) cannot use the notification center.
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        let center = UNUserNotificationCenter.current()
        // The permission was asked for when the app started,  so this reads an
        // answer that is already settled.  It never prompts here,  so the apply
        // never waits on the owner.
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(
            identifier: UUID().uuidString, content: content, trigger: nil
        )
        try? await center.add(request)
    }
}

// MARK: - Activity

/// Keeps the app awake while work is in flight.
///
/// The system's own hold cannot be seen from outside the process,  so the tests
/// substitute a holder of their own  and can tell that the app is held while a
/// notification is still waiting.
protocol ApplyActivityHolding: Sendable {
    /// Hold the app awake.  The token ends the hold.
    func begin(reason: String) -> NSObjectProtocol?
    /// Release the hold a token carries.
    func end(_ token: NSObjectProtocol)
}

/// The system's own hold,  which keeps automatic and sudden termination off.
struct SystemApplyActivity: ApplyActivityHolding {
    func begin(reason: String) -> NSObjectProtocol? {
        ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .suddenTerminationDisabled, .automaticTerminationDisabled],
            reason: reason
        )
    }

    func end(_ token: NSObjectProtocol) {
        ProcessInfo.processInfo.endActivity(token)
    }
}

// MARK: - Coordinator

@MainActor
final class ApplyCoordinator: ObservableObject {
    static let shared = ApplyCoordinator()

    @Published private(set) var sessions: [ApplySession] = []
    /// The session shown in the sheet.  Dismissing the sheet never touches the apply.
    @Published var presentedSessionID: UUID?
    /// The latest proof for each rule an apply has re-checked, keyed by `key(seat:ruleID:)`.
    @Published private(set) var provenResults: [String: FleetConfigResult] = [:]

    private let runner: FleetConfigRunning
    private let notifier: ApplyNotifying
    private let activityHolder: ApplyActivityHolding
    private var tasks: [UUID: Task<Void, Never>] = [:]
    private var activity: NSObjectProtocol?

    init(
        runner: FleetConfigRunning = FleetConfigCLI(),
        notifier: ApplyNotifying = SystemApplyNotifier(),
        activityHolder: ApplyActivityHolding = SystemApplyActivity()
    ) {
        self.runner = runner
        self.notifier = notifier
        self.activityHolder = activityHolder
    }

    nonisolated static func key(seat: String, ruleID: String) -> String { "\(seat)/\(ruleID)" }

    func session(id: UUID) -> ApplySession? { sessions.first { $0.id == id } }

    func latestResult(seat: String, ruleID: String) -> FleetConfigResult? {
        provenResults[Self.key(seat: seat, ruleID: ruleID)]
    }

    var hasWorkInFlight: Bool { sessions.contains { $0.isInFlight } }

    // MARK: Plan

    /// Starts the read-only plan for one red cell and presents it.  `kind` is optional:  without
    /// it the seat's plan is read first to find the rule's kind.
    @discardableResult
    func beginPlan(seat: String, ruleID: String, kind: String? = nil) -> UUID {
        let key = Self.key(seat: seat, ruleID: ruleID)
        if let existing = sessions.first(where: { $0.key == key && !$0.isFinished }) {
            presentedSessionID = existing.id
            return existing.id
        }
        sessions.removeAll { $0.key == key }
        let session = ApplySession(
            id: UUID(), seat: seat, ruleID: ruleID, kind: kind,
            phase: .planning, notice: nil, startedAt: Date()
        )
        sessions.append(session)
        presentedSessionID = session.id
        tasks[session.id] = Task { [weak self] in
            await self?.runPlan(sessionID: session.id)
        }
        return session.id
    }

    private func runPlan(sessionID: UUID) async {
        defer { tasks[sessionID] = nil }
        guard let session = session(id: sessionID) else { return }
        do {
            var kind = session.kind
            if kind == nil {
                let seatPlan = try await readPlan(seat: session.seat, kind: nil)
                guard let entry = seatPlan.entry(ruleID: session.ruleID) else {
                    update(sessionID) {
                        $0.phase = .failedToPlan(ApplyCopy.join(
                            "The plan has no change for this rule.",
                            "It may already match the manifest."
                        ))
                    }
                    return
                }
                kind = entry.kind
                update(sessionID) { $0.kind = entry.kind }
            }
            guard let kind else { return }
            let plan = try await readPlan(seat: session.seat, kind: kind)
            update(sessionID) { $0.phase = .review(plan, kind: kind) }
        } catch {
            update(sessionID) { $0.phase = .failedToPlan(error.localizedDescription) }
        }
    }

    private func readPlan(seat: String, kind: String?) async throws -> ApplyPlan {
        let invocation = try FleetConfigInvocation.planApply(seat: seat, kind: kind)
        let result = try await runner.run(invocation)
        if result.timedOut { throw FleetConfigCLIError.timedOut(Int(invocation.timeout)) }
        guard result.exitCode == 0 else {
            throw FleetConfigCLIError.engineRefused(
                exitCode: result.exitCode, message: result.stderrTail
            )
        }
        return try ApplyPlanParser.parse(result.stdout)
    }

    // MARK: Apply

    /// The one confirmation.  Re-reads the plan first and refuses to apply when it differs from
    /// the plan the owner reviewed.
    func confirmApply(sessionID: UUID) {
        guard let session = session(id: sessionID),
              case .review(let reviewed, let kind) = session.phase,
              let target = reviewed.entry(ruleID: session.ruleID),
              target.autofix.isAllowed,
              tasks[sessionID] == nil else { return }
        update(sessionID) { $0.phase = .applying(reviewed); $0.notice = nil }
        beginActivity()
        tasks[sessionID] = Task { [weak self] in
            await self?.runApply(sessionID: sessionID, reviewed: reviewed, kind: kind)
        }
    }

    private func runApply(sessionID: UUID, reviewed: ApplyPlan, kind: String) async {
        defer {
            tasks[sessionID] = nil
            endActivityIfIdle()
        }
        guard let session = session(id: sessionID) else { return }
        do {
            let fresh = try await readPlan(seat: session.seat, kind: kind)
            guard fresh.fingerprint == reviewed.fingerprint,
                  fresh.entry(ruleID: session.ruleID)?.autofix.isAllowed == true else {
                update(sessionID) {
                    $0.phase = .review(fresh, kind: kind)
                    $0.notice = ApplyCopy.join(
                        "The plan changed after you reviewed it, so nothing was applied.",
                        "Review the new plan before applying."
                    )
                }
                return
            }
            let invocation = try FleetConfigInvocation.apply(seat: session.seat, kind: kind)
            let applyResult = try await runner.run(invocation)

            update(sessionID) { $0.phase = .reproving }
            var reproved: FleetConfigResult?
            if applyResult.exitCode != 64 {
                reproved = try? await runner
                    .status(seat: session.seat, kind: kind, includeDoctors: false)
                    .result(seat: session.seat, ruleID: session.ruleID)
            }
            if let reproved { provenResults[session.key] = reproved }

            let outcome = ApplyOutcome.classify(
                applyResult: applyResult, targetRuleID: session.ruleID, reproved: reproved
            )
            // The result is published before the notification goes out.  A notification
            // can wait on the system,  and a session that is still open in .reproving
            // blocks dismiss,  holds the app awake and stops a new plan,  so the owner
            // would never see the green or the red.  The notification is posted off the
            // session's completion path,  so it can never hold the result hostage.
            update(sessionID) { $0.phase = .finished(outcome) }
            announceInBackground(session: session, outcome: outcome)
        } catch {
            let outcome = ApplyOutcome.failed(error.localizedDescription)
            update(sessionID) { $0.phase = .finished(outcome) }
            if let session = self.session(id: sessionID) {
                announceInBackground(session: session, outcome: outcome)
            }
        }
    }

    /// Posts the result without letting the notification hold the result.
    ///
    /// The post is off the session's completion path,  so a notification that waits
    /// on the system cannot keep the session open in .reproving,  where it would
    /// block dismiss and stop a new plan.
    ///
    /// The hold is taken here,  before the task is handed to the actor,  because the
    /// apply's own hold is released when the apply ends,  which happens before the
    /// task can run:  a hold taken inside the task would leave a window where the
    /// system can terminate the app between the apply ending and the post starting,
    /// and the notification is the only thing that tells the owner the apply ended.
    private func announceInBackground(session: ApplySession, outcome: ApplyOutcome) {
        let token = activityHolder.begin(reason: "Announcing the result of an apply")
        Task {
            await announce(session: session, outcome: outcome, token: token)
        }
    }

    private func announce(
        session: ApplySession, outcome: ApplyOutcome, token: NSObjectProtocol?
    ) async {
        defer {
            if let token = token { activityHolder.end(token) }
        }
        let title = outcome.isGreen ? "Apply Finished" : "Apply Needs Attention"
        let body = "\(session.ruleID) on \(session.seat): \(outcome.summary)"
        await notifier.post(title: title, body: body)
    }

    // MARK: Housekeeping

    /// Closes the sheet or discards a session that has not started writing.
    func dismiss(sessionID: UUID) {
        if presentedSessionID == sessionID { presentedSessionID = nil }
        guard let session = session(id: sessionID), !session.isInFlight else { return }
        if case .review = session.phase { sessions.removeAll { $0.id == sessionID } }
        if session.isFinished, case .failedToPlan = session.phase {
            sessions.removeAll { $0.id == sessionID }
        }
    }

    func present(sessionID: UUID) { presentedSessionID = sessionID }

    func clearFinished() {
        sessions.removeAll { $0.isFinished }
    }

    private func update(_ id: UUID, _ change: (inout ApplySession) -> Void) {
        guard let index = sessions.firstIndex(where: { $0.id == id }) else { return }
        change(&sessions[index])
    }

    // An apply in flight must not be cut short by the system:  hold the app awake and keep
    // automatic and sudden termination off until the last apply ends.
    private func beginActivity() {
        guard activity == nil else { return }
        activity = activityHolder.begin(reason: "Applying a fleet config fix")
    }

    private func endActivityIfIdle() {
        guard !sessions.contains(where: \.isWriting), let token = activity else { return }
        activityHolder.end(token)
        activity = nil
    }
}
