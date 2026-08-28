from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT_DIR = Path(os.getenv("PROBE_OUT", "probe_artifacts"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "humanoidrobot").strip().lstrip("#") or "humanoidrobot"
LIMIT = max(1, int(os.getenv("PROBE_LIMIT", "150")))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HASHTAG_RE = re.compile(r"#([^\s#]+)", re.UNICODE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def clean_hashtags(text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in HASHTAG_RE.findall(text):
        tag = raw.rstrip(".,!?;:，。！？；：)]}、").strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            out.append(tag)
    return out


def normalize_item(item: dict[str, Any], seed: str) -> dict[str, Any] | None:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    video = item.get("video") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    video_id = str(item.get("id") or item.get("aweme_id") or "").strip()
    uid = author.get("uniqueId") or author.get("unique_id")
    if not video_id or not uid:
        return None
    desc = item.get("desc") or item.get("description")
    return {
        "platform": "tiktok",
        "platform_video_id": video_id,
        "url": f"https://www.tiktok.com/@{uid}/video/{video_id}",
        "author_id": uid,
        "author_name": author.get("nickname"),
        "description": desc,
        "published_at": item.get("createTime") or item.get("create_time"),
        "play_count": stats.get("playCount") or stats.get("play_count"),
        "like_count": stats.get("diggCount") or stats.get("digg_count"),
        "comment_count": stats.get("commentCount") or stats.get("comment_count"),
        "share_count": stats.get("shareCount") or stats.get("share_count"),
        "hashtags": clean_hashtags(desc),
        "duration_ms": video.get("duration"),
        "width": video.get("width"),
        "height": video.get("height"),
        "discovery_method": "hashtag",
        "discovery_seed": seed,
    }


def item_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def run() -> dict[str, Any]:
    candidates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pages: list[dict[str, Any]] = []
    api_errors: list[str] = []
    started = now_iso()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        def on_response(response: Response) -> None:
            if "/api/challenge/item_list/" not in response.url or response.status != 200:
                return
            try:
                data = response.json()
            except Exception as exc:
                api_errors.append(repr(exc))
                return
            parsed = urllib.parse.urlparse(response.url)
            q = urllib.parse.parse_qs(parsed.query)
            rows = item_list(data)
            before = len(candidates)
            for raw in rows:
                row = normalize_item(raw, TAG)
                if row and row["platform_video_id"] not in candidates:
                    candidates[row["platform_video_id"]] = row
            pages.append({
                "cursor_requested": (q.get("cursor") or [None])[0],
                "cursor_returned": data.get("cursor"),
                "has_more": data.get("hasMore"),
                "items_received": len(rows),
                "new_items": len(candidates) - before,
                "total_unique": len(candidates),
            })

        page.on("response", on_response)
        url = f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        time.sleep(5)
        title = page.title()

        stale = 0
        last_count = len(candidates)
        for _ in range(30):
            if len(candidates) >= LIMIT:
                break
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                page.mouse.wheel(0, 15000)
            time.sleep(1.8)
            current = len(candidates)
            if current <= last_count:
                stale += 1
            else:
                stale = 0
            last_count = current
            if stale >= 6:
                break

        context.close()
        browser.close()

    rows = list(candidates.values())[:LIMIT]
    tag_evidence: dict[str, set[str]] = defaultdict(set)
    tag_display: dict[str, str] = {}
    account_evidence: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        vid = row["platform_video_id"]
        uid = row.get("author_id")
        if uid:
            account_evidence[uid].add(vid)
        for tag in row.get("hashtags") or []:
            key = tag.casefold()
            if key == TAG.casefold():
                continue
            tag_display.setdefault(key, tag)
            tag_evidence[key].add(vid)

    suggested_hashtags = sorted(
        (
            {"kind": "hashtag", "value": tag_display[k], "evidence_count": len(v), "example_video_ids": list(v)[:5]}
            for k, v in tag_evidence.items()
        ),
        key=lambda x: (-x["evidence_count"], x["value"].casefold()),
    )
    suggested_accounts = sorted(
        (
            {"kind": "account", "value": uid, "evidence_count": len(v), "example_video_ids": list(v)[:5]}
            for uid, v in account_evidence.items()
        ),
        key=lambda x: (-x["evidence_count"], x["value"].casefold()),
    )

    play_values = [x["play_count"] for x in rows if isinstance(x.get("play_count"), int)]
    total_plays = sum(play_values)
    top_videos = sorted(rows, key=lambda x: x.get("play_count") or 0, reverse=True)[:12]

    report = {
        "prototype": "tiktok-discovery-prototype-v1",
        "started_at": started,
        "finished_at": now_iso(),
        "seed": {"kind": "hashtag", "value": TAG},
        "page_title": title,
        "candidate_count": len(rows),
        "pages": pages,
        "api_errors": api_errors,
        "metrics": {
            "pages_observed": len(pages),
            "total_plays_across_candidates": total_plays,
            "unique_authors": len(account_evidence),
            "unique_cohashtags": len(tag_evidence),
        },
        "top_videos": top_videos,
        "suggested_hashtags": suggested_hashtags[:40],
        "suggested_accounts": suggested_accounts[:40],
        "candidates": rows,
    }
    return report


def main() -> int:
    report = run()
    dump("discovery_prototype_result.json", report)
    summary = {
        "seed": report["seed"],
        "page_title": report["page_title"],
        "candidate_count": report["candidate_count"],
        "pages": report["pages"],
        "metrics": report["metrics"],
        "top_suggested_hashtags": report["suggested_hashtags"][:15],
        "top_suggested_accounts": report["suggested_accounts"][:15],
        "top_videos": [
            {k: row.get(k) for k in ("platform_video_id", "url", "author_id", "play_count", "like_count", "description")}
            for row in report["top_videos"][:8]
        ],
    }
    dump("discovery_prototype_summary.json", summary)
    print("TIKTOK_DISCOVERY_PROTOTYPE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
