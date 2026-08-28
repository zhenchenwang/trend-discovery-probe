from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path

BASE = os.getenv("PROBE_VLM_URL", "http://127.0.0.1:8080/v1").rstrip("/")
MODEL = os.getenv("PROBE_VLM_MODEL", "trend-video-brain")
OUT = Path(os.getenv("PROBE_OUT", "llama_multimodal_artifacts"))
OUT.mkdir(parents=True, exist_ok=True)

# 1x1 red PNG. Keeping the probe dependency-free mirrors the production stdlib client.
RED_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
)


def parse_json(text: str):
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        value = json.loads(clean)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    first, last = clean.find("{"), clean.rfind("}")
    if first >= 0 and last > first:
        value = json.loads(clean[first : last + 1])
        if isinstance(value, dict):
            return value
    raise RuntimeError("model output did not contain JSON object: " + text[:500])


def main() -> int:
    image_uri = "data:image/png;base64," + base64.b64encode(RED_PNG).decode("ascii")
    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_tokens": 120,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Inspect the image. Return ONLY JSON with keys summary and confidence. summary must describe the dominant visible color.",
                    },
                    {"type": "image_url", "image_url": {"url": image_uri}},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        raw = json.loads(response.read().decode("utf-8"))
    content = raw["choices"][0]["message"]["content"]
    parsed = parse_json(str(content))
    if not parsed.get("summary"):
        raise RuntimeError("missing summary: " + repr(parsed))
    result = {
        "ok": True,
        "base": BASE,
        "model": MODEL,
        "content": content,
        "parsed": parsed,
    }
    (OUT / "llama_multimodal_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("LLAMA_MULTIMODAL=" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
