import Foundation
import LocalAuthentication

/// The owner's yes,  asked of Touch ID (or the login password where Touch ID is not set up).
///
/// It is asked in the view layer,  after the plan is on screen and before anything runs.  A
/// refusal,  a cancel and a Mac that cannot ask all mean no:  nothing runs,  and nothing is
/// retried.
enum OwnerCheck {
    enum Answer: Equatable {
        case approved
        case declined(String)
    }

    /// Ask the owner.
    @MainActor
    static func ask(reason: String) async -> Answer {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            let detail = error?.localizedDescription ?? "this Mac cannot ask"
            return .declined("Touch ID could not ask, so nothing ran:\(ServerStatusCopy.sentenceGap)\(detail)")
        }
        do {
            let approved = try await context.evaluatePolicy(
                .deviceOwnerAuthentication,
                localizedReason: reason
            )
            return approved ? .approved : .declined("Touch ID did not approve, so nothing ran.")
        } catch {
            return .declined("Touch ID did not approve, so nothing ran:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)")
        }
    }
}
