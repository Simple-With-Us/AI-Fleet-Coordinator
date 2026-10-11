"""What is refused before anything is sent:  secrets, then personal data.

Both checks run over the filer's own words (question, options, context) after `text.clean_text`, and
a hit refuses the whole request with the field's name and the kind of thing found, never the text.
The tool's own strings (message ids, uuids, the Linear URL) are not checked:  they are not typed by
the filer.

The secret scan is F3's (`agent_sync/secretscan.py`).  The personal-data pass is the open-loops C3 list:
email addresses, phone numbers, runs of six or more digits and dollar amounts.  A decision that is
really about a price or a person can say so in words ("the monthly plan", "the contact") and link the
Linear issue;  it never has to carry the number.
"""
from __future__ import annotations

import re

from . import f3
from .errors import Refusal

_PII = (
    ("an email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    ("a run of six or more digits", re.compile(r"(?<![0-9A-Za-z])\d{6,}(?![0-9A-Za-z])")),  # not inside a hex id or a sha
    ("a phone number", re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![\w])")),
    ("a dollar amount", re.compile(r"\$\s?\d")),
)


def check_text(field: str, value: str) -> None:
    """Refuse `value` (the text of `field`) when it holds a secret or personal data."""
    if not value:
        return
    kind = f3.secretscan.scan(value)
    if kind:
        raise Refusal("%s looks like it holds a secret (%s).  Nothing was filed or sent.  Describe it without the value" % (field, kind))
    for label, pattern in _PII:
        if pattern.search(value):
            raise Refusal("%s holds %s.  Nothing was filed or sent.  Say it in words and link the Linear issue instead" % (field, label))


def check_fields(fields: dict[str, str | None]) -> None:
    for name, value in fields.items():
        if value:
            check_text(name, value)
