#!/usr/bin/env python3
"""Physical controller acceptance tests for the Nexus 7.

    python3 tools/controller_test.py            # all automatic scenarios
    python3 tools/controller_test.py natural    # just one
    python3 tools/controller_test.py --list

Scenarios:

    pause     A. pause/resume freezes and continues progress
    next      B. Next reaches the following track
    previous  C. previous-track threshold behaviour
    natural   D. natural Track 3 -> 4 with NO pipeline restart  (the key one)
    stop      E. stop leaves no icedax/aplay processes
    removal   F. disc removal settles at NO_DISC   (interactive)

Scenario D is the fastest physical proof that range playback works: track 3
on the test disc is only 12.3 s long, so the boundary arrives quickly.

Judge audio continuity by ear; this tool cannot hear.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import controller as ctrl  # noqa: E402
from backend import disc as disc_mod  # noqa: E402


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def check(label, condition, detail=""):
    say("  [{0}] {1}{2}".format(
        "PASS" if condition else "FAIL", label,
        ("  -- " + detail) if detail else ""))
    return bool(condition)


def pump(controller, seconds, label=None, interval=0.25):
    """Call update() steadily for a while, as a UI event loop would."""
    deadline = time.monotonic() + seconds
    last_print = 0.0
    while time.monotonic() < deadline:
        state = controller.update()
        now = time.monotonic()
        if label and now - last_print >= 2.0:
            say("      {0}: {1} track {2} {3}".format(
                label, state.state, state.current_track,
                disc_mod.format_ms(state.track_elapsed_seconds)))
            last_print = now
        time.sleep(interval)
    return controller.update()


def stray_processes():
    found = []
    for name in ("icedax", "aplay"):
        try:
            out = subprocess.Popen(
                ["pgrep", "-a", name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).communicate()[0]
        except OSError:
            return None
        text = out.decode("utf-8", "replace").strip()
        if text:
            found.extend(text.splitlines())
    return found


# --- scenarios -------------------------------------------------------------


def scenario_pause(controller):
    """A. Pause holds position; resume continues from it."""
    say("Starting Track 1 (icedax spin-up takes several seconds)...")
    if not controller.play(1):
        return check("playback started", False, controller.last_error)
    ok = check("reached PLAYING", controller.state == ctrl.PLAYING)

    pump(controller, 10.0, "playing")
    before = controller.update().track_elapsed_seconds
    say("    position before pause: {0:.1f}s".format(before))

    ok &= check("pause accepted", controller.pause())
    ok &= check("state is PAUSED", controller.state == ctrl.PAUSED)
    say("    audio should be SILENT now; holding 5 s...")

    pump(controller, 5.0)
    during = controller.update().track_elapsed_seconds
    say("    position after 5 s paused: {0:.1f}s".format(during))
    ok &= check("progress frozen while paused", abs(during - before) < 0.5,
                "moved {0:.2f}s".format(during - before))

    ok &= check("resume accepted", controller.resume())
    say("    audio should be PLAYING again...")
    pump(controller, 4.0)
    after = controller.update().track_elapsed_seconds
    say("    position after 4 s resumed: {0:.1f}s".format(after))
    ok &= check("progress continued from the held position",
                after > during + 2.0,
                "advanced {0:.2f}s".format(after - during))
    ok &= check("did not restart the track", after > before)

    controller.stop()
    return ok


def scenario_next(controller):
    """B. Next reaches the following track."""
    say("Starting Track 1...")
    if not controller.play(1):
        return check("playback started", False, controller.last_error)
    pump(controller, 6.0, "playing")

    starts_before = controller.pipeline_start_count
    say("    Next (expect a several-second reload)...")
    ok = check("next accepted", controller.next_track())
    ok &= check("current track is 2", controller.current_track == 2,
                "got {0}".format(controller.current_track))
    ok &= check("pipeline was restarted (expected for a manual jump)",
                controller.pipeline_start_count > starts_before)
    ok &= check("LOADING was entered",
                any(to == ctrl.LOADING
                    for _, _, to in controller.transitions))
    ok &= check("state is PLAYING", controller.state == ctrl.PLAYING)
    pump(controller, 4.0, "playing")

    controller.stop()
    return ok


def scenario_previous(controller):
    """C. Previous-track threshold."""
    say("Starting Track 2...")
    if not controller.play(2):
        return check("playback started", False, controller.last_error)

    pump(controller, 6.0, "playing")   # > 3 s threshold
    say("    Previous after >3 s: expect Track 2 to restart...")
    ok = check("previous accepted", controller.previous_track())
    ok &= check("restarted Track 2", controller.current_track == 2,
                "got {0}".format(controller.current_track))

    say("    Previous again immediately (<=3 s): expect Track 1...")
    elapsed = controller.update().track_elapsed_seconds
    ok &= check("still within the threshold", elapsed <= 3.0,
                "elapsed {0:.1f}s".format(elapsed))
    ok &= check("previous accepted", controller.previous_track())
    ok &= check("moved to Track 1", controller.current_track == 1,
                "got {0}".format(controller.current_track))

    controller.stop()
    return ok


def scenario_natural(controller):
    """D. Natural Track 3 -> 4 without restarting icedax. THE KEY TEST."""
    disc = controller.disc
    track3 = disc.get_track(3)
    say("Track 3 is {0} sectors = {1:.2f}s".format(
        track3.sector_count, track3.duration_seconds))
    say("Starting Track 3...")
    if not controller.play(3):
        return check("playback started", False, controller.last_error)

    state = controller.update()
    icedax_pid = state.icedax_pid
    aplay_pid = state.aplay_pid
    starts = controller.pipeline_start_count
    marker = len(controller.transitions)
    say("    icedax pid {0}, aplay pid {1}".format(icedax_pid, aplay_pid))
    say("    waiting for the natural boundary "
        "({0:.1f}s of playback)...".format(track3.duration_seconds))

    deadline = time.monotonic() + track3.duration_seconds + 8.0
    transitioned_at = None
    while time.monotonic() < deadline:
        state = controller.update()
        if state.current_track != 3:
            transitioned_at = state.disc_elapsed_seconds
            break
        time.sleep(0.1)

    state = controller.update()
    ok = check("current track advanced to 4", state.current_track == 4,
               "got {0}".format(state.current_track))
    ok &= check("icedax pid unchanged", state.icedax_pid == icedax_pid,
                "{0} -> {1}".format(icedax_pid, state.icedax_pid))
    ok &= check("aplay pid unchanged", state.aplay_pid == aplay_pid,
                "{0} -> {1}".format(aplay_pid, state.aplay_pid))
    ok &= check("pipeline was NOT restarted",
                controller.pipeline_start_count == starts)
    later = [to for _, _, to in controller.transitions[marker:]]
    ok &= check("no LOADING state occurred", ctrl.LOADING not in later,
                "saw {0}".format(later))
    ok &= check("still PLAYING", state.state == ctrl.PLAYING)
    if transitioned_at is not None:
        say("    transition observed at disc position {0:.2f}s "
            "(track 4 starts at {1:.2f}s)".format(
                transitioned_at, 40875 / 75.0))

    say("    listening for a few more seconds -- there must be NO audible gap")
    pump(controller, 5.0, "playing")
    controller.stop()
    return ok


def scenario_stop(controller):
    """E. Stop leaves nothing behind."""
    say("Starting Track 1...")
    if not controller.play(1):
        return check("playback started", False, controller.last_error)
    pump(controller, 5.0, "playing")

    controller.stop()
    ok = check("state is STOPPED", controller.state == ctrl.STOPPED)
    ok &= check("stop is idempotent", controller.stop())
    time.sleep(0.5)

    stray = stray_processes()
    if stray is None:
        say("  [SKIP] pgrep unavailable; cannot scan for stray processes")
    else:
        ok &= check("no icedax/aplay processes remain", not stray,
                    "; ".join(stray))
    ok &= check("disc still cached", controller.disc is not None)
    return ok


def scenario_removal(controller):
    """F. UNSUPPORTED diagnostic: hot eject during playback.

    NOT a Backend V1 acceptance requirement and never run by default.

    Hot-ejecting mid-playback made the Nexus USB/SCSI stack drop the optical
    drive entirely: /dev/sg* vanished and only a physical reconnect brought it
    back (at a different SCSI address). This scenario exists to observe that
    behaviour, not to certify it. The supported disc-change workflow is
    stop -> eject -> swap -> scan.
    """
    say("*** UNSUPPORTED diagnostic scenario. Hot eject can wedge this")
    say("*** Nexus's USB/SCSI stack until the drive is reconnected.")
    say("*** The supported workflow is: stop -> eject -> swap -> scan.")
    say("")
    say("Starting Track 1...")
    if not controller.play(1):
        return check("playback started", False, controller.last_error)
    pump(controller, 5.0, "playing")

    say("")
    say("    >>> REMOVE THE DISC NOW (eject the drive) <<<")
    say("    waiting up to 90 s for the controller to notice...")

    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        state = controller.update()
        if state.state not in (ctrl.PLAYING, ctrl.PAUSED):
            break
        time.sleep(0.25)

    # Allow the rescan to settle.
    settle = time.monotonic() + 20.0
    while time.monotonic() < settle:
        state = controller.update()
        if state.state in (ctrl.NO_DISC, ctrl.NO_DRIVE):
            break
        time.sleep(0.5)

    state = controller.update()
    say("    settled state: {0}".format(state.state))
    if state.last_error:
        say("    reported error: {0}".format(state.last_error.splitlines()[0]))

    ok = check("no longer claims PLAYING", state.state != ctrl.PLAYING)
    ok &= check("settled at NO_DISC (or NO_DRIVE)",
                state.state in (ctrl.NO_DISC, ctrl.NO_DRIVE),
                "got {0}".format(state.state))
    ok &= check("error information preserved", bool(state.last_error))

    time.sleep(0.5)
    stray = stray_processes()
    if stray is not None:
        ok &= check("no stray processes", not stray, "; ".join(stray))
    return ok


SCENARIOS = [
    ("pause", scenario_pause, False),
    ("next", scenario_next, False),
    ("previous", scenario_previous, False),
    ("natural", scenario_natural, False),
    ("stop", scenario_stop, False),
    # Interactive AND unsupported: excluded from --all, runnable only by name.
    ("removal", scenario_removal, True),
]

UNSUPPORTED = ("removal",)


def main():
    parser = argparse.ArgumentParser(
        description="Physical controller acceptance tests")
    parser.add_argument("scenarios", nargs="*",
                        help="scenario names (default: all non-interactive)")
    parser.add_argument("--list", action="store_true",
                        help="list scenarios and exit")
    parser.add_argument("--all", action="store_true",
                        help="include interactive scenarios")
    args = parser.parse_args()

    if args.list:
        for name, function, interactive in SCENARIOS:
            tags = ""
            if name in UNSUPPORTED:
                tags = "   [UNSUPPORTED diagnostic, never run by default]"
            elif interactive:
                tags = "   [interactive]"
            say("{0:10s} {1}{2}".format(
                name, (function.__doc__ or "").strip().splitlines()[0], tags))
        return 0

    if args.scenarios:
        chosen = [s for s in SCENARIOS if s[0] in args.scenarios]
        unknown = set(args.scenarios) - set(s[0] for s in SCENARIOS)
        if unknown:
            say("Unknown scenario(s): " + ", ".join(sorted(unknown)))
            return 2
    else:
        # --all still excludes unsupported diagnostics; name them explicitly.
        chosen = [s for s in SCENARIOS
                  if s[0] not in UNSUPPORTED and (args.all or not s[2])]

    say("Nexus CD Player -- controller acceptance tests")
    say("Python {0}.{1}.{2}".format(*sys.version_info[:3]))
    say("")

    controller = ctrl.CDController()
    results = []
    try:
        say("Scanning disc...")
        state = controller.scan_disc()
        if state.state != ctrl.READY:
            say("Cannot run: scan produced {0} -- {1}".format(
                state.state, state.last_error or ""))
            return 1
        say("Disc: {0} tracks, total {1}".format(
            state.track_count,
            disc_mod.format_ms(state.disc_duration_seconds)))
        if state.track_count < 4:
            say("WARNING: the natural-transition scenario expects a disc "
                "with at least 4 tracks")

        for name, function, _interactive in chosen:
            say("")
            say("=" * 60)
            say("{0}  --  {1}".format(
                name.upper(), (function.__doc__ or "").strip().splitlines()[0]))
            say("=" * 60)
            try:
                passed = function(controller)
            except KeyboardInterrupt:
                say("  interrupted")
                passed = False
            except Exception as exc:            # a scenario must not abort the run
                say("  EXCEPTION: {0}".format(exc))
                passed = False
            results.append((name, passed))
            controller.stop()
    finally:
        controller.close()

    say("")
    say("=" * 60)
    for name, passed in results:
        say("{0:10s} {1}".format(name, "PASS" if passed else "FAIL"))
    failed = [n for n, p in results if not p]
    say("")
    if failed:
        say("FAILED: " + ", ".join(failed))
        return 1
    say("All scenarios passed. Continuity is still an ear judgement.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
