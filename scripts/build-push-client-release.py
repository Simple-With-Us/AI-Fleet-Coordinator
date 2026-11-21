"""Build a finite offline push client from pinned inputs. Never install it."""

import argparse
import ast
import hashlib
import json
import os
import tempfile
from pathlib import Path

BASE = {
    "scripts/simple-git": "7c375cbe1b13c836b382871e05f63a4ceb5590a3d967912ddc000ec42a873a92",
    "scripts/git-tools/git": "fb1b0b85d5d8f8ae1551c74a6813ff6bd7fbc8d9d2dd40b12a6939a5748266e6",
    "scripts/git-tools/catalog.json": "a92812f6893791957552246167fe6f46baaf198f73f0d5be427f668ce05971ea",
    "scripts/git-tools/export_catalog.py": "727f0e66aee78660c01cb9d36e5861a547fd2d962664e54f1397e9d4d57ceed9",
    "scripts/simple_tools/secretscan.py": "35cd59f8033acc4987f8524d9500de44b8d6e0a8bf485a587eadd668448f2183",
}
INPUTS = {
    "git": "47e12d5c2f9cbd8f3284801849cfa09f85abbb447ba3f69e4b504ba6853da6b4",
    "catalog.json": "3325eb0a1971fa3fb0b1843d9cf1f89d48e7867279576eb89535983d236f31cc",
}
RESULTS = dict(
    BASE,
    **{
        "scripts/git-tools/git": "67462bec3257b5179634007658c5f9d18485055c50507f1419e80849adff29fd",
        "scripts/git-tools/catalog.json": "0f9e7722bffb44cfed4da9bb612829eea2ea06f30f42fc2e4ce8e1a5fe4e9d61",
    },
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked(root, name, expected):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("input root must be a real directory")
    path = root / name
    # Reject links inside the declared input root, not aliases above it (/tmp on macOS).
    part = path
    while part != root:
        if part.is_symlink():
            raise ValueError("symlink in input path")
        part = part.parent
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError("input digest mismatch: " + name)
    return data


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("source anchor mismatch")
    return text.replace(old, new, 1)


def transform(text, source):
    nodes = {
        n.name: n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
    }

    def segment(name):
        return ast.get_source_segment(source, nodes[name]) + "\n\n"

    section = source[
        source.index("PUSH_MAX_COMMITS =") : source.index(
            "# A report, never a merge authorization."
        )
    ]
    text = once(
        text,
        "# A report, never a merge authorization.",
        section + "# A report, never a merge authorization.",
    )
    text = once(text, "def env(name: str)", segment("HostedToolExit") + "def env(name: str)")
    anchor = "# ----------------------------------------------------------------- answer parsing (mirrors board)"
    text = once(text, anchor, segment("_refusal_reason_of") + anchor)
    old = '        die(f"{tool} failed on the hosted server: {shown}{detail}{after}{note}", EXIT_ERROR)'
    new = """        reason = _refusal_reason_of(result) if code == "relay_refused" else None
        sys.stderr.write(
            f"{prog()}: {tool} failed on the hosted server: {shown}{detail}{after}{note}\\n"
        )
        status = re.search(r", github_status=([0-9]{3})\\)\\Z", detail)
        raise HostedToolExit(EXIT_ERROR, shown, reason, int(status.group(1)) if status else None)"""
    text = once(text, old, new)
    parser = next(
        n
        for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name == "build_parser"
    )
    text = once(
        text,
        ast.get_source_segment(text, parser),
        ast.get_source_segment(source, nodes["build_parser"]),
    )
    text = once(
        text,
        "    a = build_parser().parse_args(argv)\n",
        '    a = build_parser().parse_args(argv)\n    if a.cmd == "push":\n        _push_refuse_early(a)\n',
    )
    text = once(
        text,
        "overall_deadline = time.monotonic() + OVERALL_DEADLINE_S",
        'overall_deadline = time.monotonic() + (PUSH_DEADLINE_S if a.cmd == "push" else OVERALL_DEADLINE_S)',
    )
    text = once(
        text,
        '    if a.cmd == "list":\n',
        '    if a.cmd == "push":\n        return cmd_push(tools, a, bearer, server_seat, overall_deadline)\n    if a.cmd == "list":\n',
    )
    compile(text, "push-candidate", "exec")
    return text


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, mode):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build(baseline, inputs, destination):
    payload = {name: checked(baseline, name, expected) for name, expected in BASE.items()}
    source = {name: checked(inputs, name, expected) for name, expected in INPUTS.items()}
    impl = "scripts/git-tools/git"
    payload[impl] = transform(payload[impl].decode(), source["git"].decode()).encode()
    catalog = "scripts/git-tools/catalog.json"
    base, newer = json.loads(payload[catalog]), json.loads(source["catalog.json"])
    names = {"gr_wf_git_tree_create", "gr_wf_git_blob_create"}
    replacement = {t["name"]: t for t in newer["tools"] if t["name"] in names}
    if len(replacement) != 2:
        raise ValueError("missing schemas")
    for i, tool in enumerate(base["tools"]):
        if tool["name"] in replacement:
            base["tools"][i] = replacement[tool["name"]]
    payload[catalog] = (json.dumps(base, indent=1) + "\n").encode()
    if {name: digest(data) for name, data in payload.items()} != RESULTS:
        raise ValueError("result digest mismatch")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination exists")
    # Parent aliases are ordinary operator paths; canonicalize before creating output.
    destination = destination.parent.resolve(strict=True) / destination.name
    destination.mkdir(mode=0o700)
    for name, data in payload.items():
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(path, data, 0o755 if name in ("scripts/simple-git", impl) else 0o644)
    manifest = {
        "baseline": "f17794b5008cb7052f243f35a7da751dd1d11fe1",
        "source_merge": "c92db57b12c231d6cd0b473233adc6430f6baed6",
        "activation_performed": False,
        "files": RESULTS,
        "state": "UNREVIEWED_NOT_INSTALLED",
    }
    atomic_write(
        destination / "push-release-manifest.json",
        (json.dumps(manifest, indent=2) + "\n").encode(),
        0o644,
    )
    fsync_directory(destination.parent)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.baseline, args.inputs, args.destination), indent=2))
