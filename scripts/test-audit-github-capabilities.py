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


if __name__ == "__main__":
    unittest.main()
