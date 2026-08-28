from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

OUT = Path(os.getenv("PROBE_OUT", "youtube_download_artifacts"))
VIDEO_ID = os.getenv("PROBE_VIDEO_ID", "gqRO8PuPVd0")
OUT.mkdir(parents=True, exist_ok=True)
TARGET = OUT / f"{VIDEO_ID}.mp4"
URL = f"https://www.youtube.com/shorts/{VIDEO_ID}"


def main() -> int:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--retries", "2",
        "--fragment-retries", "2",
        "--max-filesize", "40M",
        "--merge-output-format", "mp4",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "-o", str(TARGET),
        URL,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    files = list(OUT.glob(f"{VIDEO_ID}*"))
    media = next((p for p in files if p.is_file() and p.stat().st_size > 10_000), None)
    report = {
        "url": URL,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-3000:],
        "stderr_tail": result.stderr[-3000:],
        "files": [{"name": p.name, "bytes": p.stat().st_size} for p in files if p.is_file()],
        "ok": media is not None,
    }
    if media is not None:
        report["media"] = {
            "path": str(media),
            "bytes": media.stat().st_size,
            "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        }
    (OUT / "youtube_download_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("YOUTUBE_DOWNLOAD=" + json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
