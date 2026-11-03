import Foundation

/// Where the window can be.  The sidebar, the overview cards and the saved place all
/// name a destination, so the list of places is written once here rather than once
/// per surface.
///
/// The raw value is the saved spelling under `MusterSettings.sidebarSelectionDefaultsKey`.
/// A saved value this app no longer names reads as the overview, never as a guess at
/// what the owner meant.
enum MusterDestination: String, CaseIterable, Identifiable {
    case overview
    case complianceGrid
    case appsAndWork
    case seats
    case driftHistory
    case services
    case wakeAndListener
    case controls
    case seatDetail
    case serverStatus
    case hosts
    case operations

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: return "Overview"
        case .complianceGrid: return "Compliance Grid"
        case .appsAndWork: return "Apps & Work"
        case .seats: return "Seats"
        case .driftHistory: return "Drift History"
        case .services: return "Services"
        case .wakeAndListener: return "Wake & Listener"
        case .controls: return "Controls"
        case .seatDetail: return "Seat Detail"
        case .serverStatus: return "Server Status"
        case .hosts: return "Hosts"
        case .operations: return "Operations"
        }
    }

    var systemImage: String {
        switch self {
        case .overview: return "house"
        case .complianceGrid: return "square.grid.3x3"
        case .appsAndWork: return "list.bullet.rectangle"
        case .seats: return "person.2.badge.gearshape"
        case .driftHistory: return "exclamationmark.triangle"
        case .services: return "square.stack.3d.up"
        case .wakeAndListener: return "bell.badge"
        case .controls: return "slider.horizontal.3"
        case .seatDetail: return "bolt.shield"
        case .serverStatus: return "cpu"
        case .hosts: return "server.rack"
        case .operations: return "gearshape.2"
        }
    }

    /// The sidebar's Fleet section, in the order it reads.
    static let fleet: [MusterDestination] = [.complianceGrid, .appsAndWork, .seats, .driftHistory]
    /// The sidebar's This Mac section, in the order it reads.
    static let thisMac: [MusterDestination] = [.services, .wakeAndListener, .controls, .seatDetail]
    /// The sidebar's Server section, in the order it reads.
    static let server: [MusterDestination] = [.serverStatus, .hosts, .operations]

    /// The destination a saved spelling names, or the overview when it names nothing.
    static func restored(from saved: String?) -> MusterDestination {
        guard let saved, let destination = MusterDestination(rawValue: saved) else { return .overview }
        return destination
    }
}

/// The overview's counts, read off the last run's cells.
///
/// Every count is nil when the run could not be trusted, and nil is what keeps the
/// overview honest:  a run that failed says nothing about the seats, so a count of
/// zero would be a claim the app cannot make.  The view renders nil as unknown.
struct OverviewCounts: Equatable {
    var red: Int?
    var unknown: Int?
    var missing: Int?
    var drift: Int?
    var stale: Int?

    /// The counts the cells give, when the run could be trusted.
    ///
    /// A cell is stale when its oldest observation differs from the run's shared check
    /// time by more than the grid's own sixty-second tolerance, which is a rule
    /// served from the cache and checked apart from the rest.  With no shared check
    /// time there is nothing to be stale against, so the count stays unknown rather
    /// than reading as none.
    static func make(cells: [GridCell], trusted: Bool, commonCheckedAt: Date?) -> OverviewCounts {
        var counts = OverviewCounts()
        guard trusted else { return counts }
        counts.red = cells.filter(\.state.isRed).count
        counts.unknown = cells.filter { $0.state == .unknown }.count
        counts.missing = cells.filter { $0.state == .missing }.count
        counts.drift = cells.filter { $0.state == .drift }.count
        if let commonCheckedAt {
            counts.stale = cells.filter { cell in
                guard let checkedAt = cell.checkedAt else { return false }
                return abs(commonCheckedAt.timeIntervalSince(checkedAt)) > 60
            }.count
        }
        return counts
    }
}
