"""Pure scratch builder for an exact one-file reviewed scanner transaction.

No host staging, service, credential, network or activation effects. Inputs are
immutable release files and the exact source baseline. All live facts must be
revalidated before any separately authorized qualified activation.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path

BEFORE = "8762d7c50c0c09b92e34f640643f77b71f78524e412233ecb266f9f9a4b84802"
AFTER = "8bd0ea0b3a9fe2782d490c97065e4aff2ec7f38a2630abb04e98eca8e491e235"
CONFIG = "af09bf3b8e0679ec50c7bf3808fb11e203a46ac2ab32e8652961182f38c9e29b"
ROOT = "/opt/git-relay/transactions/ct-match-pinned"
UNIT = "git-relay-ct-match-pinned.service"
MODULES = {
    "__init__.py": "e9c5519a796eab24d04e416e5bd30598015b5a03c09141d856a39d898d92c423",
    "deploy_guard.py": "3acb883f2163f7acf23d50213c259a17792761003e277157a5193f4e248e0aba",
    "deploy_host.py": "bbb1d3906e89f9f83f10c0a0f6976aa1e72244bbc9d112a0f841b49bbb89fce5",
    "deploy_journal.py": "bb39bbe7ced794771aa419c357509cf2a30bc719d68cdb5c29557f7cec74a2a0",
    "deploy_recovery.py": "e362ba890b0e19fa8fbd07a23096358bfbc8b29b5a9524da09546b4f4c5619d1",
    "deploy_runner.py": "36a9033e2678ae57b071c391d2dee954b4ec818419433c00d06eb7250954db72",
    "deploy_supervisor.py": "cab9dd1336c7f9eafd81fa9327bf4eb613363bd9486cb17ae3b491cc3f82c115",
    "deploy_transaction.py": "a6bf527f0cf9946ea352c84372fb49df3682cd163f2c91d46ddf301521d50852",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def build(repo, before, output):
    here = Path(__file__).resolve().parent
    package = Path(repo) / "git-relay/git_relay"
    if digest(Path(before).read_bytes()) != BEFORE:
        raise ValueError("live scanner baseline drift")
    candidate = (package / "patch_secretscan.py").read_bytes()
    if digest(candidate) != AFTER:
        raise ValueError("reviewed candidate drift")
    mirror = (Path(repo) / "scripts/simple_tools/secretscan.py").read_bytes()
    if mirror != candidate:
        raise ValueError("source mirror drift")
    sources = {}
    for name, expected in MODULES.items():
        raw = (package / name).read_bytes()
        if digest(raw) != expected:
            raise ValueError("finite recovery module drift: " + name)
        if name == "deploy_runner.py":
            if raw.count(b"git-relay-patch-1429.service") != 1:
                raise ValueError("fixed runner unit anchor drift")
            raw = raw.replace(b"git-relay-patch-1429.service", UNIT.encode())
        sources["git_relay/" + name] = raw
    sources["git_relay/wise_runtime_proof.py"] = (here / "wise_runtime_proof.py").read_bytes()
    ast.parse(sources["git_relay/wise_runtime_proof.py"].decode())
    manifest = {
        "config": CONFIG,
        "files": [
            {
                "root": "code",
                "name": "patch_secretscan.py",
                "before": BEFORE,
                "after": AFTER,
                "meta": {"uid": 0, "gid": 0, "mode": 0o664},
            }
        ],
        "runner": ROOT + "/runner.py",
        "unit": UNIT,
    }
    raw_manifest = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    hashes = {name: digest(raw) for name, raw in sources.items()}
    template = (here / "runner.template.py").read_text()
    markers = ["MANIFEST_DIGEST_PLACEHOLDER", "MODULE_HASHES = {}  # MODULE_HASHES_PLACEHOLDER"]
    if any(template.count(marker) != 1 for marker in markers):
        raise ValueError("runner template binding drift")
    entry = template.replace(markers[0], digest(raw_manifest)).replace(
        markers[1], "MODULE_HASHES = " + repr(hashes)
    )
    if "PLACEHOLDER" in entry:
        raise ValueError("incomplete fixed runner")
    ast.parse(entry)
    # New scratch directory only. Never overwrites a prior release or host target.
    output = Path(output)
    output.mkdir(mode=0o700, exist_ok=False)
    for name, raw in sources.items():
        target = output / "runner-package" / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(raw)
    for name, raw in {
        "0.candidate": candidate,
        "manifest.json": raw_manifest,
        "runner.py": entry.encode(),
    }.items():
        (output / name).write_bytes(raw)
    return {
        "manifest": digest(raw_manifest),
        "runner": digest(entry.encode()),
        "image": AFTER,
        "files": 1,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.before, args.output), sort_keys=True))
