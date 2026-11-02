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

// MARK: - Secret names and doctor (names and booleans only)

/// One path's secret names.  Names only:  this type cannot hold a value.
public struct SecretNameGroup: Equatable {
    public var path: String
    public var names: [String]
    public init(path: String, names: [String]) { self.path = path; self.names = names }
}

public extension FleetSecrets {
    /// True when a path is one `fleet-secrets names --path` may be asked for.
    static func isValidPath(_ path: String) -> Bool {
        guard path.hasPrefix("/"), path.count <= 128 else { return false }
        return path.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.contains($0) || "/_-".unicodeScalars.contains($0)
        }
    }

    /// Parse `names` answers grouped by path.  Any line that is not exactly a
    /// valid secret name refuses the whole answer (fail closed):  a value-shaped
    /// line is never rendered as a name.
    static func parseNamesByPath(_ answers: [(path: String, text: String)]) throws -> [SecretNameGroup] {
        try answers.map { answer in
            guard isValidPath(answer.path) else { throw FleetSecretError.invalidName(answer.path) }
            let lines = answer.text.split(whereSeparator: \.isNewline).map(String.init)
            for line in lines where !isValidName(line) { throw FleetSecretError.invalidName(line) }
            return SecretNameGroup(path: answer.path, names: lines.sorted())
        }
    }

    private var secretsEnvironment: [String: String] {
        let bin = home.appendingPathComponent(".local/bin")
        return ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin:\(bin.path)", "HOME": home.path]
    }
    private var secretsExecutable: String {
        home.appendingPathComponent(".local/bin/fleet-secrets").path
    }

    /// Names at one path, through the `names` verb only.  This path never calls `get`.
    func secretNames(path: String) throws -> [String] {
        guard Self.isValidPath(path) else { throw FleetSecretError.invalidName(path) }
        let text: String
        do {
            text = try commands.run(secretsExecutable, ["names", "--path", path],
                                    environment: secretsEnvironment, timeout: 20)
        } catch is CommandExitError { throw FleetSecretError.unavailable(path) }
        catch { throw FleetSecretError.unavailable(path) }
        return try Self.parseNamesByPath([(path, text)]).first?.names ?? []
    }

    /// Names grouped by path, for the Controls panel.
    func secretNamesGrouped(paths: [String] = ["/"]) throws -> [SecretNameGroup] {
        try Self.parseNamesByPath(paths.map { ($0, try secretNamesText(path: $0)) })
    }

    private func secretNamesText(path: String) throws -> String {
        guard Self.isValidPath(path) else { throw FleetSecretError.invalidName(path) }
        do {
            return try commands.run(secretsExecutable, ["names", "--path", path],
                                    environment: secretsEnvironment, timeout: 20)
        } catch { throw FleetSecretError.unavailable(path) }
    }

    /// Parse `doctor --json`:  only booleans, counts and short token strings
    /// under keys that cannot name a credential survive.  Anything else is dropped.
    static func parseDoctor(_ text: String) throws -> [String: String] {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw FleetSecretError.unavailable("doctor")
        }
        var out: [String: String] = [:]
        for (key, value) in object {
            let lowered = key.lowercased()
            if lowered.contains("secret") || lowered.contains("token")
                || lowered.contains("password") || lowered.contains("value") { continue }
            switch value {
            case let bool as Bool: out[key] = bool ? "true" : "false"
            case let number as NSNumber: out[key] = number.stringValue
            case let string as String
                where string.count <= 40 && string.range(of: "[^A-Za-z0-9._-]", options: .regularExpression) == nil:
                out[key] = string
            default: continue
            }
        }
        return out
    }

    /// The doctor report as display rows (key to rendered boolean/count/token).
    func doctorReport() throws -> [String: String] {
        let text: String
        do {
            text = try commands.run(secretsExecutable, ["doctor", "--json"],
                                    environment: secretsEnvironment, timeout: 20)
        } catch { throw FleetSecretError.unavailable("doctor") }
        return try Self.parseDoctor(text)
    }
}

// MARK: - Recall admin settings (masked display only)

/// Where the recall admin surface lives and which Infisical key NAME holds its
/// token.  Only the key name is stored, never the token.
public struct RecallSettingsConfig: Equatable {
    public var baseURLString: String
    public var tokenKeyName: String
    public init(baseURLString: String, tokenKeyName: String) {
        self.baseURLString = baseURLString; self.tokenKeyName = tokenKeyName
    }
    public static let baseURLKey = "musterRecallBaseURL"
    public static let tokenKeyNameKey = "musterRecallTokenKeyName"
    public static let defaultTokenKeyName = "RECALL_ADMIN_TOKEN"
    public static func load(defaults: UserDefaults = .standard) -> RecallSettingsConfig {
        RecallSettingsConfig(
            baseURLString: defaults.string(forKey: baseURLKey) ?? "",
            tokenKeyName: defaults.string(forKey: tokenKeyNameKey) ?? defaultTokenKeyName)
    }
    public func save(defaults: UserDefaults = .standard) {
        defaults.set(baseURLString, forKey: Self.baseURLKey)
        defaults.set(tokenKeyName, forKey: Self.tokenKeyNameKey)
    }
}

/// One masked settings row:  the display is only ever `set` or `empty`.
public struct RecallSettingRow: Equatable, Identifiable {
    public var name: String
    public var display: String
    public var id: String { name }
}

public enum RecallSettingsError: LocalizedError, Equatable {
    case invalidBaseURL
    case invalidTokenKeyName
    case invalidResponse
    public var errorDescription: String? {
        switch self {
        case .invalidBaseURL: return "The recall base URL must be an https URL with a host.\u{00A0} Nothing was sent."
        case .invalidTokenKeyName: return "The token key name is capital letters, digits and underscores."
        case .invalidResponse: return "The recall settings answer could not be read.\u{00A0} Nothing was shown."
        }
    }
}

/// Refuses every redirect: the recall request carries a bearer [REDACTED], and a
/// 3xx must never forward it to another origin (or downgrade the scheme).
final class RecallRedirectBlocker: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public enum RecallSettingsClient {
    /// The only session the bearer rides on: ephemeral, redirect-blocking.
    public static let blockingSession: URLSession = URLSession(
        configuration: .ephemeral, delegate: RecallRedirectBlocker(), delegateQueue: nil)
    /// Build the admin request.  The token goes only in the Authorization
    /// header, in memory:  never in the URL, never logged, never persisted.
    public static func buildRequest(config: RecallSettingsConfig, token: String) throws -> URLRequest {
        guard FleetSecrets.isValidName(config.tokenKeyName) else { throw RecallSettingsError.invalidTokenKeyName }
        let trimmed = config.baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme == "https", let host = url.host, !host.isEmpty,
              url.user == nil, url.password == nil, url.query == nil else {
            throw RecallSettingsError.invalidBaseURL
        }
        guard let endpoint = URL(string: "/admin/settings", relativeTo: url)?.absoluteURL else {
            throw RecallSettingsError.invalidBaseURL
        }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "GET"
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    /// Parse the admin listing, masking everything:  a raw value from the API
    /// is never displayed, whatever key it arrived under (fail closed).
    public static func parseSettings(_ data: Data) throws -> [RecallSettingRow] {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["ok"] as? Bool == true,
              let settings = object["settings"] as? [String: Any] else {
            throw RecallSettingsError.invalidResponse
        }
        var rows: [RecallSettingRow] = []
        for (name, raw) in settings {
            guard FleetSecrets.isValidName(name), let entry = raw as? [String: Any],
                  let isSet = entry["set"] as? Bool else { throw RecallSettingsError.invalidResponse }
            rows.append(RecallSettingRow(name: name, display: isSet ? "set" : "empty"))
        }
        return rows.sorted { $0.name < $1.name }
    }

    /// Fetch masked rows.  The token is read by key name into memory for this
    /// one request and released with it.
    public static func fetch(config: RecallSettingsConfig, secrets: SecretResolving,
                             session: URLSession = RecallSettingsClient.blockingSession) async throws -> [RecallSettingRow] {
        let token = try await awaitOffThePool { try secrets.secret(named: config.tokenKeyName) }
        let request = try buildRequest(config: config, token: token)
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw RecallSettingsError.invalidResponse }
        return try parseSettings(data)
    }
}
