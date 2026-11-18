#!/usr/bin/env bash
# Install reviewed broker code only; credentials, keys and runtime state stay untouched.
# Run it as the account that runs the helper (jay on the server), never as root:  the code goes to
# that account's ~/.local/lib/fleet-resource-control, owned by it and writable by no one else.
set -euo pipefail
[[ $# == 0 ]] || { echo 'This installer accepts no arguments.' >&2; exit 2; }
here=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
scripts=$(cd -P -- "$here/.." && pwd)
exec /usr/bin/python3 -I - "$scripts" <<'PY'
import contextlib
import json
import os
import pwd
import secrets
import stat
import sys

# The install folder, below the installing account's home directory (from the account database,
# never from $HOME).
DESTINATION = ('.local', 'lib', 'fleet-resource-control')
MUSTER_AGENT_DESTINATION = ('usr', 'local', 'lib', 'muster-agent')
ROOT = '/'
ROOT_UID = 0
MAX_SOURCE_BYTES = 2 * 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def home_components(uid):
    """The installing account's home directory, as path components below ROOT."""
    home = pwd.getpwuid(uid).pw_dir
    if not os.path.isabs(home):
        raise RuntimeError('the installing account has no absolute home directory')
    return tuple(part for part in home.split('/') if part)


def protected(info, owners):
    return stat.S_ISDIR(info.st_mode) and info.st_uid in owners and not info.st_mode & 0o022


def open_existing_directory(parent, name, owners):
    """A folder on the way to the home directory:  it must already exist, be owned by root or
    the installing account, and be writable by neither group nor others."""
    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if not protected(os.fstat(fd), owners):
            raise RuntimeError('a folder above the installation is not protected')
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_directory(parent, name, owner):
    """A folder of the installation:  made when missing, and it must be the installing account's
    own and writable by neither group nor others."""
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if not protected(os.fstat(fd), (owner,)):
            raise RuntimeError('an installation folder is not owned by the installing account and protected')
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_source(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
            raise RuntimeError('an installer source is not a bounded regular file')
        data = source.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise RuntimeError('an installer source is too large')
    if name.endswith('.py'):
        compile(data, name, 'exec')  # Syntax validation never imports or executes source.
    return data


def write_file(parent, name, data, owner):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise RuntimeError('an installed file is not owned by the installing account and protected')
    temporary = '.install-' + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o644, dir_fd=parent)
    try:
        os.fchmod(fd, 0o644)
        remaining = memoryview(data)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise RuntimeError('an installed file could not be fully written')
            remaining = remaining[count:]
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent)


def muster_agent_copy():
    """The bytes of muster-agent's installed copy of the shared reader, or None when it is absent."""
    descriptors = []
    try:
        fd = os.open(ROOT, DIRECTORY_FLAGS)
        descriptors.append(fd)
        for component in MUSTER_AGENT_DESTINATION:
            fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            descriptors.append(fd)
        return read_source(fd, 'muster_trust_files.py')
    except FileNotFoundError:
        return None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def install(source_directory):
    owner = os.geteuid()
    if owner == ROOT_UID or not sys.platform.startswith('linux'):
        raise RuntimeError('run as the account that runs the helper, not root, on the intended Linux host')
    home = home_components(owner)
    # Build and syntax-check the complete fixed payload before creating the install tree.
    payload = {}
    with contextlib.ExitStack() as stack:
        source = os.open(source_directory, DIRECTORY_FLAGS)
        stack.callback(os.close, source)
        for name in (
            'fleet-ops-control.py',
            'fleet-ops-control.json',
            'fleet-ops-eavesdropper-settings.json',
            'fleet_resource_control.py',
            'fleet_seat_inventory.py',
            'reduced_status.py',
            # The `--from-muster` entry's approval engine, loaded by the helper from beside itself
            # under the same owner and mode rule as the reducer.
            'muster_approval.py',
            'fleet_infisical.py',
            'fleet_infisical_cli.py',
        ):
            payload[name] = read_source(source, name)
        # The shared /etc/muster reader has one source file in the repository,
        # scripts/muster_agent/muster_trust_files.py (server-route-spec section 10), and is
        # installed flat beside the helper, byte for byte.
        agent = os.open('muster_agent', DIRECTORY_FLAGS, dir_fd=source)
        stack.callback(os.close, agent)
        payload['muster_trust_files.py'] = read_source(agent, 'muster_trust_files.py')
        config = json.loads(payload['fleet-ops-control.json'])
        if not isinstance(config, dict) or config.get('schemaVersion') != 1:
            raise RuntimeError('the broker allowlist has an invalid schema')
        settings = json.loads(payload['fleet-ops-eavesdropper-settings.json'])
        if not isinstance(settings, dict) or not isinstance(settings.get('applyEnabled'), bool):
            raise RuntimeError('the eavesdropper settings allowlist has an invalid shape')
        package = os.open('fleet_secrets', DIRECTORY_FLAGS, dir_fd=source)
        stack.callback(os.close, package)
        names = sorted(name for name in os.listdir(package) if name.endswith('.py'))
        if not {'__init__.py', 'core.py'} <= set(names):
            raise RuntimeError('the broker secrets package is incomplete')
        for name in names:
            payload['fleet_secrets/' + name] = read_source(package, name)
        ledger = os.open('simple_tools', DIRECTORY_FLAGS, dir_fd=source)
        stack.callback(os.close, ledger)
        for name in ('__init__.py', 'operator_github_state.py'):
            payload['simple_tools/' + name] = read_source(ledger, name)
        hetzner = os.open('hetzner', DIRECTORY_FLAGS, dir_fd=source)
        stack.callback(os.close, hetzner)
        for name in ('fleet_coding_memory.py', 'coding_resources.py'):
            payload['hetzner/' + name] = read_source(hetzner, name)
        payload['hetzner/__init__.py'] = b''
        parent = os.open(ROOT, DIRECTORY_FLAGS)
        stack.callback(os.close, parent)
        for component in home:
            parent = open_existing_directory(parent, component, (ROOT_UID, owner))
            stack.callback(os.close, parent)
        for component in DESTINATION:
            parent = open_directory(parent, component, owner)
            stack.callback(os.close, parent)
        os.fchmod(parent, 0o755)
        packages = {}
        for name in ('fleet_secrets', 'hetzner', 'simple_tools'):
            packages[name] = open_directory(parent, name, owner)
            stack.callback(os.close, packages[name])
            os.fchmod(packages[name], 0o755)
        for relative, data in payload.items():
            parts = relative.split('/')
            write_file(parent if len(parts) == 1 else packages[parts[0]], parts[-1], data, owner)
    # The same reader is installed for muster-agent too.  When that copy is present, the two must be
    # the same bytes; a difference fails the installation (server-route-spec section 10).
    other = muster_agent_copy()
    if other is not None and other != payload['muster_trust_files.py']:
        raise RuntimeError('the installed copies of muster_trust_files.py differ')


if __name__ == '__main__':
    try:
        install(sys.argv[1])
    except (OSError, RuntimeError, ValueError, SyntaxError, KeyError):
        print('Resource control installation failed; verify reviewed source and protected destination paths.', file=sys.stderr)
        raise SystemExit(1)
    print('Resource control code installed; credentials, keys and runtime state were untouched.')
PY
