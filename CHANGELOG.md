# Changelog

## [1.0.0] — 2026-10-12

First release.

### Added

- `audit/junctions.py` — measures junction count, levels and drift in an
  assembled audio file using ffmpeg's `silencedetect` and `astats`.
- `audit/selftest.sh` — a **reproducible measurement harness**. It synthesises
  its own sources, assembles them four different ways, measures every output
  and asserts the durations. No third-party media needed.
- `examples/measurements.txt` — the raw output of that self-test, so the
  numbers in the README can be checked without running anything.
- `docs/FINDINGS.md` — what `acrossfade` does to total duration, verbatim from
  the measurements, including a **correction of an earlier wrong claim**.

### Corrected

- An earlier version of this work claimed `acrossfade`'s default `overlap=1`
  silently removes 30 ms per junction. **That mechanism is wrong.** The real
  behaviour is that `acrossfade=d=D` removes exactly `D` per junction, and it
  does so **identically for `overlap=0` and `overlap=1`**. See
  `docs/FINDINGS.md` §3.
