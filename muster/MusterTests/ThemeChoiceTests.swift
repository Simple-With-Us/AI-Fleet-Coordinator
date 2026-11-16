import XCTest

final class ThemeChoiceTests: XCTestCase {
    private func suite() -> UserDefaults { UserDefaults(suiteName: "muster-theme-\(UUID().uuidString)")! }

    func testSystemThemeFollowsTheSystemMode() {
        XCTAssertEqual(ThemeChoice.system.appIcon(systemIsDark: true), .dark)
        XCTAssertEqual(ThemeChoice.system.appIcon(systemIsDark: false), .light)
    }

    func testAChosenThemeWinsOverTheSystemMode() {
        XCTAssertEqual(ThemeChoice.light.appIcon(systemIsDark: true), .light)
        XCTAssertEqual(ThemeChoice.dark.appIcon(systemIsDark: false), .dark)
    }

    func testSavedThemeReadsTheKeyTheWindowsWrite() {
        let defaults = suite()
        defaults.set("dark", forKey: MusterSettings.themeDefaultsKey)
        XCTAssertEqual(ThemeChoice.saved(in: defaults), .dark)
    }

    func testNothingSavedAndAnUnknownValueAreBothSystem() {
        let defaults = suite()
        XCTAssertEqual(ThemeChoice.saved(in: defaults), .system)
        defaults.set("sepia", forKey: MusterSettings.themeDefaultsKey)
        XCTAssertEqual(ThemeChoice.saved(in: defaults), .system)
    }

    func testEachVariantNamesItsOwnImageSet() {
        XCTAssertEqual(AppIconVariant.light.imageName, "MusterIconLight")
        XCTAssertEqual(AppIconVariant.dark.imageName, "MusterIconDark")
    }
}
