"""The bridge's failure codes, in one place.

The launcher boots the bridge with runpy, so bridge.py runs as __main__ and
outbound.py imports it by its package name.  A class defined in one copy of
a module is a different class in the other copy, so the handlers in main()
would never match an error raised in outbound.py.  The codes live here, so
every copy of the bridge shares one class.
"""


class BridgeError(Exception):
    """Fixed, content-free failure code."""


class StopBridge(BridgeError):
    pass
