"""Tunable constants for the Nexus CD player backend.

Everything that might reasonably need adjusting on the target hardware lives
here so the rest of the code stays free of magic numbers.

Python 3.5.2 compatible: no f-strings, no dataclasses, no variable annotations.
"""

import os

# --- external programs -----------------------------------------------------

ICEDAX = os.environ.get("NEXUS_CD_ICEDAX", "icedax")
APLAY = os.environ.get("NEXUS_CD_APLAY", "aplay")

# --- drive discovery -------------------------------------------------------

# Troubleshooting fallback, used ONLY when explicitly enabled below.
FALLBACK_DEVICE_ADDRESS = os.environ.get("NEXUS_CD_DEVICE", "2,0,0")

# OFF by default, deliberately. icedax -scanbus is physically verified to find
# the PLDS drive on this Nexus, so a scan that finds no optical device means
# NO_DRIVE -- something is genuinely wrong. Falling back to a guessed address
# there would report DRIVE_PRESENT for a drive that is not connected and make
# the NO_DRIVE state meaningless. Set NEXUS_CD_ALLOW_FALLBACK=1 to opt in when
# troubleshooting.
ALLOW_FALLBACK_ADDRESS = os.environ.get("NEXUS_CD_ALLOW_FALLBACK", "0") == "1"

ICEDAX_INTERFACE = "generic_scsi"

# --- timeouts (seconds) ----------------------------------------------------

SCANBUS_TIMEOUT = 20.0
TOC_TIMEOUT = 30.0

# A TOC read has been observed to time out once on this hardware and then
# succeed immediately on a second attempt -- occasional drive/USB wake-up
# latency. Exactly one extra attempt is made, and ONLY for a timeout: a
# malformed read, a missing disc or a permission failure are all answers, not
# transients, and retrying them would just double the delay before the user
# sees the truth.
TOC_TIMEOUT_RETRIES = 1
TOC_RETRY_DELAY = 1.0

# --- CD geometry -----------------------------------------------------------

SECTORS_PER_SECOND = 75
BYTES_PER_SECTOR = 2352

# CD-DA PCM format produced by icedax -O raw -E little
SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE  # 176400

# --- playback --------------------------------------------------------------

# Sectors per SCSI READ CD request. 10 has been proven reliable on this
# exact USB/SATA/SCSI path. Do not raise without re-measuring on hardware.
ICEDAX_SECTORS_PER_REQUEST = 10

# How long to wait for icedax to produce its first PCM byte before giving up.
# Drive spin-up measured roughly 7-8 s on this hardware; this is a generous
# ceiling for a cold or dirty disc, NOT a timer that fakes the PLAYING state.
PCM_READY_TIMEOUT = 30.0

# select() wakeup granularity while waiting for first PCM, so a dead icedax
# is noticed promptly even in the odd case where no EOF arrives.
PCM_READY_POLL_INTERVAL = 0.25

# Teardown: SIGTERM, wait this long, then SIGKILL.
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0

# Conventional CD-player "previous" behaviour: past this many seconds into a
# track, PREV restarts the current track instead of stepping back.
PREVIOUS_RESTART_THRESHOLD = 3.0

# --- logging ---------------------------------------------------------------

LOG_DIR = os.environ.get(
    "NEXUS_CD_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
)
LOG_FILE = os.path.join(LOG_DIR, "nexus-cd-player.log")
