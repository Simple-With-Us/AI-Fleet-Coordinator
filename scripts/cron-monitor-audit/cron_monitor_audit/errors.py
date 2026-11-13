import json

_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 100_000


class AuditError(Exception):
    """A local config problem or a failed read.

    The message is safe to print. It never includes a token or a response body.
    """


class SentryForbidden(AuditError):
    """The Sentry token cannot list cron monitors."""


def load_bounded_json(body: bytes, label: str) -> object:
    """Parse JSON that came from an API. Depth and node count are capped."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AuditError(f"{label} was not JSON") from None
    except RecursionError:
        raise AuditError(f"{label} was too deeply nested") from None
    count = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > _MAX_JSON_NODES:
            raise AuditError(f"{label} exceeded the node limit")
        if depth > _MAX_JSON_DEPTH:
            raise AuditError(f"{label} exceeded the depth limit")
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return value
