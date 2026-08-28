from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Page, Response, TimeoutError as PlaywrightTimeoutError, sync_playwright

OUT_DIR = Path(os.getenv("PROBE_OUT", "probe_artifacts"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "humanoidrobot").strip().lstrip("#") or "humanoidrobot"
USER = os.getenv("PROBE_KNOWN_USER", "eduard.constantin63").strip().lstrip("@")
KNOWN_VIDEO = os.getenv(
    "PROBE_KNOWN_VIDEO",
    "https://www.tiktok.com/@eduard.constantin63/video/7605238965226032406",
).strip()
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    video_id = str(item.get("id") or item.get("aweme_id") or "")
    unique_id = author.get("uniqueId") or author.get("unique_id") or author.get("unique_id_str")
    url = None
    if video_id and unique_id:
        url = f"https://www.tiktok.com/@{unique_id}/video/{video_id}"
    return {
        "id": video_id or None,
        "url": url,
        "description": item.get("desc") or item.get("description"),
        "create_time": item.get("createTime") or item.get("create_time"),
        "author_unique_id": unique_id,
        "author_nickname": author.get("nickname"),
        "plays": stats.get("playCount") or stats.get("play_count"),
        "likes": stats.get("diggCount") or stats.get("digg_count"),
        "comments": stats.get("commentCount") or stats.get("comment_count"),
        "shares": stats.get("shareCount") or stats.get("share_count"),
    }


def summarize_json(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    result: dict[str, Any] = {"top_keys": list(data.keys())[:50]}
    for key in ("cursor", "hasMore", "has_more", "statusCode", "status_code", "statusMsg", "status_msg"):
        if key in data:
            result[key] = data.get(key)
    item_list = None
    item_key = None
    for key in ("itemList", "item_list", "aweme_list", "items"):
        if isinstance(data.get(key), list):
            item_list = data[key]
            item_key = key
            break
    if item_list is not None:
        result["item_key"] = item_key
        result["item_count"] = len(item_list)
        result["items"] = [compact_item(x) for x in item_list[:8] if isinstance(x, dict)]
    # Useful nested metadata from challenge detail/profile APIs.
    for key in ("challengeInfo", "challenge_info", "userInfo", "user_info"):
        if key in data:
            result[key] = data[key]
    return result


def capture_response(response: Response, label: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "url": response.url,
        "status": response.status,
        "content_type": response.headers.get("content-type"),
    }
    try:
        data = response.json()
        dump(f"api_{label}.json", data)
        out["json"] = summarize_json(data)
    except Exception as exc:
        out["json_error"] = repr(exc)
        try:
            text = response.text()
            (OUT_DIR / f"api_{label}.txt").write_text(text[:500_000], encoding="utf-8", errors="replace")
            out["text_chars"] = len(text)
        except Exception as exc2:
            out["text_error"] = repr(exc2)
    return out


def expect_on_navigation(page: Page, url: str, matcher: Callable[[Response], bool], label: str) -> dict[str, Any]:
    try:
        with page.expect_response(matcher, timeout=35_000) as info:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        response = info.value
        time.sleep(2)
        return capture_response(response, label)
    except Exception as exc:
        return {"error": repr(exc), "page_url": page.url}


def expect_on_scroll(page: Page, matcher: Callable[[Response], bool], label: str) -> dict[str, Any]:
    try:
        with page.expect_response(matcher, timeout=20_000) as info:
            page.mouse.wheel(0, 9000)
        response = info.value
        time.sleep(1)
        return capture_response(response, label)
    except PlaywrightTimeoutError as exc:
        return {"timeout": repr(exc)}
    except Exception as exc:
        return {"error": repr(exc)}


def endpoint(path: str) -> Callable[[Response], bool]:
    return lambda r: path in r.url and r.status == 200


def hydration(page: Page) -> dict[str, Any] | None:
    try:
        raw = page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=10_000)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def profile_summary(page: Page) -> dict[str, Any]:
    data = hydration(page) or {}
    scope = data.get("__DEFAULT_SCOPE__") or {}
    detail = scope.get("webapp.user-detail") or {}
    user_info = detail.get("userInfo") or {}
    user = user_info.get("user") or {}
    stats = user_info.get("stats") or {}
    return {
        "id": user.get("id"),
        "secUid": user.get("secUid"),
        "uniqueId": user.get("uniqueId"),
        "nickname": user.get("nickname"),
        "stats": stats,
    }


def run() -> dict[str, Any]:
    report: dict[str, Any] = {"probe": "tiktok-api-probe-v1", "started_at": now_iso(), "tag": TAG, "user": USER}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA)
        page = context.new_page()
        page.set_default_timeout(20_000)
        try:
            tag_url = f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}"
            report["challenge_detail"] = expect_on_navigation(page, tag_url, endpoint("/api/challenge/detail/"), "challenge_detail")
            # Detail and item list can race on first navigation, so reload while waiting specifically for item list.
            report["challenge_items_0"] = expect_on_navigation(page, tag_url, endpoint("/api/challenge/item_list/"), "challenge_items_0")
            report["challenge_items_next"] = expect_on_scroll(
                page,
                lambda r: "/api/challenge/item_list/" in r.url and "cursor=30" in r.url and r.status == 200,
                "challenge_items_next",
            )

            user_url = f"https://www.tiktok.com/@{USER}"
            report["user_posts_0"] = expect_on_navigation(page, user_url, endpoint("/api/post/item_list/"), "user_posts_0")
            report["profile_hydration"] = profile_summary(page)
            report["user_posts_next"] = expect_on_scroll(
                page,
                lambda r: "/api/post/item_list/" in r.url and "cursor=0" not in r.url and r.status == 200,
                "user_posts_next",
            )

            report["related_items_0"] = expect_on_navigation(page, KNOWN_VIDEO, endpoint("/api/related/item_list/"), "related_items_0")
        finally:
            context.close()
            browser.close()
    report["finished_at"] = now_iso()
    return report


def main() -> int:
    report = run()
    dump("api_probe_result.json", report)
    summary = {
        "challenge_detail_ok": "json" in report.get("challenge_detail", {}),
        "challenge_items_0": report.get("challenge_items_0", {}).get("json"),
        "challenge_items_next": report.get("challenge_items_next", {}).get("json"),
        "profile_hydration": report.get("profile_hydration"),
        "user_posts_0": report.get("user_posts_0", {}).get("json"),
        "user_posts_next": report.get("user_posts_next", {}).get("json"),
        "related_items_0": report.get("related_items_0", {}).get("json"),
    }
    dump("api_summary.json", summary)
    print("TIKTOK_API_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
