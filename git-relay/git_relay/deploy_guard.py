"""Pure decisions for a bounded existing-service transaction supervisor.

A persistent ExecStartPre guard must call this from an anchored root-owned
manifest. Only the exact live deployment executor may start an uncommitted new
image. A reboot, timed-out executor or failed exit requires stopped recovery
before the existing service can start. No authorization or systemd writes here.
"""


def start_decision(
    phase, executor, observed_executor, observed_boot, current_hashes, old_hashes, new_hashes
):
    """Return old/new/recover/refuse; caller supplies independently observed facts."""
    if phase in {"prepared", "applying", "restoring"}:
        return "recover"
    if phase == "restored":
        return "old" if current_hashes == old_hashes else "refuse"
    if phase == "committed":
        return "new" if current_hashes == new_hashes else "refuse"
    if phase == "applied":
        # PID alone is not enough: boot, start ticks, executable and cgroup
        # must all match the bounded supervisor's originally pinned identity.
        required = {"pid", "start_ticks", "exe", "cgroup", "boot"}
        if (
            not isinstance(executor, dict)
            or set(executor) != required
            or type(executor["pid"]) is not int
            or executor["pid"] <= 0
            or not all(executor[k] for k in required - {"pid"})
        ):
            return "refuse"
        if executor != observed_executor or executor["boot"] != observed_boot:
            return "recover"
        return "new" if current_hashes == new_hashes else "refuse"
    return "refuse"


def freeze_qualified(expected, before, after, task_states, children, pending, stop_job):
    """A stopped single-threaded Python relay has no active request handlers.

    Applied only AFTER freezing the exact process. The server uses a separate
    thread for every accepted request. No worker thread/no child while frozen
    establishes no provider call in progress, unlike pending journal count.
    A pre-existing stop job or replacement process refuses the operation.
    """
    return (
        isinstance(expected, dict)
        and expected == before == after
        and type(expected.get("pid")) is int
        and expected["pid"] > 0
        and task_states == {expected["pid"]: "T"}
        and children == []
        and pending == 0
        and stop_job is False
    )
