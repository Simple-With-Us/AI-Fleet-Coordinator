# Historical transaction fixture

workflow-1549.py.txt is the exact workflow_verbs.py blob from AFC merge
commit ef25c94bb1b03c685bff2d1f42f547ab8b48fa5f, SHA256
33c9c8c35c2eed3af01a43b810e3672ea85f3418d1ba45f7eabc182040a2fc1b.
It is immutable evidence for the completed1549 transaction tests, not importable
runtime code or a candidate for a future deployment. Tests must not compare
current evolving workflow source to the historical pin. The builder itself
still rejects any live/candidate bytes differing from its fixed before/after.
