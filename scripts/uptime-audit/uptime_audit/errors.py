class AuditError(Exception):
    """A local config problem or a failed read of Sentry.

    The message is safe to print. It never includes a token.
    """
