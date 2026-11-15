"""Fail-closed proof placeholder pending qualified private own-seat routing.

This module deliberately holds no host inventory, user paths, host keys or secret
loader. It cannot connect or qualify a deployment. Restoring an executable proof
requires separately reviewed destination provenance and scoped secret retrieval.
"""

import os
import signal
import subprocess

QUALIFIED = False


def run_bounded(argv, *, input, capture_output, timeout, env):
    """Reap the whole local transport group on timeout, not only runuser."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def prove(hashes, run=run_bounded):
    """No subprocess or credential access until a qualified adapter exists."""
    return False


def make_proof():
    return lambda identity, hashes: prove(hashes)
