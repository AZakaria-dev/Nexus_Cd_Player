"""Disc / TOC layer.

Reads the table of contents through icedax's information mode:

    icedax -D <bus,id,lun> -I generic_scsi -g -J -H -v toc,sectors

-g selects machine/GUI-oriented output, -J means "information only, do not
extract", and -H is --no-infofile: it suppresses the .inf/CDDB side files
icedax would otherwise write next to its output. (-H is NOT about stream
headers -- for the audio path it is -O raw that yields headerless PCM.) We
want -H because an appliance must not scatter files around. No block device
is involved anywhere.

PARSER STATUS: CONFIRMED against real output from this Nexus, captured in
tests/fixtures/icedax_toc_real.txt. Real -g lines look like:

    T01:       0  5:08.43
    ...
    Leadout:  178442

i.e. a track number, a start sector and a duration -- with NO audio/data
annotation. The parser also accepts the annotated
"T01:  <sector>  audio ..." variant, since the annotation is optional.

Consequence: because real output never says "audio" or "data", every track
defaults to is_audio=True, so UNSUPPORTED_DISC will not trigger for a
data-only disc on this drive. Resolving that needs a real data disc captured
from the Nexus; guessing would be worse than the current honest default.

Anything the parser does not recognise fails loudly via ParseError with the
complete raw output attached -- no zoo of speculative formats.

Sector arithmetic uses 75 sectors per second. Verified against the real test
disc: leadout 178442 / 75 = 2379.23 s = 39:39.17 in MSF, which is exactly
what icedax reports as the total time.

Status is one of:
    NO_DISC / AUDIO_DISC / UNSUPPORTED_DISC / ERROR
"""

import errno
import re
import subprocess
import time

from . import config
from . import errors
from .log import get_logger

log = get_logger(__name__)

NO_DISC = "NO_DISC"
AUDIO_DISC = "AUDIO_DISC"
UNSUPPORTED_DISC = "UNSUPPORTED_DISC"
ERROR = "ERROR"


# --- data holders ----------------------------------------------------------


class TrackInfo(object):

    def __init__(self, number, start_sector, end_sector, is_audio=True):
        self.number = number
        self.start_sector = start_sector
        self.end_sector = end_sector          # inclusive
        self.is_audio = is_audio

    @property
    def sector_count(self):
        return self.end_sector - self.start_sector + 1

    @property
    def duration_seconds(self):
        return float(self.sector_count) / config.SECTORS_PER_SECOND

    def __repr__(self):
        return "<Track {0} {1}-{2} {3}>".format(
            self.number, self.start_sector, self.end_sector,
            format_msf(self.duration_seconds),
        )


class DiscInfo(object):

    def __init__(self, tracks, leadout_sector):
        self.tracks = tracks
        self.leadout_sector = leadout_sector

    @property
    def first_track(self):
        return self.tracks[0].number

    @property
    def last_track(self):
        return self.tracks[-1].number

    @property
    def track_count(self):
        return len(self.tracks)

    @property
    def audio_tracks(self):
        return [t for t in self.tracks if t.is_audio]

    @property
    def total_duration_seconds(self):
        first = self.tracks[0].start_sector
        return float(self.leadout_sector - first) / config.SECTORS_PER_SECOND

    def get_track(self, number):
        for track in self.tracks:
            if track.number == number:
                return track
        raise errors.InvalidTrackError(
            "no track {0} on this disc (tracks {1}-{2})".format(
                number, self.first_track, self.last_track)
        )

    def has_track(self, number):
        for track in self.tracks:
            if track.number == number:
                return True
        return False

    def track_at_offset(self, seconds_from_start_of, start_track):
        """Which track is playing `seconds` into a run that began at
        `start_track`?

        Used for automatic track advance: one long icedax process streams
        several tracks, and the controller maps elapsed time onto boundaries
        without restarting anything.
        """
        start = self.get_track(start_track)
        target_sector = start.start_sector + int(
            seconds_from_start_of * config.SECTORS_PER_SECOND)
        current = start_track
        for track in self.tracks:
            if track.number < start_track:
                continue
            if track.start_sector <= target_sector <= track.end_sector:
                return track.number
            if target_sector > track.end_sector:
                current = track.number
        return current

    def __repr__(self):
        return "<DiscInfo {0} tracks, {1}>".format(
            self.track_count, format_msf(self.total_duration_seconds))


# --- formatting helpers ----------------------------------------------------


def format_msf(seconds):
    """Format seconds as M:SS.FF (frames), the way icedax reports times."""
    frames_total = int(round(seconds * config.SECTORS_PER_SECOND))
    frames = frames_total % config.SECTORS_PER_SECOND
    whole = frames_total // config.SECTORS_PER_SECOND
    return "{0}:{1:02d}.{2:02d}".format(whole // 60, whole % 60, frames)


def format_ms(seconds):
    """Format seconds as M:SS, for user-facing progress display."""
    whole = int(seconds)
    return "{0}:{1:02d}".format(whole // 60, whole % 60)


# --- TOC parsing -----------------------------------------------------------

# The ONE recognised track-line format, icedax/cdda2wav style:
#     T01:       0  audio linear copydenied stereo title '' from ''
# Unconfirmed against this Nexus -- see the module docstring.
_TRACK_T_RE = re.compile(
    r"^\s*T(?P<num>\d+)\s*:\s*(?P<start>\d+)\b(?P<rest>.*)$", re.IGNORECASE)

# "Leadout: 178442" / "lead-out: 178442" / "leadout 178442"
_LEADOUT_RE = re.compile(
    r"^\s*lead\s*-?\s*out\s*:?\s*(?P<sector>\d+)\s*$", re.IGNORECASE)

# Phrases icedax emits when the tray is empty or the medium is unreadable.
_NO_DISC_PHRASES = (
    "no disk",
    "no disc",
    "wrong disk",
    "wrong disc",
    "load cdrom please",
    "please insert",
    "medium not present",
    "not ready",
    "no medium found",
    "cannot load media",
    "can't load media",
)

# Explicit permission wording only. "Cannot open or use SCSI driver ... make
# sure you are root" is icedax's generic failure text -- it appears just as
# readily when the device node does not exist, so treating it as a permission
# problem produces a wrong diagnosis. Drive-level classification (including
# the /dev/sg* existence check) belongs to device.classify_scanbus().
_PERMISSION_PHRASES = (
    "permission denied",
    "operation not permitted",
)


def looks_like_no_disc(text):
    lowered = text.lower()
    for phrase in _NO_DISC_PHRASES:
        if phrase in lowered:
            return True
    return False


def looks_like_permission_problem(text):
    lowered = text.lower()
    for phrase in _PERMISSION_PHRASES:
        if phrase in lowered:
            return True
    return False


def _classify_track_line(rest):
    """Decide audio vs data from the annotation after the start sector.

    Returns (is_audio, annotated).
    """
    lowered = rest.lower()
    if "audio" in lowered:
        return True, True
    if "data" in lowered:
        return False, True
    return True, False


def parse_toc(text):
    """Parse icedax TOC output into a DiscInfo.

    Raises ParseError (carrying the raw text) if no tracks or no leadout
    could be recognised. Never guesses a disc into existence.
    """
    starts = []          # list of (number, start_sector, is_audio)
    leadout = None
    any_annotated = False

    for line in text.splitlines():
        match = _LEADOUT_RE.match(line)
        if match:
            leadout = int(match.group("sector"))
            continue

        match = _TRACK_T_RE.match(line)
        if match:
            is_audio, annotated = _classify_track_line(match.group("rest"))
            any_annotated = any_annotated or annotated
            starts.append(
                (int(match.group("num")), int(match.group("start")), is_audio))

    if not starts:
        raise errors.ParseError(
            "no track entries recognised in icedax TOC output", raw=text)
    if leadout is None:
        raise errors.ParseError(
            "no lead-out sector found in icedax TOC output", raw=text)

    if not any_annotated:
        log.debug("TOC had no audio/data annotations; assuming all audio")

    return build_disc(starts, leadout, raw=text)


def build_disc(entries, leadout, raw=""):
    """Turn (track_number, start_sector, is_audio) entries into a DiscInfo.

    Holds all the sector arithmetic: a track ends where the next one begins,
    and the last ends at lead-out minus one. Separated from parse_toc so the
    arithmetic can be tested against real measured sectors without depending
    on a text format we have not yet observed.
    """
    # Deduplicate and order by track number -- never rely on dict ordering,
    # this has to run on 3.5.
    seen = {}
    for number, start, is_audio in entries:
        if number not in seen:
            seen[number] = (start, is_audio)
    numbers = sorted(seen.keys())

    tracks = []
    for index, number in enumerate(numbers):
        start, is_audio = seen[number]
        if index + 1 < len(numbers):
            end = seen[numbers[index + 1]][0] - 1
        else:
            end = leadout - 1
        if end < start:
            raise errors.ParseError(
                "track {0} has non-increasing sector range ({1} > {2})".format(
                    number, start, end),
                raw=raw,
            )
        tracks.append(TrackInfo(number, start, end, is_audio=is_audio))

    if leadout <= tracks[0].start_sector:
        raise errors.ParseError(
            "lead-out sector {0} is not after first track start {1}".format(
                leadout, tracks[0].start_sector),
            raw=raw,
        )

    return DiscInfo(tracks, leadout)


# --- reading the TOC from hardware -----------------------------------------


class DiscResult(object):

    def __init__(self, status, disc=None, message="", raw="",
                 timed_out=False):
        self.status = status
        self.disc = disc
        self.message = message
        self.raw = raw
        # True only for a genuine read timeout -- the one condition worth
        # retrying. Every other failure is a real answer.
        self.timed_out = timed_out

    def __repr__(self):
        return "<DiscResult {0} {1!r}>".format(self.status, self.disc)


def toc_argv(address, icedax=None):
    """The exact command used to read the TOC."""
    return [
        icedax or config.ICEDAX,
        "-D", address,
        "-I", config.ICEDAX_INTERFACE,
        "-g",           # machine/GUI-oriented output
        "-J",           # information only, do not extract
        "-H",           # --no-infofile: write no .inf/CDDB side files
        "-v", "toc,sectors",
    ]


def _decode(data):
    if not data:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def read_toc(address, icedax=None, timeout=None, retries=None,
             retry_delay=None, attempt=None, sleep=None):
    """Read and classify the disc, retrying ONCE on a timeout.

    A TOC read timed out once on real hardware and succeeded immediately on
    the next attempt (drive/USB wake-up latency). Only a timeout is retried:
    NO_DISC, permission failures and malformed-but-complete output are
    answers, not transients.

    `attempt` and `sleep` are injectable so the retry policy can be tested
    without a drive.
    """
    attempt = attempt or _read_toc_once
    sleep = sleep or time.sleep
    if retries is None:
        retries = config.TOC_TIMEOUT_RETRIES
    if retry_delay is None:
        retry_delay = config.TOC_RETRY_DELAY

    attempts = 0
    while True:
        result = attempt(address, icedax, timeout)
        attempts += 1
        if not result.timed_out:
            return result
        if attempts > retries:
            if attempts > 1:
                result.message += " ({0} attempts)".format(attempts)
            log.error("TOC read timed out after %s attempt(s)", attempts)
            return result
        log.warning("TOC read timed out; retrying once in %.1fs", retry_delay)
        sleep(retry_delay)


def _read_toc_once(address, icedax=None, timeout=None):
    """One icedax info-mode invocation.

    stdin is /dev/null so icedax can never block the appliance waiting for a
    "press enter" prompt.
    """
    timeout = timeout or config.TOC_TIMEOUT
    argv = toc_argv(address, icedax=icedax)
    log.debug("running %s", argv)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return DiscResult(
                ERROR,
                message="icedax not found on PATH (looked for {0!r})".format(
                    icedax or config.ICEDAX),
            )
        return DiscResult(
            ERROR, message="cannot execute icedax: {0}".format(exc))

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return DiscResult(
            ERROR,
            message="icedax TOC read timed out after {0:.0f}s".format(timeout),
            timed_out=True,
        )

    text = _decode(out) + "\n" + _decode(err)
    log.debug("toc rc=%s output:\n%s", proc.returncode, text)
    return classify_toc_output(text, proc.returncode)


def classify_toc_output(text, returncode=0):
    """Turn raw icedax output + exit code into a DiscResult.

    Split out from read_toc so it can be tested against fixtures without
    hardware.
    """
    if looks_like_permission_problem(text):
        return DiscResult(
            ERROR,
            message="no permission to use the generic SCSI device; add this "
                    "user to the group owning the /dev/sg* node",
            raw=text,
        )
    if looks_like_no_disc(text):
        return DiscResult(NO_DISC, message="no disc in the drive", raw=text)

    try:
        disc = parse_toc(text)
    except errors.ParseError as exc:
        # NO_DISC is only ever returned on a positive match against the
        # known no-medium phrases above. An unrecognised failure is an
        # unrecognised failure: guessing "empty tray" would hide real SCSI,
        # cabling and tool problems behind a benign-looking state.
        detail = exc.args[0] if exc.args else "unrecognised TOC output"
        if returncode != 0:
            message = ("icedax exited {0} and its output was not "
                       "recognised: {1}".format(returncode, detail))
        else:
            message = detail
        return DiscResult(ERROR, message=message, raw=text)

    if not disc.audio_tracks:
        return DiscResult(
            UNSUPPORTED_DISC,
            disc=disc,
            message="disc has {0} track(s) but none are CD-DA audio".format(
                disc.track_count),
            raw=text,
        )

    return DiscResult(AUDIO_DISC, disc=disc, raw=text)
