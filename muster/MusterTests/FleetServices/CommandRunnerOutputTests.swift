import XCTest

/// A stand-in for a command that warned and still answered.
private final class WarnedCommands: CommandRunning {
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        throw CommandExitError(output: "p101\nfcwd\nn/tmp/fixture-home/Code/BotFleet")
    }

    func run(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> String {
        try run(executable, arguments)
    }

    func run(_ executable: String, _ arguments: [String], environment: [String: String], timeout: TimeInterval) throws -> String {
        try run(executable, arguments)
    }
}

final class CommandRunnerOutputTests: XCTestCase {
    func testTheDefaultKeepsTheOutputOfACommandThatWarned() {
        // A command that warns and still answers is not a command that
        // failed, and the answer it printed is in the failure itself.  The
        // default has to keep it, or the same probe comes back empty from
        // one conformer and full from another.
        let commands = WarnedCommands()
        XCTAssertEqual(commands.runKeepingOutput("/usr/sbin/lsof", ["-a"]),
                       "p101\nfcwd\nn/tmp/fixture-home/Code/BotFleet")
    }
}
