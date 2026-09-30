# Deploying to the Nexus 7

Source lives on macOS; the only runtime that counts is the device:

```
Ubuntu Touch 16.04.7 Xenial   Python 3.5.2   ARMv7   kernel 3.4.0-4-flo
```

**A green test run on the Mac proves nothing about target compatibility.**
Local Python is 3.13 and will happily accept syntax 3.5.2 rejects. The AST
guard in `tests/test_py35_compat.py` catches the constructs we know about, but
the authoritative check is running the suite under the device's own
interpreter — step 3 below.

## 0. One-time setup

Enable developer mode on the device (Settings → About → Developer Mode), then
either SSH or adb. Set these once per shell on the Mac:

```bash
export NEXUS_HOST=phablet@192.168.1.50
export NEXUS_DIR=/home/phablet/nexus-cd-player
```

Find the device IP under Settings → Wi-Fi → (network) → IPv4. The Ubuntu Touch
root filesystem is read-only; `/home/phablet` is writable, which is why the
project goes there.

If you use key auth, copy your key over once:

```bash
ssh-copy-id "$NEXUS_HOST"
```

## 1. Push the project

rsync is preferable — it only sends changed files and can prune junk:

```bash
rsync -av --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' --exclude '.git' ./ "$NEXUS_HOST:$NEXUS_DIR/"
```

If rsync is not present on the device image, scp works:

```bash
scp -r backend tests tools cli.py README.md "$NEXUS_HOST:$NEXUS_DIR/"
```

Over USB with adb instead of the network:

```bash
adb push . /home/phablet/nexus-cd-player
```

Note `--delete`: it removes files on the device that no longer exist locally,
which is what keeps stale `.pyc` and deleted modules from being imported. It
also means anything created **only** on the device inside the project tree —
captured fixtures, logs — is destroyed. Pull those back (step 6) before
re-syncing, or drop the flag.

## 2. Confirm the interpreter

```bash
ssh "$NEXUS_HOST" 'python3 --version && python3 -c "import sys; print(sys.executable)"'
```

Expect `Python 3.5.2`.

## 3. Run the tests on the device

This is the real compatibility gate:

```bash
ssh "$NEXUS_HOST" 'cd /home/phablet/nexus-cd-player && python3 -m unittest discover -s tests -v'
```

A `SyntaxError` here means post-3.5 syntax slipped past the local guard — fix
it and add the pattern to `BANNED_TEXT` in `tests/test_py35_compat.py` so it
cannot recur.

## 4. Hardware smoke test

With the audio CD in the drive:

```bash
ssh "$NEXUS_HOST" 'cd /home/phablet/nexus-cd-player && python3 tools/check_hardware.py'
```

Prints drive discovery, then the parsed track table. No audio, no sudo.

## 5. Capture TOC fixtures

```bash
ssh "$NEXUS_HOST" 'cd /home/phablet/nexus-cd-player && ./tools/capture_fixtures.sh'
```

Writes `tests/fixtures/icedax_toc_real.txt` on the device.

## 6. Pull fixtures and logs back

Do this **before** the next `rsync --delete`:

```bash
rsync -av "$NEXUS_HOST:$NEXUS_DIR/tests/fixtures/" ./tests/fixtures/
```

```bash
rsync -av "$NEXUS_HOST:$NEXUS_DIR/logs/" ./logs/
```

Commit captured fixtures **unedited**, warnings included — the warnings are
part of the format the parser has to survive.

## 7. Checking for leftover processes

After a stop or a crash, nothing should remain:

```bash
ssh "$NEXUS_HOST" 'pgrep -a icedax; pgrep -a aplay; echo "exit: $?"'
```

No output means clean. This is acceptance test 10.

## Audio troubleshooting

List ALSA devices if playback is silent:

```bash
ssh "$NEXUS_HOST" 'aplay -l && amixer scontrols'
```

Playback runs as the `phablet` user with no sudo. If the backend reports a
permission failure it has already confirmed a `/dev/sg*` node exists but is
not readable/writable — check group ownership of that node and fix it with a
udev rule at provisioning time, never with sudo inside the application.

If instead it reports **NO_DRIVE** with "no `/dev/sg*` nodes exist", the drive
is not attached at all: check power and the SATA-to-USB/OTG chain. That is a
different fault from a permission problem and the backend distinguishes them
by looking at the filesystem rather than trusting icedax's wording.

## Changing discs

Supported workflow — **do not hot-eject during playback**:

```
stop  →  press the drive's physical eject button  →  swap disc  →  scan  →  play
```

Hot-ejecting mid-playback made the USB/SCSI stack drop the drive entirely;
`/dev/sg*` vanished and only a physical reconnect restored it, at a new SCSI
address.

## UI service acceptance (Phase A — HTTP, no QML)

Prove the HTTP chain over SSH before touching QML. See
[UI_SERVICE.md](UI_SERVICE.md) for the API.

Sync, then start the service (it runs as `phablet`, **outside** Click
confinement, because it needs the optical drive):

```bash
rsync -av --delete --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' ./ "$NEXUS_HOST:$NEXUS_DIR/"
```

```bash
ssh "$NEXUS_HOST" 'cd /home/phablet/nexus-cd-player && python3 -m ui_service.main'
```

Expect:

```
Nexus CD Player UI service
Listening on http://127.0.0.1:8765
```

From a **second** SSH shell:

```bash
curl -s http://127.0.0.1:8765/state
```

```bash
curl -s -X POST http://127.0.0.1:8765/scan
```

Poll until `"state":"READY"` and `"total_tracks":11`:

```bash
curl -s http://127.0.0.1:8765/state
```

Now the load-latency proof — the whole reason the adapter exists:

```bash
time curl -s -X POST -H 'Content-Type: application/json' -d '{"track":1}' http://127.0.0.1:8765/play
```

Check all four:

1. `curl` returns in well under a second with `202` and `{"accepted":true}`.
2. During the 5–8 s spin-up, `/state` reports `"state":"LOADING"` and
   `"busy":true` — and answers instantly every time.
3. Music starts.
4. `/state` then reports `"state":"PLAYING"` with a rising `track_elapsed`.

While it is still LOADING, confirm the busy rejection:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/next
```

That must print `409`.

Then exercise the rest:

```bash
curl -s -X POST http://127.0.0.1:8765/pause
```

```bash
curl -s -X POST http://127.0.0.1:8765/resume
```

```bash
curl -s -X POST http://127.0.0.1:8765/next
```

```bash
curl -s -X POST http://127.0.0.1:8765/previous
```

```bash
curl -s -X POST http://127.0.0.1:8765/stop
```

Ctrl+C the service, then confirm nothing survived:

```bash
ssh "$NEXUS_HOST" 'pgrep -a icedax; pgrep -a aplay; echo done'
```

## UI service acceptance (Phase B — QML on the device)

Only after Phase A passes. The Python service must already be running.

**Launch the frontend from the Ubuntu Touch graphical session, not from the
SSH shell.** `qmlscene` over SSH fails because that shell is not an accepted
Mir graphical session — install the Click app and tap its icon, or launch it
from the device itself.

Build the Click package on the device. `click/` is a self-contained package
root, so this needs no staging step:

```bash
ssh "$NEXUS_HOST" 'cd /home/phablet/nexus-cd-player && click build click'
```

That writes `nexus-cd-player.ayanz_0.1.0_all.click` into the project
directory. The framework is `ubuntu-sdk-16.04`, confirmed present on this
device. Installation is a separate, deliberate step and is not automated here.

The physical proof, all by touch:

| Tap | Expected |
| --- | --- |
| — | dark landscape layout renders; wordmark top-left, connection dot top-right |
| Scan | disc becomes ready, `Track 01 of 11` appears on the selector |
| Play | **Loading…** with a spinner immediately, UI stays responsive; then audio starts, headline shows `TRACK 01`, progress bar advances |
| Pause | audio stops, sub-line reads Paused, centre button shows Play |
| Resume | audio continues from the same position |
| Next | `Loading Track 02…`, then track 2 starts |
| Previous | conventional behaviour (>3 s restarts the track, otherwise steps back) |
| Track selector | opens a list of all 11 tracks with the current one marked |
| Select Track 04 while on Track 02 | popup closes, `Loading Track 04…`, then playback jumps **directly** to track 4 — track 3 must never be heard |
| Select Track 09 while on Track 04 | same, another non-adjacent jump |
| Select the current track | popup closes, nothing restarts |
| Stop | audio ends, transport disables appropriately |

While a jump is loading, confirm the transport buttons and the selector are
visibly disabled and that tapping them queues nothing — the service answers
409 and the UI simply ignores it.

### Both landscape orientations

Only the device can prove this — static tests cannot exercise an
accelerometer. Orientation comes from **QtSensors**, not `Screen.orientation`,
which was physically disproven on this hardware.

The title bar carries a temporary `Sensor: …` readout to make this checkable.

1. Open the app in one landscape orientation. Confirm the readout shows
   **LeftUp** or **RightUp** (not `Undefined`). If it stays `Undefined`, the
   sensor is not reporting and nothing below will work.
2. Confirm the UI is upright.
3. Rotate the Nexus 180° (same long edge, other way up).
4. The readout must change to the opposite value, and the UI must flip upright
   within about a second — a short crossfade, **not** a spin through 90°.
5. Rotate back; it must return.
6. Rotate **slowly** through portrait. The readout may show `TopUp`/`TopDown`,
   but the UI must keep its previous landscape rotation until the opposite
   landscape is reached. It must never reflow into a portrait layout.
7. Lay the device flat (`FaceUp`) — the rotation must not change.
8. In each orientation, tap every control and confirm each responds where it
   is *drawn*: Previous, Play/Pause, Next, Stop, Scan.
9. Open the track selector in **both** orientations. The popup must be upright
   in each — it renders in the window overlay rather than inside the rotated
   content, so this is the most likely thing to be wrong.
10. Select a non-adjacent track from the flipped popup and confirm the jump
    still works.

**If the UI rotates the wrong way**, do not redesign anything. Swap the two
numbers in `click/qml/Main.qml`:

```
readonly property int rotationWhenLeftUp: 180
readonly property int rotationWhenRightUp: 0
```

Rebuild, reinstall, retest.

Once orientation is confirmed, remove the temporary readout: delete the `Label`
between the `TEMPORARY` comments and the `sensorOrientation` property.

Then, back over SSH:

```bash
ssh "$NEXUS_HOST" 'pgrep -a icedax; pgrep -a aplay; echo done'
```

Also confirm the disconnect path: stop the Python service while the UI is open.
The UI must show **backend: disconnected**, disable its controls, and
reconnect on its own once the service is restarted — no crash, no dialog spam.
