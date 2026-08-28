from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(os.getenv("PROBE_OUT", "comment_probe_artifacts"))
URL = os.getenv(
    "PROBE_VIDEO_URL",
    "https://www.tiktok.com/@cold2998/video/7677159568731852062",
)
OUT.mkdir(parents=True, exist_ok=True)


def main() -> int:
    report = {"url": URL, "responses": [], "comments": [], "errors": []}
    seen = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
            timezone_id="UTC",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        def on_response(resp):
            if "/api/comment/" not in resp.url:
                return
            rec = {"url": resp.url, "status": resp.status}
            try:
                body = resp.body()
                rec["bytes"] = len(body)
                if body:
                    data = json.loads(body.decode("utf-8-sig"))
                    rec["keys"] = sorted(data.keys()) if isinstance(data, dict) else []
                    rows = data.get("comments") if isinstance(data, dict) else None
                    if isinstance(rows, list):
                        rec["comment_count"] = len(rows)
                        for item in rows:
                            if not isinstance(item, dict):
                                continue
                            cid = str(item.get("cid") or item.get("id") or "")
                            if not cid or cid in seen:
                                continue
                            seen.add(cid)
                            user = item.get("user") or {}
                            report["comments"].append({
                                "cid": cid,
                                "text": item.get("text"),
                                "digg_count": item.get("digg_count"),
                                "reply_comment_total": item.get("reply_comment_total"),
                                "create_time": item.get("create_time"),
                                "user": user.get("unique_id") if isinstance(user, dict) else None,
                            })
            except Exception as exc:
                rec["error"] = repr(exc)
            report["responses"].append(rec)

        page.on("response", on_response)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            for _ in range(8):
                # Scroll the page and likely comment containers. TikTok changes class
                # names often, so use structural overflow detection rather than one class.
                try:
                    page.evaluate("""
                    () => {
                      const els = Array.from(document.querySelectorAll('div'));
                      for (const el of els) {
                        const s = getComputedStyle(el);
                        if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 300) {
                          el.scrollTop = el.scrollHeight;
                        }
                      }
                      window.scrollTo(0, document.body.scrollHeight);
                    }
                    """)
                except Exception as exc:
                    report["errors"].append("scroll:" + repr(exc))
                time.sleep(1.5)
        except Exception as exc:
            report["errors"].append("nav:" + repr(exc))
        finally:
            report["title"] = page.title()
            report["final_url"] = page.url
            browser.close()

    report["response_count"] = len(report["responses"])
    report["comment_count"] = len(report["comments"])
    report["ok"] = report["comment_count"] > 0
    (OUT / "comments_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("COMMENT_PROBE=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
