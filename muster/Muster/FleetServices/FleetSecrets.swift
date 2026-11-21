import Foundation

/// Reads one secret by its name, at the moment a request needs it.
///
/// Infisical is the fleet's only source of secrets, so no token is kept in a
/// settings file:  a server host or a VM provider names its secret, and the
/// value is read through the shared declared-consumer reader when a request needs it.
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
    /// The shared reader is not in the configured engine checkout.  Kept apart from
    /// `unavailable`, so a missing command never reads as a slow one.
    case notInstalled

    public var errorDescription: String? {
        switch self {
        case .invalidName:
            return "A secret name is capital letters, digits and underscores."
        case .missing(let name):
            return "\(name) is not in Infisical.\u{00A0} Add it there:  Muster keeps no copy of it."
        case .unavailable(let name):
            return "Infisical could not read \(name) in time."
        case .refused(let name):
            return "The Infisical reader stopped with an error while reading \(name), so nothing is shown.\u{00A0} That can be a refusal or no network;  the doctor below says which when it can."
        case .notInstalled:
            return "The shared Infisical reader is unavailable in the configured engine checkout."
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

    /// Map a key-read failure: exit 3 is "absent", exit 124 is a timeout, any other
    /// exit is a refusal, a command that is not there is not installed, and
    /// anything else is the no-answer case.
    static func from(_ error: Error, name: String) -> FleetSecretError {
        if let error = error as? FleetSecretError { return error }
        if let exit = error as? CommandExitError {
            if exit.status == 124 { return .unavailable(name) }
            return exit.status == 3 ? .missing(name) : .refused(name)
        }
        if isMissingCommand(error) { return .notInstalled }
        return .unavailable(name)
    }

    /// Map a failure of a read that has no key of its own (names, doctor):
    /// an exit is a refusal, never "absent" — there is no key to be absent.
    static func fromNonKeyRead(_ error: Error, name: String) -> FleetSecretError {
        if let error = error as? FleetSecretError { return error }
        if let exit = error as? CommandExitError {
            return exit.status == 124 ? .unavailable(name) : .refused(name)
        }
        if isMissingCommand(error) { return .notInstalled }
        return .unavailable(name)
    }
}

/// Reads through the shared reader using Muster's own protected declaration.
/// Construction never accesses a credential store; every initializer selects this consumer.
public struct FleetSecrets: SecretResolving {
    private let commands: CommandRunning
    private let home: URL
    private let location: EngineLocation?

    /// The reader has separate 30-second worker and five-second store budgets.
    /// Leave room for process startup and cleanup without shortening either budget.
    static let readerTimeout: TimeInterval = 45
    static let consumer = "muster-data-reader"

    /// `engineRoot` and `interpreter` are source-test seams; production discovery ignores
    /// engine environment overrides and always uses the system Python.
    public init(commands: CommandRunning = ProcessCommandRunner(),
                home: URL = FileManager.default.homeDirectoryForCurrentUser,
                engineRoot: URL? = nil, interpreter: URL? = nil) {
        self.commands = commands
        self.home = home
        let root = engineRoot ?? EngineLocator.locate(environment: [:], home: home)?.root
        self.location = root.map {
            EngineLocation(root: $0,
                           interpreter: interpreter ?? URL(fileURLWithPath: EngineLocator.defaultInterpreter))
        }
    }

    /// Explicit production spelling; the default initializer has the same managed contract.
    public static func managed(
        commands: CommandRunning = ProcessCommandRunner(),
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        engineRoot: URL? = nil, interpreter: URL? = nil
    ) -> FleetSecrets {
        FleetSecrets(commands: commands, home: home, engineRoot: engineRoot, interpreter: interpreter)
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
        let value: String
        do {
            value = try runReader(["get", name] + Self.scopeArguments)
        } catch {
            if isCancellation(error) { throw CancellationError() }
            throw FleetSecretError.from(error, name: name)
        }
        guard !value.isEmpty else { throw FleetSecretError.missing(name) }
        return value
    }
}

/// Runs blocking work on a dispatch queue and awaits its result.
///
/// A declared read has a bounded process timeout.  Run inline, it
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
    /// True when a path is safe metadata for a names group.
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

    /// This consumer has one exact approved purpose: Fleet-OPS prod root, read-only.
    static let scopeArguments = ["--folder", "/", "--project", "fleet-ops"]

    private var secretsEnvironment: [String: String] {
        // An allowlist removes ambient authentication, API and folder markers, including
        // empty ones.  The shared project resolver uses its existing approved metadata.
        ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home.path]
    }

    private func runReader(_ arguments: [String]) throws -> String {
        guard let location else { throw FleetSecretError.notInstalled }
        let reader = location.packageDirectory.appendingPathComponent("infisical-scoped.py").path
        let declaration = home.appendingPathComponent(".config/infisical/muster.json").path
        let selector = ["--consumer-binding", declaration, "--consumer", Self.consumer]
        let missingEntryDiagnostics = ["can't open file '\(reader)': [Errno 2]",
                                       "can't open file \"\(reader)\": [Errno 2]"]
        do {
            return try commands.run(location.interpreter.path, ["-I", reader] + arguments + selector,
                                    environment: secretsEnvironment, timeout: Self.readerTimeout)
        } catch let exit as CommandExitError where exit.status == 2
            && missingEntryDiagnostics.contains(where: { exit.errorOutput.contains($0) }) {
            // Match the exact immutable entry; the child diagnostic never reaches the UI.
            throw FleetSecretError.notInstalled
        }
    }

    /// Names at the Fleet-OPS root, through the metadata-only `names` verb.
    func secretNames(path: String) throws -> [String] {
        try Self.parseNamesByPath([(path, try secretNamesText(path: path))]).first?.names ?? []
    }

    /// Scope cannot widen through a caller-supplied folder or project.
    static func namesArguments(path: String, project: String?) throws -> [String] {
        guard path == "/" else { throw FleetSecretError.invalidName(path) }
        guard project == nil || project == "fleet-ops" else {
            throw FleetSecretError.invalidName(project ?? "")
        }
        return ["names"] + scopeArguments
    }

    func secretNamesGrouped(paths: [String] = ["/"], project: String? = nil) throws -> [SecretNameGroup] {
        // Validate every selector before starting any process.
        for path in paths { _ = try Self.namesArguments(path: path, project: project) }
        return try Self.parseNamesByPath(paths.map { ($0, try secretNamesText(path: $0, project: project)) })
    }

    private func secretNamesText(path: String, project: String? = nil) throws -> String {
        let arguments = try Self.namesArguments(path: path, project: project)
        do { return try runReader(arguments) }
        catch {
            if isCancellation(error) { throw CancellationError() }
            throw FleetSecretError.fromNonKeyRead(error, name: path)
        }
    }

    /// The doctor keys the panel shows, with their plain labels, in the order shown.
    ///
    /// An allowlist, not a word filter:  a key the doctor adds later is dropped until
    /// it is named here, so nothing new can reach the screen by accident.
    static let doctorLabels: [(key: String, label: String)] = [
        ("ok", "Overall"),
        ("environment_ok", "Environment Is Prod"),
        ("project_id_resolvable", "Project Found"),
        ("reader_configured", "Reader Token Present"),
        ("fetch_names_ok", "Names Read"),
        ("names_count", "Names Found"),
        ("failure_reason", "Why The Read Failed"),
    ]

    /// Only the selected reader's fixed metadata contract reaches this panel.
    static func doctorLabel(_ key: String) -> String? {
        doctorLabels.first(where: { $0.key == key })?.label
    }

    /// Parse only the declared reader's booleans, names count and fixed refusal reason.
    /// Unknown fields and arbitrary diagnostic strings never reach the screen.
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
                if CFGetTypeID(number) == CFBooleanGetTypeID(), key != "names_count", key != "failure_reason" {
                    out[key] = number.boolValue ? "true" : "false"
                } else if CFGetTypeID(number) != CFBooleanGetTypeID(), key == "names_count",
                          number.doubleValue.isFinite, number.doubleValue >= 0,
                          number.doubleValue <= Double(Int.max),
                          number.doubleValue.rounded() == number.doubleValue {
                    out[key] = number.stringValue
                }
                continue
            }
            if key == "failure_reason", let string = value as? String,
               string == "consumer-binding-refused" {
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
    /// A nonzero result may still carry a masked report.  A timed-out operation is always
    /// reported as unavailable, even if it printed a partial report before timing out.
    func doctorReport() throws -> [String: String] {
        let text: String
        do {
            text = try runReader(["doctor"] + Self.scopeArguments)
        } catch let exit as CommandExitError {
            if exit.status != 124,
               let report = try? Self.parseDoctor(exit.output), !report.isEmpty { return report }
            throw FleetSecretError.fromNonKeyRead(exit, name: "doctor")
        } catch {
            if isCancellation(error) { throw CancellationError() }
            throw FleetSecretError.fromNonKeyRead(error, name: "doctor")
        }
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
