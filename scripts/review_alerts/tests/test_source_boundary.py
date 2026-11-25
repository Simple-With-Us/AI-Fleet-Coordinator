import unittest

from review_alerts import github_read
from review_alerts.source_boundary import snapshot


class Boundary(unittest.TestCase):
    def test_full_source_census_and_incomplete_read_refusal(self):
        class Reader:
            def pull(self, *args):
                return dict(state="open", head=dict(sha="head"))

            def reviews(self, *args):
                return [dict(id=10), dict(id=20)]

            def check_runs(self, *args):
                return [dict(id=30)]

        reader = Reader()
        self.assertEqual(snapshot(reader, "owner/repo", 12, 1000)["review_floor"], 20)

        def incomplete(*args):
            raise github_read.GithubReadUncertain("fixture")

        reader.reviews = incomplete
        with self.assertRaises(github_read.GithubReadUncertain):
            snapshot(reader, "owner/repo", 12, 1000)
