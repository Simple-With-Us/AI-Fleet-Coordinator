"""Run the fixed scanner transaction's pure builder and transport tests in CI."""

import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parents[2] / "deploy/ct-match-pinned"


def load(name):
    spec = importlib.util.spec_from_file_location("ct_transaction_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BuilderTests = load("test_build").BuilderTests
OwnWiseProofTests = load("test_wise_runtime_proof").OwnWiseProofTests
