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


def looks_like_video_response(response: Response) -> bool:
    ct = (response.headers.get("content-type") or "").lower()
    url = response.url.lower()
    return (
        ct.startswith("video/")
        or "mime_type=video_mp4" in url
        or "mime_type=video" in url
        or "webapp-prime" in url and "/video/" in url
    )


def run() -> dict[str, Any]:
    report: dict[str, Any] = {"probe": "tiktok-media-probe-v2", "started_at": now_iso(), "video": KNOWN_VIDEO}
    media_responses: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required"],
        )
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1440, "height": 1000},
            user_agent=UA,
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        def on_response(response: Response) -> None:
            if not looks_like_video_response(response):
                return
            media_responses.append({
                "url": response.url,
                "status": response.status,
                "content_type": response.headers.get("content-type"),
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
        hydration_urls = hydration_video_urls(page)
        report["hydration_media_urls"] = hydration_urls

        try:
            page.locator("video").first.evaluate("v => { v.muted = true; v.play().catch(()=>{}); }")
        except Exception as exc:
            report["video_play_error"] = repr(exc)
        time.sleep(8)
        report["observed_media_responses"] = media_responses[:20]

        # Hydration gives us TikTok's signed public media URLs. Try those first,
        # then any video responses actually seen by Chromium. The browser context
        # carries the same cookies/session state as the page.
        targets: list[str] = []
        for url in hydration_urls:
            if url not in targets:
                targets.append(url)
        for row in media_responses:
            url = row.get("url")
            if isinstance(url, str) and url not in targets:
                targets.append(url)

        attempts: list[dict[str, Any]] = []
        for index, target in enumerate(targets[:6]):
            result: dict[str, Any] = {"url": target, "source": "hydration" if target in hydration_urls else "browser_network"}
            try:
                response = context.request.get(
                    target,
                    headers={
                        "Referer": KNOWN_VIDEO,
                        "User-Agent": UA,
                        "Range": "bytes=0-1048575",
                        "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                    },
                    timeout=30000,
                    fail_on_status_code=False,
                )
                body = response.body()
                result.update({
                    "status": response.status,
                    "ok": response.ok,
                    "content_type": response.headers.get("content-type"),
                    "content_length": response.headers.get("content-length"),
                    "content_range": response.headers.get("content-range"),
                    "bytes": len(body),
                    "magic_hex": body[:16].hex(),
                    "has_ftyp": b"ftyp" in body[:64],
                })
                if response.status in (200, 206) and body:
                    filename = f"media_sample_{index}.bin"
                    (OUT_DIR / filename).write_bytes(body[:2_000_000])
                    result["saved"] = filename
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
            {k: row.get(k) for k in (
                "source", "status", "ok", "content_type", "content_length", "content_range",
                "bytes", "has_ftyp", "saved", "error"
            )}
            for row in report.get("context_request_attempts", [])
        ],
        "cookies_count": report.get("cookies_count"),
    }
    dump("media_summary.json", summary)
    print("TIKTOK_MEDIA_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
