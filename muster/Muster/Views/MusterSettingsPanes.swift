import SwiftUI
import UniformTypeIdentifiers

/// The Settings window's sizes.  Every pane is one fixed size, so the window should be the
/// same on every Mac and no taller than a short screen (not measured on a real Mac:  the
/// tests host each pane in a plain window, see MusterSettingsLayoutTests).
enum MusterSettingsLayout {
    static let paneWidth: CGFloat = 520
    /// Tall enough for the Folders pane with every warning showing and the worst-case This
    /// Build pane (606 pt on the CI runner), so no pane hides anything below the fold.
    static let paneHeight: CGFloat = 640
    /// The title bar and the tab toolbar of the Settings scene.  No test runs the scene, so
    /// this is an allowance, not a measurement (MusterSettingsLayoutTests).
    static let chromeAllowance: CGFloat = 110
    /// The shortest screen the window must fit:  1280 by 800 less the menu bar.  The Dock is
    /// left out on purpose:  with a bottom Dock on such a screen, the last lines of the
    /// worst-case Folders pane can sit under the Dock (not measured).
    static let shortestWindow: CGFloat = 768
}

/// Which folder setting a button acts on.
enum SettingsFolder { case checkout, scripts }

/// One folder setting as the window shows it.
struct FolderRowModel: Equatable {
    /// The folder Muster uses, or nil when it found none.
    var path: String?
    /// Where that folder came from, in plain words.
    var sourceLine: String
    /// Why the folder will not work, when it will not.
    var problem: String?
    /// Anything else the owner should know, such as the main window's checks using another folder.
    var note: String?
    /// True when a folder is chosen here, so Use Default has something to undo.
    var hasChoice: Bool
}

/// The words of the Settings window.  Internal key names appear only in the tooltips.
enum SettingsCopy {
    static let gap = "\u{00A0} "
    static func join(_ sentences: String...) -> String { sentences.joined(separator: gap) }

    static let notFound = "Not Found"
    static let chosen = "You chose this folder."
    static let opened = "Set when Muster was opened, because no folder is chosen here."
    static let openedOverChoice = join(
        "Set when Muster was opened, so the folder chosen here is not used.",
        "The folder chosen here is used once Muster is opened without that setting.")
    static let openedSameAsChoice = "Set when Muster was opened, and the same as the folder chosen here."
    /// Said before the row's own line when a folder set at launch had no scripts and was passed over.
    static let openedSkipped = "A folder was set when Muster was opened, but it has no fleet scripts."
    static let usual = "The usual place, because no folder is chosen here."
    static let chosenSkipped = "The folder you chose has no fleet scripts, so Muster uses the usual place."
    static let chosenMissing = "The folder you chose has no fleet scripts."
    static let usualMissing = "No folder is chosen, and the usual place has no fleet scripts."

    static let checkoutTitle = "Fleet Checkout"
    static let checkoutHelper = join(
        "Muster runs its fixes, service controls and Seat Setup from this folder.",
        "Leave it on the usual place unless your fleet checkout is somewhere else.")
    static let checkoutTooltip = join(
        "Saved as \(FleetConfigLocation.defaultsKey).",
        "\(FleetConfigLocation.environmentKey) is used only when nothing is saved.")
    static let checkoutProblem = join(
        "Muster cannot find the fleet tools in this folder.",
        "Choose the folder of the fleet checkout.")
    static func checksElsewhere(_ path: String) -> String {
        "The checks in the main window read \(path), not this folder."
    }

    static let scriptsTitle = "Fleet Scripts"
    static let scriptsHelper = join(
        "Muster reads lanes, pull requests and claims with the scripts in this folder.",
        "It is usually the scripts folder inside the fleet checkout.")
    static let scriptsTooltip = join(
        "Saved as \(MusterSettings.scriptsDirectoryDefaultsKey).",
        "\(MusterSettings.scriptsDirectoryEnvironmentKey) comes first when it holds the scripts.")
    static let scriptsProblem = join(
        "Muster cannot find the fleet scripts, so it reads no lanes, pull requests or claims.",
        "Choose the scripts folder inside the fleet checkout.")

    static let zulipHelper = join(
        "The Seats panel reads Zulip as this seat's bot.",
        "Muster only reads, and never posts.")
    static let zulipTooltip = join(
        "Saved as \(MusterSettings.zulipSeatDefaultsKey).",
        "\(MusterSettings.zulipSeatEnvironmentKey), when set, comes first.")
    static func zulipOverridden(_ seat: String) -> String {
        join("Set when Muster was opened, so Muster reads as \(seat), not the seat chosen here.",
             "The seat chosen here is used once Muster is opened without that setting.")
    }

    /// The Zulip note, shown only when a seat set at launch (`MUSTER_ZULIP_SEAT`) is not the
    /// seat chosen here.  The CLI takes seat names in any case, so case does not count.
    static func zulipNote(environmentSeat: String?, chosen: String) -> String? {
        let fromLaunch = (environmentSeat ?? "").trimmingCharacters(in: .whitespaces)
        guard !fromLaunch.isEmpty,
              fromLaunch.caseInsensitiveCompare(chosen.trimmingCharacters(in: .whitespaces)) != .orderedSame
        else { return nil }
        return zulipOverridden(fromLaunch)
    }

    static let servicesHelper = join(
        "While Muster runs, it starts a service on the Mac again when it stops or fails, waits longer after each try, and stops trying after 3.",
        "It never stops a service, and it never starts one you stopped from Muster.",
        "The Services panel says what it does for each service.")
    static let shortMenuBarHelper = "Reads M OK \u{00B7} S ? in place of Mac OK \u{00B7} Server ?, for a crowded menu bar."

    static func checkoutRow(_ resolution: FleetConfigLocation.Resolution, engineRoot: URL?,
                            fileManager: FileManager = .default) -> FolderRowModel {
        let path = resolution.location.repoRoot.standardizedFileURL.path
        let usable = (try? resolution.location.validate(fileManager: fileManager)) != nil
        let line: String
        switch resolution.source {
        case .chosen: line = chosen
        case .environment: line = opened
        case .standard: line = usual
        }
        var note: String?
        // Compared by canonical path, as the scripts row is, so a symlink or a trailing slash
        // is not called another folder.
        if let engine = engineRoot?.standardizedFileURL.path,
           FleetViewService.canonicalPath(engine) != FleetViewService.canonicalPath(path) {
            note = checksElsewhere(engine)
        }
        return FolderRowModel(path: path, sourceLine: line, problem: usable ? nil : checkoutProblem,
                              note: note, hasChoice: resolution.source == .chosen)
    }

    static func scriptsRow(_ resolution: FleetViewService.ScriptsResolution) -> FolderRowModel {
        let hasChoice = resolution.chosen != nil
        // A folder set at launch that has no scripts is passed over;  say so before the rest.
        func said(_ line: String) -> String { resolution.environmentSkipped ? join(openedSkipped, line) : line }
        guard let directory = resolution.directory, let source = resolution.source else {
            return FolderRowModel(path: nil, sourceLine: said(hasChoice ? chosenMissing : usualMissing),
                                  problem: scriptsProblem, note: nil, hasChoice: hasChoice)
        }
        let line: String
        switch (source, resolution.chosenButUnused) {
        case (.environment, true): line = openedOverChoice
        case (.environment, false): line = hasChoice ? openedSameAsChoice : opened
        case (.chosen, _): line = chosen
        case (.standard, true): line = chosenSkipped
        case (.standard, false): line = usual
        }
        return FolderRowModel(path: directory.path, sourceLine: said(line), problem: nil, note: nil, hasChoice: hasChoice)
    }
}

/// One Settings pane:  a grouped Form of the window's one fixed size.  It scrolls only if
/// its content outgrows that size.
struct MusterSettingsPane<Content: View>: View {
    private let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        Form { content }
            .formStyle(.grouped)
            .frame(width: MusterSettingsLayout.paneWidth, height: MusterSettingsLayout.paneHeight)
    }
}

/// A folder setting:  the folder in use, where it came from, and the two buttons.
struct MusterFolderSection: View {
    let title: String
    let helper: String
    let tooltip: String
    let row: FolderRowModel
    let choose: () -> Void
    let useDefault: () -> Void

    var body: some View {
        Section {
            LabeledContent("Folder") {
                Text(row.path ?? SettingsCopy.notFound)
                    .font(.system(size: 11, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .help(row.path ?? "")
            }
            Text(row.sourceLine).font(.caption).foregroundStyle(.secondary)
            if let problem = row.problem {
                Label(problem, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.orange)
            }
            if let note = row.note {
                Text(note).font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Choose Folder\u{2026}", action: choose)
                Button("Use Default", action: useDefault).disabled(!row.hasChoice)
            }
            Text(helper).font(.caption).foregroundStyle(.secondary).help(tooltip)
        } header: {
            Text(title).help(tooltip)
        }
    }
}

struct MusterFoldersPane: View {
    let checkout: FolderRowModel
    let scripts: FolderRowModel
    let choose: (SettingsFolder, String) -> Void
    let useDefault: (SettingsFolder) -> Void
    // One importer for both buttons:  a second `.fileImporter` on the same view is not
    // reliably presented.  The target is kept apart from the flag that presents it.
    @State private var picking = false
    @State private var target: SettingsFolder = .checkout

    var body: some View {
        MusterSettingsPane {
            MusterFolderSection(title: SettingsCopy.checkoutTitle, helper: SettingsCopy.checkoutHelper,
                                tooltip: SettingsCopy.checkoutTooltip, row: checkout,
                                choose: { target = .checkout; picking = true },
                                useDefault: { useDefault(.checkout) })
            MusterFolderSection(title: SettingsCopy.scriptsTitle, helper: SettingsCopy.scriptsHelper,
                                tooltip: SettingsCopy.scriptsTooltip, row: scripts,
                                choose: { target = .scripts; picking = true },
                                useDefault: { useDefault(.scripts) })
        }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.folder]) { result in
            // The app is not sandboxed (project.yml ENABLE_APP_SANDBOX: NO), so a plain path is enough.
            if case .success(let url) = result { choose(target, url.standardizedFileURL.path) }
        }
    }
}

struct MusterZulipServicesPane: View {
    @Binding var seat: String
    let seatChoices: [String]
    /// `MUSTER_ZULIP_SEAT` as Muster was opened with it, or nil;  when set, it wins over the choice.
    let environmentSeat: String?
    @Binding var keepServicesRunning: Bool

    var body: some View {
        MusterSettingsPane {
            Section {
                Picker("Read Zulip As", selection: $seat) {
                    ForEach(seatChoices, id: \.self) { Text($0).tag($0) }
                }
                if let note = SettingsCopy.zulipNote(environmentSeat: environmentSeat, chosen: seat) {
                    Label(note, systemImage: "info.circle")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text(SettingsCopy.zulipHelper).font(.caption).foregroundStyle(.secondary)
                    .help(SettingsCopy.zulipTooltip)
            } header: { Text("Zulip").help(SettingsCopy.zulipTooltip) }
            Section("Services") {
                Toggle("Keep Services Running", isOn: $keepServicesRunning)
                Text(SettingsCopy.servicesHelper)
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct MusterAppearancePane: View {
    @Binding var theme: ThemeChoice
    @Binding var shortMenuBarText: Bool

    var body: some View {
        MusterSettingsPane {
            Section("Appearance") {
                Picker("Theme", selection: $theme) {
                    ForEach(ThemeChoice.allCases) { choice in Text(choice.title).tag(choice) }
                }
                .pickerStyle(.segmented)
                Toggle("Short Menu Bar Text", isOn: $shortMenuBarText)
                Text(SettingsCopy.shortMenuBarHelper)
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
