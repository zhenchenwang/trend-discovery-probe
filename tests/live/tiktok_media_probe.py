from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT_DIR = Path(os.getenv("PROBE_OUT", "probe_artifacts"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
KNOWN_VIDEO = os.getenv(
    "PROBE_KNOWN_VIDEO",
    "https://www.tiktok.com/@newstalkhq/video/7678230444881612063",
).strip()
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def dump(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hydration_video_urls(page) -> list[str]:
    try:
        raw = page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=10000)
        data = json.loads(raw) if raw else {}
    except Exception:
        return []
    found: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for key, value in x.items():
                if key in {"playAddr", "downloadAddr", "PlayAddr", "DownloadAddr"} and isinstance(value, str):
                    if value.startswith("http") and value not in found:
                        found.append(value)
                walk(value)
        elif isinstance(x, list):
            for value in x:
                walk(value)

    walk(data)
    return found[:20]


def run() -> dict[str, Any]:
    report: dict[str, Any] = {"probe": "tiktok-media-probe-v1", "started_at": now_iso(), "video": KNOWN_VIDEO}
    media_responses: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1440, "height": 1000},
            user_agent=UA,
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        def on_response(response: Response) -> None:
            ct = (response.headers.get("content-type") or "").lower()
            url_low = response.url.lower()
            if "video/" in ct or "mime_type=video" in url_low or "tiktokcdn" in url_low or "webapp-prime" in url_low:
                media_responses.append({
                    "url": response.url,
                    "status": response.status,
                    "content_type": ct,
                    "content_length": response.headers.get("content-length"),
                    "content_range": response.headers.get("content-range"),
                    "request_headers": {
                        k: v for k, v in response.request.headers.items()
                        if k.lower() in {"referer", "origin", "range", "user-agent", "accept"}
                    },
                })

        page.on("response", on_response)
        try:
            page.goto(KNOWN_VIDEO, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            report["navigation_error"] = repr(exc)
        time.sleep(5)
        report["title"] = page.title()
        urls = hydration_video_urls(page)
        report["hydration_media_urls"] = urls

        try:
            page.locator("video").first.evaluate("v => { v.muted = true; v.play().catch(()=>{}); }")
        except Exception as exc:
            report["video_play_error"] = repr(exc)
        time.sleep(8)
        report["observed_media_responses"] = media_responses[:20]

        # Try the browser context's authenticated request client against the signed URL.
        targets: list[str] = []
        for row in media_responses:
            if row.get("url") not in targets:
                targets.append(row["url"])
        for url in urls:
            if url not in targets:
                targets.append(url)

        attempts: list[dict[str, Any]] = []
        for index, target in enumerate(targets[:3]):
            result: dict[str, Any] = {"url": target}
            try:
                response = context.request.get(
                    target,
                    headers={
                        "Referer": KNOWN_VIDEO,
                        "User-Agent": UA,
                        "Range": "bytes=0-1048575",
                        "Accept": "*/*",
                    },
                    timeout=30000,
                    fail_on_status_code=False,
                )
                result.update({
                    "status": response.status,
                    "ok": response.ok,
                    "content_type": response.headers.get("content-type"),
                    "content_length": response.headers.get("content-length"),
                    "content_range": response.headers.get("content-range"),
                })
                body = response.body()
                result["bytes"] = len(body)
                if response.status in (200, 206) and body:
                    (OUT_DIR / f"media_sample_{index}.bin").write_bytes(body[:2_000_000])
                    result["saved"] = f"media_sample_{index}.bin"
            except Exception as exc:
                result["error"] = repr(exc)
            attempts.append(result)
        report["context_request_attempts"] = attempts

        report["cookies_count"] = len(context.cookies())
        context.close()
        browser.close()

    report["finished_at"] = now_iso()
    return report


def main() -> int:
    report = run()
    dump("media_probe_result.json", report)
    summary = {
        "title": report.get("title"),
        "hydration_media_urls": len(report.get("hydration_media_urls") or []),
        "observed_media_responses": len(report.get("observed_media_responses") or []),
        "attempts": [
            {k: row.get(k) for k in ("status", "ok", "content_type", "content_length", "content_range", "bytes", "saved", "error")}
            for row in report.get("context_request_attempts", [])
        ],
        "cookies_count": report.get("cookies_count"),
    }
    dump("media_summary.json", summary)
    print("TIKTOK_MEDIA_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
