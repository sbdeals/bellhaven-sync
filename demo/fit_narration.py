"""Fit the silent demo video to a narration take.

Each of the 16 scenes is retimed (sped up <=1.35x then trimmed, or last-frame padded) so
that it starts exactly when the matching narration section starts. One slip in the take
("Sorry.") is cut. Output: demo/output/bellhaven-demo-narrated.mp4
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

OUT = Path("demo/output")
VIDEO = OUT / "bellhaven-demo.mp4"
AUDIO = OUT / "audio" / "take1.m4a"
WORK = OUT / "work"
FINAL = OUT / "bellhaven-demo-narrated.mp4"
MAX_SPEED = 1.35
FPS = 25

# video_t of each scene start (from demo/output/timestamps.json) + end
VIDEO_CUES = [0.00, 7.23, 24.31, 32.31, 45.12, 59.88, 66.09, 72.02, 80.20, 97.62,
              104.65, 116.52, 126.82, 133.98, 142.41, 154.62]
VIDEO_END = 162.20
# first-word time of each narration section in the raw take (whisper word timestamps)
AUDIO_STARTS = [0.0, 11.0, 26.9, 34.6, 47.1, 64.7, 71.7, 81.3, 87.0, 105.6,
                110.7, 121.6, 131.0, 136.5, 140.6, 150.1]
AUDIO_LAST_WORD_END = 156.8
LEAD = 0.3          # start each scene slightly before the first word
TAIL_HOLD = 1.5     # hold the last frame after the last word
CUTS = [(58.3, 60.0)]  # "Sorry." + surrounding pause


def run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd[:12]), "..." if len(cmd) > 12 else "")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def remap(t: float) -> float:
    """Raw take time -> edited audio time."""
    for a, b in CUTS:
        if t >= b:
            t -= b - a
        elif t > a:
            t = a
    return t


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    # ---------------------------------------------------------------- audio
    total = remap(AUDIO_LAST_WORD_END) + TAIL_HOLD
    parts, labels = [], []
    pos = 0.0
    for i, (a, b) in enumerate(CUTS + [(None, None)]):
        end = a if a is not None else None
        trim = f"atrim=start={pos}" + (f":end={end}" if end is not None else "")
        parts.append(f"[0:a]{trim},asetpts=N/SR/TB[p{i}]")
        labels.append(f"[p{i}]")
        if b is not None:
            pos = b
    fc = ";".join(parts) + f";{''.join(labels)}concat=n={len(labels)}:v=0:a=1,highpass=f=80," \
         f"loudnorm=I=-16:TP=-1.5:LRA=11,apad=whole_dur={total:.3f}[aout]"
    edited_audio = WORK / "narration.m4a"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(AUDIO), "-filter_complex", fc, "-map", "[aout]",
         "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", str(edited_audio)])
    print(f"audio: {total:.2f}s after cutting {sum(b - a for a, b in CUTS):.1f}s")

    # ---------------------------------------------------------------- video
    starts = [max(0.0, remap(t) - (LEAD if i else 0.0)) for i, t in enumerate(AUDIO_STARTS)]
    ends = starts[1:] + [total]
    seg_files = []
    rows = []
    for i, (vs, ve, as_, ae) in enumerate(zip(VIDEO_CUES, VIDEO_CUES[1:] + [VIDEO_END], starts, ends), 1):
        L, D = ve - vs, ae - as_
        f = WORK / f"seg{i:02d}.mp4"
        if L >= D:
            speed = min(L / D, MAX_SPEED)
            vf = f"setpts=PTS/{speed:.5f},fps={FPS}"
            mode = f"speed x{speed:.2f}" + (f", trim {L / speed - D:.1f}s" if L / speed - D > 0.05 else "")
        else:
            vf = f"fps={FPS},tpad=stop_mode=clone:stop_duration={D - L:.3f}"
            mode = f"hold +{D - L:.1f}s"
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{vs:.3f}", "-to", f"{ve:.3f}", "-i", str(VIDEO),
             "-vf", vf, "-t", f"{D:.3f}", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-r", str(FPS), str(f)])
        seg_files.append(f)
        rows.append((i, vs, L, as_, D, mode))
        print(f"scene {i:2d}: video {vs:6.2f}s len {L:5.1f}s -> audio {as_:6.2f}s len {D:5.1f}s  [{mode}]")
    concat_list = WORK / "concat.txt"
    concat_list.write_text("".join(f"file '{p.name}'\n" for p in seg_files), encoding="utf-8")
    silent = WORK / "video_fitted.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(silent)])
    run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-i", str(edited_audio), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart", "-shortest", str(FINAL)])
    json.dump({"scenes": [dict(scene=i, video_start=vs, video_len=L, audio_start=a, target_len=D, mode=m)
                          for i, vs, L, a, D, m in rows], "total": total}, open(OUT / "fit_report.json", "w"), indent=1)
    print("wrote", FINAL)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[1])
    main()
