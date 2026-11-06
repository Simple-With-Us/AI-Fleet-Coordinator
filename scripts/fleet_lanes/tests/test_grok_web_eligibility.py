import unittest
from pathlib import Path
from .. import layout as L
from .. import lane as K

class GrokWebEligibility(unittest.TestCase):
    def test_real_identity_has_unique_lane_and_branch_without_allocating(self):
        registry=L.load_registry(Path(__file__).resolve().parents[3]/'fleet-apps.json')
        seat=K.resolve_seat({'AGENT_SEAT':'GROK-WEB'},registry)
        self.assertEqual(seat.name,'GROK-WEB')
        self.assertEqual(seat.suffix,'grok-web')
        self.assertEqual(K.branch_for(seat,'fixture'),'grok-web/fixture')
