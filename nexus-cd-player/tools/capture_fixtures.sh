#!/bin/sh
# Capture REAL icedax output from the Nexus 7 as test fixtures.
#
# Run this ON THE DEVICE, with the audio test CD in the drive:
#
#     ./tools/capture_fixtures.sh
#
# It never edits or prettifies anything: the parser has to cope with whatever
# icedax actually prints, warnings included.

set -e

HERE=$(dirname "$0")
FIXTURES="$HERE/../tests/fixtures"
ICEDAX="${NEXUS_CD_ICEDAX:-icedax}"

mkdir -p "$FIXTURES"

echo "== icedax -scanbus =="
"$ICEDAX" -scanbus > "$FIXTURES/icedax_scanbus_real.txt" 2>&1 || true
cat "$FIXTURES/icedax_scanbus_real.txt"

# Prefer the address discovered above; fall back to the configured default.
ADDR="${NEXUS_CD_DEVICE:-}"
if [ -z "$ADDR" ]; then
    ADDR=$(sed -n "s/^[[:space:]]*\([0-9]*,[0-9]*,[0-9]*\).*CD-ROM.*/\1/p" \
           "$FIXTURES/icedax_scanbus_real.txt" | head -n 1)
fi
if [ -z "$ADDR" ]; then
    ADDR="2,0,0"
    echo "!! no CD-ROM line found in scanbus output; falling back to $ADDR"
fi
echo "using device address: $ADDR"

echo
echo "== icedax TOC (disc in drive) =="
"$ICEDAX" -D "$ADDR" -I generic_scsi -g -J -H -v toc,sectors \
    < /dev/null > "$FIXTURES/icedax_toc_real.txt" 2>&1 || true
cat "$FIXTURES/icedax_toc_real.txt"

echo
echo "Wrote:"
echo "  $FIXTURES/icedax_scanbus_real.txt"
echo "  $FIXTURES/icedax_toc_real.txt"
echo
echo "To also capture the empty-tray case, eject the disc and run:"
echo "  $ICEDAX -D $ADDR -I generic_scsi -g -J -H -v toc,sectors </dev/null \\"
echo "      > $FIXTURES/icedax_toc_no_disc_real.txt 2>&1"
