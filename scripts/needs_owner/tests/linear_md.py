"""What Linear hands back for a description it stored (observed Sat, Oct 10, 2026, copied from
scripts/linear-board/test_board.py):  `[`, `]` and `~` are escaped outside code spans, and `-` bullets
become `*` bullets.  Comment bodies come back as sent."""
import re


def linear_markdown(text):
    def esc(seg):
        return re.sub(r"([\[\]~])", r"\\\1", seg)
    out, pos = [], 0
    for m in re.finditer(r"(`+)(.+?)\1", text, re.S):
        out.append(esc(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(esc(text[pos:]))
    return re.sub(r"(?m)^(\s*)- ", r"\1* ", "".join(out))
