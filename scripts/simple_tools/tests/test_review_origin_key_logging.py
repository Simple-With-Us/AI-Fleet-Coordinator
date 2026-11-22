from simple_tools.review_origin_projection import KEY_ENV
from simple_tools.tests.test_daemon import DaemonHarness


class ProjectionKeyLoggingTests(DaemonHarness):
    def test_invalid_optional_key_names_variable_without_secret_or_service_failure(self):
        daemon = self.daemon()
        daemon.config.instance = "server"
        daemon.env.pop(KEY_ENV, None)
        service = daemon._configure_registry()
        sweeper = daemon.registry_sweeper
        sentinel = "invalid-projection-secret-sentinel"
        daemon.env[KEY_ENV] = "${" + sentinel + "}"
        self.assertIs(daemon._configure_registry(), service)
        self.assertIs(daemon.registry_sweeper, sweeper)
        self.assertFalse(service.has_review_origin_key)
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("review-origin-projection-held", log)
        self.assertIn("key_env", log)
        self.assertIn(KEY_ENV, log)
        self.assertIn("invalid-or-reused-read-key", log)
        self.assertNotIn(sentinel, log)
