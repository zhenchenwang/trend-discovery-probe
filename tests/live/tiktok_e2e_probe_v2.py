from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT_DIR = Path(os.getenv("PROBE_OUT", "e2e_artifacts"))
MEDIA_DIR = OUT_DIR / "media"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#") or "机器人"
CANDIDATE_LIMIT = max(20, int(os.getenv("PROBE_LIMIT", "60")))
DOWNLOAD_COUNT = max(1, min(3, int(os.getenv("PROBE_DOWNLOAD_COUNT", "2"))))
CHUNK_SIZE = 2 * 1024 * 1024
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def items_from_payload(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    video_id = str(item.get("id") or item.get("aweme_id") or "").strip()
    uid = author.get("uniqueId") or author.get("unique_id")
    if not video_id or not uid:
        return None
    return {
        "id": video_id,
        "url": f"https://www.tiktok.com/@{uid}/video/{video_id}",
        "author_id": uid,
        "description": item.get("desc") or item.get("description"),
        "play_count": stats.get("playCount") or stats.get("play_count") or 0,
        "like_count": stats.get("diggCount") or stats.get("digg_count") or 0,
    }


def extract_media_urls(page) -> list[str]:
    raw = page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=12000)
    if not raw:
        return []
    data = json.loads(raw)
    buckets = {"playAddr": [], "downloadAddr": [], "PlayAddr": [], "DownloadAddr": []}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in buckets and isinstance(child, str) and child.startswith("http"):
                    if child not in buckets[key]:
                        buckets[key].append(child)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    out: list[str] = []
    for key in ("playAddr", "downloadAddr", "PlayAddr", "DownloadAddr"):
        for url in buckets[key]:
            if url not in out:
                out.append(url)
    return out


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


def full_download(context, item: dict[str, Any]) -> dict[str, Any]:
    page = context.new_page()
    page.set_default_timeout(20000)
    title = ""
    try:
        try:
            page.goto(item["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        time.sleep(4)
        title = page.title()
        media_urls = extract_media_urls(page)
    finally:
        page.close()

    result: dict[str, Any] = {
        "id": item["id"], "url": item["url"], "title": title,
        "play_count": item["play_count"], "signed_url_count": len(media_urls), "attempts": [],
    }
    for media_url in media_urls:
        out_path = MEDIA_DIR / f"{item['id']}.mp4"
        part = out_path.with_suffix(".mp4.part")
        part.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        sha = hashlib.sha256()
        offset = 0
        total: int | None = None
        chunks = 0
        content_type: str | None = None
        try:
            with part.open("wb") as handle:
                while total is None or offset < total:
                    end = offset + CHUNK_SIZE - 1 if total is None else min(offset + CHUNK_SIZE - 1, total - 1)
                    response = context.request.get(
                        media_url,
                        headers={
                            "Referer": item["url"], "User-Agent": UA,
                            "Range": f"bytes={offset}-{end}",
                            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                        },
                        timeout=45000,
                        fail_on_status_code=False,
                    )
                    body = response.body()
                    content_type = content_type or response.headers.get("content-type")
                    if response.status not in (200, 206):
                        raise RuntimeError(f"HTTP {response.status} at offset {offset}")
                    if not body:
                        raise RuntimeError(f"empty response at offset {offset}")
                    if offset == 0 and not ((content_type or "").lower().startswith("video/") or b"ftyp" in body[:64]):
                        raise RuntimeError(f"not video media: {content_type!r}")
                    declared = parse_total(response.headers, response.status, len(body))
                    if declared is not None:
                        total = declared
                    if response.status == 200:
                        handle.seek(0)
                        handle.truncate(0)
                        handle.write(body)
                        sha = hashlib.sha256(body)
                        offset = len(body)
                        total = offset
                        chunks += 1
                        break
                    handle.write(body)
                    sha.update(body)
                    offset += len(body)
                    chunks += 1
                    if total is None and len(body) < CHUNK_SIZE:
                        total = offset
                handle.flush()
                os.fsync(handle.fileno())
            size = part.stat().st_size
            if total is not None and size != total:
                raise RuntimeError(f"size mismatch {size}!={total}")
            with part.open("rb") as handle:
                header = handle.read(64)
            if b"ftyp" not in header:
                raise RuntimeError("downloaded file has no MP4 ftyp signature")
            part.replace(out_path)
            result.update({
                "ok": True,
                "size_bytes": size,
                "declared_size": total,
                "sha256": sha.hexdigest(),
                "chunks": chunks,
                "content_type": content_type,
                "mp4_ftyp": True,
            })
            return result
        except Exception as exc:
            part.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
            result["attempts"].append({"error": repr(exc)})
    result["ok"] = False
    return result


def run() -> dict[str, Any]:
    candidates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pages: list[dict[str, Any]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA)
        page = context.new_page()
        page.set_default_timeout(20000)

        def on_response(response: Response) -> None:
            if "/api/challenge/item_list/" not in response.url or response.status != 200:
                return
            try:
                data = response.json()
            except Exception:
                return
            query = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            rows = items_from_payload(data)
            before = len(candidates)
            for raw in rows:
                row = normalize_item(raw)
                if row and row["id"] not in candidates:
                    candidates[row["id"]] = row
            pages.append({
                "cursor_requested": (query.get("cursor") or [None])[0],
                "cursor_returned": data.get("cursor"), "has_more": data.get("hasMore"),
                "items_received": len(rows), "new_items": len(candidates) - before,
                "total_unique": len(candidates),
            })

        page.on("response", on_response)
        try:
            page.goto(f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        time.sleep(5)
        for _ in range(12):
            if len(candidates) >= CANDIDATE_LIMIT:
                break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.8)
        page.close()

        rows = list(candidates.values())
        selected = sorted(rows, key=lambda x: x.get("play_count") or 0, reverse=True)[:DOWNLOAD_COUNT]
        downloads = [full_download(context, row) for row in selected]
        context.close()
        browser.close()

    return {
        "probe": "tiktok-e2e-discovery-download-v2",
        "tag": TAG,
        "candidate_count": len(candidates),
        "pages": pages,
        "selected": selected,
        "successful_downloads": sum(1 for x in downloads if x.get("ok")),
        "downloads": downloads,
        "finished_at": now_iso(),
    }


def main() -> int:
    report = run()
    dump("e2e_result.json", report)
    summary = {
        "tag": report["tag"], "candidate_count": report["candidate_count"],
        "pages": report["pages"], "selected": report["selected"],
        "successful_downloads": report["successful_downloads"],
        "downloads": [{k: x.get(k) for k in (
            "id", "ok", "size_bytes", "declared_size", "sha256", "chunks",
            "content_type", "mp4_ftyp", "attempts"
        )} for x in report["downloads"]],
    }
    dump("e2e_summary.json", summary)
    print("TIKTOK_E2E_V2_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0 if report["successful_downloads"] == len(report["downloads"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
