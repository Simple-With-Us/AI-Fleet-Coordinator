class AuditError(Exception):
    """A local config problem or a failed read.

    The message is safe to print. It never includes a token or a response body.
    """


class SentryForbidden(AuditError):
    """The Sentry token cannot list cron monitors."""
