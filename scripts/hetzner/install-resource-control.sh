#!/usr/bin/env bash
# Install reviewed broker code only; credentials, keys and runtime state stay untouched.
set -euo pipefail
[[ $# == 0 ]] || { echo 'This installer accepts no arguments.' >&2; exit 2; }
here=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
scripts=$(cd -P -- "$here/.." && pwd)
exec /usr/bin/python3 -I - "$scripts" <<'PY'
import contextlib
import json
import os
import secrets
import stat
import sys

DESTINATION = ('usr', 'local', 'lib', 'fleet-resource-control')
ROOT = '/'
ROOT_UID = 0
MAX_SOURCE_BYTES = 2 * 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_directory(parent, name):
    try:
        os.mkdir(name, 0o755, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise RuntimeError('an installation directory is not root-owned and protected')
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


def write_file(parent, name, data):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022:
            raise RuntimeError('an installed file is not root-owned and protected')
    temporary = '.install-' + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o644, dir_fd=parent)
    try:
        os.fchmod(fd, 0o644)
        os.fchown(fd, ROOT_UID, 0)
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


def install(source_directory):
    if os.geteuid() != 0 or not sys.platform.startswith('linux'):
        raise RuntimeError('run as root on the intended Linux host')
    # Build and syntax-check the complete fixed payload before creating the install tree.
    payload = {}
    with contextlib.ExitStack() as stack:
        source = os.open(source_directory, DIRECTORY_FLAGS)
        stack.callback(os.close, source)
        for name in ('fleet-ops-control.py', 'fleet-ops-control.json', 'fleet_resource_control.py'):
            payload[name] = read_source(source, name)
        config = json.loads(payload['fleet-ops-control.json'])
        if not isinstance(config, dict) or config.get('schemaVersion') != 1:
            raise RuntimeError('the broker allowlist has an invalid schema')
        package = os.open('fleet_secrets', DIRECTORY_FLAGS, dir_fd=source)
        stack.callback(os.close, package)
        names = sorted(name for name in os.listdir(package) if name.endswith('.py'))
        if not {'__init__.py', 'core.py'} <= set(names):
            raise RuntimeError('the broker secrets package is incomplete')
        for name in names:
            payload['fleet_secrets/' + name] = read_source(package, name)
        hetzner = os.open('hetzner', DIRECTORY_FLAGS, dir_fd=source)
        stack.callback(os.close, hetzner)
        for name in ('fleet_coding_memory.py', 'coding_resources.py'):
            payload['hetzner/' + name] = read_source(hetzner, name)
        payload['hetzner/__init__.py'] = b''
        parent = os.open(ROOT, DIRECTORY_FLAGS)
        stack.callback(os.close, parent)
        for component in DESTINATION:
            parent = open_directory(parent, component)
            stack.callback(os.close, parent)
        os.fchmod(parent, 0o755)
        packages = {}
        for name in ('fleet_secrets', 'hetzner'):
            packages[name] = open_directory(parent, name)
            stack.callback(os.close, packages[name])
            os.fchmod(packages[name], 0o755)
        for relative, data in payload.items():
            parts = relative.split('/')
            write_file(parent if len(parts) == 1 else packages[parts[0]], parts[-1], data)


if __name__ == '__main__':
    try:
        install(sys.argv[1])
    except (OSError, RuntimeError, ValueError, SyntaxError):
        print('Resource control installation failed; verify reviewed source and protected destination paths.', file=sys.stderr)
        raise SystemExit(1)
    print('Resource control code installed; credentials, keys and runtime state were untouched.')
PY
