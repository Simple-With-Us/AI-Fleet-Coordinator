#!/usr/bin/env bash
# fleet-recall-service bootstrap for a plain python:3.12-slim container (no git, no curl, no pip).
#
# 1. Fetch the public repo tarball with python urllib (reused if RECALL_TARBALL already exists).
# 2. Extract scripts/fleet_rag and scripts/fleet-recall-service into RECALL_APP_DIR.
# 3. Install the pinned gitleaks release so recall_contribute is gated exactly as on the Mac
#    (download failure is logged and tolerated: recall_api then reports gitleaks-unavailable).
# 4. exec python3 RECALL_APP_DIR/fleet-recall-service/server.py
#
# Env:
#   RECALL_REF               git ref / branch / tag / sha of Simple-With-Us/AI-Fleet-Coordinator (default main)
#   RECALL_APP_DIR           install root (default /app)
#   RECALL_TARBALL           tarball path; reused when present, downloaded there otherwise (default /tmp/fleet-src.tgz)
#   RECALL_TARBALL_URL       override the download URL (tests / mirrors); default is the codeload URL for RECALL_REF
#   RECALL_BOOTSTRAP_ONLY    1 = stop after extracting + installing gitleaks (tests)
#   GITLEAKS_VERSION         gitleaks release to install (default 8.30.1)
#   RECALL_GITLEAKS_DIR      where the binary goes (default /usr/local/bin)
#   RECALL_GITLEAKS_URL      override the release tarball URL (tests / mirrors)
#   RECALL_GITLEAKS_SHA256   optional: refuse the tarball unless its sha256 matches
#   RECALL_GITLEAKS_MIN_BYTES smallest plausible tarball (default 1000000; tests lower it)
#   RECALL_GITLEAKS_REQUIRED 1 = a failed install aborts (the Dockerfile build); default continue
#   RECALL_GITLEAKS_SKIP     1 = do not touch gitleaks at all
#   RECALL_GITLEAKS_ONLY     1 = only run the gitleaks step (the Dockerfile RUN), no source fetch, no server
# Everything else (RECALL_API_TOKEN, QDRANT_*, TEI_*, PORT) is read by server.py.  Nothing here
# prints a secret.
set -euo pipefail

REF="${RECALL_REF:-main}"
APP="${RECALL_APP_DIR:-/app}"
TARBALL="${RECALL_TARBALL:-/tmp/fleet-src.tgz}"
REPO_URL="${RECALL_TARBALL_URL:-https://codeload.github.com/Simple-With-Us/AI-Fleet-Coordinator/tar.gz/${REF}}"

export REF APP TARBALL REPO_URL

if [ "${RECALL_GITLEAKS_ONLY:-0}" != "1" ]; then
python3 - <<'PY'
import os, sys, tarfile, urllib.request

ref, app, tarball, url = os.environ["REF"], os.environ["APP"], os.environ["TARBALL"], os.environ["REPO_URL"]
if not (os.path.isfile(tarball) and os.path.getsize(tarball) > 0):
    print(f"bootstrap: downloading {url}", flush=True)
    tmp = tarball + ".part"
    with urllib.request.urlopen(url, timeout=180) as r, open(tmp, "wb") as fh:
        fh.write(r.read())
    os.replace(tmp, tarball)
else:
    print(f"bootstrap: reusing {tarball}", flush=True)

wanted = ("fleet_rag/", "fleet-recall-service/")
n = 0
os.makedirs(app, exist_ok=True)
with tarfile.open(tarball, "r:gz") as t:
    members = []
    for m in t.getmembers():
        parts = m.name.split("/", 2)          # <repo-ref>/scripts/<rest>
        if len(parts) != 3 or parts[1] != "scripts" or not parts[2].startswith(wanted):
            continue
        if not (m.isfile() or m.isdir()):
            continue
        m.name = parts[2]
        members.append(m)
    kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
    t.extractall(app, members=members, **kwargs)
    n = sum(1 for m in members if m.isfile())
server = os.path.join(app, "fleet-recall-service", "server.py")
if not os.path.isfile(server):
    print(f"bootstrap: {server} missing after extract (ref={ref}); aborting", flush=True)
    sys.exit(1)
print(f"bootstrap: extracted {n} files from ref {ref} into {app}", flush=True)
PY
fi

# --- gitleaks: same secret gate as the Mac seats.  Static Go binary from the pinned GitHub
# release (gitleaks_<ver>_linux_<x64|arm64>.tar.gz, picked by `uname -m`), size-checked,
# executed once (`gitleaks version` must print the pinned version), then installed atomically.
# Never prints anything sensitive.
if [ "${RECALL_GITLEAKS_SKIP:-0}" != "1" ]; then
python3 - <<'PY'
import hashlib, io, os, shutil, subprocess, sys, tarfile, urllib.request

ver = os.environ.get("GITLEAKS_VERSION", "8.30.1").strip().lstrip("v") or "8.30.1"
dest_dir = os.environ.get("RECALL_GITLEAKS_DIR") or "/usr/local/bin"
dest = os.path.join(dest_dir, "gitleaks")
required = os.environ.get("RECALL_GITLEAKS_REQUIRED") == "1"
min_bytes = int(os.environ.get("RECALL_GITLEAKS_MIN_BYTES") or 1_000_000)
max_bytes = 200_000_000
want_sha = (os.environ.get("RECALL_GITLEAKS_SHA256") or "").strip().lower()


def say(msg):
    print("bootstrap: gitleaks: " + msg, flush=True)


def give_up(msg):
    if required:
        say(msg + " -- RECALL_GITLEAKS_REQUIRED=1, aborting")
        sys.exit(1)
    say(msg + " -- continuing WITHOUT gitleaks; recall_contribute will report gitleaks-unavailable")
    sys.exit(0)


def runs_pinned_version(path):
    try:
        proc = subprocess.run([path, "version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and ver in (proc.stdout + proc.stderr)


if runs_pinned_version(dest):
    say(f"{dest} already runs {ver}; nothing to do")
    sys.exit(0)

# `uname -m` -> the suffix gitleaks uses in its release asset names (checked against the
# v8.30.1 release: linux_x64, linux_arm64, linux_x32, linux_armv6, linux_armv7).
machine = os.uname().machine
arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64",
        "i686": "x32", "i386": "x32", "armv7l": "armv7", "armv6l": "armv6"}.get(machine)
if not arch:
    give_up(f"no gitleaks release for architecture {machine}")
url = os.environ.get("RECALL_GITLEAKS_URL") or (
    f"https://github.com/gitleaks/gitleaks/releases/download/v{ver}/gitleaks_{ver}_linux_{arch}.tar.gz")
say(f"downloading {url}")
try:
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read(max_bytes + 1)
except Exception as e:  # noqa: BLE001 - URLError, HTTPError, socket timeouts, ...
    give_up(f"download failed ({type(e).__name__}: {e})")
if len(data) < min_bytes or len(data) > max_bytes:
    give_up(f"implausible tarball size {len(data)} bytes (expected {min_bytes}..{max_bytes})")
if want_sha:
    got_sha = hashlib.sha256(data).hexdigest()
    if got_sha != want_sha:
        give_up(f"sha256 mismatch (got {got_sha})")

staging = dest + ".staging"
try:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        member = next((m for m in t.getmembers() if m.isfile() and os.path.basename(m.name) == "gitleaks"), None)
        if member is None:
            give_up("tarball contains no gitleaks binary")
        os.makedirs(dest_dir, exist_ok=True)
        with t.extractfile(member) as src, open(staging, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.chmod(staging, 0o755)
    if not runs_pinned_version(staging):
        give_up(f"extracted binary does not run or does not report {ver}")
    os.replace(staging, dest)
except (OSError, tarfile.TarError) as e:
    give_up(f"install failed ({type(e).__name__}: {e})")
finally:
    try:
        os.unlink(staging)
    except OSError:
        pass
say(f"installed {ver} at {dest} ({len(data)} bytes, linux/{arch})")
PY
else
  echo "bootstrap: gitleaks: RECALL_GITLEAKS_SKIP=1, not installed (recall_contribute will report gitleaks-unavailable)"
fi

if [ "${RECALL_GITLEAKS_ONLY:-0}" = "1" ]; then
  exit 0
fi
if [ "${RECALL_BOOTSTRAP_ONLY:-0}" = "1" ]; then
  echo "bootstrap: RECALL_BOOTSTRAP_ONLY=1, not starting the server"
  exit 0
fi
exec python3 "${APP}/fleet-recall-service/server.py"
