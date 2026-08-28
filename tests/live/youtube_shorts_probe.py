from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

OUT = Path(os.getenv("PROBE_OUT", "youtube_probe_artifacts"))
QUERY = os.getenv("PROBE_QUERY", "humanoid robot")
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def fetch(url: str, *, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    body = None if data is None else json.dumps(data).encode("utf-8")
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=35) as r:
        return r.status, r.read(), dict(r.headers)


def balanced_json(text: str, marker: str) -> dict[str, Any] | None:
    pos = text.find(marker)
    if pos < 0:
        return None
    start = text.find("{", pos + len(marker))
    if start < 0:
        return None
    depth = 0
    quote = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                quote = False
            continue
        if ch == '"':
            quote = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start:i + 1])
                    return value if isinstance(value, dict) else None
                except Exception:
                    return None
    return None


def walk(value: Any):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(str(x.get("text") or "") for x in runs if isinstance(x, dict))
    if isinstance(value.get("content"), str):
        return value["content"]
    return ""


def parse_count(text: str) -> int:
    value = str(text or "").replace(",", "").strip().casefold()
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmb]?)", value)
    if not match:
        return 0
    number = float(match.group(1))
    mult = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(match.group(2), 1)
    return int(number * mult)


def shorts_from(data: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = set()
    for obj in walk(data):
        video_id = None
        endpoint = obj.get("reelWatchEndpoint")
        if isinstance(endpoint, dict):
            video_id = endpoint.get("videoId")
        if not video_id:
            tap = obj.get("onTap") or obj.get("onTapCommand")
            if isinstance(tap, dict):
                for inner in walk(tap):
                    r = inner.get("reelWatchEndpoint")
                    if isinstance(r, dict) and r.get("videoId"):
                        video_id = r["videoId"]
                        break
        if not video_id or video_id in seen:
            continue
        raw = json.dumps(obj, ensure_ascii=False)[:12000]
        if "shortsLockupViewModel" not in raw and "reelWatchEndpoint" not in raw:
            continue
        seen.add(video_id)
        overlay = obj.get("overlayMetadata") if isinstance(obj.get("overlayMetadata"), dict) else {}
        title = text_value(overlay.get("primaryText")) or text_value(obj.get("headline")) or text_value(obj.get("title"))
        views = text_value(overlay.get("secondaryText")) or text_value(obj.get("viewCountText"))
        out.append({
            "video_id": video_id,
            "title": title,
            "views_text": views,
            "view_count": parse_count(views),
            "url": f"https://www.youtube.com/shorts/{video_id}",
        })
    return out


def api_key_and_version(html: str) -> tuple[str | None, str | None]:
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    version = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html)
    return (key.group(1) if key else None, version.group(1) if version else None)


def continuation_entries(data: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for obj in walk(data):
        cmd = obj.get("continuationCommand")
        if not isinstance(cmd, dict) or not isinstance(cmd.get("token"), str):
            continue
        token = cmd["token"]
        if token in seen:
            continue
        seen.add(token)
        blob = json.dumps(obj, ensure_ascii=False)[:1500]
        entries.append({
            "token": token,
            "request": str(cmd.get("request") or ""),
            "target_id": str(obj.get("targetId") or obj.get("targetId") or ""),
            "context": blob[:500],
        })
    return entries


def comments_from(data: Any) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for obj in walk(data):
        payload = obj.get("commentEntityPayload")
        if isinstance(payload, dict):
            props = payload.get("properties") or {}
            toolbar = payload.get("toolbar") or {}
            cid = str(props.get("commentId") or "")
            text = props.get("content") or {}
            content = text.get("content") if isinstance(text, dict) else text
            if cid and cid not in seen:
                seen.add(cid)
                out.append({
                    "comment_id": cid,
                    "text": str(content or ""),
                    "like_count": toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked") or 0,
                    "reply_count": toolbar.get("replyCount") or 0,
                })
        renderer = obj.get("commentRenderer")
        if isinstance(renderer, dict):
            cid = str(renderer.get("commentId") or "")
            if cid and cid not in seen:
                seen.add(cid)
                out.append({
                    "comment_id": cid,
                    "text": text_value(renderer.get("contentText")),
                    "like_count_text": text_value(renderer.get("voteCount")),
                    "reply_count": 0,
                })
    return out


def main() -> int:
    report: dict[str, Any] = {"query": QUERY, "stages": {}, "errors": []}
    search_url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": QUERY})
    try:
        status, body, _ = fetch(search_url)
        html = body.decode("utf-8", errors="replace")
        initial = balanced_json(html, "ytInitialData")
        shorts = shorts_from(initial or {})
        shorts.sort(key=lambda x: x.get("view_count", 0), reverse=True)
        key, version = api_key_and_version(html)
        report["stages"]["search"] = {"status": status, "bytes": len(body), "shorts": len(shorts), "api_key": bool(key), "client_version": version}
        report["shorts"] = shorts[:20]
        if not shorts:
            raise RuntimeError("no Shorts found in ytInitialData")

        selected = shorts[0]
        video_id = selected["video_id"]
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        status, body, _ = fetch(watch_url)
        watch = body.decode("utf-8", errors="replace")
        initial_watch = balanced_json(watch, "ytInitialData") or {}
        key2, version2 = api_key_and_version(watch)
        key = key2 or key
        version = version2 or version or "2.20260801.00.00"
        initial_entries = continuation_entries(initial_watch)
        report["stages"]["watch"] = {
            "status": status,
            "bytes": len(body),
            "initial_tokens": len(initial_entries),
            "api_key": bool(key),
            "client_version": version,
            "continuations": [{k: v for k, v in x.items() if k != "token"} for x in initial_entries],
            "has_comments_section_text": "comments-section" in watch,
        }

        context = {"client": {"clientName": "WEB", "clientVersion": version, "hl": "en", "gl": "US"}}
        if not key:
            raise RuntimeError("INNERTUBE_API_KEY not found")
        api = f"https://www.youtube.com/youtubei/v1/next?key={urllib.parse.quote(key)}&prettyPrint=false"
        headers = {"Origin": "https://www.youtube.com", "Referer": watch_url}

        s, raw, _ = fetch(api, data={"context": context, "videoId": video_id}, headers=headers)
        next_data = json.loads(raw.decode("utf-8"))
        seeded_comments = comments_from(next_data)
        next_entries = continuation_entries(next_data)
        report["stages"]["next"] = {
            "status": s,
            "bytes": len(raw),
            "comments": len(seeded_comments),
            "continuations": len(next_entries),
            "has_comments_section": "comments-section" in raw.decode("utf-8", errors="ignore"),
            "continuation_contexts": [{k: v for k, v in x.items() if k != "token"} for x in next_entries],
        }

        comments = list(seeded_comments)
        used_token = None
        attempts = []
        for entry in next_entries[:15] + initial_entries[:10]:
            token = entry["token"]
            try:
                cs, craw, _ = fetch(api, data={"context": context, "continuation": token}, headers=headers)
                cdata = json.loads(craw.decode("utf-8"))
                found = comments_from(cdata)
                attempts.append({
                    "status": cs,
                    "bytes": len(craw),
                    "comments": len(found),
                    "request": entry.get("request"),
                    "target_id": entry.get("target_id"),
                    "has_comment_word": b"comment" in craw.lower(),
                })
                if found:
                    comments = found
                    used_token = token
                    report["stages"]["comments"] = {"status": cs, "bytes": len(craw), "comments": len(found)}
                    break
            except Exception as exc:
                attempts.append({"error": repr(exc), "request": entry.get("request"), "target_id": entry.get("target_id")})
        report["continuation_attempts"] = attempts
        report["comments"] = comments[:30]
        report["selected"] = selected
        # Discovery is independently useful and should not be marked broken if a
        # particular selected video has comments disabled. Keep the comment stage explicit.
        report["discovery_ok"] = len(shorts) > 0
        report["comments_ok"] = len(comments) > 0
        report["ok"] = report["discovery_ok"] and report["comments_ok"]
        report["used_comment_continuation"] = bool(used_token)
    except Exception as exc:
        report["ok"] = False
        report["error"] = repr(exc)

    (OUT / "youtube_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("YOUTUBE_PROBE=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
