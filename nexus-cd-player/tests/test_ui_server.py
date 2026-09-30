"""HTTP layer tests.

A real server is bound to an ephemeral loopback port and driven with
http.client. The worker is faked, so no controller, no icedax, no drive.
"""

import json
import os
import sys
import threading
import unittest
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui_service import server as server_mod  # noqa: E402
from ui_service import state as state_mod  # noqa: E402


class FakeWorker(object):
    """Implements only what the HTTP layer is allowed to touch."""

    def __init__(self):
        self.submitted = []
        self.busy = False
        self.reject_with = None
        self.state = state_mod.UiState()
        self.state.update(state="READY", total_tracks=11, service_ready=True)
        self._lock = threading.Lock()

    def snapshot(self):
        return self.state.snapshot()

    def submit(self, name, track=None):
        with self._lock:
            self.submitted.append((name, track))
        if self.busy:
            return False, "controller is busy"
        if self.reject_with is not None:
            return False, self.reject_with
        return True, None


class ServerTestCase(unittest.TestCase):

    def setUp(self):
        self.worker = FakeWorker()
        # Port 0 -> ephemeral, so tests never collide with a running service.
        self.server = server_mod.create_server(self.worker, port=0)
        self.thread = server_mod.serve_in_thread(self.server)
        self.host, self.port = self.server.server_address[:2]
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection(self.host, self.port, timeout=10)
        try:
            payload = None
            send_headers = dict(headers or {})
            if body is not None:
                payload = body.encode("utf-8") if isinstance(body, str) else body
                send_headers.setdefault("Content-Type", "application/json")
            connection.request(method, path, payload, send_headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, response.getheader("Content-Type"), raw
        finally:
            connection.close()

    def json_request(self, method, path, body=None, headers=None):
        status, content_type, raw = self.request(method, path, body, headers)
        self.assertEqual("application/json", content_type,
                         "every response must be JSON")
        # Python 3.5's json.loads needs str, not bytes.
        return status, json.loads(raw.decode("utf-8"))


class BindingTest(ServerTestCase):

    def test_binds_loopback_only(self):
        self.assertEqual("127.0.0.1", self.host)

    def test_refuses_to_bind_a_public_interface(self):
        self.assertRaises(ValueError, server_mod.create_server,
                          self.worker, "0.0.0.0", 0)
        self.assertRaises(ValueError, server_mod.create_server,
                          self.worker, "192.168.1.50", 0)

    def test_default_port_is_8765(self):
        self.assertEqual(8765, server_mod.PORT)


class StateEndpointTest(ServerTestCase):

    def test_get_state_returns_200_and_the_snapshot(self):
        status, payload = self.json_request("GET", "/state")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("READY", payload["state"]["state"])
        self.assertEqual(11, payload["state"]["total_tracks"])

    def test_state_contains_the_full_schema(self):
        _status, payload = self.json_request("GET", "/state")
        self.assertEqual(set(state_mod.DEFAULT_SNAPSHOT),
                         set(payload["state"]))

    def test_state_reflects_worker_updates(self):
        self.worker.state.update(state="PLAYING", current_track=3, busy=False)
        _status, payload = self.json_request("GET", "/state")
        self.assertEqual("PLAYING", payload["state"]["state"])
        self.assertEqual(3, payload["state"]["current_track"])

    def test_health(self):
        status, payload = self.json_request("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_query_string_is_ignored(self):
        status, _payload = self.json_request("GET", "/state?t=12345")
        self.assertEqual(200, status)

    def test_get_unknown_route_is_404(self):
        status, payload = self.json_request("GET", "/nonsense")
        self.assertEqual(404, status)
        self.assertFalse(payload["ok"])

    def test_get_on_a_command_route_is_404(self):
        status, _payload = self.json_request("GET", "/play")
        self.assertEqual(404, status)


class CommandEndpointTest(ServerTestCase):

    def test_simple_commands_return_202(self):
        for path, name in (("/scan", "scan"), ("/pause", "pause"),
                           ("/resume", "resume"), ("/stop", "stop"),
                           ("/next", "next"), ("/previous", "previous")):
            status, payload = self.json_request("POST", path)
            self.assertEqual(202, status, path)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["accepted"])
            self.assertEqual(name, payload["command"])

    def test_play_without_a_body(self):
        status, payload = self.json_request("POST", "/play")
        self.assertEqual(202, status)
        self.assertEqual(("play", None), self.worker.submitted[-1])

    def test_play_with_a_track(self):
        status, payload = self.json_request("POST", "/play",
                                            '{"track": 4}')
        self.assertEqual(202, status)
        self.assertEqual(("play", 4), self.worker.submitted[-1])

    def test_play_with_an_empty_body(self):
        status, _payload = self.json_request("POST", "/play", "")
        self.assertEqual(202, status)

    def test_play_with_a_null_track(self):
        status, _payload = self.json_request("POST", "/play",
                                             '{"track": null}')
        self.assertEqual(202, status)
        self.assertEqual(("play", None), self.worker.submitted[-1])

    def test_post_unknown_route_is_404(self):
        status, payload = self.json_request("POST", "/eject")
        self.assertEqual(404, status)
        self.assertEqual([], self.worker.submitted,
                         "an unknown route must not reach the worker")


class InvalidRequestTest(ServerTestCase):

    def test_malformed_json_is_400(self):
        status, payload = self.json_request("POST", "/play", "{not json")
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertIn("JSON", payload["error"])
        self.assertEqual([], self.worker.submitted)

    def test_non_object_body_is_400(self):
        status, payload = self.json_request("POST", "/play", "[1, 2, 3]")
        self.assertEqual(400, status)
        self.assertIn("JSON object", payload["error"])

    def test_non_integer_track_is_400(self):
        for body in ('{"track": "4"}', '{"track": 4.5}',
                     '{"track": true}', '{"track": [4]}'):
            status, payload = self.json_request("POST", "/play", body)
            self.assertEqual(400, status, body)
            self.assertEqual("track must be an integer", payload["error"])
        self.assertEqual([], self.worker.submitted)

    def test_track_on_a_non_play_route_is_400(self):
        status, payload = self.json_request("POST", "/next", '{"track": 4}')
        self.assertEqual(400, status)
        self.assertIn("only valid for /play", payload["error"])

    def test_oversized_body_is_400(self):
        status, payload = self.json_request(
            "POST", "/play", '{"track": 1, "pad": "' + "x" * 9000 + '"}')
        self.assertEqual(400, status)
        self.assertIn("too large", payload["error"])

    def test_invalid_utf8_body_is_400(self):
        status, payload = self.json_request("POST", "/play", b"\xff\xfe\x00")
        self.assertEqual(400, status)
        self.assertIn("UTF-8", payload["error"])

    def test_worker_rejection_is_400(self):
        self.worker.reject_with = "unknown command: wat"
        status, payload = self.json_request("POST", "/scan")
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])


class BusyTest(ServerTestCase):

    def test_busy_command_is_409(self):
        self.worker.busy = True
        status, payload = self.json_request("POST", "/next")
        self.assertEqual(409, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("controller is busy", payload["error"])

    def test_state_is_still_readable_while_busy(self):
        """The UI must never be locked out of polling by a busy controller."""
        self.worker.busy = True
        self.worker.state.update(state="LOADING", busy=True)
        status, payload = self.json_request("GET", "/state")
        self.assertEqual(200, status)
        self.assertEqual("LOADING", payload["state"]["state"])
        self.assertTrue(payload["state"]["busy"])


class RobustnessTest(ServerTestCase):

    def test_no_traceback_leaks_when_the_worker_explodes(self):
        def boom():
            raise RuntimeError("internal detail that must not be exposed")
        self.worker.snapshot = boom
        status, payload = self.json_request("GET", "/state")
        self.assertEqual(500, status)
        self.assertNotIn("Traceback", payload["error"])
        self.assertNotIn("internal detail", payload["error"])

    def test_concurrent_polls_all_succeed(self):
        results = []
        lock = threading.Lock()

        def poll():
            try:
                status, _payload = self.json_request("GET", "/state")
                with lock:
                    results.append(status)
            except Exception as exc:
                with lock:
                    results.append(exc)

        threads = [threading.Thread(target=poll) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual([200] * 12, results)

    def test_content_length_is_correct(self):
        _status, content_type, raw = self.request("GET", "/state")
        connection = HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request("GET", "/state")
            response = connection.getresponse()
            declared = int(response.getheader("Content-Length"))
            body = response.read()
            self.assertEqual(declared, len(body))
        finally:
            connection.close()

    def test_state_is_not_cacheable(self):
        connection = HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request("GET", "/state")
            response = connection.getresponse()
            response.read()
            self.assertEqual("no-store", response.getheader("Cache-Control"))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
