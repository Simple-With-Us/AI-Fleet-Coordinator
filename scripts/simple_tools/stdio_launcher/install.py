#!/usr/bin/env python3
"""Install per-seat stdio launchers from launcher.py.  A dry run unless --apply.

    cd scripts && python3 -m simple_tools.stdio_launcher.install --seat CLAUDE
    cd scripts && python3 -m simple_tools.stdio_launcher.install --seat CLAUDE --apply

For each seat it writes <dest>/<seat>-simple-tools (mode 0700) with the config
block filled in, and copies scripts/mcp-servers/assigned-hosted-stdio.py to
<dest>/assigned-hosted-stdio.py (mode 0700) when it differs.  An existing file is
kept as <name>.bak-<UTC stamp> before it is replaced.  It never reads a bearer
file, only names its path, and never edits shell or client configuration.
"""

import argparse
import difflib
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "launcher.py")
TRANSPORT_SOURCE = os.path.normpath(
    os.path.join(HERE, "..", "..", "mcp-servers", "assigned-hosted-stdio.py")
)
TRANSPORT_NAME = "assigned-hosted-stdio.py"
SEAT_RE = re.compile(r"[A-Z][A-Z0-9-]{1,19}")
BLOCK_RE = re.compile(
    r"(# BEGIN LAUNCHER CONFIG[^\n]*\n)(.*?)(# END LAUNCHER CONFIG\n)", re.DOTALL
)


def render(seat, bearer_file, python, transport, home, template_text=None):
    if template_text is None:
        with open(TEMPLATE, encoding="utf-8") as source:
            template_text = source.read()
    text = template_text
    values = {
        "SEAT": seat, "BEARER_FILE": bearer_file, "PYTHON": python,
        "TRANSPORT": transport, "HOME": home,
    }
    for key, value in values.items():
        if not isinstance(value, str) or not value or "\n" in value:
            raise ValueError(f"{key} must be a one-line value")
        if key != "SEAT" and not os.path.isabs(value):
            raise ValueError(f"{key} must be an absolute path")
    body = "".join(f"{key} = {value!r}\n" for key, value in values.items())
    rendered, count = BLOCK_RE.subn(lambda m: m.group(1) + body + m.group(3), text, count=1)
    if count != 1:
        raise ValueError("launcher template has no config block")
    return rendered


def launcher_name(seat):
    return f"{seat.lower()}-simple-tools"


def write_private(path, text, stamp, apply, out):
    """Write text to path at mode 0700, keeping a backup.  Returns True when it changed."""
    old = None
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError(f"{path} exists and is not a regular file; refusing to replace it")
        with open(path, encoding="utf-8", errors="replace") as source:
            old = source.read()
    if old == text:
        print(f"unchanged  {path}", file=out)
        return False
    if old is None:
        print(f"create     {path}", file=out)
    else:
        print(f"replace    {path} (backup {path}.bak-{stamp})", file=out)
        diff = difflib.unified_diff(
            old.splitlines(True), text.splitlines(True), f"{path} (installed)", f"{path} (new)", n=1
        )
        out.writelines("    " + line if line.endswith("\n") else "    " + line + "\n" for line in diff)
    if not apply:
        return True
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if old is not None:
        shutil.copy2(path, f"{path}.bak-{stamp}")
        os.chmod(f"{path}.bak-{stamp}", 0o600)
    fd, temp = tempfile.mkstemp(prefix=".install.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o700)
            target.write(text)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise
    return True


def main(argv=None, out=None):
    out = out or sys.stdout
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seat", action="append", required=True, help="seat tag, such as CLAUDE; repeatable")
    parser.add_argument("--dest", default=os.path.join(home, ".local", "libexec"))
    parser.add_argument("--home", default=home)
    parser.add_argument("--secrets-dir", default=os.path.join(home, ".secrets", "fleet-tools"),
                        help="folder holding one bearer file per seat, named by the lowercase seat")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter the launcher execs the transport with")
    parser.add_argument("--transport-source", default=TRANSPORT_SOURCE)
    parser.add_argument("--apply", action="store_true", help="write the files; without it nothing changes")
    args = parser.parse_args(argv)

    seats = []
    for raw in args.seat:
        seat = raw.strip().upper()
        if not SEAT_RE.fullmatch(seat):
            parser.error(f"not a seat tag: {raw!r}")
        seats.append(seat)
    for key in ("dest", "home", "secrets_dir", "python", "transport_source"):
        if not os.path.isabs(getattr(args, key)):
            parser.error(f"--{key.replace('_', '-')} must be an absolute path")
    if not os.path.isfile(args.transport_source):
        parser.error(f"transport source {args.transport_source} does not exist")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    print("apply: writing files" if args.apply else "dry run: nothing is written (pass --apply)", file=out)
    transport = os.path.join(args.dest, TRANSPORT_NAME)
    with open(args.transport_source, encoding="utf-8") as source:
        transport_text = source.read()
    if os.path.lexists(transport):
        print(
            f"shared     {transport} is execed by every seat's launcher in {args.dest}, "
            "so a change to it applies to all of them, not only --seat",
            file=out,
        )
    write_private(transport, transport_text, stamp, args.apply, out)
    for seat in seats:
        text = render(
            seat, os.path.join(args.secrets_dir, seat.lower()), args.python, transport, args.home
        )
        write_private(os.path.join(args.dest, launcher_name(seat)), text, stamp, args.apply, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
