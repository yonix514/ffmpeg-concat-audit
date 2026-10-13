# ffmpeg-concat-audit

**Two ways ffmpeg silently changes the duration of your audio, and a tool that
tells you when it happened.**

Assemble 943 segments and the file can lose tens of seconds without a single
error message. This repository measures exactly how, with a self-test that
needs no media files of its own.

---

## The two mechanisms, measured

Both are reproduced by `audit/selftest.sh` on **ffmpeg 7.1.5**. Raw output in
[`examples/measurements.txt`](examples/measurements.txt).

### 1. `acrossfade` removes its duration, once per junction

`acrossfade=d=D` **shortens the total by exactly `D` per junction** compared
with a plain concatenation. Its default `overlap=1` behaves this way;
`overlap=0` does **not** — it keeps the full length.

| assembly of 4 × 2.000 s sources | total | vs concat |
| --- | --- | --- |
| plain `concat` | **8.000000 s** | — |
| `acrossfade=d=0.5` (default) | **6.500000 s** | −1.500 s = 3 × 0.5 s |
| `acrossfade=d=0.5:overlap=0` | **8.000000 s** | 0 |
| `acrossfade=d=0.5:overlap=1` | **6.500000 s** | −1.500 s = 3 × 0.5 s |

So the loss is **`N × D`**, and at `D = 0.030 s` over 943 junctions the
arithmetic gives **28.29 s** — which is how a 30-minute voice track comes out
26 seconds short while every command looks correct.

**This is not a bug in ffmpeg.** A crossfade *is* an overlap: shortening the
total is what the filter is for. The trap is that the default is `overlap=1`,
so a filter you add for smoothness quietly changes your timeline, and nothing
warns you.

**If you want the joins but not the length change**, use `overlap=0`. Verified
above: 8.000000 s, identical to concatenation.

### 2. Mixing sample rates in `concat` adds resampling padding

This one is less known and was found while building this tool.

```
m1.wav    44100 Hz     1.000000 s
mgap.wav  48000 Hz     0.500000 s
m2.wav    44100 Hz     1.000000 s
--------------------------------------------------
sum                    2.500000 s
concat output          2.544218 s   (+44.2 ms)
```

`concat` resamples to a common rate and pads to frame boundaries. **The same
three files at a single 48000 Hz rate concatenate to exactly 2.500000 s.**

Every millisecond of that padding shifts your audio against your video.

---

## The third thing, which is worse

Chaining many `acrossfade` with a **very small `d`** does not merely shift the
duration — it **collapses** it, unstably.

30 chained junctions at `d = 0.03` on 31 × 1.000 s sources should give
**30.100 s**. Three runs gave:

```
1.363197 s      1.827596 s      0.823333 s
```

The signal is still there (`max_volume` unchanged at −18.1 dB), the duration is
simply wrong, and **it differs between runs**. This is the single most
dangerous failure mode here, because it is neither an error nor a stable wrong
answer.

**Workaround, verified:** put the crossfades in **one filter graph** instead of
chaining processes.

```bash
ffmpeg -i s1.wav -i s2.wav -i s3.wav -i s4.wav \
  -filter_complex "[0][1]acrossfade=d=0.5[a];[a][2]acrossfade=d=0.5[b];[b][3]acrossfade=d=0.5[c]" \
  -map "[c]" out.wav
# -> 6.500000 s, exact
```

For a realistic number of junctions, prefer a **single filter graph** or plain
concatenation.

---

## Install

Nothing to install beyond ffmpeg. Standard library only.

```bash
git clone <this repo>
cd ffmpeg-concat-audit
ffmpeg -version          # 7.1.5 is what these numbers were measured on
bash audit/selftest.sh   # reproduces every number in this file
```

## Use

```bash
# audit an assembled file against the duration you intended
python3 -m audit.junctions voix_assemblee.wav --expect 1798.800

# machine-readable
python3 -m audit.junctions voix_assemblee.wav --expect 1798.800 --json
```

Output:

```
  duration          : 1798.800000 s   [exact (data chunk / block align)]
  expected          : 1798.800000 s
  error             : +0.000 ms (+0.000000 s)
  sample frames     : 86342400
  silence windows (silencedetect):
    count           : 943
    ...
  levels (astats, Overall):
    peak            : -1.502 dBFS
    rms             : -16.041 dBFS
```

### Why "exact" matters

For PCM WAV the tool computes the duration from the **`data` chunk size divided
by the block align**, not from the container. The container duration is rounded
to a frame and can include padding. In the mixed-rate example the container
said `2.544218` where the sample count was authoritative.

---

## What this repository got wrong, and corrected

An earlier version of this work — and a note I had published elsewhere — claimed
that `acrossfade`'s default `overlap=1` **"shortens every junction by 30 ms"**
as a *hidden defect*, and separately that `overlap=0` and `overlap=1` produced
**the same** duration.

**The first explanation was wrong. The second claim was also wrong.** The
measurements above settle both:

* the shortening is real and is **exactly `D`, the crossfade duration** — not a
  mysterious fixed 30 ms;
* `overlap=0` and `overlap=1` are **not** equivalent: `overlap=0` preserves the
  full duration, `overlap=1` removes `D` per junction.

I am leaving the correction in the open, with the failing claims named, because
a measurement you cannot check is worth nothing. Every number here is
reproducible by running one script.

> The `28.29 s` figure is **arithmetic** (943 × 0.030), not a measurement of a
> 943-junction chain. The measurement is the per-junction loss, which is
> exact. Stated so you can weigh it correctly.

---

## "What I measured"

| Measurement | Method | Result |
| --- | --- | --- |
| concat of 4 × 2.000 s, `-c copy` | `ffprobe` | **8.000000 s** |
| concat of 4 × 2.000 s, re-encoded | `ffprobe` | **8.000000 s** |
| `acrossfade=d=0.5`, default | `ffprobe` | **3.500000 s** (−0.5 s) |
| `acrossfade=d=0.5:overlap=0` | `ffprobe` | **4.000000 s** (no loss) |
| `acrossfade=d=0.5:overlap=1` | `ffprobe` | **3.500000 s** (−0.5 s) |
| 3 chained junctions, `d=0.5`, default | `ffprobe` | **6.500000 s** (loss 3 × 0.5 s) |
| 3 chained junctions, `d=0.5`, `overlap=0` | `ffprobe` | **8.000000 s** |
| single filter graph, 3 junctions | `ffprobe` | **6.500000 s** |
| mixed 44100/48000 Hz concat | `ffprobe` | **2.544218 s** vs 2.500000 s sum |
| single-rate 48000 Hz concat | exact (data chunk) | **2.500000 s** |
| 30 chained junctions at `d=0.03` | `ffprobe` | **1.36 / 1.83 / 0.82 s** vs 30.100 s expected |
| self-test assertions | `audit/selftest.sh` | **12 OK, 0 failed** |

---

## Limits

- **ffmpeg 7.1.5** on Debian. Other builds may differ; the self-test is how you
  find out in one command.
- The `d=0.03` collapse is reported as **unstable**, because it is. I did not
  isolate the exact internal cause — I measured the effect, repeatedly, and
  gave a workaround that holds.
- `silencedetect` is used as a **junction counter**, which is an approximation:
  it sees true silence, not "a join". It will not help if your sources have no
  digital silence.
- The tool is not a mixer. It reports; it does not repair.

## License

[MIT](LICENSE).

## Related

- [`vegas-script-toolkit`](https://github.com/youssef-montage) — the editing scripts this
  measurement discipline grew out of.
