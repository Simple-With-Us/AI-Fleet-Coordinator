"""Build the fixed two-image 1511 transaction in a new scratch directory.

No service, root staging, credentials, installation or activation. The caller
must arrange separate review and qualified operator custody before activation.
"""

import argparse
import ast
import hashlib
import json
from pathlib import Path

BEFORE = "7a4a7cc41f9be1764fb830fdc8eeeb61f47062bb2cb14f7dc814347b6c55fc3b"
AFTER = "2d193e4d0699bd31ca978398ad2d7a571567878ec9edf9ee695058722adfa08e"
PROVIDER = "e3879275fca91f450cebd5151d929c64376e842df5906de94fef175c1e89b685"
CONFIG = "af09bf3b8e0679ec50c7bf3808fb11e203a46ac2ab32e8652961182f38c9e29b"
ROOT = "/opt/git-relay/transactions/provider-1511"
UNIT = "git-relay-provider-1511.service"
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


def overlay(raw):
    """Change only the 18 pinned old provider constants and add one import."""
    if digest(raw) != BEFORE:
        raise ValueError("live workflow baseline drift")
    text = raw.decode("utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    changes = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or "sk-" not in node.value
        ):
            continue
        value = node.value
        for pattern in [
            r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}",
            r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",
            r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}",
        ]:
            if value.startswith(pattern):
                value = value[len(pattern) :]
                break
        else:
            raise ValueError("unexpected provider alternative")
        for pattern in [
            r"|\bsk_(?:live|test)_[A-Za-z0-9]{16,}",
            r"|sk_(?:live|test)_[A-Za-z0-9]{16,}",
        ]:
            if value.startswith(pattern):
                value = value[len(pattern) :]
                break
        if value not in ("", r"|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}"):
            raise ValueError("unexpected nonprovider suffix")
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        replacement = "PROVIDER_PATTERN" + (" + " + repr(value) if value else "")
        changes.append((start, end, replacement.encode()))
    if len(changes) != 18:
        raise ValueError("provider site census drift")
    for start, end, replacement in sorted(changes, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    marker = b"from .secret_scan import find_secret"
    if raw.count(marker) != 1:
        raise ValueError("import anchor drift")
    raw = raw.replace(marker, b"from .provider_scan import PROVIDER_PATTERN\n" + marker)
    ast.parse(raw.decode())
    if digest(raw) != AFTER:
        raise ValueError("candidate image drift")
    return raw


def build(repo, before, output):
    here = Path(__file__).resolve().parent
    package = Path(repo) / "git-relay/git_relay"
    workflow = overlay(Path(before).read_bytes())
    provider = (package / "provider_scan.py").read_bytes()
    if digest(provider) != PROVIDER:
        raise ValueError("merged provider source drift")
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
    sources["git_relay/deploy_proof.py"] = (here / "runtime_proof.py").read_bytes()
    metadata = {"uid": 0, "gid": 0, "mode": 420}
    manifest = {
        "config": CONFIG,
        "files": [
            {
                "root": "code",
                "name": "workflow_verbs.py",
                "before": BEFORE,
                "after": AFTER,
                "meta": metadata,
            },
            {
                "root": "code",
                "name": "provider_scan.py",
                "before": None,
                "after": PROVIDER,
                "meta": metadata,
            },
        ],
        "runner": ROOT + "/runner.py",
        "unit": UNIT,
    }
    raw_manifest = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    entry = (here / "runner.template.py").read_text()
    entry = entry.replace("MANIFEST_DIGEST_PLACEHOLDER", digest(raw_manifest))
    entry = entry.replace(
        "MODULE_HASHES_PLACEHOLDER", repr({name: digest(raw) for name, raw in sources.items()})
    )
    ast.parse(entry)
    output = Path(output)
    output.mkdir(mode=0o700, exist_ok=False)
    for name, raw in sources.items():
        target = output / "runner-package" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    for name, raw in {
        "0.candidate": workflow,
        "1.candidate": provider,
        "manifest.json": raw_manifest,
        "runner.py": entry.encode(),
    }.items():
        (output / name).write_bytes(raw)
    return {
        "manifest": digest(raw_manifest),
        "runner": digest(entry.encode()),
        "images": [AFTER, PROVIDER],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.repo, args.before, args.output), sort_keys=True))
