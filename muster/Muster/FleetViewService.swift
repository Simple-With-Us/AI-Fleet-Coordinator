import Foundation

/// Runs `fleet-view status --json` as a subprocess and decodes the envelope.
///
/// GUI apps do not inherit the shell's `PATH`, so the interpreter and the
/// scripts directory are absolute and the environment is explicit (the
/// AFC-389 rule).  The child runs off the main thread.  Any failure — a
/// missing CLI, a non-zero exit, unparseable output, a schema the app does
/// not know — throws, and the view shows UNKNOWN.  It never serves the last
/// green report as if it were fresh.
enum FleetViewService {
    enum ServiceError: Error {
        case scriptsNotFound
        case cliFailed(String)
        case unparseable
        case unknownSchema(Int)
    }

    /// Candidate scripts directories, first hit wins.  `MUSTER_SCRIPTS_DIR`
    /// overrides for a lane;  the integration tree is the merged default.
    static func scriptsDirectory() -> URL? {
        let fileManager = FileManager.default
        var candidates: [String] = []
        if let override = ProcessInfo.processInfo.environment["MUSTER_SCRIPTS_DIR"] {
            candidates.append(override)
        }
        let home = fileManager.homeDirectoryForCurrentUser.path
        candidates.append("\(home)/Code/AI-Fleet-Coordinator/scripts")
        for candidate in candidates {
            let marker = "\(candidate)/fleet_view/__main__.py"
            if fileManager.fileExists(atPath: marker) {
                return URL(fileURLWithPath: candidate)
            }
        }
        return nil
    }

    static func load() async throws -> FleetViewEnvelope {
        guard let scripts = scriptsDirectory() else {
            throw ServiceError.scriptsNotFound
        }
        return try await Task.detached(priority: .userInitiated) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-m", "fleet_view", "status", "--json"]
            process.currentDirectoryURL = scripts
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            process.environment = [
                "HOME": home,
                "PATH": "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            ]
            let stdout = Pipe()
            let stderr = Pipe()
            process.standardOutput = stdout
            process.standardError = stderr
            try process.run()
            let data = stdout.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 || process.terminationStatus == 3 else {
                let text = String(data: stderr.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                throw ServiceError.cliFailed(text.isEmpty ? "exit \(process.terminationStatus)" : text)
            }
            guard let envelope = try? JSONDecoder().decode(FleetViewEnvelope.self, from: data) else {
                throw ServiceError.unparseable
            }
            guard envelope.schemaVersion == 1 else {
                throw ServiceError.unknownSchema(envelope.schemaVersion)
            }
            return envelope
        }.value
    }
}
