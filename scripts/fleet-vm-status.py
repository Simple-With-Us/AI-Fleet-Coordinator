#!/usr/bin/env python3
"""fleet-vm-status:  what one fleet VM is doing,  in one private JSON report.

  python3 scripts/fleet-vm-status.py [--json] [--home PATH] [--code-root PATH]
                                     [--output PATH]

The default run writes `~/.local/state/fleet-tools/vm-status.json` (mode 0600,
written atomically),  the same shape of file `fleet-server-status.py` writes on
the server.  `--json` prints the report to stdout and writes nothing,  so an
SSH forced command can pull it without a shell on the VM.

What it reads,  and what it refuses to read:

  * A process is identified by its executable:  `/proc/<pid>/exe` and
    `/proc/<pid>/comm`.  Its argv and its environment are never read,  and only
    allowlisted executable names are reported at all.
  * Harness session folders are counted,  never opened:  the session-file
    count and the newest mtime come from directory entries and `stat`,  so no
    prompt and no transcript content can reach the report.
  * A checkout is read with local git plumbing only (`rev-parse`, `status`,
    `rev-list`, `branch -r --contains`):  no fetch,  no network,  and
    `--no-optional-locks` so the read never writes the index.
  * The host label comes from `FLEET_VM_HOST_LABEL` or the alias file
    `~/.config/fleet-tools/vm-host-label`.  A raw IP address is refused as a
    label,  and without either source the label is omitted.

A measurement that cannot be taken is omitted,  never reported as a zero:  a
VM with no `/proc` carries no processes section,  and a disk that cannot be
`statvfs`-ed is left out.  A section that was measured and found empty is an
empty list,  which is a different claim.

Non-goals:  no lettering,  no wakes,  no control,  no Zulip posting,  and no
prompts,  tokens,  argv or env values anywhere in the report.

The script writes one file and changes nothing else.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = "fleet-vm-status"
SCHEMA_VERSION = 1

ALLOWED_EXECUTABLES = frozenset(
    ("muse", "mcode", "minimax", "grok", "codex", "claude", "opencode", "node")
)

# The harness session folders,  relative to home.  `muse` keeps its sessions
# under the XDG data folder;  the others keep theirs in their dot-folder.
HARNESS_DIRS = {
    "muse": Path(".local/share/muse/sessions"),
    "claude": Path(".claude"),
    "codex": Path(".codex"),
    "grok": Path(".grok"),
}
SESSION_FILE_CAP = 10000

HOST_LABEL_FILE = Path(".config/fleet-tools/vm-host-label")


# MARK: - Helpers


def utc_now() -> str:
    """The time this report was made,  in the spelling the doctor uses."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_timestamp(value: float) -> str:
    moment = datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(command, timeout=30, env=None):
    """Run a command and return its stdout,  or None when it could not be run."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def read_small(path, limit=1024 * 1024):
    """The text of a small file under `/proc`,  or None when it cannot be read."""
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


# MARK: - Identity


def _clean_label(text):
    """A host label,  or None when the text is empty or a raw IP address."""
    value = (text or "").strip().splitlines()
    if not value:
        return None
    label = value[0].strip()
    if not label or len(label) > 100 or any(ch.isspace() for ch in label):
        return None
    try:
        ipaddress.ip_address(label)
        return None
    except ValueError:
        return label


def host_label(home, env):
    """The VM's fleet label:  the env first,  then the alias file,  else None."""
    label = _clean_label(env.get("FLEET_VM_HOST_LABEL"))
    if label is not None:
        return label
    if env.get("FLEET_VM_HOST_LABEL"):
        return None  # an explicit label that is unusable never falls back silently
    try:
        text = (Path(home) / HOST_LABEL_FILE).read_text(errors="replace")
    except OSError:
        return None
    return _clean_label(text)


def seat_list(env):
    """The seats that live on this VM,  from `FLEET_VM_SEATS`,  or None."""
    raw = env.get("FLEET_VM_SEATS")
    if raw is None:
        return None
    seats = []
    for part in raw.split(","):
        seat = part.strip()
        if seat and seat not in seats:
            seats.append(seat)
    return seats or None


def resolve_code_root(home, env, explicit=None):
    """Where the checkouts live:  the flag,  then the env,  then ~/Code."""
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = env.get("FLEET_VM_CODE_ROOT")
    if from_env:
        return Path(from_env).expanduser()
    default = Path(home) / "Code"
    return default if default.is_dir() else None


# MARK: - The host


def load_average(proc=Path("/proc")):
    """The 1,  5 and 15 minute load,  or None when `/proc/loadavg` cannot be read."""
    text = read_small(Path(proc) / "loadavg")
    if not text:
        return None
    fields = text.split()
    if len(fields) < 3:
        return None
    try:
        return [round(float(field), 2) for field in fields[:3]]
    except ValueError:
        return None


def meminfo(proc=Path("/proc")):
    """`/proc/meminfo` in bytes,  by field name.  A line that does not parse is skipped."""
    text = read_small(Path(proc) / "meminfo")
    values = {}
    for line in (text or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not name or not parts:
            continue
        try:
            number = int(parts[0])
        except ValueError:
            continue
        if number < 0:
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        values[name.strip()] = number * 1024 if unit == "kb" else number
    return values


def uptime_seconds(proc=Path("/proc")):
    """How long the host has been up,  in whole seconds."""
    text = read_small(Path(proc) / "uptime")
    if not text or not text.split():
        return None
    try:
        value = float(text.split()[0])
    except ValueError:
        return None
    return int(value) if value >= 0 else None


def disk_usage(mount, statvfs=os.statvfs):
    """The size of one path,  or None when it cannot be measured.

    `freeBytes` is what an ordinary user can still write,  which is what fills up.
    """
    try:
        info = statvfs(mount)
    except OSError:
        return None
    size = info.f_frsize or info.f_bsize
    if not size or info.f_blocks <= 0:
        return None
    return {
        "mount": str(mount),
        "totalBytes": info.f_blocks * size,
        "usedBytes": max(0, (info.f_blocks - info.f_bfree) * size),
        "freeBytes": max(0, info.f_bavail * size),
    }


def collect_disks(root_mount, code_root, statvfs=os.statvfs):
    """The disks for `/` and the code root.  An unmeasurable one is left out."""
    disks = []
    seen = set()
    for path in (root_mount, code_root):
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))
        usage = disk_usage(path, statvfs)
        if usage is not None:
            disks.append(usage)
    return disks


def collect_host_metrics(proc=Path("/proc"), cpu_count=os.cpu_count, getloadavg=None):
    """Load,  cpus,  memory,  swap and uptime.  Each unmeasurable one is left out."""
    if getloadavg is None:
        getloadavg = getattr(os, "getloadavg", None)
    metrics = {}
    load = load_average(proc)
    if load is None and getloadavg is not None:
        try:
            load = [round(float(value), 2) for value in getloadavg()]
        except (OSError, TypeError, ValueError):
            load = None
    if load is not None and len(load) >= 3:
        metrics["load1"], metrics["load5"], metrics["load15"] = load[:3]
    try:
        cpus = cpu_count()
    except Exception:  # noqa: BLE001 - an unmeasurable cpu count is omitted
        cpus = None
    if isinstance(cpus, int) and cpus > 0:
        metrics["cpuCount"] = cpus
    memory = meminfo(proc)
    if "MemTotal" in memory and "MemAvailable" in memory:
        metrics["memory"] = {
            "totalBytes": memory["MemTotal"],
            "availableBytes": memory["MemAvailable"],
        }
    if "SwapTotal" in memory and "SwapFree" in memory:
        metrics["swap"] = {
            "totalBytes": memory["SwapTotal"],
            "freeBytes": memory["SwapFree"],
        }
    uptime = uptime_seconds(proc)
    if uptime is not None:
        metrics["uptimeSec"] = uptime
    return metrics


# MARK: - Processes


def process_name(pid, proc=Path("/proc")):
    """The basename of a process's executable,  and nothing else.

    `/proc/<pid>/exe` is the executable identity;  the argv of the process,
    which can hold a token,  is never read.
    """
    proc = Path(proc)
    try:
        target = os.readlink(proc / str(pid) / "exe")
        name = Path(target.removesuffix(" (deleted)")).name
        if name:
            return name
    except OSError:
        pass
    try:
        return (proc / str(pid) / "comm").read_text().strip() or None
    except (OSError, ValueError):
        return None


def process_start_ticks(pid, proc=Path("/proc")):
    """When a process started,  in clock ticks since boot.

    `/proc/<pid>/stat` is read after the last `)`,  so the command name cannot
    shift the fields.  Field 22 (starttime) is index 19 of the rest.
    """
    try:
        raw = (Path(proc) / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1 :].split()
    if len(rest) < 20:
        return None
    try:
        return int(rest[19])
    except ValueError:
        return None


def boot_time(proc=Path("/proc")):
    """The boot time from `/proc/stat` (`btime`),  or None."""
    text = read_small(Path(proc) / "stat")
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "btime":
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def collect_processes(proc=Path("/proc"), clock_ticks=None):
    """Allowlisted executables grouped by name:  a count and the oldest start.

    Returns None when `/proc` cannot be listed at all,  so the section is
    omitted rather than reported empty.  A name that is not allowlisted never
    appears,  whatever it is doing.
    """
    proc = Path(proc)
    try:
        pids = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    if clock_ticks is None:
        try:
            clock_ticks = os.sysconf("SC_CLK_TCK") or 100
        except (ValueError, OSError, AttributeError):
            clock_ticks = 100
    booted = boot_time(proc)
    grouped = {}
    for pid in sorted(pids):
        name = process_name(pid, proc)
        if name not in ALLOWED_EXECUTABLES:
            continue
        row = grouped.setdefault(name, {"name": name, "count": 0})
        row["count"] += 1
        ticks = process_start_ticks(pid, proc)
        if ticks is not None and booted is not None and clock_ticks > 0:
            started = booted + ticks // clock_ticks
            if "_oldest" not in row:
                row["_oldest"] = started
            else:
                row["_oldest"] = min(row["_oldest"], started)
    rows = []
    for name in sorted(grouped):
        row = grouped[name]
        oldest = row.pop("_oldest", None)
        if oldest is not None:
            try:
                row["oldestStartedAt"] = iso_from_timestamp(oldest)
            except (OverflowError, OSError, ValueError):
                pass
        rows.append(row)
    return rows


# MARK: - Harness sessions


def _is_session_file(name):
    lowered = name.lower()
    return lowered.endswith(".jsonl") or "session" in lowered


def collect_harness_sessions(home):
    """Per harness folder:  the session-file count and the newest mtime.

    A session file is a regular file whose name ends `.jsonl` or contains
    `session`.  Files are `stat`-ed,  never opened,  and symlinks are not
    followed.  A harness folder that is not there is omitted;  one that is
    there with no session files reports a measured zero with no mtime.
    """
    found = {}
    for harness, relative in HARNESS_DIRS.items():
        folder = Path(home) / relative
        if folder.is_symlink() or not folder.is_dir():
            continue
        count = 0
        newest = None
        for directory, dirnames, filenames in os.walk(folder):
            if count >= SESSION_FILE_CAP:
                break
            dirnames[:] = [name for name in dirnames if not (Path(directory) / name).is_symlink()]
            for filename in filenames:
                if count >= SESSION_FILE_CAP:
                    dirnames[:] = []
                    break
                if not _is_session_file(filename):
                    continue
                path = Path(directory) / filename
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                count += 1
                newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
        row = {"sessionFileCount": count}
        if newest is not None:
            try:
                row["newestMtime"] = iso_from_timestamp(newest)
            except (OverflowError, OSError, ValueError):
                pass
        found[harness] = row
    return found or None


# MARK: - Checkouts


def _git(repo, args, run_command=run, timeout=15):
    """One local git read,  with no optional locks so it never writes.

    Repo config is hostile input on a timer:  fsmonitor and hooks are
    neutralized on the command line and global/system config is isolated,
    so a cloned checkout cannot execute anything through a status read.
    """
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "safe.directory=*",
        "-C",
        str(repo),
    ] + list(args)
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return run_command(command, timeout=timeout, env=env)


def _checkout_unpushed(repo, run_command):
    """Whether HEAD holds work no local remote-tracking ref has,  or None.

    Local refs only:  the upstream when one is configured,  else any
    remote-tracking branch that contains HEAD.  Nothing is fetched.
    """
    upstream = _git(repo, ["rev-parse", "--abbrev-ref", "@{upstream}"], run_command)
    if upstream and upstream.strip():
        ahead = _git(repo, ["rev-list", "--count", "@{upstream}..HEAD"], run_command)
        if ahead is None:
            return None
        try:
            return int(ahead.strip()) > 0
        except ValueError:
            return None
    containing = _git(repo, ["branch", "-r", "--contains", "HEAD"], run_command)
    if containing is None:
        return None
    return not containing.strip()


def collect_checkout(repo, run_command=run):
    """One checkout:  name,  branch,  head,  dirty counts and unpushed."""
    repo = Path(repo)
    row = {"name": repo.name}
    branch = _git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], run_command)
    if not (branch and branch.strip()):
        branch = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], run_command)
    if branch and branch.strip() and branch.strip() != "HEAD":
        row["branch"] = branch.strip()
    head = _git(repo, ["rev-parse", "--short", "HEAD"], run_command)
    has_head = bool(head and head.strip())
    if has_head:
        row["headShort"] = head.strip()
    status = _git(repo, ["status", "--porcelain=v1", "--untracked-files=normal"], run_command)
    if status is not None:
        tracked = 0
        untracked = 0
        for line in status.splitlines():
            if line.startswith("?? "):
                untracked += 1
            elif line.strip():
                tracked += 1
        row["dirtyTracked"] = tracked
        row["dirtyUntracked"] = untracked
    if has_head:
        unpushed = _checkout_unpushed(repo, run_command)
        if unpushed is not None:
            row["unpushed"] = unpushed
    return row


def collect_checkouts(code_root, run_command=run):
    """Every git repo directly under the code root,  or None without one."""
    if code_root is None:
        return None
    code_root = Path(code_root)
    if not code_root.is_dir():
        return None
    rows = []
    try:
        children = sorted(code_root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or child.is_symlink():
            continue
        if not (child / ".git").exists():
            continue
        rows.append(collect_checkout(child, run_command))
    return rows


# MARK: - The report


def build_report(
    home,
    code_root,
    proc=Path("/proc"),
    env=None,
    run_command=run,
    statvfs=os.statvfs,
    cpu_count=os.cpu_count,
    clock_ticks=None,
):
    """The report,  as Muster will read it.  Unmeasured sections are omitted."""
    home = Path(home)
    env = os.environ if env is None else env
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "generatedAt": utc_now(),
    }
    label = host_label(home, env)
    if label is not None:
        document["hostLabel"] = label
    seats = seat_list(env)
    if seats is not None:
        document["seats"] = seats
    document.update(collect_host_metrics(proc=proc, cpu_count=cpu_count))
    disks = collect_disks("/", code_root, statvfs=statvfs)
    if disks:
        document["disks"] = disks
    processes = collect_processes(proc=proc, clock_ticks=clock_ticks)
    if processes is not None:
        document["processes"] = processes
    sessions = collect_harness_sessions(home)
    if sessions is not None:
        document["harnessSessions"] = sessions
    checkouts = collect_checkouts(code_root, run_command=run_command)
    if checkouts is not None:
        document["checkouts"] = checkouts
    return document


def write_report(path, document):
    """Atomic and private:  a 0600 temporary file in the same folder,  fsynced,
    then renamed over.  The folder is created 0700 when it is absent."""
    path = Path(path)
    if path.parent.is_symlink():
        raise ValueError("report folder is a symlink: %s" % path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=".vm-status.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as output:
            json.dump(document, output, indent=2, sort_keys=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    # The rename has already replaced the report atomically; syncing the folder only makes
    # that rename survive a power cut. Some file systems refuse a directory fsync, and a
    # lost rename only leaves the previous report, which a reader ages out to unknown,
    # so this step is best effort (same contract as fleet-tools-doctor.write_report).
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the fleet VM status report that Muster reads."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report to stdout and write nothing (for a forced-command pull)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="the home folder the default paths are under (default: this user's home)",
    )
    parser.add_argument(
        "--code-root",
        type=Path,
        default=None,
        help="the folder the checkouts live in (default: FLEET_VM_CODE_ROOT, else ~/Code)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="where to write (default: <home>/.local/state/fleet-tools/vm-status.json)",
    )
    return parser.parse_args(argv)


def main(argv=None, proc=Path("/proc"), env=None, run_command=run):
    args = parse_args(argv)
    env = os.environ if env is None else env
    code_root = resolve_code_root(args.home, env, explicit=args.code_root)
    document = build_report(args.home, code_root, proc=proc, env=env, run_command=run_command)
    if args.json:
        json.dump(document, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    output = args.output or (
        Path(args.home) / ".local" / "state" / "fleet-tools" / "vm-status.json"
    )
    write_report(output, document)
    print(
        "wrote %s  (%d processes, %d checkouts)"
        % (
            output,
            len(document.get("processes") or []),
            len(document.get("checkouts") or []),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
