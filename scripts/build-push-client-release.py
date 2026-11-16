"""Build a finite offline push client from pinned inputs. Never install it."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path

BASE = {
    "scripts/simple-git": "7c375cbe1b13c836b382871e05f63a4ceb5590a3d967912ddc000ec42a873a92",
    "scripts/git-tools/git": "8b8cc9d0b1e0b1f58d82d024deaa96edb5fd470f990b321ed3e2051a0c06ff2e",
    "scripts/git-tools/catalog.json": "a92812f6893791957552246167fe6f46baaf198f73f0d5be427f668ce05971ea",
    "scripts/git-tools/export_catalog.py": "727f0e66aee78660c01cb9d36e5861a547fd2d962664e54f1397e9d4d57ceed9",
    "scripts/simple_tools/secretscan.py": "f29d222d0d6af9726b93c268134cf4fe4c84af10a60da9460250aef9892c253c",
}
INPUTS = {
    "git": "47e12d5c2f9cbd8f3284801849cfa09f85abbb447ba3f69e4b504ba6853da6b4",
    "catalog.json": "3325eb0a1971fa3fb0b1843d9cf1f89d48e7867279576eb89535983d236f31cc",
}
RESULTS = dict(
    BASE,
    **{
        "scripts/git-tools/git": "a9b33dd4edd3a71e19eed164416f275da86b9127d50b75e42fa07e45b445f297",
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
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError("symlink in input path")
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
    for parent in destination.parents:
        if parent.is_symlink():
            raise ValueError("symlink in output parent")
    destination.mkdir(mode=0o700)
    for name, data in payload.items():
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o755 if name in ("scripts/simple-git", impl) else 0o644)
    manifest = {
        "baseline": "0fac2e8417218ff8791da9d699b66de0770bddca",
        "source_merge": "c92db57b12c231d6cd0b473233adc6430f6baed6",
        "activation_performed": False,
        "files": RESULTS,
        "state": "UNREVIEWED_NOT_INSTALLED",
    }
    (destination / "push-release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.baseline, args.inputs, args.destination), indent=2))
