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

    /// The line the engine's module prints.
    ///
    /// It names the engine's own constants,  so the app never has to know them.
    private static let question = """
    from fleet_knobs.source import PROJECT_ID, ENVIRONMENT; print(PROJECT_ID, ENVIRONMENT)
    """

    /// Ask the engine which project its copy belongs to.
    ///
    /// A failure returns nothing,  and the caller treats a copy whose project cannot
    /// be confirmed as not this fleet's copy.  The copy is then not read at all,
    /// which is the safe direction:  the alternative is showing the values of a
    /// project the app cannot name.
    static func read(
        location: EngineLocation,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        timeout: TimeInterval = 30
    ) -> Identity? {
        let process = Process()
        let output = Pipe()
        process.executableURL = location.interpreter
        process.arguments = ["-s", "-c", question]
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.environment = EngineRunner.childEnvironment(location: location, home: home)
        do {
            try process.run()
        } catch {
            return nil
        }
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        if process.isRunning {
            process.terminate()
            return nil
        }
        let text = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let parts = text.split(separator: " ").map(String.init)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }
        return Identity(project: parts[0], environment: parts[1])
    }
}
