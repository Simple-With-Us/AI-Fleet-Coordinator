import Foundation

/// The state of the machine that runs a BotFleet server.
public enum RemoteVMState: Equatable, Sendable {
    case running
    case off
    case unknown

    public var label: String {
        switch self {
        case .running: return "Running 🟢"
        case .off: return "Off ⚪"
        case .unknown: return "Unknown"
        }
    }

    /// The badge the menu shows next to the state.
    public var badge: String {
        switch self {
        case .running: return "🟢"
        case .off: return "⚪"
        case .unknown: return "⚫"
        }
    }
}

/// Why a remote machine could not be read or changed.
public enum VMProviderError: LocalizedError, Equatable {
    case notConfigured
    case refused(String)
    case invalidResponse

    public var errorDescription: String? {
        switch self {
        case .notConfigured: return "No remote VM provider is configured for this server."
        case .refused(let message): return message
        case .invalidResponse: return "The provider returned an answer BotFleet Control could not read."
        }
    }
}

/// A plugin that can power a remote machine on and off.
///
/// The Control app does not know how a machine is hosted, so the owner picks a
/// provider and the app asks that provider.  A new provider is a new type
/// that implements this protocol; nothing else in the app has to change.
public protocol VMProviderPlugin: AnyObject {
    /// A stable identifier, used to remember the configuration.
    var providerID: String { get }
    /// The name the menu shows.
    var displayName: String { get }
    /// The state of the machine right now.
    func remoteVMState() async throws -> RemoteVMState
    /// Powers the machine on.
    func startRemoteVM() async throws
    /// Powers the machine off.
    func stopRemoteVM() async throws
    /// Asks the machine to shut down cleanly.
    func shutdownRemoteVM() async throws
}

/// The default provider: the BotFleet server's own control API.
///
/// The server knows about the virtual machine it runs on, so it can start and
/// stop that machine without the app needing a cloud account.
public final class BotFleetServerVMProvider: VMProviderPlugin {
    public let providerID = "botfleet-server"
    public let displayName = "BotFleet Server VM"
    private let api: ControlAPI

    public init(api: ControlAPI) {
        self.api = api
    }

    public func remoteVMState() async throws -> RemoteVMState {
        let status = try await api.status()
        if status.isSharedVMRunning { return .running }
        if status.canStartSharedVM { return .off }
        return .unknown
    }

    public func startRemoteVM() async throws { try await api.startVM() }

    public func stopRemoteVM() async throws { try await api.stopVMs() }

    public func shutdownRemoteVM() async throws { try await api.stopVMs() }
}

/// The provider for a Hetzner Cloud server.
///
/// The API token is the caller's; the app never keeps one of its own.  A
/// configured provider names the Infisical secret that holds it, and the
/// value is read for each request.
public final class HetznerCloudVMProvider: VMProviderPlugin {
    /// The provider's API endpoint.
    ///
    /// It is read at runtime rather than forced, because a parsing failure
    /// here would otherwise take the menu down with it.
    public static var apiBase: URL? { URL(string: "https://api.hetzner.cloud/v1") }

    /// The most an answer may weigh before it is parsed.
    ///
    /// A server or an action answer is a few kilobytes.  The cap bounds the
    /// bytes, and so the nodes, that an answer from outside can make the
    /// parser hold;  `JSONSerialization` itself refuses nesting deeper than
    /// its own fixed limit.
    public static let maxAnswerBytes = 256 * 1024

    public let providerID = "hetzner-cloud"
    public let displayName = "Hetzner Cloud VM"
    private let serverID: String
    private let token: () throws -> String
    private let session: URLSession

    public init(serverID: String, token: String, session: URLSession = .shared) {
        self.serverID = serverID
        self.token = { token }
        self.session = session
    }

    /// A provider whose token is read from Infisical for each request.
    public init(serverID: String, tokenSecret: String,
                secrets: SecretResolving = FleetSecrets(), session: URLSession = .shared) {
        self.serverID = serverID
        self.token = { try secrets.secret(named: tokenSecret) }
        self.session = session
    }

    public func remoteVMState() async throws -> RemoteVMState {
        let data = try await send(path: "servers/\(try validatedServerID())", method: "GET", timeout: 15)
        guard let payload = Self.boundedObject(data),
              let server = payload["server"] as? [String: Any],
              let status = server["status"] as? String else {
            throw VMProviderError.invalidResponse
        }
        switch status {
        case "running": return .running
        case "off": return .off
        default: return .unknown
        }
    }

    public func startRemoteVM() async throws {
        try await action("poweron")
    }

    public func stopRemoteVM() async throws {
        try await action("poweroff")
    }

    public func shutdownRemoteVM() async throws {
        try await action("shutdown")
    }

    private func action(_ name: String) async throws {
        _ = try await send(path: "servers/\(try validatedServerID())/actions/\(name)", method: "POST", timeout: 30)
    }

    /// The server's identifier, checked before it reaches the URL.
    ///
    /// A Hetzner server id is a number.  A stored value with a `/`, a `?` or a
    /// `..` in it would change the path or the query that the bearer token is
    /// sent with, so anything that is not a number is refused.
    private func validatedServerID() throws -> String {
        guard !serverID.isEmpty,
              serverID.range(of: "[^0-9]", options: .regularExpression) == nil else {
            throw VMProviderError.refused("The Hetzner server ID is not a number.")
        }
        return serverID
    }

    /// Parses an answer as a JSON object, within the byte cap.
    static func boundedObject(_ data: Data) -> [String: Any]? {
        guard data.count <= maxAnswerBytes else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private func send(path: String, method: String, timeout: TimeInterval) async throws -> Data {
        guard let base = Self.apiBase else {
            throw VMProviderError.refused("The Hetzner Cloud endpoint could not be read.")
        }
        let bearer: String
        do { bearer = try token() }
        catch { throw VMProviderError.refused(error.localizedDescription) }
        let url = base.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        if method == "POST" { request.httpBody = Data("{}".utf8) }
        let data: Data
        let response: URLResponse
        // Redirects are refused, so the bearer token never leaves the
        // Hetzner Cloud endpoint;  a 3xx answer is a refusal below.
        do { (data, response) = try await session.data(for: request, delegate: NoRedirects()) }
        catch {
            // A read that failed changed nothing.  A power action that failed
            // may have reached Hetzner before the connection dropped, so the
            // owner is told to read the state rather than to try again.
            throw VMProviderError.refused(method == "GET"
                ? "Hetzner Cloud did not answer."
                : "Hetzner Cloud did not answer, so the action may or may not have been made.  Read the machine's state before trying again.")
        }
        guard let http = response as? HTTPURLResponse else { throw VMProviderError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            let message = Self.boundedObject(data)?["error"] as? [String: Any]
            throw VMProviderError.refused((message?["message"] as? String).map { String($0.prefix(300)) }
                ?? "Hetzner Cloud refused the request (HTTP \(http.statusCode)).")
        }
        guard data.count <= Self.maxAnswerBytes else { throw VMProviderError.invalidResponse }
        return data
    }
}

/// The provider for a machine the owner drives with their own script.
///
/// The script is called with one argument: `status`, `start`, `stop` or
/// `shutdown`.  A status call succeeds when the script exits 0 and prints
/// `running` or `off`.
public final class CustomScriptVMProvider: VMProviderPlugin {
    public let providerID = "custom-script"
    public let displayName = "Custom Script VM"
    private let command: String
    private let arguments: [String]
    private let commands: CommandRunning

    public init(command: String, arguments: [String] = [], commands: CommandRunning = ProcessCommandRunner()) {
        self.command = command
        self.arguments = arguments
        self.commands = commands
    }

    public func remoteVMState() async throws -> RemoteVMState {
        let output = (try? commands.run(command, arguments + ["status"])) ?? ""
        // The script's answer is compared exactly, because a substring match
        // would read "not running" as running.
        let answer = output.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if answer == "running" { return .running }
        if answer == "off" || answer == "stopped" { return .off }
        return .unknown
    }

    public func startRemoteVM() async throws { try run("start") }

    public func stopRemoteVM() async throws { try run("stop") }

    public func shutdownRemoteVM() async throws { try run("shutdown") }

    private func run(_ action: String) throws {
        do {
            _ = try commands.run(command, arguments + [action], timeout: 60)
        } catch let error as CommandExitError {
            throw VMProviderError.refused(error.output.isEmpty
                ? "The script refused to \(action) the machine."
                : error.output)
        }
    }
}

/// How one server's remote machine is driven.
public struct VMProviderConfiguration: Codable, Equatable, Identifiable, Sendable {
    /// The server this provider belongs to.
    public let id: String
    /// Which provider to use: `botfleet-server`, `hetzner-cloud` or
    /// `custom-script`.
    public var providerID: String
    /// The name the menu shows.
    public var displayName: String
    /// The machine's identifier at the provider: a Hetzner server id or a
    /// BotFleet VM key.
    public var remoteVMID: String?
    /// The name of the Infisical secret that holds the provider's API token,
    /// when it needs one.  Only the name is kept;  the value is read through
    /// `fleet-secrets` when a request needs it.
    public var apiTokenSecret: String?
    /// The script to run, for the custom provider.
    public var command: String?
    /// The arguments to pass to the script, before the action.
    public var arguments: [String]

    public init(id: String, providerID: String, displayName: String,
                remoteVMID: String? = nil, apiTokenSecret: String? = nil,
                command: String? = nil, arguments: [String] = []) {
        self.id = id
        self.providerID = providerID
        self.displayName = displayName
        self.remoteVMID = remoteVMID
        self.apiTokenSecret = apiTokenSecret
        self.command = command
        self.arguments = arguments
    }

    /// The provider kinds the app offers.
    public static let providerIDs = ["botfleet-server", "hetzner-cloud", "custom-script"]

    /// The name a provider kind shows in the menu.
    public static func displayName(for providerID: String) -> String {
        switch providerID {
        case "botfleet-server": return "BotFleet Server VM"
        case "hetzner-cloud": return "Hetzner Cloud VM"
        case "custom-script": return "Custom Script VM"
        default: return providerID
        }
    }
}

/// Keeps the provider configuration on disk.
///
/// The file names secrets but holds none.  It is still written with
/// owner-only permissions, and every owner-entered field is scanned before it
/// is saved, so a token pasted into the wrong field is refused rather than
/// written.
public final class VMProviderStore {
    private let fileURL: URL
    private let lock = NSLock()

    public init(fileURL: URL? = nil) {
        if let fileURL {
            self.fileURL = fileURL
        } else {
            let home = FileManager.default.homeDirectoryForCurrentUser
            self.fileURL = home.appendingPathComponent(".botfleet/control-vm-plugins.json")
        }
    }

    /// The file the configuration lives in.
    public var configurationURL: URL { fileURL }

    /// Reads every configuration, or an empty list when none was ever saved.
    ///
    /// A file that exists and cannot be read is a refusal that names the
    /// file, not an empty list:  an empty list would hide the failure, and a
    /// save on top of it would erase every configuration.
    public func loadConfigurations() throws -> [VMProviderConfiguration] {
        lock.lock()
        defer { lock.unlock() }
        return try loadUnlocked()
    }

    /// The configuration for one server, when it has one.
    public func configuration(for serverID: String) throws -> VMProviderConfiguration? {
        try loadConfigurations().first { $0.id == serverID }
    }

    /// Saves the configuration list atomically with owner-only permissions.
    public func saveConfigurations(_ configs: [VMProviderConfiguration]) throws {
        lock.lock()
        defer { lock.unlock() }
        try saveUnlocked(configs)
    }

    /// Adds or replaces one server's configuration.
    ///
    /// The read, the merge and the write happen under one hold of the lock,
    /// so two saves at once cannot lose either one.
    public func upsert(_ config: VMProviderConfiguration) throws {
        lock.lock()
        defer { lock.unlock() }
        var configs = try loadUnlocked()
        configs.removeAll { $0.id == config.id }
        configs.append(config)
        try saveUnlocked(configs)
    }

    /// Removes one server's configuration.
    @discardableResult
    public func remove(id: String) throws -> Bool {
        lock.lock()
        defer { lock.unlock() }
        var configs = try loadUnlocked()
        let before = configs.count
        configs.removeAll { $0.id == id }
        guard configs.count != before else { return false }
        try saveUnlocked(configs)
        return true
    }

    /// The field, if any, that looks like it holds a credential.
    ///
    /// Owner-entered text is checked before it is written:  a known token
    /// prefix, a bearer header or a long unbroken run of key characters is a
    /// credential pasted into the wrong place.  Only the field's name is ever
    /// reported, never its value or any part of it.
    public static func fieldHoldingACredential(_ config: VMProviderConfiguration) -> String? {
        var fields: [(String, String)] = [("display name", config.displayName)]
        if let remoteVMID = config.remoteVMID { fields.append(("machine ID", remoteVMID)) }
        if let command = config.command { fields.append(("command", command)) }
        for (index, argument) in config.arguments.enumerated() {
            fields.append(("argument \(index + 1)", argument))
        }
        return fields.first { looksLikeACredential($0.1) }?.0
    }

    static func looksLikeACredential(_ text: String) -> Bool {
        // A known token prefix counts only where a word starts, so an
        // argument such as `--disk-size` is not mistaken for an `sk-` key.
        let prefixes = "(^|[^a-z0-9])(ghp_|gho_|ghs_|github_pat_|sk-|xoxb-|xoxp-|glpat-|bearer )"
        if text.lowercased().range(of: prefixes, options: .regularExpression) != nil { return true }
        // A key is a long run of letters, digits, `_` and `-` with no path
        // separator in it;  a path or a sentence breaks the run.
        return text.range(of: "[A-Za-z0-9_\\-]{40,}", options: .regularExpression) != nil
    }

    private func loadUnlocked() throws -> [VMProviderConfiguration] {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return [] }
        guard let data = try? Data(contentsOf: fileURL),
              let configs = try? JSONDecoder().decode([VMProviderConfiguration].self, from: data) else {
            throw VMProviderError.refused("The provider configuration at \(fileURL.path) could not be read.")
        }
        return configs
    }

    private func saveUnlocked(_ configs: [VMProviderConfiguration]) throws {
        for config in configs {
            if let field = Self.fieldHoldingACredential(config) {
                throw VMProviderError.refused("The \(field) looks like it holds a credential, so it was not saved.  Keep tokens in Infisical and name the secret instead.")
            }
        }
        let dir = fileURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(configs)
        let tempURL = dir.appendingPathComponent("control-vm-plugins.\(UUID().uuidString).tmp")
        defer { try? FileManager.default.removeItem(at: tempURL) }
        // The file holds an API token, so it is created with owner-only
        // permissions rather than written wide and tightened afterwards.
        guard FileManager.default.createFile(atPath: tempURL.path, contents: nil,
                                             attributes: [.posixPermissions: 0o600]) else {
            throw VMProviderError.refused("The provider configuration could not be written.")
        }
        try data.write(to: tempURL)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tempURL.path)
        if FileManager.default.fileExists(atPath: fileURL.path) {
            _ = try FileManager.default.replaceItemAt(fileURL, withItemAt: tempURL)
        } else {
            try FileManager.default.moveItem(at: tempURL, to: fileURL)
        }
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
    }
}

/// Builds the plugin a configuration asks for.
public enum VMProviderFactory {
    /// The plugin for one configuration, or nil when it cannot be built.
    ///
    /// A configuration whose server has been removed builds nothing.  Falling
    /// back to the local server would mean that "Stop Remote VM" on a server
    /// that is gone would stop the local machine's virtual machines instead.
    public static func makeProvider(for config: VMProviderConfiguration,
                                    hosts: [ServerHost],
                                    secrets: SecretResolving = FleetSecrets(),
                                    session: URLSession = .shared) -> VMProviderPlugin? {
        switch config.providerID {
        case "botfleet-server":
            guard let host = hosts.first(where: { $0.id == config.id }) else { return nil }
            return BotFleetServerVMProvider(api: ControlAPI(session: session, host: host, secrets: secrets))
        case "hetzner-cloud":
            guard let serverID = config.remoteVMID, !serverID.isEmpty,
                  let secret = config.apiTokenSecret, !secret.isEmpty else { return nil }
            return HetznerCloudVMProvider(serverID: serverID, tokenSecret: secret,
                                          secrets: secrets, session: session)
        case "custom-script":
            guard let command = config.command, !command.isEmpty else { return nil }
            return CustomScriptVMProvider(command: command, arguments: config.arguments)
        default:
            return nil
        }
    }
}
