import XCTest
/// The engine's own answer about which Infisical project its copy belongs to.
///
/// The answer comes from a real child process,  so the test runs a real one:  a
/// script that prints the same line the engine's module prints.  That covers the
/// whole path,  the run,  the draining of both pipes while the child runs,  and
/// the reading of the answer.
final class EngineIdentityTests: XCTestCase {
    /// A place to run a child process from.
    private func makeLocation(interpreter: URL) -> EngineLocation {
        let base = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-identity-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(
            at: base.appendingPathComponent("scripts", isDirectory: true),
            withIntermediateDirectories: true
        )
        return EngineLocation(root: base, interpreter: interpreter)
    }

    /// A script that prints what the engine's module prints.
    private func makeScript(_ body: String) throws -> URL {
        let base = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-identity-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        let script = base.appendingPathComponent("answer.sh")
        try body.write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o755],
            ofItemAtPath: script.path
        )
        return script
    }

    /// The answer is read from the child.
    func test_the_answer_is_read_from_the_engine() throws {
        let script = try makeScript("#!/bin/bash\necho 'the-project-id prod'\n")
        let answer = EngineIdentity.read(location: makeLocation(interpreter: script))
        XCTAssertEqual(answer.identity?.project, "the-project-id")
        XCTAssertEqual(answer.identity?.environment, "prod")
        XCTAssertNil(answer.reason)
    }

    /// A child that writes more than a pipe buffer holds still answers.
    ///
    /// The pipes are drained while the child runs,  so a child that fills one does
    /// not block in the write and never exit.  A reader that drained them only
    /// after the exit would report a timeout for an engine that had answered.
    /// The noise is on the error pipe,  which is where the engine's own import
    /// writes when it complains.
    func test_a_child_that_fills_a_pipe_still_answers() throws {
        let script = try makeScript(
            """
            #!/bin/bash
            echo "the-project-id prod"
            # A megabyte of noise,  far past a pipe buffer.
            for _ in $(seq 1 20000); do
              echo "noise that fills the pipe buffer and then some" >&2
            done
            """
        )
        let answer = EngineIdentity.read(location: makeLocation(interpreter: script), timeout: 60)
        XCTAssertEqual(answer.identity?.project, "the-project-id")
        XCTAssertEqual(answer.identity?.environment, "prod")
        XCTAssertNil(answer.reason)
    }

    /// A child that fails says why.
    func test_a_child_that_fails_says_why() throws {
        let script = try makeScript(
            """
            #!/bin/bash
            echo "No module named fleet_knobs" >&2
            exit 1
            """
        )
        let answer = EngineIdentity.read(location: makeLocation(interpreter: script))
        XCTAssertNil(answer.identity)
        XCTAssertNotNil(answer.reason)
        XCTAssertTrue(
            answer.reason?.contains("fleet_knobs") ?? false,
            "the reason carries what the child said: \(answer.reason ?? "")"
        )
    }

    /// An interpreter that is not there is a reason,  not a crash.
    func test_a_missing_interpreter_is_a_reason() throws {
        let missing = URL(fileURLWithPath: "/nonexistent/python3-\(UUID().uuidString)")
        let answer = EngineIdentity.read(location: makeLocation(interpreter: missing))
        XCTAssertNil(answer.identity)
        XCTAssertNotNil(answer.reason)
    }
}
