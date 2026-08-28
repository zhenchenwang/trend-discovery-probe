from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path

from playwright.sync_api import sync_playwright


ALIASES = [
    {"text": "灵异", "language": "zh"},
    {"text": "paranormal", "language": "en"},
    {"text": "心霊", "language": "ja"},
    {"text": "초자연", "language": "ko"},
]


def _items(data):
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def tiktok_probe(context):
    all_ids = OrderedDict()
    variants = []
    for variant in ALIASES:
        page = context.new_page()
        found = OrderedDict()
        api_responses = 0

        def on_response(response):
            nonlocal api_responses
            if "/api/challenge/item_list/" not in response.url:
                return
            api_responses += 1
            try:
                payload = response.json()
            except Exception:
                return
            for item in _items(payload):
                video_id = str(item.get("id") or item.get("aweme_id") or "")
                if video_id:
                    found.setdefault(video_id, True)
                    all_ids.setdefault(video_id, variant["language"])

        page.on("response", on_response)
        url = "https://www.tiktok.com/tag/" + urllib.parse.quote(variant["text"], safe="")
        error = None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(2200)
            for _ in range(2):
                page.mouse.wheel(0, 1500)
                page.wait_for_timeout(1200)
        except Exception as exc:
            error = repr(exc)
        finally:
            page.close()
        variants.append({
            **variant,
            "api_responses": api_responses,
            "unique_videos": len(found),
            "error": error,
        })
    languages_with_results = sum(1 for row in variants if row["unique_videos"] > 0)
    return {
        "variants": variants,
        "languages_with_results": languages_with_results,
        "unique_videos_total": len(all_ids),
        "ok": languages_with_results >= 2 and len(all_ids) >= 8,
    }


def douyin_probe(context):
    page = context.new_page()
    ids = OrderedDict()
    api_responses = 0
    api_parse_errors = 0

    def on_response(response):
        nonlocal api_responses, api_parse_errors
        if "/aweme/v1/web/general/search/single/" not in response.url:
            return
        api_responses += 1
        try:
            payload = response.json()
        except Exception:
            api_parse_errors += 1
            return
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = payload.get("aweme_list") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            aweme = row.get("aweme_info") or row.get("aweme_detail") or row
            if not isinstance(aweme, dict):
                continue
            video_id = str(aweme.get("aweme_id") or aweme.get("id") or "")
            if video_id:
                ids.setdefault(video_id, True)

    page.on("response", on_response)
    url = "https://www.douyin.com/search/" + urllib.parse.quote("灵异监控", safe="") + "?type=video"
    error = None
    challenge = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(2500)
        for _ in range(3):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(1300)
        title = (page.title() or "").casefold()
        try:
            body = (page.locator("body").inner_text(timeout=2500) or "")[:6000].casefold()
        except Exception:
            body = ""
        for signal in ("验证", "验证码", "访问过于频繁", "安全验证", "verify", "captcha"):
            if signal.casefold() in title or signal.casefold() in body:
                challenge.append(signal)
        try:
            hrefs = page.locator('a[href*="/video/"]').evaluate_all(
                "els => els.map(e => e.href || e.getAttribute('href')).filter(Boolean)"
            )
        except Exception:
            hrefs = []
        for href in hrefs:
            match = re.search(r"/video/(\d+)", str(href))
            if match:
                ids.setdefault(match.group(1), True)
    except Exception as exc:
        error = repr(exc)
    finally:
        page.close()
    if ids:
        status = "success"
    elif challenge:
        status = "challenged"
    elif api_responses:
        status = "api-empty"
    else:
        status = "no-observed-search-api"
    return {
        "status": status,
        "api_responses": api_responses,
        "api_parse_errors": api_parse_errors,
        "unique_videos": len(ids),
        "challenge_signals": sorted(set(challenge)),
        "error": error,
        "live_validated": bool(ids),
    }


def main():
    out = Path("probe-output")
    out.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font"}
            else route.continue_(),
        )
        tiktok = tiktok_probe(context)
        douyin = douyin_probe(context)
        context.close()
        browser.close()
    report = {
        "probe": "multilingual-tiktok-douyin",
        "timestamp": time.time(),
        "tiktok": tiktok,
        "douyin": douyin,
        "policy": {
            "tiktok_source_language": "unrestricted multilingual variants",
            "narration_language": "zh-CN handled by formal engine",
            "browser_execution": "single context sequential",
        },
        "ok": bool(tiktok["ok"]),
    }
    (out / "multilingual_tiktok_douyin.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
