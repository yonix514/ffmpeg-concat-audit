#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit.junctions — inspect assembled audio for junction and duration problems.

Why this exists
---------------
When you build one long audio file out of many segments, the total duration
drifts and the seams are audible, and neither is obvious from the commands you
ran. This tool reports both.

It measures, it does not guess:

* **Duration, exactly, for PCM WAV** — by parsing the `data` chunk and dividing
  by the block align, not by asking the container. The container duration for
  PCM can be off by up to a frame of padding; the sample count cannot.
* **Duration, approximately, for anything else** — via `ffprobe`.
* **Silence windows** — via `ffmpeg -af silencedetect`, used as a junction
  counter.
* **Levels** — via `ffmpeg -af astats`.

Standard library only (plus ffmpeg / ffprobe on PATH). No pip installs.

Measurement caveats, stated rather than hidden
----------------------------------------------
1. `astats=measure_overall=Peak_level,RMS_level,DC_offset` **fails on ffmpeg 7**
   (it rejects `RMS_level`). This tool uses plain `astats` and reads the
   `Overall` block, which is the **last** occurrence of each label. Reading the
   **first** occurrence returns channel 1 as if it were the whole file — a real
   bug this tool previously had.

2. `silencedetect` with a `-120 dB` gate finds **nothing**, even in a file of
   pure zeros, because a 16-bit zero sample reports `-inf` and the comparison
   path does not behave as "everything is below the gate". `-50 dB` works and
   still only matches true silence. This tool's default is `-50 dB`.

3. Do **not** put `highpass` in front of `silencedetect` when you care about
   junction positions: the filter ring-in moves the reported start. Measured:
   `silence_start: 1.000` without highpass versus `1.007708` with
   `highpass=f=20` — a **7.7 ms** error, which is exactly the order of magnitude
   of the junctions you are trying to count.

4. `anullsrc` reports `mean_volume: -91.0 dB` (a 1-LSB dither) while `astats`
   reports `Peak level: -inf`. Prefer `aevalsrc=0:d=<seconds>:s=<rate>` when you
   need provably zero samples.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field, asdict

# -50 dB matches true digital silence and nothing else in practice.
# See caveat 2 in the module docstring for why -120 dB does not work.
SILENCE_NOISE_DB = -50.0
SILENCE_MIN_S = 0.010


class ToolMissing(RuntimeError):
    pass


def _require(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolMissing(
            f"{name!r} is not on PATH. Install ffmpeg (it provides both ffmpeg "
            f"and ffprobe), then re-run."
        )
    return path


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


# --------------------------------------------------------------- WAV exact
def wav_exact_duration(path: str) -> dict | None:
    """Exact duration of a PCM WAV by parsing chunks. None if not a WAV.

    The container's reported duration is rounded to a frame and can include
    padding. The number of sample frames in the `data` chunk cannot be wrong.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None

    i = 12
    fmt = None
    data_size = None
    while i + 8 <= len(data):
        cid = data[i:i + 4]
        size = struct.unpack("<I", data[i + 4:i + 8])[0]
        body = i + 8
        if cid == b"fmt " and size >= 16 and body + 16 <= len(data):
            fmt = struct.unpack("<HHIIHH", data[body:body + 16])
        elif cid == b"data":
            data_size = size
        i = body + size + (size & 1)

    if fmt is None or data_size is None:
        return None
    channels, sample_rate, _byte_rate, block_align, bits = fmt[1], fmt[2], fmt[3], fmt[4], fmt[5]
    if not block_align or not sample_rate:
        return None
    # Honour the declared data size, but never read past the real file.
    usable = min(data_size, max(0, len(data) - 44))
    frames = usable // block_align
    return {
        "exact_duration_s": frames / sample_rate,
        "sample_frames": frames,
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits,
        "block_align": block_align,
        "data_bytes": data_size,
        "container": "wav",
    }


# ------------------------------------------------------------------- probe
def probe(path: str) -> dict:
    cmd = [
        _require("ffprobe"), "-v", "error",
        "-show_entries", "format=duration,size,bit_rate,format_name",
        "-show_entries", "stream=codec_name,sample_rate,channels,bits_per_sample",
        "-of", "json", path,
    ]
    out = _run(cmd)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path!r}: {out.stderr.strip()}")
    d = json.loads(out.stdout or "{}")
    fmt = d.get("format", {}) or {}
    streams = d.get("streams") or [{}]
    s = streams[0]
    info = {
        "path": path,
        "container_duration_s": float(fmt.get("duration") or 0.0),
        "size_bytes": int(fmt.get("size") or (os.path.getsize(path) if os.path.exists(path) else 0)),
        "format_name": fmt.get("format_name"),
        "codec_name": s.get("codec_name"),
        "sample_rate": s.get("sample_rate"),
        "channels": s.get("channels"),
        "bits_per_sample": s.get("bits_per_sample"),
    }
    exact = wav_exact_duration(path)
    if exact:
        info.update(exact)
        info["duration_s"] = exact["exact_duration_s"]
        info["duration_source"] = "exact (data chunk / block align)"
    else:
        info["duration_s"] = info["container_duration_s"]
        info["duration_source"] = "container (approximate for PCM)"
    return info


# ----------------------------------------------------------------- levels
def _last_float(label: str, text: str) -> float | None:
    """Last occurrence of '<label> <number>'. `astats` prints an Overall block
    after the per-channel blocks, so the last value is the whole file."""
    hits = re.findall(re.escape(label) + r"\s*:\s*(-?(?:inf|infinity|[0-9.]+))", text)
    if not hits:
        return None
    v = hits[-1]
    if v in ("inf", "-inf", "infinity", "-infinity"):
        return None          # pure digital silence is not a number
    return float(v)


@dataclass
class Silences:
    count: int = 0
    total_s: float = 0.0
    shortest_s: float | None = None
    longest_s: float | None = None
    median_s: float | None = None
    windows: list = field(default_factory=list)


@dataclass
class Levels:
    peak_db: float | None = None
    rms_db: float | None = None
    dc_offset: float | None = None


@dataclass
class Report:
    file: dict
    silences: Silences
    levels: Levels
    expected_duration_s: float | None = None
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def duration_error_s(self) -> float | None:
        if self.expected_duration_s is None:
            return None
        return self.file["duration_s"] - self.expected_duration_s

    @property
    def duration_error_ms(self) -> float | None:
        e = self.duration_error_s
        return None if e is None else e * 1000.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_error_s"] = self.duration_error_s
        d["duration_error_ms"] = self.duration_error_ms
        return d


def measure_silences(path: str, noise_db: float = SILENCE_NOISE_DB) -> tuple[Silences, list]:
    cmd = [
        _require("ffmpeg"), "-nostdin", "-v", "info", "-i", path,
        # No highpass here on purpose: it shifts the reported start. Caveat 3.
        "-af", f"silencedetect=noise={noise_db}dB:d={SILENCE_MIN_S}",
        "-f", "null", "-",
    ]
    out = _run(cmd)
    text = (out.stderr or "") + (out.stdout or "")
    warn: list = []

    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[0-9.]+)", text)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[0-9.]+)", text)]

    windows = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > s:
            windows.append({"start_s": round(s, 6), "end_s": round(e, 6),
                            "duration_s": round(e - s, 6)})

    sil = Silences(count=len(starts), windows=windows,
                   total_s=round(sum(w["duration_s"] for w in windows), 6))
    if windows:
        w = sorted(x["duration_s"] for x in windows)
        sil.shortest_s, sil.longest_s = round(w[0], 6), round(w[-1], 6)
        mid = len(w) // 2
        sil.median_s = round(w[mid] if len(w) % 2 else (w[mid - 1] + w[mid]) / 2, 6)
    if out.returncode != 0:
        tail = (out.stderr or "").strip().splitlines()[-1:]
        warn.append(f"silencedetect did not run cleanly: {tail}")
    return sil, warn


def measure_levels(path: str) -> tuple[Levels, list]:
    cmd = [
        _require("ffmpeg"), "-nostdin", "-v", "info", "-i", path,
        "-af", "astats", "-f", "null", "-",
    ]
    out = _run(cmd)
    text = (out.stderr or "") + (out.stdout or "")
    lv = Levels(
        peak_db=_last_float("Peak level dB", text),
        rms_db=_last_float("RMS level dB", text),
        dc_offset=_last_float("DC offset", text),
    )
    warn: list = []
    if lv.peak_db is None and lv.rms_db is None:
        warn.append(
            "No numeric level: this is expected for pure digital silence "
            "(ffmpeg reports -inf). If the file is not silent, astats output "
            "could not be parsed."
        )
    return lv, warn


def audit(path: str, expected_duration_s: float | None = None,
          noise_db: float = SILENCE_NOISE_DB) -> Report:
    file_info = probe(path)
    sil, w1 = measure_silences(path, noise_db)
    lv, w2 = measure_levels(path)
    rep = Report(file=file_info, silences=sil, levels=lv,
                 expected_duration_s=expected_duration_s, warnings=w1 + w2)

    if expected_duration_s is not None and rep.duration_error_ms is not None:
        err = rep.duration_error_ms
        if abs(err) >= 1.0:
            rep.notes.append(
                f"DURATION DRIFT: {err:+.1f} ms ({rep.duration_error_s:+.4f} s) "
                "against expected. A chain of N `acrossfade` with default "
                "`overlap=1` removes D per junction versus plain concatenation; "
                "mixing sample rates in `concat` adds resampling padding."
            )
        else:
            rep.notes.append(f"Duration matches expected within {abs(err):.1f} ms.")
    if lv.peak_db is not None and lv.peak_db <= -90.0:
        rep.notes.append("SILENT OUTPUT: peak at or below -90 dBFS.")
    if sil.count == 0:
        rep.notes.append(
            "No silence window at or below the gate. Either the chain "
            "crossfaded the joins away, or the source has no true silence."
        )
    return rep


def format_report(rep: Report) -> str:
    f = rep.file
    L = []
    L.append("=" * 72)
    L.append(f"JUNCTION AUDIT — {f['path']}")
    L.append("=" * 72)
    L.append(f"  duration          : {f['duration_s']:.6f} s   [{f['duration_source']}]")
    if f.get("container_duration_s") is not None and f["duration_source"].startswith("exact"):
        L.append(f"  container said    : {f['container_duration_s']:.6f} s "
                 f"(difference {(f['duration_s']-f['container_duration_s'])*1000:+.3f} ms)")
    if rep.expected_duration_s is not None:
        L.append(f"  expected          : {rep.expected_duration_s:.6f} s")
        L.append(f"  error             : {rep.duration_error_ms:+.3f} ms "
                 f"({rep.duration_error_s:+.6f} s)")
    L.append(f"  size              : {f['size_bytes']} bytes")
    L.append(f"  codec             : {f['codec_name']} {f['sample_rate']} Hz, "
             f"{f['channels']} ch, {f['bits_per_sample']} bit")
    if f.get("sample_frames"):
        L.append(f"  sample frames     : {f['sample_frames']}")
    L.append("")
    L.append("  silence windows (silencedetect):")
    L.append(f"    count           : {rep.silences.count}")
    L.append(f"    total           : {rep.silences.total_s:.6f} s")
    if rep.silences.count:
        L.append(f"    shortest        : {rep.silences.shortest_s:.6f} s")
        L.append(f"    median          : {rep.silences.median_s:.6f} s")
        L.append(f"    longest         : {rep.silences.longest_s:.6f} s")
        for w in rep.silences.windows[:5]:
            L.append(f"      {w['start_s']:8.3f} -> {w['end_s']:8.3f}  ({w['duration_s']:.3f} s)")
        if rep.silences.count > 5:
            L.append(f"      ... {rep.silences.count - 5} more")
    L.append("")
    L.append("  levels (astats, Overall):")
    L.append(f"    peak            : {rep.levels.peak_db} dBFS")
    L.append(f"    rms             : {rep.levels.rms_db} dBFS")
    L.append(f"    dc offset       : {rep.levels.dc_offset}")
    if rep.notes:
        L.append("")
        L.append("  notes:")
        for n in rep.notes:
            L.append(f"    - {n}")
    if rep.warnings:
        L.append("")
        L.append("  warnings:")
        for w in rep.warnings:
            L.append(f"    ! {w}")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    expected = None
    if "--expect" in args:
        i = args.index("--expect")
        if i + 1 >= len(args):
            print("--expect needs a value in seconds", file=sys.stderr)
            return 2
        try:
            expected = float(args[i + 1])
        except ValueError:
            print("--expect value must be a number", file=sys.stderr)
            return 2
        del args[i:i + 2]
    if not args:
        print(__doc__)
        print("Usage: python -m audit.junctions FILE [--expect SECONDS] [--json]")
        return 2
    try:
        rep = audit(args[0], expected)
    except ToolMissing as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print(json.dumps(rep.to_dict(), indent=1) if as_json else format_report(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
