from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

OUT = Path(os.getenv("PROBE_OUT", "artifacts/youtube_comments"))
OUT.mkdir(parents=True, exist_ok=True)
QUERY = os.getenv("PROBE_QUERY", "机器人 shorts")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"')
VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


def request(url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    body = None if payload is None else json.dumps(payload).encode()
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
    if body is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=35) as resp:
        return int(resp.status), resp.read()


def walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("simpleText"), str):
        return value["simpleText"]
    if isinstance(value.get("content"), str):
        return value["content"]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(str(row.get("text") or "") for row in runs if isinstance(row, dict))
    return ""


def comments(data: Any, video_id: str) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for obj in walk(data):
        entity = obj.get("commentEntityPayload")
        if isinstance(entity, dict):
            props = entity.get("properties") or {}
            toolbar = entity.get("toolbar") or {}
            cid = str(props.get("commentId") or "").strip()
            body = props.get("content") or {}
            value = body.get("content") if isinstance(body, dict) else body
            value = str(value or "").strip()
            if cid and value:
                result[cid] = {"id": cid, "text": value, "likes_raw": toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked"), "video_id": video_id}
        renderer = obj.get("commentRenderer")
        if isinstance(renderer, dict):
            cid = str(renderer.get("commentId") or "").strip()
            value = text(renderer.get("contentText")).strip()
            if cid and value:
                result[cid] = {"id": cid, "text": value, "likes_raw": text(renderer.get("voteCount")), "video_id": video_id}
    return list(result.values())


def continuations(data: Any) -> list[str]:
    out: list[str] = []
    for obj in walk(data):
        command = obj.get("continuationCommand")
        if isinstance(command, dict) and isinstance(command.get("token"), str):
            token = command["token"]
            if token not in out:
                out.append(token)
    return out


def fetch_comments(video_id: str) -> dict[str, Any]:
    watch = f"https://www.youtube.com/watch?v={video_id}"
    status, raw = request(watch)
    html = raw.decode("utf-8", errors="replace")
    key = API_KEY_RE.search(html)
    version = CLIENT_VERSION_RE.search(html)
    if not key or not version:
        return {"video_id": video_id, "watch_status": status, "comments": [], "error": "innertube config missing"}
    context = {"client": {"clientName": "WEB", "clientVersion": version.group(1), "hl": "en", "gl": "US"}}
    endpoint = f"https://www.youtube.com/youtubei/v1/next?key={urllib.parse.quote(key.group(1))}&prettyPrint=false"
    common = {"Origin": "https://www.youtube.com", "Referer": watch}
    next_status, next_raw = request(endpoint, {"context": context, "videoId": video_id}, common)
    data = json.loads(next_raw.decode("utf-8"))
    found = comments(data, video_id)
    tokens = continuations(data)
    tried = 0
    for token in tokens[:12]:
        if found:
            break
        tried += 1
        try:
            _, body = request(endpoint, {"context": context, "continuation": token}, common)
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            continue
        found = comments(payload, video_id)
    return {"video_id": video_id, "watch_status": status, "next_status": next_status, "continuations": len(tokens), "tried": tried, "comments": found[:30]}


def main() -> int:
    search_url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": QUERY})
    status, raw = request(search_url)
    html = raw.decode("utf-8", errors="replace")
    video_ids: list[str] = []
    for vid in VIDEO_ID_RE.findall(html):
        if vid not in video_ids:
            video_ids.append(vid)
    attempts = []
    winner = None
    for vid in video_ids[:10]:
        row = fetch_comments(vid)
        attempts.append({k: row.get(k) for k in ("video_id", "watch_status", "next_status", "continuations", "tried", "error")})
        if row.get("comments"):
            winner = row
            break
    report = {"ok": bool(winner), "query": QUERY, "search_status": status, "video_ids": video_ids[:10], "attempts": attempts, "winner": winner}
    (OUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("YOUTUBE_COMMENTS=" + json.dumps(report, ensure_ascii=False))
    return 0 if winner else 2


if __name__ == "__main__":
    raise SystemExit(main())
