import Foundation

/// The theme the window follows.
enum ThemeChoice: String, CaseIterable, Identifiable {
    case system
    case light
    case dark

    var id: String { rawValue }

    var title: String {
        switch self {
        case .system: return "System"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }

    /// The saved theme.  Nothing saved, or a value this build does not know, is System,
    /// the same answer the windows' `@AppStorage` gives.
    static func saved(in defaults: UserDefaults) -> ThemeChoice {
        defaults.string(forKey: MusterSettings.themeDefaultsKey).flatMap(ThemeChoice.init(rawValue:)) ?? .system
    }

    /// The icon that matches this theme:  a theme the owner chose wins over the system's mode.
    func appIcon(systemIsDark: Bool) -> AppIconVariant {
        switch self {
        case .system: return systemIsDark ? .dark : .light
        case .light: return .light
        case .dark: return .dark
        }
    }
}

/// The two pieces of artwork in the asset catalog.
enum AppIconVariant: Equatable {
    case light
    case dark

    var imageName: String {
        switch self {
        case .light: return "MusterIconLight"
        case .dark: return "MusterIconDark"
        }
    }
}
