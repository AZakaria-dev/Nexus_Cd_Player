"""Loopback HTTP/JSON server for the QML frontend.

Binds 127.0.0.1 ONLY. There is no authentication because nothing off the
device can reach it.

Handler threads never touch CDController -- they call worker.submit() and
worker.snapshot(), both of which are thread-safe and non-blocking.

Python 3.5.2 compatible: no ThreadingHTTPServer (3.7+), and json.loads is
given str rather than bytes (3.5 cannot decode bytes).
"""

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from backend.log import get_logger

# Routed through the project logger so records land in logs/, not on the
# service console via logging's last-resort stderr handler.
log = get_logger("ui_service.server")

HOST = "127.0.0.1"
PORT = 8765

# Longest JSON body we will read. The real bodies are a few dozen bytes.
MAX_BODY_BYTES = 8192

COMMAND_ROUTES = {
    "/scan": "scan",
    "/play": "play",
    "/pause": "pause",
    "/resume": "resume",
    "/stop": "stop",
    "/next": "next",
    "/previous": "previous",
}


class UiRequestHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "NexusCDPlayer/1.0"

    # -- plumbing -----------------------------------------------------------

    @property
    def worker(self):
        return self.server.worker

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler writes to stderr by default, which would
        # scribble over the service's own console output.
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # The UI polls constantly; never let anything cache /state.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # QML navigated away or timed out mid-response. Not our problem.
            log.debug("client disconnected before the response was written")

    def _error(self, status, message):
        self._send_json(status, {"ok": False, "error": message})

    def _read_json_body(self):
        """Return (payload, error_message). Missing body is an empty dict."""
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return {}, None
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return None, "invalid Content-Length"
        if length < 0:
            return None, "invalid Content-Length"
        if length > MAX_BODY_BYTES:
            return None, "request body too large"
        if length == 0:
            return {}, None

        raw = self.rfile.read(length)
        try:
            # Python 3.5's json.loads does not accept bytes.
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "body must be UTF-8"
        text = text.strip()
        if not text:
            return {}, None
        try:
            payload = json.loads(text)
        except ValueError:
            return None, "body must be valid JSON"
        if not isinstance(payload, dict):
            return None, "body must be a JSON object"
        return payload, None

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/state":
                self._send_json(200, {"ok": True,
                                      "state": self.worker.snapshot()})
            elif path == "/health":
                self._send_json(200, {"ok": True,
                                      "service": "nexus-cd-player-ui"})
            else:
                self._error(404, "unknown endpoint: " + path)
        except Exception:
            # Never leak a traceback to the UI.
            log.exception("unhandled error serving GET %s", self.path)
            self._error(500, "internal service error")

    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]
            command = COMMAND_ROUTES.get(path)
            if command is None:
                self._error(404, "unknown endpoint: " + path)
                return

            payload, error = self._read_json_body()
            if error is not None:
                self._error(400, error)
                return

            track = payload.get("track")
            if track is not None:
                # bool is an int subclass; {"track": true} is not a track.
                if isinstance(track, bool) or not isinstance(track, int):
                    self._error(400, "track must be an integer")
                    return
            if track is not None and command != "play":
                self._error(400, "track is only valid for /play")
                return

            accepted, reason = self.worker.submit(command, track)
            if accepted:
                self._send_json(202, {"ok": True, "accepted": True,
                                      "command": command})
                return
            if reason == "controller is busy":
                self._error(409, reason)
                return
            self._error(400, reason or "command rejected")
        except Exception:
            log.exception("unhandled error serving POST %s", self.path)
            self._error(500, "internal service error")


class UiHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Threaded so a slow client cannot stall the whole UI.

    ThreadingMixIn rather than http.server.ThreadingHTTPServer, which does not
    exist until Python 3.7; the device runs 3.5.2.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, worker):
        HTTPServer.__init__(self, server_address, handler_class)
        self.worker = worker


def create_server(worker, host=HOST, port=PORT):
    """Build a server bound to loopback.

    Pass port=0 to get an ephemeral port (used by the tests).
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "the UI service must bind loopback only, refusing host " + host)
    return UiHTTPServer((host, port), UiRequestHandler, worker)


def serve_in_thread(server):
    """Run serve_forever on a daemon thread; returns the thread."""
    thread = threading.Thread(target=server.serve_forever,
                              name="ui-http-server")
    thread.daemon = True
    thread.start()
    return thread
