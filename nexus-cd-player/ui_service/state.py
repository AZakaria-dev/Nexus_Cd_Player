"""Thread-safe cached UI state.

The HTTP threads and the controller worker share exactly one mutable object:
this one. It never hands out a reference to its internal dict -- every reader
gets a private copy -- so no caller can mutate shared state by accident and no
reader can observe a half-written update.

Nothing here invents CD timing. Every field is mapped from what
CDController's ControllerState already reports.
"""

import copy
import threading

# UI-facing states are the controller's own states; the controller stays the
# source of truth. Listed here only so the QML side has a fixed vocabulary.
UI_STATES = (
    "NO_DRIVE",
    "NO_DISC",
    "READY",
    "LOADING",
    "PLAYING",
    "PAUSED",
    "STOPPED",
    "ERROR",
)

# "busy" is adapter metadata, not a controller concept: it means a blocking
# command is in flight.
DEFAULT_SNAPSHOT = {
    "state": "NO_DISC",
    "busy": False,

    "current_track": None,
    "total_tracks": 0,

    "track_elapsed": 0.0,
    "track_duration": 0.0,

    "disc_elapsed": 0.0,
    "disc_duration": 0.0,

    "has_disc": False,
    "drive_address": None,

    "error": None,
    "service_ready": False,

    # Increments on every publish, so a client can tell a stale poll from a
    # fresh one without needing clock agreement.
    "revision": 0,
}


class UiState(object):
    """A snapshot holder guarded by one lock."""

    def __init__(self, initial=None):
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULT_SNAPSHOT)
        self._data["revision"] = 0
        if initial:
            self._data.update(initial)

    def snapshot(self):
        """A private copy of the current state. Safe to mutate or serialise."""
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, **fields):
        """Merge fields atomically. Unknown keys are rejected loudly.

        Returns the resulting snapshot so a caller never has to do a
        read-after-write that could race with another writer.
        """
        unknown = [key for key in fields if key not in DEFAULT_SNAPSHOT]
        if unknown:
            raise KeyError(
                "unknown UI state field(s): " + ", ".join(sorted(unknown)))
        with self._lock:
            self._data.update(fields)
            self._data["revision"] += 1
            return copy.deepcopy(self._data)

    def replace(self, snapshot):
        """Publish a whole snapshot, preserving the revision counter."""
        unknown = [key for key in snapshot if key not in DEFAULT_SNAPSHOT]
        if unknown:
            raise KeyError(
                "unknown UI state field(s): " + ", ".join(sorted(unknown)))
        with self._lock:
            revision = self._data["revision"] + 1
            self._data = copy.deepcopy(DEFAULT_SNAPSHOT)
            self._data.update(snapshot)
            self._data["revision"] = revision
            return copy.deepcopy(self._data)

    def get(self, key):
        with self._lock:
            return copy.deepcopy(self._data[key])


def snapshot_from_controller(controller_state, busy=False, service_ready=True):
    """Map a CDController ControllerState onto the UI snapshot.

    Pure translation: no timing is computed here, and no field is invented.
    Kept as a free function so it can be tested without a worker or a server.
    """
    return {
        "state": controller_state.state,
        "busy": bool(busy),

        "current_track": controller_state.current_track,
        "total_tracks": controller_state.track_count,

        "track_elapsed": float(controller_state.track_elapsed_seconds),
        "track_duration": float(controller_state.track_duration_seconds),

        "disc_elapsed": float(controller_state.disc_elapsed_seconds),
        "disc_duration": float(controller_state.disc_duration_seconds),

        "has_disc": bool(controller_state.has_disc),
        "drive_address": controller_state.drive_address,

        "error": controller_state.last_error,
        "service_ready": bool(service_ready),
    }
