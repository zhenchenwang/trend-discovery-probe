from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(os.getenv("PROBE_ROOT", "artifacts/real_mix_crosssource"))
ROOT.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#") or "机器人"
LIMIT = max(30, min(120, int(os.getenv("PROBE_LIMIT", "55"))))
MINIMUM_DURATION = max(8.0, float(os.getenv("PROBE_MIN_DURATION", "12")))
FPS = 24
W, H = 360, 640


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    return float(Fraction(value))


def cjk_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def clean_context(text: str, limit: int = 34) -> str:
    value = re.sub(r"(?:\s*#[^\s#]+)+\s*$", "", str(text or "")).strip()
    value = re.sub(r"\s+", " ", value).strip(" ，,。.!！?？")
    return value[:limit].rstrip(" ，,。.!！?？")


def choose_youtube_comment(yt: ModuleType) -> tuple[dict[str, Any], dict[str, Any]]:
    query = f"{TAG} shorts"
    search_url = "https://www.youtube.com/results?" + yt.urllib.parse.urlencode({"search_query": query})
    status, raw = yt.request(search_url)
    html = raw.decode("utf-8", errors="replace")
    video_ids: list[str] = []
    for video_id in yt.VIDEO_ID_RE.findall(html):
        if video_id not in video_ids:
            video_ids.append(video_id)

    attempts: list[dict[str, Any]] = []
    pool: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for video_id in video_ids[:8]:
        try:
            row = yt.fetch_comments(video_id)
        except Exception as exc:
            attempts.append({"video_id": video_id, "error": repr(exc)})
            continue
        comments = list(row.get("comments") or [])
        attempts.append(
            {
                "video_id": video_id,
                "watch_status": row.get("watch_status"),
                "next_status": row.get("next_status"),
                "continuations": row.get("continuations"),
                "tried": row.get("tried"),
                "comments": len(comments),
            }
        )
        for comment in comments:
            text = str(comment.get("text") or "").strip()
            if text:
                pool.append((row, comment))
        if any(cjk_count(str(comment.get("text") or "")) >= 2 for _, comment in pool):
            break
        if len(pool) >= 6:
            break

    if not pool:
        raise RuntimeError(f"YouTube plain-HTTP comment pool was empty; attempts={attempts}")
    pool.sort(
        key=lambda pair: (
            cjk_count(str(pair[1].get("text") or "")),
            min(100, len(str(pair[1].get("text") or ""))),
        ),
        reverse=True,
    )
    source, comment = pool[0]
    diagnostics = {
        "query": query,
        "search_status": status,
        "video_ids_seen": video_ids[:8],
        "attempts": attempts,
        "selected_video_id": source["video_id"],
        "comment_pool_size": len(pool),
        "selected_cjk_chars": cjk_count(str(comment.get("text") or "")),
    }
    return comment, diagnostics


def main() -> int:
    report: dict[str, Any] = {
        "probe": "real-tiktok-footage-youtube-comment-mix-v3",
        "tag": TAG,
        "policy": "minimum duration floor + complete last clip + video context then direct comment",
        "requested_minimum_seconds": MINIMUM_DURATION,
        "stages": {},
    }
    try:
        here = Path(__file__).resolve().parent

        tik_out = ROOT / "tiktok"
        os.environ["PROBE_OUT"] = str(tik_out)
        os.environ["PROBE_TAG"] = TAG
        os.environ["PROBE_LIMIT"] = str(LIMIT)
        os.environ["PROBE_DOWNLOAD_COUNT"] = "2"
        tik = load_module("real_mix_tiktok_e2e", here / "tiktok_e2e_probe_v2.py")
        tik_report = tik.run()
        report["stages"]["tiktok"] = {
            "candidate_count": tik_report.get("candidate_count"),
            "pages": tik_report.get("pages"),
            "selected": tik_report.get("selected"),
            "successful_downloads": tik_report.get("successful_downloads"),
            "downloads": tik_report.get("downloads"),
        }
        if int(tik_report.get("successful_downloads") or 0) != 2:
            raise RuntimeError("TikTok discovery/download did not produce two complete MP4 finalists")

        selected = list(tik_report["selected"])
        paths = [tik_out / "media" / f"{row['id']}.mp4" for row in selected[:2]]
        if not all(path.exists() and path.stat().st_size > 10_000 for path in paths):
            raise RuntimeError(f"downloaded TikTok paths missing: {paths}")

        yt_out = ROOT / "youtube"
        os.environ["PROBE_OUT"] = str(yt_out)
        os.environ["PROBE_QUERY"] = f"{TAG} shorts"
        yt = load_module("real_mix_youtube_comments", here / "youtube_comments_probe.py")
        comment, comment_diag = choose_youtube_comment(yt)
        report["stages"]["comment"] = {
            "platform": "youtube",
            "comment_id": comment.get("id"),
            "text": comment.get("text"),
            **comment_diag,
        }

        os.environ["PROBE_OUT"] = str(ROOT)
        mix = load_module("real_mix_helpers", here / "real_mix_e2e_probe.py")
        audio = [mix.audio_signal(path) for path in paths]
        report["stages"]["audio"] = [
            {"video_id": selected[index]["id"], "path": str(path), **audio[index]}
            for index, path in enumerate(paths)
        ]

        context = clean_context(str(selected[0].get("description") or ""))
        direct_comment = re.sub(r"\s+", " ", str(comment.get("text") or "")).strip()
        direct_comment = direct_comment[:90].rstrip()
        tts_text = f"{context}。{direct_comment}" if context else direct_comment
        tts_path = ROOT / "comment.mp3"
        subprocess.run(
            [
                "python", "-m", "edge_tts",
                "--voice", "zh-CN-XiaoxiaoNeural",
                "--text", tts_text,
                "--write-media", str(tts_path),
            ],
            check=True,
            timeout=120,
        )
        tts_info = mix.ffprobe(tts_path)
        tts_duration = float(tts_info["format"]["duration"])
        tts_slot = min(5.5, max(2.5, tts_duration + 0.25))
        report["stages"]["tts"] = {
            "voice": "zh-CN-XiaoxiaoNeural",
            "text": tts_text,
            "comment_prefix": "none",
            "bytes": tts_path.stat().st_size,
            "duration": tts_duration,
        }

        source_infos = [mix.ffprobe(path) for path in paths]
        source_durations = [float(info["format"]["duration"]) for info in source_infos]
        # Complete natural test beats. No duration is shortened because only a few
        # seconds remain before the requested minimum.
        clip_durations = [min(5.0, max(2.5, value - 0.15)) for value in source_durations]
        seg1, seg2, seg3 = ROOT / "seg1.mp4", ROOT / "seg2.mp4", ROOT / "seg3.mp4"
        aligned = [
            mix.make_segment(
                paths[0], seg1, start=0.10, duration=clip_durations[0],
                keep_audio=bool(audio[0].get("signal_detected")),
            ),
            mix.make_tts_segment(
                paths[0], seg2, at=max(0.1, clip_durations[0] - 0.1),
                tts=tts_path, duration=tts_slot,
            ),
            mix.make_segment(
                paths[1], seg3, start=0.10, duration=clip_durations[1],
                keep_audio=bool(audio[1].get("signal_detected")),
            ),
        ]

        listing = ROOT / "mix.ffconcat"
        lines = ["ffconcat version 1.0"]
        for path, segment_duration in zip((seg1, seg2, seg3), aligned):
            lines.append(f"file '{path.resolve().as_posix()}'")
            lines.append(f"duration {segment_duration:.9f}")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output = ROOT / "real_mix.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-movflags", "+faststart",
                "-video_track_timescale", str(FPS * 1000), str(output),
            ],
            check=True,
            timeout=120,
        )

        final = mix.ffprobe(output, count_frames=True)
        streams = final.get("streams") or []
        video = next(row for row in streams if row.get("codec_type") == "video")
        audio_stream = next(row for row in streams if row.get("codec_type") == "audio")
        avg = rate(video.get("avg_frame_rate"))
        frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
        duration = float(final["format"]["duration"])
        expected_duration = sum(aligned)
        expected_frames = round(expected_duration * FPS)
        assertions = {
            "real_tiktok_candidates": int(tik_report.get("candidate_count") or 0) >= 20,
            "two_real_tiktok_mp4s": len(paths) == 2 and all(path.stat().st_size > 10_000 for path in paths),
            "real_youtube_comment": bool(comment.get("id") and comment.get("text")),
            "real_audio_inspection": len(audio) == 2 and all("has_audio_stream" in item for item in audio),
            "direct_comment_no_wrapper": "网友评论" not in tts_text and "网友表示" not in tts_text,
            "chinese_tts_generated": tts_path.stat().st_size > 1000,
            "h264": video.get("codec_name") == "h264",
            "aac": audio_stream.get("codec_name") == "aac",
            "portrait": int(video.get("width") or 0) == W and int(video.get("height") or 0) == H,
            "true_24fps": 23.8 <= avg <= 24.2,
            "frame_count": abs(frames - expected_frames) <= 1,
            "duration_matches_complete_segments": abs(duration - expected_duration) <= 0.16,
            "minimum_duration_floor": duration + 0.02 >= MINIMUM_DURATION,
            "last_segment_complete": abs(aligned[-1] - clip_durations[1]) <= (1.0 / FPS + 0.02),
        }
        report["stages"]["render"] = {
            "output": str(output),
            "bytes": output.stat().st_size,
            "requested_minimum_seconds": MINIMUM_DURATION,
            "aligned_segment_durations": aligned,
            "expected_duration": expected_duration,
            "duration": duration,
            "overrun_seconds": max(0.0, duration - MINIMUM_DURATION),
            "last_segment_requested": clip_durations[1],
            "last_segment_rendered": aligned[-1],
            "avg_fps": avg,
            "frames": frames,
            "expected_frames": expected_frames,
            "video": video,
            "audio": audio_stream,
        }
        report["assertions"] = assertions
        report["ok"] = all(assertions.values())
    except Exception as exc:
        report["ok"] = False
        report["error"] = repr(exc)

    (ROOT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REAL_MIX_CROSSSOURCE=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
