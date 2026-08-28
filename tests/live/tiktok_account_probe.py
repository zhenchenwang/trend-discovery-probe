from __future__ import annotations

import json
import os
import re
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
        "source": "post_api",
    }


def safe_prefix(raw: bytes, limit: int = 500) -> str:
    return raw[:limit].decode("utf-8", errors="replace").replace("\n", "\\n")


def walk_videoish(obj: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 14:
        return
    if isinstance(obj, dict):
        maybe = normalize(obj)
        if maybe is not None:
            maybe["source"] = "hydration"
            out.append(maybe)
        for value in obj.values():
            walk_videoish(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:1000]:
            walk_videoish(value, out, depth + 1)


def run() -> dict[str, Any]:
    candidates: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pages: list[dict[str, Any]] = []
    errors: list[str] = []
    response_diagnostics: list[dict[str, Any]] = []
    hydration_diagnostics: list[dict[str, Any]] = []
    dom_video_urls: list[str] = []
    script_inventory: list[dict[str, Any]] = []

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
            q = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            diag: dict[str, Any] = {
                "status": response.status,
                "url": response.url,
                "cursor": (q.get("cursor") or [None])[0],
                "content_type": response.headers.get("content-type"),
                "content_length_header": response.headers.get("content-length"),
                "content_encoding": response.headers.get("content-encoding"),
            }
            try:
                raw = response.body()
                diag["body_bytes"] = len(raw)
                diag["body_prefix"] = safe_prefix(raw)
            except Exception as exc:
                diag["body_error"] = repr(exc)
                raw = b""
            response_diagnostics.append(diag)
            if response.status != 200 or not raw:
                return
            try:
                data = json.loads(raw.decode("utf-8-sig"))
            except Exception as exc:
                errors.append("post_api_json: " + repr(exc))
                return
            rows = item_list(data)
            before = len(candidates)
            for raw_item in rows:
                row = normalize(raw_item)
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

        # DOM fallback: public profile pages often expose canonical video anchors even
        # when post/item_list returns an empty anti-bot response.
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
                    "create_time": None,
                    "source": "dom_anchor",
                }

        # Inspect JSON script payloads. Keep only compact diagnostics in the report,
        # but recursively look for item-like objects that already contain stats.
        try:
            scripts = page.locator("script").evaluate_all(
                "els => els.map(e => ({id:e.id||'', type:e.type||'', text:(e.textContent||'')}))"
            )
        except Exception as exc:
            errors.append("scripts: " + repr(exc))
            scripts = []
        hydration_rows: list[dict[str, Any]] = []
        for script in scripts:
            text = script.get("text") or ""
            sid = script.get("id") or ""
            stype = script.get("type") or ""
            script_inventory.append({
                "id": sid,
                "type": stype,
                "length": len(text),
                "prefix": text[:120].replace("\n", "\\n"),
            })
            if not text or text[0] not in "[{":
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            before = len(hydration_rows)
            walk_videoish(data, hydration_rows)
            if len(hydration_rows) > before:
                hydration_diagnostics.append(
                    {"id": sid, "type": stype, "length": len(text), "videoish_found": len(hydration_rows) - before}
                )
        for row in hydration_rows:
            if row["id"] not in candidates:
                candidates[row["id"]] = row
            else:
                existing = candidates[row["id"]]
                if existing.get("play_count") is None and row.get("play_count") is not None:
                    candidates[row["id"]] = row

        final_url = page.url
        context.close()
        browser.close()

    rows = list(candidates.values())[:LIMIT]
    source_counts: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
    return {
        "probe_version": 2,
        "account": ACCOUNT,
        "title": title,
        "final_url": final_url,
        "candidate_count": len(rows),
        "candidate_source_counts": source_counts,
        "pages": pages,
        "response_diagnostics": response_diagnostics[:12],
        "dom_video_url_count": len(dom_video_urls),
        "dom_video_urls": dom_video_urls[:20],
        "hydration_diagnostics": hydration_diagnostics[:20],
        "script_inventory": script_inventory[:30],
        "errors": errors,
        "top_videos": sorted(rows, key=lambda x: x.get("play_count") or 0, reverse=True)[:8],
    }


def main() -> int:
    report = run()
    (OUT / "account_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TIKTOK_ACCOUNT_PROBE_V2=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["candidate_count"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
