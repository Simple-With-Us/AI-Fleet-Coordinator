import AppKit

/// Keeps the Dock icon on the artwork for the mode Muster is showing.
///
/// The icon follows the saved theme, and when that is System it follows macOS.  A window's
/// `preferredColorScheme` sets that window's appearance and leaves the application's alone,
/// so `NSApp.effectiveAppearance` still answers for the system when another theme is saved.
///
/// macOS shows the icon in the app bundle whenever Muster is not running, in Finder and in
/// a Dock tile, and that one cannot change with the mode.
final class AppIconFollower: NSObject, NSApplicationDelegate {
    private var appearanceObservation: NSKeyValueObservation?
    private var defaultsObserver: NSObjectProtocol?
    private var shown: AppIconVariant?

    func applicationDidFinishLaunching(_ notification: Notification) {
        update()
        appearanceObservation = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            guard let self else { return }
            Task { @MainActor in self.update() }
        }
        defaultsObserver = NotificationCenter.default.addObserver(
            forName: UserDefaults.didChangeNotification,
            object: UserDefaults.standard,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in self.update() }
        }
    }

    /// A server action that was sent and has not answered is the one thing a quit can lose,
    /// so Muster asks first.  The question lives with the plan sheet,  in `OpsQuitGuard`.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let quit = MainActor.assumeIsolated { OpsQuitGuard.confirmQuit() }
        return quit ? .terminateNow : .terminateCancel
    }

    private func update() {
        let systemIsDark = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let wanted = ThemeChoice.saved(in: .standard).appIcon(systemIsDark: systemIsDark)
        guard wanted != shown, let image = NSImage(named: wanted.imageName) else { return }
        NSApp.applicationIconImage = image
        shown = wanted
    }
}
