#!/usr/bin/env python3
"""Phase-1 hardware check: drive discovery + TOC parsing. No audio.

Run on the Nexus with the test CD inserted:

    python3 tools/check_hardware.py

Exits 0 only if an audio disc was found and fully parsed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import device  # noqa: E402
from backend import disc as disc_mod  # noqa: E402


def main():
    print("== 1. drive discovery ==")
    found = device.discover()
    print("status : " + found.status)
    if found.message:
        print("note   : " + found.message)
    if found.drive is None:
        print("\nFAILED: no optical drive.")
        if found.raw:
            print("--- raw scanbus output ---")
            print(found.raw.strip())
        if found.status == device.NO_DRIVE:
            print("")
            print("scanbus ran but listed no CD/DVD device. Check power and")
            print("the SATA-to-USB/OTG chain. To force a known address for")
            print("troubleshooting only:")
            print("  NEXUS_CD_ALLOW_FALLBACK=1 NEXUS_CD_DEVICE=2,0,0 \\")
            print("      python3 tools/check_hardware.py")
        return 1
    print("drive  : " + found.drive.describe())
    address = found.drive.address

    print("\n== 2. table of contents ==")
    result = disc_mod.read_toc(address)
    print("status : " + result.status)
    if result.message:
        print("note   : " + result.message)

    if result.status != disc_mod.AUDIO_DISC:
        print("\nFAILED: no audio disc to read.")
        if result.raw:
            print("--- raw icedax output ---")
            print(result.raw.strip())
        return 1

    found_disc = result.disc
    print("tracks : {0} (numbered {1}-{2})".format(
        found_disc.track_count, found_disc.first_track, found_disc.last_track))
    print("total  : " + disc_mod.format_msf(found_disc.total_duration_seconds))
    print("leadout: {0}".format(found_disc.leadout_sector))
    print("")
    print(" trk    start      end   duration")
    for track in found_disc.tracks:
        print("{0:4d} {1:8d} {2:8d}   {3:>8s}{4}".format(
            track.number, track.start_sector, track.end_sector,
            disc_mod.format_msf(track.duration_seconds),
            "" if track.is_audio else "  (data)"))

    print("\nOK: drive discovered and TOC parsed.")
    print("Now capture the fixture:  ./tools/capture_fixtures.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
