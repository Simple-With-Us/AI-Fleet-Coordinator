"""Build the fixed one-module preservation-pairs-2426 transaction in a scratch directory.

No service, root staging, credentials, installation or activation. The caller
must arrange separate review and qualified operator custody before activation.
The live config.env and seats.json policy digests are execution-time
observations: both are explicit required arguments, never hardcoded
placeholders, and the build refuses to run without them.

One root, one attempt. The engine keeps the supervisor journal of every attempt
as audit evidence and refuses a root whose journal already exists
(deploy_journal.Journal.arm), so a root admits exactly one attempt and a busy
freeze can never be retried in place. `--name` gives a retry a root and unit of
its own (`preservation-pairs-2426-a01`, `-a02`, ...); without it the build
targets the original root. Nothing else differs between attempt roots.
"""

import argparse
import ast
import hashlib
import json
import os
import re
from pathlib import Path

BEFORE = "3f72088feb8e69b29431fb8811a4a29d3c0e5c51cbed947de34cecf2783323a3"
AFTER = "97cac773211630c55e8bf507b47cd6ee6b7c2167505721388c73ad3849437fb0"
DEFAULT_NAME = "preservation-pairs-2426"
TRANSACTIONS = "/opt/git-relay/transactions"
NAME = re.compile(r"preservation-pairs-2426(?:-a[0-9]{2,3})?")
PROOF_TEMPLATE = "569da0fc4cb5b7c73e75e48d97868b7a426f9321c835b0614d7ac324fb178c85"
MODULES = {
    "__init__.py": "e9c5519a796eab24d04e416e5bd30598015b5a03c09141d856a39d898d92c423",
    "deploy_guard.py": "536787ce91394b13e11620785d3a781aaac8ab8f38d5e7fa25757d82eba0d262",
    "deploy_host.py": "10eac3f0d7ed7da6bca1ba9548c3296e31ed8fe97427c7cb529570336247b8ec",
    "deploy_journal.py": "bb39bbe7ced794771aa419c357509cf2a30bc719d68cdb5c29557f7cec74a2a0",
    "deploy_recovery.py": "e362ba890b0e19fa8fbd07a23096358bfbc8b29b5a9524da09546b4f4c5619d1",
    "deploy_runner.py": "36a9033e2678ae57b071c391d2dee954b4ec818419433c00d06eb7250954db72",
    "deploy_supervisor.py": "4d605bcaf528d517037f99edf6d10f496512bfc508f93e580d2045a3905c854d",
    "deploy_transaction.py": "a6bf527f0cf9946ea352c84372fb49df3682cd163f2c91d46ddf301521d50852",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def observed_digest(value, label):
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(label + " must be 64 lowercase hex characters")
    return value


def transaction_name(value):
    """(name, root, unit) for `--name`: the original root, or one numbered attempt root.

    The name is the only thing an operator chooses, so it is held to the one
    shape the engine's runner path and systemd unit rules both accept.
    """
    name = DEFAULT_NAME if value is None else value
    if not isinstance(name, str) or NAME.fullmatch(name) is None:
        raise ValueError("transaction name must be preservation-pairs-2426 or -aNN")
    return name, TRANSACTIONS + "/" + name, "git-relay-" + name + ".service"


def image(before, candidate):
    if digest(before) != BEFORE:
        raise ValueError("live preservation module baseline drift")
    if digest(candidate) != AFTER:
        raise ValueError("reviewed preservation module candidate drift")
    ast.parse(candidate.decode())
    return candidate


def render_proof(template, policy):
    """Bind the execution-time seats policy digest into the proof module."""
    if digest(template) != PROOF_TEMPLATE:
        raise ValueError("runtime proof template drift")
    if template.count(b"POLICY_SHA256_PLACEHOLDER") != 1:
        raise ValueError("proof template placeholder drift")
    rendered = template.replace(b"POLICY_SHA256_PLACEHOLDER", policy.encode())
    if b"PLACEHOLDER" in rendered:
        raise ValueError("proof template placeholder remains")
    text = rendered.decode()
    ast.parse(text)
    bindings = {}
    classes = [
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeProof"
    ]
    if len(classes) != 1:
        raise ValueError("proof class shape")
    for node in classes[0].body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("BASELINE", "CANDIDATE", "POLICY")
        ):
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                raise ValueError("proof binding shape")
            bindings[node.targets[0].id] = node.value.value
    if bindings != {"BASELINE": BEFORE, "CANDIDATE": AFTER, "POLICY": policy}:
        raise ValueError("proof binding drift")
    return rendered


def private_dir(path):
    if path.is_symlink():
        raise ValueError("scratch package symlink refused")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("scratch package must be owner-only")


def write_file(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(raw)


def render_entry(template, raw_manifest, sources, root):
    module_hashes = {name: digest(raw) for name, raw in sources.items()}
    if not module_hashes:
        raise ValueError("runner module hashes are empty")
    manifest_placeholder = "MANIFEST_DIGEST_PLACEHOLDER"
    module_placeholder = "MODULE_HASHES = {}  # MODULE_HASHES_PLACEHOLDER"
    root_placeholder = "ROOT_PATH_PLACEHOLDER"
    for marker in (manifest_placeholder, module_placeholder, root_placeholder):
        if template.count(marker) != 1:
            raise ValueError("runner template placeholder drift")
    entry = (
        template.replace(manifest_placeholder, digest(raw_manifest))
        .replace(module_placeholder, "MODULE_HASHES = " + repr(module_hashes))
        .replace(root_placeholder, root)
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
        "DIRECTORY": root,
    }:
        raise ValueError("runner binding drift")
    return entry


def build(repo, before, output, config, policy, attempt=None):
    _name, root, unit = transaction_name(attempt)
    here = Path(__file__).resolve().parent
    package = Path(repo) / "git-relay/git_relay"
    module = image(
        Path(before).read_bytes(),
        (package / "reviewed_preservation.py").read_bytes(),
    )
    sources = {}
    for filename, expected in MODULES.items():
        raw = (package / filename).read_bytes()
        if digest(raw) != expected:
            raise ValueError("finite recovery module drift: " + filename)
        if filename == "deploy_runner.py":
            if raw.count(b"git-relay-patch-1429.service") != 1:
                raise ValueError("runner unit anchor drift")
            raw = raw.replace(b"git-relay-patch-1429.service", unit.encode())
        sources["git_relay/" + filename] = raw
    proof = render_proof((here / "runtime_proof.py").read_bytes(), policy)
    sources["git_relay/deploy_proof.py"] = proof
    metadata = {"uid": 0, "gid": 0, "mode": 420}
    manifest = {
        "config": config,
        "files": [
            {
                "root": "code",
                "name": "reviewed_preservation.py",
                "before": BEFORE,
                "after": AFTER,
                "meta": metadata,
            },
        ],
        "runner": root + "/runner.py",
        "unit": unit,
    }
    raw_manifest = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    entry = render_entry((here / "runner.template.py").read_text(), raw_manifest, sources, root)
    output = Path(output)
    output.mkdir(mode=0o700, exist_ok=False)
    for name, raw in sources.items():
        target = output / "runner-package" / name
        private_dir(target.parent.parent)
        private_dir(target.parent)
        write_file(target, raw)
    for name, raw in {
        "0.candidate": module,
        "manifest.json": raw_manifest,
        "runner.py": entry.encode(),
    }.items():
        write_file(output / name, raw)
    return {
        "manifest": digest(raw_manifest),
        "runner": digest(entry.encode()),
        "proof": digest(proof),
        "images": [AFTER],
        "root": root,
        "unit": unit,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--config-sha256",
        required=True,
        help="execution-time sha256 of /etc/git-relay/config.env (digest only)",
    )
    parser.add_argument(
        "--policy-sha256",
        required=True,
        help="execution-time sha256 of the physical seats.json policy file (digest only)",
    )
    parser.add_argument(
        "--name",
        help="attempt root: preservation-pairs-2426 (default) or preservation-pairs-2426-aNN; "
        "a root admits one attempt, so every retry builds under a new name",
    )
    args = parser.parse_args()
    config = observed_digest(args.config_sha256, "config digest")
    policy = observed_digest(args.policy_sha256, "policy digest")
    print(
        json.dumps(
            build(args.repo, args.before, args.output, config, policy, args.name),
            sort_keys=True,
        )
    )
