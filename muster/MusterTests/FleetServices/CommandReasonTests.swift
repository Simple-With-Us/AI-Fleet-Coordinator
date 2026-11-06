import XCTest

/// A launchctl failure names the agent and keeps the reason launchctl gave.
final class CommandReasonTests: XCTestCase {
    /// Answers launchctl,  and fails the one verb it is told to with the error it is given.
    /// `print` always fails,  which is how launchctl says an agent is not loaded.
    private final class Launchctl: CommandRunning {
        var calls: [[String]] = []
        var failure: Error?
        var failOn: String?

        func run(_ executable: String, _ arguments: [String]) throws -> String {
            calls.append(arguments)
            if arguments.first == "print" { throw CommandExitError(output: "", status: 113) }
            if let failure, failOn == nil || failOn == arguments.first { throw failure }
            return ""
        }
    }

    private func home() throws -> URL {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: home.appendingPathComponent("Library/LaunchAgents"),
                                                withIntermediateDirectories: true)
        for label in ["app.botfleet.companion", "app.botfleet.server"] {
            try Data().write(to: home.appendingPathComponent("Library/LaunchAgents/\(label).plist"))
        }
        return home
    }

    private func policy(_ commands: Launchctl, home: URL) -> StartupPolicy {
        StartupPolicy(home: home, commands: commands, uid: 501, environment: [:])
    }

    private func message(_ error: Error) -> String {
        guard case StartupPolicyError.refused(let text) = error else { return "not a refusal: \(error)" }
        return text
    }

    func testAStartThatLaunchctlRefusedNamesTheAgentAndKeepsTheReason() throws {
        let home = try home()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = Launchctl()
        commands.failOn = "kickstart"
        commands.failure = CommandExitError(output: "", status: 5,
                                            errorOutput: "Could not find service \"app.botfleet.companion\" in domain for port")
        XCTAssertThrowsError(try policy(commands, home: home).startAgent(label: "app.botfleet.companion")) { error in
            let text = message(error)
            XCTAssertTrue(text.contains("app.botfleet.companion"), text)
            XCTAssertTrue(text.contains("on the Mac"), text)
            XCTAssertTrue(text.contains("Could not find service"), text)
            XCTAssertFalse(text.contains("BotFleet Server"), text)
        }
    }

    func testATimeoutNamesLaunchctlAndTheAgentItWasChanging() throws {
        let home = try home()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = Launchctl()
        commands.failOn = "bootout"
        commands.failure = CommandTimedOutError(executable: "/bin/launchctl", arguments: ["bootout", "gui/501/app.botfleet.companion"])
        XCTAssertThrowsError(try policy(commands, home: home).stopAgent(label: "app.botfleet.companion")) { error in
            let text = message(error)
            XCTAssertEqual(text, "launchctl did not finish in time, so macOS did not stop the app.botfleet.companion LaunchAgent on the Mac.")
        }
    }

    func testAStartThatFailedWithNothingToSayStillNamesTheAgent() throws {
        let home = try home()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = Launchctl()
        commands.failOn = "enable"
        commands.failure = CommandExitError(output: "", status: 1)
        XCTAssertThrowsError(try policy(commands, home: home).startAgent(label: "app.botfleet.companion")) { error in
            XCTAssertEqual(message(error), "macOS could not start the app.botfleet.companion LaunchAgent on the Mac.")
        }
    }

    func testARestartNamesTheAgentToo() throws {
        let home = try home()
        defer { try? FileManager.default.removeItem(at: home) }
        let commands = Launchctl()
        // `print` fails in the fake,  so a restart is a start of an agent that is not loaded.
        commands.failOn = "kickstart"
        commands.failure = CommandExitError(output: "", status: 1, errorOutput: "Operation not permitted")
        XCTAssertThrowsError(try policy(commands, home: home).restartAgent(label: "app.botfleet.companion")) { error in
            let text = message(error)
            XCTAssertTrue(text.contains("app.botfleet.companion"), text)
            XCTAssertTrue(text.contains("Operation not permitted"), text)
        }
    }

    func testTheGenericCommandErrorNamesNoService() {
        XCTAssertEqual(StartupPolicyError.commandFailed.errorDescription, "A system command did not work.")
        XCTAssertFalse((StartupPolicyError.commandFailed.errorDescription ?? "").contains("BotFleet Server"))
    }
}
