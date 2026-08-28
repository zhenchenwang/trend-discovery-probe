from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Response, sync_playwright

OUT_DIR = Path(os.getenv("PROBE_OUT", "probe_artifacts"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
QUERY = os.getenv("PROBE_QUERY", "humanoid robot").strip() or "humanoid robot"
TAG = os.getenv("PROBE_TAG", "humanoidrobot").strip().lstrip("#") or "humanoidrobot"
LIMIT = max(10, int(os.getenv("PROBE_LIMIT", "100")))
SAMPLE_VIDEOS = max(1, min(8, int(os.getenv("PROBE_SAMPLE_VIDEOS", "5"))))
KNOWN_VIDEO = os.getenv(
    "PROBE_KNOWN_VIDEO",
    "https://www.tiktok.com/@eduard.constantin63/video/7605238965226032406",
).strip()
KNOWN_USER = os.getenv("PROBE_KNOWN_USER", "eduard.constantin63").strip().lstrip("@")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump_json(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def canonical_video_url(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def extract_video_links(page: Page, limit: int = LIMIT) -> list[str]:
    try:
        hrefs = page.locator('a[href*="/video/"]').evaluate_all(
            "els => els.map(e => e.href || e.getAttribute('href')).filter(Boolean)"
        )
    except Exception:
        hrefs = []
    out: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        if not isinstance(href, str) or "/video/" not in href:
            continue
        clean = canonical_video_url(urllib.parse.urljoin("https://www.tiktok.com/", href))
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def page_state(page: Page) -> dict[str, Any]:
    title = ""
    text = ""
    try:
        title = page.title()
    except Exception:
        pass
    try:
        text = page.locator("body").inner_text(timeout=8_000)
    except Exception:
        pass
    low = text.lower()
    title_low = title.lower()
    return {
        "url": page.url,
        "title": title,
        "body_chars": len(text),
        "captcha": any(x in low for x in ("captcha", "verify to continue", "security verification")),
        "login_wall": title_low.startswith("log in") or "log in to search for popular content" in low,
        "access_error": any(x in low for x in ("access denied", "too many requests", "temporarily unavailable")),
    }


def capture(page: Page, label: str) -> dict[str, Any]:
    state = page_state(page)
    try:
        html = page.content()
        state["html_chars"] = len(html)
        (OUT_DIR / f"{label}.html").write_text(html, encoding="utf-8", errors="replace")
    except Exception as exc:
        state["html_error"] = repr(exc)
    try:
        text = page.locator("body").inner_text(timeout=8_000)
        (OUT_DIR / f"{label}.txt").write_text(text, encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        page.screenshot(path=str(OUT_DIR / f"{label}.png"), full_page=True)
    except Exception:
        pass
    state["video_links"] = extract_video_links(page)
    return state


class NetworkRecorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._seen: set[tuple[str, int]] = set()

    @staticmethod
    def interesting(url: str) -> bool:
        u = url.lower()
        return "tiktok.com" in u and any(
            token in u
            for token in (
                "/api/",
                "/aweme/",
                "item_list",
                "challenge",
                "search",
                "post/item",
                "user/post",
                "recommend",
            )
        )

    def handle(self, response: Response) -> None:
        url = response.url
        if not self.interesting(url):
            return
        key = (url, response.status)
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            request = response.request
            headers = response.headers
            row = {
                "status": response.status,
                "method": request.method,
                "url": url,
                "content_type": headers.get("content-type"),
                "resource_type": request.resource_type,
            }
            post_data = request.post_data
            if post_data:
                row["post_data_sample"] = post_data[:2000]
            self.rows.append(row)
        except Exception as exc:
            self.rows.append({"url": url, "status": response.status, "error": repr(exc)})

    def summary(self) -> dict[str, Any]:
        path_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        for row in self.rows:
            try:
                parsed = urllib.parse.urlsplit(row["url"])
                path_counts[f"{parsed.netloc}{parsed.path}"] += 1
            except Exception:
                pass
            status_counts[str(row.get("status"))] += 1
        return {
            "responses": len(self.rows),
            "top_paths": path_counts.most_common(30),
            "statuses": dict(status_counts),
        }


def hydration_json(page: Page) -> dict[str, Any] | None:
    try:
        raw = page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=10_000)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def find_item_struct(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    scope = data.get("__DEFAULT_SCOPE__") or {}
    video_detail = scope.get("webapp.video-detail") or {}
    item_info = video_detail.get("itemInfo") or {}
    item = item_info.get("itemStruct")
    return item if isinstance(item, dict) else None


def normalize_item(item: dict[str, Any], source_url: str) -> tuple[dict[str, Any], dict[str, str]]:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    video = item.get("video") or {}
    music = item.get("music") or {}
    challenges = item.get("challenges") or []
    hashtags = []
    for challenge in challenges:
        if isinstance(challenge, dict):
            title = challenge.get("title")
            if title and title not in hashtags:
                hashtags.append(title)

    play_addr = video.get("playAddr") if isinstance(video.get("playAddr"), str) else ""
    download_addr = video.get("downloadAddr") if isinstance(video.get("downloadAddr"), str) else ""
    bitrate_urls: list[str] = []
    for entry in video.get("bitrateInfo") or []:
        if not isinstance(entry, dict):
            continue
        play = entry.get("PlayAddr") or entry.get("playAddr") or {}
        url_list = play.get("UrlList") or play.get("urlList") or []
        for url in url_list:
            if isinstance(url, str) and url not in bitrate_urls:
                bitrate_urls.append(url)

    normalized = {
        "id": item.get("id"),
        "source_url": source_url,
        "description": item.get("desc"),
        "create_time": item.get("createTime"),
        "author": {
            "id": author.get("id"),
            "unique_id": author.get("uniqueId"),
            "nickname": author.get("nickname"),
        },
        "stats": {
            "plays": stats.get("playCount"),
            "likes": stats.get("diggCount"),
            "comments": stats.get("commentCount"),
            "shares": stats.get("shareCount"),
            "collects": stats.get("collectCount"),
        },
        "video": {
            "duration": video.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
            "cover": video.get("cover"),
            "has_play_addr": bool(play_addr),
            "has_download_addr": bool(download_addr),
            "bitrate_url_count": len(bitrate_urls),
        },
        "music": {
            "id": music.get("id"),
            "title": music.get("title"),
            "author_name": music.get("authorName"),
            "has_play_url": bool(music.get("playUrl")),
        },
        "hashtags": hashtags,
    }
    media = {
        "play_addr": play_addr,
        "download_addr": download_addr,
        "bitrate_addr": bitrate_urls[0] if bitrate_urls else "",
    }
    return normalized, media


def navigate(page: Page, url: str, wait: float = 2.5) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(wait)


def scroll_collect(page: Page, scrolls: int, limit: int) -> tuple[list[str], list[int]]:
    found: list[str] = []
    seen: set[str] = set()
    counts: list[int] = []
    for index in range(scrolls + 1):
        for url in extract_video_links(page, limit=limit):
            if url not in seen:
                seen.add(url)
                found.append(url)
        counts.append(len(found))
        if len(found) >= limit or index == scrolls:
            break
        page.mouse.wheel(0, 6500)
        time.sleep(1.8)
    return found[:limit], counts


def probe_media(url: str, referer: str) -> dict[str, Any]:
    if not url:
        return {"ok": False, "reason": "missing_url"}
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": referer,
            "Range": "bytes=0-1048575",
            "Accept": "*/*",
        },
    )
    result: dict[str, Any] = {"ok": False}
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            chunk = response.read(1024 * 1024)
            result.update(
                ok=bool(chunk),
                status=getattr(response, "status", None),
                bytes_read=len(chunk),
                content_type=response.headers.get("content-type"),
                content_range=response.headers.get("content-range"),
                content_length=response.headers.get("content-length"),
                final_host=urllib.parse.urlsplit(response.geturl()).netloc,
            )
            if chunk:
                (OUT_DIR / "media_probe.bin").write_bytes(chunk)
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def oembed(video_url: str) -> dict[str, Any]:
    endpoint = "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(video_url, safe="")
    req = urllib.request.Request(endpoint, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            data = json.loads(response.read(500_000).decode("utf-8", errors="replace"))
            return {
                "ok": True,
                "status": getattr(response, "status", None),
                "author_name": data.get("author_name"),
                "author_url": data.get("author_url"),
                "thumbnail_url": data.get("thumbnail_url"),
            }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def dns_probe() -> dict[str, Any]:
    try:
        rows = socket.getaddrinfo("www.tiktok.com", 443, proto=socket.IPPROTO_TCP)
        addresses = []
        for row in rows:
            address = row[4][0]
            if address not in addresses:
                addresses.append(address)
        return {"ok": True, "addresses": addresses[:10]}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def main() -> int:
    report: dict[str, Any] = {
        "probe": "tiktok-live-probe-v3",
        "started_at": now_iso(),
        "query": QUERY,
        "tag": TAG,
        "limit": LIMIT,
        "dns": dns_probe(),
        "oembed": oembed(KNOWN_VIDEO),
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1440, "height": 1000},
            user_agent=UA,
        )
        page = context.new_page()
        page.set_default_timeout(20_000)
        network = NetworkRecorder()
        page.on("response", network.handle)

        try:
            tag_url = f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}"
            navigate(page, tag_url, 4.0)
            tag_links, tag_counts = scroll_collect(page, scrolls=12, limit=LIMIT)
            tag_state = capture(page, "tag")
            tag_state.update({"scroll_counts": tag_counts, "collected_video_links": tag_links})
            report["tag"] = tag_state

            samples: list[dict[str, Any]] = []
            private_media: list[dict[str, str]] = []
            for index, video_url in enumerate(tag_links[:SAMPLE_VIDEOS]):
                row: dict[str, Any] = {"source_url": video_url}
                try:
                    navigate(page, video_url, 1.7)
                    data = hydration_json(page)
                    item = find_item_struct(data)
                    if item:
                        normalized, media = normalize_item(item, video_url)
                        row.update({"ok": True, "metadata": normalized})
                        private_media.append({"source_url": video_url, **media})
                    else:
                        row.update({"ok": False, "reason": "missing_item_struct", "page": page_state(page)})
                except Exception as exc:
                    row.update({"ok": False, "error": repr(exc)})
                samples.append(row)
                if index == 0:
                    capture(page, "sample_video_0")
            report["sample_videos"] = samples

            known: dict[str, Any] = {"source_url": KNOWN_VIDEO}
            try:
                navigate(page, KNOWN_VIDEO, 1.7)
                data = hydration_json(page)
                item = find_item_struct(data)
                if item:
                    normalized, media = normalize_item(item, KNOWN_VIDEO)
                    known.update({"ok": True, "metadata": normalized})
                    private_media.append({"source_url": KNOWN_VIDEO, **media})
                else:
                    known.update({"ok": False, "reason": "missing_item_struct", "page": page_state(page)})
            except Exception as exc:
                known.update({"ok": False, "error": repr(exc)})
            report["known_video"] = known

            search_url = "https://www.tiktok.com/search?q=" + urllib.parse.quote(QUERY)
            try:
                navigate(page, search_url, 4.0)
                search_links, search_counts = scroll_collect(page, scrolls=5, limit=LIMIT)
                search_state = capture(page, "search")
                search_state.update({"scroll_counts": search_counts, "collected_video_links": search_links})
                report["search"] = search_state
            except Exception as exc:
                report["search"] = {"error": repr(exc)}

            user_url = f"https://www.tiktok.com/@{KNOWN_USER}"
            try:
                navigate(page, user_url, 4.0)
                user_links, user_counts = scroll_collect(page, scrolls=5, limit=LIMIT)
                user_state = capture(page, "user")
                user_state.update({"scroll_counts": user_counts, "collected_video_links": user_links})
                report["user"] = user_state
            except Exception as exc:
                report["user"] = {"error": repr(exc)}

            first_media = next((m for m in private_media if m.get("play_addr")), None)
            if first_media:
                report["media_probe"] = probe_media(first_media["play_addr"], first_media["source_url"])
            else:
                report["media_probe"] = {"ok": False, "reason": "no_play_addr"}

            dump_json("media_urls.json", private_media)
            report["network_summary"] = network.summary()
            dump_json("network.json", network.rows)
        finally:
            context.close()
            browser.close()

    report["finished_at"] = now_iso()
    dump_json("probe_result.json", report)

    good_samples = [x for x in report.get("sample_videos", []) if x.get("ok")]
    hashtags: set[str] = set()
    authors: set[str] = set()
    for sample in good_samples:
        metadata = sample.get("metadata") or {}
        hashtags.update(metadata.get("hashtags") or [])
        author = (metadata.get("author") or {}).get("unique_id")
        if author:
            authors.add(author)

    summary = {
        "dns_ok": report.get("dns", {}).get("ok"),
        "oembed_ok": report.get("oembed", {}).get("ok"),
        "tag_video_links": len(report.get("tag", {}).get("collected_video_links") or []),
        "tag_scroll_counts": report.get("tag", {}).get("scroll_counts"),
        "hydration_samples_ok": len(good_samples),
        "hydration_samples_total": len(report.get("sample_videos") or []),
        "new_hashtags_from_samples": sorted(hashtags)[:30],
        "authors_from_samples": sorted(authors)[:20],
        "media_probe": report.get("media_probe"),
        "search_video_links": len(report.get("search", {}).get("collected_video_links") or []),
        "search_login_wall": report.get("search", {}).get("login_wall"),
        "user_video_links": len(report.get("user", {}).get("collected_video_links") or []),
        "network": report.get("network_summary"),
    }
    dump_json("summary.json", summary)
    print("TIKTOK_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
