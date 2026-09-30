# UI service — loopback HTTP adapter

Proof-of-concept touchscreen layer. Deliberately narrow: the goal is to prove
`touch → HTTP → CDController → physical CD → UI reflects it`, not to look good.

```
QML (qmlscene, inside Click confinement)
        │  HTTP + JSON, 127.0.0.1:8765
        ▼
ui_service.server        HTTP threads — never touch CDController
        │  command queue  ↑ cached snapshot
        ▼                 │
ui_service.worker        THE ONLY thread that owns CDController
        ▼
CDController → PlaybackEngine → icedax → aplay
```

Nothing in `backend/` was modified for this phase.

## Why the adapter exists

`controller.play()` blocks for 5–8 s while the drive spins up and the engine
waits for real PCM. That was fine for a CLI. A touchscreen cannot freeze for
six seconds, and the fix must not touch the frozen `PlaybackEngine`.

## Thread ownership

| Thread | May call |
| --- | --- |
| `controller-worker` (exactly one) | every `CDController` method, including `close()` |
| HTTP handler threads | `worker.submit()`, `worker.snapshot()` — nothing else |
| main thread | starts/stops the worker and server |

`CDController` is never called concurrently: all commands are serialised
through one `queue.Queue` consumed by the worker.

`controller.close()` runs **on the worker thread** in its `finally` block, not
from `stop()`. Closing from the stopping thread would break single ownership
at exactly the moment things are going wrong. `stop()` signals, then joins with
a bounded timeout and logs if the worker overruns.

## How LOADING stays visible while `play()` blocks

Two mechanisms, because one alone has a race:

1. **`submit()` publishes immediately**, on the HTTP thread, before the worker
   even dequeues: `busy=True, state="LOADING"`. A `GET /state` racing the
   command still sees the transition.
2. **`_publish()` derives LOADING from the in-flight flag**, not as a one-shot.
   This is the part that matters. The controller honestly reports `READY`
   throughout a blocking `play()` — it has not started anything yet — so a
   background tick landing mid-command would otherwise flip the UI back to
   READY while the drive was audibly spinning up. Both were caught by tests
   before ever reaching hardware.

While the worker is stuck inside `play()`, it publishes nothing at all; HTTP
threads keep serving the last snapshot instantly, because they only ever read
`UiState`.

## State synchronisation

`ui_service/state.py` holds one dict behind one `threading.Lock`. It never
hands out a reference:

- `snapshot()` returns a `deepcopy` — callers may mutate it freely.
- `update(**fields)` / `replace(snapshot)` merge atomically and reject unknown
  keys loudly, so a typo cannot silently invent a field.
- `revision` increments on every publish.

`snapshot_from_controller()` is a pure mapper from `ControllerState`. **No CD
timing is computed in the adapter** — every value comes from the controller.

An exception escaping the controller is an *adapter-level* failure and is held
in a sticky `_adapter_error` that overrides the published state until the next
command succeeds. Without it the next 150 ms tick would overwrite the error
with the controller's own (still cheerful) view and the user would never see
that their command failed.

## HTTP API

Binds **127.0.0.1 only** — `create_server()` raises `ValueError` for any other
host. No authentication, because nothing off-device can reach it. All responses
are `application/json` with a correct `Content-Length`; `/state` is
`Cache-Control: no-store`.

| Method | Path | Success | Body |
| --- | --- | --- | --- |
| GET | `/state` | 200 | `{"ok":true,"state":{…}}` |
| GET | `/health` | 200 | `{"ok":true,"service":"nexus-cd-player-ui"}` |
| POST | `/scan` | 202 | — |
| POST | `/play` | 202 | optional `{"track": 4}` |
| POST | `/pause` | 202 | — |
| POST | `/resume` | 202 | — |
| POST | `/stop` | 202 | — |
| POST | `/next` | 202 | — |
| POST | `/previous` | 202 | — |

Accepted command → `202 {"ok":true,"accepted":true,"command":"next"}`

| Failure | Status | Example |
| --- | --- | --- |
| bad JSON / bad track / oversized body | 400 | `{"ok":false,"error":"track must be an integer"}` |
| a command already in flight | 409 | `{"ok":false,"error":"controller is busy"}` |
| unknown route | 404 | `{"ok":false,"error":"unknown endpoint: /eject"}` |
| unexpected internal failure | 500 | `{"ok":false,"error":"internal service error"}` |

Tracebacks are never exposed; they go to `logs/nexus-cd-player.log`.

### `/state` snapshot

```json
{
    "state": "PLAYING",
    "busy": false,
    "current_track": 3,
    "total_tracks": 11,
    "track_elapsed": 7.0,
    "track_duration": 12.293,
    "disc_elapsed": 539.7,
    "disc_duration": 2379.2,
    "has_disc": true,
    "drive_address": "2,0,0",
    "error": null,
    "service_ready": true,
    "revision": 412
}
```

`state` is one of `NO_DRIVE, NO_DISC, READY, LOADING, PLAYING, PAUSED,
STOPPED, ERROR` — the controller's own vocabulary. `busy` is adapter metadata.

## Command rejection policy

While a command is in flight, **every** further command is rejected with 409
rather than queued. Navigation taps piling up behind a 6 s spin-up would be
worse than useless — the disc would lurch through several tracks afterwards.
QML disables the buttons while busy; the 409 is the enforcement.

Pause/resume/stop do not interrupt a blocking spin-up in this phase.

## Running it

The service runs **outside Click confinement**, as the normal `phablet` user,
because it needs the optical drive.

```bash
cd /home/phablet/nexus-cd-player && python3 -m ui_service.main
```

```
Nexus CD Player UI service
Listening on http://127.0.0.1:8765
```

Options: `--port`, `--host` (loopback only), `--no-scan`, `--verbose`. It scans
the disc once at startup so the UI has something real to show, and never starts
playback on its own. Ctrl+C shuts down cleanly.

## Python 3.5.2 notes

Two traps were checked explicitly on the target version:

- `json.loads()` does **not** accept `bytes` until 3.6, so request bodies are
  decoded to `str` first.
- `http.server.ThreadingHTTPServer` does not exist until 3.7, so the server is
  `socketserver.ThreadingMixIn` + `HTTPServer`.

Otherwise: stdlib only (`threading`, `queue`, `json`, `copy`, `http.server`,
`socketserver`), no f-strings, no dataclasses, no annotations. The existing AST
compatibility guard now scans `ui_service/` too.

## Click package structure

`click/` is the package **root**: everything that ships lives inside it, so the
build needs no staging step.

```
click/
    manifest.json              framework ubuntu-sdk-16.04, architecture all
    apparmor.json              policy_groups: ["networking"] and nothing else
    nexus-cd-player.desktop    Exec=qmlscene qml/Main.qml, landscape
    qml/
        Main.qml               layout, state, HTTP, track selector
        TransportButton.qml    Canvas-drawn transport control
    assets/
        icon.svg               placeholder development icon
```

There is exactly one `Main.qml`, inside the package; a test fails the build if
a second copy appears anywhere in the tree. Sibling `.qml` files in the same
directory are implicitly importable, so `TransportButton` needs no import.

**The Click app is a frontend only.** AppArmor requests `networking` for
loopback HTTP and nothing else. The privilege lives with the Python service,
outside confinement, which is **not** packaged; a test asserts no `.py` file
ever lands in `click/`.

The framework is the generic `ubuntu-sdk-16.04`, confirmed present on the
device alongside the 16.04.1-16.04.8 point releases.

### Building on the device

```bash
cd /home/phablet/nexus-cd-player && click build click
```

That writes `nexus-cd-player.ayanz_0.1.0_all.click` into the project
directory. Installation is a separate, deliberate step and is not automated
here.

## Frontend design notes

The UI is a dedicated appliance screen, not a debugging utility: near-black
chassis, warm off-white type, muted grey secondary text, and a single amber
accent (`#D9A441`) reserved for the active element.

**Visual hierarchy**, largest to smallest: the track readout dominates the
centre, then elapsed/duration, then the progress bar, then the transport row,
then the selector and stop, with the wordmark and connection dot reduced to
quiet chrome at the top.

**Direct track selection.** The selector button opens a modal `Popup`
containing a `ListView` of `total_tracks` rows, 64 px each, with the current
track marked by an amber bar and label. Selecting a different track calls the
existing `POST /play` with `{"track": N}` — one request, so track 2 to track 9
never plays anything in between. Selecting the *current* track closes the popup
and does nothing, since restarting would cost another spin-up for no gain.

**Per-track durations are deliberately absent.** `/state` exposes
`track_duration` for the current track only, and the backend is frozen, so
there is nothing to render for the other rows. Inventing them was not an
option; the row layout leaves the slot free for when the API can supply them.

**`requestedTrack`** is presentation metadata only. Because the controller
keeps reporting the previous track while a direct jump blocks on drive
spin-up, the UI remembers what was asked for and shows `Loading Track 04…`
instead of a stale number. It is cleared as soon as the service reports real
playback, and it never influences an API call. `Next` sets it too (the target
is knowable); `Previous` deliberately does not, because the controller's ~3 s
threshold makes the destination ambiguous and a wrong number would be worse
than none.

**Enable rules**, one per control:

| Control | Enabled when |
| --- | --- |
| Previous / Play-Pause / Next | connected, not loading, disc present |
| Stop | the above, and playing or paused |
| Track selector | the above, and `total_tracks > 0` |
| Scan | connected and not loading |

Scan is prominent only in `NO_DISC` / `NO_DRIVE` / `ERROR`; it shrinks to a
secondary "Rescan" in `READY` / `STOPPED` and disappears during playback.

**Progress** is display-only — no `MouseArea`, no `Slider`, so there is no
scrubbing. A `Behavior on width` smooths the 250 ms polling into continuous
movement. Nothing is drawn while loading: there is no honest progress during
spin-up, so none is invented.

**Icons** are drawn with `Canvas` rather than glyphs or images, because the
device font is not guaranteed to carry media symbols and the package must stay
free of raster assets.

### Both landscape orientations

The tablet sits in a 3D-printed enclosure that may face either way depending
on which side gives access to ports and cables, so the UI must read upright
with **either long edge on top**.

This is one layout rotated 180°, never a second design:

```
ApplicationWindow
    OrientationSensor (QtSensors)
    Binding -> Overlay.overlay.rotation = contentRotation
    Item (id: contentRoot, rotation: contentRotation, origin: centre)
        the entire player UI
```

**`Screen.orientation` was tried first and does not work on this device.**
Physical testing showed Ubuntu Touch / Mir never delivers the change, even with
`orientationUpdateMask` set. That path is fully removed — along with the
`QtQuick.Window` import that existed only to provide it — so there is exactly
one orientation mechanism, not two competing ones. A test enforces that.

`QtSensors` is the replacement, confirmed present on the device as
`qml-module-qtsensors 5.12.9-1~ubports3`. An `OrientationSensor { active: true }`
notifies on `readingChanged`; nothing polls it, and a test asserts the only
`Timer` in the file is the state poller.

| `reading.orientation` | `contentRotation` |
| --- | --- |
| `OrientationReading.LeftUp` | `rotationWhenLeftUp` (0) |
| `OrientationReading.RightUp` | `rotationWhenRightUp` (180) |
| TopUp / TopDown / FaceUp / FaceDown / Undefined | **unchanged** |

The sensor reports which physical edge points up, independently of the UI's own
rotation. Everything that is not a landscape reading deliberately leaves the
rotation alone, so the last valid landscape orientation persists while the
device is being turned through portrait or is lying flat.

**If the device flips the wrong way**, swap the two numbers in
`rotationWhenLeftUp` / `rotationWhenRightUp`. That is the entire fix — the
mapping is deliberately centralised so no logic has to be re-reasoned.

At startup `reading` may be null; `applyOrientation()` returns early rather
than throwing, and the default rotation stands until a real reading arrives.

Two details that are easy to get wrong:

- **The overlay needs the same rotation.** Qt Quick Controls renders `Popup` in
  the window's `Overlay`, which is *not* a child of `contentRoot`, so without
  the explicit `Binding` the track selector would appear upside down after a
  flip.
- **The transition is a crossfade, not a spin.** Animating `rotation` from 0 to
  180 sweeps through 90°, which momentarily looks like a portrait layout. A
  `Behavior` with `PropertyAction` between two short opacity animations flips
  instantly behind a ~210 ms fade instead.

Rotating an `Item` also rotates hit-testing, so touches keep landing on the
controls where they are drawn; no coordinate mapping is done by hand.

No AppArmor change was needed — the sensor is read through QML, and the
profile still requests only `networking`.

### Temporary sensor readout

A small dim `Sensor: LeftUp` label sits in the title bar for this development
build, to make physical validation possible. It is bracketed by `TEMPORARY`
comments; delete the label and the `sensorOrientation` property to remove it.
