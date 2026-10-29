import Foundation

/// Reads one secret by its name, at the moment a request needs it.
///
/// Infisical is the fleet's only source of secrets, so no token is kept in a
/// settings file:  a server host or a VM provider names its secret, and the
/// value is read through `fleet-secrets get NAME` when a request needs it.
/// The value is never written anywhere and never shown.
public protocol SecretResolving {
    func secret(named name: String) throws -> String
}

/// Why a secret could not be read.  Only the secret's name is ever reported.
public enum FleetSecretError: LocalizedError, Equatable {
    case invalidName(String)
    case missing(String)
    case unavailable(String)

    public var errorDescription: String? {
        switch self {
        case .invalidName:
            return "A secret name is capital letters, digits and underscores."
        case .missing(let name):
            return "\(name) is not in Infisical.  Add it there:  Muster keeps no copy of it."
        case .unavailable(let name):
            return "fleet-secrets could not read \(name) in time."
        }
    }
}

/// The secrets the owner keeps in Infisical, read through `fleet-secrets`.
public struct FleetSecrets: SecretResolving {
    private let commands: CommandRunning
    private let home: URL

    public init(commands: CommandRunning = ProcessCommandRunner(),
                home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.commands = commands
        self.home = home
    }

    /// True when the name is one a secret can have.
    ///
    /// The name is passed to a command, so it is checked first:  a name that
    /// starts with a dash, or carries anything but capital letters, digits
    /// and underscores, is refused.
    public static func isValidName(_ name: String) -> Bool {
        guard let first = name.unicodeScalars.first, name.count <= 128 else { return false }
        guard CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZ").contains(first) else { return false }
        return name.range(of: "[^A-Z0-9_]", options: .regularExpression) == nil
    }

    public func secret(named name: String) throws -> String {
        guard Self.isValidName(name) else { throw FleetSecretError.invalidName(name) }
        // A menu bar app inherits no shell PATH, so `fleet-secrets` (which
        // lives in `~/.local/bin`) is named by its full path.
        let bin = home.appendingPathComponent(".local/bin")
        let environment = [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:\(bin.path)",
            "HOME": home.path,
        ]
        let value: String
        do {
            value = try commands.run(bin.appendingPathComponent("fleet-secrets").path, ["get", name],
                                     environment: environment, timeout: 20)
        } catch is CommandExitError {
            // `fleet-secrets get` exits non-zero when the key is absent.
            throw FleetSecretError.missing(name)
        } catch {
            throw FleetSecretError.unavailable(name)
        }
        guard !value.isEmpty else { throw FleetSecretError.missing(name) }
        return value
    }
}

/// Runs blocking work on a dispatch queue and awaits its result.
///
/// `fleet-secrets get` can take up to its 20-second timeout.  Run inline, it
/// would hold one of the cooperative pool's few threads for that long and
/// stall every other fleet read;  a global queue thread is the one that
/// waits instead.
func awaitOffThePool<T>(_ work: @escaping () throws -> T) async throws -> T {
    try await withCheckedThrowingContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            continuation.resume(with: Result { try work() })
        }
    }
}

/// True when an error means the caller's task was cancelled.
///
/// URLSession reports a cancelled task as `URLError.cancelled`;  either form
/// is passed on as `CancellationError`, never mapped to a server failure.
func isCancellation(_ error: Error) -> Bool {
    error is CancellationError || (error as? URLError)?.code == .cancelled
}
