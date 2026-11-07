import XCTest

/// The Settings window's folder rows:  which folder Muster uses, where it came from, and the
/// words the window shows for it.  Real temporary folders, so nothing is faked.
final class SettingsFoldersTests: XCTestCase {
    private var root: URL!
    private var suites: [String] = []
    private var home: URL { root.appendingPathComponent("home") }

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("settings-folders-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
        for name in suites { UserDefaults.standard.removePersistentDomain(forName: name) }
        suites = []
    }

    private func suite() throws -> UserDefaults {
        let name = "settings-folders-\(UUID().uuidString)"
        suites.append(name)
        return try XCTUnwrap(UserDefaults(suiteName: name))
    }

    @discardableResult
    private func touch(_ relative: String, in base: URL) throws -> URL {
        let file = base.appendingPathComponent(relative)
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        XCTAssertTrue(FileManager.default.createFile(atPath: file.path, contents: Data()))
        return file
    }

    // MARK: The fleet checkout

    func testCheckoutSourceIsChosenThenEnvironmentThenTheUsualPlace() throws {
        let d = try suite()
        let env = [FleetConfigLocation.environmentKey: "/tmp/env-root"]
        XCTAssertEqual(FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home).source, .standard)
        XCTAssertEqual(FleetConfigLocation.resolveWithSource(defaults: d, environment: env, home: home).source, .environment)
        d.set("/tmp/chosen", forKey: FleetConfigLocation.defaultsKey)
        let chosen = FleetConfigLocation.resolveWithSource(defaults: d, environment: env, home: home)
        XCTAssertEqual(chosen.source, .chosen)
        XCTAssertEqual(chosen.location, FleetConfigLocation.resolve(defaults: d, environment: env, home: home))
    }

    func testCheckoutRowSaysWhenTheFolderLacksTheFleetTools() throws {
        let d = try suite()
        let row = SettingsCopy.checkoutRow(FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home),
                                           engineRoot: nil)
        XCTAssertEqual(row.sourceLine, SettingsCopy.usual)
        XCTAssertEqual(row.problem, SettingsCopy.checkoutProblem)
        XCTAssertFalse(row.hasChoice)
        XCTAssertEqual(row.path, home.appendingPathComponent("Code/AI-Fleet-Coordinator").standardizedFileURL.path)
        try touch("Code/AI-Fleet-Coordinator/scripts/fleet_config/__main__.py", in: home)
        XCTAssertNil(SettingsCopy.checkoutRow(FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home),
                                              engineRoot: nil).problem)
    }

    func testAChosenCheckoutCanBeReset() throws {
        let d = try suite()
        d.set(root.appendingPathComponent("elsewhere").path, forKey: FleetConfigLocation.defaultsKey)
        let row = SettingsCopy.checkoutRow(FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home),
                                           engineRoot: nil)
        XCTAssertEqual(row.sourceLine, SettingsCopy.chosen)
        XCTAssertTrue(row.hasChoice)
        // Use Default saves an empty string, which every reader treats as nothing saved.
        d.set("", forKey: FleetConfigLocation.defaultsKey)
        XCTAssertEqual(FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home).source, .standard)
    }

    func testCheckoutRowNamesTheChecksFolderWhenItDiffers() throws {
        let resolution = FleetConfigLocation.resolveWithSource(defaults: try suite(), environment: [:], home: home)
        let other = URL(fileURLWithPath: "/Applications/Elsewhere")
        XCTAssertEqual(SettingsCopy.checkoutRow(resolution, engineRoot: other).note,
                       SettingsCopy.checksElsewhere("/Applications/Elsewhere"))
        XCTAssertNil(SettingsCopy.checkoutRow(resolution, engineRoot: resolution.location.repoRoot).note)
    }

    /// The checks' folder reached through a symlink, or with a trailing slash, is this folder.
    func testCheckoutRowCallsTheSameFolderSpelledAnotherWayTheSame() throws {
        let real = root.appendingPathComponent("checkout")
        try FileManager.default.createDirectory(at: real, withIntermediateDirectories: true)
        let link = root.appendingPathComponent("linked-checkout")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real)
        let d = try suite()
        d.set(real.path, forKey: FleetConfigLocation.defaultsKey)
        let viaReal = FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home)
        for engine in [link, URL(fileURLWithPath: real.path + "/")] {
            XCTAssertNil(SettingsCopy.checkoutRow(viaReal, engineRoot: engine).note, engine.path)
        }
        d.set(link.path, forKey: FleetConfigLocation.defaultsKey)
        let viaLink = FleetConfigLocation.resolveWithSource(defaults: d, environment: [:], home: home)
        XCTAssertNil(SettingsCopy.checkoutRow(viaLink, engineRoot: real).note)
    }

    func testCheckoutFromTheEnvironmentSaysItWasSetWhenMusterWasOpened() throws {
        let row = SettingsCopy.checkoutRow(
            FleetConfigLocation.resolveWithSource(defaults: try suite(),
                                                  environment: [FleetConfigLocation.environmentKey: "/tmp/env-root"],
                                                  home: home),
            engineRoot: nil)
        XCTAssertEqual(row.sourceLine, SettingsCopy.opened)
        XCTAssertFalse(row.hasChoice)
        XCTAssertEqual(row.path, "/tmp/env-root")
    }

    // MARK: The fleet scripts

    func testAChosenScriptsFolderWithoutScriptsFallsBackAndSaysSo() throws {
        try touch("Code/AI-Fleet-Coordinator/scripts/fleet_view/__main__.py", in: home)
        let d = try suite()
        d.set(root.appendingPathComponent("empty").path, forKey: MusterSettings.scriptsDirectoryDefaultsKey)
        let r = FleetViewService.resolveScripts(environment: [:], defaults: d, home: home.path)
        XCTAssertEqual(r.source, .standard)
        XCTAssertTrue(r.chosenButUnused)
        XCTAssertEqual(SettingsCopy.scriptsRow(r).sourceLine, SettingsCopy.chosenSkipped)
        XCTAssertTrue(SettingsCopy.scriptsRow(r).hasChoice)
        XCTAssertNil(SettingsCopy.scriptsRow(r).problem)
    }

    func testTheEnvironmentWinsAndTheChosenFolderIsSaidToBeUnused() throws {
        let envScripts = root.appendingPathComponent("env-scripts")
        let chosenScripts = root.appendingPathComponent("chosen-scripts")
        try touch("fleet_view/__main__.py", in: envScripts)
        try touch("fleet_view/__main__.py", in: chosenScripts)
        let d = try suite()
        d.set(chosenScripts.path, forKey: MusterSettings.scriptsDirectoryDefaultsKey)
        let r = FleetViewService.resolveScripts(
            environment: [MusterSettings.scriptsDirectoryEnvironmentKey: envScripts.path], defaults: d, home: home.path)
        XCTAssertEqual(r.directory?.path, envScripts.path)
        XCTAssertEqual(r.source, .environment)
        XCTAssertEqual(SettingsCopy.scriptsRow(r).sourceLine, SettingsCopy.openedOverChoice)
        let alone = FleetViewService.resolveScripts(environment: [:], defaults: d, home: home.path)
        XCTAssertEqual(alone.source, .chosen)
        XCTAssertEqual(alone.directory?.path, chosenScripts.path)
        XCTAssertFalse(alone.chosenButUnused)
        XCTAssertEqual(SettingsCopy.scriptsRow(alone).sourceLine, SettingsCopy.chosen)
    }

    /// The same folder spelled another way, with a trailing slash or through a symlink, is the
    /// folder chosen here, not a different one.
    func testTheChosenFolderSpelledAnotherWayIsNotCalledUnused() throws {
        let chosenScripts = root.appendingPathComponent("chosen-scripts")
        try touch("fleet_view/__main__.py", in: chosenScripts)
        let link = root.appendingPathComponent("linked-scripts")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: chosenScripts)
        let d = try suite()
        d.set(chosenScripts.path, forKey: MusterSettings.scriptsDirectoryDefaultsKey)
        for spelling in [chosenScripts.path + "/", link.path] {
            let r = FleetViewService.resolveScripts(
                environment: [MusterSettings.scriptsDirectoryEnvironmentKey: spelling], defaults: d, home: home.path)
            XCTAssertEqual(r.source, .environment, spelling)
            XCTAssertFalse(r.chosenButUnused, spelling)
            XCTAssertEqual(SettingsCopy.scriptsRow(r).sourceLine, SettingsCopy.openedSameAsChoice, spelling)
        }
    }

    func testNoScriptsAnywhereIsNotFoundWithAProblem() throws {
        let r = FleetViewService.resolveScripts(environment: [:], defaults: try suite(), home: home.path)
        XCTAssertNil(r.directory)
        XCTAssertNil(r.source)
        XCTAssertFalse(r.environmentSkipped)
        let row = SettingsCopy.scriptsRow(r)
        XCTAssertNil(row.path)
        XCTAssertEqual(row.problem, SettingsCopy.scriptsProblem)
        XCTAssertEqual(row.sourceLine, SettingsCopy.usualMissing)
        XCTAssertFalse(row.hasChoice)
    }

    func testScriptsFromTheEnvironmentWithNoChoiceSayItWasSetWhenMusterWasOpened() throws {
        let envScripts = root.appendingPathComponent("env-scripts")
        try touch("fleet_view/__main__.py", in: envScripts)
        let r = FleetViewService.resolveScripts(
            environment: [MusterSettings.scriptsDirectoryEnvironmentKey: envScripts.path],
            defaults: try suite(), home: home.path)
        XCTAssertEqual(r.source, .environment)
        XCTAssertFalse(r.environmentSkipped)
        let row = SettingsCopy.scriptsRow(r)
        XCTAssertEqual(row.sourceLine, SettingsCopy.opened)
        XCTAssertFalse(row.hasChoice)
    }

    /// A folder set at launch without scripts is passed over, and the row says so first.
    func testAnEnvironmentFolderWithoutScriptsIsSaidToBePassedOver() throws {
        let missing = [MusterSettings.scriptsDirectoryEnvironmentKey: root.appendingPathComponent("no-scripts").path]
        let chosenScripts = root.appendingPathComponent("chosen-scripts")
        try touch("fleet_view/__main__.py", in: chosenScripts)
        let d = try suite()
        d.set(chosenScripts.path, forKey: MusterSettings.scriptsDirectoryDefaultsKey)

        // With a chosen folder that holds the scripts.
        let withChoice = FleetViewService.resolveScripts(environment: missing, defaults: d, home: home.path)
        XCTAssertEqual(withChoice.source, .chosen)
        XCTAssertTrue(withChoice.environmentSkipped)
        XCTAssertEqual(SettingsCopy.scriptsRow(withChoice).sourceLine,
                       SettingsCopy.join(SettingsCopy.openedSkipped, SettingsCopy.chosen))

        // With no choice, and the usual place holding the scripts.
        try touch("Code/AI-Fleet-Coordinator/scripts/fleet_view/__main__.py", in: home)
        let noChoice = FleetViewService.resolveScripts(environment: missing, defaults: try suite(), home: home.path)
        XCTAssertEqual(noChoice.source, .standard)
        XCTAssertTrue(noChoice.environmentSkipped)
        XCTAssertEqual(SettingsCopy.scriptsRow(noChoice).sourceLine,
                       SettingsCopy.join(SettingsCopy.openedSkipped, SettingsCopy.usual))
    }

    /// A blank or whitespace-only value set at launch counts as unset, so no passed-over line shows.
    func testABlankEnvironmentFolderCountsAsUnset() throws {
        try touch("Code/AI-Fleet-Coordinator/scripts/fleet_view/__main__.py", in: home)
        for blank in ["", "   ", "\t"] {
            let r = FleetViewService.resolveScripts(
                environment: [MusterSettings.scriptsDirectoryEnvironmentKey: blank],
                defaults: try suite(), home: home.path)
            XCTAssertEqual(r.source, .standard, "[\(blank)]")
            XCTAssertFalse(r.environmentSkipped, "[\(blank)]")
            XCTAssertEqual(SettingsCopy.scriptsRow(r).sourceLine, SettingsCopy.usual, "[\(blank)]")
        }
    }

    func testAnEnvironmentFolderWithoutScriptsAndNothingElseFound() throws {
        let r = FleetViewService.resolveScripts(
            environment: [MusterSettings.scriptsDirectoryEnvironmentKey: root.appendingPathComponent("no-scripts").path],
            defaults: try suite(), home: home.path)
        XCTAssertNil(r.directory)
        XCTAssertTrue(r.environmentSkipped)
        let row = SettingsCopy.scriptsRow(r)
        XCTAssertEqual(row.sourceLine, SettingsCopy.join(SettingsCopy.openedSkipped, SettingsCopy.usualMissing))
        XCTAssertEqual(row.problem, SettingsCopy.scriptsProblem)
    }

    /// Choosing the usual place itself is still a choice:  the row says so, and Use Default works.
    func testAChosenFolderThatIsTheUsualPlaceIsCalledChosen() throws {
        let usual = try touch("Code/AI-Fleet-Coordinator/scripts/fleet_view/__main__.py", in: home)
            .deletingLastPathComponent().deletingLastPathComponent()
        let d = try suite()
        d.set(usual.path, forKey: MusterSettings.scriptsDirectoryDefaultsKey)
        let r = FleetViewService.resolveScripts(environment: [:], defaults: d, home: home.path)
        XCTAssertEqual(r.source, .chosen)
        XCTAssertFalse(r.chosenButUnused)
        let row = SettingsCopy.scriptsRow(r)
        XCTAssertEqual(row.sourceLine, SettingsCopy.chosen)
        XCTAssertTrue(row.hasChoice)
    }

    // MARK: The Zulip seat

    /// The note shows only when a seat was set at launch and differs from the one chosen here.
    func testTheZulipNoteShowsOnlyForADifferentSeatSetAtLaunch() {
        XCTAssertNil(SettingsCopy.zulipNote(environmentSeat: nil, chosen: "CLAUDE"))
        XCTAssertNil(SettingsCopy.zulipNote(environmentSeat: "", chosen: "CLAUDE"))
        XCTAssertNil(SettingsCopy.zulipNote(environmentSeat: "  ", chosen: "CLAUDE"))
        XCTAssertNil(SettingsCopy.zulipNote(environmentSeat: "CLAUDE", chosen: "CLAUDE"))
        XCTAssertNil(SettingsCopy.zulipNote(environmentSeat: " claude ", chosen: "CLAUDE"))
        XCTAssertEqual(SettingsCopy.zulipNote(environmentSeat: " MM ", chosen: "CLAUDE"),
                       SettingsCopy.zulipOverridden("MM"))
    }

    // MARK: The words

    /// Owner copy rules:  no internal key names outside tooltips, and a real no-break space
    /// plus a space between sentences.
    func testTheWindowsWordsUseNoKeyNamesAndTheSentenceGap() throws {
        let visible = [SettingsCopy.chosen, SettingsCopy.opened, SettingsCopy.openedOverChoice,
                       SettingsCopy.openedSameAsChoice, SettingsCopy.openedSkipped, SettingsCopy.usual,
                       SettingsCopy.chosenSkipped, SettingsCopy.chosenMissing, SettingsCopy.usualMissing,
                       SettingsCopy.checkoutHelper, SettingsCopy.checkoutProblem, SettingsCopy.checksElsewhere("/x/y"),
                       SettingsCopy.scriptsHelper, SettingsCopy.scriptsProblem, SettingsCopy.zulipHelper,
                       SettingsCopy.zulipOverridden("MM"), SettingsCopy.servicesHelper, SettingsCopy.shortMenuBarHelper]
        let keys = [FleetConfigLocation.defaultsKey, FleetConfigLocation.environmentKey,
                    MusterSettings.scriptsDirectoryDefaultsKey, MusterSettings.scriptsDirectoryEnvironmentKey,
                    MusterSettings.zulipSeatDefaultsKey, MusterSettings.zulipSeatEnvironmentKey, "FleetConfigLocation"]
        let plainGap = try NSRegularExpression(pattern: "[.!?] [A-Z]")
        for text in visible {
            for key in keys { XCTAssertFalse(text.contains(key), "\(key) shows in: \(text)") }
            XCTAssertFalse(text.contains(".  "), "two plain spaces in: \(text)")
            XCTAssertNil(plainGap.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
                         "a plain space between sentences in: \(text)")
        }
        // The tooltips are where the key names live.
        XCTAssertTrue(SettingsCopy.checkoutTooltip.contains(FleetConfigLocation.defaultsKey))
        XCTAssertTrue(SettingsCopy.scriptsTooltip.contains(MusterSettings.scriptsDirectoryEnvironmentKey))
    }

    /// The three errors that send the owner to Settings name the tab the window really has.
    func testTheErrorsNameTheSettingsTheWindowShows() {
        let engine = FleetConfigCLIError.engineNotFound("/x/scripts/fleet_config/__main__.py").errorDescription ?? ""
        let helper = OpsControlError.helperNotFound("/x/scripts/fleet-ops-control.py").errorDescription ?? ""
        let scripts = FleetViewService.ServiceError.scriptsNotFound.errorDescription ?? ""
        for text in [engine, helper, scripts] {
            XCTAssertTrue(text.contains("Settings, open Folders"), text)
            XCTAssertFalse(text.contains(FleetConfigLocation.defaultsKey), text)
            XCTAssertFalse(text.contains(FleetConfigLocation.environmentKey), text)
        }
    }
}
