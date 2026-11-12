import Foundation
import LocalAuthentication

/// The owner's yes,  asked of the Mac's owner check (`.deviceOwnerAuthentication`):
/// Touch ID,  or the login password,  which that policy lets macOS offer in its place and
/// not only where Touch ID is not set up.  So the words on the screen say "approval",
/// never Touch ID alone.
///
/// It is asked in the view layer,  after the plan is on screen and before anything runs.  A
/// cancel or a refused approval leaves the plan on screen with a plain note,  so the owner
/// can approve it after all.  A Mac that cannot ask ends the sheet.  Nothing is ever retried
/// on its own.
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
