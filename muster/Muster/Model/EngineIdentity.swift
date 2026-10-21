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
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        if process.isRunning {
            process.terminate()
            return Answer(
                identity: nil,
                reason: "the engine did not answer in \(Int(timeout)) seconds"
            )
        }
        let text = String(
            data: output.fileHandleForReading.readDataToEndOfFile(),
            encoding: .utf8
        ) ?? ""
        let parts = text.split(separator: " ").map(String.init)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else {
            let complaint = String(
                data: errors.fileHandleForReading.readDataToEndOfFile(),
                encoding: .utf8
            ) ?? ""
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
}
