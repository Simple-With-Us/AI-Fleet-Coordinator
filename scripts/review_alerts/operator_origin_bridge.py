"""Existing Operator client bridge for an in-process protected creation store.

The lookup is supplied from the service-owned PROwnerStore, not a request body.
The registry's existing resolve projection provides host/scope; platform is the
configured namespace actually queried, not inferred from a title or seat spelling.
"""

from .operator_pr_origin import Held, bind_creation


class RegistryOriginBinder:
    def __init__(self, creation_store, registry_call, seat_map, platform_map):
        self.store = creation_store
        self.registry_call = registry_call
        self.seat_map = dict(seat_map)
        self.platform_map = dict(platform_map)

    def bind(self, repo, number):
        # This first read selects only the resolve request. bind_creation independently
        # reads and rechecks the protected first-confirmed receipt before mutation.
        row = self.store.lookup(repo, number)
        if not isinstance(row, dict):
            raise Held("no-confirmed-live-creation")
        seat = self.seat_map.get(row.get("seat"))
        platform = self.platform_map.get(seat)
        if not platform:
            raise Held("platform-not-configured")

        def history(principal):
            if principal != seat:
                raise Held("seat-changed")
            resolved = self.registry_call(
                "resolve", dict(platform=platform, letter=row.get("origin_letter_claim"))
            )
            if (
                not isinstance(resolved, dict)
                or "error" in resolved
                or resolved.get("found") is not True
            ):
                raise Held("resolve-unavailable")
            # Never allow returned metadata to override the queried namespace.
            if resolved.get("platform", platform) != platform:
                raise Held("platform-mismatch")
            resolved = dict(resolved, platform=platform)
            return dict(items=[resolved])

        return bind_creation(
            repo,
            number,
            lookup_creation=self.store.lookup,
            registry_history=history,
            registry_bind=lambda p: self.registry_call("bind-pr", p),
            seat_map=self.seat_map,
        )
