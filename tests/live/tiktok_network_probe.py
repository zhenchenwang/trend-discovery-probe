from __future__ import annotations

import json
import os
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

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

WATCH = (
    "/api/challenge/detail/",
    "/api/challenge/item_list/",
    "/api/post/item_list/",
    "/api/related/item_list/",
    "/api/search/suggest/guide/",
    "/api/search/user/preview/",
    "/api/search/general/full/",
    "/api/search/item/full/",
    "/api/prefetch/explore/item_list/",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    video_id = str(item.get("id") or item.get("aweme_id") or "")
    uid = author.get("uniqueId") or author.get("unique_id")
    return {
        "id": video_id or None,
        "url": f"https://www.tiktok.com/@{uid}/video/{video_id}" if uid and video_id else None,
        "description": item.get("desc") or item.get("description"),
        "create_time": item.get("createTime") or item.get("create_time"),
        "author_unique_id": uid,
        "plays": stats.get("playCount") or stats.get("play_count"),
        "likes": stats.get("diggCount") or stats.get("digg_count"),
        "comments": stats.get("commentCount") or stats.get("comment_count"),
        "shares": stats.get("shareCount") or stats.get("share_count"),
    }


def summarize_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    out: dict[str, Any] = {"top_keys": list(data.keys())[:60]}
    for key in (
        "cursor", "hasMore", "has_more", "statusCode", "status_code", "status_msg",
        "search_id", "log_pb",
    ):
        if key in data:
            value = data.get(key)
            out[key] = value if key != "log_pb" else bool(value)

    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            out["item_key"] = key
            out["item_count"] = len(rows)
            out["items"] = [compact_item(x) for x in rows[:12] if isinstance(x, dict)]
            break
    return out


def endpoint_name(url: str) -> str | None:
    path = urllib.parse.urlparse(url).path
    for watched in WATCH:
        if watched in path:
            return watched.strip("/").replace("/", "_")
    return None


def query_snapshot(url: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    pairs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    important = (
        "challengeID", "challengeName", "cursor", "count", "secUid", "id", "itemID",
        "keyword", "search_id", "offset", "type", "from_page", "WebIdLastTime", "aid",
        "msToken", "X-Bogus",
    )
    out: dict[str, Any] = {}
    for key in important:
        if key in pairs:
            val = pairs[key][0] if pairs[key] else ""
            if key in ("msToken", "X-Bogus"):
                out[key] = f"present:{len(val)}"
            else:
                out[key] = val
    out["all_query_keys"] = sorted(pairs.keys())
    return out


def run() -> dict[str, Any]:
    report: dict[str, Any] = {
        "probe": "tiktok-network-probe-v1",
        "started_at": now_iso(),
        "tag": TAG,
        "user": USER,
        "known_video": KNOWN_VIDEO,
        "events": [],
    }
    events: list[dict[str, Any]] = report["events"]
    sequence = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA
        )
        page = context.new_page()
        page.set_default_timeout(20_000)

        def on_response(response: Response) -> None:
            nonlocal sequence
            name = endpoint_name(response.url)
            if not name:
                return
            sequence += 1
            event: dict[str, Any] = {
                "seq": sequence,
                "endpoint": name,
                "status": response.status,
                "url": response.url,
                "query": query_snapshot(response.url),
            }
            try:
                data = response.json()
                event["json"] = summarize_payload(data)
                dump(f"network_{sequence:03d}_{name}.json", data)
            except Exception as exc:
                event["json_error"] = repr(exc)
            events.append(event)

        page.on("response", on_response)

        def visit(url: str, wait: float = 5.0, scrolls: int = 0) -> None:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                report.setdefault("navigation_errors", []).append({"url": url, "error": repr(exc)})
            time.sleep(wait)
            for _ in range(scrolls):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    page.mouse.wheel(0, 12000)
                time.sleep(2.2)

        visit(f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}", wait=7, scrolls=8)
        report["tag_title"] = page.title()
        visit(f"https://www.tiktok.com/@{USER}", wait=7, scrolls=8)
        report["user_title"] = page.title()
        visit(KNOWN_VIDEO, wait=7, scrolls=4)
        report["video_title"] = page.title()
        visit("https://www.tiktok.com/search?q=" + urllib.parse.quote(TAG), wait=6, scrolls=3)
        report["search_title"] = page.title()

        context.close()
        browser.close()

    # Aggregate what matters to adapter design.
    by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        by_endpoint[ev["endpoint"]].append(ev)

    endpoint_summary: dict[str, Any] = {}
    unique_ids: set[str] = set()
    for name, rows in by_endpoint.items():
        cursors = []
        item_counts = []
        ids = []
        for row in rows:
            q = row.get("query", {})
            if "cursor" in q:
                cursors.append(q.get("cursor"))
            js = row.get("json", {})
            if isinstance(js.get("item_count"), int):
                item_counts.append(js["item_count"])
            for item in js.get("items", []) or []:
                if item.get("id"):
                    ids.append(item["id"])
                    unique_ids.add(item["id"])
        endpoint_summary[name] = {
            "response_count": len(rows),
            "statuses": dict(Counter(str(r.get("status")) for r in rows)),
            "cursors": cursors,
            "item_counts": item_counts,
            "sample_ids": ids[:20],
            "sample_query": rows[0].get("query") if rows else None,
        }

    report["endpoint_summary"] = endpoint_summary
    report["unique_sample_video_ids"] = len(unique_ids)
    report["finished_at"] = now_iso()
    return report


def main() -> int:
    report = run()
    dump("network_probe_result.json", report)
    summary = {
        "tag": report.get("tag"),
        "tag_title": report.get("tag_title"),
        "user_title": report.get("user_title"),
        "video_title": report.get("video_title"),
        "search_title": report.get("search_title"),
        "unique_sample_video_ids": report.get("unique_sample_video_ids"),
        "endpoint_summary": report.get("endpoint_summary"),
    }
    dump("network_summary.json", summary)
    print("TIKTOK_NETWORK_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
