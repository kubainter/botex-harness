# probe_gemini_models.py — read-only probe: which models does the Gemini API key see?
# Usage:  python probe_gemini_models.py
# Reads GEMINI_API_KEY / GOOGLE_API_KEY from env, or parses harness .env ITSELF in-memory.
# NEVER prints key material. Output: model ids + generation methods only.
import json
import os
import sys
import urllib.request

key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if not key:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        env_path = os.path.join(os.path.expanduser("~"), ".ai", "harness", ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(("GEMINI_API_KEY=", "GOOGLE_API_KEY=")):
                    key = line.split("=", 1)[1].strip().strip(chr(34)).strip("'")
                    break
    except OSError as exc:
        print(f"cannot read env file: {exc}")
        sys.exit(2)
if not key:
    print("NO_KEY_FOUND")
    sys.exit(2)

url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=500"
req = urllib.request.Request(url, headers={"x-goog-api-key": key})
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except Exception as exc:
    print(f"API_ERROR: {type(exc).__name__}: {exc}")
    sys.exit(1)

models = data.get("models", [])
print(f"total visible: {len(models)}")
MEDIA_HINTS = (
    "image",
    "imagen",
    "banana",
    "veo",
    "omni",
    "lyria",
    "tts",
    "music",
    "video",
    "audio",
)
media, other = [], []
for m in models:
    mid = m.get("name", "").replace("models/", "")
    methods = ",".join(m.get("supportedGenerationMethods", []))
    row = f"{mid}  [{methods}]"
    (media if any(h in mid.lower() for h in MEDIA_HINTS) else other).append(row)
print("\n== media-capable candidates ==")
print("\n".join(sorted(media)) or "(none)")
print("\n== text/other ==")
print("\n".join(sorted(other)))
