import Foundation

// The words for the Operator's voice, whether the Operator can post to Zulip.
//
// The Mac's own eavesdropper status that used to carry it is retired:  the one wake router and
// the server's Eavesdropper answer for it now (ServerEavesdropperStatus).  What remains is the
// two pieces the Operator panel and the server rows still use:  a reason code in plain words,
// and a time on Jay's clock.

enum OperatorVoiceReading {
    /// The cause in plain words for a reason code the eavesdropper writes,  or nil for a code
    /// Muster does not know (that code stays in the tooltip,  never guessed at).
    static func cause(_ code: String) -> String? {
        switch code {
        case "operator_credential_rejected":
            return "Zulip refused the Operator's credential."
        case "operator_owner_not_pinned":
            return "The owner is not pinned in the listener's settings, so the Operator does not post."
        case "operator_unavailable":
            // The catch-all for any failed build of the Operator client:  an unreadable
            // credential, not the pinned bot, or an identity it could not confirm.
            return "The listener could not get the Operator ready to post."
        default:
            return nil
        }
    }

    /// A time on Jay's clock:  the time alone on the same day as `now`,  else the month and
    /// day with it.
    static func whenText(_ date: Date, now: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Chicago") ?? .current
        if calendar.isDate(date, inSameDayAs: now) {
            return FleetTime.clock(date)
        }
        let parts = calendar.dateComponents([.month, .day], from: date)
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let month = parts.month.map { months[max(0, min(11, $0 - 1))] } ?? ""
        return "\(month) \(parts.day ?? 0), \(FleetTime.clock(date))"
    }
}
