"""ControllerWorker tests.

The controller is faked throughout: no icedax, no aplay, no drive. The fake
records which thread called it, which is how the single-ownership rule is
actually enforced rather than merely asserted in a comment.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import controller as ctrl  # noqa: E402
from backend import disc as disc_mod  # noqa: E402
from ui_service.worker import ControllerWorker  # noqa: E402

REAL_STARTS = [0, 23143, 39953, 40875, 63496, 65149, 82562, 100181, 107383,
               129223, 151830]
REAL_LEADOUT = 178442

DEADLINE = 10.0         # generous: the Nexus is slow


def real_disc():
    entries = [(i + 1, s, True) for i, s in enumerate(REAL_STARTS)]
    return disc_mod.build_disc(entries, REAL_LEADOUT)


class FakeController(object):
    """Records calls and the thread that made them."""

    def __init__(self):
        self.calls = []
        self.threads = set()
        self.closed = False
        self.state = ctrl.READY
        self.current_track = None
        self.disc = real_disc()
        self.last_error = None

        self.block = None               # set to an Event to stall play()
        self.raise_on = None            # command name that should explode
        self._lock = threading.Lock()

    def _record(self, name):
        with self._lock:
            self.calls.append(name)
            self.threads.add(threading.current_thread().name)
        if self.raise_on == name:
            raise RuntimeError("simulated failure in " + name)

    def scan_disc(self):
        self._record("scan_disc")
        self.state = ctrl.READY
        return self.get_state()

    def play(self, track_number=None):
        self._record("play:" + str(track_number))
        if self.block is not None:
            # Stand in for the 5-8 s icedax spin-up.
            self.block.wait(DEADLINE)
        self.state = ctrl.PLAYING
        self.current_track = track_number or 1
        return True

    def pause(self):
        self._record("pause")
        self.state = ctrl.PAUSED
        return True

    def resume(self):
        self._record("resume")
        self.state = ctrl.PLAYING
        return True

    def stop(self):
        self._record("stop")
        self.state = ctrl.STOPPED
        return True

    def next_track(self):
        self._record("next_track")
        self.current_track = (self.current_track or 1) + 1
        return True

    def previous_track(self):
        self._record("previous_track")
        self.current_track = max(1, (self.current_track or 1) - 1)
        return True

    def update(self):
        with self._lock:
            self.calls.append("update")
            self.threads.add(threading.current_thread().name)
        return self.get_state()

    def get_state(self):
        return ctrl.ControllerState(
            state=self.state, disc=self.disc,
            current_track=self.current_track,
            track_elapsed_seconds=1.0, track_duration_seconds=12.293,
            disc_elapsed_seconds=2.0, disc_duration_seconds=2379.2,
            last_error=self.last_error, drive_address="2,0,0")

    def close(self):
        self._record("close")
        self.closed = True

    # helpers
    def commands(self):
        with self._lock:
            return [c for c in self.calls if c != "update"]

    def update_calls(self):
        with self._lock:
            return self.calls.count("update")


def wait_for(predicate, timeout=DEADLINE, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class WorkerTestCase(unittest.TestCase):

    def make(self, **kwargs):
        controller = FakeController()
        worker = ControllerWorker(controller, poll_interval=0.02, **kwargs)
        self.addCleanup(worker.stop, 5.0)
        return controller, worker


class OwnershipTest(WorkerTestCase):

    def test_only_the_worker_thread_touches_the_controller(self):
        controller, worker = self.make()
        worker.start()
        for name in ("scan", "pause", "resume", "stop"):
            self.assertTrue(wait_for(lambda: not worker._in_flight))
            worker.submit(name)
        self.assertTrue(wait_for(lambda: len(controller.commands()) >= 4))
        worker.stop(5.0)
        self.assertEqual({"controller-worker"}, controller.threads,
                         "the controller was touched from another thread")

    def test_snapshot_from_another_thread_never_calls_the_controller(self):
        controller, worker = self.make()
        worker.start()
        self.assertTrue(wait_for(lambda: controller.update_calls() > 0))
        before = len(controller.calls)
        for _ in range(50):
            worker.snapshot()
        # snapshot() adds no controller calls of its own beyond the worker's
        # own background ticks, and certainly no command calls.
        self.assertEqual(controller.commands(),
                         [c for c in controller.calls[:before]
                          if c != "update"])


class CommandDispatchTest(WorkerTestCase):

    def run_command(self, name, track=None):
        controller, worker = self.make()
        worker.start()
        accepted, error = worker.submit(name, track)
        self.assertTrue(accepted, error)
        self.assertTrue(wait_for(lambda: len(controller.commands()) >= 1),
                        "command never executed")
        return controller

    def test_scan(self):
        self.assertIn("scan_disc", self.run_command("scan").commands())

    def test_play_with_track(self):
        self.assertIn("play:4", self.run_command("play", 4).commands())

    def test_play_without_track(self):
        self.assertIn("play:None", self.run_command("play").commands())

    def test_pause(self):
        self.assertIn("pause", self.run_command("pause").commands())

    def test_resume(self):
        self.assertIn("resume", self.run_command("resume").commands())

    def test_stop(self):
        self.assertIn("stop", self.run_command("stop").commands())

    def test_next(self):
        self.assertIn("next_track", self.run_command("next").commands())

    def test_previous(self):
        self.assertIn("previous_track",
                      self.run_command("previous").commands())

    def test_unknown_command_rejected(self):
        controller, worker = self.make()
        worker.start()
        accepted, error = worker.submit("eject")
        self.assertFalse(accepted)
        self.assertIn("unknown command", error)

    def test_non_integer_track_rejected(self):
        controller, worker = self.make()
        worker.start()
        for bad in ("4", 4.5, True, [4]):
            accepted, error = worker.submit("play", bad)
            self.assertFalse(accepted, "accepted track=%r" % (bad,))
            self.assertEqual("track must be an integer", error)

    def test_commands_execute_in_order(self):
        controller, worker = self.make()
        worker.start()
        for name in ("scan", "pause", "resume", "stop"):
            self.assertTrue(wait_for(lambda: worker._in_flight is None))
            self.assertTrue(worker.submit(name)[0])
        self.assertTrue(wait_for(lambda: len(controller.commands()) >= 4))
        self.assertEqual(["scan_disc", "pause", "resume", "stop"],
                         controller.commands()[:4])


class PeriodicUpdateTest(WorkerTestCase):

    def test_update_is_called_periodically(self):
        controller, worker = self.make()
        worker.start()
        self.assertTrue(wait_for(lambda: controller.update_calls() >= 3),
                        "worker is not polling controller.update()")

    def test_cached_state_reflects_the_controller(self):
        controller, worker = self.make()
        worker.start()
        self.assertTrue(wait_for(
            lambda: worker.snapshot()["state"] == ctrl.READY))
        snapshot = worker.snapshot()
        self.assertEqual(11, snapshot["total_tracks"])
        self.assertEqual("2,0,0", snapshot["drive_address"])
        self.assertTrue(snapshot["service_ready"])

    def test_state_updates_after_a_command(self):
        controller, worker = self.make()
        worker.start()
        worker.submit("play", 3)
        self.assertTrue(wait_for(
            lambda: worker.snapshot()["state"] == ctrl.PLAYING))
        self.assertEqual(3, worker.snapshot()["current_track"])


class BlockingPlayTest(WorkerTestCase):
    """The whole reason this adapter exists."""

    def setUp(self):
        self.controller, self.worker = self.make()
        self.gate = threading.Event()
        self.controller.block = self.gate
        self.addCleanup(self.gate.set)

    def test_busy_and_loading_are_published_before_play_returns(self):
        self.worker.start()
        accepted, error = self.worker.submit("play", 1)
        self.assertTrue(accepted, error)
        snapshot = self.worker.snapshot()
        self.assertTrue(snapshot["busy"])
        self.assertEqual("LOADING", snapshot["state"],
                         "LOADING must be visible immediately, not after "
                         "the blocking call finishes")

    def test_state_stays_readable_while_play_blocks(self):
        self.worker.start()
        self.worker.submit("play", 1)
        self.assertTrue(wait_for(lambda: self.controller.commands()))

        # The worker is now stuck inside play(). Reads must still be instant.
        for _ in range(20):
            started = time.time()
            snapshot = self.worker.snapshot()
            self.assertLess(time.time() - started, 0.5,
                            "snapshot() blocked behind the controller")
            self.assertEqual("LOADING", snapshot["state"])
            self.assertTrue(snapshot["busy"])

    def test_busy_clears_and_state_refreshes_after_completion(self):
        self.worker.start()
        self.worker.submit("play", 1)
        self.assertTrue(wait_for(lambda: self.controller.commands()))
        self.assertTrue(self.worker.snapshot()["busy"])

        self.gate.set()                 # icedax "finishes spinning up"
        self.assertTrue(wait_for(lambda: not self.worker.snapshot()["busy"]),
                        "busy never cleared")
        self.assertEqual("PLAYING", self.worker.snapshot()["state"])

    def test_second_command_is_rejected_while_busy(self):
        self.worker.start()
        self.assertTrue(self.worker.submit("play", 1)[0])
        self.assertTrue(wait_for(lambda: self.controller.commands()))

        for name in ("next", "previous", "play", "scan", "pause"):
            accepted, error = self.worker.submit(name)
            self.assertFalse(accepted, name + " was queued while busy")
            self.assertEqual("controller is busy", error)

        self.gate.set()
        self.assertTrue(wait_for(lambda: not self.worker.snapshot()["busy"]))
        # Exactly one command ran; taps during the block were dropped, not
        # replayed afterwards.
        self.assertEqual(["play:1"], self.controller.commands())

    def test_commands_are_accepted_again_once_idle(self):
        self.worker.start()
        self.worker.submit("play", 1)
        self.gate.set()
        self.assertTrue(wait_for(lambda: not self.worker.snapshot()["busy"]))
        self.assertTrue(self.worker.submit("next")[0])
        self.assertTrue(wait_for(
            lambda: "next_track" in self.controller.commands()))


class FailureTest(WorkerTestCase):

    def test_controller_exception_becomes_a_ui_error(self):
        controller, worker = self.make()
        controller.raise_on = "play:1"
        worker.start()
        worker.submit("play", 1)
        self.assertTrue(wait_for(
            lambda: worker.snapshot()["state"] == "ERROR"),
            "exception did not surface as ERROR")
        snapshot = worker.snapshot()
        self.assertIn("simulated failure", snapshot["error"])
        self.assertFalse(snapshot["busy"], "busy must clear after a failure")

    def test_worker_survives_a_command_exception(self):
        controller, worker = self.make()
        controller.raise_on = "play:1"
        worker.start()
        worker.submit("play", 1)
        self.assertTrue(wait_for(
            lambda: worker.snapshot()["state"] == "ERROR"))
        self.assertTrue(worker.is_running(), "worker thread died")

        controller.raise_on = None
        self.assertTrue(worker.submit("scan")[0],
                        "worker stopped accepting commands after a failure")
        self.assertTrue(wait_for(
            lambda: "scan_disc" in controller.commands()))

    def test_no_traceback_is_exposed_in_the_error_field(self):
        controller, worker = self.make()
        controller.raise_on = "stop"
        worker.start()
        worker.submit("stop")
        self.assertTrue(wait_for(
            lambda: worker.snapshot()["state"] == "ERROR"))
        self.assertNotIn("Traceback", worker.snapshot()["error"])


class ShutdownTest(WorkerTestCase):

    def test_stop_closes_the_controller(self):
        controller, worker = self.make()
        worker.start()
        self.assertTrue(wait_for(lambda: controller.update_calls() > 0))
        self.assertTrue(worker.stop(5.0))
        self.assertTrue(controller.closed, "controller.close() was not called")

    def test_close_runs_on_the_worker_thread(self):
        """Single ownership must hold during shutdown too."""
        controller, worker = self.make()
        worker.start()
        worker.stop(5.0)
        self.assertEqual({"controller-worker"}, controller.threads)

    def test_service_ready_goes_false_after_shutdown(self):
        controller, worker = self.make()
        worker.start()
        self.assertTrue(wait_for(lambda: worker.snapshot()["service_ready"]))
        worker.stop(5.0)
        self.assertFalse(worker.snapshot()["service_ready"])

    def test_stop_is_safe_without_start(self):
        controller, worker = self.make()
        self.assertTrue(worker.stop(1.0))

    def test_double_start_is_rejected(self):
        controller, worker = self.make()
        worker.start()
        self.assertRaises(RuntimeError, worker.start)


if __name__ == "__main__":
    unittest.main()
