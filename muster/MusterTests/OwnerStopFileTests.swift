import XCTest

/// The file that keeps the owner's stops between launches of Muster.
final class OwnerStopFileTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("owner-stop-tests-\(UUID().uuidString)", isDirectory: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    private var file: OwnerStopFile {
        OwnerStopFile(url: directory.appendingPathComponent("Muster/owner-stops.json"))
    }

    func testThereIsNoRecordBeforeTheFirstStop() {
        XCTAssertEqual(file.load(), .stops(OwnerStops()))
    }

    func testAStopComesBackWithItsTime() throws {
        // Whole seconds,  because the file writes ISO 8601 text.
        let when = Date(timeIntervalSince1970: 1_800_000_000)
        var stops = OwnerStops()
        stops.record(.stop, id: "shellular", at: when)
        stops.record(.stop, id: "seat-mcp", at: when.addingTimeInterval(60))
        XCTAssertNil(file.save(stops))
        XCTAssertEqual(file.load(), .stops(stops))
    }

    func testAStartTakesTheStopOutOfTheFile() {
        var stops = OwnerStops()
        stops.record(.stop, id: "shellular")
        XCTAssertNil(file.save(stops))
        stops.record(.start, id: "shellular")
        XCTAssertNil(file.save(stops))
        XCTAssertEqual(file.load(), .stops(OwnerStops()))
    }

    func testTheFileHoldsOnlyIdsAndTimes() throws {
        var stops = OwnerStops()
        stops.record(.stop, id: "shellular", at: Date(timeIntervalSince1970: 1_800_000_000))
        XCTAssertNil(file.save(stops))
        let text = try String(contentsOf: directory.appendingPathComponent("Muster/owner-stops.json"), encoding: .utf8)
        XCTAssertTrue(text.contains("\"shellular\""))
        XCTAssertTrue(text.contains("\"schemaVersion\""))
        for word in ["token", "password", "secret", "key"] {
            XCTAssertFalse(text.lowercased().contains(word), word)
        }
    }

    func testAFileThatCannotBeReadIsUnreadableAndNotEmpty() throws {
        // An empty record would let Muster start everything the owner stopped.
        try FileManager.default.createDirectory(at: directory.appendingPathComponent("Muster"),
                                                withIntermediateDirectories: true)
        try Data("not json".utf8).write(to: directory.appendingPathComponent("Muster/owner-stops.json"))
        guard case .unreadable(let why) = file.load() else { return XCTFail("The file was read as a record.") }
        XCTAssertTrue(why.contains("owner-stops.json"), why)
        XCTAssertTrue(why.contains("starts nothing"), why)
    }

    func testAFileOfAnotherVersionIsUnreadable() throws {
        try FileManager.default.createDirectory(at: directory.appendingPathComponent("Muster"),
                                                withIntermediateDirectories: true)
        try Data(#"{"schemaVersion":2,"stops":{"shellular":"2026-11-05T14:41:00Z"}}"#.utf8)
            .write(to: directory.appendingPathComponent("Muster/owner-stops.json"))
        guard case .unreadable = file.load() else { return XCTFail("A newer file was read as a record.") }
    }

    func testAStopWithATimeThatDoesNotParseIsStillAStop() throws {
        try FileManager.default.createDirectory(at: directory.appendingPathComponent("Muster"),
                                                withIntermediateDirectories: true)
        try Data(#"{"schemaVersion":1,"stops":{"shellular":"yesterday"}}"#.utf8)
            .write(to: directory.appendingPathComponent("Muster/owner-stops.json"))
        guard case .stops(let stops) = file.load() else { return XCTFail("The file was not read.") }
        XCTAssertEqual(stops.ids, ["shellular"])
        XCTAssertNil(stops.stoppedAt["shellular"])
    }

    func testAWriteThatFailsSaysSo() throws {
        // The parent is a file,  so the folder cannot be made.
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let blocker = directory.appendingPathComponent("Muster")
        try Data("x".utf8).write(to: blocker)
        var stops = OwnerStops()
        stops.record(.stop, id: "shellular")
        let problem = file.save(stops)
        XCTAssertNotNil(problem)
        XCTAssertTrue(problem?.contains("will not remember it after Muster quits") == true, problem ?? "")
    }

    func testTheStandardFileIsInApplicationSupport() {
        let home = URL(fileURLWithPath: "/Users/tester", isDirectory: true)
        XCTAssertEqual(OwnerStopFile.standardURL(home: home).path,
                       "/Users/tester/Library/Application Support/Muster/owner-stops.json")
    }
}
