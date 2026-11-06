import AppKit
import SwiftUI

/// The window Muster is showing,  so the menu can bring it forward.
///
/// The scene stays a `WindowGroup`:  a `Window` as the main scene is reported to end the app
/// when it closes,  which would take the menu bar item with it.  New Window is gone from the
/// commands,  and this keeps the rest from opening a second copy:  `openWindow` on a
/// `WindowGroup` opens another instance each time,  so it is called only when no window is
/// on screen.
@MainActor
enum MusterWindow {
    private static weak var window: NSWindow?

    /// Remembers the window a view just moved into.
    static func track(_ newWindow: NSWindow) {
        window = newWindow
    }

    /// Brings the app and its window forward,  or opens the window when there is none.
    static func show(openWindow: OpenWindowAction) {
        NSApp.activate(ignoringOtherApps: true)
        if let window, window.isVisible || window.isMiniaturized {
            if window.isMiniaturized { window.deminiaturize(nil) }
            window.makeKeyAndOrderFront(nil)
        } else {
            openWindow(id: DriftMenuBarContent.windowID)
        }
    }

    /// Writes the saved place,  which the window follows,  and brings the window forward.
    static func show(_ destination: MusterDestination, openWindow: OpenWindowAction) {
        UserDefaults.standard.set(destination.rawValue, forKey: MusterSettings.sidebarSelectionDefaultsKey)
        show(openWindow: openWindow)
    }
}

/// Attached to the window's root,  so `MusterWindow` knows the window.
struct MusterWindowTracker: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView { TrackingView() }
    func updateNSView(_ nsView: NSView, context: Context) {}

    private final class TrackingView: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let window { MusterWindow.track(window) }
        }
    }
}
