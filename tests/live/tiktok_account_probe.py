from __future__ import annotations

import json
import os
import random
import re
import string
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT = Path(os.getenv("PROBE_OUT", "account_artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
ACCOUNT = os.getenv("PROBE_ACCOUNT", "gdgdryyds").strip().lstrip("@") or "gdgdryyds"
LIMIT = max(1, int(os.getenv("PROBE_LIMIT", "100")))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
VIDEO_RE = re.compile(r"/@([^/]+)/video/(\d+)")


def item_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def normalize(item: dict[str, Any], source: str) -> dict[str, Any] | None:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    video_id = str(item.get("id") or item.get("aweme_id") or "").strip()
    uid = author.get("uniqueId") or author.get("unique_id") or ACCOUNT
    if not video_id:
        return None
    return {
        "id": video_id,
        "url": f"https://www.tiktok.com/@{uid}/video/{video_id}",
        "author_id": uid,
        "description": item.get("desc") or item.get("description"),
        "play_count": stats.get("playCount") or stats.get("play_count"),
        "like_count": stats.get("diggCount") or stats.get("digg_count"),
        "comment_count": stats.get("commentCount") or stats.get("comment_count"),
        "share_count": stats.get("shareCount") or stats.get("share_count"),
        "create_time": item.get("createTime") or item.get("create_time"),
        "source": source,
    }


def safe_prefix(raw: bytes, limit: int = 350) -> str:
    return raw[:limit].decode("utf-8", errors="replace").replace("\n", "\\n")


def find_user_detail(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    default_scope = data.get("__DEFAULT_SCOPE__")
    if isinstance(default_scope, dict):
        detail = default_scope.get("webapp.user-detail")
        if isinstance(detail, dict):
            return detail
    return {}


def creator_query(*, sec_uid: str, cursor: int, device_id: str) -> dict[str, str]:
    verify_fp = "verify_" + "".join(random.choices(string.hexdigits, k=7))
    return {
        "aid": "1988",
        "app_language": "en",
        "app_name": "tiktok_web",
        "browser_language": "en-US",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Linux x86_64",
        "browser_version": "5.0 (X11; Linux x86_64)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "count": "15",
        "cursor": str(cursor),
        "device_id": device_id,
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "user",
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "en",
        "os": "linux",
        "priority_region": "",
        "referer": "",
        "region": "US",
        "screen_height": "1000",
        "screen_width": "1440",
        "secUid": sec_uid,
        "type": "1",
        "tz_name": "UTC",
        "verifyFp": verify_fp,
        "webcast_language": "en",
    }


def run() -> dict[str, Any]:
    candidates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    post_api_diagnostics: list[dict[str, Any]] = []
    creator_pages: list[dict[str, Any]] = []
    errors: list[str] = []
    dom_video_urls: list[str] = []
    user_detail_summary: dict[str, Any] = {}

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

        def on_response(response: Response) -> None:
            if "/api/post/item_list/" not in response.url:
                return
            diag: dict[str, Any] = {
                "status": response.status,
                "content_type": response.headers.get("content-type"),
                "content_length_header": response.headers.get("content-length"),
            }
            try:
                raw = response.body()
                diag["body_bytes"] = len(raw)
                diag["body_prefix"] = safe_prefix(raw)
            except Exception as exc:
                diag["body_error"] = repr(exc)
            post_api_diagnostics.append(diag)

        page.on("response", on_response)
        profile_url = f"https://www.tiktok.com/@{urllib.parse.quote(ACCOUNT)}"
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            errors.append("navigation: " + repr(exc))
        time.sleep(5)
        title = page.title()

        # Extract stable account identifiers from the large hydration object. This
        # payload is available even when post/item_list is deliberately empty.
        sec_uid: str | None = None
        device_id: str | None = None
        try:
            hydration_text = page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content() or ""
            hydration = json.loads(hydration_text) if hydration_text else {}
            detail = find_user_detail(hydration)
            user_info = detail.get("userInfo") if isinstance(detail, dict) else {}
            user = user_info.get("user") if isinstance(user_info, dict) else {}
            stats = user_info.get("statsV2") or user_info.get("stats") or {} if isinstance(user_info, dict) else {}
            sec_uid = user.get("secUid") if isinstance(user, dict) else None
            user_detail_summary = {
                "statusCode": detail.get("statusCode") if isinstance(detail, dict) else None,
                "uniqueId": user.get("uniqueId") if isinstance(user, dict) else None,
                "secUid": sec_uid,
                "videoCount": stats.get("videoCount") if isinstance(stats, dict) else None,
                "itemList_count": len(user_info.get("itemList") or []) if isinstance(user_info, dict) else 0,
            }
            default_scope = hydration.get("__DEFAULT_SCOPE__") if isinstance(hydration, dict) else {}
            app_context = default_scope.get("webapp.app-context") if isinstance(default_scope, dict) else {}
            device_id = str(app_context.get("wid") or "") if isinstance(app_context, dict) else None
        except Exception as exc:
            errors.append("hydration: " + repr(exc))

        # TikTok's current profile webpage calls post/item_list but on public cloud
        # runners it can return HTTP 200 with Content-Length: 0. The current yt-dlp
        # extractor instead uses /api/creator/item_list/ with secUid and a timestamp
        # cursor. Test that route directly in the same browser context/session.
        if sec_uid:
            if not device_id:
                device_id = str(random.randint(10**18, 10**19 - 1))
            cursor = int(time.time() * 1000)
            seen_batches: set[tuple[str, ...]] = set()
            for page_number in range(1, 9):
                if len(candidates) >= LIMIT:
                    break
                query = creator_query(sec_uid=sec_uid, cursor=cursor, device_id=device_id)
                try:
                    response = context.request.get(
                        "https://www.tiktok.com/api/creator/item_list/",
                        params=query,
                        headers={"Referer": profile_url, "User-Agent": UA, "Accept": "application/json"},
                        timeout=30_000,
                    )
                    raw = response.body()
                    diag: dict[str, Any] = {
                        "page": page_number,
                        "cursor_requested": cursor,
                        "status": response.status,
                        "content_type": response.headers.get("content-type"),
                        "content_length_header": response.headers.get("content-length"),
                        "body_bytes": len(raw),
                        "body_prefix": safe_prefix(raw),
                    }
                    if not raw:
                        creator_pages.append(diag)
                        break
                    data = json.loads(raw.decode("utf-8-sig"))
                    rows = item_list(data)
                    ids = tuple(sorted(str(x.get("id") or x.get("aweme_id") or "") for x in rows if isinstance(x, dict)))
                    if ids and ids in seen_batches:
                        diag["repeated_batch"] = True
                        creator_pages.append(diag)
                        break
                    if ids:
                        seen_batches.add(ids)
                    before = len(candidates)
                    for raw_item in rows:
                        row = normalize(raw_item, "creator_api")
                        if row and row["id"] not in candidates:
                            candidates[row["id"]] = row
                    diag.update(
                        {
                            "items_received": len(rows),
                            "new_items": len(candidates) - before,
                            "total_unique": len(candidates),
                            "hasMorePrevious": data.get("hasMorePrevious"),
                        }
                    )
                    creator_pages.append(diag)
                    old_cursor = cursor
                    if rows:
                        last_time = rows[-1].get("createTime") or rows[-1].get("create_time")
                        if last_time:
                            cursor = int(float(last_time) * 1000)
                    if cursor == old_cursor:
                        cursor = old_cursor - 7 * 86_400_000
                    if not data.get("hasMorePrevious"):
                        break
                except Exception as exc:
                    errors.append(f"creator_api_page_{page_number}: {exc!r}")
                    break

        # Last-resort DOM anchors for environments where TikTok actually renders the grid.
        try:
            hrefs = page.locator('a[href*="/video/"]').evaluate_all(
                "els => els.map(e => e.href || e.getAttribute('href')).filter(Boolean)"
            )
        except Exception as exc:
            errors.append("dom_anchors: " + repr(exc))
            hrefs = []
        for href in hrefs:
            if not isinstance(href, str):
                continue
            match = VIDEO_RE.search(urllib.parse.urlparse(href).path)
            if not match:
                continue
            uid, vid = match.groups()
            canonical = f"https://www.tiktok.com/@{uid}/video/{vid}"
            if canonical not in dom_video_urls:
                dom_video_urls.append(canonical)
            if vid not in candidates:
                candidates[vid] = {
                    "id": vid,
                    "url": canonical,
                    "author_id": uid,
                    "description": None,
                    "play_count": None,
                    "like_count": None,
                    "comment_count": None,
                    "share_count": None,
                    "create_time": None,
                    "source": "dom_anchor",
                }

        final_url = page.url
        context.close()
        browser.close()

    rows = list(candidates.values())[:LIMIT]
    source_counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    return {
        "probe_version": 3,
        "account": ACCOUNT,
        "title": title,
        "final_url": final_url,
        "user_detail": user_detail_summary,
        "candidate_count": len(rows),
        "candidate_source_counts": source_counts,
        "post_api_diagnostics": post_api_diagnostics[:6],
        "creator_pages": creator_pages,
        "dom_video_url_count": len(dom_video_urls),
        "errors": errors,
        "top_videos": sorted(rows, key=lambda x: x.get("play_count") or 0, reverse=True)[:8],
    }


def main() -> int:
    report = run()
    (OUT / "account_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TIKTOK_ACCOUNT_PROBE_V3=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["candidate_count"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
