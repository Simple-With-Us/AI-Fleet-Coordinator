"""Build the fixed one-image ECHO-read transaction in a new scratch directory.

No service, root staging, credentials, installation or activation. The caller
must arrange separate review and qualified operator custody before activation.
"""

import argparse
import ast
import hashlib
import json
import os
import sys
from pathlib import Path

BEFORE = "30aaa6b107e241499435bc797c9d0ed40fe6bba6973f56174514b9e2ee7248e4"
AFTER = "PENDING_QUALIFIED_OPERATOR_OBSERVATION_PIN"
CONFIG = "af09bf3b8e0679ec50c7bf3808fb11e203a46ac2ab32e8652961182f38c9e29b"
ROOT = "/opt/git-relay/transactions/echo-admin-read"
UNIT = "git-relay-echo-admin-read.service"
PROOF = "67460d00c873056a6ebcf937f55c0fc9c7741986149261d03029978cbd4f52fa"
MODULES = {
    "__init__.py": "e9c5519a796eab24d04e416e5bd30598015b5a03c09141d856a39d898d92c423",
    "deploy_guard.py": "3acb883f2163f7acf23d50213c259a17792761003e277157a5193f4e248e0aba",
    "deploy_host.py": "eaa8fc8bde34e0714f61e4f758911c81beac51d6218c030f259b48c3fd33b695",
    "deploy_journal.py": "bb39bbe7ced794771aa419c357509cf2a30bc719d68cdb5c29557f7cec74a2a0",
    "deploy_recovery.py": "e362ba890b0e19fa8fbd07a23096358bfbc8b29b5a9524da09546b4f4c5619d1",
    "deploy_runner.py": "bb11dcc965a7aa49a29d7b0ab8583e3486c9a7af9f0d932a6a89bbac115b5267",
    "deploy_supervisor.py": "1a1b9f45adb6498d78811d1c7742257440043c19e3fbaf9f174e66ca88081040",
    "deploy_transaction.py": "58cbd2aa2ce842e51d8b9c265adfe08b88634d87a6409a1b96da88bfb82321f9",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def image(before):
    if digest(before) != BEFORE:
        raise ValueError("live ECHO policy baseline drift")
    from git_relay.echo_admin_read import digest as policy_digest
    from git_relay.echo_admin_read import prepare

    policy = json.loads(before)
    after = (
        json.dumps(prepare(policy, policy_digest(policy)), sort_keys=True, indent=2) + "\n"
    ).encode()
    if digest(after) != AFTER:
        raise ValueError("exact ECHO-only candidate drift")
    return after


def private_dir(path):
    if path.is_symlink():
        raise ValueError("scratch package symlink refused")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("scratch package must be owner-only")


def write_file(path, raw, private=False):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600 if private else 0o644)
        stream.write(raw)


def render_entry(template, raw_manifest, sources):
    module_hashes = {name: digest(raw) for name, raw in sources.items()}
    if not module_hashes:
        raise ValueError("runner module hashes are empty")
    manifest_placeholder = "MANIFEST_DIGEST_PLACEHOLDER"
    module_placeholder = "MODULE_HASHES = {}  # MODULE_HASHES_PLACEHOLDER"
    for marker in (manifest_placeholder, module_placeholder):
        if template.count(marker) != 1:
            raise ValueError("runner template placeholder drift")
    entry = template.replace(manifest_placeholder, digest(raw_manifest)).replace(
        module_placeholder, "MODULE_HASHES = " + repr(module_hashes)
    )
    if "PLACEHOLDER" in entry:
        raise ValueError("runner template placeholder remains")
    bindings = {}
    for node in ast.parse(entry).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                    "MANIFEST_DIGEST",
                    "MODULE_HASHES",
                    "DIRECTORY",
                ):
                    if target.id in bindings:
                        raise ValueError("duplicate runner binding")
                    if target.id == "DIRECTORY":
                        value = node.value
                        if not (
                            isinstance(value, ast.Call)
                            and isinstance(value.func, ast.Name)
                            and value.func.id == "Path"
                            and len(value.args) == 1
                            and not value.keywords
                        ):
                            raise ValueError("runner directory binding shape")
                        bindings[target.id] = ast.literal_eval(value.args[0])
                    else:
                        bindings[target.id] = ast.literal_eval(node.value)
    if bindings != {
        "MANIFEST_DIGEST": digest(raw_manifest),
        "MODULE_HASHES": module_hashes,
        "DIRECTORY": ROOT,
    }:
        raise ValueError("runner binding drift")
    return entry


def build(repo, before, output):
    here = Path(__file__).resolve().parent
    package = Path(repo) / "git-relay/git_relay"
    candidate = image(Path(before).read_bytes())
    sources = {}
    for name, expected in MODULES.items():
        raw = (package / name).read_bytes()
        if digest(raw) != expected:
            raise ValueError("finite recovery module drift: " + name)
        if name == "deploy_runner.py":
            if raw.count(b"git-relay-patch-1429.service") != 1:
                raise ValueError("runner unit anchor drift")
            raw = raw.replace(b"git-relay-patch-1429.service", UNIT.encode())
        sources["git_relay/" + name] = raw
    proof = (here / "runtime_proof.py").read_bytes()
    if digest(proof) != PROOF:
        raise ValueError("runtime proof module drift")
    sources["git_relay/deploy_proof.py"] = proof
    metadata = {"uid": 65534, "gid": 65534, "mode": 384}
    manifest = {
        "config": CONFIG,
        "files": [
            {
                "root": "state",
                "name": "seats.json",
                "before": BEFORE,
                "after": AFTER,
                "meta": metadata,
            },
        ],
        "runner": ROOT + "/runner.py",
        "unit": UNIT,
    }
    raw_manifest = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    entry = render_entry((here / "runner.template.py").read_text(), raw_manifest, sources)
    output = Path(output)
    output.mkdir(mode=0o700, exist_ok=False)
    for name, raw in sources.items():
        target = output / "runner-package" / name
        private_dir(target.parent.parent)
        private_dir(target.parent)
        write_file(target, raw)
    for name, raw in {
        "0.candidate": candidate,
        "manifest.json": raw_manifest,
        "runner.py": entry.encode(),
    }.items():
        write_file(output / name, raw, private=name == "0.candidate")
    return {
        "manifest": digest(raw_manifest),
        "runner": digest(entry.encode()),
        "images": [AFTER],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / "git-relay"))
    print(json.dumps(build(args.repo, args.before, args.output), sort_keys=True))
