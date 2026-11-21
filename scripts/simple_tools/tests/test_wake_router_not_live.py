"""`not-live` (a pre-update Mac's GROK wake answer, PR 2306) settles delivered: false.

Split out of test_wake_router.SeatNeverRan so the relay's per-file publish scan does not
refuse the whole router suite over its planted must-never-travel canary env values;  the
harness (Base) is imported, not copied.
"""

from simple_tools.tests.test_wake_router import Base
from simple_tools import wake_router as WR
from simple_tools.tests.test_wake_router import SID_A, row


class NotLiveNeverRan(Base):
    """The GROK platform's pre-update answer must read as never ran, end to end."""

    def test_a_not_live_answer_settles_delivered_false(self) -> None:
        self.assertNotIn("not-live", WR.NEVER_RAN)
        self.registry.rows = [row(SID_A, "AA", "Only chat")]
        self.results = [WR.Delivery("refused", code="not-live")]
        router = self.router()
        router.never_ran = WR.NEVER_RAN | frozenset(("not-live",))
        wake_id = "w-not-live"
        router.accept(self.wake(wake_id))
        self.drain(router)
        self.assertIs(self.done(wake_id)["delivered"], False)
