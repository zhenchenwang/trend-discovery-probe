from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path("youtube_player_probe_artifacts")
OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
QUERY = "humanoid robot"
IDS = ["gqRO8PuPVd0", "RdgNurNf0jQ", "UFxsosMQ5Dw"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def post(url, payload):
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=raw, headers={"User-Agent": UA, "Content-Type": "application/json", "Origin": "https://www.youtube.com"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def main():
    html = get("https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": QUERY})).decode("utf-8", errors="replace")
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html).group(1)
    version = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html).group(1)
    api = f"https://www.youtube.com/youtubei/v1/player?key={urllib.parse.quote(key)}&prettyPrint=false"
    context = {"client": {"clientName": "WEB", "clientVersion": version, "hl": "en", "gl": "US"}}
    rows=[]
    total=0
    for vid in IDS:
        status, raw = post(api, {"context": context, "videoId": vid, "contentCheckOk": True, "racyCheckOk": True})
        total += len(raw)
        data=json.loads(raw)
        vd=data.get("videoDetails") or {}
        mf=(data.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        rows.append({"id":vid,"status":status,"bytes":len(raw),"title":vd.get("title"),"author":vd.get("author"),"channel_id":vd.get("channelId"),"view_count":vd.get("viewCount"),"length_seconds":vd.get("lengthSeconds"),"publish_date":mf.get("publishDate"),"upload_date":mf.get("uploadDate"),"playability":(data.get("playabilityStatus") or {}).get("status")})
    report={"ok":all(x["status"]==200 and x["publish_date"] for x in rows),"client_version":version,"total_player_bytes":total,"rows":rows}
    (OUT/"player_metadata.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("PLAYER_METADATA="+json.dumps(report,ensure_ascii=False))
    return 0 if report["ok"] else 2

if __name__=="__main__": raise SystemExit(main())
