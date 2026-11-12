// TouchIDGate:  ask the owner for Touch ID once, and exit 0 only when it was confirmed.
//
// Usage:  TouchIDGate --reason "the sentence the owner reads"
//
// Exit codes, mirrored by scripts/fleet_config/touchid.py:
//   0   the owner confirmed with Touch ID
//   2   Touch ID is unavailable on this machine (not enrolled, no hardware)
//   3   the evaluation failed or the owner cancelled
//   64  a usage error
//
// The gate exists so no agent on this Mac can write a hook or a plugin unattended.  A
// missing confirmation is a refusal, never a pass.

import Foundation
import LocalAuthentication

@main
struct TouchIDGate {
    static func main() {
        var reason = "Confirm the fleet config write"
        var args = Array(CommandLine.arguments.dropFirst())
        while !args.isEmpty {
            let arg = args.removeFirst()
            switch arg {
            case "--reason":
                guard !args.isEmpty else {
                    fail(64, "usage: TouchIDGate --reason TEXT")
                }
                reason = args.removeFirst()
            case "--help", "-h":
                print("usage: TouchIDGate --reason TEXT")
                exit(0)
            default:
                fail(64, "unknown argument \(arg)")
            }
        }

        let context = LAContext()
        context.localizedCancelTitle = "Cancel"
        var availabilityError: NSError?
        guard context.canEvaluatePolicy(
            .deviceOwnerAuthenticationWithBiometrics,
            error: &availabilityError
        ) else {
            let detail = availabilityError?.localizedDescription ?? "no reason given"
            fail(2, "Touch ID is not available on this machine: \(detail)")
        }

        let semaphore = DispatchSemaphore(value: 0)
        var confirmed = false
        var failure = ""
        context.evaluatePolicy(
            .deviceOwnerAuthenticationWithBiometrics,
            localizedReason: reason
        ) { success, evaluationError in
            confirmed = success
            if let evaluationError = evaluationError {
                failure = evaluationError.localizedDescription
            }
            semaphore.signal()
        }
        semaphore.wait()

        guard confirmed else {
            fail(3, "the Touch ID confirmation did not succeed: \(failure)")
        }
        exit(0)
    }

    static func fail(_ code: Int32, _ message: String) -> Never {
        FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
        exit(code)
    }
}
