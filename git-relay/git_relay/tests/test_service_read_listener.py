import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from git_relay.server import (
    BoundedThreadingHTTPServer,
    _service_read_listener_address,
    make_handler,
)


class Listener(unittest.TestCase):
    def test_private_bind_never_wildcard_public_or_bootstrap(self):
        self.assertIsNone(_service_read_listener_address({}))
        self.assertEqual(
            _service_read_listener_address({"GIT_RELAY_SERVICE_READ_BIND": "10.0.2.1"}),
            ("10.0.2.1", 8473),
        )
        for addr in ("0.0.0.0", "::", "8.8.8.8", "169.254.169.254", "example.com"):
            with self.assertRaises(SystemExit):
                _service_read_listener_address({"GIT_RELAY_SERVICE_READ_BIND": addr})

    def test_only_exact_service_read_route_reaches_relay(self):
        relay = mock.Mock()
        relay.handle.return_value = (401, {"error": "unauthorized"})
        server = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(relay, service_read_only=True)
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for path in (
                "/v1/open_pr",
                "/v1/mint",
                "/v1/wf_file_patch",
                "/git/example/repo/info/refs",
                "/v1/service_read/",
                "/v1/service_read?x=1",
            ):
                req = urllib.request.Request(
                    "http://127.0.0.1:" + str(server.server_address[1]) + path,
                    data=b"{}",
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(req, timeout=2)
                self.assertEqual(e.exception.code, 404)
            relay.handle.assert_not_called()
            req = urllib.request.Request(
                "http://127.0.0.1:" + str(server.server_address[1]) + "/v1/service_read",
                data=b"{}",
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(e.exception.code, 401)
            self.assertEqual(relay.handle.call_args.args[1], "service_read")
            self.assertNotIn("operator_bootstrap", relay.handle.call_args.kwargs)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
