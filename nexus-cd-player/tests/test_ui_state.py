"""Thread-safe UI state tests."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import controller as ctrl  # noqa: E402
from backend import disc as disc_mod  # noqa: E402
from ui_service import state as state_mod  # noqa: E402

REAL_STARTS = [0, 23143, 39953, 40875, 63496, 65149, 82562, 100181, 107383,
               129223, 151830]
REAL_LEADOUT = 178442


def real_disc():
    entries = [(i + 1, s, True) for i, s in enumerate(REAL_STARTS)]
    return disc_mod.build_disc(entries, REAL_LEADOUT)


class SnapshotIsolationTest(unittest.TestCase):

    def setUp(self):
        self.state = state_mod.UiState()

    def test_defaults(self):
        snapshot = self.state.snapshot()
        self.assertEqual(False, snapshot["busy"])
        self.assertIsNone(snapshot["current_track"])
        self.assertIsNone(snapshot["error"])
        self.assertFalse(snapshot["service_ready"])

    def test_snapshot_is_a_private_copy(self):
        """Mutating a snapshot must not corrupt the shared state."""
        snapshot = self.state.snapshot()
        snapshot["state"] = "TAMPERED"
        snapshot["current_track"] = 99
        self.assertNotEqual("TAMPERED", self.state.snapshot()["state"])
        self.assertIsNone(self.state.snapshot()["current_track"])

    def test_two_snapshots_are_independent(self):
        first = self.state.snapshot()
        second = self.state.snapshot()
        self.assertIsNot(first, second)
        first["busy"] = True
        self.assertFalse(second["busy"])

    def test_update_merges(self):
        self.state.update(state="PLAYING", current_track=3)
        self.state.update(track_elapsed=7.5)
        snapshot = self.state.snapshot()
        self.assertEqual("PLAYING", snapshot["state"])
        self.assertEqual(3, snapshot["current_track"])
        self.assertEqual(7.5, snapshot["track_elapsed"])

    def test_unknown_field_is_rejected(self):
        self.assertRaises(KeyError, self.state.update, nonsense=1)
        self.assertRaises(KeyError, self.state.replace, {"nonsense": 1})

    def test_revision_increments(self):
        first = self.state.snapshot()["revision"]
        self.state.update(busy=True)
        self.assertEqual(first + 1, self.state.snapshot()["revision"])

    def test_replace_resets_unmentioned_fields(self):
        self.state.update(error="old failure")
        self.state.replace({"state": "READY"})
        snapshot = self.state.snapshot()
        self.assertIsNone(snapshot["error"], "replace must not leak old fields")
        self.assertEqual("READY", snapshot["state"])

    def test_replace_preserves_revision_ordering(self):
        self.state.update(busy=True)
        before = self.state.snapshot()["revision"]
        self.state.replace({"state": "READY"})
        self.assertGreater(self.state.snapshot()["revision"], before)


class ConcurrencyTest(unittest.TestCase):

    def test_concurrent_writers_do_not_corrupt_state(self):
        state = state_mod.UiState()
        errors = []

        def writer(index):
            try:
                for _ in range(200):
                    state.update(current_track=index, total_tracks=index)
                    snapshot = state.snapshot()
                    # Both fields are always written together, so a reader
                    # must never see one writer's track with another's total.
                    if snapshot["current_track"] != snapshot["total_tracks"]:
                        errors.append((snapshot["current_track"],
                                       snapshot["total_tracks"]))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,))
                   for i in range(1, 6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], errors, "torn read across a locked update")

    def test_reads_during_writes_are_always_complete(self):
        state = state_mod.UiState()
        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                snapshot = state.snapshot()
                if set(snapshot) != set(state_mod.DEFAULT_SNAPSHOT):
                    errors.append(sorted(snapshot))

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for _ in range(300):
                state.replace({"state": "PLAYING", "current_track": 4})
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual([], errors, "snapshot seen with missing keys")


class ControllerMappingTest(unittest.TestCase):
    """Field mapping uses the real ControllerState, inventing nothing."""

    def make_state(self, **kwargs):
        defaults = dict(
            state=ctrl.PLAYING, disc=real_disc(), current_track=3,
            track_elapsed_seconds=7.0, track_duration_seconds=12.293,
            disc_elapsed_seconds=539.0, disc_duration_seconds=2379.2,
            last_error=None, drive_address="2,0,0")
        defaults.update(kwargs)
        return ctrl.ControllerState(**defaults)

    def test_maps_every_field(self):
        snapshot = state_mod.snapshot_from_controller(self.make_state())
        self.assertEqual("PLAYING", snapshot["state"])
        self.assertEqual(3, snapshot["current_track"])
        self.assertEqual(11, snapshot["total_tracks"])
        self.assertAlmostEqual(7.0, snapshot["track_elapsed"])
        self.assertAlmostEqual(12.293, snapshot["track_duration"])
        self.assertAlmostEqual(539.0, snapshot["disc_elapsed"])
        self.assertTrue(snapshot["has_disc"])
        self.assertEqual("2,0,0", snapshot["drive_address"])
        self.assertIsNone(snapshot["error"])

    def test_busy_is_adapter_metadata_not_controller_state(self):
        controller_state = self.make_state()
        self.assertFalse(
            state_mod.snapshot_from_controller(controller_state)["busy"])
        self.assertTrue(
            state_mod.snapshot_from_controller(controller_state,
                                               busy=True)["busy"])

    def test_no_disc_maps_cleanly(self):
        snapshot = state_mod.snapshot_from_controller(
            self.make_state(state=ctrl.NO_DISC, disc=None, current_track=None,
                            track_duration_seconds=0.0,
                            disc_duration_seconds=0.0))
        self.assertEqual("NO_DISC", snapshot["state"])
        self.assertEqual(0, snapshot["total_tracks"])
        self.assertFalse(snapshot["has_disc"])
        self.assertIsNone(snapshot["current_track"])

    def test_error_is_carried_through(self):
        snapshot = state_mod.snapshot_from_controller(
            self.make_state(state=ctrl.ERROR, last_error="icedax exited 2"))
        self.assertEqual("ERROR", snapshot["state"])
        self.assertEqual("icedax exited 2", snapshot["error"])

    def test_mapped_snapshot_matches_the_published_schema(self):
        snapshot = state_mod.snapshot_from_controller(self.make_state())
        extra = set(snapshot) - set(state_mod.DEFAULT_SNAPSHOT)
        self.assertEqual(set(), extra)
        # revision is added by UiState, not by the mapper.
        missing = set(state_mod.DEFAULT_SNAPSHOT) - set(snapshot)
        self.assertEqual({"revision"}, missing)

    def test_every_controller_state_is_a_known_ui_state(self):
        for name in (ctrl.NO_DRIVE, ctrl.NO_DISC, ctrl.READY, ctrl.LOADING,
                     ctrl.PLAYING, ctrl.PAUSED, ctrl.STOPPED, ctrl.ERROR):
            self.assertIn(name, state_mod.UI_STATES)


if __name__ == "__main__":
    unittest.main()
