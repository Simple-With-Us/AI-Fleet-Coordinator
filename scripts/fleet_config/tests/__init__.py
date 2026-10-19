"""Tests for ``fleet_config``:  the proof machinery and the collector dispatch.

Every test writes only inside a temporary directory it makes itself.  Nothing here
reads the real home, a real repo or a real lane, and no test runs the fleet's own
hook:  the L1 probe is proved against a fake hook script written into a temp dir.
"""
