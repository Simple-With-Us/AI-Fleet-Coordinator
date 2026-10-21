"""Guard: the caller template in docs/PR-AUTOFIX.md must grant every scope and
map every secret the pr-autofix reusable declares.

The reusable's token is capped by the caller job's permissions block and it can
only read secrets the caller maps, so a template that drifts behind the
reusable fails every caller copied from it.  Plain-text parsing on purpose:
the template lives inside a Markdown code fence, not a loadable workflow.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "PR-AUTOFIX.md"
REUSABLE = ROOT / ".github" / "workflows" / "pr-autofix-reusable.yml"

REQUIRED_SCOPES = {
    "contents": "write",
    "pull-requests": "read",
    "issues": "write",
    "actions": "read",
}


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def template_yaml():
    text = DOC.read_text()
    for fence in re.findall(r"```(?:yaml|yml)?\n(.*?)```", text, re.DOTALL):
        if "pr-autofix-reusable.yml@<sha>" in fence:
            return fence
    fail("docs/PR-AUTOFIX.md has no caller template fence naming pr-autofix-reusable.yml@<sha>")


def block_lines(yaml, header):
    """Lines of the first <header>: block at any indent, by indentation."""
    lines = yaml.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)" + re.escape(header) + r":\s*$", line)
        if m:
            indent = len(m.group(1))
            out = []
            for rest in lines[i + 1 :]:
                if rest.strip() and len(rest) - len(rest.lstrip()) <= indent:
                    break
                out.append(rest)
            return out
    return []


def mapping(block):
    """First-level keys only: the block's own entries, not nested values."""
    out = {}
    first_indent = None
    for line in block:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if first_indent is None:
            first_indent = indent
        if indent != first_indent:
            continue
        m = re.match(r"^\s*([A-Za-z_-]+):(?:\s*(\S+))?", line)
        if m:
            out[m.group(1)] = m.group(2) or ""
    return out


def main():
    tpl = template_yaml()
    perms = mapping(block_lines(tpl, "permissions"))
    secrets = mapping(block_lines(tpl, "secrets"))
    for scope, level in REQUIRED_SCOPES.items():
        got = perms.get(scope)
        if got != level:
            fail(f"template permissions grants {scope}: {got!r}, need {level!r}")

    reusable = REUSABLE.read_text()
    declared = mapping(block_lines(reusable, "secrets"))
    if not declared:
        fail("could not parse the reusable's secrets: block")
    missing = sorted(set(declared) - set(secrets))
    if missing:
        fail(f"template secrets: block does not map reusable secrets: {', '.join(missing)}")

    print(
        f"ok: template grants {len(REQUIRED_SCOPES)} scopes and maps all "
        f"{len(declared)} reusable secrets"
    )


if __name__ == "__main__":
    main()
