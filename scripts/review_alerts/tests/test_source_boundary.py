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

            def review_comments(self, *args):
                return [dict(id=40), dict(id=45)]

        reader = Reader()
        boundary = snapshot(reader, "owner/repo", 12, 1000)
        self.assertEqual(boundary["review_floor"], 20)
        self.assertEqual(boundary["comment_floor"], 45)

        def incomplete(*args):
            raise github_read.GithubReadUncertain("fixture")

        reader.reviews = incomplete
        with self.assertRaises(github_read.GithubReadUncertain):
            snapshot(reader, "owner/repo", 12, 1000)

    def test_incomplete_findings_census_refuses(self):
        class Reader:
            def pull(self, *args):
                return dict(state="open", head=dict(sha="head"))

            def reviews(self, *args):
                return []

            def check_runs(self, *args):
                return []

            def review_comments(self, *args):
                raise github_read.GithubReadUncertain("fixture")

        with self.assertRaises(github_read.GithubReadUncertain):
            snapshot(Reader(), "owner/repo", 12, 1000)

    def test_empty_findings_census_has_zero_comment_floor(self):
        class Reader:
            def pull(self, *args):
                return dict(state="open", head=dict(sha="head"))

            def reviews(self, *args):
                return []

            def check_runs(self, *args):
                return []

            def review_comments(self, *args):
                return []

        self.assertEqual(snapshot(Reader(), "owner/repo", 12, 1000)["comment_floor"], 0)
