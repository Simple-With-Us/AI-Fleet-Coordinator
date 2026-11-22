"""Exercise actual current-main Daemon methods, not a parallel lifecycle."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from simple_tools.daemon import Daemon


class Lifecycle(unittest.TestCase):
    def daemon(self):
        d = Daemon.__new__(Daemon)
        d.stop = threading.Event()
        d.config = SimpleNamespace(instance="server")
        d.env = {}
        d.log = mock.Mock()
        d.registry_service = object()
        return d

    def test_stop_joins_before_registry_replacement(self):
        d = self.daemon()
        stop = threading.Event()
        thread = mock.Mock()
        d._review_alert_stop = stop
        d._review_alert_thread = thread

        def joined():
            self.assertTrue(stop.is_set())
            self.assertIsNotNone(d.registry_service)

        thread.join.side_effect = joined
        d.config = SimpleNamespace(instance="mac")
        self.assertIsNone(d._configure_registry())
        thread.join.assert_called_once_with()
        self.assertIsNone(d.registry_service)

    def test_reconfigure_joins_old_and_starts_with_current_registry(self):
        d = self.daemon()
        old = mock.Mock()
        stop = threading.Event()
        d._review_alert_thread = old
        d._review_alert_stop = stop
        current = d.registry_service
        with mock.patch("review_alerts.operator_loop.start", return_value=mock.Mock()) as start:
            d._configure_review_alerts()
        self.assertTrue(stop.is_set())
        old.join.assert_called_once_with()
        self.assertIs(start.call_args.args[1], current)
        self.assertIsNot(start.call_args.args[2], d.stop)

    def test_reload_server_to_mac_stops_without_restart_and_shutdown_joins(self):
        d = self.daemon()
        d.config = SimpleNamespace(instance="mac")
        old = mock.Mock()
        d._review_alert_thread = old
        d._review_alert_stop = threading.Event()
        with mock.patch("review_alerts.operator_loop.start") as start:
            d._configure_review_alerts()
            start.assert_not_called()
        old.join.assert_called_once_with()
        d._review_alert_thread = mock.Mock()
        t = d._review_alert_thread
        d._review_alert_stop = threading.Event()
        d.outgoing_webhook_server = None
        d.listener_api_server = None
        d.seats = {}
        d.write_status = mock.Mock()
        d.shutdown()
        t.join.assert_called_once_with()
        self.assertTrue(d.stop.is_set())
        self.assertIsNone(d.registry_service)

    def test_actual_reload_hook_after_listener_reconfig(self):
        d = self.daemon()
        d.clock = mock.Mock()
        d.root = "unused"
        d.config_path = "unused"
        d.partition_check = mock.Mock(return_value=[])
        new = SimpleNamespace(instance="server", errors=[])
        d.seats = {}
        order = []
        for name in (
            "_apply_config",
            "_configure_outgoing_webhook",
            "_configure_listener_api",
            "_configure_review_alerts",
            "_configure_linear_comments",
            "_remember_operator_settings",
        ):
            setattr(d, name, mock.Mock(side_effect=lambda name=name: order.append(name)))
        with mock.patch("simple_tools.daemon.C.load", return_value=new):
            d.reload()
        self.assertLess(
            order.index("_configure_listener_api"), order.index("_configure_review_alerts")
        )
