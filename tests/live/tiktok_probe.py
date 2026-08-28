from __future__ import annotations

import html as html_lib
import json
import os
import re
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
KNOWN_VIDEO = os.getenv(
    "PROBE_KNOWN_VIDEO",
    "https://www.tiktok.com/@eduard.constantin63/video/7605238965226032406",
).strip()
KNOWN_USER = os.getenv("PROBE_KNOWN_USER", "eduard.constantin63").strip().lstrip("@")
KNOWN_TAG = os.getenv("PROBE_KNOWN_TAG", "humanoidrobot").strip().lstrip("#")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump_json(name: str, data: Any) -> None:
    (OUT_DIR / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def fetch_bytes(url: str, timeout: int = 30, limit: int = 1_500_000) -> tuple[dict[str, Any], bytes]:
    meta: dict[str, Any] = {"ok": False, "url": url}
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(limit)
            meta.update(
                ok=True,
                status=getattr(response, "status", None),
                final_url=response.geturl(),
                bytes_read=len(body),
                content_type=response.headers.get("content-type"),
            )
            return meta, body
    except Exception as exc:
        meta["error"] = repr(exc)
        return meta, b""


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
    result, body = fetch_bytes("https://www.tiktok.com/", limit=200_000)
    if body:
        (OUT_DIR / "http_home_sample.bin").write_bytes(body)
    return result


def oembed_probe(video_url: str) -> dict[str, Any]:
    url = "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(video_url, safe="")
    meta, body = fetch_bytes(url, limit=500_000)
    result: dict[str, Any] = {"request": meta}
    if body:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            result["ok"] = True
            result["data"] = {
                "title": data.get("title"),
                "author_name": data.get("author_name"),
                "author_url": data.get("author_url"),
                "provider_name": data.get("provider_name"),
                "thumbnail_url": data.get("thumbnail_url"),
                "html_chars": len(data.get("html", "")),
            }
        except Exception as exc:
            result["ok"] = False
            result["decode_error"] = repr(exc)
            result["body_sample"] = body[:2000].decode("utf-8", errors="replace")
    else:
        result["ok"] = False
    return result


def extract_tiktok_urls(text: str) -> list[str]:
    decoded = urllib.parse.unquote(html_lib.unescape(text.replace("\\u002F", "/")))
    patterns = [
        r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9._-]+/video/\d+",
        r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9._-]+",
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.findall(pattern, decoded):
            clean = match.split("?", 1)[0].split("#", 1)[0]
            if clean not in seen:
                seen.add(clean)
                urls.append(clean)
                if len(urls) >= LIMIT:
                    return urls
    return urls


def external_search_probe() -> dict[str, Any]:
    query = f'site:tiktok.com/@ "{QUERY}"'
    targets = {
        "bing": "https://www.bing.com/search?q=" + urllib.parse.quote(query),
        "duckduckgo": "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query),
    }
    result: dict[str, Any] = {"query": query, "engines": {}, "video_urls": []}
    all_urls: list[str] = []
    seen: set[str] = set()
    for name, url in targets.items():
        meta, body = fetch_bytes(url)
        text = body.decode("utf-8", errors="replace") if body else ""
        (OUT_DIR / f"search_{name}.html").write_text(text, encoding="utf-8", errors="replace")
        urls = extract_tiktok_urls(text)
        for found in urls:
            if found not in seen:
                seen.add(found)
                all_urls.append(found)
        result["engines"][name] = {
            **meta,
            "html_chars": len(text),
            "tiktok_urls": urls[:20],
            "tiktok_url_count": len(urls),
        }
    result["video_urls"] = [u for u in all_urls if "/video/" in u][:LIMIT]
    result["all_tiktok_urls"] = all_urls[:LIMIT]
    return result


def body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=8_000)
    except Exception:
        return ""


def classify_page(text: str, url: str, title: str = "") -> dict[str, bool]:
    low = text.lower()
    title_low = title.lower()
    url_low = url.lower()
    captcha_terms = (
        "captcha",
        "verify to continue",
        "security verification",
        "drag the slider",
        "puzzle",
    )
    login_terms = (
        "log in to search for popular content",
        "log in to continue",
        "login to continue",
    )
    access_terms = (
        "access denied",
        "too many requests",
        "temporarily unavailable",
    )
    return {
        "captcha": any(term in low for term in captcha_terms),
        "login_wall": (
            any(term in low for term in login_terms)
            or title_low.startswith("log in")
            or "/login" in url_low
        ),
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
    title = ""
    try:
        title = page.title()
    except Exception:
        pass
    state: dict[str, Any] = {
        "url": page.url,
        "title": title,
        "html_chars": len(html),
        "body_chars": len(text),
    }
    state.update(classify_page(text, page.url, title))
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


def navigate_and_capture(page: Page, url: str, label: str, wait_seconds: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = {"requested_url": url}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(wait_seconds)
        result.update(capture(page, label))
        result["video_links"] = extract_video_links(page)[:LIMIT]
    except PlaywrightTimeoutError as exc:
        result["timeout"] = repr(exc)
        result.update(capture(page, label + "_timeout"))
    except Exception as exc:
        result["error"] = repr(exc)
        result.update(capture(page, label + "_error"))
    return result


def run_browser_probe(external: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "query": QUERY,
        "limit": LIMIT,
        "headless": HEADLESS,
        "started_at": now_iso(),
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            viewport={"width": 1440, "height": 1000},
            user_agent=UA,
        )
        page = context.new_page()
        page.set_default_timeout(20_000)
        try:
            result["homepage"] = navigate_and_capture(page, "https://www.tiktok.com/", "browser_home", 4)

            search_url = "https://www.tiktok.com/search?q=" + urllib.parse.quote(QUERY)
            search = navigate_and_capture(page, search_url, "browser_search", 7)
            observed = list(search.get("video_links", []))
            seen = set(observed)
            scroll_counts = [len(observed)]
            if not search.get("login_wall"):
                for _ in range(6):
                    if len(observed) >= LIMIT:
                        break
                    page.mouse.wheel(0, 5_000)
                    time.sleep(2.0)
                    for link in extract_video_links(page):
                        if link not in seen:
                            seen.add(link)
                            observed.append(link)
                    scroll_counts.append(len(observed))
            search["video_links_after_scroll"] = observed[:LIMIT]
            search["scroll_counts"] = scroll_counts
            result["search"] = search

            result["known_video_page"] = navigate_and_capture(page, KNOWN_VIDEO, "browser_known_video", 6)
            result["known_user_page"] = navigate_and_capture(
                page, f"https://www.tiktok.com/@{KNOWN_USER}", "browser_known_user", 6
            )
            result["known_tag_page"] = navigate_and_capture(
                page, f"https://www.tiktok.com/tag/{KNOWN_TAG}", "browser_known_tag", 6
            )

            ext_videos = external.get("video_urls") or []
            if ext_videos:
                result["external_video_page"] = navigate_and_capture(
                    page, ext_videos[0], "browser_external_video", 6
                )

            result["known_video_yt_dlp"] = yt_dlp_probe(KNOWN_VIDEO)

            candidate_links: list[str] = []
            for source in (
                observed,
                result["known_user_page"].get("video_links", []),
                result["known_tag_page"].get("video_links", []),
                ext_videos,
            ):
                for url in source:
                    if "/video/" in url and url not in candidate_links:
                        candidate_links.append(url)
            result["candidate_video_links"] = candidate_links[:LIMIT]
            result["status"] = "ok" if candidate_links else (
                "search_login_wall" if search.get("login_wall") else "empty"
            )
            result["ok"] = bool(candidate_links)
        finally:
            context.close()
            browser.close()

    result["finished_at"] = now_iso()
    return result


def main() -> int:
    external = external_search_probe()
    report: dict[str, Any] = {
        "probe": "tiktok-live-probe-v2",
        "query": QUERY,
        "known_video": KNOWN_VIDEO,
        "started_at": now_iso(),
        "python": sys.version,
        "platform": sys.platform,
        "dns": dns_probe(),
        "http": http_probe(),
        "oembed": oembed_probe(KNOWN_VIDEO),
        "external_search": external,
    }
    try:
        report["browser"] = run_browser_probe(external)
    except Exception as exc:
        report["browser"] = {"ok": False, "status": "fatal", "error": repr(exc)}
    report["finished_at"] = now_iso()
    dump_json("probe_result.json", report)

    browser = report.get("browser", {})
    summary = {
        "dns_ok": report.get("dns", {}).get("ok"),
        "http_ok": report.get("http", {}).get("ok"),
        "oembed_ok": report.get("oembed", {}).get("ok"),
        "external_video_urls": len(external.get("video_urls") or []),
        "search_login_wall": browser.get("search", {}).get("login_wall"),
        "known_video_page_login_wall": browser.get("known_video_page", {}).get("login_wall"),
        "known_user_video_links": len(browser.get("known_user_page", {}).get("video_links") or []),
        "known_tag_video_links": len(browser.get("known_tag_page", {}).get("video_links") or []),
        "candidate_video_links": len(browser.get("candidate_video_links") or []),
        "known_video_yt_dlp_ok": browser.get("known_video_yt_dlp", {}).get("ok"),
        "browser_status": browser.get("status"),
    }
    dump_json("summary.json", summary)
    print("TIKTOK_PROBE_SUMMARY=" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
