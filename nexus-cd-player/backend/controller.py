"""CD player controller: turns the proven PlaybackEngine into a CD player.

Layering:

    cli.py / future GUI
        |
    CDController          <- state machine, track logic, progress
        |
    device.discover()  disc.read_toc()  PlaybackEngine
        |
    icedax -> aplay

The controller never touches processes or PCM directly; PlaybackEngine owns
those and its architecture is frozen. Drive discovery and TOC parsing are
likewise reused as-is.

Polling model: nothing runs in a background thread. Call update() from
whatever loop already exists (CLI prompt, GUI timer). It is cheap and safe to
call as often as you like -- it never re-reads the TOC during playback.

SUPPORTED DISC-CHANGE WORKFLOW
------------------------------

    PLAYING -> stop() -> [icedax/aplay cleaned up]
            -> user presses the drive's physical eject button
            -> user swaps the disc
            -> scan_disc() -> READY -> play()

Changing the disc while playback is running is NOT supported. Hot-ejecting
mid-playback made this Nexus's USB/SCSI stack drop the optical drive
completely -- /dev/sg* disappeared and only a physical reconnect restored it,
at a different SCSI address. There is therefore deliberately no media-presence
polling, no sg_turs watchdog and no background SCSI probing: nothing here
touches the drive except when the user asks.

Generic PlaybackEngine failure handling remains, so if icedax or aplay does
die for any reason the controller still cleans up, reports it and rescans
once. That is a safety net, not a supported disc-change mechanism.

Python 3.5.2 compatible.
"""

import time

from . import config
from . import device as device_mod
from . import disc as disc_mod
from . import errors
from . import playback
from .log import get_logger

log = get_logger(__name__)

# --- controller states -----------------------------------------------------
#
# Deliberately distinct from playback.* : those describe one pipeline, these
# describe the whole appliance and include the disc-level states a pipeline
# knows nothing about. Overlapping names carry identical string values.

NO_DRIVE = "NO_DRIVE"
NO_DISC = "NO_DISC"
READY = "READY"
LOADING = "LOADING"
PLAYING = "PLAYING"
PAUSED = "PAUSED"
STOPPED = "STOPPED"
ERROR = "ERROR"

ACTIVE_STATES = (PLAYING, PAUSED)
HAS_DISC_STATES = (READY, LOADING, PLAYING, PAUSED, STOPPED)


class ControllerState(object):
    """Immutable snapshot of everything a UI needs to render one frame."""

    def __init__(self, state, disc=None, current_track=None,
                 track_elapsed_seconds=0.0, track_duration_seconds=0.0,
                 disc_elapsed_seconds=0.0, disc_duration_seconds=0.0,
                 last_error=None, drive_address=None,
                 icedax_pid=None, aplay_pid=None, pcm_wait_seconds=None):
        self.state = state
        self.disc = disc
        self.current_track = current_track
        self.track_elapsed_seconds = track_elapsed_seconds
        self.track_duration_seconds = track_duration_seconds
        self.disc_elapsed_seconds = disc_elapsed_seconds
        self.disc_duration_seconds = disc_duration_seconds
        self.last_error = last_error
        self.drive_address = drive_address
        self.icedax_pid = icedax_pid
        self.aplay_pid = aplay_pid
        self.pcm_wait_seconds = pcm_wait_seconds

    @property
    def is_playing(self):
        return self.state == PLAYING

    @property
    def is_paused(self):
        return self.state == PAUSED

    @property
    def has_disc(self):
        return self.disc is not None

    @property
    def track_count(self):
        return self.disc.track_count if self.disc else 0

    @property
    def first_track(self):
        return self.disc.first_track if self.disc else None

    @property
    def last_track(self):
        return self.disc.last_track if self.disc else None

    def describe(self):
        lines = ["State: " + self.state]
        if self.disc is not None and self.current_track is not None:
            lines.append("Track: {0} / {1}".format(
                self.current_track, self.track_count))
            lines.append("Time: {0} / {1}".format(
                disc_mod.format_ms(self.track_elapsed_seconds),
                disc_mod.format_ms(self.track_duration_seconds)))
        elif self.disc is not None:
            lines.append("Disc: {0} tracks".format(self.track_count))
        if self.last_error:
            lines.append("Error: " + self.last_error)
        return "\n".join(lines)

    def __repr__(self):
        return "<ControllerState {0} track={1}>".format(
            self.state, self.current_track)


class CDController(object):
    """The CD player.

    Dependencies are injectable so unit tests never need a drive, a disc,
    icedax, aplay, or real time.
    """

    def __init__(self, engine=None, discover_fn=None, read_toc_fn=None,
                 clock=None, previous_threshold=None, rescan_interval=None):
        self._engine = engine if engine is not None else playback.PlaybackEngine()
        self._discover = discover_fn or device_mod.discover
        self._read_toc = read_toc_fn or disc_mod.read_toc
        self._clock = clock or time.monotonic
        self._previous_threshold = (
            previous_threshold if previous_threshold is not None
            else config.PREVIOUS_RESTART_THRESHOLD)
        self._rescan_interval = (
            rescan_interval if rescan_interval is not None else 5.0)

        self._state = NO_DISC
        self._disc = None
        self._drive_address = None
        self._current_track = None
        self._range_start_track = None

        self._playing_started_at = None
        self._accumulated_pause = 0.0
        self._pause_started_at = None

        self.last_error = None
        self._needs_rescan = False
        self._last_rescan_at = None

        # Observability, so callers (and the hardware acceptance tool) can
        # prove that a natural track transition did NOT restart the pipeline.
        self.pipeline_start_count = 0
        self.transitions = []

    # -- state plumbing -----------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def disc(self):
        return self._disc

    @property
    def current_track(self):
        return self._current_track

    @property
    def drive_address(self):
        return self._drive_address

    def _set_state(self, new_state):
        if new_state == self._state:
            return
        log.info("state %s -> %s", self._state, new_state)
        self.transitions.append((self._clock(), self._state, new_state))
        self._state = new_state

    def _reject(self, message):
        """User error: record it, change nothing, report failure."""
        log.warning("rejected: %s", message)
        self.last_error = message
        return False

    # -- disc scanning ------------------------------------------------------

    def scan_disc(self):
        """Discover the drive and read the TOC. Caches the result.

        Stops any active playback first: reading the TOC competes with icedax
        for the same drive, so scanning mid-playback is never done implicitly.
        update() and get_state() never call this.
        """
        if self._engine.is_running():
            log.info("stopping playback for an explicit disc scan")
            self._stop_pipeline()

        found = self._discover()
        if found.status == device_mod.NO_DRIVE:
            self._disc = None
            self._drive_address = None
            self._current_track = None
            self.last_error = found.message or "no optical drive found"
            self._set_state(NO_DRIVE)
            return self.get_state()
        if found.status == device_mod.ERROR or found.drive is None:
            self._disc = None
            self.last_error = found.message or "drive discovery failed"
            self._set_state(ERROR)
            return self.get_state()

        self._drive_address = found.drive.address
        result = self._read_toc(self._drive_address)

        if result.status == disc_mod.AUDIO_DISC:
            self._disc = result.disc
            self._current_track = result.disc.first_track
            self._range_start_track = None
            self._reset_timing()
            self.last_error = None
            self._set_state(READY)
        elif result.status == disc_mod.NO_DISC:
            self._disc = None
            self._current_track = None
            self.last_error = result.message or "no disc in the drive"
            self._set_state(NO_DISC)
        elif result.status == disc_mod.UNSUPPORTED_DISC:
            # A data disc is not an error, it simply has nothing to play.
            self._disc = None
            self._current_track = None
            self.last_error = result.message or "disc has no audio tracks"
            self._set_state(NO_DISC)
        else:
            self._disc = None
            self._current_track = None
            self.last_error = result.message or "could not read the disc"
            self._set_state(ERROR)

        return self.get_state()

    # -- timing -------------------------------------------------------------

    def _reset_timing(self):
        self._playing_started_at = None
        self._accumulated_pause = 0.0
        self._pause_started_at = None

    def _active_elapsed(self):
        """Seconds of real playback since the pipeline started, pause excluded.

        While paused, 'now' is pinned to the moment the pause began, so
        progress freezes without any special-casing elsewhere.
        """
        if self._playing_started_at is None:
            return 0.0
        now = self._clock()
        if self._pause_started_at is not None:
            now = self._pause_started_at
        elapsed = now - self._playing_started_at - self._accumulated_pause
        return elapsed if elapsed > 0.0 else 0.0

    def _absolute_sector(self):
        """Where on the disc the stream has reached, in sectors."""
        if self._disc is None or self._range_start_track is None:
            return None
        start = self._disc.get_track(self._range_start_track).start_sector
        return start + int(self._active_elapsed() * config.SECTORS_PER_SECOND)

    def track_elapsed_seconds(self):
        """How far into the CURRENT track playback has reached."""
        sector = self._absolute_sector()
        if sector is None or self._current_track is None:
            return 0.0
        if not self._disc.has_track(self._current_track):
            return 0.0
        track = self._disc.get_track(self._current_track)
        elapsed = float(sector - track.start_sector) / config.SECTORS_PER_SECOND
        return elapsed if elapsed > 0.0 else 0.0

    def track_duration_seconds(self):
        if self._disc is None or self._current_track is None:
            return 0.0
        if not self._disc.has_track(self._current_track):
            return 0.0
        return self._disc.get_track(self._current_track).duration_seconds

    def disc_elapsed_seconds(self):
        sector = self._absolute_sector()
        if sector is None:
            return 0.0
        first = self._disc.tracks[0].start_sector
        elapsed = float(sector - first) / config.SECTORS_PER_SECOND
        return elapsed if elapsed > 0.0 else 0.0

    def disc_duration_seconds(self):
        if self._disc is None:
            return 0.0
        return self._disc.total_duration_seconds

    # -- track helpers ------------------------------------------------------

    def _last_audio_track(self):
        audio = self._disc.audio_tracks
        return audio[-1].number if audio else self._disc.last_track

    def _first_audio_track(self):
        audio = self._disc.audio_tracks
        return audio[0].number if audio else self._disc.first_track

    def _validate_track(self, number):
        if not self._disc.has_track(number):
            return self._reject(
                "no track {0} on this disc (tracks {1}-{2})".format(
                    number, self._disc.first_track, self._disc.last_track))
        if not self._disc.get_track(number).is_audio:
            return self._reject(
                "track {0} is not an audio track".format(number))
        return True

    # -- playback -----------------------------------------------------------

    def play(self, track_number=None):
        """Start playback. Returns True if playback was (re)started.

        play() with no argument while PAUSED resumes rather than restarting;
        otherwise it starts at the first audio track.
        """
        if self._state == PAUSED and track_number is None:
            return self.resume()
        if self._disc is None:
            return self._reject("no audio disc; run a disc scan first")
        if track_number is None:
            track_number = self._first_audio_track()
        if self._validate_track(track_number) is not True:
            return False
        return self._start_pipeline(track_number)

    def _start_pipeline(self, track_number):
        """Stop any current pipeline and start a range from track_number.

        Always a RANGE through the final audio track, so natural boundaries
        after this one need no further icedax restarts.
        """
        self._stop_pipeline()
        self._set_state(LOADING)
        self._reset_timing()
        self._current_track = track_number
        self._range_start_track = track_number

        last = self._last_audio_track()
        self.pipeline_start_count += 1
        log.info("starting pipeline: tracks %s..%s", track_number, last)

        try:
            self._engine.start(self._drive_address, track_number, last)
        except errors.PlaybackError as exc:
            self.last_error = str(exc)
            log.error("pipeline failed to start: %s", exc)
            self._stop_pipeline()
            self._needs_rescan = True
            self._set_state(ERROR)
            return False

        self._playing_started_at = self._clock()
        self._accumulated_pause = 0.0
        self._pause_started_at = None
        self.last_error = None
        self._set_state(PLAYING)
        return True

    def _stop_pipeline(self):
        try:
            self._engine.stop()
        except Exception as exc:            # never let teardown mask a state
            log.error("error stopping playback engine: %s", exc)

    def pause(self):
        if self._state != PLAYING:
            return self._reject("not playing")
        if not self._engine.pause():
            return self._reject("playback engine refused to pause")
        self._pause_started_at = self._clock()
        self._set_state(PAUSED)
        return True

    def resume(self):
        if self._state != PAUSED:
            return self._reject("not paused")
        if not self._engine.resume():
            return self._reject("playback engine refused to resume")
        if self._pause_started_at is not None:
            self._accumulated_pause += self._clock() - self._pause_started_at
            self._pause_started_at = None
        self._set_state(PLAYING)
        return True

    def stop(self):
        """Stop playback. Idempotent.

        The cached DiscInfo is kept -- stopping says nothing about whether the
        disc is still there -- and current_track is retained for display so
        the UI can still show what was last selected.
        """
        self._stop_pipeline()
        self._reset_timing()
        self._range_start_track = None
        if self._disc is not None:
            self._set_state(STOPPED)
        return True

    def next_track(self):
        """Advance one track. At the last track this is a no-op (no wrap)."""
        if self._disc is None:
            return self._reject("no audio disc; run a disc scan first")
        last = self._last_audio_track()
        if self._current_track is None:
            return self._start_pipeline(self._first_audio_track())
        if self._current_track >= last:
            return self._reject("already at the last track")
        return self._start_pipeline(self._current_track + 1)

    def previous_track(self):
        """Conventional CD behaviour.

        More than PREVIOUS_RESTART_THRESHOLD seconds into the current track,
        restart it; otherwise step back. On the first track, restart it.
        Paused time does not count toward the threshold.
        """
        if self._disc is None:
            return self._reject("no audio disc; run a disc scan first")
        first = self._first_audio_track()
        current = self._current_track
        if current is None:
            return self._start_pipeline(first)
        if self.track_elapsed_seconds() > self._previous_threshold:
            return self._start_pipeline(current)
        if current > first:
            return self._start_pipeline(current - 1)
        return self._start_pipeline(first)

    # -- the polling entry point -------------------------------------------

    def update(self):
        """Advance logical state. Cheap, safe to call repeatedly.

        Never reads the TOC while playback is running.
        """
        if self._state in ACTIVE_STATES:
            result = self._engine.poll_status()
            if result is None:
                self._refresh_current_track()
            elif result.ok:
                self._on_completion()
            else:
                self._on_failure(result)

        if self._needs_rescan:
            self._maybe_rescan()

        return self.get_state()

    def _refresh_current_track(self):
        """Natural track transition: logical only, no process is touched."""
        if self._disc is None or self._range_start_track is None:
            return
        number = self._disc.track_at_offset(
            self._active_elapsed(), self._range_start_track)
        if number != self._current_track:
            log.info("natural track transition %s -> %s (no pipeline restart)",
                     self._current_track, number)
            self._current_track = number

    def _on_completion(self):
        """The selected range finished normally."""
        log.info("playback range completed")
        self._stop_pipeline()
        self._reset_timing()
        self._range_start_track = None
        if self._disc is not None:
            self._current_track = self._last_audio_track()
        self.last_error = None
        # READY, not STOPPED: the disc is still in the drive and immediately
        # playable again.
        self._set_state(READY)

    def _on_failure(self, result):
        log.error("playback failed: %s", result.message)
        self.last_error = result.message
        self._stop_pipeline()
        self._reset_timing()
        self._range_start_track = None
        self._needs_rescan = True
        self._set_state(ERROR)

    def _maybe_rescan(self):
        """One rate-limited rescan after a failure -- never a tight loop."""
        now = self._clock()
        if (self._last_rescan_at is not None
                and now - self._last_rescan_at < self._rescan_interval):
            return
        self._last_rescan_at = now
        self._needs_rescan = False

        playback_error = self.last_error
        log.info("rescanning after playback failure")
        self.scan_disc()
        if playback_error and self._state != READY:
            # Keep the reason playback died; the scan result explains the
            # resulting state but not what went wrong.
            self.last_error = playback_error

    # -- snapshot / teardown ------------------------------------------------

    def get_state(self):
        return ControllerState(
            state=self._state,
            disc=self._disc,
            current_track=self._current_track,
            track_elapsed_seconds=self.track_elapsed_seconds(),
            track_duration_seconds=self.track_duration_seconds(),
            disc_elapsed_seconds=self.disc_elapsed_seconds(),
            disc_duration_seconds=self.disc_duration_seconds(),
            last_error=self.last_error,
            drive_address=self._drive_address,
            icedax_pid=getattr(self._engine, "icedax_pid", None),
            aplay_pid=getattr(self._engine, "aplay_pid", None),
            pcm_wait_seconds=self._engine.pcm_wait_seconds,
        )

    def close(self):
        """Release playback resources. Safe to call more than once."""
        try:
            self._engine.close()
        except Exception as exc:
            log.error("error closing playback engine: %s", exc)
        self._reset_timing()
        self._range_start_track = None
