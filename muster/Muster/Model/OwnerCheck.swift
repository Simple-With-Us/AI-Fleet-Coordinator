import Foundation
import LocalAuthentication

/// The owner's yes,  asked of the Mac's owner check (`.deviceOwnerAuthentication`):
/// Touch ID,  or the login password,  which that policy lets macOS offer in its place and
/// not only where Touch ID is not set up.  So the words on the screen say "approval",
/// never Touch ID alone.
///
/// Action sheets ask after showing their plan; settings autosave asks automatically before
/// sending the edit.  Cancellation keeps the plan or draft, and never retries a write.
enum OwnerCheck {
    /// Ask the owner.
    @MainActor
    static func ask(reason: String) async -> OpsApprovalAnswer {
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            let detail = error?.localizedDescription ?? "the Mac cannot ask"
            return .unavailable("This Mac cannot ask for approval, so nothing ran:\(ServerStatusCopy.sentenceGap)\(detail)")
        }
        do {
            let approved = try await context.evaluatePolicy(
                .deviceOwnerAuthentication,
                localizedReason: reason
            )
            return approved ? .approved : .notApproved("Not approved, so nothing ran.")
        } catch {
            return answer(for: error)
        }
    }

    /// The words for an approval that did not happen.
    private static func answer(for error: Error) -> OpsApprovalAnswer {
        let gap = ServerStatusCopy.sentenceGap
        switch (error as? LAError)?.code {
        case .some(.userCancel), .some(.appCancel):
            return .notApproved("You canceled the approval, so nothing ran.")
        case .some(.systemCancel):
            return .notApproved("The approval was interrupted, so nothing ran.\(gap)You can approve it again.")
        case .some(.authenticationFailed):
            return .notApproved("This Mac did not accept the approval, so nothing ran.")
        case .some(.passcodeNotSet), .some(.notInteractive):
            return .unavailable("This Mac cannot ask for approval, so nothing ran:\(gap)\(error.localizedDescription)")
        default:
            return .notApproved("The approval did not finish, so nothing ran:\(gap)\(error.localizedDescription)")
        }
    }
}
