from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

ROOT = Path("artifacts/render_probe")
ROOT.mkdir(parents=True, exist_ok=True)


def run(*args: str) -> None:
    subprocess.run(list(args), check=True, timeout=120)


def probe_json(path: Path, *, count_frames: bool = False) -> dict:
    args = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,size:stream=codec_name,codec_type,width,height,r_frame_rate,avg_frame_rate,time_base,sample_rate,channels,nb_frames,nb_read_frames",
    ]
    if count_frames:
        args.append("-count_frames")
    args += ["-of", "json", str(path)]
    return json.loads(subprocess.check_output(args, text=True, timeout=30))


def rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    return float(Fraction(value))


# Two deliberately different source shapes to prove normalization before concat.
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
    "-f", "lavfi", "-i", "color=c=red:s=540x960:r=30:d=3",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(ROOT/"a.mp4"))
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
    "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=25:d=3",
    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=44100:duration=3",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(ROOT/"b.mp4"))

vf = "scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2:color=black,fps=24,format=yuv420p"
common_video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-video_track_timescale", "24000"]
common_audio = ["-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2"]

# Clip A keeps original audio.
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.2", "-i", str(ROOT/"a.mp4"), "-t", "1.2",
    "-map", "0:v:0", "-map", "0:a:0", "-vf", vf,
    "-af", "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
    *common_video, *common_audio, "-shortest", str(ROOT/"seg1.mp4"))

# Real Chinese TTS over a frozen frame.
run("python", "-m", "edge_tts", "--voice", "zh-CN-XiaoxiaoNeural", "--text", "有网友评论：这个动作太快了。", "--write-media", str(ROOT/"comment.mp3"))
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.36", "-i", str(ROOT/"a.mp4"), "-frames:v", "1", "-vf", vf.replace(",fps=24,format=yuv420p", ""), "-q:v", "3", str(ROOT/"freeze.jpg"))
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "24", "-i", str(ROOT/"freeze.jpg"), "-i", str(ROOT/"comment.mp3"), "-t", "0.8",
    "-map", "0:v:0", "-map", "1:a:0", "-vf", "fps=24,format=yuv420p",
    "-af", "apad=pad_dur=0.8,atrim=duration=0.8,aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
    *common_video, *common_audio, "-shortest", str(ROOT/"seg2.mp4"))

# Clip B intentionally discards source audio and injects silence.
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.5", "-i", str(ROOT/"b.mp4"), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1.0",
    "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
    *common_video, *common_audio, "-shortest", str(ROOT/"seg3.mp4"))

listing = ROOT / "mix.ffconcat"
listing.write_text("ffconcat version 1.0\n" + "".join(f"file '{(ROOT/name).resolve().as_posix()}'\n" for name in ("seg1.mp4", "seg2.mp4", "seg3.mp4")), encoding="utf-8")
run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", "-video_track_timescale", "24000", str(ROOT/"mix.mp4"))

info = probe_json(ROOT/"mix.mp4", count_frames=True)
duration = float(info["format"]["duration"])
streams = info.get("streams", [])
video = next(x for x in streams if x.get("codec_type") == "video")
audio = next(x for x in streams if x.get("codec_type") == "audio")
avg = rate(video.get("avg_frame_rate"))
read_frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
assert 2.85 <= duration <= 3.15, duration
assert video["codec_name"] == "h264"
assert int(video["width"]) == 360 and int(video["height"]) == 640
assert 23.8 <= avg <= 24.2, (video.get("r_frame_rate"), video.get("avg_frame_rate"))
assert 68 <= read_frames <= 76, read_frames
assert audio["codec_name"] == "aac"
summary = {
    "ok": True,
    "duration": duration,
    "size": int(info["format"]["size"]),
    "video": video,
    "actual_avg_fps": avg,
    "counted_frames": read_frames,
    "audio": audio,
    "tts_bytes": (ROOT/"comment.mp3").stat().st_size,
    "strategy": "sequential normalize -> fixed track timescale -> concat stream copy",
}
(ROOT/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
