"""The one thread that owns CDController.

Thread ownership model
----------------------

    HTTP threads          ControllerWorker thread
    ------------          -----------------------
    submit()  ----------> command queue ---------> CDController.<method>()
    snapshot() <--------- UiState <--------------- CDController.update()

HTTP threads NEVER call CDController. They only:
  * push a command onto a queue, and
  * read a copied snapshot out of UiState.

That is what keeps the interface responsive while controller.play() blocks for
5-8 s: the worker is stuck inside the controller, but the cached snapshot --
already published as LOADING/busy before the call began -- is still served
instantly by other threads.

Python 3.5.2 compatible.
"""

import threading
import traceback

try:
    import queue
except ImportError:                     # pragma: no cover - Python 2 only
    import Queue as queue               # noqa: N812

from backend.log import get_logger

from .state import UiState, snapshot_from_controller

# Routed through the project logger so records land in logs/, not on the
# service console via logging's last-resort stderr handler.
log = get_logger("ui_service.worker")

# Commands the service accepts. Anything else is a 400 at the HTTP layer.
COMMANDS = ("scan", "play", "pause", "resume", "stop", "next", "previous")

# Commands that can block for seconds because icedax has to spin the drive up.
BLOCKING_COMMANDS = ("scan", "play", "next", "previous")

DEFAULT_POLL_INTERVAL = 0.15        # seconds between controller.update() calls
DEFAULT_QUEUE_SIZE = 4


class ControllerWorker(object):
    """Serialises every CDController call onto a single thread."""

    def __init__(self, controller, ui_state=None,
                 poll_interval=DEFAULT_POLL_INTERVAL,
                 queue_size=DEFAULT_QUEUE_SIZE):
        self._controller = controller
        self.state = ui_state if ui_state is not None else UiState()
        self._poll_interval = poll_interval

        self._queue = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._thread = None

        # Guards _in_flight and _adapter_error. Held briefly and never while
        # calling the controller, so submit() can never block behind a 5-8 s
        # play().
        self._lock = threading.Lock()
        self._in_flight = None

        # An exception escaping the controller is an ADAPTER-level failure:
        # the controller itself may still report READY, so a plain publish
        # would overwrite the error on the very next poll and the UI would
        # never see it. This stays set until the next command succeeds.
        self._adapter_error = None

        self.update_count = 0           # observability for tests/diagnostics

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        if self._thread is not None:
            raise RuntimeError("worker already started")
        self._thread = threading.Thread(target=self._run,
                                        name="controller-worker")
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self, timeout=10.0):
        """Signal the worker and wait for it to finish.

        controller.close() deliberately runs ON the worker thread (see _run),
        not here: this thread must never touch the controller, or the single-
        ownership rule would be broken exactly when things are going wrong.
        """
        self._stop_event.set()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("controller worker did not stop within %.1fs", timeout)
            return False
        self._thread = None
        return True

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    # -- HTTP-facing surface ------------------------------------------------

    def snapshot(self):
        """Called from HTTP threads. Never touches the controller."""
        return self.state.snapshot()

    def submit(self, name, track=None):
        """Queue a command. Returns (accepted, error_message).

        Rejects rather than queues while a command is in flight. Stale
        navigation taps piling up behind a 6 s spin-up would be worse than
        useless -- the disc would lurch through several tracks after the fact.
        QML disables the buttons while busy; this is the enforcement.
        """
        if name not in COMMANDS:
            return False, "unknown command: " + str(name)
        if name == "play" and track is not None:
            if isinstance(track, bool) or not isinstance(track, int):
                return False, "track must be an integer"

        with self._lock:
            if self._in_flight is not None:
                return False, "controller is busy"
            self._in_flight = name
            busy = name in BLOCKING_COMMANDS

        command = {"name": name, "track": track}
        try:
            self._queue.put_nowait(command)
        except queue.Full:              # pragma: no cover - guarded by _in_flight
            with self._lock:
                self._in_flight = None
            return False, "command queue is full"

        # Publish busy/LOADING immediately, from THIS thread, so a GET /state
        # racing the command still sees the transition. Waiting for the worker
        # to pick the command up would leave a window where the UI looks idle
        # while the drive is already spinning up.
        if busy:
            self.state.update(busy=True, state="LOADING")
        return True, None

    # -- worker thread ------------------------------------------------------

    def _run(self):
        log.info("controller worker started")
        try:
            self._publish()             # service_ready + initial snapshot
            while not self._stop_event.is_set():
                command = self._next_command()
                if command is not None:
                    self._execute(command)
                self._tick()
        except Exception:               # pragma: no cover - last-resort guard
            log.error("controller worker crashed:\n%s", traceback.format_exc())
        finally:
            self._shutdown_controller()
            log.info("controller worker stopped")

    def _next_command(self):
        try:
            return self._queue.get(timeout=self._poll_interval)
        except queue.Empty:
            return None

    def _execute(self, command):
        name = command["name"]
        track = command.get("track")
        log.info("executing command: %s (track=%s)", name, track)
        try:
            self._call_controller(name, track)
        except Exception:
            detail = traceback.format_exc()
            log.error("command %s failed:\n%s", name, detail)
            # One bad command must not kill the worker or the service.
            self._set_adapter_error(
                "command '{0}' failed: {1}".format(name, _last_line(detail)))
        else:
            # A command that worked clears any previous adapter failure.
            self._set_adapter_error(None)
        finally:
            with self._lock:
                self._in_flight = None

    def _call_controller(self, name, track):
        controller = self._controller
        if name == "scan":
            controller.scan_disc()
        elif name == "play":
            if track is None:
                controller.play()
            else:
                controller.play(track)
        elif name == "pause":
            controller.pause()
        elif name == "resume":
            controller.resume()
        elif name == "stop":
            controller.stop()
        elif name == "next":
            controller.next_track()
        elif name == "previous":
            controller.previous_track()
        else:                           # pragma: no cover - filtered in submit
            raise ValueError("unsupported command: " + str(name))

    def _tick(self):
        try:
            self._controller.update()
            self.update_count += 1
        except Exception:
            detail = traceback.format_exc()
            log.error("controller update failed:\n%s", detail)
            self._set_adapter_error("controller update failed: "
                                    + _last_line(detail))
            return
        self._publish()

    def _publish(self):
        """Republish the cached snapshot from the controller.

        A sticky adapter error overrides the controller's own view: the
        controller can happily report READY after raising, and the user would
        otherwise never learn that their command failed.
        """
        try:
            controller_state = self._controller.get_state()
        except Exception:
            detail = traceback.format_exc()
            log.error("controller.get_state() failed:\n%s", detail)
            self._set_adapter_error("controller state unreadable: "
                                    + _last_line(detail))
            return

        with self._lock:
            in_flight = self._in_flight
            adapter_error = self._adapter_error
        busy = in_flight is not None

        snapshot = snapshot_from_controller(controller_state, busy=busy,
                                            service_ready=True)

        # LOADING is derived from the in-flight flag, never published as a
        # one-shot. The controller still honestly reports READY throughout a
        # blocking play() -- it has not started anything yet -- so a tick
        # landing mid-command would otherwise flip the UI back to READY while
        # the drive was audibly spinning up.
        if in_flight in BLOCKING_COMMANDS:
            snapshot["state"] = "LOADING"

        if adapter_error is not None:
            snapshot["state"] = "ERROR"
            snapshot["error"] = adapter_error
            snapshot["busy"] = busy
        self.state.replace(snapshot)

    def _set_adapter_error(self, message):
        with self._lock:
            changed = self._adapter_error != message
            self._adapter_error = message
        if message is not None:
            # Publish straight away so the UI does not wait for the next tick.
            try:
                self.state.update(state="ERROR", busy=False, error=message,
                                  service_ready=True)
            except Exception:           # pragma: no cover
                log.error("could not publish error state")
        elif changed:
            log.info("adapter error cleared")

    def _shutdown_controller(self):
        try:
            self._controller.close()
        except Exception:
            log.error("controller.close() failed:\n%s", traceback.format_exc())
        try:
            self.state.update(service_ready=False)
        except Exception:               # pragma: no cover
            pass


def _last_line(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "unknown error"
