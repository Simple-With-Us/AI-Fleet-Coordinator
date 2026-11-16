"""CI red streak report package (AFC-737)."""

try:
    from .scanner import SCHEMA_VERSION, build_report, render_markdown
except ImportError:
    from scanner import SCHEMA_VERSION, build_report, render_markdown  # type: ignore

__all__ = ["SCHEMA_VERSION", "build_report", "render_markdown"]
