import XCTest

/// Knobs in an Infisical folder other than the root:  each folder has its own copy on this
/// Mac,  named and checked the way `scripts/fleet_knobs/lkg.py` names and checks it,  and a
/// knob is read from its own folder's copy and from no other.
final class KnobFolderTests: XCTestCase {
    private static let project = "test-infisical-project"
    private static let environment = "prod"

    private var identity: EngineIdentity.Identity {
        EngineIdentity.Identity(project: Self.project, environment: Self.environment)
    }

    /// A repo whose generated registry has a root knob,  a `/wake-router` knob and a
    /// `/wake-router` secret,  and a home with a private copy directory.
    private func makeRepo() throws -> (root: URL, home: URL) {
        let base = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-knob-folders-\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: base) }
        let root = base.appendingPathComponent("repo", isDirectory: true)
        let home = base.appendingPathComponent("home", isDirectory: true)
        let inventoryFolder = root.appendingPathComponent("scripts/fleet_knobs", isDirectory: true)
        try FileManager.default.createDirectory(at: inventoryFolder, withIntermediateDirectories: true)
        let copyDirectory = home.appendingPathComponent(".local/state/fleet-knobs", isDirectory: true)
        try FileManager.default.createDirectory(at: copyDirectory, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: copyDirectory.path)
        try """
        {"schema": 1, "knobs": [
          {"name": "FLEET_AUTOPUSH_MODE", "owner": "C1", "kind": "enum", "kind_text": "enum: off < report",
           "choices": ["off", "report"], "secret": false, "default_text": "off", "default": "off", "folder": "/"},
          {"name": "WAKE_ROUTER_WORKERS", "owner": "W2", "kind": "int", "kind_text": "int 1 to 64",
           "minimum": 1, "maximum": 64, "narrower": "min", "secret": false, "default_text": "4", "default": "4",
           "folder": "/wake-router"},
          {"name": "WAKE_ROUTER_CLAUDE_KEY", "owner": "W2", "kind": "str", "kind_text": "str",
           "secret": true, "folder": "/wake-router"}
        ]}
        """.write(to: inventoryFolder.appendingPathComponent("registry.json"), atomically: true, encoding: .utf8)
        return (root, home)
    }

    /// Write one folder's copy the way the engine writes it.  `path` is the folder the copy
    /// records;  nil leaves it out,  as the engine does for the root.
    private func writeCopy(
        in home: URL,
        file: String,
        knobs: [String: String],
        age: TimeInterval = 60,
        path: String? = nil
    ) throws {
        var body: [String: Any] = [
            "schema": 1,
            "project": Self.project,
            "environment": Self.environment,
            "knobs": knobs,
            "saved_epoch": Date().timeIntervalSince1970 - age,
        ]
        if let path { body["path"] = path }
        let url = home.appendingPathComponent(".local/state/fleet-knobs/\(file)")
        try JSONSerialization.data(withJSONObject: body).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private func knob(_ registry: KnobRegistry, _ name: String) throws -> KnobRegistry.Knob {
        try XCTUnwrap(registry.knobs.first { $0.name == name }, name)
    }

    // MARK: Reading each folder's copy

    /// A `/wake-router` knob is read from `lkg-wake-router.json`,  never from the root copy.
    func test_a_wake_router_knob_is_read_from_its_own_copy() throws {
        let (root, home) = try makeRepo()
        // The root copy carries an old value under the same name,  and it must not be used.
        try writeCopy(in: home, file: "lkg.json",
                      knobs: ["FLEET_AUTOPUSH_MODE": "report", "WAKE_ROUTER_WORKERS": "2"])
        try writeCopy(in: home, file: "lkg-wake-router.json",
                      knobs: ["WAKE_ROUTER_WORKERS": "8"], path: "/wake-router")
        let registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        let workers = try knob(registry, "WAKE_ROUTER_WORKERS")
        XCTAssertEqual(workers.folder, "/wake-router")
        XCTAssertEqual(workers.current, .current("8"))
        XCTAssertEqual(workers.source, .macCopy)
        XCTAssertEqual(try knob(registry, "FLEET_AUTOPUSH_MODE").current, .current("report"))
        // A secret in the folder is still never read from a copy.
        XCTAssertEqual(try knob(registry, "WAKE_ROUTER_CLAUDE_KEY").current, .secretUnconfirmed)
        let other = try XCTUnwrap(registry.folderCopies.first)
        XCTAssertEqual(registry.folderCopies.count, 1)
        XCTAssertEqual(other.folder, "/wake-router")
        XCTAssertTrue(other.isReadable)
        XCTAssertFalse(other.isStale)
        XCTAssertTrue(other.headline.hasPrefix("Values in /wake-router are from this Mac's copy of that folder, saved at "),
                      other.headline)
        // The root copy's headline is the one it always was.
        XCTAssertTrue(registry.copyHeadline.hasPrefix("Values are from this Mac's copy, saved at "))
    }

    /// With no `/wake-router` copy,  that folder's knob is unknown,  never its default,
    /// even while the root copy is there and fresh.
    func test_a_wake_router_knob_is_unknown_when_its_copy_is_missing() throws {
        let (root, home) = try makeRepo()
        try writeCopy(in: home, file: "lkg.json", knobs: ["FLEET_AUTOPUSH_MODE": "report"])
        let registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        let workers = try knob(registry, "WAKE_ROUTER_WORKERS")
        XCTAssertEqual(workers.current, .unknown("no local copy (folder /wake-router)"))
        XCTAssertEqual(workers.source, .notRead)
        XCTAssertNotEqual(workers.nowText, "default (4)")
        // The root knob is still read,  and the root copy is still the readable one.
        XCTAssertEqual(try knob(registry, "FLEET_AUTOPUSH_MODE").current, .current("report"))
        XCTAssertTrue(registry.copyIsReadable)
        let other = try XCTUnwrap(registry.folderCopies.first)
        XCTAssertFalse(other.isReadable)
        XCTAssertEqual(other.headline, KnobReader.folderNoCopyHeadline("/wake-router"))
        XCTAssertTrue(registry.freshnessLines().contains(
            KnobFreshnessLine(text: KnobReader.folderNoCopyHeadline("/wake-router"), isWarning: true)))
    }

    /// A copy whose recorded folder is not the one asked for is refused,  as the engine
    /// refuses it (`wrong-folder`):  the folder's own file with the root's folder,  with no
    /// folder at all,  and the root's file claiming another folder.
    func test_a_copy_for_another_folder_is_refused() throws {
        let (root, home) = try makeRepo()
        try writeCopy(in: home, file: "lkg.json", knobs: ["FLEET_AUTOPUSH_MODE": "report"])
        try writeCopy(in: home, file: "lkg-wake-router.json",
                      knobs: ["WAKE_ROUTER_WORKERS": "8"], path: "/")
        var registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        XCTAssertEqual(try knob(registry, "WAKE_ROUTER_WORKERS").current,
                       .unknown("the copy is for another folder (folder /wake-router)"))
        XCTAssertEqual(registry.folderCopies.first?.headline,
                       "This Mac's copy of the /wake-router knobs is for another folder, so it is not read.")

        // A non-root copy that records no folder is the root's shape,  and is refused too.
        try writeCopy(in: home, file: "lkg-wake-router.json", knobs: ["WAKE_ROUTER_WORKERS": "8"])
        registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        XCTAssertEqual(try knob(registry, "WAKE_ROUTER_WORKERS").current,
                       .unknown("the copy is for another folder (folder /wake-router)"))

        // The root file that records another folder is refused for the root.
        try writeCopy(in: home, file: "lkg.json",
                      knobs: ["FLEET_AUTOPUSH_MODE": "report"], path: "/wake-router")
        registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        XCTAssertEqual(try knob(registry, "FLEET_AUTOPUSH_MODE").current,
                       .unknown("the copy is for another folder"))
        XCTAssertFalse(registry.copyIsReadable)
    }

    /// Each copy's age is its own:  a stale `/wake-router` copy is marked stale while the
    /// root copy is fresh,  and the root's own lines say nothing about staleness.
    func test_a_stale_folder_copy_is_stale_while_the_root_copy_is_fresh() throws {
        let (root, home) = try makeRepo()
        try writeCopy(in: home, file: "lkg.json", knobs: ["FLEET_AUTOPUSH_MODE": "report"], age: 30)
        try writeCopy(in: home, file: "lkg-wake-router.json",
                      knobs: ["WAKE_ROUTER_WORKERS": "8"], age: 2 * 3600, path: "/wake-router")
        let registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        XCTAssertFalse(registry.copyIsStale)
        XCTAssertEqual(registry.copyAge, "30s")
        let other = try XCTUnwrap(registry.folderCopies.first)
        XCTAssertTrue(other.isReadable)
        XCTAssertTrue(other.isStale)
        // A stale copy is still a reading.
        XCTAssertEqual(try knob(registry, "WAKE_ROUTER_WORKERS").current, .current("8"))
        let lines = registry.freshnessLines()
        XCTAssertFalse(lines.map(\.text).contains("It may not be current."), "the root copy is fresh")
        XCTAssertTrue(lines.contains(KnobFreshnessLine(
            text: "\(other.headline)\(ApplyCopy.sentenceGap)It may not be current.", isWarning: true)))
        // The panel keys the lines by their text,  so no two may be the same.
        XCTAssertEqual(Set(lines.map(\.text)).count, lines.count)
    }

    /// A folder copy past the engine's expiry is not a reading,  and says which folder.
    func test_an_expired_folder_copy_is_not_read() throws {
        let (root, home) = try makeRepo()
        try writeCopy(in: home, file: "lkg.json", knobs: ["FLEET_AUTOPUSH_MODE": "report"])
        try writeCopy(in: home, file: "lkg-wake-router.json",
                      knobs: ["WAKE_ROUTER_WORKERS": "8"], age: 3 * 24 * 3600, path: "/wake-router")
        let registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        XCTAssertEqual(try knob(registry, "WAKE_ROUTER_WORKERS").current,
                       .unknown("the copy is past its expiry (folder /wake-router)"))
        XCTAssertEqual(try knob(registry, "FLEET_AUTOPUSH_MODE").current, .current("report"))
    }

    // MARK: Folder names

    /// The file names are the engine's (`_suffix` in `lkg.py`),  and a folder the engine's
    /// pattern refuses has no file at all.
    func test_folder_file_names_follow_the_engine() {
        XCTAssertEqual(KnobReader.copyFileName(for: "/"), "lkg.json")
        XCTAssertEqual(KnobReader.copyFileName(for: "/wake-router"), "lkg-wake-router.json")
        XCTAssertEqual(KnobReader.copyFileName(for: "/a/b"), "lkg-a.b.json")
        XCTAssertEqual(KnobReader.copyFileName(for: "/a--b"), "lkg-a--b.json")
        XCTAssertEqual(KnobReader.copyFileName(for: "/a/b/c/d"), "lkg-a.b.c.d.json")
        for bad in ["", "wake-router", "/Wake", "/a/", "//a", "/-a", "/a.b", "/../x", "/a b",
                    "/a/b/c/d/e", "/a\n", "/" + String(repeating: "a", count: 64)] {
            XCTAssertNil(KnobReader.copyFileName(for: bad), bad.debugDescription)
        }
    }

    /// A knob whose folder the engine would refuse reads unknown,  never the root's copy.
    func test_a_knob_in_a_folder_the_engine_refuses_is_unknown() throws {
        let (root, home) = try makeRepo()
        try """
        {"schema": 1, "knobs": [
          {"name": "FLEET_AUTOPUSH_MODE", "kind": "enum", "kind_text": "enum: off < report",
           "choices": ["off", "report"], "secret": false, "default_text": "off", "folder": "/Bad.Folder"}
        ]}
        """.write(to: root.appendingPathComponent(KnobReader.inventoryPath), atomically: true, encoding: .utf8)
        try writeCopy(in: home, file: "lkg.json", knobs: ["FLEET_AUTOPUSH_MODE": "report"])
        let registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        guard case .unknown(let why) = try knob(registry, "FLEET_AUTOPUSH_MODE").current else {
            return XCTFail("a knob in a refused folder must not borrow the root's copy")
        }
        XCTAssertTrue(why.contains("/Bad.Folder"), why)
        XCTAssertEqual(registry.folders, ["/"])
    }

    /// A registry that predates folders puts every knob in the root.
    func test_a_registry_without_folders_is_all_root() throws {
        let (root, home) = try makeRepo()
        try """
        {"schema": 1, "knobs": [
          {"name": "FLEET_AUTOPUSH_MODE", "kind": "enum", "kind_text": "enum: off < report",
           "choices": ["off", "report"], "secret": false, "default_text": "off"}
        ]}
        """.write(to: root.appendingPathComponent(KnobReader.inventoryPath), atomically: true, encoding: .utf8)
        try writeCopy(in: home, file: "lkg.json", knobs: ["FLEET_AUTOPUSH_MODE": "report"])
        let registry = KnobReader.read(repoRoot: root, home: home, identity: identity)
        XCTAssertEqual(try knob(registry, "FLEET_AUTOPUSH_MODE").folder, "/")
        XCTAssertEqual(try knob(registry, "FLEET_AUTOPUSH_MODE").current, .current("report"))
        XCTAssertTrue(registry.folderCopies.isEmpty)
    }

    /// The table fallback reads a folder from the note the generator leads it with.
    func test_the_table_fallback_reads_the_folder_from_the_note() {
        XCTAssertEqual(KnobReader.folder(fromNote: "in folder /wake-router;  wake router worker threads"),
                       "/wake-router")
        XCTAssertEqual(KnobReader.folder(fromNote: "what the autopush hooks may do"), "/")
        XCTAssertEqual(KnobReader.folder(fromNote: "in folder without an end"), "/")
    }

    // MARK: The folder list the Secret Names panel reads

    /// The panel reads every folder the registry names:  the root first,  always,  then the
    /// rest once each and in order.  A folder the engine would refuse is never asked for.
    func test_the_folders_are_the_root_first_then_the_rest_in_order() throws {
        let (root, home) = try makeRepo()
        XCTAssertEqual(KnobReader.read(repoRoot: root, home: home, identity: identity).folders,
                       ["/", "/wake-router"])
        func knobIn(_ folder: String) -> KnobRegistry.Knob {
            var made = KnobRegistry.Knob(
                name: "X", owner: "C1", kind: "bool", defaultValue: "false", isSecret: false,
                secretColumn: "no", localMay: "", note: "", current: .unknown("not read")
            )
            made.folder = folder
            return made
        }
        XCTAssertEqual(
            KnobReader.folders(of: ["/zeta", "/wake-router", "/", "/alpha", "/wake-router", "Bad"].map(knobIn)),
            ["/", "/alpha", "/wake-router", "/zeta"]
        )
        // The root comes first even when no knob is in it.
        XCTAssertEqual(KnobReader.folders(of: ["/x"].map(knobIn)), ["/", "/x"])
        XCTAssertEqual(KnobReader.folders(of: []), ["/"])
    }

    /// The repository's own registry names the wake router's folder after the root.
    func test_the_real_registry_names_the_wake_router_folder() throws {
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        guard FileManager.default.fileExists(
            atPath: repoRoot.appendingPathComponent(KnobReader.inventoryPath).path
        ) else {
            throw XCTSkip("The repository's registry.json is not beside this test.")
        }
        let (_, home) = try makeRepo()
        let registry = KnobReader.read(repoRoot: repoRoot, home: home, identity: identity)
        XCTAssertEqual(registry.folders.first, "/")
        XCTAssertTrue(registry.folders.contains("/wake-router"))
        for knob in registry.knobs where knob.name.hasPrefix("WAKE_ROUTER_") {
            XCTAssertEqual(knob.folder, "/wake-router", knob.name)
        }
        // The table fallback puts every knob in the same folder as the inventory does.
        let table = try String(contentsOf: repoRoot.appendingPathComponent("INFISICAL.md"), encoding: .utf8)
        let (fallbackRoot, _) = try makeRepo()
        try FileManager.default.removeItem(at: fallbackRoot.appendingPathComponent(KnobReader.inventoryPath))
        try table.write(to: fallbackRoot.appendingPathComponent("INFISICAL.md"), atomically: true, encoding: .utf8)
        let fallback = KnobReader.read(repoRoot: fallbackRoot, home: home, identity: identity)
        XCTAssertEqual(fallback.registryFile, "INFISICAL.md")
        XCTAssertEqual(fallback.knobs.map(\.folder), registry.knobs.map(\.folder))
    }

    /// The knob folders' names are read from the knobs' own project,  through `names`.
    func test_knob_folder_names_are_read_from_the_knobs_project() throws {
        XCTAssertEqual(try FleetSecrets.namesArguments(path: "/", project: nil), ["names", "--path", "/"])
        XCTAssertEqual(
            try FleetSecrets.namesArguments(path: "/wake-router", project: FleetSecrets.knobProject),
            ["names", "--path", "/wake-router", "--project", "ai-fleet-coordinator"]
        )
        XCTAssertThrowsError(try FleetSecrets.namesArguments(path: "/wake-router", project: "--other"))
        XCTAssertThrowsError(try FleetSecrets.namesArguments(path: "wake-router", project: nil))

        let fake = FolderNamesCommands()
        fake.reply = "WAKE_ROUTER_CLAUDE_KEY\n"
        let secrets = FleetSecrets(commands: fake, home: URL(fileURLWithPath: "/Users/example", isDirectory: true))
        let groups = try secrets.secretNamesGrouped(paths: ["/wake-router"], project: FleetSecrets.knobProject)
        XCTAssertEqual(groups, [SecretNameGroup(path: "/wake-router", names: ["WAKE_ROUTER_CLAUDE_KEY"])])
        XCTAssertEqual(fake.calls.map(\.1), [["names", "--path", "/wake-router", "--project", "ai-fleet-coordinator"]])
    }
}

private final class FolderNamesCommands: CommandRunning {
    var calls: [(String, [String])] = []
    var reply = ""
    func run(_ executable: String, _ arguments: [String]) throws -> String {
        calls.append((executable, arguments))
        return reply
    }
}
