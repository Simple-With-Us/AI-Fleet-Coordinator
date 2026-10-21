import XCTest
/// The read-only artifact panel:  what it shows,  and what it refuses to show.
final class ConfigFileReaderTests: XCTestCase {
    /// A home directory with a file,  a directory and a secret file in it.
    private func makeHome() throws -> URL {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-cfg-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent(".claude", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent(".secrets/Zulip", isDirectory: true),
            withIntermediateDirectories: true
        )
        try "the lane map\n".write(
            to: home.appendingPathComponent(".claude/CLAUDE.md"),
            atomically: true,
            encoding: .utf8
        )
        try "key".write(
            to: home.appendingPathComponent(".secrets/Zulip/Claude-zuliprc"),
            atomically: true,
            encoding: .utf8
        )
        return home
    }

    /// A tilde path resolves against the home directory.
    func test_a_tilde_path_resolves_to_the_home_directory() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let view = ConfigFileReader.read(
            target: "~/.claude/CLAUDE.md",
            checkType: "marked_block_hash",
            home: home
        )
        guard case .file(let path, let text, _) = view else {
            return XCTFail("the file should have been read")
        }
        XCTAssertEqual(path, "~/.claude/CLAUDE.md")
        XCTAssertEqual(text, "the lane map\n")
    }

    /// A directory shows the names it holds,  not its contents.
    func test_a_directory_shows_its_entries() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let view = ConfigFileReader.read(
            target: "~/.claude",
            checkType: "skills_installed",
            home: home
        )
        guard case .directory(_, let entries) = view else {
            return XCTFail("the directory should have been listed")
        }
        XCTAssertEqual(entries, ["CLAUDE.md"])
    }

    /// A path that is not on this Mac says so,  and reads nothing.
    func test_a_missing_path_says_it_is_missing() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let view = ConfigFileReader.read(
            target: "~/.claude/AGENTS.md",
            checkType: "marked_block_hash",
            home: home
        )
        guard case .missing = view else {
            return XCTFail("the path should have read as missing")
        }
    }

    /// A credential path is refused,  and the panel says why.
    ///
    /// The manifest points a `permissions` rule at the seat's own secret file,  and a
    /// fleet admin panel that renders a Zulip key on screen is a leak,  not a feature.
    func test_a_credential_path_is_refused_and_never_read() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let view = ConfigFileReader.read(
            target: "~/.secrets/Zulip/Claude-zuliprc",
            checkType: "credential_stat",
            home: home
        )
        guard case .refused(_, let reason) = view else {
            return XCTFail("the credential path should have been refused")
        }
        XCTAssertFalse(reason.isEmpty)
    }

    /// A credential check is refused even when the path looks ordinary.
    func test_a_credential_check_is_refused_whatever_the_path() {
        let reason = ConfigFileReader.refusalReason(
            target: "~/.claude/CLAUDE.md",
            checkType: "credential_key_present"
        )
        XCTAssertNotNil(reason)
    }

    /// The secret store itself is refused.
    func test_the_secret_store_is_refused() {
        XCTAssertNotNil(ConfigFileReader.refusalReason(target: "/zulip", checkType: nil))
    }
}
