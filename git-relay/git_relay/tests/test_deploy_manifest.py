import copy
import hashlib
import json
import unittest

from git_relay.deploy_manifest import finalize
from git_relay.deploy_transaction import TransactionError
from git_relay.patch_policy import digest, prepare


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.code = {
            n: n.encode()
            for n in [
                "workflow_verbs.py",
                "policy.py",
                "patch_policy.py",
                "provenance_patch.py",
                "patch_secretscan.py",
            ]
        }
        self.policy = {
            f"seat{i}": {"actions": ["wf_file_put"], "private_marker": i} for i in range(41)
        }
        self.raw = json.dumps(self.policy).encode()
        self.prep = {
            "config": "config-digest",
            "source_commit": "23acbe1cb3a04737caece3602c022f0a534a2aca",
            "files": [
                {
                    "root": "code",
                    "name": n,
                    "before": None,
                    "after": hashlib.sha256(b).hexdigest(),
                    "meta": {"uid": 0, "gid": 0, "mode": 0o644},
                }
                for n, b in self.code.items()
            ],
            "policy_raw_before": hashlib.sha256(self.raw).hexdigest(),
            "policy_semantic_before": digest(self.policy),
            "policy_semantic_after": digest(prepare(self.policy, digest(self.policy))),
        }

    def test_exact_final_shape_digest_and_preserved_41_private_rows(self):
        raw, sha, candidates = finalize(self.prep, self.code, self.raw)
        self.assertEqual(sha, hashlib.sha256(raw).hexdigest())
        doc = json.loads(raw)
        self.assertEqual(set(doc), {"config", "files", "runner", "unit"})
        self.assertEqual(len(doc["files"]), 6)
        self.assertEqual(len(candidates), 6)
        policy = json.loads(candidates[-1])
        for seat, row in self.policy.items():
            self.assertEqual(policy[seat]["private_marker"], row["private_marker"])
            self.assertEqual(policy[seat]["actions"], row["actions"] + ["wf_file_patch"])

    def test_drift_extra_missing_noop_and_wrong_source_refused(self):
        for kind in ["extra", "missing", "noop", "source", "metadata"]:
            prep = copy.deepcopy(self.prep)
            if kind == "extra":
                prep["extra"] = True
            if kind == "missing":
                prep["files"].pop()
            if kind == "noop":
                prep["files"][0]["before"] = prep["files"][0]["after"]
            if kind == "source":
                prep["source_commit"] = "0" * 40
            if kind == "metadata":
                prep["files"][0]["meta"]["mode"] = 0o777
            with self.subTest(kind=kind), self.assertRaises(TransactionError):
                finalize(prep, self.code, self.raw)
        with self.assertRaises(TransactionError):
            finalize(self.prep, self.code, self.raw + b" ")
        code = dict(self.code)
        code["policy.py"] = b"different"
        with self.assertRaises(TransactionError):
            finalize(self.prep, code, self.raw)
