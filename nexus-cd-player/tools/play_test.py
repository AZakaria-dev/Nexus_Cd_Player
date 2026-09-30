#!/usr/bin/env python3
"""Physical playback acceptance test for the Nexus 7.

    python3 tools/play_test.py --track 1 --seconds 60

Exercises the production path exactly: drive discovery, then one icedax
track-range process piped to aplay, with the LOADING -> PLAYING transition
driven by real PCM arrival. Prints state transitions and, after stopping,
checks that no child processes survive.

Never prints PCM.

The TOC parser is still unverified against this hardware, so --last-track
lets playback be tested even if TOC parsing fails. Playback testing must not
be blocked on the parser.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import device as device_mod  # noqa: E402
from backend import disc as disc_mod  # noqa: E402
from backend import errors  # noqa: E402
from backend import playback  # noqa: E402


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def mmss(seconds):
    whole = int(seconds)
    return "{0}:{1:02d}".format(whole // 60, whole % 60)


def pid_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_stray_processes():
    """Any icedax/aplay left anywhere on the system (acceptance items 10-11)."""
    stray = []
    for name in ("icedax", "aplay"):
        try:
            out = subprocess.Popen(
                ["pgrep", "-a", name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).communicate()[0]
        except OSError:
            return None  # pgrep unavailable; caller should say so
        text = out.decode("utf-8", "replace").strip()
        if text:
            stray.extend(text.splitlines())
    return stray


def resolve_device(requested):
    if requested:
        say("Drive: {0} (from --device)".format(requested))
        return requested
    found = device_mod.discover()
    if found.drive is None:
        say("Drive: NOT FOUND -- " + found.status)
        if found.message:
            say("  " + found.message)
        return None
    say("Drive: {0}  [{1}]".format(found.drive.address, found.drive.describe()))
    if found.message:
        say("  note: " + found.message)
    return found.drive.address


def resolve_last_track(address, override):
    if override:
        say("Last track: {0} (from --last-track, TOC skipped)".format(override))
        return override
    result = disc_mod.read_toc(address)
    if result.status != disc_mod.AUDIO_DISC:
        say("TOC: {0} -- {1}".format(result.status, result.message))
        if result.raw:
            say("--- raw icedax output ---")
            say(result.raw.strip())
        say("")
        say("If this is the unverified TOC format rather than a real disc")
        say("problem, re-run with --last-track to test playback anyway.")
        return None
    disc = result.disc
    say("Disc: {0} tracks, total {1}".format(
        disc.track_count, disc_mod.format_msf(disc.total_duration_seconds)))
    return disc.last_track


def main():
    parser = argparse.ArgumentParser(
        description="Physical playback acceptance test")
    parser.add_argument("--track", type=int, default=1,
                        help="track to start at (default 1)")
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="how long to play before stopping (default 60)")
    parser.add_argument("--device",
                        help="Bus,ID,LUN override; skips discovery")
    parser.add_argument("--last-track", type=int,
                        help="final track of the range; skips the TOC read")
    parser.add_argument("--timeout", type=float,
                        default=playback.config.PCM_READY_TIMEOUT,
                        help="seconds to wait for first PCM")
    parser.add_argument("--use-duration-flag", action="store_true",
                        help="add icedax -d for the test run; NOT the "
                             "production path, for drive isolation only")
    args = parser.parse_args()

    say("Nexus CD Player -- playback acceptance test")
    say("Python {0}.{1}.{2} on {3}".format(
        sys.version_info[0], sys.version_info[1], sys.version_info[2],
        sys.platform))
    say("")

    address = resolve_device(args.device)
    if address is None:
        return 1

    last_track = resolve_last_track(address, args.last_track)
    if last_track is None:
        return 1

    if args.track < 1 or args.track > last_track:
        say("Invalid track {0} (disc has 1-{1})".format(
            args.track, last_track))
        return 1

    duration = args.seconds if args.use_duration_flag else None
    argv = playback.icedax_argv(address, args.track, last_track,
                                duration_seconds=duration)
    say("")
    say("Command: " + " ".join(argv))
    say("         | " + " ".join(playback.aplay_argv()))
    say("")

    engine = playback.PlaybackEngine(pcm_ready_timeout=args.timeout)

    say("State: LOADING")
    say("Starting icedax and waiting for first PCM (no timer, real data)...")

    try:
        waited = engine.start(address, args.track, last_track,
                              duration_seconds=duration)
    except errors.PlaybackError as exc:
        say("")
        say("LOADING FAILED: {0}".format(exc))
        say("")
        say("aplay launched: {0}".format(
            "yes -- BUG" if engine.aplay_pid else "no (correct)"))
        engine.close()
        say("Cleanup complete")
        return 1
    except KeyboardInterrupt:
        engine.close()
        say("\nInterrupted during loading; cleanup complete")
        return 130

    say("PCM available after {0:.2f} seconds".format(waited))
    say("Starting aplay...")
    say("State: PLAYING   (icedax pid {0}, aplay pid {1})".format(
        engine.icedax_pid, engine.aplay_pid))
    say("")

    # The continuity clock starts when PLAYING begins, not when icedax was
    # launched -- spin-up time is not playback.
    playing_since = time.monotonic()
    played = 0.0
    result = None
    interrupted = False

    # Poll health often so the measured duration is accurate, but only print
    # every few seconds. Checking status only between prints would overstate
    # how long playback actually lasted by up to the print interval.
    check_interval = 0.25
    print_interval = 5.0
    next_print = 0.0

    try:
        while True:
            played = time.monotonic() - playing_since
            if played >= args.seconds:
                played = args.seconds
                break
            result = engine.poll_status()
            if result is not None:
                played = time.monotonic() - playing_since
                say("Playback ended after {0:.1f}s: {1}".format(
                    played, result.message))
                break
            if played >= next_print:
                say("  {0} / {1} playing".format(
                    mmss(played), mmss(args.seconds)))
                next_print += print_interval
            time.sleep(check_interval)
    except KeyboardInterrupt:
        played = time.monotonic() - playing_since
        interrupted = True
        say("\nInterrupted by user after {0:.1f}s".format(played))

    icedax_pid = engine.icedax_pid
    aplay_pid = engine.aplay_pid

    say("")
    say("Stopping...")
    engine.stop()

    say("icedax exited: {0}".format(engine.icedax_returncode))
    say("aplay exited: {0}".format(engine.aplay_returncode))
    say("Cleanup complete")
    say("")

    ok = True

    # Sustained-playback check. A COMPLETED result is NOT sufficient: if the
    # stream ends after 15 s of a requested 60 s, continuity has not been
    # demonstrated no matter how cleanly it finished.
    #
    # With --use-duration-flag icedax is *asked* to stop at the requested
    # duration, so finishing right at the boundary is expected and the
    # tolerance covers the drain; otherwise the tolerance only absorbs
    # scheduler jitter.
    tolerance = 2.0 if args.use_duration_flag else 0.5
    required = args.seconds - tolerance

    if interrupted:
        say("RESULT: interrupted after {0:.1f}s -- inconclusive".format(played))
        ok = False
    elif played < required:
        say("FAIL: played only {0:.1f}s of the requested {1:.1f}s "
            "(tolerance {2:.1f}s)".format(played, args.seconds, tolerance))
        if result is not None and result.ok:
            say("      the pipeline shut down cleanly, but it did not "
                "sustain playback for the requested duration")
        ok = False
    else:
        say("Sustained {0:.1f}s of playback (requested {1:.1f}s)".format(
            played, args.seconds))

    if result is not None and not result.ok:
        say("FAIL: {0}".format(result.message))
        ok = False

    time.sleep(0.3)
    for name, pid in (("icedax", icedax_pid), ("aplay", aplay_pid)):
        if pid_alive(pid):
            say("FAIL: {0} pid {1} is still alive".format(name, pid))
            ok = False

    stray = find_stray_processes()
    if stray is None:
        say("(pgrep unavailable; could not scan for stray processes)")
    elif stray:
        say("FAIL: stray processes remain:")
        for line in stray:
            say("  " + line)
        ok = False
    else:
        say("No icedax/aplay processes remain.")

    say("")
    if ok:
        say("PASS: played {0:.1f}s from track {1} with a clean shutdown.".format(
            played, args.track))
        say("Judge continuity by ear: any repeating gap or stutter is a FAIL.")
        return 0
    say("FAIL: see above.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
