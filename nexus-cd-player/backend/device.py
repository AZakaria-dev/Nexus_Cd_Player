"""Optical drive discovery via `icedax -scanbus`.

Deliberately does NOT look for /dev/sr0 or /dev/cdrom: the Nexus 7 kernel
(3.4.0-4-flo) exposes the USB-attached PLDS drive only through the generic
SCSI layer (a /dev/sg* node). icedax speaks to it with -I generic_scsi using a
Bus,ID,LUN address, so that address is the only device identity we need.

Discovery result is a DriveInfo plus one of:
    NO_DRIVE / DRIVE_PRESENT / ERROR
"""

import errno
import glob
import os
import re
import subprocess

from . import config
from . import errors
from .log import get_logger

log = get_logger(__name__)

# --- status constants ------------------------------------------------------

NO_DRIVE = "NO_DRIVE"
DRIVE_PRESENT = "DRIVE_PRESENT"
ERROR = "ERROR"


# --- data holders ----------------------------------------------------------


class DriveInfo(object):
    """One entry from the scanbus listing."""

    def __init__(self, bus, target, lun, vendor="", model="", revision="",
                 kind="", discovered=True):
        self.bus = int(bus)
        self.target = int(target)
        self.lun = int(lun)
        self.vendor = vendor
        self.model = model
        self.revision = revision
        self.kind = kind          # e.g. "Removable CD-ROM"
        self.discovered = discovered  # False => came from configured fallback

    @property
    def address(self):
        """The Bus,ID,LUN string icedax wants after -D."""
        return "{0},{1},{2}".format(self.bus, self.target, self.lun)

    def describe(self):
        name = (self.vendor + " " + self.model).strip()
        if not name:
            name = "unknown device"
        text = "{0} at {1}".format(name, self.address)
        if self.revision:
            text += " (fw {0})".format(self.revision)
        if not self.discovered:
            text += " [configured fallback]"
        return text

    def __repr__(self):
        return "<DriveInfo {0}>".format(self.describe())


class DeviceResult(object):
    """Outcome of a discovery attempt."""

    def __init__(self, status, drive=None, message="", candidates=None,
                 raw=""):
        self.status = status
        self.drive = drive
        self.message = message
        self.candidates = candidates or []
        self.raw = raw

    def __repr__(self):
        return "<DeviceResult {0} {1!r}>".format(self.status, self.drive)


# --- scanbus parsing -------------------------------------------------------

# Matches the address at the start of a scanbus device line, tolerating both
# the raw cdrtools layout:
#     \t2,0,0\t200) 'PLDS    ' 'DVD+-RW DH-16ABS' 'PD11' Removable CD-ROM
# and a whitespace-collapsed form:
#     2,0,0  'PLDS' 'DVD+-RW DH-16ABS' 'PD11' Removable CD-ROM
_ADDRESS_RE = re.compile(r"^\s*(\d+),(\d+),(\d+)\s+(?:\d+\)\s*)?(.*)$")
_QUOTED_RE = re.compile(r"'([^']*)'")

# Device-type words that mean "this can read a CD".
_OPTICAL_WORDS = ("CD-ROM", "CD-R", "CD-RW", "DVD", "BD-ROM", "WORM")

# scanbus prints '*' for empty target slots.
_EMPTY_SLOT = "*"


def parse_scanbus(text):
    """Parse `icedax -scanbus` output into a list of DriveInfo.

    Returns every device line found, optical or not; use is_optical() or
    select_optical() to filter. Unparseable lines are ignored rather than
    fatal -- scanbus interleaves banner and bus-header lines.
    """
    drives = []
    for line in text.splitlines():
        match = _ADDRESS_RE.match(line)
        if not match:
            continue
        tail = match.group(4).strip()
        if not tail or tail.startswith(_EMPTY_SLOT):
            continue
        quoted = _QUOTED_RE.findall(tail)
        vendor = quoted[0].strip() if len(quoted) > 0 else ""
        model = quoted[1].strip() if len(quoted) > 1 else ""
        revision = quoted[2].strip() if len(quoted) > 2 else ""
        # Everything after the last quoted field is the device type.
        kind = tail
        if quoted:
            last = tail.rfind("'")
            kind = tail[last + 1:]
        drives.append(
            DriveInfo(
                match.group(1), match.group(2), match.group(3),
                vendor=vendor, model=model, revision=revision,
                kind=kind.strip(),
            )
        )
    return drives


def is_optical(drive):
    """True when a scanbus entry looks like a CD/DVD reader."""
    haystack = " ".join([drive.kind, drive.vendor, drive.model]).upper()
    for word in _OPTICAL_WORDS:
        if word in haystack:
            return True
    return False


def select_optical(drives):
    """Pick the optical drive to use, preferring lowest Bus,ID,LUN."""
    optical = [d for d in drives if is_optical(d)]
    optical.sort(key=lambda d: (d.bus, d.target, d.lun))
    return optical


# --- running scanbus -------------------------------------------------------


def _run_scanbus(icedax=None, timeout=None):
    """Run `icedax -scanbus`, returning combined stdout+stderr text.

    cdrtools writes the scanbus listing to stderr in some builds and stdout
    in others, so both are captured and concatenated. Both pipes are fully
    read, never left dangling.
    """
    icedax = icedax or config.ICEDAX
    timeout = timeout or config.SCANBUS_TIMEOUT
    argv = [icedax, "-scanbus"]
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
            raise errors.ToolMissingError(
                "icedax not found on PATH (looked for {0!r})".format(icedax)
            )
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise errors.ToolMissingError(
                "not permitted to execute {0}: {1}".format(icedax, exc)
            )
        raise errors.ToolMissingError(
            "cannot execute {0}: {1}".format(icedax, exc)
        )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise errors.ToolFailedError(
            "icedax -scanbus timed out after {0:.0f}s".format(timeout)
        )
    text = _decode(out) + "\n" + _decode(err)
    log.debug("scanbus rc=%s output:\n%s", proc.returncode, text)
    return proc.returncode, text


def _decode(data):
    if not data:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


# --- generic SCSI node evidence -------------------------------------------
#
# icedax prints the same "cannot open or use SCSI driver ... make sure you are
# root" text whether the device node is MISSING or merely UNREADABLE. Taking
# that wording as proof of a permission problem produced a badly wrong
# diagnosis on real hardware: after a hot eject the Nexus USB/SCSI stack
# dropped the drive entirely, /dev/sg* disappeared, and the user was told to
# fix permissions that were already correct.
#
# So the wording is treated as "the scan failed", nothing more, and the
# filesystem decides which failure it was.

SG_NODE_GLOB = "/dev/sg*"

# Phrases that mean "the scan did not work", with no implied cause.
_SCAN_FAILURE_PHRASES = (
    "cannot open or use scsi driver",
    "cannot open scsi driver",
    "could not open",
    "no such file or directory",
    "make sure you are root",
    "permission denied",
    "operation not permitted",
)

# Explicit permission wording. Corroborating evidence, never sufficient alone:
# an EACCES message is meaningless if the node does not exist at all.
_PERMISSION_PHRASES = (
    "permission denied",
    "operation not permitted",
)


def find_sg_nodes(pattern=None):
    """Generic SCSI device nodes currently present on the system.

    Deliberately a glob, never a hardcoded /dev/sg0: this drive has been
    physically observed to move from 2,0,0 to 8,0,0 across a USB reconnect,
    and the node numbering moves with it.
    """
    try:
        return sorted(glob.glob(pattern or SG_NODE_GLOB))
    except OSError:
        return []


def _is_accessible(path):
    try:
        return os.access(path, os.R_OK | os.W_OK)
    except OSError:
        return False


def looks_like_scan_failure(text, returncode=0):
    """Did the scan fail at all? Says nothing about why."""
    if returncode != 0:
        return True
    lowered = text.lower()
    for phrase in _SCAN_FAILURE_PHRASES:
        if phrase in lowered:
            return True
    return False


def _mentions_permission(text):
    lowered = text.lower()
    for phrase in _PERMISSION_PHRASES:
        if phrase in lowered:
            return True
    return False


def discover(icedax=None, allow_fallback=None, fallback_address=None,
             sg_nodes=None, is_accessible=None):
    """Find the optical drive.

    Returns a DeviceResult. Never raises for the ordinary "no drive plugged
    in" case -- that is a status, not an exception.
    """
    try:
        returncode, text = _run_scanbus(icedax=icedax)
    except errors.ToolMissingError as exc:
        return DeviceResult(ERROR, message=str(exc))
    except errors.ToolFailedError as exc:
        return DeviceResult(ERROR, message=str(exc))

    return classify_scanbus(text, returncode=returncode,
                            allow_fallback=allow_fallback,
                            fallback_address=fallback_address,
                            sg_nodes=sg_nodes,
                            is_accessible=is_accessible)


def classify_scanbus(text, returncode=0, allow_fallback=None,
                     fallback_address=None, sg_nodes=None,
                     is_accessible=None):
    """Turn scanbus output into a DeviceResult.

    Split out from discover() so the decision logic is testable without an
    optical drive attached.

        optical device listed                   -> DRIVE_PRESENT
        scan succeeded, none listed             -> NO_DRIVE
        scan failed, no /dev/sg* nodes exist    -> NO_DRIVE  (not attached)
        scan failed, nodes exist but unreadable -> ERROR (permission)
        scan failed for any other reason        -> ERROR

    The node check is what stops a vanished drive being reported as a
    permission problem. See the notes above _SCAN_FAILURE_PHRASES.

    The configured fallback address is used only when fallback is explicitly
    enabled; it is off by default so NO_DRIVE keeps its meaning.
    """
    if allow_fallback is None:
        allow_fallback = config.ALLOW_FALLBACK_ADDRESS
    if fallback_address is None:
        fallback_address = config.FALLBACK_DEVICE_ADDRESS
    if sg_nodes is None:
        sg_nodes = find_sg_nodes()
    if is_accessible is None:
        is_accessible = _is_accessible

    drives = parse_scanbus(text)
    optical = select_optical(drives)

    if optical:
        # A drive we can actually see beats any warning text in the output.
        chosen = optical[0]
        log.info("discovered optical drive: %s", chosen.describe())
        return DeviceResult(DRIVE_PRESENT, drive=chosen,
                            candidates=optical, raw=text)

    if looks_like_scan_failure(text, returncode):
        return _classify_scan_failure(text, returncode, sg_nodes,
                                      is_accessible)

    if allow_fallback:
        fallback = _make_fallback(fallback_address)
        if fallback is not None:
            log.warning(
                "scanbus listed no optical drive (rc=%s); using explicitly "
                "enabled fallback %s", returncode, fallback.address)
            return DeviceResult(
                DRIVE_PRESENT, drive=fallback, raw=text,
                message="no optical drive in scanbus output; using fallback "
                        "address " + fallback.address + " (fallback is "
                        "enabled; this drive was not actually detected)",
            )
        log.error("fallback enabled but address %r is malformed",
                  fallback_address)

    return DeviceResult(
        NO_DRIVE,
        message="no optical drive found in icedax -scanbus output",
        raw=text,
    )


def _classify_scan_failure(text, returncode, sg_nodes, is_accessible):
    """Decide *why* a failed scan failed, using filesystem evidence."""
    if not sg_nodes:
        # Nothing to have permission to. Physically observed on this Nexus
        # after a hot eject: /dev/sg* vanished entirely and the drive had to
        # be reconnected (reappearing at a different SCSI address).
        log.warning("scanbus failed and no %s nodes exist: drive not attached",
                    SG_NODE_GLOB)
        return DeviceResult(
            NO_DRIVE,
            message="no optical drive: icedax could not reach a SCSI device "
                    "and no {0} nodes exist, so the drive is not attached "
                    "(check power and the USB/OTG connection)".format(
                        SG_NODE_GLOB),
            raw=text,
        )

    blocked = [node for node in sg_nodes if not is_accessible(node)]
    if blocked:
        log.error("generic SCSI nodes exist but are not accessible: %s",
                  blocked)
        return DeviceResult(
            ERROR,
            message="no permission to use the generic SCSI device(s) {0}; "
                    "add this user to the owning group".format(
                        ", ".join(blocked)),
            raw=text,
        )

    if _mentions_permission(text):
        return DeviceResult(
            ERROR,
            message="icedax reported a permission failure even though {0} "
                    "is accessible; check group membership for the current "
                    "session".format(", ".join(sg_nodes)),
            raw=text,
        )

    return DeviceResult(
        ERROR,
        message="icedax -scanbus failed (exit {0}) and the reason was not "
                "recognised; {1} exist and are accessible".format(
                    returncode, ", ".join(sg_nodes)),
        raw=text,
    )


def _make_fallback(address):
    parts = address.split(",")
    if len(parts) != 3:
        log.error("malformed fallback address %r", address)
        return None
    try:
        bus, target, lun = [int(p.strip()) for p in parts]
    except ValueError:
        log.error("malformed fallback address %r", address)
        return None
    return DriveInfo(bus, target, lun, discovered=False)
