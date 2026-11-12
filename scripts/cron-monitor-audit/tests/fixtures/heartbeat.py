MONITOR_PREFIX = "operator-"

def monitor_slug(instance: str) -> str:
    clean = "".join(c if c.isalnum() else "-" for c in (instance or "mac").lower()).strip("-")
    return MONITOR_PREFIX + (clean or "mac")

def tick(snapshot: dict) -> str:
    instance = str(snapshot.get("instance") or "mac")
    return monitor_slug(instance)
