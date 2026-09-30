#!/usr/bin/env python3
"""Interactive CLI for the Nexus CD player backend.

    python3 cli.py

A backend test surface, not the final UI. Commands:

    disc        show the table of contents
    status      current state, track and position
    play        play from the first track (or resume if paused)
    play <n>    play from track n
    pause       pause playback
    resume      resume playback
    stop        stop playback
    next        next track
    prev        previous track (or restart the current one)
    scan        re-read the drive and disc
    help        this list
    quit        stop playback and exit

Manual track changes restart icedax and block for several seconds while the
drive spins up -- that is expected. Natural track transitions do not.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import controller as ctrl  # noqa: E402
from backend import disc as disc_mod  # noqa: E402

PROMPT = "> "


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def show_disc(controller):
    state = controller.update()
    if state.disc is None:
        say("No disc. State: " + state.state)
        if state.last_error:
            say("  " + state.last_error)
        return
    disc = state.disc
    say("Disc: {0} tracks, total {1}".format(
        disc.track_count, disc_mod.format_ms(disc.total_duration_seconds)))
    say("")
    say(" trk   duration")
    for track in disc.tracks:
        marker = " *" if track.number == state.current_track else "  "
        say("{0}{1:3d}   {2:>7s}{3}".format(
            marker, track.number,
            disc_mod.format_ms(track.duration_seconds),
            "" if track.is_audio else "  (data)"))


def show_status(controller):
    state = controller.update()
    say("State: " + state.state)
    if state.disc is not None and state.current_track is not None:
        say("Track: {0} / {1}".format(state.current_track, state.track_count))
        say("Time: {0} / {1}".format(
            disc_mod.format_ms(state.track_elapsed_seconds),
            disc_mod.format_ms(state.track_duration_seconds)))
        say("Disc: {0} / {1}".format(
            disc_mod.format_ms(state.disc_elapsed_seconds),
            disc_mod.format_ms(state.disc_duration_seconds)))
    if state.last_error:
        say("Error: " + state.last_error)


def report(controller, action):
    """Print the outcome of a command that may have been rejected."""
    state = controller.update()
    if state.state == ctrl.PLAYING:
        say("Playing Track {0}".format(state.current_track))
    elif state.state == ctrl.PAUSED:
        say("Paused at Track {0} {1}".format(
            state.current_track,
            disc_mod.format_ms(state.track_elapsed_seconds)))
    elif state.state == ctrl.STOPPED:
        say("Stopped")
    elif state.state == ctrl.READY:
        say("Ready")
    else:
        say("{0} failed: {1}".format(action, state.last_error or state.state))


def parse_track(argument):
    try:
        return int(argument)
    except ValueError:
        return None


def do_play(controller, argument):
    if argument:
        number = parse_track(argument)
        if number is None:
            say("Not a track number: " + argument)
            return
        # Only promise to load a track that exists, so an invalid request
        # does not print a misleading "Loading..." first.
        if controller.disc is not None and controller.disc.has_track(number):
            say("Loading Track {0}...".format(number))
        started = controller.play(number)
    elif controller.state == ctrl.PAUSED:
        started = controller.play()          # resumes
    else:
        say("Loading...")
        started = controller.play()
    if started:
        report(controller, "play")
    else:
        say(controller.last_error or "cannot play")


def do_next(controller):
    say("Loading next track...")
    if controller.next_track():
        report(controller, "next")
    else:
        say(controller.last_error or "cannot go to the next track")


def do_prev(controller):
    say("Loading...")
    if controller.previous_track():
        report(controller, "prev")
    else:
        say(controller.last_error or "cannot go to the previous track")


def main():
    say("Nexus CD Player")
    controller = ctrl.CDController()
    try:
        state = controller.scan_disc()
        if state.disc is not None:
            say("Disc: {0} tracks".format(state.track_count))
        else:
            say("State: " + state.state)
            if state.last_error:
                say("  " + state.last_error)
        say("Type 'help' for commands.")
        say("")

        while True:
            try:
                line = input(PROMPT)
            except EOFError:
                say("")
                break
            except KeyboardInterrupt:
                say("")
                continue

            line = line.strip()
            if not line:
                # A bare Enter is a cheap way to refresh a running display.
                controller.update()
                continue

            parts = line.split(None, 1)
            command = parts[0].lower()
            argument = parts[1].strip() if len(parts) > 1 else ""

            if command in ("quit", "exit", "q"):
                break
            elif command == "help":
                say(__doc__.split("Commands:")[-1].strip())
            elif command == "disc":
                show_disc(controller)
            elif command == "status":
                show_status(controller)
            elif command == "play":
                do_play(controller, argument)
            elif command == "pause":
                if controller.pause():
                    report(controller, "pause")
                else:
                    say(controller.last_error or "cannot pause")
            elif command == "resume":
                if controller.resume():
                    report(controller, "resume")
                else:
                    say(controller.last_error or "cannot resume")
            elif command == "stop":
                controller.stop()
                say("Stopped")
            elif command == "next":
                do_next(controller)
            elif command == "prev":
                do_prev(controller)
            elif command == "scan":
                say("Scanning...")
                controller.scan_disc()
                show_disc(controller)
            else:
                say("Unknown command: " + command)
    finally:
        controller.close()
        say("Bye.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
