from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
from array import array
from pathlib import Path

OUT = Path(os.getenv("PROBE_OUT", "audio_probe_artifacts"))
TIKTOK_OUT = OUT / "tiktok"
TAG = os.getenv("PROBE_TAG", "机器人")
OUT.mkdir(parents=True, exist_ok=True)


def load_probe():
    os.environ["PROBE_OUT"] = str(TIKTOK_OUT)
    os.environ["PROBE_TAG"] = TAG
    os.environ["PROBE_LIMIT"] = "30"
    os.environ["PROBE_DOWNLOAD_COUNT"] = "1"
    path = Path(__file__).resolve().parent / "tiktok_e2e_probe_v2.py"
    spec = importlib.util.spec_from_file_location("e2e", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return 0.0
    return math.sqrt(sum(int(x) * int(x) for x in samples) / len(samples))


def analyze(path: Path, seconds: float) -> dict:
    import webrtcvad

    sample_rate = 16000
    frame_ms = 30
    frame_bytes = int(sample_rate * frame_ms / 1000) * 2
    budget = min(seconds, 36.0)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-t", f"{budget:.3f}", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, check=False, timeout=90)
    pcm = result.stdout
    usable = len(pcm) - len(pcm) % frame_bytes
    vad = webrtcvad.Vad(2)
    total = voiced = active = non_speech = 0
    longest = current = bursts = 0
    was = False
    for off in range(0, usable, frame_bytes):
        frame = pcm[off:off + frame_bytes]
        total += 1
        level = rms(frame)
        is_active = level >= 390
        is_voice = vad.is_speech(frame, sample_rate)
        active += int(is_active)
        voiced += int(is_voice)
        if is_active and not is_voice:
            non_speech += 1
        if is_voice:
            current += 1
            longest = max(longest, current)
            if not was:
                bursts += 1
        else:
            current = 0
        was = is_voice
    speech_ratio = voiced / total if total else 0
    active_ratio = active / total if total else 0
    non_speech_ratio = non_speech / total if total else 0
    longest_s = longest * frame_ms / 1000
    narration = min(1.0, 0.68 * speech_ratio + 0.22 * min(1.0, longest_s / 7.0) + 0.10 * min(1.0, bursts / max(1.0, budget / 3.0)))
    clean = max(0.0, min(1.0, 1.0 - narration * 0.82 + max(0.0, non_speech_ratio * 1.35 + active_ratio * 0.15 - narration * 0.30) * 0.18))
    return {
        "analyzed_seconds": round(total * frame_ms / 1000, 3),
        "speech_ratio": round(speech_ratio, 4),
        "active_audio_ratio": round(active_ratio, 4),
        "non_speech_active_ratio": round(non_speech_ratio, 4),
        "longest_speech_run_seconds": round(longest_s, 3),
        "speech_bursts": bursts,
        "narration_probability": round(narration, 4),
        "clean_source_score": round(clean, 4),
        "pcm_bytes_processed": len(pcm),
        "full_video_decoded": seconds <= 36.0,
    }


def main() -> int:
    probe = load_probe()
    report = probe.run()
    selected = report.get("selected") or []
    downloads = report.get("downloads") or []
    if not selected or not downloads or not downloads[0].get("ok"):
        raise SystemExit("TikTok download stage failed")
    video_id = str(selected[0]["id"])
    path = TIKTOK_OUT / "media" / f"{video_id}.mp4"
    seconds = duration(path)
    audio = analyze(path, seconds)
    result = {
        "ok": True,
        "tag": TAG,
        "video_id": video_id,
        "duration": seconds,
        "download_size": path.stat().st_size,
        "audio": audio,
        "policy": "max 36s mono 16k PCM + WebRTC VAD; no ASR/no GPU",
    }
    (OUT / "audio_probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("AUDIO_PROBE=" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
