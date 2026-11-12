import Foundation

/// The Infisical project and environment the engine's own copy belongs to.
///
/// The app does not carry the project id itself.  The engine owns that value,  and a
/// private identifier does not belong in the app's source,  so the app asks the engine
/// and the engine answers.  The answer is a constant in the engine's own module,  so
/// it needs no credential and no network.
enum EngineIdentity {
    /// The project and environment the engine's copies carry.
    struct Identity: Equatable {
        let project: String
        let environment: String
    }

    /// The engine's answer,  or why it could not answer.
    struct Answer {
        /// The identity,  when the engine answered.
        let identity: Identity?
        /// Why the engine could not answer,  when it could not.
        let reason: String?
    }

    /// The line the engine's module prints.
    ///
    /// It names the engine's own constants,  so the app never has to know them.
    private static let question = """
    from fleet_knobs.source import PROJECT_ID, ENVIRONMENT; print(PROJECT_ID, ENVIRONMENT)
    """

    /// Ask the engine which project its copy belongs to.
    ///
    /// A failure answers with no identity,  and the caller treats a copy whose project
    /// cannot be confirmed as not this fleet's copy.  The copy is then not read at all,
    /// which is the safe direction:  the alternative is showing the values of a
    /// project the app cannot name.
    static func read(
        location: EngineLocation,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        timeout: TimeInterval = 30
    ) -> Answer {
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        process.executableURL = location.interpreter
        process.arguments = ["-s", "-c", question]
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = errors
        process.environment = EngineRunner.childEnvironment(location: location, home: home)
        do {
            try process.run()
        } catch {
            return Answer(
                identity: nil,
                reason: "the engine could not be asked:  \(error.localizedDescription)"
            )
        }
        // Both pipes are drained while the child runs.  A child that writes more
        // than a pipe buffer holds would otherwise block in the write and never
        // exit,  and the reader would report a timeout for an engine that had
        // already answered.
        let collected = CollectedOutput()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global().async {
            collected.output = output.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }
        group.enter()
        DispatchQueue.global().async {
            collected.errors = errors.fileHandleForReading.readDataToEndOfFile()
            group.leave()
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        if process.isRunning {
            process.terminate()
            // Reap the child,  so it is not left as a zombie.
            process.waitUntilExit()
        }
        group.wait()
        let text = String(data: collected.output, encoding: .utf8) ?? ""
        // The engine prints one line,  and a line ends with a newline.  The
        // newline is trimmed here,  because the copy's own environment field
        // carries no newline,  and a comparison of the two would never match.
        let parts = text.split(separator: " ")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else {
            let complaint = String(data: collected.errors, encoding: .utf8) ?? ""
            let first = complaint.split(separator: "\n").first.map(String.init) ?? ""
            let because = first.isEmpty ? "" : ":  \(first)"
            return Answer(
                identity: nil,
                reason: "the engine did not say which project its copy belongs to\(because)"
            )
        }
        return Answer(
            identity: Identity(project: parts[0], environment: parts[1]),
            reason: nil
        )
    }

    /// What the child's two pipes carried.
    private final class CollectedOutput: @unchecked Sendable {
        var output = Data()
        var errors = Data()
    }
}
