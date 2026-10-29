import Foundation

/// A configured BotFleet Server host that BotFleet Control can inspect and command.
public struct ServerHost: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public var name: String
    public var baseURL: URL
    /// The name of the Infisical secret that holds this server's bearer
    /// token, such as `BOTFLEET_GATE_TOKEN`.
    ///
    /// Only the name is kept:  the value is read through `fleet-secrets` when
    /// a request needs it, so no bearer credential is ever written to the
    /// hosts file.  A file written by an older build may still carry an
    /// `authToken` value;  it is ignored when read and dropped on the next
    /// save.
    public var authTokenSecret: String?
    public let isLocal: Bool

    public init(
        id: String = UUID().uuidString,
        name: String,
        baseURL: URL,
        authTokenSecret: String? = nil,
        isLocal: Bool = false
    ) {
        self.id = id
        self.name = name
        self.baseURL = baseURL
        self.authTokenSecret = authTokenSecret
        self.isLocal = isLocal
    }

    /// The default primary Mac local server on loopback port 8799.
    public static let defaultLocal = ServerHost(
        id: "local",
        name: "Local Mac",
        baseURL: URL(string: "http://127.0.0.1:8799")!,
        authTokenSecret: nil,
        isLocal: true
    )

    /// The default Hetzner cloud server endpoint.
    public static let defaultHetzner = ServerHost(
        id: "hetzner",
        name: "Hetzner Server",
        baseURL: URL(string: "https://hetzner.botfleet.app")!,
        authTokenSecret: nil,
        isLocal: false
    )
}

/// Atomic storage for known server hosts in `~/.botfleet/control-hosts.json`.
public final class ServerHostStore: @unchecked Sendable {
    private let fileURL: URL
    private let lock = NSLock()

    public init(fileURL: URL? = nil) {
        if let fileURL {
            self.fileURL = fileURL
        } else {
            let home = FileManager.default.homeDirectoryForCurrentUser
            self.fileURL = home.appendingPathComponent(".botfleet/control-hosts.json")
        }
    }

    /// Loads the saved server hosts, seeding with Local Mac and Hetzner Server if missing.
    public func loadHosts() -> [ServerHost] {
        lock.lock()
        defer { lock.unlock() }

        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            let defaults = [ServerHost.defaultLocal, ServerHost.defaultHetzner]
            saveInternal(defaults)
            return defaults
        }

        do {
            let data = try Data(contentsOf: fileURL)
            let hosts = try JSONDecoder().decode([ServerHost].self, from: data)
            // Ensure Local Mac always exists as the anchor host.
            if !hosts.contains(where: { $0.id == ServerHost.defaultLocal.id || $0.isLocal }) {
                var augmented = [ServerHost.defaultLocal]
                augmented.append(contentsOf: hosts)
                saveInternal(augmented)
                return augmented
            }
            return hosts
        } catch {
            // Preserve the unreadable file by backing it up rather than silently erasing it.
            let backupURL = fileURL.deletingPathExtension().appendingPathExtension("corrupt.\(Int(Date().timeIntervalSince1970)).json")
            try? FileManager.default.copyItem(at: fileURL, to: backupURL)
            let defaults = [ServerHost.defaultLocal, ServerHost.defaultHetzner]
            return defaults
        }
    }

    /// Saves the host list atomically with 0600 file permissions.
    ///
    /// The temporary file is created owner-only before anything is written
    /// to it, so the list is never readable by another user, not even for
    /// the moment between the write and a later chmod.
    public func saveHosts(_ hosts: [ServerHost]) throws {
        lock.lock()
        defer { lock.unlock() }
        saveInternal(hosts)
    }

    /// Appends or updates a host. Refuses to overwrite if an existing file failed decoding.
    public func upsertHost(_ host: ServerHost) {
        lock.lock()
        defer { lock.unlock() }
        guard var current = loadHostsInternal() else {
            return
        }
        if let idx = current.firstIndex(where: { $0.id == host.id }) {
            current[idx] = host
        } else {
            current.append(host)
        }
        saveInternal(current)
    }

    /// Removes a host by id (refusing to remove the local host anchor).
    @discardableResult
    public func removeHost(id: String) -> Bool {
        guard id != ServerHost.defaultLocal.id else { return false }
        lock.lock()
        defer { lock.unlock() }
        guard var current = loadHostsInternal() else {
            return false
        }
        let countBefore = current.count
        current.removeAll { $0.id == id && !$0.isLocal }
        if current.count != countBefore {
            saveInternal(current)
            return true
        }
        return false
    }

    private func loadHostsInternal() -> [ServerHost]? {
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            return [ServerHost.defaultLocal, ServerHost.defaultHetzner]
        }
        guard let data = try? Data(contentsOf: fileURL),
              let hosts = try? JSONDecoder().decode([ServerHost].self, from: data) else {
            return nil
        }
        return hosts
    }

    private func saveInternal(_ hosts: [ServerHost]) {
        let dir = fileURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(hosts) else { return }

        let tempURL = dir.appendingPathComponent("control-hosts.\(UUID().uuidString).tmp")
        do {
            guard FileManager.default.createFile(atPath: tempURL.path, contents: nil,
                                                 attributes: [.posixPermissions: 0o600]) else { return }
            // Written in place rather than with `.atomic`:  an atomic write
            // makes a fresh file with the default permissions and renames it
            // over this one.  The rename below is what makes the save atomic.
            try data.write(to: tempURL)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tempURL.path)
            if FileManager.default.fileExists(atPath: fileURL.path) {
                _ = try FileManager.default.replaceItemAt(fileURL, withItemAt: tempURL)
            } else {
                try FileManager.default.moveItem(at: tempURL, to: fileURL)
            }
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
        } catch {
            try? FileManager.default.removeItem(at: tempURL)
        }
    }
}
