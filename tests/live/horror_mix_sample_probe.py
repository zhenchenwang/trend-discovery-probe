from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import subprocess
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(os.getenv("PROBE_ROOT", "artifacts/horror_mix_sample"))
ROOT.mkdir(parents=True, exist_ok=True)
MINIMUM = max(30.0, float(os.getenv("PROBE_MINIMUM", "30")))
FPS = 24
TAGS = [
    ("灵异", "zh", "这段画面记录了一处疑似异常，先看原片的完整处理。"),
    ("paranormal", "en", "这一段来自海外的灵异短片，重点看画面里反复强调的异常。"),
    ("心霊", "ja", "这一段来自日本的灵异短片，保留原作者的回放和停顿。"),
]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cjk_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def clean_chinese_context(description: str, fallback: str) -> str:
    raw = re.sub(r"https?://\S+", "", description or "")
    raw = re.sub(r"[#＃][^\s#＃]+", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" ，,。.!！?？;；:-")
    if cjk_count(raw) >= 6:
        return raw[:30].rstrip(" ，,。.!！?？;；:-") + "。"
    return fallback


def duration_from_raw(item: dict[str, Any]) -> float:
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    value = video.get("duration") or item.get("duration") or 0
    try:
        duration = float(value)
    except Exception:
        return 0.0
    if duration > 1000:
        duration /= 1000.0
    return max(0.0, duration)


def discover_tag(tik: ModuleType, context, tag: str, *, target: int = 45) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page = context.new_page()
    page.set_default_timeout(20000)
    found: OrderedDict[str, dict[str, Any]] = OrderedDict()
    api_responses = 0

    def on_response(response) -> None:
        nonlocal api_responses
        if "/api/challenge/item_list/" not in response.url or response.status != 200:
            return
        api_responses += 1
        try:
            data = response.json()
        except Exception:
            return
        for raw in tik.items_from_payload(data):
            row = tik.normalize_item(raw)
            if row is None:
                continue
            row["duration_hint"] = duration_from_raw(raw)
            row["tag"] = tag
            found.setdefault(row["id"], row)

    page.on("response", on_response)
    try:
        try:
            page.goto(
                f"https://www.tiktok.com/tag/{urllib.parse.quote(tag)}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception:
            pass
        page.wait_for_timeout(4500)
        for _ in range(6):
            if len(found) >= target:
                break
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(1400)
    finally:
        page.close()
    return list(found.values()), {"tag": tag, "api_responses": api_responses, "candidates": len(found)}


def choose_short_complete(rows: list[dict[str, Any]], used_authors: set[str]) -> list[dict[str, Any]]:
    def pool(lo: float, hi: float) -> list[dict[str, Any]]:
        return [row for row in rows if lo <= float(row.get("duration_hint") or 0) <= hi]

    candidates = pool(5.0, 14.0) or pool(3.0, 20.0) or [
        row for row in rows if float(row.get("duration_hint") or 0) > 0
    ] or rows
    candidates.sort(key=lambda row: int(row.get("play_count") or 0), reverse=True)
    fresh = [row for row in candidates if str(row.get("author_id") or "").casefold() not in used_authors]
    return fresh + [row for row in candidates if row not in fresh]


def freeze_hint(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-t", "20", "-an", "-vf", "fps=4,scale=96:-2:flags=fast_bilinear,format=gray",
            "-f", "framemd5", "-",
        ],
        capture_output=True, text=True, timeout=90, check=False,
    )
    hashes: list[str] = []
    for line in proc.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 6:
            hashes.append(parts[-1])
    longest = 0
    current = 0
    previous = None
    for value in hashes:
        if value == previous:
            current += 1
        else:
            current = 1
            previous = value
        longest = max(longest, current)
    return {
        "sample_fps": 4,
        "frames": len(hashes),
        "longest_identical_run_frames": longest,
        "possible_freeze_seconds": round(longest / 4.0, 3) if longest >= 3 else 0.0,
    }


def synthesize(index: int, mix: ModuleType, context: str, comment: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    direct = re.sub(r"\s+", " ", str(comment.get("text") or "")).strip()[:70]
    text = context.rstrip("。.!！?？") + "。" + direct
    path = ROOT / f"comment_{index}.mp3"
    subprocess.run(
        [
            "python", "-m", "edge_tts", "--voice", "zh-CN-XiaoxiaoNeural",
            "--text", text, "--write-media", str(path),
        ],
        check=True, timeout=120,
    )
    duration = float(mix.ffprobe(path)["format"]["duration"])
    return path, {
        "voice": "zh-CN-XiaoxiaoNeural",
        "text": text,
        "direct_comment": direct,
        "comment_prefix": "none",
        "duration": duration,
        "slot": max(3.0, duration + 0.25),
    }


def main() -> int:
    report: dict[str, Any] = {
        "probe": "multilingual-horror-mix-sample-v1",
        "theme": "恐怖 / 灵异 / paranormal / 心霊",
        "minimum_seconds": MINIMUM,
        "policy": {
            "source_languages": ["zh", "en", "ja"],
            "narration_language": "zh-CN",
            "clip_policy": "whole-clip preserve creator edits",
            "duration_policy": "minimum floor; never shorten last beat",
            "comments": "direct comment, no wrapper",
        },
        "stages": {},
    }
    here = Path(__file__).resolve().parent
    try:
        tik_root = ROOT / "tiktok"
        os.environ["PROBE_OUT"] = str(tik_root)
        tik = load_module("horror_tiktok_helpers", here / "tiktok_e2e_probe_v2.py")

        chosen: list[dict[str, Any]] = []
        downloads: list[dict[str, Any]] = []
        discovery: list[dict[str, Any]] = []
        used_authors: set[str] = set()
        with tik.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(
                locale="en-US", timezone_id="UTC", viewport={"width": 1280, "height": 900},
                user_agent=tik.UA,
            )
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )
            for tag, language, fallback in TAGS:
                rows, diag = discover_tag(tik, context, tag)
                diag["language"] = language
                discovery.append(diag)
                success = None
                selected_row = None
                for row in choose_short_complete(rows, used_authors)[:5]:
                    result = tik.full_download(context, row)
                    if result.get("ok"):
                        success = result
                        selected_row = dict(row)
                        selected_row["language"] = language
                        selected_row["context_fallback"] = fallback
                        break
                if success is None or selected_row is None:
                    raise RuntimeError(f"could not download a complete short finalist for #{tag}")
                used_authors.add(str(selected_row.get("author_id") or "").casefold())
                chosen.append(selected_row)
                downloads.append(success)
            context.close()
            browser.close()

        paths = [tik_root / "media" / f"{row['id']}.mp4" for row in chosen]
        if not all(path.exists() and path.stat().st_size > 10000 for path in paths):
            raise RuntimeError("one or more horror source MP4 files are missing")
        report["stages"]["discovery"] = discovery
        report["stages"]["selected"] = chosen
        report["stages"]["downloads"] = downloads

        os.environ["PROBE_OUT"] = str(ROOT)
        mix = load_module("horror_mix_helpers", here / "real_mix_e2e_probe.py")
        source_info = [mix.ffprobe(path) for path in paths]
        source_duration = [float(info["format"]["duration"]) for info in source_info]
        audio = [mix.audio_signal(path) for path in paths]
        edit_hints = [freeze_hint(path) for path in paths]
        report["stages"]["source_analysis"] = [
            {
                "video_id": chosen[i]["id"],
                "language": chosen[i]["language"],
                "source_duration": source_duration[i],
                "audio": audio[i],
                "edit_hint": edit_hints[i],
            }
            for i in range(3)
        ]

        os.environ["PROBE_TAG"] = "灵异"
        os.environ["PROBE_ROOT"] = str(ROOT)
        sample = load_module("horror_comment_helpers", here / "sample_30s_mix_probe.py")
        os.environ["PROBE_OUT"] = str(ROOT / "youtube")
        os.environ["PROBE_QUERY"] = "灵异 恐怖 paranormal shorts 中文"
        yt = load_module("horror_youtube_comments", here / "youtube_comments_probe.py")
        comments, comment_diag = sample.choose_youtube_comments(yt, count=2)
        report["stages"]["comments"] = {
            "items": [{"id": row.get("id"), "text": row.get("text")} for row in comments],
            **comment_diag,
        }

        contexts = [
            clean_chinese_context(str(chosen[i].get("description") or ""), TAGS[i][2])
            for i in range(2)
        ]
        tts = [synthesize(i + 1, mix, contexts[i], comments[i]) for i in range(2)]
        report["stages"]["tts"] = [meta for _, meta in tts]

        specs = [("clip", 0), ("tts", 0), ("clip", 1), ("tts", 1), ("clip", 2)]
        segments: list[Path] = []
        planned: list[float] = []
        rendered_clip_duration: dict[int, float] = {}
        for pos, (kind, index) in enumerate(specs, start=1):
            target = ROOT / f"seg_{pos}.mp4"
            if kind == "clip":
                requested = math.ceil(source_duration[index] * FPS - 1e-9) / FPS
                aligned = mix.make_segment(
                    paths[index], target, start=0.0, duration=requested,
                    keep_audio=bool(audio[index].get("signal_detected")),
                )
                actual = float(mix.ffprobe(target)["format"]["duration"])
                rendered_clip_duration[index] = actual
                planned.append(aligned)
            else:
                tts_path, meta = tts[index]
                aligned = mix.make_tts_segment(
                    paths[index], target,
                    at=max(0.0, source_duration[index] - 0.08),
                    tts=tts_path, duration=meta["slot"],
                )
                planned.append(aligned)
            segments.append(target)

        expected = sum(planned)
        if expected < MINIMUM:
            raise RuntimeError(f"complete horror beats total only {expected:.3f}s")

        listing = ROOT / "horror_mix.ffconcat"
        lines = ["ffconcat version 1.0"]
        for path, duration in zip(segments, planned):
            lines.append(f"file '{path.resolve().as_posix()}'")
            lines.append(f"duration {duration:.9f}")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

        output = ROOT / "horror_mix_sample.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c", "copy", "-movflags", "+faststart",
                "-video_track_timescale", str(FPS * 1000), str(output),
            ],
            check=True, timeout=120,
        )
        final = mix.ffprobe(output, count_frames=True)
        streams = final.get("streams") or []
        video = next(row for row in streams if row.get("codec_type") == "video")
        audio_stream = next(row for row in streams if row.get("codec_type") == "audio")
        final_duration = float(final["format"]["duration"])
        complete = [rendered_clip_duration[i] >= source_duration[i] - 0.10 for i in range(3)]
        assertions = {
            "three_languages": [row["language"] for row in chosen] == ["zh", "en", "ja"],
            "three_real_tiktok_mp4s": all(path.stat().st_size > 10000 for path in paths),
            "whole_clips_preserved": all(complete),
            "last_clip_complete": complete[-1],
            "minimum_floor": final_duration >= MINIMUM - 0.02,
            "chinese_tts": all(meta["voice"].startswith("zh-CN-") for _, meta in tts),
            "direct_comments": all("网友" not in meta["text"] and "有人说" not in meta["text"] for _, meta in tts),
            "h264": video.get("codec_name") == "h264",
            "aac": audio_stream.get("codec_name") == "aac",
            "portrait": int(video.get("width") or 0) == 360 and int(video.get("height") or 0) == 640,
        }
        report["stages"]["render"] = {
            "output": str(output),
            "duration_seconds": final_duration,
            "source_durations": source_duration,
            "rendered_clip_durations": [rendered_clip_duration[i] for i in range(3)],
            "expected_segment_sum": expected,
            "codec": video.get("codec_name"),
            "audio_codec": audio_stream.get("codec_name"),
            "assertions": assertions,
        }
        report["ok"] = all(assertions.values())
    except Exception as exc:
        report["ok"] = False
        report["error"] = repr(exc)

    (ROOT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("HORROR_MIX_SAMPLE=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
