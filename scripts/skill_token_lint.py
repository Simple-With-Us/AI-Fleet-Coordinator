#!/usr/bin/env python3
"""Report skill markdown over a token budget, and paragraphs shared across skills.

Token estimate is the character count divided by four (integer division).  The
default budget is 2500 tokens.  Pass --budget N, or set SKILL_TOKEN_BUDGET, to
change it.  A paragraph that is the same or nearly the same in two different
skills is reported once.  Seat copies of one skill are one row, not a duplicate
of themselves.

The default exit status is 0 even when the report has findings, so CI can print
the table without blocking.  --strict exits 1 when a file is over budget, a
shared paragraph is found, or a skill file cannot be read.

    python3 scripts/skill_token_lint.py
    python3 scripts/skill_token_lint.py --budget 2500 --strict
    python3 scripts/skill_token_lint.py --format json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_BUDGET = 2500
CHARS_PER_TOKEN = 4
DEFAULT_MIN_PARAGRAPH_CHARS = 180
DEFAULT_SIMILARITY = 0.8
SHINGLE_SIZE = 3
SHINGLE_CAP = 120
MAX_POSTINGS = 200
MIN_SHARED_SHINGLES = 3
DEFAULT_MAX_GROUPS = 40
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".build", "dist", ".venv", "venv"})
FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$")


@dataclass(frozen=True)
class FileSize:
    path: str
    skill: str
    chars: int
    tokens: int
    over: bool


@dataclass(frozen=True)
class SkillSummary:
    skill: str
    max_tokens: int
    max_chars: int
    copies: int
    over: bool
    largest: str


@dataclass(frozen=True)
class SharedGroup:
    skills: tuple[str, ...]
    paths: tuple[str, ...]
    similarity: float
    sample: str
    chars: int


@dataclass(frozen=True)
class Paragraph:
    skill: str
    path: str
    sample: str
    normalized: str
    norm_chars: int
    shingles: frozenset[tuple[str, ...]]


@dataclass(frozen=True)
class Report:
    budget: int
    chars_per_token: int
    similarity: float
    min_paragraph_chars: int
    files: tuple[FileSize, ...]
    skills: tuple[SkillSummary, ...]
    shared: tuple[SharedGroup, ...]
    unreadable: tuple[str, ...]

    @property
    def has_findings(self) -> bool:
        over = any(row.over for row in self.files)
        return over or bool(self.shared) or bool(self.unreadable)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["has_findings"] = self.has_findings
        payload["file_count"] = len(self.files)
        payload["skill_count"] = len(self.skills)
        payload["over_budget_count"] = sum(1 for row in self.files if row.over)
        payload["shared_group_count"] = len(self.shared)
        return payload


def estimate_tokens(text: str, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    """Estimate tokens as len(text) // chars_per_token."""
    if chars_per_token < 1:
        raise ValueError("chars_per_token must be >= 1")
    return len(text) // chars_per_token


def default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_skill_markdown(path: Path) -> bool:
    """True for SKILL.md anywhere, and for any other .md under a skills directory."""
    if path.suffix != ".md":
        return False
    if path.name == "SKILL.md":
        return True
    return "skills" in path.parts


def skill_slug(path: Path) -> str:
    """Group seat copies of one skill by the directory that holds the file."""
    if path.name == "SKILL.md" or path.parent.name != "skills":
        return path.parent.name
    return path.stem


def skill_markdown_files(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if is_skill_markdown(path):
                found.append(path)
    return found


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def iter_blocks(text: str) -> list[str]:
    """Split prose on blank lines, and keep each fenced block as its own paragraph."""
    blocks: list[str] = []
    prose: list[str] = []
    code: list[str] = []
    fence: str | None = None

    def flush_prose() -> None:
        if prose:
            blocks.append("\n".join(prose))
            prose.clear()

    def flush_code() -> None:
        if code:
            blocks.append("\n".join(code))
            code.clear()

    for line in text.splitlines():
        if fence is None:
            opened = FENCE_OPEN.match(line)
            if opened:
                flush_prose()
                fence = opened.group(1)
                continue
            if line.strip() == "":
                flush_prose()
                continue
            prose.append(line)
            continue
        closed = FENCE_CLOSE.match(line)
        if closed and closed.group(1)[0] == fence[0] and len(closed.group(1)) >= len(fence):
            flush_code()
            fence = None
            continue
        code.append(line)
    if fence is None:
        flush_prose()
    else:
        flush_code()
    return blocks


def normalize(text: str) -> str:
    folded = text.replace("\u00a0", " ").replace("\u202f", " ").lower()
    folded = re.sub(r"[^a-z0-9\s]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def one_line(text: str, limit: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def make_shingles(
    words: list[str], size: int = SHINGLE_SIZE, cap: int = SHINGLE_CAP
) -> frozenset[tuple[str, ...]]:
    total = len(words) - size + 1
    if total <= 0:
        return frozenset()
    step = 1 if total <= cap else max(1, total // cap)
    shingles: list[tuple[str, ...]] = []
    for index in range(0, total, step):
        shingles.append(tuple(words[index : index + size]))
        if len(shingles) >= cap:
            break
    return frozenset(shingles)


def jaccard(left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    return intersection / (len(left) + len(right) - intersection)


def similarity_ratio(left: str, right: str) -> float:
    """Near-duplicate score in 0..1.  Identical normalized text scores 1."""
    a = normalize(left)
    b = normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return jaccard(make_shingles(a.split()), make_shingles(b.split()))


def _paragraphs_for(
    skill: str,
    path: str,
    text: str,
    min_paragraph_chars: int,
) -> list[Paragraph]:
    found: list[Paragraph] = []
    seen: set[str] = set()
    for block in iter_blocks(text):
        if len(block.strip()) < min_paragraph_chars:
            continue
        normalized = normalize(block)
        if len(normalized) < min_paragraph_chars // 2 or normalized in seen:
            continue
        seen.add(normalized)
        found.append(
            Paragraph(
                skill=skill,
                path=path,
                sample=one_line(block),
                normalized=normalized,
                norm_chars=len(normalized),
                shingles=make_shingles(normalized.split()),
            )
        )
    return found


def _cluster(paragraphs: list[Paragraph], similarity: float) -> list[SharedGroup]:
    count = len(paragraphs)
    parent = list(range(count))
    best: dict[int, float] = {}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def unite(left: int, right: int, score: float) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left == root_right:
            best[root_left] = max(best.get(root_left, score), score)
            return
        parent[root_right] = root_left
        best[root_left] = max(best.get(root_left, 0.0), best.pop(root_right, 0.0), score)

    by_norm: dict[str, list[int]] = defaultdict(list)
    for index, paragraph in enumerate(paragraphs):
        by_norm[paragraph.normalized].append(index)
    for indexes in by_norm.values():
        first_by_skill: dict[str, int] = {}
        for index in indexes:
            first_by_skill.setdefault(paragraphs[index].skill, index)
        ids = list(first_by_skill.values())
        if len(ids) < 2:
            continue
        for other in ids[1:]:
            unite(ids[0], other, 1.0)

    postings: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, paragraph in enumerate(paragraphs):
        for shingle in paragraph.shingles:
            postings[shingle].append(index)

    pair_score: dict[tuple[int, int], int] = defaultdict(int)
    for indexes in postings.values():
        if len(indexes) > MAX_POSTINGS:
            continue
        for left_at in range(len(indexes)):
            left = indexes[left_at]
            left_skill = paragraphs[left].skill
            for right_at in range(left_at + 1, len(indexes)):
                right = indexes[right_at]
                if paragraphs[right].skill == left_skill:
                    continue
                pair = (left, right) if left < right else (right, left)
                pair_score[pair] += 1

    for (left, right), shared in pair_score.items():
        if shared < MIN_SHARED_SHINGLES:
            continue
        if paragraphs[left].normalized == paragraphs[right].normalized:
            continue
        score = jaccard(paragraphs[left].shingles, paragraphs[right].shingles)
        if score >= similarity:
            unite(left, right, score)

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(count):
        clusters[find(index)].append(index)

    groups: list[SharedGroup] = []
    for root, indexes in clusters.items():
        by_skill: dict[str, Paragraph] = {}
        for index in indexes:
            paragraph = paragraphs[index]
            current = by_skill.get(paragraph.skill)
            if current is None or paragraph.norm_chars > current.norm_chars:
                by_skill[paragraph.skill] = paragraph
        if len(by_skill) < 2:
            continue
        skills = tuple(sorted(by_skill))
        representative = max(by_skill.values(), key=lambda item: item.norm_chars)
        groups.append(
            SharedGroup(
                skills=skills,
                paths=tuple(by_skill[skill].path for skill in skills),
                similarity=round(best.get(root, 0.0), 4),
                sample=representative.sample,
                chars=representative.norm_chars,
            )
        )
    groups.sort(
        key=lambda group: (-len(group.skills), -group.similarity, -group.chars, group.skills)
    )
    return groups


def _read_skill(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def scan(
    root: Path,
    *,
    budget: int = DEFAULT_BUDGET,
    chars_per_token: int = CHARS_PER_TOKEN,
    min_paragraph_chars: int = DEFAULT_MIN_PARAGRAPH_CHARS,
    similarity: float = DEFAULT_SIMILARITY,
) -> Report:
    """Measure skill markdown under root.  The tree is only read."""
    if budget < 0:
        raise ValueError("budget must be >= 0")
    if not 0 < similarity <= 1:
        raise ValueError("similarity must be in (0, 1]")
    if min_paragraph_chars < 1:
        raise ValueError("min_paragraph_chars must be >= 1")

    root = root.resolve()
    files: list[FileSize] = []
    unreadable: list[str] = []
    paragraphs: list[Paragraph] = []
    seen_paragraph: set[tuple[str, str]] = set()

    for path in skill_markdown_files(root):
        rel = relative_path(root, path)
        text = _read_skill(path)
        if text is None:
            unreadable.append(rel)
            continue
        skill = skill_slug(path)
        chars = len(text)
        tokens = estimate_tokens(text, chars_per_token)
        files.append(
            FileSize(path=rel, skill=skill, chars=chars, tokens=tokens, over=tokens > budget)
        )
        for paragraph in _paragraphs_for(skill, rel, text, min_paragraph_chars):
            key = (skill, paragraph.normalized)
            if key in seen_paragraph:
                continue
            seen_paragraph.add(key)
            paragraphs.append(paragraph)

    files_sorted = tuple(sorted(files, key=lambda row: (-row.tokens, row.path)))
    by_skill: dict[str, list[FileSize]] = defaultdict(list)
    for row in files_sorted:
        by_skill[row.skill].append(row)
    skills: list[SkillSummary] = []
    for skill, rows in by_skill.items():
        largest = max(rows, key=lambda row: (row.tokens, row.path))
        skills.append(
            SkillSummary(
                skill=skill,
                max_tokens=largest.tokens,
                max_chars=largest.chars,
                copies=len(rows),
                over=largest.tokens > budget,
                largest=largest.path,
            )
        )
    skills.sort(key=lambda row: (-row.max_tokens, row.skill))
    shared = _cluster(paragraphs, similarity)
    return Report(
        budget=budget,
        chars_per_token=chars_per_token,
        similarity=similarity,
        min_paragraph_chars=min_paragraph_chars,
        files=files_sorted,
        skills=tuple(skills),
        shared=tuple(shared),
        unreadable=tuple(sorted(unreadable)),
    )


def _count(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _table(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> str:
    rendered = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def format_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)).rstrip()

    lines = [format_row(list(headers)), format_row(["-" * width for width in widths])]
    lines.extend(format_row(row) for row in rendered)
    return "\n".join(lines)


def render_text(
    report: Report, *, strict: bool = False, max_groups: int = DEFAULT_MAX_GROUPS
) -> str:
    over_files = [row for row in report.files if row.over]
    over_skills = [row for row in report.skills if row.over]
    if report.has_findings:
        status = "FAIL" if strict else "WARNING"
        blocking = (
            "Strict mode fails the process." if strict else "This job warns and does not block."
        )
        pieces = [
            (
                f"{_count(len(over_files), 'file', 'files')} "
                f"({_count(len(over_skills), 'skill', 'skills')}) over the {report.budget} token budget"
            ),
            _count(len(report.shared), "shared paragraph group", "shared paragraph groups"),
        ]
        if report.unreadable:
            pieces.append(_count(len(report.unreadable), "unreadable file", "unreadable files"))
        header = f"{status}: {', '.join(pieces)}. {blocking}"
    else:
        header = (
            f"OK: every skill markdown file is within the {report.budget} token budget "
            "and no shared paragraphs were found."
        )
    lines = [
        header,
        (
            f"budget: {report.budget} tokens  estimate: chars/{report.chars_per_token}  "
            f"files: {len(report.files)}  skills: {len(report.skills)}  "
            f"similarity: {report.similarity:.2f}  min paragraph chars: {report.min_paragraph_chars}"
        ),
        "",
        "LARGEST SKILLS",
    ]
    if report.skills:
        lines.append(
            _table(
                ("skill", "max_tokens", "max_chars", "copies", "status", "largest"),
                [
                    (
                        row.skill,
                        row.max_tokens,
                        row.max_chars,
                        row.copies,
                        "OVER" if row.over else "ok",
                        row.largest,
                    )
                    for row in report.skills
                ],
            )
        )
    else:
        lines.append("(no skill markdown found)")

    lines.extend(["", "OVER BUDGET"])
    if over_skills:
        lines.append(
            _table(
                ("skill", "max_tokens", "copies", "largest"),
                [(row.skill, row.max_tokens, row.copies, row.largest) for row in over_skills],
            )
        )
    else:
        lines.append("none")

    lines.extend(["", "SHARED PARAGRAPHS"])
    if not report.shared:
        lines.append("none")
    else:
        shown = report.shared[:max_groups]
        for index, group in enumerate(shown, start=1):
            skill_list = ", ".join(group.skills)
            lines.append(
                f"{index}. similarity {group.similarity:.2f}  {len(group.skills)} skills  "
                f"{group.chars} chars  {skill_list}"
            )
            lines.append(f"   sample: {group.sample}")
        hidden = len(report.shared) - len(shown)
        if hidden:
            lines.append(f"... and {hidden} more shared paragraph groups")

    if report.unreadable:
        lines.extend(["", "UNREADABLE"])
        lines.extend(report.unreadable)
    return "\n".join(lines) + "\n"


def annotation_lines(report: Report, limit: int = 30) -> list[str]:
    """GitHub workflow warnings.  One line per over-budget skill, then shared groups."""
    lines: list[str] = []

    def emit(path: str, message: str) -> None:
        safe_path = path.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        safe_message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        lines.append(f"::warning file={safe_path}::{safe_message}")

    for row in report.skills:
        if not row.over or len(lines) >= limit:
            break
        emit(
            row.largest,
            f"{row.skill} is {row.max_tokens} tokens, over the {report.budget} budget ({row.copies} copies)",
        )
    for group in report.shared:
        if len(lines) >= limit:
            break
        emit(
            group.paths[0],
            "near-duplicate paragraph shared by "
            + ", ".join(group.skills)
            + f" (similarity {group.similarity:.2f})",
        )
    for path in report.unreadable:
        if len(lines) >= limit:
            break
        emit(path, "skill markdown could not be read as UTF-8")
    return lines


def resolve_budget(cli_value: int | None) -> int:
    if cli_value is not None:
        return cli_value
    raw = os.environ.get("SKILL_TOKEN_BUDGET")
    if raw is None or raw.strip() == "":
        return DEFAULT_BUDGET
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"SKILL_TOKEN_BUDGET must be an integer, got {raw!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=None, help="repository root (default: this repo)"
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help=f"token budget (default {DEFAULT_BUDGET}, or SKILL_TOKEN_BUDGET)",
    )
    parser.add_argument("--min-paragraph-chars", type=int, default=DEFAULT_MIN_PARAGRAPH_CHARS)
    parser.add_argument(
        "--similarity",
        type=float,
        default=DEFAULT_SIMILARITY,
        help="near-duplicate threshold in (0, 1]",
    )
    parser.add_argument(
        "--max-groups",
        type=int,
        default=DEFAULT_MAX_GROUPS,
        help="shared groups to print in text mode",
    )
    parser.add_argument("--strict", action="store_true", help="exit 1 when the report has findings")
    parser.add_argument(
        "--annotations", action="store_true", help="also print GitHub ::warning annotations"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = (args.root or default_root()).resolve()
    if not root.is_dir():
        print(f"skill token lint: root is not a directory: {root}", file=sys.stderr)
        return 2
    try:
        budget = resolve_budget(args.budget)
        report = scan(
            root,
            budget=budget,
            min_paragraph_chars=args.min_paragraph_chars,
            similarity=args.similarity,
        )
    except ValueError as exc:
        print(f"skill token lint: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        json.dump(report.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report, strict=args.strict, max_groups=args.max_groups))
    if args.annotations:
        for line in annotation_lines(report):
            print(line)
    if args.strict and report.has_findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
