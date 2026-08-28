from __future__ import annotations

import json
import os
import random
import string
import time
import urllib.parse
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT = Path(os.getenv("PROBE_OUT", "seed_chain_artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#") or "机器人"
HASHTAG_LIMIT = max(30, int(os.getenv("PROBE_HASHTAG_LIMIT", "60")))
ACCOUNT_LIMIT = max(15, int(os.getenv("PROBE_ACCOUNT_LIMIT", "45")))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def item_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def video_row(item: dict[str, Any]) -> dict[str, Any] | None:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    vid = str(item.get("id") or item.get("aweme_id") or "")
    uid = author.get("uniqueId") or author.get("unique_id")
    if not vid or not uid:
        return None
    return {
        "id": vid,
        "author": str(uid),
        "description": item.get("desc") or item.get("description") or "",
        "play_count": stats.get("playCount") or stats.get("play_count"),
        "like_count": stats.get("diggCount") or stats.get("digg_count"),
        "create_time": item.get("createTime") or item.get("create_time"),
    }


def creator_query(sec_uid: str, cursor: int, device_id: str) -> dict[str, str]:
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
        "verifyFp": "verify_" + "".join(random.choices(string.hexdigits, k=7)),
        "webcast_language": "en",
    }


def run() -> dict[str, Any]:
    hashtag_rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
    hashtag_pages: list[dict[str, Any]] = []
    account_rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
    creator_pages: list[dict[str, Any]] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA
        )
        page = context.new_page()

        def on_response(response: Response) -> None:
            if "/api/challenge/item_list/" not in response.url or response.status != 200:
                return
            try:
                data = response.json()
            except Exception as exc:
                errors.append("hashtag_json: " + repr(exc))
                return
            rows = item_list(data)
            before = len(hashtag_rows)
            for item in rows:
                row = video_row(item)
                if row and row["id"] not in hashtag_rows:
                    hashtag_rows[row["id"]] = row
            q = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            hashtag_pages.append(
                {
                    "cursor": (q.get("cursor") or [None])[0],
                    "received": len(rows),
                    "new": len(hashtag_rows) - before,
                    "total": len(hashtag_rows),
                }
            )

        page.on("response", on_response)
        page.goto(
            f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        time.sleep(4)
        for _ in range(12):
            if len(hashtag_rows) >= HASHTAG_LIMIT:
                break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.5)

        author_counts = Counter(row["author"] for row in hashtag_rows.values())
        eligible = [(count, author) for author, count in author_counts.items() if count >= 2]
        eligible.sort(reverse=True)
        if not eligible:
            context.close(); browser.close()
            return {
                "ok": False, "tag": TAG, "hashtag_candidates": len(hashtag_rows),
                "error": "no account with >=2 hashtag evidences", "errors": errors,
            }
        evidence_count, selected_account = eligible[0]

        profile = context.new_page()
        profile_url = f"https://www.tiktok.com/@{urllib.parse.quote(selected_account)}"
        profile.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(4)
        hydration_text = profile.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content() or "{}"
        hydration = json.loads(hydration_text)
        scope = hydration.get("__DEFAULT_SCOPE__") or {}
        detail = scope.get("webapp.user-detail") or {}
        user_info = detail.get("userInfo") or {}
        user = user_info.get("user") or {}
        stats = user_info.get("statsV2") or user_info.get("stats") or {}
        app_context = scope.get("webapp.app-context") or {}
        sec_uid = user.get("secUid")
        device_id = str(app_context.get("wid") or random.randint(10**18, 10**19 - 1))
        if not sec_uid:
            raise RuntimeError("profile hydration did not contain secUid")

        cursor = int(time.time() * 1000)
        for page_no in range(1, 8):
            if len(account_rows) >= ACCOUNT_LIMIT:
                break
            resp = context.request.get(
                "https://www.tiktok.com/api/creator/item_list/",
                params=creator_query(sec_uid, cursor, device_id),
                headers={"Referer": profile_url, "User-Agent": UA, "Accept": "application/json"},
                timeout=30_000,
            )
            data = resp.json()
            rows = item_list(data)
            before = len(account_rows)
            for item in rows:
                row = video_row(item)
                if row and row["id"] not in account_rows:
                    account_rows[row["id"]] = row
            creator_pages.append(
                {
                    "page": page_no,
                    "received": len(rows),
                    "new": len(account_rows) - before,
                    "total": len(account_rows),
                    "has_more": bool(data.get("hasMorePrevious")),
                }
            )
            old_cursor = cursor
            if rows and rows[-1].get("createTime"):
                cursor = int(float(rows[-1]["createTime"]) * 1000)
            elif rows and rows[-1].get("create_time"):
                cursor = int(float(rows[-1]["create_time"]) * 1000)
            if cursor == old_cursor:
                cursor -= 7 * 86_400_000
            if not data.get("hasMorePrevious"):
                break

        profile.close()
        context.close()
        browser.close()

    hashtag_ids = set(hashtag_rows)
    account_ids = set(account_rows)
    account_only = account_ids - hashtag_ids
    return {
        "ok": bool(hashtag_rows and account_rows),
        "tag": TAG,
        "hashtag_candidates": len(hashtag_rows),
        "hashtag_pages": hashtag_pages,
        "selected_account": selected_account,
        "selected_account_evidence": evidence_count,
        "profile_video_count": stats.get("videoCount"),
        "account_candidates": len(account_rows),
        "creator_pages": creator_pages,
        "overlap_with_hashtag": len(account_ids & hashtag_ids),
        "new_account_history_candidates": len(account_only),
        "top_account_videos": sorted(
            account_rows.values(), key=lambda row: row.get("play_count") or 0, reverse=True
        )[:5],
        "errors": errors,
    }


def main() -> int:
    report = run()
    (OUT / "seed_chain_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("TIKTOK_SEED_CHAIN=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
