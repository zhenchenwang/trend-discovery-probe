from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT = Path(os.getenv("PROBE_OUT", "artifacts/real_mix_e2e"))
MEDIA = OUT / "media"
OUT.mkdir(parents=True, exist_ok=True)
MEDIA.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#") or "机器人"
LIMIT = max(30, min(120, int(os.getenv("PROBE_LIMIT", "55"))))
FPS = 24
W, H = 360, 640
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)


def run(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=True, text=True, capture_output=True, timeout=timeout)


def ffprobe(path: Path, *, count_frames: bool = False) -> dict[str, Any]:
    args = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,size:stream=codec_name,codec_type,width,height,r_frame_rate,avg_frame_rate,time_base,sample_rate,channels,nb_frames,nb_read_frames,duration",
    ]
    if count_frames:
        args.append("-count_frames")
    args += ["-of", "json", str(path)]
    return json.loads(subprocess.check_output(args, text=True, timeout=60))


def rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    return float(Fraction(value))


def frame_duration(seconds: float) -> float:
    return max(1, round(max(0.0, seconds) * FPS)) / FPS


def items_from_payload(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    video = item.get("video") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    vid = str(item.get("id") or item.get("aweme_id") or "").strip()
    uid = str(author.get("uniqueId") or author.get("unique_id") or "").strip()
    if not vid or not uid:
        return None
    return {
        "id": vid,
        "author": uid,
        "url": f"https://www.tiktok.com/@{uid}/video/{vid}",
        "description": str(item.get("desc") or item.get("description") or ""),
        "play_count": int(stats.get("playCount") or stats.get("play_count") or 0),
        "like_count": int(stats.get("diggCount") or stats.get("digg_count") or 0),
        "comment_count": int(stats.get("commentCount") or stats.get("comment_count") or 0),
        "duration_hint": float(video.get("duration") or 0),
    }


def add_comments(target: list[dict[str, Any]], seen: set[str], data: Any) -> int:
    rows = data.get("comments") if isinstance(data, dict) else None
    if not isinstance(rows, list) and isinstance(data, dict):
        body = data.get("body")
        rows = body.get("comments") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return 0
    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("cid") or row.get("id") or "").strip()
        text = str(row.get("text") or "").strip()
        if not cid or not text or cid in seen:
            continue
        seen.add(cid)
        user = row.get("user") or {}
        target.append({
            "id": cid,
            "text": text,
            "likes": int(row.get("digg_count") or row.get("like_count") or 0),
            "replies": int(row.get("reply_comment_total") or row.get("reply_count") or 0),
            "author": user.get("unique_id") if isinstance(user, dict) else None,
        })
        added += 1
    return added


def hydration_media_urls(page) -> list[str]:
    raw = page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=12000)
    if not raw:
        return []
    data = json.loads(raw)
    found: dict[str, list[str]] = {key: [] for key in ("playAddr", "downloadAddr", "PlayAddr", "DownloadAddr")}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in found and isinstance(child, str) and child.startswith("http") and child not in found[key]:
                    found[key].append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    urls: list[str] = []
    for key in ("playAddr", "downloadAddr", "PlayAddr", "DownloadAddr"):
        for url in found[key]:
            if url not in urls:
                urls.append(url)
    return urls


def inspect_video(context, item: dict[str, Any]) -> dict[str, Any]:
    page = context.new_page()
    page.set_default_timeout(20000)
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    responses: list[dict[str, Any]] = []

    def on_response(resp: Response) -> None:
        if "/api/comment/" not in resp.url:
            return
        rec: dict[str, Any] = {"url": resp.url, "status": resp.status}
        try:
            body = resp.body()
            rec["bytes"] = len(body)
            if body:
                data = json.loads(body.decode("utf-8-sig"))
                rec["added"] = add_comments(comments, seen, data)
        except Exception as exc:
            rec["error"] = repr(exc)
        responses.append(rec)

    page.on("response", on_response)
    try:
        try:
            page.goto(item["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        time.sleep(3.5)
        urls = hydration_media_urls(page)
        for _ in range(3):
            try:
                page.evaluate("""
                () => {
                  for (const el of Array.from(document.querySelectorAll('div'))) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 250) {
                      el.scrollTop = el.scrollHeight;
                    }
                  }
                  window.scrollTo(0, document.body.scrollHeight);
                }
                """)
            except Exception:
                pass
            time.sleep(0.8)

        if not comments:
            try:
                response = context.request.get(
                    "https://www.tiktok.com/api/comment/list/",
                    params={
                        "aid": "1988", "aweme_id": item["id"], "count": "20",
                        "cursor": "0", "device_platform": "web_pc",
                    },
                    headers={"Referer": item["url"], "User-Agent": UA, "Accept": "application/json"},
                    timeout=30000,
                    fail_on_status_code=False,
                )
                body = response.body()
                if body:
                    add_comments(comments, seen, json.loads(body.decode("utf-8-sig")))
            except Exception:
                pass
        return {
            "item": item,
            "title": page.title(),
            "media_urls": urls,
            "comments": comments,
            "comment_responses": responses,
        }
    finally:
        page.close()


def parse_total(headers: dict[str, str], status: int, body_len: int) -> int | None:
    value = headers.get("content-range")
    if value:
        match = CONTENT_RANGE_RE.search(value)
        if match and match.group(3) != "*":
            return int(match.group(3))
    if status == 200:
        try:
            return int(headers.get("content-length") or body_len)
        except Exception:
            return body_len
    return None


def download_signed(context, inspected: dict[str, Any]) -> dict[str, Any]:
    item = inspected["item"]
    attempts: list[str] = []
    for media_url in inspected["media_urls"]:
        target = MEDIA / f"{item['id']}.mp4"
        part = target.with_suffix(".mp4.part")
        part.unlink(missing_ok=True)
        offset = 0
        total: int | None = None
        sha = hashlib.sha256()
        try:
            with part.open("wb") as handle:
                while total is None or offset < total:
                    end = offset + 2 * 1024 * 1024 - 1 if total is None else min(offset + 2 * 1024 * 1024 - 1, total - 1)
                    response = context.request.get(
                        media_url,
                        headers={
                            "Referer": item["url"], "User-Agent": UA,
                            "Range": f"bytes={offset}-{end}",
                            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                        },
                        timeout=45000, fail_on_status_code=False,
                    )
                    body = response.body()
                    if response.status not in (200, 206) or not body:
                        raise RuntimeError(f"HTTP {response.status} at {offset}")
                    if offset == 0 and not (
                        (response.headers.get("content-type") or "").lower().startswith("video/")
                        or b"ftyp" in body[:64]
                    ):
                        raise RuntimeError("response was not an MP4")
                    found_total = parse_total(response.headers, response.status, len(body))
                    if found_total is not None:
                        total = found_total
                    if response.status == 200:
                        handle.seek(0)
                        handle.truncate(0)
                        handle.write(body)
                        sha = hashlib.sha256(body)
                        offset = len(body)
                        total = offset
                        break
                    handle.write(body)
                    sha.update(body)
                    offset += len(body)
                    if total is None and len(body) < 2 * 1024 * 1024:
                        total = offset
                handle.flush()
                os.fsync(handle.fileno())
            if total is not None and part.stat().st_size != total:
                raise RuntimeError("download size mismatch")
            part.replace(target)
            return {
                "ok": True, "path": str(target), "bytes": target.stat().st_size,
                "sha256": sha.hexdigest(), "probe": ffprobe(target),
            }
        except Exception as exc:
            part.unlink(missing_ok=True)
            attempts.append(repr(exc))
    return {"ok": False, "attempts": attempts}


def audio_signal(path: Path) -> dict[str, Any]:
    info = ffprobe(path)
    streams = info.get("streams") or []
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    result: dict[str, Any] = {"has_audio_stream": audio is not None}
    if audio is None:
        return result
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(path),
            "-t", "8", "-vn", "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    text = proc.stderr or ""
    mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", text)
    peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", text)
    result.update({
        "mean_volume_db": float(mean.group(1)) if mean else None,
        "max_volume_db": float(peak.group(1)) if peak else None,
        "signal_detected": bool(peak and float(peak.group(1)) > -55.0),
    })
    return result


def safe_tts_text(text: str) -> str:
    value = re.sub(r"https?://\S+", "", text)
    value = re.sub(r"\s+", " ", value).strip()
    value = value[:72].strip()
    return "有网友评论：" + (value or "这个画面太有意思了。")


def make_segment(source: Path, target: Path, *, start: float, duration: float, keep_audio: bool) -> float:
    aligned = frame_duration(duration)
    vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS},format=yuv420p"
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start):.3f}", "-i", str(source),
    ]
    if not keep_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += [
        "-t", f"{aligned:.9f}", "-map", "0:v:0",
        "-map", "0:a:0" if keep_audio else "1:a:0",
        "-vf", vf,
        "-af", f"atrim=duration={aligned:.9f},aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-video_track_timescale", str(FPS * 1000),
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart", str(target),
    ]
    run(*args)
    return aligned


def make_tts_segment(background: Path, target: Path, *, at: float, tts: Path, duration: float) -> float:
    aligned = frame_duration(duration)
    frame = OUT / "tts_frame.jpg"
    vf_frame = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, at):.3f}", "-i", str(background),
        "-frames:v", "1", "-vf", vf_frame, "-q:v", "3", str(frame),
    )
    run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", str(FPS), "-i", str(frame), "-i", str(tts),
        "-t", f"{aligned:.9f}", "-map", "0:v:0", "-map", "1:a:0",
        "-vf", f"fps={FPS},format=yuv420p",
        "-af", f"apad=pad_dur={aligned:.9f},atrim=duration={aligned:.9f},aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-video_track_timescale", str(FPS * 1000),
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart", str(target),
    )
    return aligned


def main() -> int:
    report: dict[str, Any] = {"tag": TAG, "candidate_limit": LIMIT, "stages": {}}
    candidates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pages: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA)
        page = context.new_page()

        def on_discovery(resp: Response) -> None:
            if "/api/challenge/item_list/" not in resp.url or resp.status != 200:
                return
            try:
                data = resp.json()
            except Exception:
                return
            rows = items_from_payload(data)
            before = len(candidates)
            for raw in rows:
                item = normalize_item(raw)
                if item and item["id"] not in candidates:
                    candidates[item["id"]] = item
            query = urllib.parse.parse_qs(urllib.parse.urlparse(resp.url).query)
            pages.append({
                "cursor": (query.get("cursor") or [None])[0],
                "received": len(rows), "new": len(candidates) - before, "total": len(candidates),
            })

        page.on("response", on_discovery)
        try:
            page.goto(f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        time.sleep(4)
        for _ in range(10):
            if len(candidates) >= LIMIT:
                break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)
        page.close()

        report["stages"]["discovery"] = {"candidates": len(candidates), "pages": pages}
        ranked = sorted(candidates.values(), key=lambda row: (row["play_count"], row["comment_count"]), reverse=True)
        inspected: list[dict[str, Any]] = []
        for item in ranked[:8]:
            detail = inspect_video(context, item)
            if detail["media_urls"]:
                inspected.append(detail)
            if len(inspected) >= 2 and any(row["comments"] for row in inspected):
                break
        if len(inspected) < 2:
            raise RuntimeError(f"only {len(inspected)} downloadable candidates found")
        comment_source = next((row for row in inspected if row["comments"]), None)
        if comment_source is None:
            raise RuntimeError("no real TikTok comments were observed on inspected finalists")
        selected = [comment_source]
        selected += [row for row in inspected if row is not comment_source][:1]

        downloads = [download_signed(context, row) for row in selected]
        browser.close()

    if any(not row.get("ok") for row in downloads):
        raise RuntimeError(f"download failed: {downloads}")
    paths = [Path(str(row["path"])) for row in downloads]
    audio = [audio_signal(path) for path in paths]
    report["stages"]["finalists"] = [
        {
            "id": detail["item"]["id"], "url": detail["item"]["url"],
            "plays": detail["item"]["play_count"], "comments_observed": len(detail["comments"]),
            "download": downloads[index], "audio": audio[index],
        }
        for index, detail in enumerate(selected)
    ]

    comments = sorted(comment_source["comments"], key=lambda row: (row["likes"], row["replies"]), reverse=True)
    best = comments[0]
    tts_text = safe_tts_text(best["text"])
    tts = OUT / "comment.mp3"
    run("python", "-m", "edge_tts", "--voice", "zh-CN-XiaoxiaoNeural", "--text", tts_text, "--write-media", str(tts), timeout=120)
    tts_info = ffprobe(tts)
    tts_duration = float(tts_info["format"]["duration"])
    tts_slot = min(5.5, max(2.5, tts_duration + 0.25))
    report["stages"]["comment_tts"] = {
        "real_comment_id": best["id"], "likes": best["likes"],
        "source_video": comment_source["item"]["url"],
        "tts_text": tts_text, "tts_bytes": tts.stat().st_size, "tts_duration": tts_duration,
    }

    source_probes = [ffprobe(path) for path in paths]
    source_durations = [float(info["format"]["duration"]) for info in source_probes]
    clip1_dur = min(5.0, max(2.5, source_durations[0] - 0.15))
    clip2_dur = min(5.0, max(2.5, source_durations[1] - 0.15))
    seg1, seg2, seg3 = OUT / "seg1.mp4", OUT / "seg2.mp4", OUT / "seg3.mp4"
    aligned = [
        make_segment(paths[0], seg1, start=0.10, duration=clip1_dur, keep_audio=bool(audio[0].get("has_audio_stream"))),
        make_tts_segment(paths[0], seg2, at=max(0.1, clip1_dur - 0.1), tts=tts, duration=tts_slot),
        make_segment(paths[1], seg3, start=0.10, duration=clip2_dur, keep_audio=bool(audio[1].get("has_audio_stream"))),
    ]

    listing = OUT / "mix.ffconcat"
    lines = ["ffconcat version 1.0"]
    for path, duration in zip((seg1, seg2, seg3), aligned):
        lines.append(f"file '{path.resolve().as_posix()}'")
        lines.append(f"duration {duration:.9f}")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    mix = OUT / "real_mix.mp4"
    run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart",
        "-video_track_timescale", str(FPS * 1000), str(mix),
    )

    final = ffprobe(mix, count_frames=True)
    streams = final.get("streams") or []
    video = next(row for row in streams if row.get("codec_type") == "video")
    audio_stream = next(row for row in streams if row.get("codec_type") == "audio")
    final_duration = float(final["format"]["duration"])
    avg = rate(video.get("avg_frame_rate"))
    frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    expected_frames = round(sum(aligned) * FPS)
    assertions = {
        "discovery_candidates_ge_20": len(candidates) >= 20,
        "two_real_downloads": len(paths) == 2 and all(path.stat().st_size > 10000 for path in paths),
        "real_comment_observed": len(comment_source["comments"]) > 0,
        "tts_generated": tts.stat().st_size > 1000,
        "h264": video.get("codec_name") == "h264",
        "aac": audio_stream.get("codec_name") == "aac",
        "portrait": int(video.get("width") or 0) == W and int(video.get("height") or 0) == H,
        "true_24fps": 23.8 <= avg <= 24.2,
        "frame_count": abs(frames - expected_frames) <= 1,
        "duration": abs(final_duration - sum(aligned)) <= 0.16,
    }
    report["stages"]["render"] = {
        "mix_path": str(mix), "mix_bytes": mix.stat().st_size,
        "planned_aligned_duration": sum(aligned), "duration": final_duration,
        "avg_fps": avg, "frames": frames, "expected_frames": expected_frames,
        "video": video, "audio": audio_stream,
    }
    report["assertions"] = assertions
    report["ok"] = all(assertions.values())
    (OUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REAL_MIX_E2E=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
