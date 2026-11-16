import Foundation

/// The two machines Muster can act on.
///
/// Every surface takes its place words from here,  and the words are computed from the
/// value that decides the route,  so a label and the machine it names cannot disagree.
public enum FleetHost: String, CaseIterable, Sendable {
    case mac
    case server

    /// The place,  as a section of the menu names it.
    public var heading: String {
        switch self {
        case .mac: return "The Mac"
        case .server: return "The Server"
        }
    }

    /// The machine,  inside a sentence.
    public var name: String {
        switch self {
        case .mac: return "the Mac"
        case .server: return "the server"
        }
    }

    /// The place,  inside a sentence.
    public var inProse: String { "on \(name)" }

    /// The place,  on a button or a Title Case heading.
    public var onTitle: String { "On \(heading)" }

    /// The symbol that goes with the place's words,  never instead of them.
    public var systemImage: String {
        switch self {
        case .mac: return "desktopcomputer"
        case .server: return "server.rack"
        }
    }

    /// The host a report or a helper names.
    ///
    /// Only the two exact words count:  anything else names no machine this app knows.
    public init?(reportValue: String?) {
        switch reportValue {
        case "mac": self = .mac
        case "server": self = .server
        default: return nil
        }
    }
}

/// Where an action's effect lands,  which is not always where it runs:  a knob write
/// runs on the Mac and changes a value every machine reads.
public enum FleetPlace: Equatable, Sendable {
    case machine(FleetHost)
    case acrossFleet

    /// The place,  on a badge.
    public var title: String {
        switch self {
        case .machine(let host): return host.onTitle
        case .acrossFleet: return "Across The Fleet"
        }
    }

    /// The place,  inside a sentence.
    public var inProse: String {
        switch self {
        case .machine(let host): return host.inProse
        case .acrossFleet: return "for the whole fleet"
        }
    }

    /// The symbol that goes with the place's words,  never instead of them.
    public var systemImage: String {
        switch self {
        case .machine(let host): return host.systemImage
        case .acrossFleet: return "globe"
        }
    }
}
