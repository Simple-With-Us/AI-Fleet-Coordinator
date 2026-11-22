import Combine
import Foundation

/// The model behind the Services panel's Fleet Processes And Ports section.
///
/// The census is the one `FleetProcessMonitor` takes:  every BotFleet process on
/// the Mac,  the ones nothing is driving any more,  and the TCP ports the fleet
/// holds.  Muster's service rows cannot show any of this,  because they probe
/// each service through its owning manager,  and a helper whose manager has
/// gone is exactly the one no manager reports.
///
/// Reading runs off the main actor,  because it shells out to `ps` and `lsof`.
/// Stopping a process and freeing a port go through the monitor's own guards:
/// PID 1 and this app are refused,  and a port is only freed when one of the
/// fleet's own processes holds it.
@MainActor
final class FleetProcessModel: ObservableObject {
    @Published private(set) var snapshot: FleetProcessSnapshot = .empty
    @Published private(set) var reading = false
    @Published private(set) var hasRead = false
    /// The reason the last change did not work,  in words for the panel.
    @Published private(set) var problem: String?

    private let monitor: FleetProcessMonitor

    init(monitor: FleetProcessMonitor = FleetProcessMonitor()) {
        self.monitor = monitor
    }

    /// The fleet processes that nothing is driving any more.
    var orphans: [FleetProcess] { snapshot.orphans }

    /// Reads the census once,  for the panel's first appearance.  Later reads
    /// are the Check Processes button's,  so returning to the panel does not
    /// run `ps` and `lsof` again for a read it already holds.
    func refreshIfNeeded() async {
        guard !hasRead else { return }
        await refresh()
    }

    /// Reads the census now,  at the owner's asking,  so any problem on show
    /// is the last change's and a fresh read clears it.
    func checkProcesses() async {
        problem = nil
        await refresh()
    }

    /// Reads the census now.  A read does not clear a problem:  the change
    /// actions refresh after acting,  and their outcome has to survive that
    /// refresh to be read.
    func refresh() async {
        guard !reading else { return }
        reading = true
        let monitor = self.monitor
        let taken = await Task.detached(priority: .userInitiated) { monitor.snapshot() }.value
        snapshot = taken
        hasRead = true
        reading = false
    }

    /// Stops one fleet process,  then reads the census again so the panel shows
    /// what the system says now rather than what it said before.
    ///
    /// The census on screen can be older than the machine:  a PID can be
    /// reused between the read and the click.  The stop therefore takes a
    /// fresh census first and only stops the PID when it still holds the
    /// same fleet process the row showed — the same re-check `freePort`
    /// makes before it stops a port's holder.
    func stop(_ process: FleetProcess) async {
        guard !reading else { return }
        reading = true
        problem = nil
        let monitor = self.monitor
        let pid = process.pid
        let name = process.name
        let command = process.command
        let failure = await Task.detached(priority: .userInitiated) { () -> String? in
            let fresh = monitor.snapshot()
            guard let current = fresh.processes.first(where: { $0.pid == pid }),
                  current.name == name, current.command == command else {
                return "That process is no longer running as shown, so nothing was stopped."
            }
            do {
                try monitor.terminate(pid: pid)
                return nil
            } catch let error as FleetProcessError {
                return error.errorDescription ?? "That process could not be stopped."
            } catch {
                return "That process could not be stopped."
            }
        }.value
        reading = false
        await refresh()
        problem = failure
    }

    /// Frees one port by stopping the fleet process that holds it,  then reads
    /// the census again.
    func freePort(_ listener: PortListener) async {
        guard !reading else { return }
        reading = true
        problem = nil
        let monitor = self.monitor
        let port = listener.port
        let failure = await Task.detached(priority: .userInitiated) { () -> String? in
            do {
                _ = try monitor.freePort(port)
                return nil
            } catch let error as FleetProcessError {
                return error.errorDescription ?? "That port could not be freed."
            } catch {
                return "That port could not be freed."
            }
        }.value
        reading = false
        await refresh()
        problem = failure
    }
}
