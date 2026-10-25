"""The `guide` tool (AFC-331, docs/protocols/per-call-identity.md section 7):  the Agent-Sync tools guide,
whole or one `##` section.  The text is docs/protocols/simple-tools-guide.md, read from beside the
package (the repo root, or the server mirror's copy of it).  The hosted Worker bundles the same file at
build time and applies the same section rule (src/guide.js).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

GUIDE_RELPATH = ("docs", "protocols", "simple-tools-guide.md")
GUIDE_PATH = Path(__file__).resolve().parents[3].joinpath(*GUIDE_RELPATH)
ENV_GUIDE = "AGENT_SYNC_GUIDE"
GUIDE_URL = "https://fleetlink.online/simple-tools-guide/"


class GuideError(Exception):
    def __init__(self, code: str, message: str, sections: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.sections = sections or []


def read_text(env: Mapping[str, str] | None = None) -> str:
    override = (env if env is not None else os.environ).get(ENV_GUIDE)
    path = Path(override).expanduser() if override else GUIDE_PATH
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        raise GuideError(
            "not_found", "the guide is not on this host;  read %s" % GUIDE_URL
        ) from None


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, text)] for every `## ` section, each text from its heading line up to the next one.  A
    fenced code block never starts a section.  The part before the first heading is not a section."""
    sections: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        elif sections:
            sections[-1][1].append(line)
    return [(name, "\n".join(lines).rstrip() + "\n") for name, lines in sections]


def section_names(text: str) -> list[str]:
    return [name for name, _ in split_sections(text)]


def lookup(text: str, section: str | None) -> tuple[str, str | None, list[str]]:
    """(text to return, the section's heading or None for the whole guide, every section name).  A name that
    matches no section (case, spaces and punctuation ignored) raises GuideError listing the names."""
    names = section_names(text)
    if section is None or not section.strip():
        return text, None, names

    def key(value: str) -> str:
        return "".join(ch for ch in value.casefold() if ch.isalnum())

    wanted = key(section)
    for name, body in split_sections(text):
        if key(name) == wanted and wanted:
            return body, name, names
    raise GuideError(
        "not_found",
        "the guide has no section %s;  its sections are:  %s"
        % ('"%s"' % section[:60].replace('"', "'"), ";  ".join(names) or "none"),
        names,
    )
