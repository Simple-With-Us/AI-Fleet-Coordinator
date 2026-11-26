import Foundation

/// The Notes queue as the Operator's settings and the doctor's report tell it.
///
/// The settings say what the queue is configured to do; the doctor's `notes:queue`,
/// `notes:runner` and `notes:action-log` rows say whether it is doing it.  Each is kept apart so
/// a configured-but-not-running queue is never shown as healthy, and a report that is not current
/// is never read as a pass.
struct NotesQueueHealth: Equatable {
    /// One doctor row, with the host it was read on.
    struct DoctorRow: Equatable {
        let host: String
        let status: String
        let state: String?
        let detail: String?
    }

    var enabled: Bool?
    var listenerAPI: Bool?
    var workerSeats: [String] = []
    var serverSeats: [String] = []
    var configuredSeats: Set<String> = []
    var queue: DoctorRow?
    var runner: DoctorRow?
    var actionLog: DoctorRow?
    var doctorProblem: String?
    var doctorAge: String?

    static func make(settings: [String: ServerSettingValue],
                     doctor: FleetToolsReading?) -> NotesQueueHealth {
        var health = NotesQueueHealth()
        health.enabled = settings["notes_queue.enabled"]?.boolValue
        health.listenerAPI = settings["listener_api.enabled"]?.boolValue
        health.workerSeats = ServerSeatNames.names(in: settings["notes_queue.worker_seats"])
        health.serverSeats = ServerSeatNames.names(in: settings["notes_queue.server_seats"])
        health.configuredSeats = Set(settings.keys.compactMap { key -> String? in
            let parts = key.split(separator: ".").map(String.init)
            guard parts.count >= 3, parts[0] == "seat" else { return nil }
            return parts[1]
        })
        guard let doctor else { return health }
        if let problem = doctor.problem {
            health.doctorProblem = problem
            return health
        }
        health.doctorAge = doctor.age
        guard doctor.isCurrent else {
            health.doctorProblem = "The doctor's report is not current, so queue health is not read."
            return health
        }
        let rows = doctor.hosts.flatMap { host in
            host.allRows.map { (host: host.host, row: $0) }
        }
        health.queue = row(named: "notes:queue", in: rows)
        health.runner = row(named: "notes:runner", in: rows)
        health.actionLog = row(named: "notes:action-log", in: rows)
        return health
    }

    private static func row(named check: String,
                            in rows: [(host: String, row: FleetToolsRow)]) -> DoctorRow? {
        guard let found = rows.first(where: { $0.row.check == check }) else { return nil }
        return DoctorRow(host: found.host, status: found.row.status ?? "UNKNOWN",
                         state: found.row.state, detail: found.row.detail)
    }

    /// The configured state and the doctor's reading, in the panel's own words.
    var lines: [String] {
        var lines: [String] = []
        lines.append("Queue:  " + word(enabled))
        lines.append("Listener API:  " + word(listenerAPI))
        let shared = Set(workerSeats).intersection(serverSeats).count
        lines.append("Seats:  \(workerSeats.count) through the Worker key, "
            + "\(serverSeats.count) through the Server key"
            + (shared > 0 ? ", \(shared) in both" : "") + ".")
        if let detail = queue?.detail, !detail.isEmpty {
            lines.append("Server queue:  \(detail)")
        }
        if let detail = runner?.detail, !detail.isEmpty {
            lines.append("Mac runner:  \(detail)")
        }
        if let detail = actionLog?.detail, !detail.isEmpty {
            lines.append("Action log:  \(detail)")
        }
        if let doctorProblem {
            lines.append(doctorProblem)
        } else if queue == nil, runner == nil, actionLog == nil {
            lines.append("The doctor's report holds no Notes row, so queue health is not read.")
        }
        if let doctorAge, doctorProblem == nil {
            lines.append("The doctor's report is \(doctorAge) old.")
        }
        return lines
    }

    /// What would stop the queue working, or make its state read as healthier than it is.
    var warnings: [String] {
        var warnings: [String] = []
        if enabled == true, listenerAPI != true {
            warnings.append("The Listener API is off, so the queue cannot serve a request until it is on.")
        }
        if enabled == true, workerSeats.isEmpty, serverSeats.isEmpty {
            warnings.append("Both seat lists are empty, so no seat may enqueue a request.")
        }
        if let queue, queue.status == "WARN" || queue.status == "FAIL" {
            warnings.append("The server queue reads \(queue.status)"
                + (queue.detail.map { ":  \($0)" } ?? "."))
        }
        if let runner, runner.status == "WARN" || runner.status == "FAIL" {
            warnings.append("The Mac runner reads \(runner.status)"
                + (runner.detail.map { ":  \($0)" } ?? "."))
        }
        if workerSeats.contains("OPERATOR") || serverSeats.contains("OPERATOR") {
            warnings.append("OPERATOR is a service, not a seat the queue can act for.")
        }
        return warnings
    }

    private func word(_ value: Bool?) -> String {
        switch value {
        case true?: return "On"
        case false?: return "Off"
        case nil: return "not reported"
        }
    }
}