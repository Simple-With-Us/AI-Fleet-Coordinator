import importlib.util
import unittest

s = importlib.util.spec_from_file_location("audit", "scripts/audit-github-capabilities.py")
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class Inventory(unittest.TestCase):
    def test_routes_normalize_repo_without_claiming_full_support(self):
        self.assertEqual(
            m.route_candidates("/repos/{}/pulls/{}?per_page=100"), "/repos/{}/{}/pulls/{}"
        )

    def test_conservative_statuses_preserve_unmatched_endpoint(self):
        spec = {
            "paths": {
                "/repos/{owner}/{repo}/pulls/{pull_number}": {
                    "get": {"operationId": "pulls/get", "tags": ["pulls"]}
                },
                "/unknown": {"get": {"operationId": "new/get", "tags": ["new"]}},
            }
        }
        x = m.inventory(spec, [])
        self.assertEqual(len(x["operations"]), 2)
        byid = {o["operation_id"]: o for o in x["operations"]}
        self.assertEqual(byid["new/get"]["status"], "no-direct-route-evidence")
        self.assertIn("not supported", x["classification_note"])

    def test_no_supported_claim_from_candidates(self):
        x = m.inventory({"paths": {}}, [])
        self.assertEqual(x["operations"], [])
        self.assertIn("GraphQL", x["classification_note"])

    def test_missing_paths_fails_explicitly_instead_of_publishing_empty_coverage(self):
        for spec in ({}, {"paths": []}, None):
            with self.assertRaises(ValueError):
                m.inventory(spec, [])

    def test_malformed_path_entries_and_empty_tags_are_safe(self):
        x = m.inventory(
            {"paths": {"/bad": None, "/empty": {"get": {"operationId": "test/get", "tags": []}}}},
            [],
        )
        self.assertEqual(len(x["operations"]), 1)
        self.assertEqual(x["operations"][0]["category"], "other")


if __name__ == "__main__":
    unittest.main()
