# Nexus CD Player — Backend V1

A standalone touchscreen audio-CD player for a Nexus 7 (2013 LTE) running
Ubuntu Touch 16.04.7. This repository is the **backend and CLI only** — no GUI.

Python controls; `icedax` reads the disc; `aplay` makes sound. See
[ARCHITECTURE.md](ARCHITECTURE.md).

## Requirements

On the device: `icedax`, `aplay`, Python 3.5.2. No pip packages, no network,
no sudo, no temporary audio files.

## Layout

```
backend/config.py      tunables (device fallback, timeouts, latencies)
backend/errors.py      typed exceptions
backend/log.py         file logging + child stderr sinks
backend/device.py      optical drive discovery via icedax -scanbus
backend/disc.py        TOC read + parse, sector/time arithmetic
backend/playback.py    icedax -> aplay pipeline and process lifecycle
backend/controller.py  state machine, track logic, progress
cli.py                 interactive backend test surface
ui_service/            loopback HTTP+JSON adapter (worker thread + server)
click/                 Click package root (frontend only)
  click/qml/Main.qml     touchscreen proof-of-concept frontend
  click/assets/icon.svg  placeholder icon
tools/check_hardware.py   phase-1 hardware check (discovery + TOC, no audio)
tools/play_test.py        playback acceptance test (real audio)
tools/controller_test.py  controller acceptance tests (pause/next/prev/natural)
tools/capture_fixtures.sh capture real icedax output as test fixtures
tests/                 unit tests, no hardware required
```



## Running the tests

Anywhere, no hardware and no CD needed:

```bash
python3 -m unittest discover -s tests -v
```

Tests that need the real icedax TOC fixture skip until it is captured.

## On the device

Check that the drive is found and the TOC parses:

```bash
python3 tools/check_hardware.py
```

Then capture real tool output as fixtures (commit the result **unedited**):

```bash
./tools/capture_fixtures.sh
```

Play real audio and check the shutdown is clean:

```bash
python3 tools/play_test.py --track 1 --seconds 60
```

Then the controller scenarios (pause/resume, next, previous, natural track
transition, stop):

```bash
python3 tools/controller_test.py
```

Or drive it by hand:

```bash
python3 cli.py
```

If the TOC parser fails (its format is still unverified), playback can still
be tested by supplying the range endpoint yourself:

```bash
python3 tools/play_test.py --track 1 --seconds 60 --last-track 11
```

## Configuration

All via environment variables, all optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXUS_CD_DEVICE` | `2,0,0` | Troubleshooting fallback Bus,ID,LUN |
| `NEXUS_CD_ALLOW_FALLBACK` | `0` | Set `1` to use that fallback when discovery finds no optical drive. Off by default: a scan that finds nothing means `NO_DRIVE`, not a guessed address. |
| `NEXUS_CD_ICEDAX` | `icedax` | Path to icedax |
| `NEXUS_CD_APLAY` | `aplay` | Path to aplay |
| `NEXUS_CD_LOG_DIR` | `./logs` | Log destination |

## Touchscreen proof-of-concept

A QML frontend talks to a Python UI service over loopback HTTP. The service
runs outside Click confinement as `phablet`; the Click app is frontend only.

```bash
python3 -m ui_service.main
```

See [UI_SERVICE.md](UI_SERVICE.md) for the API and thread model, and
[DEPLOY.md](DEPLOY.md) for the acceptance procedure.

## Status

**Backend V1 is complete and verified on the physical Nexus.**

Playback: drive discovered, real 11-track TOC parsed, PCM after 5.84 s, 60 s
continuous audio, clean shutdown, no surviving processes, Python 3.5.2.

Controller: natural Track 3 → 4 transition with icedax and aplay PIDs
unchanged and no LOADING state; pause/resume; next; previous threshold; stop.

`PlaybackEngine` and `CDController` are **frozen** unless a concrete
requirement exposes a bug.

The UI service and QML frontend are written and unit-tested but **have not run
on the device**. QML in particular is statically checked only — there is no Qt
on the development machine, so it has never been executed anywhere.

## Changing discs

Supported workflow — **do not hot-eject during playback**:

```
stop  →  press the drive's physical eject button  →  swap disc  →  scan  →  play
```

Hot-ejecting mid-playback made the Nexus USB/SCSI stack drop the drive
entirely; `/dev/sg*` vanished and only a physical reconnect restored it, at a
new SCSI address. There is deliberately no media-presence polling and no
background SCSI probing.

## Known limitations

- Data-only discs are not distinguished: real `-g` TOC output carries no
  audio/data annotation, so every track is assumed to be audio.
- `play()` and manual track changes block ~5–8 s while the drive spins up.
  Acceptable for the CLI, not for a touchscreen — the next phase adds an
  asynchronous command adapter around the synchronous controller.
