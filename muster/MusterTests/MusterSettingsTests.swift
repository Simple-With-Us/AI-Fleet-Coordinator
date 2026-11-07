import XCTest

final class MusterSettingsTests: XCTestCase {
    private func suite() -> UserDefaults { UserDefaults(suiteName: "muster-settings-\(UUID().uuidString)")! }

    func testRepoRootRoundTripUsesReaderKey() {
        let d = suite()
        d.set("/tmp/my-repo", forKey: MusterSettings.repoRootDefaultsKey)
        XCTAssertEqual(MusterSettings.repoRootDefaultsKey, FleetConfigLocation.defaultsKey)
        XCTAssertEqual(FleetConfigLocation.resolve(defaults: d, environment: [:], home: URL(fileURLWithPath: "/Users/t")).repoRoot.path, "/tmp/my-repo")
    }

    // The scripts folder's order (environment, then saved, then the usual place, each only when
    // it holds the scripts) is tested against its one resolver in SettingsFoldersTests.

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
