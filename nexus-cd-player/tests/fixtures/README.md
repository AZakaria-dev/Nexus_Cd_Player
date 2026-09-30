# Test fixtures — provenance

Parser tests must never be written against invented tool output. Each fixture
here is labelled with where it actually came from.

| File | Provenance |
| --- | --- |
| `icedax_scanbus_real.txt` | **Real.** Verbatim device line reported from the Nexus 7. |
| `icedax_scanbus_full_layout.txt` | **Synthetic.** Full cdrtools scanbus layout (bus headers, `NNN)` slot numbers, `*` empty slots) to prove the parser tolerates the unabridged form. |
| `icedax_scanbus_no_drive.txt` | **Synthetic.** Bus present, no optical device. |
| `icedax_toc_real.txt` | **Real, captured on the Nexus** with `-g`. Verbatim stdout+stderr including the wodim deprecation warning. The parser is locked to this. |
| `icedax_toc_no_disc.txt` | **Synthetic.** Empty tray. |
| `icedax_toc_malformed.txt` | **Synthetic.** Truncated/garbage output. |
| `icedax_toc_single_track.txt` | **Synthetic.** One-track audio CD. |
| `icedax_toc_data_disc.txt` | **Synthetic.** Non-audio disc. |

Real `-g` track lines look like `T01:       0  5:08.43` — a start sector and a
duration, with **no `audio`/`data` annotation**. The synthetic fixtures use the
annotated `T01:  <sector>  audio …` form, which this drive does not actually
produce; the parser accepts both because the annotation is optional.

Consequence worth knowing: because real output never says "audio" or "data",
every track defaults to `is_audio=True`, so `UNSUPPORTED_DISC` will not trigger
for a data-only disc on this drive. `icedax_toc_data_disc.txt` therefore tests
the classifier, not this drive's real behaviour. Resolving it properly needs a
real data disc captured from the Nexus.

The measured sector numbers from the physical test disc are **not** stored as a
fixture — normalised stand-in text must not masquerade as a format fixture.
They live in `tests/test_disc.py` as `REAL_STARTS` / `REAL_LEADOUT` and drive
`disc.build_disc()` directly, so the sector arithmetic is verified against real
numbers without asserting anything about text layout.

## Capturing the real TOC fixture

On the Nexus:

```
./tools/capture_fixtures.sh
```

It writes `tests/fixtures/icedax_toc_real.txt` (and refreshes the scanbus
fixture) from the actual drive. Commit whatever it produces **unedited** —
including any warnings icedax prints. The warnings are part of the format the
parser has to survive.
