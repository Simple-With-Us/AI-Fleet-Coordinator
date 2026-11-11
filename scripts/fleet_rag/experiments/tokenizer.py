"""Identifier-aware tokenizer for the BM25 experiment.

The fleet corpus is identifier-heavy: env var names (SERVICE_PASSWORD), error strings
(EADDRINUSE), board ids (32-char hex), file paths (docs/RAG-FLEET-INFRA.md), dotted
names (vm.swappiness).  A whitespace/punctuation tokenizer that shatters these into
single letters or drops them at word-boundaries would defeat the whole point of trying
BM25 here, so this tokenizer treats `.` `_` `/` `-` as *identifier glue*, not as word
separators to throw away.
"""
from __future__ import annotations

import re

# A "compound" is a run of alnum chunks glued by . _ / - : SERVICE_PASSWORD, EADDRINUSE
# (no glue -- stays one chunk), af4d7bb6998445f7a25cfe5c8bc97b2a (hex id), vm.swappiness,
# docs/RAG-FLEET-INFRA.md, PINECONE_TRIAL_ENDS_AT.
_COMPOUND_RE = re.compile(r"[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*")
_GLUE_RE = re.compile(r"[._/-]")

# Same short stopword list core.query_terms already filters for the production keyword
# leg, so the two tokenizers agree on what counts as noise.
STOPWORDS = frozenset("""
a an and are as at be but by for from has have how i if in into is it its of on or
that the their there these this to was what when where which who why will with you your
our we do does did not no yes can could should would may might must me my
""".split())


def tokenize(text: str, min_len: int = 2) -> list[str]:
    """Lowercase, extract compounds, and emit each compound as ONE atomic term.

    A compound that actually contains glue is ALSO split into its parts, and those
    parts are emitted as additional terms -- so a plain-English query for "password"
    still retrieves a chunk that only ever spells it as SERVICE_PASSWORD, while a query
    that names SERVICE_PASSWORD outright still gets the exact, rare, high-IDF compound
    term.  A compound with no glue (EADDRINUSE, a bare hex id) needs no decomposition:
    splitting it would just reproduce the same single token, so it is not duplicated.

    No stemming -- identifiers are kept literal on purpose.  Tokens shorter than
    min_len or in STOPWORDS are dropped from both the compound and its parts.
    """
    out: list[str] = []
    for compound in _COMPOUND_RE.findall(text.lower()):
        parts = _GLUE_RE.split(compound)
        candidates = (compound,) if len(parts) == 1 else (compound, *parts)
        for tok in candidates:
            if len(tok) >= min_len and tok not in STOPWORDS:
                out.append(tok)
    return out
