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

ROOT = Path(os.getenv("PROBE_ROOT", "artifacts/sample_30s_mix"))
ROOT.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "猫咪").strip().lstrip("#") or "猫咪"
LIMIT = max(40, min(120, int(os.getenv("PROBE_LIMIT", "70"))))
MINIMUM = max(30.0, float(os.getenv("PROBE_MINIMUM", "30")))
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


def clean_context(text: str, limit: int = 22) -> str:
    raw = re.sub(r"https?://\S+", "", text or "")
    raw = re.sub(r"[#＃][^\s#＃]+", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" ，,。.!！?？;；:-")
    # Keep the public sample conservative and cheap: when the source caption is
    # Japanese/English/hashtag-only, do not hallucinate a specific action. The
    # production path can replace this fallback with Video Brain's visual summary.
    if cjk_count(raw) < 4:
        return f"{TAG}日常"
    if len(raw) > limit:
        raw = raw[:limit].rstrip(" ，,。.!！?？;；:-")
    return raw or f"{TAG}日常"


def choose_youtube_comments(yt: ModuleType, count: int = 2) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = f"{TAG} shorts 中文"
    search_url = "https://www.youtube.com/results?" + yt.urllib.parse.urlencode({"search_query": query})
    status, raw = yt.request(search_url)
    html = raw.decode("utf-8", errors="replace")
    video_ids: list[str] = []
    for video_id in yt.VIDEO_ID_RE.findall(html):
        if video_id not in video_ids:
            video_ids.append(video_id)

    attempts: list[dict[str, Any]] = []
    pool: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for video_id in video_ids[:12]:
        try:
            row = yt.fetch_comments(video_id)
        except Exception as exc:
            attempts.append({"video_id": video_id, "error": repr(exc)})
            continue
        comments = list(row.get("comments") or [])
        attempts.append({
            "video_id": video_id,
            "watch_status": row.get("watch_status"),
            "next_status": row.get("next_status"),
            "comments": len(comments),
        })
        for comment in comments:
            text = re.sub(r"\s+", " ", str(comment.get("text") or "")).strip()
            if 4 <= len(text) <= 72:
                pool.append((row, comment))
        chinese = [pair for pair in pool if cjk_count(str(pair[1].get("text") or "")) >= 4]
        if len(chinese) >= max(6, count):
            break

    if not pool:
        raise RuntimeError(f"YouTube comment pool was empty; attempts={attempts}")

    # For a short Mix, prefer comments that are actually speakable in a few
    # seconds. This is a deterministic resource/time decision, not an LLM call.
    pool.sort(
        key=lambda pair: (
            cjk_count(str(pair[1].get("text") or "")) >= 4,
            6 <= cjk_count(str(pair[1].get("text") or "")) <= 16,
            -abs(cjk_count(str(pair[1].get("text") or "")) - 11),
            -abs(len(str(pair[1].get("text") or "")) - 18),
        ),
        reverse=True,
    )
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()
    chosen_texts: set[str] = set()
    selected_video_ids: list[str] = []
    for source, comment in pool:
        cid = str(comment.get("id") or "")
        text = re.sub(r"\s+", " ", str(comment.get("text") or "")).strip()
        norm = re.sub(r"\W+", "", text).casefold()
        if not cid or cid in chosen_ids or not norm or norm in chosen_texts:
            continue
        chosen.append(comment)
        chosen_ids.add(cid)
        chosen_texts.add(norm)
        selected_video_ids.append(str(source.get("video_id") or ""))
        if len(chosen) >= count:
            break
    if len(chosen) < count:
        raise RuntimeError(f"only {len(chosen)} distinct comments available")
    return chosen, {
        "query": query,
        "search_status": status,
        "video_ids_seen": video_ids[:12],
        "attempts": attempts,
        "selected_video_ids": selected_video_ids,
        "comment_pool_size": len(pool),
        "selected_cjk_chars": [cjk_count(str(item.get("text") or "")) for item in chosen],
    }


def synthesize_tts(mix: ModuleType, *, index: int, context: str, comment: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    direct_comment = re.sub(r"\s+", " ", str(comment.get("text") or "")).strip()[:72]
    text = f"{context.rstrip('。.!！?？')}。{direct_comment}"
    path = ROOT / f"comment_{index}.mp3"
    subprocess.run(
        [
            "python", "-m", "edge_tts",
            "--voice", "zh-CN-XiaoxiaoNeural",
            "--text", text,
            "--write-media", str(path),
        ],
        check=True,
        timeout=120,
    )
    info = mix.ffprobe(path)
    duration = float(info["format"]["duration"])
    # Duration is a floor everywhere: the TTS slot is derived from the *actual*
    # synthesized file and is never capped below the speech length.
    slot = max(3.0, duration + 0.25)
    return path, {
        "voice": "zh-CN-XiaoxiaoNeural",
        "context": context,
        "direct_comment": direct_comment,
        "text": text,
        "comment_prefix": "none",
        "bytes": path.stat().st_size,
        "duration": duration,
        "slot": slot,
    }


def main() -> int:
    report: dict[str, Any] = {
        "probe": "sample-30s-mix-v3",
        "theme": TAG,
        "requested_minimum_seconds": MINIMUM,
        "policy": "minimum-duration floor; complete source clips; complete direct-comment TTS beats",
        "stages": {},
    }
    try:
        here = Path(__file__).resolve().parent

        tik_out = ROOT / "tiktok"
        os.environ["PROBE_OUT"] = str(tik_out)
        os.environ["PROBE_TAG"] = TAG
        os.environ["PROBE_LIMIT"] = str(LIMIT)
        os.environ["PROBE_DOWNLOAD_COUNT"] = "3"
        tik = load_module("sample_30s_tiktok", here / "tiktok_e2e_probe_v2.py")
        tik_report = tik.run()
        report["stages"]["tiktok"] = {
            "candidate_count": tik_report.get("candidate_count"),
            "pages": tik_report.get("pages"),
            "selected": tik_report.get("selected"),
            "successful_downloads": tik_report.get("successful_downloads"),
            "downloads": tik_report.get("downloads"),
        }
        if int(tik_report.get("successful_downloads") or 0) != 3:
            raise RuntimeError("TikTok did not produce three complete MP4 finalists")

        selected = list(tik_report["selected"])
        paths = [tik_out / "media" / f"{row['id']}.mp4" for row in selected[:3]]
        if not all(path.exists() and path.stat().st_size > 10_000 for path in paths):
            raise RuntimeError(f"downloaded TikTok paths missing: {paths}")

        yt_out = ROOT / "youtube"
        os.environ["PROBE_OUT"] = str(yt_out)
        os.environ["PROBE_QUERY"] = f"{TAG} shorts 中文"
        yt = load_module("sample_30s_youtube_comments", here / "youtube_comments_probe.py")
        comments, comment_diag = choose_youtube_comments(yt, count=2)
        report["stages"]["comments"] = {
            "platform": "youtube",
            "items": [{"comment_id": item.get("id"), "text": item.get("text")} for item in comments],
            **comment_diag,
        }

        os.environ["PROBE_OUT"] = str(ROOT)
        mix = load_module("sample_30s_helpers", here / "real_mix_e2e_probe.py")
        audio = [mix.audio_signal(path) for path in paths]
        report["stages"]["audio"] = [
            {"video_id": selected[index]["id"], "path": str(path), **audio[index]}
            for index, path in enumerate(paths)
        ]

        contexts = [clean_context(str(selected[i].get("description") or "")) for i in range(2)]
        tts1, tts1_meta = synthesize_tts(mix, index=1, context=contexts[0], comment=comments[0])
        tts2, tts2_meta = synthesize_tts(mix, index=2, context=contexts[1], comment=comments[1])
        report["stages"]["tts"] = [tts1_meta, tts2_meta]

        source_infos = [mix.ffprobe(path) for path in paths]
        source_durations = [float(info["format"]["duration"]) for info in source_infos]
        clip_durations = [max(2.0, min(9.5, value - 0.18)) for value in source_durations]

        segment_paths = [
            ROOT / "seg1.mp4", ROOT / "seg_tts1.mp4",
            ROOT / "seg2.mp4", ROOT / "seg_tts2.mp4", ROOT / "seg3.mp4",
        ]
        aligned = [
            mix.make_segment(
                paths[0], segment_paths[0], start=0.08, duration=clip_durations[0],
                keep_audio=bool(audio[0].get("signal_detected")),
            ),
            mix.make_tts_segment(
                paths[0], segment_paths[1], at=max(0.08, clip_durations[0] - 0.08),
                tts=tts1, duration=tts1_meta["slot"],
            ),
            mix.make_segment(
                paths[1], segment_paths[2], start=0.08, duration=clip_durations[1],
                keep_audio=bool(audio[1].get("signal_detected")),
            ),
            mix.make_tts_segment(
                paths[1], segment_paths[3], at=max(0.08, clip_durations[1] - 0.08),
                tts=tts2, duration=tts2_meta["slot"],
            ),
            mix.make_segment(
                paths[2], segment_paths[4], start=0.08, duration=clip_durations[2],
                keep_audio=bool(audio[2].get("signal_detected")),
            ),
        ]
        expected_duration = sum(aligned)
        if expected_duration < MINIMUM:
            raise RuntimeError(
                f"complete beats total only {expected_duration:.3f}s; source durations={source_durations}"
            )

        listing = ROOT / "mix.ffconcat"
        lines = ["ffconcat version 1.0"]
        for path, duration in zip(segment_paths, aligned):
            lines.append(f"file '{path.resolve().as_posix()}'")
            lines.append(f"duration {duration:.9f}")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output = ROOT / "sample_30s_mix.mp4"
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
        expected_frames = round(expected_duration * FPS)
        assertions = {
            "different_theme": TAG != "机器人",
            "real_tiktok_candidates": int(tik_report.get("candidate_count") or 0) >= 20,
            "three_real_tiktok_mp4s": len(paths) == 3 and all(path.stat().st_size > 10_000 for path in paths),
            "two_real_youtube_comments": len(comments) == 2 and all(item.get("id") and item.get("text") for item in comments),
            "direct_comments_no_wrapper": all("网友" not in meta["text"] and "有人说" not in meta["text"] for meta in (tts1_meta, tts2_meta)),
            "tts_beats_complete": aligned[1] >= tts1_meta["duration"] and aligned[3] >= tts2_meta["duration"],
            "real_audio_inspection": len(audio) == 3 and all("has_audio_stream" in item for item in audio),
            "tts_generated": tts1.stat().st_size > 1000 and tts2.stat().st_size > 1000,
            "h264": video.get("codec_name") == "h264",
            "aac": audio_stream.get("codec_name") == "aac",
            "portrait": int(video.get("width") or 0) == W and int(video.get("height") or 0) == H,
            "true_24fps": 23.8 <= avg <= 24.2,
            "frame_count": abs(frames - expected_frames) <= 1,
            "duration_matches_complete_segments": abs(duration - expected_duration) <= 0.16,
            "minimum_30s_floor": duration >= MINIMUM - 0.02,
            "last_segment_complete": abs(aligned[-1] - mix.frame_duration(clip_durations[-1])) <= 0.001,
        }
        report["stages"]["render"] = {
            "output": str(output),
            "bytes": output.stat().st_size,
            "requested_minimum_seconds": MINIMUM,
            "source_durations": source_durations,
            "clip_durations": clip_durations,
            "aligned_segment_durations": aligned,
            "expected_duration": expected_duration,
            "duration": duration,
            "overrun_seconds": duration - MINIMUM,
            "last_segment_requested": clip_durations[-1],
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
    print("SAMPLE_30S_MIX=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
