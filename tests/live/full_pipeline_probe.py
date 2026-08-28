from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.getenv("PROBE_OUT", "full_pipeline_artifacts"))
TIKTOK_OUT = OUT / "tiktok"
FRAME_DIR = OUT / "frames"
DB_PATH = OUT / "probe.sqlite3"
BASE = os.getenv("PROBE_VLM_URL", "http://127.0.0.1:8080/v1").rstrip("/")
MODEL = os.getenv("PROBE_VLM_MODEL", "trend-video-brain")
TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#") or "机器人"
LIMIT = max(20, int(os.getenv("PROBE_LIMIT", "40")))

OUT.mkdir(parents=True, exist_ok=True)
FRAME_DIR.mkdir(parents=True, exist_ok=True)


def dump(name: str, payload: Any) -> None:
    (OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def load_tiktok_probe():
    os.environ["PROBE_OUT"] = str(TIKTOK_OUT)
    os.environ["PROBE_TAG"] = TAG
    os.environ["PROBE_LIMIT"] = str(LIMIT)
    os.environ["PROBE_DOWNLOAD_COUNT"] = "1"
    path = ROOT / "tests" / "live" / "tiktok_e2e_probe_v2.py"
    spec = importlib.util.spec_from_file_location("tiktok_e2e_probe_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load TikTok E2E probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ffprobe_duration(video: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True,
        text=True,
        check=True,
    )
    return max(0.1, float(result.stdout.strip()))


def extract_frames(video: Path) -> list[dict[str, Any]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    duration = ffprobe_duration(video)
    ratios = (0.10, 0.32, 0.55, 0.78, 0.92)
    frames: list[dict[str, Any]] = []
    for index, ratio in enumerate(ratios, start=1):
        timestamp = min(duration - 0.05, max(0.0, duration * ratio))
        target = FRAME_DIR / f"frame_{index:02d}_{timestamp:.2f}.jpg"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp:.3f}", "-i", str(video),
                "-frames:v", "1",
                "-vf", "scale=768:-2:force_original_aspect_ratio=decrease",
                "-q:v", "3", str(target),
            ],
            check=True,
        )
        if not target.exists() or target.stat().st_size < 1000:
            raise RuntimeError(f"frame extraction failed at {timestamp:.2f}s")
        frames.append({"path": str(target), "timestamp": round(timestamp, 3), "size": target.stat().st_size})
    return frames


def parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        value = json.loads(clean)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    first, last = clean.find("{"), clean.rfind("}")
    if first >= 0 and last > first:
        value = json.loads(clean[first:last + 1])
        if isinstance(value, dict):
            return value
    raise RuntimeError("VLM response did not contain a JSON object: " + text[:800])


def analyze_frames(frames: list[dict[str, Any]], selected: dict[str, Any], duration: float) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "You are analyzing sampled frames from one short video. "
                "Infer the visible event conservatively. Return ONLY one JSON object with keys: "
                "summary (string), categories (array of strings), hook_score (0-10 number), "
                "main_event (object with start,end,description), best_clip (object with start,end), "
                "needs_narration (boolean), original_audio_value (low|medium|high), confidence (0-1 number). "
                f"Video duration is {duration:.2f}s. TikTok caption: {selected.get('description') or ''}"
            ),
        }
    ]
    for frame in frames:
        raw = Path(frame["path"]).read_bytes()
        uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        content.append({"type": "text", "text": f"Frame sampled at {frame['timestamp']:.2f}s:"})
        content.append({"type": "image_url", "image_url": {"url": uri}})

    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 450,
        "messages": [{"role": "user", "content": content}],
    }
    request = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        raw = json.loads(response.read().decode("utf-8"))
    text = str(raw["choices"][0]["message"]["content"])
    parsed = parse_json_object(text)
    return {"raw_text": text, "parsed": parsed}


def persist_and_verify(selected: dict[str, Any], download: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS media(video_id TEXT PRIMARY KEY, url TEXT, author_id TEXT, description TEXT, play_count INTEGER, sha256 TEXT, local_path TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS brain(video_id TEXT PRIMARY KEY, analysis_json TEXT NOT NULL)"
        )
        video_id = str(selected["id"])
        local_path = str(TIKTOK_OUT / "media" / f"{video_id}.mp4")
        conn.execute(
            "INSERT OR REPLACE INTO media VALUES(?,?,?,?,?,?,?)",
            (video_id, selected.get("url"), selected.get("author_id"), selected.get("description"), int(selected.get("play_count") or 0), download.get("sha256"), local_path),
        )
        conn.execute(
            "INSERT OR REPLACE INTO brain VALUES(?,?)",
            (video_id, json.dumps(analysis["parsed"], ensure_ascii=False)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT m.video_id,m.sha256,b.analysis_json FROM media m JOIN brain b ON b.video_id=m.video_id WHERE m.video_id=?",
            (video_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("SQLite roundtrip failed")
        restored = json.loads(row[2])
        return {"ok": True, "video_id": row[0], "sha256": row[1], "summary": restored.get("summary")}
    finally:
        conn.close()


def main() -> int:
    report: dict[str, Any] = {"tag": TAG, "limit": LIMIT, "stages": {}}
    try:
        probe = load_tiktok_probe()
        tiktok = probe.run()
        report["stages"]["discovery"] = {
            "ok": tiktok.get("candidate_count", 0) > 0,
            "candidate_count": tiktok.get("candidate_count"),
            "page_count": len(tiktok.get("pages") or []),
        }
        if not tiktok.get("selected") or not tiktok.get("downloads"):
            raise RuntimeError("TikTok stage produced no selected/downloaded material")
        selected = tiktok["selected"][0]
        download = tiktok["downloads"][0]
        if not download.get("ok"):
            raise RuntimeError("TikTok media download failed: " + repr(download))
        report["stages"]["download"] = {k: download.get(k) for k in ("ok", "id", "size_bytes", "declared_size", "sha256", "chunks", "content_type")}

        video = TIKTOK_OUT / "media" / f"{selected['id']}.mp4"
        duration = ffprobe_duration(video)
        frames = extract_frames(video)
        report["stages"]["frames"] = {"ok": len(frames) >= 4, "duration": duration, "frames": frames}

        analysis = analyze_frames(frames, selected, duration)
        if not analysis["parsed"].get("summary"):
            raise RuntimeError("VLM analysis missing summary")
        report["stages"]["vlm"] = {"ok": True, "model": MODEL, "analysis": analysis["parsed"]}

        roundtrip = persist_and_verify(selected, download, analysis)
        report["stages"]["sqlite"] = roundtrip
        report["selected"] = selected
        report["ok"] = all(stage.get("ok") for stage in report["stages"].values())
    except Exception as exc:
        report["ok"] = False
        report["error"] = repr(exc)
        dump("full_pipeline_summary.json", report)
        print("FULL_PIPELINE=" + json.dumps(report, ensure_ascii=False))
        return 2

    dump("full_pipeline_summary.json", report)
    print("FULL_PIPELINE=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
