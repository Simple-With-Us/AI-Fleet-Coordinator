"""Filesystem and keychain operations for the local Muster builder.  Never launches an app."""

import datetime
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def acquire_lock(path):
    """Only an existing lock means another build holds it."""
    try:
        Path(path).mkdir()
    except FileExistsError:
        raise RuntimeError(
            "Another Muster build holds the lock; inspect it before removal."
        ) from None


def fsync_path(path):
    flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if path.is_dir() else 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root):
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_symlink():
            fsync_path(path)
    fsync_path(root)


def install_bundle(staged, installed, verify):
    """Prepare on the target filesystem, then replace with rollback on failure."""
    staged, installed = Path(staged), Path(installed)
    temporary = Path(tempfile.mkdtemp(prefix=".muster-install-", dir=installed.parent))
    prepared, previous = temporary / "new.app", temporary / "previous.app"
    saved_handlers = {}
    committed = False

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    try:
        shutil.copytree(staged, prepared, symlinks=True)
        verify(prepared)
        fsync_tree(prepared)
        fsync_path(temporary)
        for signum in (signal.SIGTERM, signal.SIGINT):
            saved_handlers[signum] = signal.signal(signum, interrupted)
        if os.path.lexists(installed):
            os.replace(installed, previous)
            fsync_path(temporary)
            fsync_path(installed.parent)
        try:
            os.replace(prepared, installed)
            fsync_path(temporary)
            fsync_path(installed.parent)
            verify(installed)
            committed = True
        except BaseException:
            # A first-time install has nothing to restore, so drop the unverified bundle.
            if os.path.lexists(installed):
                shutil.rmtree(installed)
                fsync_path(installed.parent)
            if previous.exists():
                os.replace(previous, installed)
                fsync_path(temporary)
                fsync_path(installed.parent)
            raise
    finally:
        try:
            # The signal may arrive between the first rename and the inner try.
            if previous.exists() and not os.path.lexists(installed):
                os.replace(previous, installed)
                fsync_path(temporary)
                fsync_path(installed.parent)
        finally:
            for signum, handler in saved_handlers.items():
                signal.signal(signum, handler)
            # A failed rollback must preserve the old bundle for recovery.
            if committed or not previous.exists():
                shutil.rmtree(temporary)


def installed_pids(binary):
    """Find only the installed executable, without reading any process arguments."""
    binary = Path(binary).resolve()
    found = subprocess.run(
        ["/usr/bin/pgrep", "-x", binary.name],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if found.returncode == 1:
        return []
    if found.returncode:
        raise RuntimeError("Cannot discover active Muster processes.")
    pids = found.stdout.split()
    if not pids or any(not pid.isdecimal() or int(pid) <= 0 for pid in pids):
        raise RuntimeError("Invalid Muster process discovery result.")
    matches = []
    for pid in dict.fromkeys(pids):
        try:
            inspected = subprocess.run(
                ["/usr/sbin/lsof", "-b", "-nP", "-a", "-p", pid, "-d", "txt", "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            paths = [line[1:] for line in inspected.stdout.splitlines() if line.startswith("n")]
            if inspected.returncode or not paths:
                raise RuntimeError("Cannot inspect an active Muster executable.")
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            raise RuntimeError("Cannot inspect an active Muster executable.") from None
        if any(Path(path).resolve() == binary for path in paths):
            matches.append(pid)
    return matches


SECURITY = "/usr/bin/security"
LOGIN_KEYCHAIN = "~/Library/Keychains/login.keychain-db"
SECURITY_TIMEOUT = 30

# The public certificate metadata that security prints for each identity.  No key
# material and no certificate body is ever read, printed or kept.
IDENTITY_PATTERN = re.compile(
    r'\b([A-Fa-f0-9]{40}) "(Developer ID Application: [^"\n]+ \(([A-Z0-9]{10})\))"'
)

# The owner reads this, so it names the cause and the fix and stays as it is.
KEYCHAIN_SEARCH_LIST_MESSAGE = (
    "The login keychain is not on the keychain search list,  so the private key that "
    "signs this app cannot be reached.  The certificate is there and the identity is "
    "correct;  the key is locked away.  The owner restores it with:\n"
    '  security list-keychains -s "$HOME/Library/Keychains/login.keychain-db" '
    "/Library/Keychains/System.keychain\n"
    "and by unlocking the login keychain,  which needs the owner's password."
)


class BuildFailure(Exception):
    """A named failure and the exit status the build script should use."""

    def __init__(self, message, status=1):
        super().__init__(message)
        self.status = status


def run_security(arguments, timeout=None):
    """Run one security command, and name every way it can fail.

    A crashed command is a failed command: the caller is told which command
    failed and why, and gets an exit status instead of a traceback.
    """
    command = [SECURITY] + list(arguments)
    seconds = SECURITY_TIMEOUT if timeout is None else timeout
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=seconds,
        )
    except subprocess.TimeoutExpired:
        raise BuildFailure(
            "The command 'security %s' did not answer within %s seconds, so the "
            "signing identity is unknown and the build refuses to continue."
            % (" ".join(arguments), seconds),
            124,
        ) from None
    except FileNotFoundError:
        raise BuildFailure(
            "The command %s is missing, so the keychain tools are unavailable and "
            "the signing identity is unknown." % SECURITY,
            127,
        ) from None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise BuildFailure(
            "The command 'security %s' failed with status %d%s."
            % (
                " ".join(arguments),
                exc.returncode,
                (": " + detail[0][:200]) if detail else "",
            ),
            exc.returncode or 1,
        ) from None


def find_coding_identities():
    """Every Developer ID Application identity the build can reach, by fingerprint.

    The owner's keychain search list is kept to the System keychain on purpose,
    so that a path-less lookup cannot reach the login keychain or raise an unlock
    prompt on the owner's screen.  The login keychain is therefore still named by
    path, and that by-path lookup is the difference between signing and not.  A
    build or CI keychain holds the identity instead in other setups, and only the
    search list reaches it, so both are searched and their identities are unioned.
    The same certificate can be visible through both, so identities are
    de-duplicated by their SHA-1 fingerprint.
    """
    searches = (
        (["find-identity", "-v", "-p", "codesigning"], False),
        (
            ["find-identity", "-v", "-p", "codesigning", os.path.expanduser(LOGIN_KEYCHAIN)],
            True,
        ),
    )
    found = {}
    for arguments, is_login_keychain in searches:
        try:
            stdout = run_security(arguments).stdout
        except BuildFailure:
            # A search that cannot answer is a search that found nothing.  The
            # other search still runs,  so an unreadable search-list keychain
            # does not stop the by-path lookup from reaching the identity that
            # can sign.  The by-path lookup is the one that has to succeed:
            # when it cannot,  the build cannot sign and says so.
            if is_login_keychain:
                raise
            continue
        for fingerprint, label, team in IDENTITY_PATTERN.findall(stdout):
            found.setdefault(fingerprint.upper(), (fingerprint, label, team))
    return list(found.values())


def select_signing_identity(requested=None):
    """The one identity to sign with, as a fingerprint and a team id."""
    identities = find_coding_identities()
    if requested:
        normalized = requested.replace(":", "").upper()
        identities = [
            item for item in identities if normalized == item[0].upper() or requested == item[1]
        ]
    if len(identities) != 1:
        raise BuildFailure(
            "Expected exactly one matching valid Developer ID Application identity; "
            "set MUSTER_SIGNING_IDENTITY to the certificate SHA-1 (colons optional) "
            "or the full 'Developer ID Application: <name> (<TEAM>)' label.",
            1,
        )
    fingerprint, _, team = identities[0]
    return fingerprint, team


def check_keychain_search_list():
    """Refuse to sign when the login keychain is off the keychain search list.

    The certificate is public and can be read by path, but codesign needs the
    private key, and the key is reachable only through the keychain search
    list.  When the login keychain is locked or is not on that list, the build
    fails deep inside xcodebuild with a message about certificates that does not
    name the cause.  So the cause is checked here, where it can be named.
    """
    listing = run_security(["list-keychains"]).stdout
    login = os.path.expanduser(LOGIN_KEYCHAIN)
    if login in listing:
        return
    raise BuildFailure(KEYCHAIN_SEARCH_LIST_MESSAGE, 3)


GIT = "git"
GIT_TIMEOUT = 30
BUILD_NUMBER_PATTERN = re.compile(r"^[0-9]{12}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CHANNELS = ("release", "main", "lane", "development")


def run_git(directory, arguments):
    """Run one read-only git command in the Muster folder and return its output."""
    try:
        result = subprocess.run(
            [GIT, "-C", str(directory)] + list(arguments),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise BuildFailure(
            "git did not answer within %s seconds, so the commit this build is made from is "
            "unknown and the build refuses to continue." % GIT_TIMEOUT,
            124,
        ) from None
    except FileNotFoundError:
        raise BuildFailure(
            "git is missing, so the commit this build is made from is unknown and the build "
            "refuses to continue.",
            127,
        ) from None
    except OSError:
        raise BuildFailure(
            "git could not be run to read the commit this build is made from, so the build "
            "number and the commit stamp are unknown and the build refuses to continue.",
            2,
        ) from None
    return result


def build_number_for(commit_seconds):
    """The 12-digit build number:  the commit's time in UTC, YYYYMMDDHHMM.

    UTC and not the Mac's zone, because a zone that falls back an hour in November would
    give a later merge a smaller number, and Sparkle offers only a larger one.  A copy built
    under the earlier America/Chicago numbering always reads as older than the UTC number
    for the same moment, so the change never strands an installed copy.
    """
    moment = datetime.datetime.fromtimestamp(commit_seconds, tz=datetime.timezone.utc)
    return moment.strftime("%Y%m%d%H%M"), moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_identity(directory, configuration, channel=None, override=None):
    """What this build is made from:  build number, commit, commit time and channel.

    `directory` is the Muster folder.  The channel is `development` for the Debug
    configuration, `main` for a clean tree whose HEAD is on origin/main as last fetched,
    and `lane` otherwise.  The release workflow names its channel itself.
    """
    head = run_git(directory, ["rev-parse", "HEAD"])
    commit = head.stdout.strip().lower()
    if head.returncode or not COMMIT_PATTERN.match(commit):
        raise BuildFailure(
            "git did not name the commit this build is made from, so the build refuses to "
            "continue.  Run the script from a checkout of AI-Fleet-Coordinator.",
            2,
        )
    shown = run_git(directory, ["show", "-s", "--format=%ct", "HEAD"])
    seconds = shown.stdout.strip()
    if shown.returncode or not seconds.isdecimal():
        raise BuildFailure("git did not give the commit's time, so the build number is unknown.", 2)
    number, commit_time = build_number_for(int(seconds))
    if override:
        if not BUILD_NUMBER_PATTERN.match(override):
            raise BuildFailure(
                "MUSTER_BUILD_NUMBER must be the 12-digit merge-time build number (YYYYMMDDHHMM).",
                2,
            )
        number = override
    if channel:
        if channel not in CHANNELS:
            raise BuildFailure("The build channel must be one of: %s." % ", ".join(CHANNELS), 2)
    elif configuration == "Debug":
        channel = "development"
    else:
        dirty = run_git(directory, ["status", "--porcelain", "--", "."])
        on_main = run_git(directory, ["merge-base", "--is-ancestor", "HEAD", "origin/main"])
        clean = dirty.returncode == 0 and not dirty.stdout.strip()
        channel = "main" if clean and on_main.returncode == 0 else "lane"
    return {
        "BUILD_NUMBER": number,
        "COMMIT": commit,
        "COMMIT_TIME": commit_time,
        "CHANNEL": channel,
    }


def installed_build_number(app):
    """The installed copy's CFBundleVersion, or None when there is no readable copy."""
    try:
        with (Path(app) / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    value = info.get("CFBundleVersion") if isinstance(info, dict) else None
    return value if isinstance(value, str) and BUILD_NUMBER_PATTERN.match(value) else None


def refuse_older(app, number, allow_older):
    """Refuse to install a build numbered below the installed one, unless asked to."""
    installed = installed_build_number(app)
    if installed is None or allow_older or int(number) >= int(installed):
        return
    raise BuildFailure(
        "The installed Muster is build %s, newer than this build %s.  Installing it would "
        "go backward, and Sparkle would then offer the newer release again.  Pass "
        "--allow-older to install it anyway." % (installed, number),
        4,
    )


def main(argv):
    """Run one builder step: signing-identity, keychain-preflight or build-identity.

    build-identity CONFIGURATION [INSTALLED_APP ALLOW_OLDER] prints KEY=VALUE lines.
    MUSTER_BUILD_NUMBER overrides the number and MUSTER_BUILD_CHANNEL names the channel.
    """
    commands = ("signing-identity", "keychain-preflight", "build-identity")
    valid = len(argv) == 2 and argv[1] in commands[:2]
    valid = valid or (len(argv) in (3, 5) and argv[1] == "build-identity")
    if not valid:
        print("Usage: build_support.py [%s]" % "|".join(commands), file=sys.stderr)
        return 2
    try:
        if argv[1] == "signing-identity":
            fingerprint, team = select_signing_identity(os.environ.get("MUSTER_SIGNING_IDENTITY"))
            print(fingerprint, team)
        elif argv[1] == "build-identity":
            identity = build_identity(
                Path(__file__).resolve().parent.parent,
                argv[2],
                channel=os.environ.get("MUSTER_BUILD_CHANNEL"),
                override=os.environ.get("MUSTER_BUILD_NUMBER"),
            )
            if len(argv) == 5:
                refuse_older(argv[3], identity["BUILD_NUMBER"], argv[4] == "1")
            for key, value in identity.items():
                print("%s=%s" % (key, value))
        else:
            check_keychain_search_list()
    except BuildFailure as failure:
        print(str(failure), file=sys.stderr)
        return failure.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
