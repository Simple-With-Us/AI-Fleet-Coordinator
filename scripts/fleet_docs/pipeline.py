"""Refresh one authoritative library after the existing authorized Git sweep."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from fleet_lanes.code_main_sync import run_child, state_directory

from . import snapshot

REMOTE_COMMAND = (
    '/usr/bin/python3 -I -B "$HOME/.local/share/code-mirrors/scripts/fleet_docs/__main__.py"'
)


class ConfigurationRefused(snapshot.Refused):
    """A validated source identity and fixed remediation, never provider diagnostics."""

    def __init__(self, reason, source=None):
        super().__init__(reason)
        self.source = source


def settings():
    from fleet_knobs import knobs
    from fleet_lanes.code_mirror_credentials import settings_read_binding

    with settings_read_binding():
        knobs.refresh()
        enabled = knobs.resolve("DOCS_SYNC_ENABLED")
        snapshot.require(
            enabled.source in ("infisical", "lkg") and type(enabled.value) is bool,
            "trusted-docs-configuration-unavailable",
        )
        if enabled.value is not True:
            return None
        configuration = knobs.resolve("DOCS_SYNC_CONFIGURATION")
    snapshot.require(
        enabled.source in ("infisical", "lkg") and configuration.source in ("infisical", "lkg"),
        "trusted-docs-configuration-unavailable",
    )
    return parse_configuration(configuration.value)


def parse_configuration(raw):
    snapshot.require(isinstance(raw, str) and len(raw) <= 16384, "invalid-docs-configuration")
    value = json.loads(raw)
    snapshot.require(
        isinstance(value, dict)
        and set(value) == {"peer", "actor", "private_docs", "remote_sources"},
        "invalid-docs-configuration",
    )
    snapshot.require(
        isinstance(value["actor"], str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", value["actor"]),
        "invalid-docs-actor",
    )
    snapshot.require(
        isinstance(value["peer"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", value["peer"]),
        "invalid-docs-peer",
    )
    c = snapshot.collector()
    optins = value["private_docs"]
    snapshot.require(
        isinstance(optins, list)
        and len(optins) == len(set(optins))
        and all(
            isinstance(item, str) and item.casefold() in c.VERIFIED_PRIVATE_DOC_REPOS
            for item in optins
        ),
        "invalid-private-docs-opt-in",
    )
    sources = value["remote_sources"]
    snapshot.require(isinstance(sources, list) and len(sources) <= 2, "invalid-remote-docs-profile")
    seen = set()
    for source in sources:
        snapshot.require(
            isinstance(source, dict)
            and set(source) == {"owner_repo", "repository_id", "visibility"},
            "invalid-remote-docs-profile",
        )
        name = source["owner_repo"]
        snapshot.require(
            isinstance(name, str)
            and name.casefold() in c.VERIFIED_PRIVATE_DOC_REPOS
            and name in optins
            and name not in seen
            and type(source["repository_id"]) is int
            and source["repository_id"] > 0
            and source["visibility"] == "private",
            "unapproved-remote-docs-profile",
        )
        seen.add(name)
    return value


@contextmanager
def lifetime_lock(state):
    with state_directory(state, private=True) as state_fd:
        fd = os.open(
            "pipeline.lock",
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=state_fd,
        )
        try:
            info = os.fstat(fd)
            snapshot.require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600,
                "unsafe-pipeline-lock",
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise snapshot.Refused("docs-pipeline-already-running") from None
            yield
        finally:
            os.close(fd)


def load_exporter():
    source = Path(__file__).resolve().parents[1] / "export-repo-git-source.py"
    spec = importlib.util.spec_from_file_location("_fleet_docs_exporter", source)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    return exporter


def child_relay_environment(configuration, inherited=None):
    """Bind the explicit trusted profile actor only in a child; never override a declaration."""
    env = dict(os.environ if inherited is None else inherited)
    actor = configuration.get("actor")
    snapshot.require(
        isinstance(actor, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{0,31}", actor),
        "docs-actor-required",
    )
    launcher = (env.get("AGENT_LAUNCHER") or "").strip()
    launch_seat = (env.get("AGENT_LAUNCH_SEAT") or "").strip().upper()
    declared = [
        (env.get(key) or "").strip().upper()
        for key in ("AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG")
    ]
    snapshot.require(
        bool(launcher) == bool(launch_seat)
        and all(not value or value == actor for value in declared),
        "docs-actor-conflicts-with-inherited-assignment",
    )
    if not any(declared):
        env["AGENT_SEAT"] = actor
    return env


def prepare_remote_sources(configuration, stage):
    if not configuration["remote_sources"]:
        return []
    exporter = load_exporter()
    child_env = child_relay_environment(configuration)
    actor = exporter.verify_managed_route(child_env)
    snapshot.require(actor == configuration["actor"], "docs-actor-whoami-mismatch")

    def call(tool, arguments):
        return exporter.relay_read(tool, arguments, environ=child_env)

    descriptors = []
    receipts = []
    for configured in configuration["remote_sources"]:
        name = configured["owner_repo"]
        repo = call("gr_wf_repo_get", {"repo": name})["repository"]
        snapshot.require(
            repo.get("full_name") == name
            and repo.get("id") == configured["repository_id"]
            and repo.get("visibility") == configured["visibility"]
            and repo.get("private") is True,
            "remote-docs-identity-mismatch",
        )
        branch = call("gr_wf_branch_get", {"repo": name, "branch": repo["default_branch"]})[
            "branch"
        ]
        commit = branch["commit"]["sha"]
        exported = exporter.remote.export_snapshot(call, name, configured["visibility"], commit)
        snapshot.require(
            exported.identity["repository_id"] == configured["repository_id"],
            "remote-docs-id-rebound",
        )
        owner, repository = name.split("/", 1)
        path = stage / (repository + ".json")
        c = snapshot.collector()
        info = c.RepoInfo(
            path, owner, repository, exported.identity["source_ref"], commit, exported
        )
        selected, _, _ = c._select_blobs(
            info, c._parse_tree(info), include_private_audits=name in configuration["private_docs"]
        )
        exported.require_selected(set(selected))
        exporter.remote.write_descriptor(exported, path)
        descriptors.append((path, name))
        receipts.append(
            {
                "actor": actor,
                "owner_repo": name,
                "repository_id": configured["repository_id"],
                "source_commit": commit,
                "selected_blobs": len(selected),
                "descriptor_sha256": exporter.remote.digest(json.loads(path.read_text())),
            }
        )
    fd = os.open(
        stage / "relay-proof.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "wb") as output:
        output.write(snapshot.canonical(receipts))
    return descriptors


def collection_batches(code_root, configuration, descriptors):
    c = snapshot.collector()
    local = c.discover_repositories(code_root)[0]
    batches = []
    public = []
    for repo in local:
        identity = repo.owner + "/" + repo.name
        optin = next(
            (
                name
                for name in configuration["private_docs"]
                if canonical_identity(name) == canonical_identity(identity)
            ),
            None,
        )
        if optin is None:
            public.append(repo)
        else:
            batches.append(({"repo_paths": [repo.path], "private_docs_repo": optin}, ([repo], [])))
    if public:
        batches.insert(0, ({"repo_paths": [repo.path for repo in public]}, (public, [])))
    for path, identity in descriptors:
        discovered = c.discover_repositories(
            None, git_source_descriptors=[path], private_docs_repo=identity
        )
        batches.append(({"private_docs_repo": identity}, discovered))
    return batches


def canonical_identity(owner_repo):
    from fleet_lanes.code_main_sync import CANONICAL_OWNERS

    owner, name = owner_repo.split("/", 1)
    return CANONICAL_OWNERS.get(owner.casefold(), owner).casefold() + "/" + name.casefold()


def validate_existing_profile(root, configuration, batches):
    """Preserve the already approved private opt-ins and refuse silently stale collections."""
    c = snapshot.collector()
    identity = canonical_identity

    identities = {
        identity(repo.owner + "/" + repo.name)
        for _, discovered in batches
        for repo in discovered[0]
    }
    manifests = {}
    for path in root.glob("*/manifest.json"):
        value = c.parse_existing_manifest(snapshot.read_file(path), path.parent.name)
        source_identity = identity(value["owner_repo"])
        manifests[source_identity] = value
        if source_identity not in identities:
            raise ConfigurationRefused("docs-source-profile-incomplete", value["owner_repo"])
        optin = value.get("private_docs_opt_in")
        if optin is not None and not any(
            identity(optin) == identity(configured) for configured in configuration["private_docs"]
        ):
            raise ConfigurationRefused("existing-private-opt-in-missing", value["owner_repo"])
    for optin in configuration["private_docs"]:
        previous = manifests.get(identity(optin))
        if not (
            previous is not None
            and previous.get("private_docs_opt_in") is not None
            and identity(previous["private_docs_opt_in"]) == identity(optin)
        ):
            raise ConfigurationRefused("new-private-opt-in-not-authorized-by-library", optin)


def peer_status(peer, run=run_child):
    result = run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            peer,
            REMOTE_COMMAND + " status",
        ],
        capture_output=True,
        timeout=45,
        check=False,
    )
    snapshot.require(
        result.returncode == 0 and len(result.stdout) <= snapshot.MAX_HEADER,
        "docs-peer-unavailable",
    )
    value = json.loads(result.stdout)
    snapshot.require(
        isinstance(value, dict) and value.get("state") in ("empty", "ready"), "docs-peer-not-ready"
    )
    return value


def replicate(root, desired, configuration, *, status=peer_status, runner=run_child):
    previous = status(configuration["peer"])
    if previous.get("sha256") == desired["sha256"]:
        return {
            "state": "current",
            "sha256": desired["sha256"],
            "files": len(desired["files"]),
            "bytes": desired["bytes"],
        }
    with tempfile.TemporaryFile() as payload:
        snapshot.write_snapshot(root, desired, payload, previous.get("sha256"))
        payload.seek(0)
        # The payload is private disk-backed staging and stdin, never argv or logs.
        try:
            result = runner(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "ConnectTimeout=10",
                    configuration["peer"],
                    REMOTE_COMMAND + " receive",
                ],
                stdin=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            raise snapshot.Refused("docs-peer-transfer-timeout") from None
    output = result.stdout or b""
    snapshot.require(result.returncode == 0 and len(output) <= 4096, "docs-peer-transfer-failed")
    receipt = json.loads(output)
    snapshot.require(
        isinstance(receipt, dict)
        and receipt.get("state") == "synced"
        and receipt.get("sha256") == desired["sha256"]
        and receipt.get("files") == len(desired["files"])
        and receipt.get("bytes") == desired["bytes"],
        "docs-peer-receipt-mismatch",
    )
    return receipt


def refresh(code_root, state, configuration, *, remote=prepare_remote_sources, transfer=replicate):
    c = snapshot.collector()
    root = code_root / "Docs"
    with (
        lifetime_lock(state),
        tempfile.TemporaryDirectory(prefix=".docs-sources-", dir=state) as scratch,
    ):
        descriptors = remote(configuration, Path(scratch))
        remote_proof_path = Path(scratch) / "relay-proof.json"
        remote_proof = (
            json.loads(snapshot.read_file(remote_proof_path)) if remote_proof_path.exists() else []
        )
        batches = collection_batches(code_root, configuration, descriptors)
        snapshot.require(batches, "no-docs-sources")
        for options, discovered in batches:
            for repo in discovered[0]:
                snapshot.require(
                    not c._paths_overlap(root, repo.path), "docs-source-output-overlap"
                )
        # One existing collector lock spans every batch, snapshot construction and transfer.
        with c._collection_lock(root, exclusive=True):
            validate_existing_profile(root, configuration, batches)
            for options, discovered in batches:
                c._collect_unlocked(None, root, resolved_repositories=discovered, **options)
            desired = snapshot.validate_generated(root)
            source_commits = {
                path.parent.name: json.loads(snapshot.read_file(path))["source_commit"]
                for path in root.glob("*/manifest.json")
            }
            receipt = transfer(root, desired, configuration)
    receipt["remote_sources"] = remote_proof
    receipt["source_commits"] = source_commits
    return receipt


def record(state, receipt):
    with state_directory(state, private=True) as state_fd:
        previous = None
        try:
            fd = os.open(
                "receipt.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=state_fd
            )
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                snapshot.require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_size <= 1024 * 1024,
                    "unsafe-docs-receipt",
                )
                previous = json.loads(source.read(1024 * 1024 + 1))
        receipt = {
            **receipt,
            "docs_exit": int(receipt["state"] == "unavailable"),
            "checked_epoch": time.time(),
        }
        snapshot.atomic_write(
            state, "receipt.json", snapshot.canonical(receipt), directory_fd=state_fd
        )
    changed = previous is None or (previous.get("state"), previous.get("reason")) != (
        receipt.get("state"),
        receipt.get("reason"),
    )
    if changed and receipt["state"] == "unavailable":
        print("Docs sync state=unavailable; next mirror cycle will retry", file=sys.stderr)
    state_name = (
        receipt["state"]
        if receipt["state"] in {"current", "synced", "disabled", "held", "unavailable"}
        else "unavailable"
    )
    reason = "none"
    if state_name == "held":
        reason = "git"
    elif state_name == "unavailable":
        detail = receipt.get("reason", "")
        reason = (
            "peer"
            if "peer" in detail
            else "configuration"
            if any(
                word in detail
                for word in ("configuration", "profile", "opt-in", "assigned", "managed")
            )
            else "library"
            if any(
                word in detail
                for word in ("library", "generated", "archive", "transaction", "snapshot")
            )
            else "runtime"
        )
    print(f"docs: state={state_name} reason={reason} changed={int(changed)}", flush=True)
    return receipt


def run(mirror_command, *, read=settings, update=refresh, state=None, code_root=None):
    from fleet_lanes.code_main_sync import run_sweep_command

    git_status = run_sweep_command(mirror_command)
    state = state or Path.home() / ".local/state/code-docs-sync"
    if git_status:
        try:
            record(state, {"state": "held", "reason": "git-sweep-failed"})
        except Exception:
            pass
        return git_status
    try:
        try:
            configuration = read()
        except Exception:
            raise ConfigurationRefused("trusted-docs-configuration-unavailable") from None
        receipt = (
            {"state": "disabled"}
            if configuration is None
            else update(
                code_root
                or Path(os.environ.get("FLEET_CODE_ROOT") or Path.home() / "Code").resolve(),
                state,
                configuration,
            )
        )
    except Exception as error:
        reason = str(error) if isinstance(error, snapshot.Refused) else "docs-refresh-failed"
        receipt = {"state": "unavailable", "reason": reason}
        if isinstance(error, ConfigurationRefused):
            receipt.update(
                setting="DOCS_SYNC_CONFIGURATION",
                source=error.source,
                remediation="restore-configured-source-or-review-preservation-plan",
            )
    try:
        record(state, receipt)
    except Exception:
        print("Docs sync receipt unavailable; next mirror cycle will retry", file=sys.stderr)
        print("docs: state=unavailable reason=runtime changed=1", flush=True)
    # A Docs outage never changes Git's result; its separate receipt remains actionable.
    return git_status
