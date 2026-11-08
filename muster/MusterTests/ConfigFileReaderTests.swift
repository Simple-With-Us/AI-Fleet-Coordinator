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

    /// A config file larger than the panel's limit is shown truncated, and only
    /// the first slice of it is ever in memory.
    func test_a_file_past_the_limit_reads_as_truncated() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-cfg-big-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let big = String(repeating: "a", count: ConfigFileReader.byteLimit + 4096)
        try big.write(to: home.appendingPathComponent("big.md"), atomically: true, encoding: .utf8)
        let view = ConfigFileReader.read(target: "~/big.md", checkType: "marked_block_hash", home: home)
        guard case .file(_, let text, let truncated, _) = view else {
            return XCTFail("the file should have been read")
        }
        XCTAssertTrue(truncated)
        XCTAssertEqual(text.count, ConfigFileReader.byteLimit)
    }

    // MARK: GRID-28:  secrets in lists and inline tables

    /// A string in a list is checked as a string under a key is,  and the value after a
    /// secret flag or a header flag is hidden too.
    func test_a_secret_in_a_json_list_is_redacted() {
        let opaque = "abcdef0123456789" + "ABCDEF0123456789"  // made up;  split and named so the secret scanner does not flag it
        let text = """
        {"mcpServers": {"x": {"command": "npx", "args": [
          "--header", "Authorization: Bearer abc123",
          "--token", "short1",
          "\(opaque)",
          "-m", "fleet_rag"
        ]}}}
        """
        let out = ConfigFileReader.redact(text)
        XCTAssertFalse(out.contains("abc123"), out)
        XCTAssertFalse(out.contains("short1"), out)
        XCTAssertFalse(out.contains(opaque), out)
        XCTAssertTrue(out.contains("npx"))
        XCTAssertTrue(out.contains("fleet_rag"))
        XCTAssertTrue(out.contains("--header"))
    }

    /// A TOML inline table is read pair by pair,  so a header inside it is hidden.
    func test_a_secret_in_a_toml_inline_table_is_redacted() {
        let text = """
        [mcp_servers.sentry]
        command = "npx"
        http_headers = { Authorization = "Bearer x1", "X-Custom" = "plain" }
        env = { ZULIP_KEY = "short", MODE = "strict" }
        """
        let out = ConfigFileReader.redact(text)
        XCTAssertFalse(out.contains("Bearer x1"), out)
        XCTAssertFalse(out.contains("\"short\""), out)
        XCTAssertTrue(out.contains("X-Custom"), out)
        XCTAssertTrue(out.contains("\"plain\""), out)
        XCTAssertTrue(out.contains("\"strict\""), out)
        XCTAssertTrue(out.contains("command = \"npx\""), out)
        XCTAssertTrue(out.contains("[mcp_servers.sentry]"), out)
    }

    /// A TOML list,  on one line or one element per line,  hides the value after a flag.
    func test_a_secret_in_a_toml_list_is_redacted() {
        let oneLine = ConfigFileReader.redact(#"args = ["--api-key", "k1", "-m", "fleet_rag"]"#)
        XCTAssertFalse(oneLine.contains("\"k1\""), oneLine)
        XCTAssertTrue(oneLine.contains("fleet_rag"), oneLine)
        let manyLines = ConfigFileReader.redact("""
        args = [
          "--token",
          "t0ken",
          "--verbose",
        ]
        model = "gpt-5"
        """)
        XCTAssertFalse(manyLines.contains("t0ken"), manyLines)
        XCTAssertTrue(manyLines.contains("--verbose"), manyLines)
        XCTAssertTrue(manyLines.contains("gpt-5"), manyLines)
    }

    /// A name ending in `_KEY` or `_PAT` is a secret name,  and a word that only ends in
    /// the same letters is not.
    func test_key_and_pat_endings_are_secret_names() {
        XCTAssertTrue(ConfigFileReader.isSecretKey("ZULIP_KEY"))
        XCTAssertTrue(ConfigFileReader.isSecretKey("GITHUB_PAT"))
        XCTAssertTrue(ConfigFileReader.isSecretKey("signing-key"))
        XCTAssertFalse(ConfigFileReader.isSecretKey("monkey"))
        XCTAssertFalse(ConfigFileReader.isSecretKey("compat"))
        XCTAssertFalse(ConfigFileReader.isSecretKey("command"))
    }

    /// A credential inside a value with spaces is found,  and prose about one is not.
    func test_a_credential_inside_a_value_is_found() {
        XCTAssertTrue(ConfigFileReader.isSecretString("Authorization: Bearer abc123"))
        XCTAssertTrue(ConfigFileReader.isSecretString("Bearer x1"))
        XCTAssertTrue(ConfigFileReader.isSecretString("run --token abc now"))
        XCTAssertTrue(ConfigFileReader.isSecretString("API_KEY=abc python3 x.py"))
        XCTAssertFalse(ConfigFileReader.isSecretString("send a Bearer token with each call"))
        XCTAssertFalse(ConfigFileReader.isSecretString("python3 -m fleet_rag"))
    }

    // MARK: GRID-29:  a URL with a port

    /// Only a user part before an `@` can hold a password:  a host and a port cannot.
    func test_a_url_with_a_port_is_not_a_secret() {
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("http://localhost:8080/mcp"))
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("https://server.example.test:8799"))
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("https://user@host.example.test/x"))
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("https://user:pass@host.example.test:8080/x"))
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("https://abcdef0123456789ABCDEF0123456789@host.example.test"))
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("https://host.example.test/a?api_key=value"))
        let toml = ConfigFileReader.redact(#"url = "http://localhost:8080/mcp""#)
        XCTAssertEqual(toml, #"url = "http://localhost:8080/mcp""#)
    }

    /// A key in a URL's query,  or a token in its path,  is a secret even when the URL has a
    /// port,  and a path made of words is not.
    func test_a_url_key_or_path_token_is_a_secret() {
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("http://h:8080/x?key=" + "AIza" + "SyA0123456789abcdefghijklmnopqrstu"))
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("https://h.example.test/x?sig=abc&code=xyz"))
        XCTAssertTrue(ConfigFileReader.looksLikeSecretValue("http://host.example.test:8443/hooks/T0123ABCDEFabcdef0123456"))
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("https://github.com/Simple-With-Us/AI-Fleet-Coordinator"))
        XCTAssertFalse(ConfigFileReader.looksLikeSecretValue("http://localhost:8080/mcp?mode=strict"))
    }

    // MARK: Words that only look like a scheme or a header flag

    /// `-h` is help or sizes,  and `Basic` and `Bearer` in prose are not a credential:  none
    /// of these is hidden,  so the hook command and the description stay on screen.
    func test_help_flags_and_scheme_prose_are_not_secrets() {
        XCTAssertFalse(ConfigFileReader.isSecretString("df -h / | tail -1"))
        XCTAssertFalse(ConfigFileReader.isSecretString("df -H /"))
        XCTAssertFalse(ConfigFileReader.isSecretString("Basic checks for the lane"))
        XCTAssertFalse(ConfigFileReader.isSecretString("A basic guard"))
        XCTAssertFalse(ConfigFileReader.isSecretString("the bearer of the key"))
        XCTAssertTrue(ConfigFileReader.isSecretString("Basic dXNlcjpwYXNz"))
        XCTAssertTrue(ConfigFileReader.isSecretString("curl -H 'Authorization: Basic " + "dXNlcjpwYXNz' x"))
        XCTAssertTrue(ConfigFileReader.isSecretString("curl -H 'X-Custom: value' x"))
        let json = #"{"args": ["serve", "-h", "0.0.0.0"], "description": "Basic lint checks"}"#
        let hidden = ConfigFileReader.redaction(of: json)
        XCTAssertEqual(hidden.text, json)
        XCTAssertEqual(hidden.replaced, 0)
    }

    // MARK: Inline tables written without spaces

    /// A table that opens straight after `=` is read pair by pair,  so a secret-named value
    /// inside it is hidden however short it is.
    func test_a_secret_in_an_unspaced_inline_table_is_redacted() {
        let tight = ConfigFileReader.redact(#"env = {API_KEY="abc123xyz"}"#)
        XCTAssertFalse(tight.contains("abc123xyz"), tight)
        XCTAssertTrue(tight.hasPrefix("env = {API_KEY="), tight)
        let spaced = ConfigFileReader.redact(#"env = {API_KEY = "short"}"#)
        XCTAssertFalse(spaced.contains("short"), spaced)
        let nested = ConfigFileReader.redact(#"a = { credentials = {user="x1y2"} }"#)
        XCTAssertFalse(nested.contains("x1y2"), nested)
        XCTAssertTrue(nested.hasPrefix("a = { credentials = "), nested)
    }

    /// A minified JSON file cut off at the limit cannot be parsed,  and its secret-named
    /// values are still hidden line by line.
    func test_a_cut_off_minified_json_line_hides_its_secrets() {
        let cut = #"{"mcpServers":{"x":{"command":"npx","env":{"API_KEY":"abc"}}},"model":"gpt-"#
        let hidden = ConfigFileReader.redaction(of: cut, truncated: true)
        XCTAssertFalse(hidden.text.contains(#""abc""#), hidden.text)
        XCTAssertTrue(hidden.text.contains("npx"), hidden.text)
        XCTAssertTrue(hidden.cutOffJSON)
        XCTAssertEqual(hidden.replaced, 1)
    }

    // MARK: A hidden JSON value never renames a key

    /// A hidden value spelled like a key leaves the key alone.
    func test_a_hidden_json_value_spelled_like_a_key_keeps_the_key() {
        let same = ConfigFileReader.redaction(of: #"{"token": "token"}"#)
        XCTAssertEqual(same.text, #"{"token": "<redacted>"}"#)
        XCTAssertFalse(same.reformatted)
        let server = #"{"mcpServers": {"abc123": {"args": ["--token", "abc123"]}}}"#
        let hidden = ConfigFileReader.redaction(of: server)
        XCTAssertEqual(hidden.text, #"{"mcpServers": {"abc123": {"args": ["--token", "<redacted>"]}}}"#)
        XCTAssertEqual(hidden.replaced, 1)
        XCTAssertFalse(hidden.reformatted)
    }

    // MARK: GRID-30:  JSON keeps its own bytes,  and redacted means a value was hidden

    /// A JSON file with nothing to hide is shown byte for byte and is not called redacted.
    func test_a_json_file_with_nothing_to_hide_is_shown_as_it_is() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let original = """
        {"z": {"command": "/Users/jay/bin/guard"},
         "a": 1}
        """
        try original.write(to: home.appendingPathComponent(".claude/settings.json"), atomically: true, encoding: .utf8)
        let read = ConfigFileReader.inspect(target: "~/.claude/settings.json", checkType: "hook_registered", home: home)
        guard case .file(_, let text, _, let redacted) = read.view else {
            return XCTFail("the file should have been read")
        }
        XCTAssertEqual(text, original)
        XCTAssertFalse(redacted)
        XCTAssertFalse(read.reformatted)
        XCTAssertEqual(read.redactedCount, 0)
        XCTAssertEqual(read.summary, "the file")
        XCTAssertFalse(text.contains("\\/"))
    }

    /// A secret string is hidden where it stands,  and the rest of the file keeps its bytes.
    func test_a_json_secret_is_hidden_in_place() {
        let original = """
        {"z": "/Users/jay/x",
         "apiKey": "sk-LIVE_SECRET_VALUE",
         "a": 1}
        """
        let hidden = ConfigFileReader.redaction(of: original)
        XCTAssertEqual(
            hidden.text,
            original.replacingOccurrences(of: "\"sk-LIVE_SECRET_VALUE\"", with: "\"<redacted>\"")
        )
        XCTAssertEqual(hidden.replaced, 1)
        XCTAssertFalse(hidden.reformatted)
    }

    /// A secret key that holds an object is hidden by rewriting the file,  and the rewrite
    /// says so and keeps slashes as they are.
    func test_a_rewritten_json_file_says_so_and_keeps_its_slashes() {
        let hidden = ConfigFileReader.redaction(of: #"{"credentials": {"user": "jay"}, "path": "/Users/jay/x"}"#)
        XCTAssertTrue(hidden.reformatted)
        XCTAssertEqual(hidden.replaced, 1)
        XCTAssertFalse(hidden.text.contains("\"jay\""), hidden.text)
        XCTAssertTrue(hidden.text.contains("/Users/jay/x"), hidden.text)
        XCTAssertFalse(hidden.text.contains("\\/"), hidden.text)
        let read = ConfigFileRead(
            state: .text,
            view: .file(path: "~/x.json", text: hidden.text, truncated: false, redacted: true),
            redactedCount: hidden.replaced,
            reformatted: hidden.reformatted
        )
        XCTAssertEqual(read.summary, "the file, reformatted for reading, with 1 secret value redacted")
        XCTAssertEqual(read.notes.count, 1)
    }

    // MARK: GRID-31:  missing,  unreadable and cut-off are told apart

    /// A file the app may not read is unreadable,  with the system's reason,  never missing.
    func test_a_file_that_cannot_be_read_is_unreadable_not_missing() throws {
        let home = try makeHome()
        defer {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o644],
                ofItemAtPath: home.appendingPathComponent(".claude/locked.json").path
            )
            try? FileManager.default.removeItem(at: home)
        }
        let locked = home.appendingPathComponent(".claude/locked.json")
        try "{}".write(to: locked, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: locked.path)
        if FileManager.default.isReadableFile(atPath: locked.path) {
            throw XCTSkip("this user can read a file with no permissions, so the case cannot be made")
        }
        let read = ConfigFileReader.inspect(target: "~/.claude/locked.json", checkType: "hook_registered", home: home)
        guard case .unreadable(let reason) = read.state else {
            return XCTFail("the file should read as unreadable, not \(read.state)")
        }
        XCTAssertFalse(reason.isEmpty)
        XCTAssertEqual(read.summary, "on this Mac, not readable")
        XCTAssertTrue(read.message?.hasPrefix("Something is at this path on this Mac, and it could not be read.") ?? false)
        guard case .refused(_, let shown) = read.view else {
            return XCTFail("the panel should show the reason, not a missing path")
        }
        XCTAssertEqual(shown, read.message)
    }

    /// A link to nothing is unreadable,  and a path that is not there is missing.
    func test_a_link_to_nothing_is_unreadable_and_a_missing_path_is_missing() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createSymbolicLink(
            atPath: home.appendingPathComponent(".claude/dangling.json").path,
            withDestinationPath: home.appendingPathComponent(".claude/gone.json").path
        )
        let dangling = ConfigFileReader.inspect(target: "~/.claude/dangling.json", checkType: nil, home: home)
        guard case .unreadable = dangling.state else {
            return XCTFail("a link to nothing should read as unreadable, not \(dangling.state)")
        }
        let missing = ConfigFileReader.inspect(target: "~/.claude/gone.json", checkType: nil, home: home)
        XCTAssertEqual(missing.state, .missing)
        XCTAssertEqual(missing.message, ConfigFileReader.missingMessage)
    }

    // MARK: PERF-28 and COPY-63:  a cut inside a character,  and a file that is not text

    /// A cut at the limit that lands inside a character drops the partial character,  so
    /// a long text file reads as text.
    func test_a_cut_inside_a_character_still_reads_as_text() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-cfg-cut-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        // The two bytes of "é" sit across the limit.
        let text = String(repeating: "a", count: ConfigFileReader.byteLimit - 1) + "é" + String(repeating: "b", count: 64)
        try text.write(to: home.appendingPathComponent("long.md"), atomically: true, encoding: .utf8)
        let read = ConfigFileReader.inspect(target: "~/long.md", checkType: "marked_block_hash", home: home)
        XCTAssertEqual(read.state, .truncated)
        guard case .file(_, let shown, true, _) = read.view else {
            return XCTFail("the file should have been read as truncated text")
        }
        XCTAssertEqual(shown.count, ConfigFileReader.byteLimit - 1)
        XCTAssertEqual(read.summary, "the file's first part")
    }

    /// A file that is not text says it is not shown,  and promises nothing more.
    func test_a_file_that_is_not_text_says_it_is_not_shown() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try Data([0x80, 0x81, 0xFF, 0x00, 0xC3]).write(to: home.appendingPathComponent(".claude/blob.bin"))
        let read = ConfigFileReader.inspect(target: "~/.claude/blob.bin", checkType: nil, home: home)
        XCTAssertEqual(read.state, .notText)
        XCTAssertEqual(read.summary, "not text")
        XCTAssertEqual(read.message, "This file is not text, so it is not shown here.")
        XCTAssertFalse(read.message?.contains("bytes") ?? true)
        guard case .refused(_, let reason) = read.view else {
            return XCTFail("the panel should say the file is not shown")
        }
        XCTAssertEqual(reason, ConfigFileReader.notTextMessage)
        XCTAssertEqual(read.view.summary, "not shown")
    }

    // MARK: PR 1477 review:  scheme words,  fail closed,  one pass,  cut-off arrays

    /// `basic` and `bearer` as plain list elements name a mode,  so the next flag is kept.
    func test_a_scheme_word_before_a_flag_hides_nothing() {
        XCTAssertFalse(ConfigFileReader.isSecretFlag("basic"))
        XCTAssertFalse(ConfigFileReader.isSecretFlag("Bearer"))
        XCTAssertTrue(ConfigFileReader.isSecretFlag("--token"))
        XCTAssertTrue(ConfigFileReader.isSecretFlag("--api-key"))
        XCTAssertTrue(ConfigFileReader.isSecretFlag("--header"))
        XCTAssertTrue(ConfigFileReader.isSecretFlag("-H"))
        let json = #"{"args": ["--auth-mode", "basic", "--port", "8080"], "more": ["Bearer", "tokens", "basic", "credentials"]}"#
        let hidden = ConfigFileReader.redaction(of: json)
        XCTAssertEqual(hidden.text, json)
        XCTAssertEqual(hidden.replaced, 0)
        let toml = #"args = ["--auth-mode", "basic", "--port", "8080"]"#
        XCTAssertEqual(ConfigFileReader.redact(toml), toml)
        let manyLines = """
        args = [
          "--auth-mode",
          "bearer",
          "--port",
          "8080",
        ]
        """
        XCTAssertEqual(ConfigFileReader.redact(manyLines), manyLines)
    }

    /// The element after `basic` or `bearer` is hidden unless it is a flag,  a phrase or a
    /// prose word.
    func test_a_scheme_word_before_a_credential_hides_it() {
        let encoded = "dXNlcjpw" + "YXNz"  // made up;  split so the secret scanner does not flag it
        let shaped = "abc123" + "xyz789"
        let json = #"{"args": ["--auth-mode", "basic", ""# + encoded + #"", "--port", "8080"]}"#
        let hidden = ConfigFileReader.redaction(of: json)
        XCTAssertFalse(hidden.text.contains(encoded), hidden.text)
        XCTAssertTrue(hidden.text.contains(#""--port", "8080""#), hidden.text)
        XCTAssertEqual(hidden.replaced, 1)
        XCTAssertFalse(hidden.reformatted)
        let bearer = ConfigFileReader.redaction(of: #"{"args": ["Bearer", ""# + shaped + #""]}"#)
        XCTAssertFalse(bearer.text.contains(shaped), bearer.text)
        XCTAssertEqual(bearer.replaced, 1)
        let toml = ConfigFileReader.redact(#"args = ["Bearer", ""# + shaped + #"", "--port", "8080"]"#)
        XCTAssertFalse(toml.contains(shaped), toml)
        XCTAssertTrue(toml.contains(#""--port", "8080""#), toml)
        let manyLines = ConfigFileReader.redact("args = [\n  \"basic\",\n  \"" + encoded + "\",\n]")
        XCTAssertFalse(manyLines.contains(encoded), manyLines)
        // Short,  all-lowercase and mid-length values after a scheme word stay hidden,  as
        // they were before scheme words stopped being flags.
        let short = "x1y2" + "z3"
        let lower = "abcdef" + "ghijk"
        for value in [short, lower] {
            for scheme in ["Bearer", "Basic"] {
                let list = ConfigFileReader.redaction(of: #"{"args": [""# + scheme + #"", ""# + value + #""]}"#)
                XCTAssertFalse(list.text.contains(value), list.text)
                XCTAssertEqual(list.replaced, 1)
                let line = ConfigFileReader.redact(#"args = [""# + scheme + #"", ""# + value + #""]"#)
                XCTAssertFalse(line.contains(value), line)
            }
        }
    }

    /// A line pattern that did not compile makes the line reader hide more,  never less,
    /// and the read says so.
    func test_a_missing_line_pattern_hides_every_value_and_says_so() {
        let standard = ConfigFileReader.LineExpressions.standard
        XCTAssertTrue(standard.isComplete)
        let tableLine = #"env = { ZULIP_KEY = ""# + "sh" + #"ort", MODE = "strict" }"#
        let text = "[mcp_servers.sentry]\n" + tableLine + "\nmodel = \"gpt-5\"\n"
        let working = ConfigFileReader.redaction(of: text)
        XCTAssertFalse(working.failedClosed)
        XCTAssertFalse(working.text.contains("\"short\""), working.text)
        XCTAssertTrue(working.text.contains("\"strict\""), working.text)
        let broken = [
            ConfigFileReader.LineExpressions(pair: nil, quoted: standard.quoted),
            ConfigFileReader.LineExpressions(pair: standard.pair, quoted: nil),
        ]
        for expressions in broken {
            XCTAssertFalse(expressions.isComplete)
            let hidden = ConfigFileReader.redaction(of: text, expressions: expressions)
            XCTAssertTrue(hidden.failedClosed)
            XCTAssertFalse(hidden.text.contains("short"), hidden.text)
            XCTAssertEqual(hidden.text, "[mcp_servers.sentry]\nenv = <redacted>\nmodel = <redacted>\n")
            XCTAssertEqual(hidden.replaced, 2)
        }
        let flagged = ConfigFileReader.redaction(
            of: "args = [\n  \"--to" + "ken\",\n  \"t0" + "ken\",\n]",
            expressions: broken[1]
        )
        XCTAssertFalse(flagged.text.contains("t0ken"), flagged.text)
        // A `=` or `:` inside a list element is part of the value,  not a separator,  so no
        // piece of the element is kept before it.
        let padded = "c2VjcmV0" + "LXZhbHVl=="
        let colon = "sk-abc" + ":def"
        let list = "args = [\n  \"--to" + "ken\",\n  \"" + padded + "\",\n  '" + colon + "',\n]\n\"quoted.name\" = \"kept\"\n"
        for expressions in broken {
            let hidden = ConfigFileReader.redaction(of: list, expressions: expressions)
            for piece in ["c2VjcmV0", "LXZhbHVl", "sk-abc", "def"] {
                XCTAssertFalse(hidden.text.contains(piece), hidden.text)
            }
            XCTAssertEqual(
                hidden.text,
                "args = <redacted>\n  <redacted>\n  <redacted>\n  <redacted>\n]\n\"quoted.name\" = <redacted>\n"
            )
            XCTAssertEqual(hidden.replaced, 5)
        }
        let read = ConfigFileRead(
            state: .text,
            view: .file(path: "~/x.toml", text: "env = <redacted>", truncated: false, redacted: true),
            redactedCount: 1,
            failedClosed: true
        )
        XCTAssertTrue(read.notes.contains(ConfigFileReader.failedClosedNote))
    }

    /// Many secrets in one JSON file are all hidden where they stand,  and every key stays.
    func test_many_json_secrets_are_hidden_in_place() {
        let total = 400
        var original = "{\n"
        var expected = "{\n"
        for index in 0..<total {
            let value = "Ab\(index)" + String(repeating: "x9", count: 12)
            let comma = index < total - 1 ? "," : ""
            original += "  \"entry\(index)\": \"\(value)\"\(comma)\n"
            expected += "  \"entry\(index)\": \"<redacted>\"\(comma)\n"
        }
        original += "}"
        expected += "}"
        let hidden = ConfigFileReader.redaction(of: original)
        XCTAssertEqual(hidden.replaced, total)
        XCTAssertFalse(hidden.reformatted)
        XCTAssertEqual(hidden.text, expected)
    }

    /// A value quoted inside another string is part of that string and is left alone,  and a
    /// hidden value the file escapes another way makes the reader rewrite the document.
    func test_in_place_json_hiding_reads_whole_literals() {
        let shaped = "abc" + "123"
        let quotedInside = #"{"note": "say \""# + shaped + #"\" here", "args": ["--token", ""# + shaped + #""]}"#
        let hidden = ConfigFileReader.redaction(of: quotedInside)
        XCTAssertEqual(hidden.text, #"{"note": "say \""# + shaped + #"\" here", "args": ["--token", "<redacted>"]}"#)
        XCTAssertFalse(hidden.reformatted)
        let escaped = ConfigFileReader.redaction(of: #"{"apiKey": "a\/b-"# + #"LIVE_VALUE", "path": "/x"}"#)
        XCTAssertTrue(escaped.reformatted)
        XCTAssertEqual(escaped.replaced, 1)
        XCTAssertFalse(escaped.text.contains("LIVE_VALUE"), escaped.text)
        XCTAssertTrue(escaped.text.contains("/x"), escaped.text)
    }

    /// A JSON array cut off at the limit reads as cut-off JSON,  as an object does,  and a
    /// TOML table header does not.
    func test_a_cut_off_json_array_is_called_cut_off_json() {
        let cut = #"[{"mcpServers":{"x":{"command":"npx","env":{"API_KEY":"abc"}}}},{"model":"gpt-"#
        let hidden = ConfigFileReader.redaction(of: cut, truncated: true)
        XCTAssertTrue(hidden.cutOffJSON)
        XCTAssertFalse(hidden.text.contains(#""abc""#), hidden.text)
        XCTAssertTrue(hidden.text.contains("npx"), hidden.text)
        XCTAssertEqual(hidden.replaced, 1)
        XCTAssertFalse(ConfigFileReader.redaction(of: cut, truncated: false).cutOffJSON)
        XCTAssertTrue(ConfigFileReader.redaction(of: "\n  [\n  {\"command\": \"npx\",", truncated: true).cutOffJSON)
        XCTAssertTrue(ConfigFileReader.opensLikeJSON("["))
        XCTAssertTrue(ConfigFileReader.opensLikeJSON("  [1, 2"))
        XCTAssertTrue(ConfigFileReader.opensLikeJSON("[-1.5e3,"))
        XCTAssertTrue(ConfigFileReader.opensLikeJSON("[12"))
        XCTAssertTrue(ConfigFileReader.opensLikeJSON("[ 7 ]"))
        // A TOML table header whose bare name starts with a digit or a `-` is not a number.
        XCTAssertFalse(ConfigFileReader.opensLikeJSON("[1password]\nx = 1"))
        XCTAssertFalse(ConfigFileReader.opensLikeJSON("[2fa.keys]\nx = 1"))
        XCTAssertFalse(ConfigFileReader.opensLikeJSON("[-x]\ny = 2"))
        XCTAssertFalse(ConfigFileReader.redaction(of: "[1password]\nx = 1\n", truncated: true).cutOffJSON)
        XCTAssertFalse(ConfigFileReader.redaction(of: "[mcp_servers.x]\ncommand = \"npx\"\n", truncated: true).cutOffJSON)
        XCTAssertFalse(ConfigFileReader.redaction(of: "[[servers]]\nname = \"x\"\n", truncated: true).cutOffJSON)
    }
}
