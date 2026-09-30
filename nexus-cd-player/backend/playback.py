"""Playback engine: icedax -> aplay, with Python out of the PCM path.

    icedax.stdout ----(OS pipe)----> aplay.stdin ----> ALSA

Python creates the pipe, waits for the first PCM to arrive, hands the read end
to aplay and then lets the kernel do all the copying. It never reads, peeks at,
or buffers a single PCM byte, and no temporary RAW/WAV file is ever created.

LOADING -> PLAYING is driven by select.poll() on icedax's stdout. No timer ever
fakes the PLAYING state; the measured ~7-8 s drive spin-up only informs how
generous the timeout should be.

This module deliberately knows nothing about discs, tracks or UI state beyond
the track range it is asked to play. The controller layer sits on top.
"""

import select
import signal
import subprocess
import time

from . import config
from . import errors
from . import log as log_module
from .log import get_logger

log = get_logger(__name__)

# --- engine states ---------------------------------------------------------

IDLE = "IDLE"
LOADING = "LOADING"
PLAYING = "PLAYING"
PAUSED = "PAUSED"
STOPPED = "STOPPED"
ERROR = "ERROR"

# --- outcome constants -----------------------------------------------------

COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"


class PlaybackResult(object):
    """Terminal outcome of one playback run."""

    def __init__(self, outcome, message="", icedax_returncode=None,
                 aplay_returncode=None):
        self.outcome = outcome
        self.message = message
        self.icedax_returncode = icedax_returncode
        self.aplay_returncode = aplay_returncode

    @property
    def ok(self):
        return self.outcome == COMPLETED

    def __repr__(self):
        return "<PlaybackResult {0} icedax={1} aplay={2} {3!r}>".format(
            self.outcome, self.icedax_returncode, self.aplay_returncode,
            self.message)


# --- command construction --------------------------------------------------


def icedax_argv(address, start_track, last_track, command=None,
                sectors_per_request=None, duration_seconds=None):
    """Build the icedax command line.

    Sequential playback uses a track RANGE (-t START+LAST) so one process
    streams straight through natural track boundaries -- restarting icedax at
    every boundary would cost ~7-8 s each time.

    -P 0 is deliberately not forced: measured on this hardware, default
    overlap extracted 10 s in ~7.494 s versus ~9.147 s with -P 0.

    duration_seconds adds -d. It is a TEST-ONLY escape hatch for isolating
    drive behaviour; normal range playback must never pass it, because -d
    would cut the stream short of the requested range.
    """
    if sectors_per_request is None:
        sectors_per_request = config.ICEDAX_SECTORS_PER_REQUEST
    argv = list(command) if command else [config.ICEDAX]
    argv += [
        "-D", address,
        "-I", config.ICEDAX_INTERFACE,
        "-q",                                   # quiet
        "-H",                                   # --no-infofile: write no
                                                # .inf/CDDB side files
        "-x",                                   # maximum quality
        "-n", str(sectors_per_request),         # 10 sectors per request
        "-O", "raw",
        "-E", "little",
    ]
    if duration_seconds is not None:
        argv += ["-d", str(int(duration_seconds))]
    argv += [
        "-t", "{0}+{1}".format(start_track, last_track),
        "-",                                    # PCM to stdout
    ]
    return argv


def aplay_argv(command=None):
    """aplay -q -t raw -f cd  (44100 Hz, 16-bit signed LE, stereo)."""
    argv = list(command) if command else [config.APLAY]
    return argv + ["-q", "-t", "raw", "-f", "cd"]


# --- the engine ------------------------------------------------------------


class PlaybackEngine(object):
    """Owns exactly two child processes and the pipe between them.

    Both children are launched with start_new_session=True and are managed
    individually through their Popen objects -- never through bare PIDs, so a
    reaped child's PID can never be signalled by accident.
    """

    def __init__(self, icedax_command=None, aplay_command=None,
                 pcm_ready_timeout=None):
        self._icedax_command = icedax_command
        self._aplay_command = aplay_command
        self._pcm_ready_timeout = (
            pcm_ready_timeout if pcm_ready_timeout is not None
            else config.PCM_READY_TIMEOUT)

        self._icedax = None
        self._aplay = None
        self._icedax_log = None
        self._aplay_log = None

        self._state = IDLE
        self._reset_run_metadata()

    def _reset_run_metadata(self):
        """Clear everything that describes one run.

        Public PIDs and return codes deliberately outlive cleanup so callers
        can report on a finished run -- which means they MUST be cleared when
        the next run begins. Otherwise a run that fails before aplay starts
        would still expose the previous run's aplay_pid, and diagnostics would
        claim aplay was launched when it never was.
        """
        self._result = None
        self._pcm_wait_seconds = None
        self._started_at = None
        self._icedax_log_path = None
        self._aplay_log_path = None

        self.icedax_returncode = None
        self.aplay_returncode = None
        self.icedax_pid = None
        self.aplay_pid = None

    # -- introspection ------------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def result(self):
        """Terminal PlaybackResult, or None while running."""
        return self._result

    @property
    def pcm_wait_seconds(self):
        """How long icedax took to produce its first PCM. None until it did."""
        return self._pcm_wait_seconds

    def is_running(self):
        return self._state in (LOADING, PLAYING, PAUSED)

    def is_paused(self):
        return self._state == PAUSED

    # -- starting -----------------------------------------------------------

    def start(self, device_address, start_track, last_track,
              sectors_per_request=None, duration_seconds=None):
        """Launch icedax, wait for real PCM, then launch aplay.

        Returns the seconds spent waiting for first PCM. Raises PlaybackError
        (after full cleanup) if audio never materialises -- in which case
        aplay is never launched at all.
        """
        if self.is_running():
            raise errors.PlaybackError(
                "playback already running; stop() before starting again")

        self._reset_run_metadata()
        self._state = LOADING

        argv = icedax_argv(
            device_address, start_track, last_track,
            command=self._icedax_command,
            sectors_per_request=sectors_per_request,
            duration_seconds=duration_seconds,
        )
        log.info("starting icedax: %s", argv)

        try:
            self._icedax_log, self._icedax_log_path = \
                log_module.open_stderr_log("icedax")
            self._icedax = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,       # parent holds the read end
                stderr=self._icedax_log,      # a real file, never an idle PIPE
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup()
            self._state = ERROR
            raise errors.PlaybackError(
                "cannot start icedax: {0}".format(exc))

        self.icedax_pid = self._icedax.pid
        self._started_at = time.monotonic()
        try:
            self._pcm_wait_seconds = self._wait_for_pcm()
        except errors.PlaybackError:
            self._cleanup()
            self._state = ERROR
            raise

        log.info("first PCM after %.2fs; starting aplay",
                 self._pcm_wait_seconds)

        try:
            self._aplay_log, self._aplay_log_path = \
                log_module.open_stderr_log("aplay")
            self._aplay = subprocess.Popen(
                aplay_argv(command=self._aplay_command),
                stdin=self._icedax.stdout,    # inherits the pipe read end
                stdout=subprocess.DEVNULL,
                stderr=self._aplay_log,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            self._cleanup()
            self._state = ERROR
            raise errors.PlaybackError("cannot start aplay: {0}".format(exc))
        finally:
            # Drop the parent's duplicate read end either way. Descriptor
            # hygiene: aplay has its own copy, and a stray read here would
            # steal PCM from it.
            self._close_icedax_stdout()

        self.aplay_pid = self._aplay.pid
        self._state = PLAYING
        return self._pcm_wait_seconds

    def _wait_for_pcm(self):
        """Block until icedax's stdout actually carries PCM.

        Non-consuming: poll() only reports readiness, nothing is read.

        POLLIN wins even when POLLHUP is set alongside it -- data that arrived
        before icedax exited is still perfectly good audio. POLLHUP/POLLERR/
        POLLNVAL *without* POLLIN means icedax closed stdout having produced
        nothing.

        Platform note: on Linux (the target) pipe_poll() raises POLLIN only
        when bytes are genuinely queued, so EOF-without-data is POLLHUP alone.
        BSD/macOS also sets POLLIN at EOF, so this discrimination is weaker
        when developing on a Mac. Behaviour on the Nexus is what counts.
        """
        fd = self._icedax.stdout.fileno()
        poller = select.poll()
        poller.register(
            fd,
            select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL,
        )

        deadline = time.monotonic() + self._pcm_ready_timeout
        interval_ms = int(config.PCM_READY_POLL_INTERVAL * 1000)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise errors.PlaybackError(
                    "no PCM from icedax within {0:.0f}s{1}".format(
                        self._pcm_ready_timeout, self._icedax_diagnosis()))

            events = poller.poll(min(interval_ms,
                                     max(1, int(remaining * 1000))))
            for _fd, mask in events:
                if mask & select.POLLIN:
                    return time.monotonic() - self._started_at
                if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                    raise errors.PlaybackError(
                        "icedax closed its output without producing audio"
                        + self._icedax_diagnosis())

    def _icedax_diagnosis(self):
        """Return code plus the tail of icedax's stderr, for error messages."""
        return self._diagnosis(self._icedax, self._icedax_log,
                               self._icedax_log_path, "icedax")

    def _aplay_diagnosis(self):
        return self._diagnosis(self._aplay, self._aplay_log,
                               self._aplay_log_path, "aplay")

    def _diagnosis(self, proc, handle, path, name):
        """' (name exited N)\\nname said: ...' -- never swallow the reason."""
        text = ""
        if proc is not None:
            code = proc.poll()
            if code is not None:
                text += " ({0} exited {1})".format(name, code)
        if handle is not None:
            try:
                handle.flush()
            except (IOError, OSError, ValueError):
                pass
        if path:
            detail = log_module.tail(path)
            if detail:
                text += "\n{0} said:\n{1}".format(name, detail)
        return text

    # -- pause / resume -----------------------------------------------------

    def pause(self):
        """SIGSTOP icedax then aplay. Returns True if the state changed.

        Producer first, so aplay drains what is already in flight rather than
        being frozen mid-buffer. Audio already inside ALSA still plays out;
        sub-second pause latency is expected and acceptable.
        """
        if self._state != PLAYING:
            return False
        self._signal_child(self._icedax, signal.SIGSTOP, "icedax")
        self._signal_child(self._aplay, signal.SIGSTOP, "aplay")
        self._state = PAUSED
        log.info("paused")
        return True

    def resume(self):
        """SIGCONT aplay then icedax -- consumer before producer."""
        if self._state != PAUSED:
            return False
        self._signal_child(self._aplay, signal.SIGCONT, "aplay")
        self._signal_child(self._icedax, signal.SIGCONT, "icedax")
        self._state = PLAYING
        log.info("resumed")
        return True

    # -- status -------------------------------------------------------------

    def poll_status(self):
        """Non-blocking health check. None while running, else a result.

        Cleans up and records a terminal result the moment either child
        finishes in a way that ends playback.
        """
        if self._result is not None:
            return self._result
        if not self.is_running():
            return None

        icedax_code = self._icedax.poll() if self._icedax else None
        aplay_code = self._aplay.poll() if self._aplay else None

        if aplay_code is not None:
            if icedax_code is None:
                # aplay died with the source still feeding it.
                return self._finish(
                    FAILED,
                    "aplay exited unexpectedly with code {0}{1}".format(
                        aplay_code, self._aplay_diagnosis()),
                    icedax_code, aplay_code)
            if icedax_code == 0 and aplay_code == 0:
                return self._finish(COMPLETED, "playback finished",
                                    icedax_code, aplay_code)
            return self._finish(
                FAILED,
                "playback ended badly (icedax {0}, aplay {1}){2}".format(
                    icedax_code, aplay_code, self._aplay_diagnosis()),
                icedax_code, aplay_code)

        # aplay still alive.
        if icedax_code is not None and icedax_code != 0:
            return self._finish(
                FAILED,
                "icedax exited {0} during playback{1}".format(
                    icedax_code, self._icedax_diagnosis()),
                icedax_code, None)

        # icedax_code == 0 means the track range finished: leave aplay alone
        # so it drains the pipe and ALSA buffer, and report completion on a
        # later call once it exits.
        return None

    def _finish(self, outcome, message, icedax_code, aplay_code):
        if outcome == COMPLETED:
            log.info("playback complete: %s", message)
        else:
            log.error("playback failed: %s", message)
        self._cleanup()
        self._state = STOPPED if outcome == COMPLETED else ERROR
        self._result = PlaybackResult(outcome, message, icedax_code,
                                      aplay_code)
        return self._result

    def wait(self, timeout=None):
        """Block until playback ends. Returns the result, or None on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.poll_status()
            if status is not None:
                return status
            if not self.is_running():
                return self._result
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    # -- stopping -----------------------------------------------------------

    def stop(self):
        """Terminate playback. Idempotent and safe to call at any time."""
        if not self.is_running() and self._icedax is None \
                and self._aplay is None:
            if self._state not in (ERROR, STOPPED):
                self._state = STOPPED
            return self._result

        log.info("stopping playback")
        self._cleanup()
        if self._result is None:
            self._state = STOPPED
            self._result = PlaybackResult(
                CANCELLED, "stopped by request",
                self.icedax_returncode, self.aplay_returncode)
        return self._result

    def close(self):
        self.stop()

    # -- internals ----------------------------------------------------------

    def _signal_child(self, proc, sig, name):
        """Signal a child only while it is demonstrably still ours.

        Guarding on poll() is what prevents ever signalling a PID that has
        been reaped and possibly reused by an unrelated process.
        """
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.send_signal(sig)
            return True
        except OSError as exc:
            # Includes ProcessLookupError: the child exited between the poll()
            # above and the signal. Harmless, but never silent.
            log.warning("could not signal %s: %s", name, exc)
            return False

    def _close_icedax_stdout(self):
        if self._icedax is None or self._icedax.stdout is None:
            return
        try:
            if not self._icedax.stdout.closed:
                self._icedax.stdout.close()
        except (IOError, OSError, ValueError) as exc:
            log.warning("closing icedax stdout: %s", exc)

    def _cleanup(self):
        """Terminate both children and release every owned descriptor.

        Idempotent. Order matters: a SIGSTOPped process would never act on
        SIGTERM, so everything is SIGCONTed first.
        """
        children = []
        if self._icedax is not None:
            children.append((self._icedax, "icedax"))
        if self._aplay is not None:
            children.append((self._aplay, "aplay"))

        for proc, name in children:
            self._signal_child(proc, signal.SIGCONT, name)
        for proc, name in children:
            self._signal_child(proc, signal.SIGTERM, name)

        deadline = time.monotonic() + config.TERM_GRACE_SECONDS
        for proc, name in children:
            self._wait_until(proc, deadline)

        for proc, name in children:
            if proc.poll() is None:
                log.warning("%s ignored SIGTERM; sending SIGKILL", name)
                self._signal_child(proc, signal.SIGKILL, name)

        # Reap unconditionally so nothing is left as a zombie.
        kill_deadline = time.monotonic() + config.KILL_GRACE_SECONDS
        for proc, name in children:
            self._wait_until(proc, kill_deadline)
            if proc.poll() is None:
                log.error("%s survived SIGKILL", name)
            else:
                log.debug("%s exited %s", name, proc.returncode)

        if self._icedax is not None:
            self.icedax_returncode = self._icedax.returncode
        if self._aplay is not None:
            self.aplay_returncode = self._aplay.returncode

        self._close_icedax_stdout()
        for handle, name in ((self._icedax_log, "icedax log"),
                             (self._aplay_log, "aplay log")):
            if handle is None:
                continue
            try:
                handle.close()
            except (IOError, OSError, ValueError) as exc:
                log.warning("closing %s: %s", name, exc)

        self._icedax = None
        self._aplay = None
        self._icedax_log = None
        self._aplay_log = None

    def _wait_until(self, proc, deadline):
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if proc.poll() is not None:
            try:
                proc.wait()
            except OSError:
                pass
