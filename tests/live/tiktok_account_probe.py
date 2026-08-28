from __future__ import annotations

import json
import os
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


def item_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def normalize(item: dict[str, Any]) -> dict[str, Any] | None:
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
        "play_count": stats.get("playCount") or stats.get("play_count"),
        "like_count": stats.get("diggCount") or stats.get("digg_count"),
        "create_time": item.get("createTime") or item.get("create_time"),
    }


def run() -> dict[str, Any]:
    candidates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pages: list[dict[str, Any]] = []
    errors: list[str] = []

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
            if "/api/post/item_list/" not in response.url or response.status != 200:
                return
            try:
                data = response.json()
            except Exception as exc:
                errors.append(repr(exc))
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            rows = item_list(data)
            before = len(candidates)
            for raw in rows:
                row = normalize(raw)
                if row and row["id"] not in candidates:
                    candidates[row["id"]] = row
            pages.append(
                {
                    "cursor_requested": (q.get("cursor") or [None])[0],
                    "cursor_returned": data.get("cursor"),
                    "has_more": data.get("hasMore"),
                    "items_received": len(rows),
                    "new_items": len(candidates) - before,
                    "total_unique": len(candidates),
                }
            )

        page.on("response", on_response)
        try:
            page.goto(f"https://www.tiktok.com/@{urllib.parse.quote(ACCOUNT)}", wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            errors.append("navigation: " + repr(exc))
        time.sleep(5)
        title = page.title()

        stale = 0
        last_count = len(candidates)
        for _ in range(25):
            if len(candidates) >= LIMIT:
                break
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                page.mouse.wheel(0, 15000)
            time.sleep(1.8)
            current = len(candidates)
            stale = stale + 1 if current <= last_count else 0
            last_count = current
            if stale >= 6:
                break

        final_url = page.url
        context.close()
        browser.close()

    rows = list(candidates.values())[:LIMIT]
    return {
        "account": ACCOUNT,
        "title": title,
        "final_url": final_url,
        "candidate_count": len(rows),
        "pages": pages,
        "errors": errors,
        "top_videos": sorted(rows, key=lambda x: x.get("play_count") or 0, reverse=True)[:8],
    }


def main() -> int:
    report = run()
    (OUT / "account_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TIKTOK_ACCOUNT_PROBE=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["candidate_count"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
