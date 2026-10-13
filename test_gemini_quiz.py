"""Quiz boyutunda Gemini istegi vs kucuk istek — 503 nedenini ayikla."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def load_secrets() -> None:
    p = Path(__file__).resolve().parent / "secrets.env"
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def call(model: str, body: dict, label: str) -> None:
    key = os.environ.get("GEMINI_API_KEY") or ""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={urllib.parse.quote(key)}"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        txt = data["candidates"][0]["content"]["parts"][0]["text"]
        print(f"[{label}] {model}: OK {time.time()-t0:.1f}s -> {txt[:80].replace(chr(10),' ')}")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        msg = ""
        try:
            msg = json.loads(err).get("error", {}).get("message", err)[:160]
        except Exception:
            msg = err[:160]
        print(f"[{label}] {model}: HTTP {e.code} {time.time()-t0:.1f}s -> {msg}")
    except Exception as e:
        print(f"[{label}] {model}: FAIL {e}")


def main() -> None:
    load_secrets()
    key = os.environ.get("GEMINI_API_KEY") or ""
    print("KEY:", "VAR" if key else "YOK", "len=", len(key))
    print()

    # 1) kucuk istek
    small = {"contents": [{"parts": [{"text": "Say ok"}]}]}
    call("gemini-3.5-flash", small, "KUCUK")
    call("gemini-3.5-flash-lite", small, "KUCUK")
    print()

    # 2) botun gonderdigine benzer buyuk quiz JSON
    questions = []
    for i in range(9):
        questions.append(
            {
                "qid": i,
                "question": (
                    "The position of the hands on the keyboard is correct when... "
                    if i == 0
                    else f"Sample workplace safety question number {i} about VDU ergonomics?"
                ),
                "options": [
                    {"label": "a. the articulation of the hands is horizontal with the fingers relaxed.", "type": "radio"},
                    {"label": "b. the joint of the hands is bent downwards. The shoulders should be slightly raised.", "type": "radio"},
                    {"label": "c. the joint of the hands is bent upwards. The support of the hands is negligible.", "type": "radio"},
                ],
            }
        )
    system = (
        "You answer Moodle workplace-safety / VDU ergonomics True/False quizzes. "
        "For EACH question pick exactly one option label. Never Back. JSON only."
    )
    user = json.dumps({"questions": questions}, ensure_ascii=False)
    big = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    print("BUYUK payload byte:", len(json.dumps(big).encode("utf-8")))
    call("gemini-3.5-flash", big, "BUYUK+JSON")
    call("gemini-3.5-flash-lite", big, "BUYUK+JSON")
    print()

    # 3) ayni buyuk ama JSON mime olmadan
    big2 = {
        "systemInstruction": {"parts": [{"text": system + " Reply JSON only."}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0},
    }
    call("gemini-3.5-flash-lite", big2, "BUYUK-noMime")
    print()
    print("OZET:")
    print("- KEY gecersiz olsa 400/403 olur, 503 olmaz.")
    print("- 503 = Google sunucusu dolu / free tier yogunluk.")
    print("- Kucuk OK buyuk 503 ise: yuksek talep + buyuk istek.")


if __name__ == "__main__":
    main()
