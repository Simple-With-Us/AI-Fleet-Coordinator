"""Read-only freeze for supported single-source events, never arms a generation."""

import math


def snapshot(reader, repo, number, epoch):
    # Existing authenticated reader refuses incomplete pagination. No detection
    # watermark, cached ledger timestamp or supplied alert body is a source census.
    if type(epoch) not in (int, float) or not math.isfinite(epoch) or epoch <= 0:
        raise ValueError("boundary-epoch")
    pull = reader.pull(repo, number)
    if pull.get("state") != "open":
        raise ValueError("boundary-pr-not-open")
    head = (pull.get("head") or {}).get("sha")
    if not isinstance(head, str) or not head:
        raise ValueError("boundary-head")
    reviews = reader.reviews(repo, number)
    checks = reader.check_runs(repo, head)
    if any(type(row.get("id")) is not int or row["id"] < 1 for row in reviews + checks):
        raise ValueError("boundary-source-id")
    return dict(
        repo=repo,
        number=number,
        review_floor=max([row["id"] for row in reviews] or [0]),
        check_floor=max([row["id"] for row in checks] or [0]),
        complete=True,
        epoch=epoch,
    )
