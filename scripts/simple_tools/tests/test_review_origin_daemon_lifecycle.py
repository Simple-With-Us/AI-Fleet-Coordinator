from unittest import mock

from simple_tools import daemon as D
from simple_tools.review_origin_projection import KEY_ENV
from simple_tools.tests.test_daemon import DaemonHarness


class ProjectionDaemonLifecycleTests(DaemonHarness):
    def make_server(self):
        daemon = self.daemon()
        daemon.config.instance = "server"
        daemon.env.pop(D.LREG.OPERATOR_KEY_ENV, None)
        daemon.env.pop(KEY_ENV, None)
        return daemon

    def test_absent_invalid_key_reload_keeps_service_and_sweeper(self):
        daemon = self.make_server()
        service = daemon._configure_registry()
        sweeper = daemon.registry_sweeper
        self.assertFalse(service.has_review_origin_key)
        for value in ("short", "${" + "x" * 40 + "}", "x" * 513):
            daemon.env[KEY_ENV] = value
            self.assertIs(daemon._configure_registry(), service)
            self.assertIs(daemon.registry_sweeper, sweeper)
            self.assertFalse(service.has_review_origin_key)

    def test_key_add_reload_remove_and_collision_preserve_general_service(self):
        daemon = self.make_server()
        daemon.env[D.LREG.OPERATOR_KEY_ENV] = "k" * 40
        base = daemon._configure_registry()
        daemon.env[KEY_ENV] = "k" * 40
        self.assertIs(daemon._configure_registry(), base)
        daemon.env[KEY_ENV] = "p" * 40
        enabled = daemon._configure_registry()
        self.assertIsNot(enabled, base)
        self.assertTrue(enabled.has_review_origin_key)
        self.assertNotIn("p" * 40, enabled.keys.values())
        sweeper = daemon.registry_sweeper
        self.assertIs(daemon._configure_registry(), enabled)
        self.assertIs(daemon.registry_sweeper, sweeper)
        daemon.env.pop(KEY_ENV)
        disabled = daemon._configure_registry()
        self.assertFalse(disabled.has_review_origin_key)
        self.assertEqual(disabled.keys, base.keys)
        self.assertIs(daemon.registry_sweeper.service, disabled)
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertNotIn("p" * 40, log)
        self.assertNotIn("k" * 40, log)

    def test_dedicated_key_only_listener_lifecycle_and_bind_failure(self):
        daemon = self.make_server()
        daemon.env[KEY_ENV] = "p" * 40
        daemon.config.listener_api_enabled = True
        server = mock.Mock()
        server.address = ("127.0.0.1", 0)
        with mock.patch.object(D.LAPI, "ListenerReadApi", return_value=server) as api:
            daemon._configure_listener_api()
            service = daemon.registry_service
            self.assertEqual(service.keys, {})
            self.assertTrue(service.has_review_origin_key)
            self.assertIs(api.call_args.kwargs["registry"], service)
            server.start.assert_called_once()
            daemon.config.listener_api_enabled = False
            daemon._configure_listener_api()
        server.close.assert_called_once()
        self.assertIsNone(daemon.listener_api_server)
        self.assertIs(daemon.registry_service, service)
        self.assertIs(daemon.registry_sweeper.service, service)
        daemon.config.listener_api_enabled = True
        with mock.patch.object(D.LAPI, "ListenerReadApi", side_effect=OSError("busy")):
            daemon._configure_listener_api()
        self.assertIsNone(daemon.listener_api_server)
        self.assertIs(daemon.registry_service, service)
        self.assertIs(daemon.registry_sweeper.service, service)
