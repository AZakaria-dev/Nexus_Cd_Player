"""Playback engine tests.

No CD, no drive, no audio device: icedax and aplay are replaced by small
Python programs that reproduce the behaviours we care about (produces PCM,
dies before PCM, refuses to open the audio device, ignores SIGTERM). That
exercises the real pipe, the real poll() gate and the real teardown code.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import errors  # noqa: E402
from backend import playback  # noqa: E402

IS_LINUX = sys.platform.startswith("linux")

# --- stand-in child programs ----------------------------------------------

# Writes PCM forever (until killed), after an optional startup delay given as
# a DELAY=<seconds> argument (the engine appends its own args after ours, so
# position cannot be relied on).
FAKE_ICEDAX_OK = """
import sys, time
delay = 0.0
for arg in sys.argv:
    if arg.startswith('DELAY='):
        delay = float(arg.split('=', 1)[1])
sys.stderr.write('fake icedax: starting\\n'); sys.stderr.flush()
time.sleep(delay)
block = b'\\0' * 4096
try:
    while True:
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
        time.sleep(0.01)
except Exception:
    sys.exit(0)
"""

# Writes a fixed amount of PCM then exits 0 -- the "track range finished" case.
FAKE_ICEDAX_SHORT = """
import sys
sys.stderr.write('fake icedax: short run\\n'); sys.stderr.flush()
sys.stdout.buffer.write(b'\\0' * 8192)
sys.stdout.buffer.flush()
sys.exit(0)
"""

# Dies before producing any PCM at all.
FAKE_ICEDAX_NO_DISC = """
import sys
sys.stderr.write('fake icedax: No disk / Wrong disk!\\n')
sys.exit(2)
"""

# Produces nothing and never exits -- forces the PCM-ready timeout.
FAKE_ICEDAX_HANG = """
import sys, time
sys.stderr.write('fake icedax: spinning up\\n'); sys.stderr.flush()
time.sleep(120)
"""

# Ignores SIGTERM, so cleanup must escalate to SIGKILL.
FAKE_ICEDAX_STUBBORN = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.stderr.write('fake icedax: ignoring SIGTERM\\n'); sys.stderr.flush()
block = b'\\0' * 4096
while True:
    sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    time.sleep(0.01)
"""

# Produces PCM on its first run, then hangs without producing any on later
# runs -- lets one engine instance do a good run followed by a failing one.
FAKE_ICEDAX_ONCE = """
import os, sys, time
marker = None
for arg in sys.argv:
    if arg.startswith('MARKER='):
        marker = arg.split('=', 1)[1]
if marker and os.path.exists(marker):
    sys.stderr.write('fake icedax: second run, no audio\\n'); sys.stderr.flush()
    time.sleep(120)
open(marker, 'w').write('used')
sys.stderr.write('fake icedax: first run\\n'); sys.stderr.flush()
block = b'\\0' * 4096
while True:
    sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    time.sleep(0.01)
"""

# Consumes PCM quietly until EOF.
FAKE_APLAY_OK = """
import sys
while True:
    chunk = sys.stdin.buffer.read(4096)
    if not chunk:
        break
sys.exit(0)
"""

# Fails immediately, as if the audio device were busy.
FAKE_APLAY_FAILS = """
import sys
sys.stderr.write('fake aplay: cannot open audio device\\n')
sys.exit(1)
"""


def program(source):
    return [sys.executable, "-c", source]


def engine(icedax_source, aplay_source=FAKE_APLAY_OK, timeout=10.0):
    return playback.PlaybackEngine(
        icedax_command=program(icedax_source),
        aplay_command=program(aplay_source),
        pcm_ready_timeout=timeout,
    )


def running_pids(pids):
    """Which of these PIDs are still alive (and not reaped)?"""
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except OSError:
            pass
    return alive


class ArgvTest(unittest.TestCase):
    """The exact command lines proven on the Nexus."""

    def test_icedax_range_playback(self):
        self.assertEqual(
            ["icedax", "-D", "2,0,0", "-I", "generic_scsi",
             "-q", "-H", "-x", "-n", "10", "-O", "raw", "-E", "little",
             "-t", "1+11", "-"],
            playback.icedax_argv("2,0,0", 1, 11, command=["icedax"]),
        )

    def test_range_starts_mid_disc(self):
        argv = playback.icedax_argv("2,0,0", 6, 11, command=["icedax"])
        self.assertIn("-t", argv)
        self.assertEqual("6+11", argv[argv.index("-t") + 1])

    def test_no_duration_flag_by_default(self):
        """-d would cut the stream short of the requested range."""
        self.assertNotIn("-d", playback.icedax_argv("2,0,0", 1, 11))

    def test_overlap_is_not_disabled(self):
        """-P 0 measured slower on this hardware (9.147s vs 7.494s)."""
        self.assertNotIn("-P", playback.icedax_argv("2,0,0", 1, 11))

    def test_duration_is_opt_in_for_tests_only(self):
        argv = playback.icedax_argv("2,0,0", 1, 11, command=["icedax"],
                                    duration_seconds=60)
        self.assertEqual("60", argv[argv.index("-d") + 1])

    def test_sector_count_default_is_ten(self):
        argv = playback.icedax_argv("2,0,0", 1, 11)
        self.assertEqual("10", argv[argv.index("-n") + 1])

    def test_aplay_cd_format(self):
        self.assertEqual(
            ["aplay", "-q", "-t", "raw", "-f", "cd"],
            playback.aplay_argv(command=["aplay"]),
        )

    def test_no_shell_metacharacters_anywhere(self):
        """argv lists only -- nothing is ever handed to a shell."""
        for arg in playback.icedax_argv("2,0,0", 1, 11):
            self.assertNotIn("|", arg)
            self.assertNotIn(">", arg)


class StartAndStopTest(unittest.TestCase):

    def setUp(self):
        self.engine = engine(FAKE_ICEDAX_OK)
        self.addCleanup(self.engine.close)

    def test_reaches_playing_and_reports_pcm_wait(self):
        self.assertEqual(playback.IDLE, self.engine.state)
        waited = self.engine.start("2,0,0", 1, 11)
        self.assertEqual(playback.PLAYING, self.engine.state)
        self.assertTrue(self.engine.is_running())
        self.assertIsNotNone(self.engine.pcm_wait_seconds)
        self.assertGreaterEqual(waited, 0.0)

    def test_parent_closes_its_copy_of_the_pipe(self):
        self.engine.start("2,0,0", 1, 11)
        handle = self.engine._icedax.stdout
        self.assertTrue(handle is None or handle.closed,
                        "parent must not keep a readable copy of the pipe")

    def test_stop_leaves_no_processes(self):
        self.engine.start("2,0,0", 1, 11)
        pids = [self.engine._icedax.pid, self.engine._aplay.pid]
        self.engine.stop()
        self.assertEqual(playback.STOPPED, self.engine.state)
        self.assertFalse(self.engine.is_running())
        time.sleep(0.2)
        self.assertEqual([], running_pids(pids),
                         "icedax/aplay still alive after stop")

    def test_stop_is_idempotent(self):
        self.engine.start("2,0,0", 1, 11)
        self.engine.stop()
        self.engine.stop()
        self.engine.close()
        self.assertFalse(self.engine.is_running())

    def test_stop_without_start_is_safe(self):
        fresh = playback.PlaybackEngine()
        fresh.stop()
        fresh.close()
        self.assertFalse(fresh.is_running())

    def test_double_start_is_rejected(self):
        self.engine.start("2,0,0", 1, 11)
        self.assertRaises(errors.PlaybackError,
                          self.engine.start, "2,0,0", 1, 11)

    def test_no_zombies_after_stop(self):
        self.engine.start("2,0,0", 1, 11)
        self.engine.stop()
        # Popen objects are dropped only after wait(); a surviving zombie
        # would show up as a still-running pid above. Belt and braces:
        self.assertIsNone(self.engine._icedax)
        self.assertIsNone(self.engine._aplay)


class PcmGateTest(unittest.TestCase):

    def test_waits_for_slow_startup(self):
        """PLAYING must not be entered before PCM actually arrives."""
        eng = playback.PlaybackEngine(
            icedax_command=program(FAKE_ICEDAX_OK) + ["DELAY=0.7"],
            aplay_command=program(FAKE_APLAY_OK),
            pcm_ready_timeout=10.0,
        )
        self.addCleanup(eng.close)
        waited = eng.start("2,0,0", 1, 11)
        self.assertGreaterEqual(
            waited, 0.6, "reported PCM wait is shorter than the real delay")
        self.assertEqual(playback.PLAYING, eng.state)

    def test_timeout_when_no_pcm_ever_arrives(self):
        eng = engine(FAKE_ICEDAX_HANG, timeout=1.0)
        self.addCleanup(eng.close)
        started = time.monotonic()
        try:
            eng.start("2,0,0", 1, 11)
        except errors.PlaybackError as exc:
            self.assertIn("no PCM", str(exc))
        else:
            self.fail("expected a loading timeout")
        self.assertGreaterEqual(time.monotonic() - started, 1.0,
                                "must actually wait out the timeout")
        self.assertEqual(playback.ERROR, eng.state)
        self.assertIsNone(eng._aplay, "aplay must never be launched")

    def test_timeout_kills_icedax(self):
        eng = engine(FAKE_ICEDAX_HANG, timeout=1.0)
        self.addCleanup(eng.close)
        self.assertRaises(errors.PlaybackError, eng.start, "2,0,0", 1, 11)
        self.assertIsNone(eng._icedax, "icedax must be cleaned up")

    @unittest.skipUnless(
        IS_LINUX,
        "Linux pipe_poll() reports POLLHUP alone at EOF; BSD/macOS also sets "
        "POLLIN, so this discrimination only holds on the target platform")
    def test_icedax_dying_before_pcm_never_launches_aplay(self):
        """Acceptance item 14."""
        eng = engine(FAKE_ICEDAX_NO_DISC, timeout=5.0)
        self.addCleanup(eng.close)
        try:
            eng.start("2,0,0", 1, 11)
        except errors.PlaybackError as exc:
            message = str(exc)
            self.assertIn("without producing audio", message)
            self.assertIn("No disk", message,
                          "icedax's own stderr must be preserved")
        else:
            self.fail("expected a playback start error")
        self.assertEqual(playback.ERROR, eng.state)
        self.assertIsNone(eng._aplay)

    def test_icedax_failure_is_reported_either_way(self):
        """Platform-independent: a dead icedax must never end up PLAYING."""
        eng = engine(FAKE_ICEDAX_NO_DISC, timeout=5.0)
        self.addCleanup(eng.close)
        try:
            eng.start("2,0,0", 1, 11)
        except errors.PlaybackError:
            self.assertEqual(playback.ERROR, eng.state)
            return
        # macOS may pass the gate on an EOF-only pipe; the failure must then
        # surface immediately from poll_status().
        result = eng.wait(timeout=10.0)
        self.assertIsNotNone(result, "failure was never reported")
        self.assertFalse(result.ok)


class ChildFailureTest(unittest.TestCase):

    def test_aplay_dying_terminates_icedax(self):
        eng = engine(FAKE_ICEDAX_OK, FAKE_APLAY_FAILS)
        self.addCleanup(eng.close)
        eng.start("2,0,0", 1, 11)
        icedax_pid = eng._icedax.pid
        result = eng.wait(timeout=10.0)
        self.assertIsNotNone(result, "aplay death was never noticed")
        self.assertFalse(result.ok)
        self.assertIn("aplay", result.message)
        self.assertEqual(playback.ERROR, eng.state)
        time.sleep(0.2)
        self.assertEqual([], running_pids([icedax_pid]),
                         "icedax must be terminated when aplay dies")

    def test_aplay_error_output_is_preserved(self):
        eng = engine(FAKE_ICEDAX_OK, FAKE_APLAY_FAILS)
        self.addCleanup(eng.close)
        eng.start("2,0,0", 1, 11)
        result = eng.wait(timeout=10.0)
        self.assertIn("cannot open audio device", result.message)

    def test_range_completion_drains_and_completes(self):
        """icedax exiting 0 is success: aplay drains, then we report done."""
        eng = engine(FAKE_ICEDAX_SHORT, FAKE_APLAY_OK)
        self.addCleanup(eng.close)
        eng.start("2,0,0", 1, 11)
        result = eng.wait(timeout=10.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(playback.COMPLETED, result.outcome)
        self.assertEqual(0, result.icedax_returncode)
        self.assertEqual(0, result.aplay_returncode)
        self.assertEqual(playback.STOPPED, eng.state)

    def test_stubborn_child_is_sigkilled(self):
        eng = engine(FAKE_ICEDAX_STUBBORN)
        self.addCleanup(eng.close)
        eng.start("2,0,0", 1, 11)
        pids = [eng._icedax.pid, eng._aplay.pid]
        eng.stop()
        time.sleep(0.3)
        self.assertEqual([], running_pids(pids),
                         "SIGTERM-ignoring child must be SIGKILLed")

    def test_missing_executable_is_a_clean_error(self):
        eng = playback.PlaybackEngine(
            icedax_command=["/nonexistent/icedax"],
            aplay_command=program(FAKE_APLAY_OK),
        )
        self.addCleanup(eng.close)
        try:
            eng.start("2,0,0", 1, 11)
        except errors.PlaybackError as exc:
            self.assertIn("cannot start icedax", str(exc))
        else:
            self.fail("expected PlaybackError")
        self.assertEqual(playback.ERROR, eng.state)


class PauseResumeTest(unittest.TestCase):

    def setUp(self):
        self.engine = engine(FAKE_ICEDAX_OK)
        self.addCleanup(self.engine.close)

    def test_pause_and_resume_state(self):
        self.engine.start("2,0,0", 1, 11)
        self.assertTrue(self.engine.pause())
        self.assertEqual(playback.PAUSED, self.engine.state)
        self.assertTrue(self.engine.is_paused())
        self.assertTrue(self.engine.is_running())
        self.assertTrue(self.engine.resume())
        self.assertEqual(playback.PLAYING, self.engine.state)

    def test_pause_is_not_valid_when_idle(self):
        self.assertFalse(self.engine.pause())
        self.assertFalse(self.engine.resume())

    def test_double_pause_is_harmless(self):
        self.engine.start("2,0,0", 1, 11)
        self.assertTrue(self.engine.pause())
        self.assertFalse(self.engine.pause())
        self.assertEqual(playback.PAUSED, self.engine.state)

    def test_stop_while_paused_still_terminates(self):
        """A SIGSTOPped process ignores SIGTERM until it is continued."""
        self.engine.start("2,0,0", 1, 11)
        pids = [self.engine._icedax.pid, self.engine._aplay.pid]
        self.engine.pause()
        self.engine.stop()
        time.sleep(0.3)
        self.assertEqual([], running_pids(pids),
                         "cleanup must SIGCONT before SIGTERM")


class RunMetadataTest(unittest.TestCase):
    """Per-run metadata must never leak from one run into the next."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.marker = os.path.join(self.tmpdir, "used")
        self.engine = playback.PlaybackEngine(
            icedax_command=program(FAKE_ICEDAX_ONCE) + ["MARKER=" + self.marker],
            aplay_command=program(FAKE_APLAY_OK),
            pcm_ready_timeout=1.5,
        )
        self.addCleanup(self.engine.close)

    def test_fresh_engine_has_no_metadata(self):
        self.assertIsNone(self.engine.icedax_pid)
        self.assertIsNone(self.engine.aplay_pid)
        self.assertIsNone(self.engine.icedax_returncode)
        self.assertIsNone(self.engine.aplay_returncode)

    def test_failed_run_does_not_inherit_previous_aplay_details(self):
        """The bug this guards: a run that never launches aplay must not
        still report the previous run's aplay_pid, or diagnostics would
        claim aplay started when it never did."""
        self.engine.start("2,0,0", 1, 11)
        first_icedax_pid = self.engine.icedax_pid
        self.assertIsNotNone(self.engine.aplay_pid)
        self.engine.stop()
        self.assertIsNotNone(self.engine.aplay_returncode)
        self.assertIsNotNone(self.engine.icedax_returncode)

        # Second run: icedax produces nothing, so the gate times out and
        # aplay is never launched.
        self.assertRaises(errors.PlaybackError,
                          self.engine.start, "2,0,0", 1, 11)

        self.assertIsNone(self.engine.aplay_pid,
                          "aplay_pid leaked from the previous run")
        self.assertIsNone(self.engine.aplay_returncode,
                          "aplay_returncode leaked from the previous run")
        self.assertNotEqual(first_icedax_pid, self.engine.icedax_pid,
                            "icedax_pid is stale from the previous run")

    def test_pcm_wait_and_result_reset_on_restart(self):
        self.engine.start("2,0,0", 1, 11)
        self.engine.stop()
        self.assertIsNotNone(self.engine.result)
        self.assertIsNotNone(self.engine.pcm_wait_seconds)
        try:
            self.engine.start("2,0,0", 1, 11)
        except errors.PlaybackError:
            pass
        self.assertIsNone(self.engine.pcm_wait_seconds,
                          "pcm_wait_seconds leaked from the previous run")


class FileDescriptorTest(unittest.TestCase):

    def test_no_descriptor_leak_across_runs(self):
        """Repeated play/stop cycles must not accumulate open descriptors."""
        def open_count():
            path = "/proc/self/fd"
            if os.path.isdir(path):
                return len(os.listdir(path))
            return len(subprocess.check_output(
                ["lsof", "-p", str(os.getpid())]).splitlines())

        eng = engine(FAKE_ICEDAX_OK)
        self.addCleanup(eng.close)
        eng.start("2,0,0", 1, 11)
        eng.stop()
        baseline = open_count()
        for _ in range(3):
            eng.start("2,0,0", 1, 11)
            eng.stop()
        self.assertLessEqual(open_count(), baseline + 2,
                             "descriptors leaked across play/stop cycles")

    def test_no_temporary_audio_files_created(self):
        """Acceptance item 13: nothing is written to the working directory."""
        before = set(os.listdir("."))
        eng = engine(FAKE_ICEDAX_SHORT)
        self.addCleanup(eng.close)
        eng.start("2,0,0", 1, 11)
        eng.wait(timeout=10.0)
        new = set(os.listdir(".")) - before
        for name in new:
            self.assertFalse(
                name.lower().endswith((".wav", ".raw", ".pcm", ".au")),
                "temporary audio file created: " + name)


if __name__ == "__main__":
    unittest.main()
