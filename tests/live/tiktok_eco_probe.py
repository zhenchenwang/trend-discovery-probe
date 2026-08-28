from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#")
OUT = Path(os.getenv("PROBE_OUT", "tiktok_eco_artifacts"))
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    candidates: dict[str, dict[str, Any]] = {}
    blocked: Counter[str] = Counter()
    api_pages: list[dict[str, Any]] = []
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )

        def route_handler(route):
            kind = route.request.resource_type
            if kind in {"image", "media", "font"}:
                blocked[kind] += 1
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_handler)
        page = context.new_page()

        def response_handler(response):
            if "/api/challenge/item_list/" not in response.url:
                return
            try:
                payload = response.json()
                items = payload.get("itemList") or payload.get("item_list") or []
                new = 0
                for item in items:
                    vid = str(item.get("id") or item.get("aweme_id") or "")
                    if not vid or vid in candidates:
                        continue
                    candidates[vid] = {
                        "id": vid,
                        "desc": item.get("desc"),
                        "author": (item.get("author") or {}).get("uniqueId") if isinstance(item.get("author"), dict) else item.get("author"),
                    }
                    new += 1
                api_pages.append({
                    "status": response.status,
                    "items": len(items),
                    "new": new,
                    "total": len(candidates),
                })
            except Exception as exc:
                errors.append(repr(exc))

        page.on("response", response_handler)
        try:
            page.goto(f"https://www.tiktok.com/tag/{TAG}", wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            errors.append("goto:" + repr(exc))
        for _ in range(5):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                page.mouse.wheel(0, 12000)
            time.sleep(1.2)
            if len(candidates) >= 60:
                break
        page.close()
        context.close()
        browser.close()

    report = {
        "ok": len(candidates) >= 20 and bool(api_pages),
        "tag": TAG,
        "candidates": len(candidates),
        "api_pages": api_pages,
        "blocked": dict(blocked),
        "blocked_total": sum(blocked.values()),
        "errors": errors,
        "sample": list(candidates.values())[:5],
    }
    (OUT / "tiktok_eco_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TIKTOK_ECO=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
