Historical source fixture: finite recovery before freeze diagnostics v1

Test-only bytes recovered from commit 868036e36d4982529b382c6c3f5df3a1766b26d1.
The eight SHA-256 values match the unchanged MODULES maps in all four historical
transaction builders. The .txt suffix prevents accidental module discovery.
The test helper explicitly copies these bytes into a temporary source root.
It never imports them, chooses an automatic fallback or changes production pins.
The existing workflow-1549.py.txt fixture supplies the mocked image-gate input.
Current diagnostic source is separately tested, and each historical builder
must refuse that changed source before creating its output directory.
This fixture is not permission to execute or replay any historical transaction.
