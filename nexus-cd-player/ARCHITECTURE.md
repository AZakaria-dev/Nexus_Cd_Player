# Nexus CD Player — Backend V1 architecture

Target: Nexus 7 (2013 LTE), Ubuntu Touch 16.04.7, kernel 3.4.0-4-flo, Python
3.5.2, ARMv7. No GUI in this phase.

## 1. Shape of the system

Python is a **controller only**. It never touches PCM data.

```
                    ┌───────────────────────────────┐
   cli.py ────────► │        controller.py          │
                    │  state machine + progress     │
                    └───┬───────────┬───────────┬───┘
                        │           │           │
                   device.py    disc.py    playback.py
                        │           │           │
                 icedax        icedax        icedax ──stdout──┐
                 -scanbus      -J -v toc     (extract)        │  OS pipe
                        │           │                         ▼
                    /dev/sg*    /dev/sg*                   aplay ──► ALSA
                        └──── generic SCSI ────┘                     headphones
```

Audio goes `icedax.stdout → aplay.stdin` as a **direct OS-level pipe** created
by `Popen`. No bytes cross the Python heap, so the 3.4 kernel's scheduler and
the ARMv7 CPU never wait on the interpreter. Consequence worth stating up
front: because Python is not in the data path, it **cannot observe audio
bytes** — it can only observe that bytes *exist*, which is what §5 exploits.

### Why no block device
The kernel exposes the USB-attached PLDS drive only through the generic SCSI
layer. A `/dev/sg*` node exists, `/dev/sr0` does not, so `cdparanoia` and every
block-oriented approach are unavailable. icedax with `-I generic_scsi` and a
`Bus,ID,LUN` address is the entire device interface. A test
(`test_py35_compat.py`) fails the build if `/dev/sr0`, `/dev/cdrom`, or
`cdparanoia` ever appear in production code.

The node number is **never** assumed: after a USB reconnect this drive was
physically observed to move from `2,0,0` to `8,0,0`, so both the SCSI address
and the `/dev/sg*` node are discovered every time.

## 2. Modules

| Module | Responsibility | Status |
| --- | --- | --- |
| `backend/config.py` | All tunables in one place | done |
| `backend/errors.py` | Typed exceptions; `ParseError` carries raw output | done |
| `backend/log.py` | File logging; child-stderr sinks | done |
| `backend/device.py` | `icedax -scanbus` → `DriveInfo` + NO_DRIVE/DRIVE_PRESENT/ERROR | done |
| `backend/disc.py` | TOC read + parse → `DiscInfo` + NO_DISC/AUDIO_DISC/UNSUPPORTED_DISC/ERROR | done |
| `backend/playback.py` | icedax→aplay pipeline, process lifecycle | done, **verified on hardware** |
| `backend/controller.py` | State machine, progress, track advance | done, **untested on hardware** |
| `cli.py` | Interactive shell / backend test surface | done |

`tools/play_test.py` and `tools/controller_test.py` are the hardware
acceptance harnesses for `playback.py` and `controller.py` respectively.

## 3. Sector arithmetic

75 sectors per second; a track's end sector is the next track's start minus
one, and the last track ends at leadout − 1.

This is **verified against the real disc**, not assumed. Our computed
durations reproduce all eleven of icedax's own reported track times exactly,
and leadout 178442 / 75 renders as 39:39.17 — icedax's reported total. Note
those times are **MSF** (`M:SS.FF`, 75 frames per second), not decimal
seconds; `format_msf()` handles this and `format_ms()` is the user-facing
`M:SS` form.

## 4. Track advance without restarting icedax

icedax costs ~7–8 s of spin-up per launch, so paying it at every track
boundary would be unacceptable. Instead, sequential playback launches **one**
icedax spanning a track range — starting at track N, `-t N+<last>` — and the
controller maps elapsed time onto the known TOC boundaries:

```
DiscInfo.track_at_offset(seconds_since_pipeline_start, start_track) -> track no.
```

Crossing a boundary updates `current_track` in software only. The icedax and
aplay processes are untouched. This is implemented and unit-tested already
(including a run starting mid-disc and the 12-second track 3, which two
consecutive boundary crossings pass through quickly).

The icedax invocation is a **range**, `-t N+LAST` (e.g. `-t 1+11`), reading to
stdout via a trailing `-`. `-d` (duration) is not used for normal whole-track
or range playback — it would cut the stream short of the range.

Manual next/prev/direct selection **does** restart the pipeline in V1 and
reports LOADING while icedax spins up.

## 5. LOADING → PLAYING via `select.poll()`

No timer ever fakes PLAYING. The transition is driven by a real observation:
PCM has arrived in the pipe. Python still never touches a PCM byte.

### Sequence

```
1.  icedax = Popen(argv, stdin=DEVNULL, stdout=PIPE, stderr=logs/icedax.log,
                   close_fds=True, start_new_session=True)
        state = LOADING            # parent holds the pipe READ end

2.  poller = select.poll()
    poller.register(icedax.stdout.fileno(),
                    POLLIN | POLLHUP | POLLERR | POLLNVAL)

    until PCM_READY_TIMEOUT:
        POLLIN                     -> PCM is queued, go to 3
                                      (wins even if POLLHUP is also set)
        POLLHUP/POLLERR/POLLNVAL   -> icedax closed stdout with no audio
           without POLLIN             -> cleanup, loading error
        timeout                    -> terminate icedax, loading error

3.  aplay = Popen([...], stdin=icedax.stdout, stdout=DEVNULL,
                  stderr=logs/aplay.log, close_fds=True,
                  start_new_session=True)

4.  icedax.stdout.close()         # drop the parent's duplicate read end

5.  state = PLAYING;  playback clock starts here
```

`poll()` is non-consuming: it reports readiness and nothing else. No read, no
peek, no FIONREAD.

### Why `poll()` rather than `select()`

A pipe is "readable" at EOF as well as when data is queued, so `select()`
alone cannot tell "icedax produced audio" from "icedax died". `poll()` gives
the two apart as distinct event bits, which is why the gate uses it.

**Platform caveat, verified empirically.** On Linux — the target — `pipe_poll()`
raises `POLLIN` only when bytes are genuinely queued, so EOF-without-data
arrives as `POLLHUP` alone and aplay is never launched. On BSD/macOS an
EOF-only pipe reports `POLLIN|POLLHUP`, because `read()` would return 0
immediately. Measured on the dev Mac:

| pipe state | events |
| --- | --- |
| data only | `POLLIN` |
| EOF only | `POLLIN|POLLHUP` *(Linux: `POLLHUP` alone)* |
| data + EOF | `POLLIN|POLLHUP` |

Consequence: on macOS a dead icedax slips past the gate and the failure is
caught one step later by `poll_status()` instead — still reported, still
cleaned up, but aplay was launched. **Acceptance item 14 ("failure before PCM
does not launch aplay") is therefore only meaningful on the Nexus**, and the
corresponding unit test is skipped off-Linux rather than asserting something
the platform cannot deliver.

### Why no PCM is lost

The bytes that raised `POLLIN` stay in the kernel's pipe buffer; `poll()`
consumes nothing. aplay inherits that same read end at step 3 and reads them
as its first input, so playback starts at sample zero.

Between steps 1 and 3 icedax writes into a pipe nobody is reading. Once the
kernel's pipe buffer fills, icedax simply blocks in `write()` — correct
backpressure, and step 3 follows within milliseconds. No pipe capacity is
assumed or hardcoded anywhere.

### Step 4 is hygiene, not EOF correctness

Worth being precise, since the usual pipe idiom concerns the *write* end: the
parent here holds a **read** end, and keeping it open would **not** stop aplay
from seeing EOF — a pipe signals EOF when all *write* ends close, and the only
write end lives in icedax. Closing it matters because otherwise every `play()`
leaks a descriptor, and a stray read in the parent would steal PCM from aplay.

### Timeout

`PCM_READY_TIMEOUT` (default 30 s) generously exceeds the measured 7–8 s
spin-up. The measurement informs the ceiling; it never determines the state.
On expiry: terminate icedax, clean up, raise a loading error. aplay is never
launched and PLAYING is never entered.

## 6. Controller state machine

`backend/controller.py` owns these. They are deliberately distinct from
`playback.*`, which describe one pipeline and know nothing about discs;
overlapping names carry identical string values.

```
                       scan_disc()
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
    NO_DRIVE             NO_DISC              READY ◄──────────┐
                            ▲                   │              │
                            │              play(N) / next      │ range
                            │                   │              │ finished
                            │                   ▼              │
                            │                LOADING           │
                            │                   │              │
                            │                   ▼              │
                            │                PLAYING ──────────┘
                            │                 │   ▲
                            │            pause│   │resume
                            │                 ▼   │
                            │                PAUSED
                            │                 │
                            │            stop()│
                            │                 ▼
                            │              STOPPED ──play()──► LOADING
                            │                 
                     one rate-limited
                        rescan
                            ▲
                          ERROR ◄──── engine failure, after cleanup
```

**LOADING is transient.** `PlaybackEngine.start()` blocks until PCM arrives
(~5.8 s measured on the Nexus), so a single-threaded caller polling
`get_state()` cannot catch the controller in LOADING. Rather than modify the
frozen engine, the controller records a `transitions` log and a
`pipeline_start_count`, which makes both "LOADING was entered" and "the
pipeline was *not* restarted" directly assertable — exactly what the natural-
transition acceptance test needs.

Documented choices:

| Situation | Behaviour |
| --- | --- |
| range finishes naturally | **READY** — disc still inserted and immediately playable |
| `stop()` | **STOPPED**, DiscInfo cached, `current_track` retained for display |
| `next` on the last track | no-op, **no wrap** to track 1 |
| `play()` with no argument while PAUSED | resumes, does not restart |
| `play()` with no argument otherwise | first audio track |
| data-only disc | NO_DISC with an explanatory `last_error` |
| playback failure | ERROR, then one rescan → normally NO_DISC |
| disc change | **stop first** — see below; hot eject is unsupported |

`update()` is the only polling entry point: cheap, no thread, and it never
re-reads the TOC while playback is running (a unit test asserts this).

### Supported disc-change workflow

```
PLAYING → stop() → [icedax/aplay cleaned up]
        → user presses the drive's physical eject button
        → user swaps the disc
        → scan_disc() → READY → play()
```

Changing the disc **while playback is running is not supported**. Hot-ejecting
mid-playback made this Nexus's USB/SCSI stack drop the optical drive entirely:
`/dev/sg*` disappeared and only a physical reconnect restored it — at a
different SCSI address (2,0,0 → 8,0,0), which dynamic discovery then handled
correctly.

Consequently there is deliberately **no media-presence polling, no `sg_turs`
watchdog and no background SCSI probing**. Nothing touches the drive except
when the user asks. Generic engine-failure handling remains as a safety net,
but it is not a disc-change mechanism.

The `removal` scenario in `tools/controller_test.py` is retained as an
explicitly labelled unsupported diagnostic and never runs by default.

### Progress

Sectors are the source of truth; displayed `mm:ss` is never used for
boundaries.

```
active_elapsed  = now − playing_started_at − accumulated_pause
                  (while paused, 'now' is pinned to pause_started_at,
                   so progress freezes with no special-casing)
absolute_sector = disc.get_track(range_start_track).start_sector
                  + int(active_elapsed × 75)
current_track   = disc.track_at_offset(active_elapsed, range_start_track)
track_elapsed   = (absolute_sector − current_track.start_sector) / 75
```

Track 3 → 4 on the test disc is 922 sectors = 12.293 s. Against the fake
pipeline the observed transition landed at disc position 545.03 s versus
track 4's true start of 545.00 s.

## 7. Process lifecycle (`playback.py`)

### Exact Popen structure

```python
icedax_argv = [ICEDAX, "-D", address, "-I", "generic_scsi",
               "-q",        # quiet
               "-H",        # --no-infofile: write no .inf/CDDB side files.
                            # NOT about stream headers -- -O raw is what
                            # makes the PCM headerless.
               "-x",        # maximum quality
               "-n", "10",  # sectors per request; proven on this hardware
               "-O", "raw", "-E", "little",
               "-t", "{0}+{1}".format(start_track, last_track),
               "-"]

icedax = subprocess.Popen(
    icedax_argv,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,          # parent holds the read end
    stderr=icedax_log,               # a real file, never an idle PIPE
    close_fds=True,
    start_new_session=True,
)

# ... select.poll() gate (section 5) ...

aplay = subprocess.Popen(
    [APLAY, "-q", "-t", "raw", "-f", "cd"],
    stdin=icedax.stdout,             # inherits the pipe read end
    stdout=subprocess.DEVNULL,
    stderr=aplay_log,
    close_fds=True,
    start_new_session=True,
)
icedax.stdout.close()                # parent's duplicate read end
```

No `shell=True`, argv lists only, no sudo, no temp files, no SoX.

### Two independent children, managed by PID

Each child gets its own session via `start_new_session=True`. There is no
shared process group and no `preexec_fn`: with exactly two children, holding
the `Popen` objects and signalling each individually is simpler and safer than
arranging a common group.

Every signal goes through one guard:

```python
if proc is None or proc.poll() is not None:
    return False          # never signal a reaped (possibly reused) PID
proc.send_signal(sig)
```

`Popen` objects are kept, never bare PIDs, so a PID cannot outlive its child
and be signalled by accident. `ProcessLookupError` (a subclass of `OSError`)
from the race between `poll()` and `send_signal` is caught and logged, never
swallowed silently.

`restore_signals` is left at its default of `True`, which resets `SIGPIPE`
from Python's `SIG_IGN` back to `SIG_DFL` before `exec` — without it icedax
would spin on `EPIPE` instead of dying when aplay goes away. No custom signal
handling is added.

### Pause / resume

| | order | why |
| --- | --- | --- |
| pause | `SIGSTOP` icedax, then aplay | producer first, so aplay drains what is in flight |
| resume | `SIGCONT` aplay, then icedax | consumer first, so no burst on restart |

Audio already inside the ALSA buffer still plays out; sub-second pause latency
is expected and acceptable.

### Descriptor ownership

| fd | owner | closed by |
| --- | --- | --- |
| pipe write end | icedax only | icedax exit |
| pipe read end (parent copy) | parent | explicit `close()` immediately after aplay starts, in a `finally` so a failed launch still closes it |
| pipe read end (aplay copy) | aplay | aplay exit |
| `logs/icedax.log` | parent | `_cleanup()` |
| `logs/aplay.log` | parent | `_cleanup()` |

Child logs are truncated per run — on an appliance an append-only child log
would grow without bound, and the narrative lives in `logs/nexus-cd-player.log`.
A unit test runs repeated play/stop cycles and asserts the parent's open
descriptor count does not grow.

### Teardown

One idempotent `_cleanup()`:

1. `SIGCONT` both — **a `SIGSTOP`ped process would never act on `SIGTERM`**, so
   stopping while paused would otherwise hang. Covered by a unit test.
2. `SIGTERM` both.
3. Bounded wait (`TERM_GRACE_SECONDS`).
4. `SIGKILL` only whatever is still alive.
5. `wait()` both unconditionally — no zombies.
6. Close every owned descriptor; release the `Popen` references.

`stop()` is safe to call repeatedly, before `start()`, or after a failure.

### Failure matrix

| Failure | Response |
| --- | --- |
| icedax exits/closes stdout before first PCM | **aplay never launched**; cleanup; error carries icedax's return code and stderr tail |
| no PCM within `PCM_READY_TIMEOUT` | terminate icedax; cleanup; loading timeout error |
| icedax executable missing | cleanup; `PlaybackError` |
| aplay exits unexpectedly | terminate icedax; cleanup; ERROR with aplay's stderr tail |
| icedax exits 0 mid-playback (range finished) | leave aplay alone to drain pipe + ALSA, then report COMPLETED |
| icedax exits non-zero mid-playback | terminate aplay; cleanup; ERROR |
| disc removed mid-playback | surfaces as one of the two above; cleanup; ERROR |

Every path ends in `_cleanup()` and a known state, with the reason recorded and
the child's own stderr attached to the message.

## 8. Assumptions

Stated explicitly so they can be checked rather than trusted:

1. **The icedax TOC format is now CONFIRMED** against real Nexus output
   (`tests/fixtures/icedax_toc_real.txt`, captured with `-g`). Real lines look
   like `T01:       0  5:08.43` followed by `Leadout:  178442`, and the parser
   reproduces all 11 start sectors and durations exactly. Anything else still
   fails loudly with the raw output attached.
   **Caveat this exposed:** the real `-g` output carries *no* `audio`/`data`
   annotation — just a duration. Every track therefore defaults to
   `is_audio=True`, so `UNSUPPORTED_DISC` would not trigger for a data-only
   disc on this drive. The synthetic `icedax_toc_data_disc.txt` fixture uses an
   annotated form this drive does not actually produce. Harmless for audio CDs;
   needs a real data disc to resolve properly, and is not worth guessing at.
2. Track 1 starting at LBA 0 implies icedax reports pregap-adjusted LBAs.
   Total duration is computed as `(leadout − first_track_start)/75`, which is
   correct either way.
3. `-t N+M` continuing across track boundaries in one process is **confirmed
   on hardware**: Track 3 → 4 crossed with the icedax and aplay PIDs unchanged,
   no pipeline restart and no LOADING state, at disc position 545.05 s against
   a predicted 545.00 s.
4. Drive address is always discovered, never hardcoded. `2,0,0` exists only as
   a troubleshooting fallback (`NEXUS_CD_DEVICE`) and is **off by default**
   (`NEXUS_CD_ALLOW_FALLBACK=1` to opt in). Since `icedax -scanbus` is
   physically verified to find this drive, a scan that lists no optical device
   means NO_DRIVE — substituting a guessed address there would report
   DRIVE_PRESENT for a drive that is not connected and make NO_DRIVE
   unreachable.
5. Empty-tray output is matched **positively** by phrase ("no disk", "medium
   not present", …). An unrecognised failure — whatever the exit code — is
   ERROR carrying the return code and the complete raw output, never a guess
   at NO_DISC: calling a SCSI, cabling or firmware fault "no disc" would hide
   a real problem behind a benign-looking state. Real empty-tray output should
   still be captured to widen the phrase list.
6. `-n 10` and default overlap are kept, since `-P 0` measured *slower*
   (9.147 s vs 7.494 s) on this hardware.
7. No sudo, no temp files, no network anywhere in the design.
8. **A failed scan is diagnosed from the filesystem, not from icedax's
   wording.** icedax prints the same "Cannot open or use SCSI driver … make
   sure you are root" text whether the node is missing or unreadable. No
   `/dev/sg*` nodes → NO_DRIVE; nodes present but inaccessible → ERROR
   (permission); anything else → ERROR. This fixes a real misdiagnosis where a
   vanished drive was reported as a permission problem.
9. **A TOC read is retried exactly once, and only on timeout.** Observed on
   hardware: one 30 s timeout, then immediate success. `TOC_TIMEOUT_RETRIES=1`,
   `TOC_RETRY_DELAY=1.0 s`. NO_DISC, permission failures and malformed-but-
   complete output are answers, not transients, and are never retried.
10. **Disc changes require a stop first.** No media-presence polling, no
    `sg_turs` watchdog, no background probing — hot eject is out of scope
    because it wedged this Nexus's USB/SCSI stack.

## 9. Verification status

166 unit tests, no hardware required (1 skip: the Linux-only `POLLHUP`
discrimination). Coverage: scanbus parsing and drive classification, TOC
parsing against the **real captured fixture**, sector arithmetic, playback
process lifecycle with fake children, and the full controller state machine
with a fake engine and injected clock.

### Verified on the physical Nexus

**Playback V1** — drive discovered (PLDS DVD+-RW DH-16ABS, PD11, `2,0,0`),
real 11-track TOC parsed, PCM available after 5.84 s, 60 s continuous audio,
clean shutdown with no surviving processes, under Python 3.5.2.

**Controller V1** —

| Scenario | Result |
| --- | --- |
| Natural Track 3 → 4 | PID-stable, no restart, no LOADING, 545.05 s vs 545.00 s predicted |
| Pause / resume | froze at ~10.1 s, drifted 0.05 s over 5 s, resumed without restart |
| Next | Track 1 → 2, deliberate pipeline restart, LOADING → PLAYING |
| Previous | >3 s restarted the track; immediate repeat stepped back |
| Stop | STOPPED, idempotent, no surviving processes, DiscInfo cached |

`PlaybackEngine` and `CDController` are **frozen** unless a concrete
requirement exposes a bug.

### Known limitations

- Data-only discs are not distinguished (§8 assumption 1) — the real `-g`
  output carries no audio/data annotation.
- Hot eject during playback is unsupported by design (§6, §8 assumption 10).
- **`play()` and manual track changes block for ~5–8 s** while the engine waits
  for PCM. Fine for the CLI; not acceptable for a touchscreen, which must stay
  responsive and show LOADING. To be solved next by an asynchronous command
  adapter *around* the synchronous controller — not by redesigning the frozen
  engine.
