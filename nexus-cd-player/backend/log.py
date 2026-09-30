"""Logging setup.

Child-process stderr is routed to a log file rather than to a PIPE nobody
reads (an unread PIPE will eventually block icedax when the buffer fills).
"""

import logging
import os

from . import config

# One configured parent logger; every module gets a child of it. Configuring
# per-module instead would leave all but the first module handler-less, and
# logging's handler of last resort would then dump warnings straight onto the
# terminal -- unacceptable for an appliance whose stderr the UI may show.
_ROOT_NAME = "nexus_cd"
_configured = False


def _ensure_log_dir():
    try:
        if not os.path.isdir(config.LOG_DIR):
            os.makedirs(config.LOG_DIR)
    except OSError:
        pass


def _configure_root():
    global _configured
    if _configured:
        return logging.getLogger(_ROOT_NAME)
    _ensure_log_dir()
    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    try:
        handler = logging.FileHandler(config.LOG_FILE)
    except (IOError, OSError):
        handler = logging.NullHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.propagate = False       # never reaches the real root logger
    _configured = True
    return root


def get_logger(name=None):
    """Child logger under the one configured parent."""
    root = _configure_root()
    if not name or name == _ROOT_NAME:
        return root
    return logging.getLogger(_ROOT_NAME + "." + name)


def child_log_path(tag):
    return os.path.join(config.LOG_DIR, tag + ".log")


def open_stderr_log(tag):
    """Return (file_object, path) for a child process's stderr.

    Truncated per run: on an appliance an append-only child log would grow
    without bound, and the narrative lives in the main log anyway. The caller
    owns the handle and must close it during cleanup.

    Falls back to os.devnull if the log directory is not writable, so a
    read-only filesystem can never break playback.
    """
    _ensure_log_dir()
    path = child_log_path(tag)
    try:
        return open(path, "wb"), path
    except (IOError, OSError):
        return open(os.devnull, "wb"), os.devnull


def tail(path, max_lines=15):
    """Last few lines of a child log, for attaching to error messages."""
    try:
        handle = open(path, "rb")
    except (IOError, OSError):
        return ""
    try:
        data = handle.read()
    finally:
        handle.close()
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])
