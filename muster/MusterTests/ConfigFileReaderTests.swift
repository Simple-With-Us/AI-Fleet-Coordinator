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
        guard case .file(let path, let text, _, _) = view else {
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

    /// A secret value in a config file is hidden,  and the setting beside it is not.
    ///
    /// The MCP config holds the server names the operator came for and,  in the same
    /// file,  the token that authenticates one of them.  The panel shows the first and
    /// hides the second.
    func test_a_secret_value_in_a_config_file_is_redacted() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent(".minimax", isDirectory: true),
            withIntermediateDirectories: true
        )
        try """
        {
         "mcpServers": {
          "fleet-recall": {"command": "python3", "args": ["-m", "fleet_rag"]},
          "sentry": {"command": "npx", "env": {"SENTRY_ACCESS_TOKEN": "sntrys_SECRET_VALUE"}}
         },
         "apiKey": "sk-LIVE_SECRET_VALUE"
        }
        """.write(
            to: home.appendingPathComponent(".minimax/mcp.json"),
            atomically: true,
            encoding: .utf8
        )
        let view = ConfigFileReader.read(
            target: "~/.minimax/mcp.json",
            checkType: "mcp_servers_registered",
            home: home
        )
        guard case .file(_, let text, _, let redacted) = view else {
            return XCTFail("the file should have been read")
        }
        XCTAssertTrue(redacted, "the file should report that it was redacted")
        XCTAssertFalse(text.contains("sntrys_SECRET_VALUE"), "the token must not survive")
        XCTAssertFalse(text.contains("sk-LIVE_SECRET_VALUE"), "the api key must not survive")
        // The information the file is for is still there.
        XCTAssertTrue(text.contains("fleet-recall"), "the server name must survive")
        XCTAssertTrue(text.contains("python3"), "the command must survive")
    }

    /// A key whose name ends in a secret word is a secret too.
    func test_a_key_that_ends_in_a_secret_word_is_a_secret() {
        XCTAssertTrue(ConfigFileReader.isSecretKey("anthropicApiKey"))
        XCTAssertTrue(ConfigFileReader.isSecretKey("SENTRY_ACCESS_TOKEN"))
        XCTAssertFalse(ConfigFileReader.isSecretKey("command"))
        XCTAssertFalse(ConfigFileReader.isSecretKey("args"))
        XCTAssertFalse(ConfigFileReader.isSecretKey("fleet-recall"))
    }

    /// A TOML secret is hidden,  and the rest of the file is left alone.
    func test_a_toml_secret_is_redacted_and_the_rest_is_untouched() {
        let text = """
        model = "gpt-5"
        api_key = "sk-LIVE_SECRET_VALUE"
        base_url = "https://example.test"
        """
        let out = ConfigFileReader.redact(text)
        XCTAssertFalse(out.contains("sk-LIVE_SECRET_VALUE"))
        XCTAssertTrue(out.contains("gpt-5"))
        XCTAssertTrue(out.contains("https://example.test"))
    }

    /// A secret under a name nobody thought of is still hidden.
    ///
    /// The key name is the first defence and not the whole of it,  so a long opaque
    /// value is hidden whatever it is called.
    func test_an_opaque_value_is_hidden_whatever_its_key_is_called() {
        let text = """
        {"mcpServers": {"sentry": {"command": "npx",
          "env": {"SOME_UNEXPECTED_NAME": "abcdef0123456789ABCDEF0123456789"}}}}
        """
        let out = ConfigFileReader.redact(text)
        XCTAssertFalse(out.contains("abcdef0123456789ABCDEF0123456789"))
        XCTAssertTrue(out.contains("npx"))
    }

    /// A URL that carries a password is a secret,  and a plain URL is not.
    func test_a_url_with_a_password_is_hidden_and_a_plain_url_is_not() {
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("https://user:hunter2@example.test"))
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("https://example.test/path"))
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("short"))
    }

    /// A long path is not a secret,  even though it is long.
    func test_a_long_path_is_not_a_secret() {
        XCTAssertFalse(
            ConfigFileReader.looksLikeSecretValue("/Users/jay/apps/lane-tools/fleet-guard-hook")
        )
    }
}
