import hashlib
import os
import stat
import sys
from pathlib import Path

DIRECTORY = Path("/opt/git-relay/transactions/workflow-1549")
MANIFEST_DIGEST = "MANIFEST_DIGEST_PLACEHOLDER"
MODULE_HASHES = {}  # MODULE_HASHES_PLACEHOLDER


def main():
    if os.getuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in {"deploy", "guard", "recover"}:
        raise SystemExit("fixed root transaction command required")
    if len(MANIFEST_DIGEST) != 64 or any(c not in "0123456789abcdef" for c in MANIFEST_DIGEST):
        raise SystemExit("transaction is not qualified")
    for p in [DIRECTORY, *DIRECTORY.parents]:
        s = p.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise SystemExit("unsafe runner anchor")
    package = DIRECTORY / "runner-package"
    for name, digest in MODULE_HASHES.items():
        p = package / name
        for parent in [p.parent, package]:
            s = parent.lstat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
                raise SystemExit("unsafe package anchor")
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(f.fileno())
            if (
                not stat.S_ISREG(s.st_mode)
                or s.st_nlink != 1
                or s.st_uid != 0
                or s.st_mode & 0o022
                or hashlib.sha256(f.read()).hexdigest() != digest
            ):
                raise SystemExit("runner module drift")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(package))
    from git_relay.deploy_proof import RuntimeProof
    from git_relay.deploy_runner import Runner
    from git_relay.deploy_transaction import read_regular

    secret, meta = read_regular(DIRECTORY / "own-seat-proof-bearer")
    if meta != {"uid": 0, "gid": 0, "mode": 0o600}:
        raise SystemExit("unsafe proof credential")
    r = Runner(DIRECTORY, MANIFEST_DIGEST, RuntimeProof(secret.decode().strip()))
    getattr(r, sys.argv[1])()


if __name__ == "__main__":
    main()
