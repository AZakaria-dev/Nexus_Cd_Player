"""Exception types for the Nexus CD player backend.

Every failure path raises one of these (or is converted into an explicit
state + message by the controller). Nothing is swallowed silently.
"""


class BackendError(Exception):
    """Base class for all backend errors."""


class ToolMissingError(BackendError):
    """icedax / aplay is not installed or not executable."""


class ToolFailedError(BackendError):
    """An external tool ran but exited non-zero or timed out."""

    def __init__(self, message, returncode=None, output=None):
        BackendError.__init__(self, message)
        self.returncode = returncode
        self.output = output or ""


class PermissionError_(BackendError):
    """The generic SCSI device exists but is not accessible to this user."""


class NoDriveError(BackendError):
    """No optical drive could be found."""


class NoDiscError(BackendError):
    """The drive is present but holds no readable medium."""


class UnsupportedDiscError(BackendError):
    """A disc is present but contains no playable CD-DA audio tracks."""


class ParseError(BackendError):
    """Output from an external tool did not match any known format.

    Always carries the raw text so the failure is diagnosable rather than
    mysterious.
    """

    def __init__(self, message, raw=""):
        BackendError.__init__(self, message)
        self.raw = raw

    def __str__(self):
        base = BackendError.__str__(self)
        if not self.raw:
            return base
        return base + "\n--- raw output ---\n" + self.raw.strip()


class InvalidTrackError(BackendError):
    """Requested track number is outside the disc's track range."""


class PlaybackError(BackendError):
    """A playback pipeline failed to start or died unexpectedly."""
