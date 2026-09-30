"""Controller tests.

Everything is faked: drive discovery, TOC, PlaybackEngine and the clock. No
icedax, no aplay, no drive, no real time. Timing tests are therefore exact
rather than flaky.

The disc used throughout is the real 11-track test disc, so track 3 really is
922 sectors (12.293 s) long -- the boundary the hardware test exercises.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import controller as ctrl  # noqa: E402
from backend import device as device_mod  # noqa: E402
from backend import disc as disc_mod  # noqa: E402
from backend import errors  # noqa: E402
from backend import playback  # noqa: E402

REAL_STARTS = [0, 23143, 39953, 40875, 63496, 65149, 82562, 100181, 107383,
               129223, 151830]
REAL_LEADOUT = 178442

TRACK3_DURATION = (40875 - 39953) / 75.0        # 12.293... seconds
TRACK1_DURATION = 23143 / 75.0                  # 308.573... seconds


def real_disc():
    entries = [(i + 1, s, True) for i, s in enumerate(REAL_STARTS)]
    return disc_mod.build_disc(entries, REAL_LEADOUT)


# --- fakes -----------------------------------------------------------------


class FakeClock(object):
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeEngine(object):
    """Stands in for PlaybackEngine with the same public surface."""

    def __init__(self):
        self.start_calls = []
        self.stop_calls = 0
        self.running = False
        self.paused = False
        self.icedax_pid = None
        self.aplay_pid = None
        self.pcm_wait_seconds = None
        self._next_status = None
        self._start_error = None
        self._pid_seq = 100

    # behaviour knobs
    def fail_next_start(self, message="icedax died before producing audio"):
        self._start_error = message

    def report(self, result):
        """What the next poll_status() should return."""
        self._next_status = result

    # PlaybackEngine surface
    def start(self, address, start_track, last_track,
              sectors_per_request=None, duration_seconds=None):
        if self._start_error is not None:
            message = self._start_error
            self._start_error = None
            self.icedax_pid = None
            self.aplay_pid = None
            raise errors.PlaybackError(message)
        self.start_calls.append((address, start_track, last_track))
        self.running = True
        self.paused = False
        self._pid_seq += 1
        self.icedax_pid = self._pid_seq
        self._pid_seq += 1
        self.aplay_pid = self._pid_seq
        self.pcm_wait_seconds = 5.84
        return self.pcm_wait_seconds

    def pause(self):
        if not self.running or self.paused:
            return False
        self.paused = True
        return True

    def resume(self):
        if not self.running or not self.paused:
            return False
        self.paused = False
        return True

    def stop(self):
        self.stop_calls += 1
        self.running = False
        self.paused = False
        return None

    def close(self):
        self.stop()

    def is_running(self):
        return self.running

    def is_paused(self):
        return self.paused

    def poll_status(self):
        status = self._next_status
        self._next_status = None
        if status is not None:
            self.running = False
        return status


def ok_result():
    return playback.PlaybackResult(playback.COMPLETED, "playback finished", 0, 0)


def failed_result(message="icedax exited 2 during playback"):
    return playback.PlaybackResult(playback.FAILED, message, 2, None)


def drive_found(address="2,0,0"):
    drive = device_mod.DriveInfo(2, 0, 0, vendor="PLDS",
                                 model="DVD+-RW DH-16ABS", revision="PD11",
                                 kind="Removable CD-ROM")
    return device_mod.DeviceResult(device_mod.DRIVE_PRESENT, drive=drive)


def no_drive():
    return device_mod.DeviceResult(device_mod.NO_DRIVE,
                                   message="no optical drive found")


def audio_disc():
    return disc_mod.DiscResult(disc_mod.AUDIO_DISC, disc=real_disc())


def no_disc():
    return disc_mod.DiscResult(disc_mod.NO_DISC, message="no disc in the drive")


def build(discover=None, toc=None, clock=None, engine=None, **kwargs):
    clock = clock or FakeClock()
    engine = engine or FakeEngine()
    controller = ctrl.CDController(
        engine=engine,
        discover_fn=discover or drive_found,
        read_toc_fn=toc or (lambda address: audio_disc()),
        clock=clock,
        **kwargs
    )
    return controller, engine, clock


def ready(**kwargs):
    controller, engine, clock = build(**kwargs)
    controller.scan_disc()
    return controller, engine, clock


# --- scanning --------------------------------------------------------------


class ScanTest(unittest.TestCase):

    def test_scan_reaches_ready(self):
        controller, _, _ = build()
        state = controller.scan_disc()
        self.assertEqual(ctrl.READY, state.state)
        self.assertEqual(11, state.track_count)
        self.assertEqual(1, controller.current_track)
        self.assertIsNone(state.last_error)

    def test_scan_no_drive(self):
        controller, _, _ = build(discover=no_drive)
        state = controller.scan_disc()
        self.assertEqual(ctrl.NO_DRIVE, state.state)
        self.assertIsNone(state.disc)
        self.assertTrue(state.last_error)

    def test_scan_no_disc(self):
        controller, _, _ = build(toc=lambda address: no_disc())
        state = controller.scan_disc()
        self.assertEqual(ctrl.NO_DISC, state.state)
        self.assertIsNone(state.disc)

    def test_scan_toc_error(self):
        def broken(address):
            return disc_mod.DiscResult(disc_mod.ERROR,
                                       message="icedax exited 3")
        controller, _, _ = build(toc=broken)
        self.assertEqual(ctrl.ERROR, controller.scan_disc().state)
        self.assertIn("icedax exited 3", controller.last_error)

    def test_data_disc_is_not_playable(self):
        def data(address):
            return disc_mod.DiscResult(disc_mod.UNSUPPORTED_DISC,
                                       message="no CD-DA audio")
        controller, _, _ = build(toc=data)
        self.assertEqual(ctrl.NO_DISC, controller.scan_disc().state)

    def test_scan_stops_active_playback_first(self):
        controller, engine, _ = ready()
        controller.play(1)
        before = engine.stop_calls
        controller.scan_disc()
        self.assertGreater(engine.stop_calls, before)

    def test_update_never_rereads_the_toc(self):
        """A status poll must not start another icedax TOC operation."""
        calls = []

        def counting_toc(address):
            calls.append(address)
            return audio_disc()

        controller, _, clock = build(toc=counting_toc)
        controller.scan_disc()
        controller.play(1)
        self.assertEqual(1, len(calls))
        for _ in range(20):
            clock.advance(1.0)
            controller.update()
        self.assertEqual(1, len(calls), "update() re-read the TOC")


# --- play ------------------------------------------------------------------


class PlayTest(unittest.TestCase):

    def test_play_defaults_to_first_track(self):
        controller, engine, _ = ready()
        self.assertTrue(controller.play())
        self.assertEqual(1, controller.current_track)
        self.assertEqual(ctrl.PLAYING, controller.state)

    def test_play_uses_a_range_through_the_last_track(self):
        controller, engine, _ = ready()
        controller.play(3)
        self.assertEqual([("2,0,0", 3, 11)], engine.start_calls)

    def test_play_explicit_track(self):
        controller, _, _ = ready()
        self.assertTrue(controller.play(6))
        self.assertEqual(6, controller.current_track)

    def test_invalid_track_rejected(self):
        controller, engine, _ = ready()
        self.assertFalse(controller.play(12))
        self.assertFalse(controller.play(0))
        self.assertEqual([], engine.start_calls)
        self.assertEqual(ctrl.READY, controller.state,
                         "a rejected request must not change state")
        self.assertTrue(controller.last_error)

    def test_play_without_disc_rejected(self):
        controller, engine, _ = build(toc=lambda address: no_disc())
        controller.scan_disc()
        self.assertFalse(controller.play(1))
        self.assertEqual([], engine.start_calls)

    def test_loading_is_entered_before_playing(self):
        controller, _, _ = ready()
        controller.play(1)
        states = [to_state for _, _, to_state in controller.transitions]
        self.assertIn(ctrl.LOADING, states)
        self.assertLess(states.index(ctrl.LOADING), states.index(ctrl.PLAYING))

    def test_failed_start_becomes_error(self):
        controller, engine, _ = ready()
        engine.fail_next_start("icedax closed its output without audio")
        self.assertFalse(controller.play(1))
        self.assertEqual(ctrl.ERROR, controller.state)
        self.assertIn("without audio", controller.last_error)

    def test_play_while_paused_resumes(self):
        controller, engine, clock = ready()
        controller.play(1)
        clock.advance(10.0)
        controller.pause()
        self.assertTrue(controller.play())
        self.assertEqual(ctrl.PLAYING, controller.state)
        self.assertEqual(1, len(engine.start_calls),
                         "resume must not restart the pipeline")


# --- progress and pause ----------------------------------------------------


class ProgressTest(unittest.TestCase):

    def test_elapsed_tracks_the_clock(self):
        controller, _, clock = ready()
        controller.play(1)
        clock.advance(30.0)
        controller.update()
        self.assertAlmostEqual(30.0, controller.track_elapsed_seconds(),
                               places=1)

    def test_track_duration_comes_from_sectors(self):
        controller, _, _ = ready()
        controller.play(3)
        self.assertAlmostEqual(TRACK3_DURATION,
                               controller.track_duration_seconds(), places=3)

    def test_pause_freezes_progress(self):
        controller, _, clock = ready()
        controller.play(1)
        clock.advance(10.0)
        controller.pause()
        frozen = controller.track_elapsed_seconds()
        clock.advance(5.0)
        controller.update()
        self.assertAlmostEqual(frozen, controller.track_elapsed_seconds(),
                               places=3)

    def test_resume_continues_from_the_same_position(self):
        controller, engine, clock = ready()
        controller.play(1)
        clock.advance(10.0)
        controller.pause()
        clock.advance(5.0)
        controller.resume()
        self.assertAlmostEqual(10.0, controller.track_elapsed_seconds(),
                               places=1)
        clock.advance(2.0)
        self.assertAlmostEqual(12.0, controller.track_elapsed_seconds(),
                               places=1)

    def test_paused_time_is_excluded_entirely(self):
        controller, _, clock = ready()
        controller.play(1)
        for _ in range(3):
            clock.advance(4.0)
            controller.pause()
            clock.advance(60.0)     # a whole minute of being paused
            controller.resume()
        self.assertAlmostEqual(12.0, controller.track_elapsed_seconds(),
                               places=1)

    def test_pause_only_valid_while_playing(self):
        controller, _, _ = ready()
        self.assertFalse(controller.pause())
        controller.play(1)
        self.assertTrue(controller.pause())
        self.assertFalse(controller.pause(), "double pause must be a no-op")
        self.assertEqual(ctrl.PAUSED, controller.state)

    def test_resume_only_valid_while_paused(self):
        controller, _, _ = ready()
        self.assertFalse(controller.resume())
        controller.play(1)
        self.assertFalse(controller.resume())

    def test_disc_elapsed_is_absolute(self):
        controller, _, clock = ready()
        controller.play(3)
        clock.advance(5.0)
        expected = 39953 / 75.0 + 5.0
        self.assertAlmostEqual(expected, controller.disc_elapsed_seconds(),
                               places=1)

    def test_disc_duration(self):
        controller, _, _ = ready()
        self.assertEqual("39:39.17",
                         disc_mod.format_msf(controller.disc_duration_seconds()))


# --- natural track transitions (the critical behaviour) --------------------


class NaturalTransitionTest(unittest.TestCase):

    def test_track_3_to_4_without_restarting_the_pipeline(self):
        controller, engine, clock = ready()
        controller.play(3)
        icedax_pid = engine.icedax_pid
        aplay_pid = engine.aplay_pid
        starts = len(engine.start_calls)

        clock.advance(TRACK3_DURATION - 0.5)
        controller.update()
        self.assertEqual(3, controller.current_track)

        clock.advance(1.0)                       # now past the boundary
        controller.update()

        self.assertEqual(4, controller.current_track)
        self.assertEqual(starts, len(engine.start_calls),
                         "natural transition restarted the pipeline")
        self.assertEqual(icedax_pid, engine.icedax_pid)
        self.assertEqual(aplay_pid, engine.aplay_pid)
        self.assertEqual(ctrl.PLAYING, controller.state)

    def test_no_loading_state_during_natural_transition(self):
        controller, _, clock = ready()
        controller.play(3)
        marker = len(controller.transitions)
        clock.advance(TRACK3_DURATION + 1.0)
        controller.update()
        later = [to for _, _, to in controller.transitions[marker:]]
        self.assertNotIn(ctrl.LOADING, later)
        self.assertEqual([], later, "no state change at all should occur")

    def test_track_elapsed_resets_at_the_boundary(self):
        controller, _, clock = ready()
        controller.play(3)
        clock.advance(TRACK3_DURATION + 2.0)
        controller.update()
        self.assertEqual(4, controller.current_track)
        self.assertAlmostEqual(2.0, controller.track_elapsed_seconds(),
                               places=1)

    def test_multiple_consecutive_boundaries(self):
        controller, engine, clock = ready()
        controller.play(3)
        # tracks 3 (12.3s) and 4 (301.6s) then into 5
        clock.advance(TRACK3_DURATION + (63496 - 40875) / 75.0 + 1.0)
        controller.update()
        self.assertEqual(5, controller.current_track)
        self.assertEqual(1, len(engine.start_calls),
                         "crossing several boundaries must not restart icedax")

    def test_pipeline_start_count_is_stable_across_boundaries(self):
        controller, _, clock = ready()
        controller.play(1)
        before = controller.pipeline_start_count
        clock.advance(TRACK1_DURATION + 1.0)
        controller.update()
        self.assertEqual(2, controller.current_track)
        self.assertEqual(before, controller.pipeline_start_count)

    def test_paused_across_a_boundary_does_not_advance(self):
        controller, _, clock = ready()
        controller.play(3)
        clock.advance(5.0)
        controller.pause()
        clock.advance(TRACK3_DURATION * 3)
        controller.update()
        self.assertEqual(3, controller.current_track,
                         "paused time must not advance the track")


# --- next / previous -------------------------------------------------------


class NextPreviousTest(unittest.TestCase):

    def test_next_starts_the_following_track(self):
        controller, engine, _ = ready()
        controller.play(1)
        self.assertTrue(controller.next_track())
        self.assertEqual(2, controller.current_track)
        self.assertEqual(("2,0,0", 2, 11), engine.start_calls[-1])

    def test_next_on_last_track_is_a_no_op(self):
        controller, engine, _ = ready()
        controller.play(11)
        starts = len(engine.start_calls)
        self.assertFalse(controller.next_track())
        self.assertEqual(11, controller.current_track)
        self.assertEqual(starts, len(engine.start_calls))
        self.assertEqual(ctrl.PLAYING, controller.state)

    def test_next_after_natural_transition_uses_the_new_track(self):
        controller, engine, clock = ready()
        controller.play(3)
        clock.advance(TRACK3_DURATION + 1.0)
        controller.update()
        self.assertEqual(4, controller.current_track)
        controller.next_track()
        self.assertEqual(5, controller.current_track)
        self.assertEqual(("2,0,0", 5, 11), engine.start_calls[-1])

    def test_previous_restarts_current_track_after_threshold(self):
        controller, engine, clock = ready()
        controller.play(5)
        clock.advance(4.0)          # > 3.0 s default
        self.assertTrue(controller.previous_track())
        self.assertEqual(5, controller.current_track)
        self.assertEqual(("2,0,0", 5, 11), engine.start_calls[-1])

    def test_previous_steps_back_within_threshold(self):
        controller, engine, clock = ready()
        controller.play(5)
        clock.advance(2.0)          # <= 3.0 s
        self.assertTrue(controller.previous_track())
        self.assertEqual(4, controller.current_track)

    def test_previous_on_first_track_restarts_it(self):
        controller, engine, clock = ready()
        controller.play(1)
        clock.advance(1.0)
        self.assertTrue(controller.previous_track())
        self.assertEqual(1, controller.current_track)
        self.assertEqual(("2,0,0", 1, 11), engine.start_calls[-1])

    def test_previous_threshold_is_configurable(self):
        controller, _, clock = ready(previous_threshold=10.0)
        controller.play(5)
        clock.advance(5.0)          # under the raised threshold
        controller.previous_track()
        self.assertEqual(4, controller.current_track)

    def test_previous_threshold_ignores_paused_time(self):
        controller, _, clock = ready()
        controller.play(5)
        clock.advance(1.0)
        controller.pause()
        clock.advance(600.0)        # ten minutes paused
        controller.resume()
        controller.previous_track()
        self.assertEqual(4, controller.current_track,
                         "paused time must not count toward the threshold")

    def test_next_previous_without_disc_rejected(self):
        controller, _, _ = build(toc=lambda address: no_disc())
        controller.scan_disc()
        self.assertFalse(controller.next_track())
        self.assertFalse(controller.previous_track())


# --- completion, failure, teardown ----------------------------------------


class CompletionTest(unittest.TestCase):

    def test_natural_completion_becomes_ready(self):
        controller, engine, clock = ready()
        controller.play(10)
        engine.report(ok_result())
        state = controller.update()
        self.assertEqual(ctrl.READY, state.state)
        self.assertIsNone(state.last_error)
        self.assertEqual(11, controller.current_track,
                         "last track retained for display")

    def test_disc_is_kept_after_completion(self):
        controller, engine, _ = ready()
        controller.play(1)
        engine.report(ok_result())
        controller.update()
        self.assertIsNotNone(controller.disc)
        self.assertTrue(controller.play(1), "must be playable again")

    def test_playback_failure_becomes_error_then_rescans(self):
        toc_results = [audio_disc(), no_disc()]
        controller, engine, _ = build(
            toc=lambda address: toc_results.pop(0))
        controller.scan_disc()
        controller.play(1)
        engine.report(failed_result("icedax exited 2 during playback"))
        state = controller.update()
        self.assertEqual(ctrl.NO_DISC, state.state,
                         "removal should settle at NO_DISC")
        self.assertIn("icedax exited 2", state.last_error,
                      "the playback failure reason must survive the rescan")

    def test_failure_never_leaves_controller_claiming_playing(self):
        controller, engine, _ = ready()
        controller.play(1)
        engine.report(failed_result())
        controller.update()
        self.assertNotEqual(ctrl.PLAYING, controller.state)

    def test_failure_when_drive_vanished_becomes_no_drive(self):
        discoveries = [drive_found(), no_drive()]
        controller, engine, _ = build(
            discover=lambda: discoveries.pop(0))
        controller.scan_disc()
        controller.play(1)
        engine.report(failed_result())
        self.assertEqual(ctrl.NO_DRIVE, controller.update().state)

    def test_rescan_is_rate_limited(self):
        scans = []

        def counting_discover():
            scans.append(1)
            return drive_found()

        controller, engine, clock = build(discover=counting_discover,
                                          rescan_interval=5.0)
        controller.scan_disc()
        controller.play(1)
        engine.report(failed_result())
        controller.update()
        after_first = len(scans)
        for _ in range(50):
            controller.update()
        self.assertEqual(after_first, len(scans),
                         "update() rescanned in a tight loop")

    def test_update_is_safe_when_idle(self):
        controller, _, _ = build()
        for _ in range(5):
            controller.update()
        self.assertEqual(ctrl.NO_DISC, controller.state)


class StopAndCloseTest(unittest.TestCase):

    def test_stop_is_idempotent(self):
        controller, engine, _ = ready()
        controller.play(1)
        self.assertTrue(controller.stop())
        self.assertTrue(controller.stop())
        self.assertEqual(ctrl.STOPPED, controller.state)
        self.assertFalse(engine.is_running())

    def test_stop_keeps_the_disc_and_current_track(self):
        controller, _, _ = ready()
        controller.play(7)
        controller.stop()
        self.assertIsNotNone(controller.disc)
        self.assertEqual(7, controller.current_track)

    def test_stop_resets_progress(self):
        controller, _, clock = ready()
        controller.play(1)
        clock.advance(30.0)
        controller.stop()
        self.assertEqual(0.0, controller.track_elapsed_seconds())

    def test_play_after_stop_works(self):
        controller, engine, _ = ready()
        controller.play(1)
        controller.stop()
        self.assertTrue(controller.play(2))
        self.assertEqual(2, controller.current_track)

    def test_stop_without_disc_does_not_claim_stopped(self):
        controller, _, _ = build(toc=lambda address: no_disc())
        controller.scan_disc()
        controller.stop()
        self.assertEqual(ctrl.NO_DISC, controller.state)

    def test_close_releases_the_engine(self):
        controller, engine, _ = ready()
        controller.play(1)
        controller.close()
        self.assertFalse(engine.is_running())

    def test_close_is_safe_twice(self):
        controller, _, _ = ready()
        controller.close()
        controller.close()


class SnapshotTest(unittest.TestCase):

    def test_snapshot_fields(self):
        controller, engine, clock = ready()
        controller.play(3)
        clock.advance(5.0)
        state = controller.update()
        self.assertTrue(state.is_playing)
        self.assertFalse(state.is_paused)
        self.assertTrue(state.has_disc)
        self.assertEqual(3, state.current_track)
        self.assertEqual(11, state.track_count)
        self.assertEqual(1, state.first_track)
        self.assertEqual(11, state.last_track)
        self.assertEqual(engine.icedax_pid, state.icedax_pid)
        self.assertEqual(5.84, state.pcm_wait_seconds)

    def test_describe_is_renderable(self):
        controller, _, clock = ready()
        controller.play(3)
        clock.advance(7.0)
        text = controller.update().describe()
        self.assertIn("PLAYING", text)
        self.assertIn("Track: 3 / 11", text)
        self.assertIn("0:07", text)


if __name__ == "__main__":
    unittest.main()
