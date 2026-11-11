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
    /// The command ran and refused (a non-zero exit that is not "absent").
    /// Kept apart from `unavailable`, which is the timeout/no-answer case:
    /// calling a refusal "in time" sent the owner hunting a slow read that
    /// never happened (AFC-592).
    case refused(String)
    /// The `fleet-secrets` command is not where Muster looks for it.  Kept apart from
    /// `unavailable`, so a missing command never reads as a slow one.
    case notInstalled

    public var errorDescription: String? {
        switch self {
        case .invalidName:
            return "A secret name is capital letters, digits and underscores."
        case .missing(let name):
            return "\(name) is not in Infisical.\u{00A0} Add it there:  Muster keeps no copy of it."
        case .unavailable(let name):
            return "fleet-secrets could not read \(name) in time."
        case .refused(let name):
            return "fleet-secrets stopped with an error while reading \(name), so nothing is shown.\u{00A0} That can be a refusal or no network;  the doctor below says which when it can."
        case .notInstalled:
            return "fleet-secrets is not installed on this Mac:  Muster looks for it in ~/.local/bin."
        }
    }

    /// True when a command could not start because its file is not there.
    static func isMissingCommand(_ error: Error) -> Bool {
        let ns = error as NSError
        if ns.domain == NSCocoaErrorDomain {
            return ns.code == NSFileNoSuchFileError || ns.code == NSFileReadNoSuchFileError
        }
        return ns.domain == NSPOSIXErrorDomain && ns.code == Int(ENOENT)
    }

    /// Map a key-read failure:  fleet-secrets exit 3 is "absent", any other
    /// exit is a refusal, a command that is not there is not installed, and
    /// anything else is the no-answer case.
    static func from(_ error: Error, name: String) -> FleetSecretError {
        if let exit = error as? CommandExitError {
            return exit.status == 3 ? .missing(name) : .refused(name)
        }
        if isMissingCommand(error) { return .notInstalled }
        return .unavailable(name)
    }

    /// Map a failure of a read that has no key of its own (names, doctor):
    /// an exit is a refusal, never "absent" — there is no key to be absent.
    static func fromNonKeyRead(_ error: Error, name: String) -> FleetSecretError {
        if error is CommandExitError { return .refused(name) }
        if isMissingCommand(error) { return .notInstalled }
        return .unavailable(name)
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
        } catch {
            // Exit 3 is "absent";  any other failure is not (AFC-592).
            throw FleetSecretError.from(error, name: name)
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

/// One doctor check, as the panel words it:  a plain label and a short value.
public struct SecretsDoctorRow: Equatable, Identifiable {
    public let label: String
    public let value: String
    public var id: String { label }
}

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

    /// True when a line from a `names` answer may be shown as a name.
    ///
    /// Listing is metadata only:  the line is displayed, never passed to a
    /// command, so a real Infisical key with lowercase letters (live data
    /// carries one) is listable even though `isValidName` rightly refuses it
    /// as a command argument.  The fail-closed half is unchanged:  anything
    /// value-shaped — an `=`, whitespace, a leading dash or digit — refuses
    /// the whole answer rather than being rendered as a name.
    static func isListableName(_ name: String) -> Bool {
        guard let first = name.unicodeScalars.first, name.count <= 128 else { return false }
        guard CharacterSet.letters.contains(first) || first == "_" else { return false }
        return name.range(of: "[^A-Za-z0-9_]", options: .regularExpression) == nil
    }

    /// Parse `names` answers grouped by path.  Any line that is not exactly a
    /// listable secret name refuses the whole answer (fail closed):  a value-shaped
    /// line is never rendered as a name.
    static func parseNamesByPath(_ answers: [(path: String, text: String)]) throws -> [SecretNameGroup] {
        try answers.map { answer in
            guard isValidPath(answer.path) else { throw FleetSecretError.invalidName(answer.path) }
            let lines = answer.text.split(whereSeparator: \.isNewline).map(String.init)
            for line in lines where !isListableName(line) { throw FleetSecretError.invalidName(line) }
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
        } catch { throw FleetSecretError.fromNonKeyRead(error, name: path) }
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
        } catch { throw FleetSecretError.fromNonKeyRead(error, name: path) }
    }

    /// The doctor keys the panel shows, with their plain labels, in the order shown.
    ///
    /// An allowlist, not a word filter:  a key the doctor adds later is dropped until
    /// it is named here, so nothing new can reach the screen by accident.
    static let doctorLabels: [(key: String, label: String)] = [
        ("ok", "Overall"),
        ("environment_ok", "Environment Is Prod"),
        ("cli_present", "Infisical CLI Installed"),
        ("cli_version", "Infisical CLI Version"),
        ("project_id_resolvable", "Project Found"),
        ("auth_mode", "Sign-In Mode"),
        ("reader_configured", "Reader Token Present"),
        ("writer_configured", "Writer Token Present"),
        ("fetch_names_ok", "Names Read"),
        ("names_count", "Names Found"),
        ("unresolved_reference_count", "Unresolved References"),
        ("failure_reason", "Why The Read Failed"),
    ]

    /// The label for a doctor key, including the per-project pair the doctor adds for each
    /// extra project (`<project>_project_id_resolvable`, `<project>_fetch_ok`), or nil when
    /// the key is not one the panel shows.
    static func doctorLabel(_ key: String) -> String? {
        if let known = doctorLabels.first(where: { $0.key == key }) { return known.label }
        for (suffix, words) in [("_project_id_resolvable", "Project Found"), ("_fetch_ok", "Read")] {
            guard key.hasSuffix(suffix) else { continue }
            let slug = String(key.dropLast(suffix.count))
            guard !slug.isEmpty, slug.range(of: "[^a-z_]", options: .regularExpression) == nil else { return nil }
            let name = slug.split(separator: "_").map { $0.prefix(1).uppercased() + $0.dropFirst() }
                .joined(separator: " ")
            return "\(name) \(words)"
        }
        return nil
    }

    /// Parse `doctor --json`:  only the keys `doctorLabel` names survive, as booleans,
    /// counts and short words.  Anything else is dropped.
    static func parseDoctor(_ text: String) throws -> [String: String] {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw FleetSecretError.unavailable("doctor")
        }
        var out: [String: String] = [:]
        for (key, value) in object {
            guard doctorLabel(key) != nil else { continue }
            if let number = value as? NSNumber {
                // JSON true and false arrive as NSNumber too, and a count of 0 or 1 would
                // read as a boolean if it were asked first, so the type is checked.
                if CFGetTypeID(number) == CFBooleanGetTypeID() {
                    out[key] = number.boolValue ? "true" : "false"
                } else if key.hasSuffix("_count") {
                    out[key] = number.stringValue
                }
                continue
            }
            guard let string = value as? String else { continue }
            let allowed = key == "failure_reason" ? "[^A-Za-z0-9 ._,'()/-]" : "[^A-Za-z0-9._-]"
            let limit = key == "failure_reason" ? 60 : 40
            if !string.isEmpty, string.count <= limit,
               string.range(of: allowed, options: .regularExpression) == nil {
                out[key] = string
            }
        }
        return out
    }

    /// The doctor rows, as the panel words them, in the order shown.
    static func doctorRows(_ report: [String: String]) -> [SecretsDoctorRow] {
        let extra = report.keys.filter { key in !doctorLabels.contains { $0.key == key } }.sorted()
        let order = doctorLabels.map { $0.key } + extra
        return order.compactMap { key -> SecretsDoctorRow? in
            guard let value = report[key], let label = doctorLabel(key) else { return nil }
            let shown: String
            switch (key, value) {
            case ("ok", "true"): shown = "Working"
            case ("ok", "false"): shown = "Problem Found"
            case (_, "true"): shown = "Yes"
            case (_, "false"): shown = "No"
            default: shown = value
            }
            return SecretsDoctorRow(label: label, value: shown)
        }
    }

    /// The doctor report as display rows (key to rendered boolean/count/token).
    ///
    /// The doctor exits 1 when it finds a problem and still prints its report, so a failed
    /// exit with a readable report is that report:  the panel shows what is wrong rather
    /// than hiding the doctor exactly when it matters.
    func doctorReport() throws -> [String: String] {
        let text: String
        do {
            text = try commands.run(secretsExecutable, ["doctor", "--json"],
                                    environment: secretsEnvironment, timeout: 20)
        } catch let exit as CommandExitError {
            if let report = try? Self.parseDoctor(exit.output), !report.isEmpty { return report }
            throw FleetSecretError.fromNonKeyRead(exit, name: "doctor")
        } catch { throw FleetSecretError.fromNonKeyRead(error, name: "doctor") }
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
    public static let blockingSession: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration, delegate: RecallRedirectBlocker(), delegateQueue: nil)
    }()
    /// The admin endpoint for a config, or the reason the config cannot be used.
    ///
    /// It is checked before any token is read, so a blank or wrong address says so and
    /// never costs a secret read first.
    public static func endpoint(for config: RecallSettingsConfig) throws -> URL {
        guard FleetSecrets.isValidName(config.tokenKeyName) else { throw RecallSettingsError.invalidTokenKeyName }
        let trimmed = config.baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme == "https", let host = url.host, !host.isEmpty,
              url.user == nil, url.password == nil, url.query == nil else {
            throw RecallSettingsError.invalidBaseURL
        }
        guard let endpoint = URL(string: "/admin/settings", relativeTo: url)?.absoluteURL else {
            throw RecallSettingsError.invalidBaseURL
        }
        return endpoint
    }

    /// Build the admin request.  The token goes only in the Authorization
    /// header, in memory:  never in the URL, never logged, never persisted.
    public static func buildRequest(config: RecallSettingsConfig, token: String) throws -> URLRequest {
        var request = URLRequest(url: try endpoint(for: config))
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
        _ = try endpoint(for: config)
        let token = try await awaitOffThePool { try secrets.secret(named: config.tokenKeyName) }
        let request = try buildRequest(config: config, token: token)
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw RecallSettingsError.invalidResponse }
        return try parseSettings(data)
    }
}
