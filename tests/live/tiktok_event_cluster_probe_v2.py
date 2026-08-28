from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path

import tiktok_event_cluster_probe as base

OUT = Path(os.getenv("PROBE_OUT", "event_cluster_v2_artifacts"))
OUT.mkdir(parents=True, exist_ok=True)
THRESHOLD = float(os.getenv("PROBE_EVENT_THRESHOLD", "0.34"))

GENERIC_TERMS = {
    "the", "and", "for", "with", "will", "how", "what", "this", "that", "are", "robot", "robots",
    "china", "chinese", "fyp", "unitree", "t800",
    "机器人", "中国机", "国机器", "机器人", "中国机器", "国机器人", "中国机器人",
    "人形机器人", "人工智能", "智能机器人", "科技", "中国宇树", "宇树机器人",
}


def _build_features(rows):
    features = []
    for row in rows:
        tags = base.tag_set(row)
        terms = {term for term in base.text_terms(row.get("description") or "") if term not in GENERIC_TERMS}
        features.append(base.Feature(row=row, hashtags=tags, terms=terms))
    return features


def _drop_author_boilerplate(features, attr: str, threshold: float) -> None:
    by_author = defaultdict(list)
    for feature in features:
        by_author[feature.row.get("author") or ""].append(feature)
    for author, rows in by_author.items():
        if not author or len(rows) < 4:
            continue
        counts = Counter()
        for feature in rows:
            counts.update(getattr(feature, attr))
        boilerplate = {token for token, count in counts.items() if count / len(rows) >= threshold}
        if not boilerplate:
            continue
        for feature in rows:
            setattr(feature, attr, getattr(feature, attr) - boilerplate)


def _similarity(a, b, tag_weights, term_weights):
    tag_score = base.weighted_jaccard(a.hashtags, b.hashtags, tag_weights)
    text_score = base.weighted_jaccard(a.terms, b.terms, term_weights)
    shared_tags = a.hashtags & b.hashtags
    shared_terms = a.terms & b.terms

    # Metadata-only clustering must prefer precision. Creator boilerplate has already
    # been removed; remaining matches still need event-specific evidence.
    if not shared_tags and not shared_terms:
        return 0.0
    if text_score < 0.08 and tag_score < 0.38:
        return 0.0
    if a.row.get("author") == b.row.get("author") and text_score < 0.10 and len(shared_tags) < 2:
        return 0.0

    score = 0.25 * tag_score + 0.75 * text_score
    rare_tags = [token for token in shared_tags if tag_weights.get(token, 1.0) >= 1.45]
    if rare_tags:
        score += min(0.05, 0.02 + 0.01 * len(rare_tags))

    ta, tb = a.row.get("create_time"), b.row.get("create_time")
    if ta and tb:
        gap = abs(float(ta) - float(tb)) / 3600.0
        if gap <= 48 and score >= 0.15:
            score += 0.025
        elif gap > 14 * 24:
            score *= 0.55
    return max(0.0, min(1.0, score))


def cluster_rows_v2(rows):
    features = _build_features(rows)
    _drop_author_boilerplate(features, "hashtags", 0.32)
    _drop_author_boilerplate(features, "terms", 0.38)
    tag_weights = base.idf(features, "hashtags")
    term_weights = base.idf(features, "terms")
    ordered = sorted(features, key=lambda f: base.trend_proxy(f.row), reverse=True)
    clusters = []

    for feature in ordered:
        best = None
        best_score = 0.0
        for cluster in clusters:
            scores = sorted(
                (_similarity(feature, existing, tag_weights, term_weights) for existing in cluster.features),
                reverse=True,
            )
            if not scores or scores[0] < THRESHOLD:
                continue
            if len(cluster.features) == 1:
                score = scores[0]
            else:
                top = scores[: min(3, len(scores))]
                score = 0.60 * top[0] + 0.40 * (sum(top) / len(top))
                # The seed/representative must still agree, preventing A~B~C chains.
                representative_score = _similarity(feature, cluster.features[0], tag_weights, term_weights)
                if len(cluster.features) >= 4 and representative_score < THRESHOLD * 0.65:
                    continue
            if score > best_score:
                best_score, best = score, cluster

        if best is not None and best_score >= THRESHOLD:
            best.features.append(feature)
            best.similarities[feature.row["id"]] = best_score
        else:
            clusters.append(base.Cluster([feature], {feature.row["id"]: 1.0}))
    return clusters


def main() -> int:
    base.cluster_rows = cluster_rows_v2
    base.THRESHOLD = THRESHOLD
    report = base.run()
    report["algorithm"] = "metadata-v2-author-aware"
    report["threshold"] = THRESHOLD
    report["precision_guards"] = {
        "author_hashtag_boilerplate_ratio": 0.32,
        "author_text_boilerplate_ratio": 0.38,
        "tag_weight": 0.25,
        "text_weight": 0.75,
        "chain_guard": True,
    }
    path = OUT / "event_cluster_v2_summary.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TIKTOK_EVENT_CLUSTER_V2=" + json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
