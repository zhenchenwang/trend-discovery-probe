from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

OUT_DIR = Path(os.getenv("PROBE_OUT", "probe_artifacts"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
QUERY = os.getenv("PROBE_QUERY", "humanoid robot").strip() or "humanoid robot"
LIMIT = max(1, int(os.getenv("PROBE_LIMIT", "40")))
HEADLESS = os.getenv("PROBE_HEADLESS", "1") != "0"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump_json(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def dns_probe() -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "host": "www.tiktok.com"}
    try:
        rows = socket.getaddrinfo("www.tiktok.com", 443, proto=socket.IPPROTO_TCP)
        addresses: list[str] = []
        for row in rows:
            address = row[4][0]
            if address not in addresses:
                addresses.append(address)
        result.update(ok=True, addresses=addresses[:10])
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def http_probe() -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "url": "https://www.tiktok.com/"}
    req = urllib.request.Request(
        result["url"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = response.read(200_000)
            result.update(
                ok=True,
                status=getattr(response, "status", None),
                final_url=response.geturl(),
                bytes_read=len(body),
                content_type=response.headers.get("content-type"),
            )
            (OUT_DIR / "http_home_sample.bin").write_bytes(body)
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=8_000)
    except Exception:
        return ""


def classify_page(text: str, url: str) -> dict[str, bool]:
    low = text.lower()
    url_low = url.lower()
    captcha_terms = (
        "captcha",
        "verify to continue",
        "security verification",
        "drag the slider",
        "puzzle",
    )
    access_terms = (
        "access denied",
        "too many requests",
        "temporarily unavailable",
    )
    return {
        "captcha": any(term in low for term in captcha_terms),
        "login_redirect": "/login" in url_low,
        "access_error": any(term in low for term in access_terms),
    }


def capture(page: Page, label: str) -> dict[str, Any]:
    html = ""
    try:
        html = page.content()
    except Exception:
        pass
    (OUT_DIR / f"{label}.html").write_text(html, encoding="utf-8", errors="replace")
    try:
        page.screenshot(path=str(OUT_DIR / f"{label}.png"), full_page=True)
    except Exception:
        pass
    text = body_text(page)
    (OUT_DIR / f"{label}.txt").write_text(text, encoding="utf-8", errors="replace")
    state: dict[str, Any] = {
        "url": page.url,
        "title": "",
        "html_chars": len(html),
        "body_chars": len(text),
    }
    try:
        state["title"] = page.title()
    except Exception:
        pass
    state.update(classify_page(text, page.url))
    return state


def extract_video_links(page: Page) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    try:
        hrefs = page.locator('a[href*="/video/"]').evaluate_all(
            "els => els.map(e => e.href || e.getAttribute('href')).filter(Boolean)"
        )
    except Exception:
        hrefs = []
    for href in hrefs:
        if not isinstance(href, str):
            continue
        href = urllib.parse.urljoin("https://www.tiktok.com/", href)
        if "/video/" not in href:
            continue
        clean = href.split("?", 1)[0].split("#", 1)[0]
        if clean not in seen:
            seen.add(clean)
            links.append(clean)
        if len(links) >= LIMIT:
            break
    return links


def yt_dlp_probe(url: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--dump-single-json",
        "--no-warnings",
        url,
    ]
    result: dict[str, Any] = {"ok": False, "url": url}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        result["returncode"] = proc.returncode
        result["stderr"] = proc.stderr[-8_000:]
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            result.update(
                ok=True,
                metadata={
                    "id": data.get("id"),
                    "title": data.get("title"),
                    "uploader": data.get("uploader"),
                    "uploader_id": data.get("uploader_id"),
                    "duration": data.get("duration"),
                    "timestamp": data.get("timestamp"),
                    "view_count": data.get("view_count"),
                    "like_count": data.get("like_count"),
                    "comment_count": data.get("comment_count"),
                    "repost_count": data.get("repost_count"),
                    "webpage_url": data.get("webpage_url"),
                },
            )
        else:
            result["stdout_tail"] = proc.stdout[-4_000:]
    except Exception as exc:
        result["error"] = repr(exc)
    return result


def run_browser_probe() -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "query": QUERY,
        "limit": LIMIT,
        "headless": HEADLESS,
        "started_at": now_iso(),
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(20_000)

        try:
            try:
                page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60_000)
                time.sleep(5)
                result["homepage"] = capture(page, "browser_home")
            except Exception as exc:
                result["homepage"] = {"error": repr(exc), "url": page.url}
                capture(page, "browser_home_error")

            search_url = "https://www.tiktok.com/search?q=" + urllib.parse.quote(QUERY)
            result["search_url"] = search_url
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(8)
            except PlaywrightTimeoutError as exc:
                result["search_navigation_timeout"] = repr(exc)
            except Exception as exc:
                result["search_navigation_error"] = repr(exc)

            initial_links = extract_video_links(page)
            observed: list[str] = list(initial_links)
            seen = set(observed)
            scroll_counts = [len(observed)]

            for _ in range(6):
                if len(observed) >= LIMIT:
                    break
                try:
                    page.mouse.wheel(0, 5_000)
                    time.sleep(2.5)
                    for link in extract_video_links(page):
                        if link not in seen:
                            seen.add(link)
                            observed.append(link)
                            if len(observed) >= LIMIT:
                                break
                    scroll_counts.append(len(observed))
                except Exception as exc:
                    result.setdefault("scroll_errors", []).append(repr(exc))
                    break

            result["search"] = capture(page, "browser_search")
            result["initial_video_links"] = len(initial_links)
            result["video_links_after_scroll"] = len(observed)
            result["scroll_counts"] = scroll_counts
            result["video_links"] = observed[:LIMIT]

            if observed:
                first_video = observed[0]
                result["first_video_url"] = first_video
                try:
                    page.goto(first_video, wait_until="domcontentloaded", timeout=60_000)
                    time.sleep(6)
                    result["first_video_page"] = capture(page, "browser_first_video")
                except Exception as exc:
                    result["first_video_page"] = {"error": repr(exc), "url": page.url}
                    capture(page, "browser_first_video_error")
                result["yt_dlp"] = yt_dlp_probe(first_video)

            flags = result.get("search", {})
            if observed:
                result["status"] = "ok"
                result["ok"] = True
            elif flags.get("captcha") or flags.get("login_redirect") or flags.get("access_error"):
                result["status"] = "blocked"
            else:
                result["status"] = "empty"
        finally:
            context.close()
            browser.close()

    result["finished_at"] = now_iso()
    return result


def main() -> int:
    report: dict[str, Any] = {
        "probe": "tiktok-live-probe-v1",
        "query": QUERY,
        "started_at": now_iso(),
        "python": sys.version,
        "platform": sys.platform,
        "dns": dns_probe(),
        "http": http_probe(),
    }
    try:
        report["browser"] = run_browser_probe()
    except Exception as exc:
        report["browser"] = {"ok": False, "status": "fatal", "error": repr(exc)}
    report["finished_at"] = now_iso()
    dump_json("probe_result.json", report)

    browser = report.get("browser", {})
    summary = {
        "dns_ok": report.get("dns", {}).get("ok"),
        "http_ok": report.get("http", {}).get("ok"),
        "browser_status": browser.get("status"),
        "video_links": browser.get("video_links_after_scroll", 0),
        "captcha": browser.get("search", {}).get("captcha"),
        "login_redirect": browser.get("search", {}).get("login_redirect"),
        "yt_dlp_ok": browser.get("yt_dlp", {}).get("ok"),
    }
    dump_json("summary.json", summary)
    print("TIKTOK_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
