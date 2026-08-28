from __future__ import annotations

import json
import math
import os
import random
import re
import string
import time
import urllib.parse
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright

OUT = Path(os.getenv("PROBE_OUT", "event_cluster_artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
TAG = os.getenv("PROBE_TAG", "机器人").strip().lstrip("#") or "机器人"
HASHTAG_LIMIT = max(60, int(os.getenv("PROBE_HASHTAG_LIMIT", "120")))
ACCOUNT_LIMIT = max(15, int(os.getenv("PROBE_ACCOUNT_LIMIT", "60")))
ACCOUNT_COUNT = max(1, min(6, int(os.getenv("PROBE_ACCOUNT_COUNT", "3"))))
THRESHOLD = float(os.getenv("PROBE_EVENT_THRESHOLD", "0.36"))
MAX_GAP_HOURS = float(os.getenv("PROBE_MAX_GAP_HOURS", str(30 * 24)))
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HASHTAG_RE = re.compile(r"#([^\s#]+)", re.UNICODE)
LATIN_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{2,}")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
GENERIC_TAGS = {
    "fyp", "foryou", "foryoupage", "viral", "trending", "tiktok",
    "抖音", "热门", "推荐", "机器人", "robot", "robots", "ai", "中国", "🇨🇳",
}


def item_list(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("itemList", "item_list", "aweme_list", "items"):
        rows = data.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def video_row(item: dict[str, Any], source: str) -> dict[str, Any] | None:
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    if isinstance(author, str):
        author = {"uniqueId": author}
    vid = str(item.get("id") or item.get("aweme_id") or "")
    uid = author.get("uniqueId") or author.get("unique_id")
    if not vid or not uid:
        return None
    description = item.get("desc") or item.get("description") or ""
    return {
        "id": vid,
        "author": str(uid),
        "description": description,
        "hashtags": HASHTAG_RE.findall(description),
        "play_count": stats.get("playCount") or stats.get("play_count"),
        "like_count": stats.get("diggCount") or stats.get("digg_count"),
        "comment_count": stats.get("commentCount") or stats.get("comment_count"),
        "share_count": stats.get("shareCount") or stats.get("share_count"),
        "create_time": item.get("createTime") or item.get("create_time"),
        "source": source,
    }


def creator_query(sec_uid: str, cursor: int, device_id: str) -> dict[str, str]:
    return {
        "aid": "1988", "app_language": "en", "app_name": "tiktok_web",
        "browser_language": "en-US", "browser_name": "Mozilla", "browser_online": "true",
        "browser_platform": "Linux x86_64", "browser_version": "5.0 (X11; Linux x86_64)",
        "channel": "tiktok_web", "cookie_enabled": "true", "count": "15",
        "cursor": str(cursor), "device_id": device_id, "device_platform": "web_pc",
        "focus_state": "true", "from_page": "user", "history_len": "2",
        "is_fullscreen": "false", "is_page_visible": "true", "language": "en",
        "os": "linux", "priority_region": "", "referer": "", "region": "US",
        "screen_height": "1000", "screen_width": "1440", "secUid": sec_uid,
        "type": "1", "tz_name": "UTC",
        "verifyFp": "verify_" + "".join(random.choices(string.hexdigits, k=7)),
        "webcast_language": "en",
    }


def clean_tag(value: str) -> str:
    return value.strip().lstrip("#").rstrip(".,!?;:，。！？；：)]}、").casefold()


def tag_set(row: dict[str, Any]) -> set[str]:
    out = set()
    for raw in row.get("hashtags") or []:
        tag = clean_tag(str(raw))
        if len(tag) >= 2 and tag not in GENERIC_TAGS:
            out.add(tag)
    return out


def text_terms(description: str) -> set[str]:
    text = HASHTAG_RE.sub(" ", description or "").casefold()
    terms = {token.casefold() for token in LATIN_TOKEN_RE.findall(text)}
    for chunk in CJK_RE.findall(text):
        if len(chunk) <= 6:
            terms.add(chunk)
            continue
        for width in (3, 4, 5):
            if len(chunk) < width:
                continue
            for idx in range(len(chunk) - width + 1):
                terms.add(chunk[idx:idx + width])
    return terms


@dataclass
class Feature:
    row: dict[str, Any]
    hashtags: set[str]
    terms: set[str]


@dataclass
class Cluster:
    features: list[Feature]
    similarities: dict[str, float] = field(default_factory=dict)


def idf(features: list[Feature], attr: str) -> dict[str, float]:
    n = max(1, len(features))
    freq: Counter[str] = Counter()
    for f in features:
        freq.update(getattr(f, attr))
    result = {}
    for token, count in freq.items():
        result[token] = 0.12 if count / n >= 0.45 else math.log((n + 1.0) / (count + 1.0)) + 1.0
    return result


def weighted_jaccard(a: set[str], b: set[str], weights: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return sum(weights.get(x, 1.0) for x in a & b) / sum(weights.get(x, 1.0) for x in union)


def similarity(a: Feature, b: Feature, tag_weights: dict[str, float], term_weights: dict[str, float]) -> float:
    tag_score = weighted_jaccard(a.hashtags, b.hashtags, tag_weights)
    text_score = weighted_jaccard(a.terms, b.terms, term_weights)
    shared_rare = [x for x in a.hashtags & b.hashtags if tag_weights.get(x, 1.0) >= 1.35]
    score = 0.42 * tag_score + 0.58 * text_score
    if shared_rare:
        score += min(0.12, 0.05 + 0.02 * len(shared_rare))
    ta, tb = a.row.get("create_time"), b.row.get("create_time")
    if ta and tb:
        gap = abs(float(ta) - float(tb)) / 3600.0
        if gap <= 72:
            score += 0.05
        elif gap > MAX_GAP_HOURS:
            score *= 0.68
    return max(0.0, min(1.0, score))


def trend_proxy(row: dict[str, Any]) -> float:
    plays = float(row.get("play_count") or 0)
    published = row.get("create_time")
    age_h = max(0.25, (time.time() - float(published)) / 3600.0) if published else 720.0
    velocity = plays / age_h
    likes = float(row.get("like_count") or 0)
    comments = float(row.get("comment_count") or 0)
    shares = float(row.get("share_count") or 0)
    engagement = (likes + 2 * comments + 3 * shares) / max(plays, 1.0)
    freshness = 28.0 * math.exp(-age_h / 72.0)
    return min(100.0, min(52.0, 8.0 * math.log10(1.0 + velocity)) + freshness + min(12.0, engagement * 35.0))


def cluster_rows(rows: list[dict[str, Any]]) -> list[Cluster]:
    features = [Feature(row=row, hashtags=tag_set(row), terms=text_terms(row.get("description") or "")) for row in rows]
    tw = idf(features, "hashtags")
    xw = idf(features, "terms")
    ordered = sorted(features, key=lambda f: trend_proxy(f.row), reverse=True)
    clusters: list[Cluster] = []
    for feature in ordered:
        best = None
        best_score = 0.0
        for cluster in clusters:
            scores = sorted((similarity(feature, existing, tw, xw) for existing in cluster.features), reverse=True)
            if not scores:
                continue
            score = scores[0]
            if len(cluster.features) >= 3 and len(scores) >= 2:
                score = 0.7 * scores[0] + 0.3 * scores[1]
            if score > best_score:
                best_score, best = score, cluster
        if best is not None and best_score >= THRESHOLD:
            best.features.append(feature)
            best.similarities[feature.row["id"]] = best_score
        else:
            clusters.append(Cluster([feature], {feature.row["id"]: 1.0}))
    return clusters


def keywords(cluster: Cluster, limit: int = 6) -> list[str]:
    counts: Counter[str] = Counter()
    for f in cluster.features:
        counts.update({tag: 3 for tag in f.hashtags})
        counts.update(term for term in f.terms if 3 <= len(term) <= 18)
    return [token for token, _ in counts.most_common(limit)]


def cluster_summary(cluster: Cluster) -> dict[str, Any]:
    members = sorted(cluster.features, key=lambda f: trend_proxy(f.row), reverse=True)
    authors = sorted({f.row["author"] for f in members})
    top = members[0].row
    return {
        "member_count": len(members),
        "author_count": len(authors),
        "authors": authors[:8],
        "keywords": keywords(cluster),
        "representative": {
            "id": top["id"], "author": top["author"], "description": top["description"],
            "play_count": top.get("play_count"), "create_time": top.get("create_time"),
            "trend_proxy": round(trend_proxy(top), 2),
        },
        "members": [
            {
                "id": f.row["id"], "author": f.row["author"],
                "description": f.row["description"], "play_count": f.row.get("play_count"),
                "create_time": f.row.get("create_time"),
                "similarity": round(cluster.similarities.get(f.row["id"], 1.0), 3),
            }
            for f in members[:12]
        ],
    }


def run() -> dict[str, Any]:
    hashtag_rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
    account_rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
    errors: list[str] = []
    account_reports: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(locale="en-US", timezone_id="UTC", viewport={"width": 1440, "height": 1000}, user_agent=UA)
        page = context.new_page()

        def on_response(response: Response) -> None:
            if "/api/challenge/item_list/" not in response.url or response.status != 200:
                return
            try:
                data = response.json()
            except Exception as exc:
                errors.append("hashtag_json: " + repr(exc)); return
            for item in item_list(data):
                row = video_row(item, "hashtag")
                if row and row["id"] not in hashtag_rows:
                    hashtag_rows[row["id"]] = row

        page.on("response", on_response)
        page.goto(f"https://www.tiktok.com/tag/{urllib.parse.quote(TAG)}", wait_until="domcontentloaded", timeout=60_000)
        time.sleep(4)
        stale = 0
        previous = 0
        for _ in range(20):
            if len(hashtag_rows) >= HASHTAG_LIMIT:
                break
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.4)
            current = len(hashtag_rows)
            stale = stale + 1 if current <= previous else 0
            previous = current
            if stale >= 5:
                break

        author_counts = Counter(row["author"] for row in hashtag_rows.values())
        selected_accounts = [author for author, count in author_counts.most_common() if count >= 2][:ACCOUNT_COUNT]

        for selected_account in selected_accounts:
            try:
                profile = context.new_page()
                profile_url = f"https://www.tiktok.com/@{urllib.parse.quote(selected_account)}"
                profile.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(3)
                hydration_text = profile.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content() or "{}"
                hydration = json.loads(hydration_text)
                scope = hydration.get("__DEFAULT_SCOPE__") or {}
                detail = scope.get("webapp.user-detail") or {}
                user_info = detail.get("userInfo") or {}
                user = user_info.get("user") or {}
                stats = user_info.get("statsV2") or user_info.get("stats") or {}
                app_context = scope.get("webapp.app-context") or {}
                sec_uid = user.get("secUid")
                device_id = str(app_context.get("wid") or random.randint(10**18, 10**19 - 1))
                if not sec_uid:
                    raise RuntimeError("no secUid")
                local: OrderedDict[str, dict[str, Any]] = OrderedDict()
                cursor = int(time.time() * 1000)
                for page_no in range(1, 10):
                    if len(local) >= ACCOUNT_LIMIT:
                        break
                    resp = context.request.get(
                        "https://www.tiktok.com/api/creator/item_list/",
                        params=creator_query(sec_uid, cursor, device_id),
                        headers={"Referer": profile_url, "User-Agent": UA, "Accept": "application/json"},
                        timeout=30_000,
                    )
                    data = resp.json()
                    items = item_list(data)
                    for item in items:
                        row = video_row(item, "account:" + selected_account)
                        if row and row["id"] not in local:
                            local[row["id"]] = row
                    old_cursor = cursor
                    if items and (items[-1].get("createTime") or items[-1].get("create_time")):
                        cursor = int(float(items[-1].get("createTime") or items[-1].get("create_time")) * 1000)
                    if cursor == old_cursor:
                        cursor -= 7 * 86_400_000
                    if not data.get("hasMorePrevious"):
                        break
                profile.close()
                for vid, row in local.items():
                    if vid not in account_rows:
                        account_rows[vid] = row
                account_reports.append({
                    "account": selected_account,
                    "hashtag_evidence": author_counts[selected_account],
                    "profile_video_count": stats.get("videoCount"),
                    "history_candidates": len(local),
                })
            except Exception as exc:
                errors.append(f"account:{selected_account}: {exc!r}")

        context.close(); browser.close()

    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in hashtag_rows.values(): merged[row["id"]] = row
    for row in account_rows.values(): merged.setdefault(row["id"], row)
    clusters = cluster_rows(list(merged.values()))
    multi = [cluster for cluster in clusters if len(cluster.features) >= 2]
    cross_author = [cluster for cluster in multi if len({f.row["author"] for f in cluster.features}) >= 2]
    multi.sort(key=lambda c: (len({f.row["author"] for f in c.features}), len(c.features), max(trend_proxy(f.row) for f in c.features)), reverse=True)

    report = {
        "ok": bool(hashtag_rows),
        "tag": TAG,
        "threshold": THRESHOLD,
        "hashtag_candidates": len(hashtag_rows),
        "selected_accounts": account_reports,
        "account_history_unique": len(account_rows),
        "merged_unique_candidates": len(merged),
        "cluster_count_total": len(clusters),
        "multi_member_clusters": len(multi),
        "cross_author_clusters": len(cross_author),
        "singleton_count": sum(1 for c in clusters if len(c.features) == 1),
        "largest_cluster_size": max((len(c.features) for c in clusters), default=0),
        "top_clusters": [cluster_summary(c) for c in multi[:15]],
        "errors": errors,
    }
    return report


def main() -> int:
    report = run()
    (OUT / "event_cluster_summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TIKTOK_EVENT_CLUSTER=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
