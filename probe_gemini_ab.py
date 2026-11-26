# probe_gemini_ab.py — decisive A/B: native vs shim text gen + one image-gen attempt.
# Run: python probe_gemini_ab.py    (same key rules as probe_gemini_models.py)
# Never prints key material. Prints ONLY status + first chars of responses.
import json
import os
import sys
import urllib.error
import urllib.request

key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if not key:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(("GEMINI_API_KEY=", "GOOGLE_API_KEY=")):
                    key = line.split("=", 1)[1].strip().strip(chr(34)).strip("'")
                    break
    except OSError:
        pass
if not key:
    print("NO_KEY_FOUND")
    sys.exit(2)


def post(url, payload, bearer=False):
    hdrs = {"Content-Type": "application/json"}
    if bearer:
        hdrs["Authorization"] = "Bearer " + key
    else:
        hdrs["x-goog-api-key"] = key
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=hdrs,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"raw": "unparseable"}
        return exc.code, body
    except Exception as exc:
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


print("=== TEST 1: native generateContent, gemini-2.5-flash ===")
st, body = post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
    {"contents": [{"parts": [{"text": "Reply with exactly: pong"}]}]},
)
if st == 200:
    try:
        txt = body["candidates"][0]["content"]["parts"][0]["text"][:60]
        print(f"OK {st}: text={txt!r}")
    except Exception:
        print(f"OK {st} but unexpected shape: keys={list(body.keys())}")
else:
    print(f"FAIL {st}: body_type={type(body).__name__} snip={str(body)[:300]}")

print("\n=== TEST 2: shim /v1beta/openai chat/completions, gemini-2.5-flash ===")
st, body = post(
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    {
        "model": "gemini-2.5-flash",
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
    },
    bearer=True,
)
if st == 200:
    ch = body.get("choices") or []
    if ch:
        msg = (ch[0].get("message") or {}).get("content")
        print(f"OK {st}: choices={len(ch)} content={str(msg)[:60]!r}")
    else:
        print(f"OK {st} but EMPTY CHOICES - keys={list(body.keys())} body_snip={str(body)[:250]}")
else:
    print(f"FAIL {st}: body_type={type(body).__name__} snip={str(body)[:300]}")

print(
    "\n=== TEST 3: image gen, gemini-2.5-flash-image (cost iff success and billing on: ~$0.03) ==="
)
st, body = post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
    {"contents": [{"parts": [{"text": "A plain solid blue square, minimal"}]}]},
)
if st == 200:
    kinds = []
    try:
        for p in body["candidates"][0]["content"]["parts"]:
            kinds.append("inline_image" if "inlineData" in p or "inline_data" in p else "text")
    except Exception:
        kinds = ["unparseable-shape"]
    print(f"OK {st}: parts={kinds} — IMAGE GENERATION WORKS WITH THIS KEY")
else:
    print(f"FAIL {st}: body_type={type(body).__name__} snip={str(body)[:900]}")
