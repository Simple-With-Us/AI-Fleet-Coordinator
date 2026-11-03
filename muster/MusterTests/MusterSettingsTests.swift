import XCTest

final class MusterSettingsTests: XCTestCase {
    private func suite() -> UserDefaults { UserDefaults(suiteName: "muster-settings-\(UUID().uuidString)")! }

    func testRepoRootRoundTripUsesReaderKey() {
        let d = suite()
        d.set("/tmp/my-repo", forKey: MusterSettings.repoRootDefaultsKey)
        XCTAssertEqual(MusterSettings.repoRootDefaultsKey, FleetConfigLocation.defaultsKey)
        XCTAssertEqual(FleetConfigLocation.resolve(defaults: d, environment: [:], home: URL(fileURLWithPath: "/Users/t")).repoRoot.path, "/tmp/my-repo")
    }

    func testScriptsDirEnvOverridesDefaults() {
        let d = suite()
        d.set("/tmp/saved-scripts", forKey: MusterSettings.scriptsDirectoryDefaultsKey)
        XCTAssertEqual(MusterSettings.scriptsDirectory(defaults: d, environment: [:], home: URL(fileURLWithPath: "/Users/t")).path, "/tmp/saved-scripts")
        XCTAssertEqual(MusterSettings.scriptsDirectory(defaults: d, environment: ["MUSTER_SCRIPTS_DIR": "/tmp/env-scripts"], home: URL(fileURLWithPath: "/Users/t")).path, "/tmp/env-scripts")
        XCTAssertEqual(MusterSettings.scriptsDirectory(defaults: suite(), environment: [:], home: URL(fileURLWithPath: "/Users/t")).path, "/Users/t/Code/AI-Fleet-Coordinator/scripts")
    }

    func testZulipSeatEnvOverridesDefaultsThenCLAUDE() {
        let d = suite()
        XCTAssertEqual(MusterSettings.zulipSeat(defaults: d, environment: [:]), "CLAUDE")
        d.set("MM", forKey: MusterSettings.zulipSeatDefaultsKey)
        XCTAssertEqual(MusterSettings.zulipSeat(defaults: d, environment: [:]), "MM")
        XCTAssertEqual(MusterSettings.zulipSeat(defaults: d, environment: ["MUSTER_ZULIP_SEAT": "MC"]), "MC")
        XCTAssertEqual(ChatLocator.readingSeat(defaults: d, environment: ["MUSTER_ZULIP_SEAT": "MC"]), "MC")
        XCTAssertEqual(ChatLocator.readingSeat(defaults: d, environment: [:]), "MM")
    }
}
