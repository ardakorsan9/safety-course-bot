"""Gemini key / model testi — key'i ekrana yazmaz."""
from __future__ import annotations

import json
import os
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
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v


def main() -> int:
    load_secrets()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not key:
        print("SONUC: KEY_YOK — secrets.env icine GEMINI_API_KEY yaz")
        return 1
    print("SONUC: KEY_VAR")
    print("KEY_LEN:", len(key))
    print("KEY_PREFIX:", key[:4] + "..." + key[-4:])

    models = [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-flash-latest",
    ]
    body = json.dumps(
        {"contents": [{"parts": [{"text": "Reply with exactly one word: ok"}]}]}
    ).encode("utf-8")

    any_ok = False
    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={urllib.parse.quote(key)}"
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            txt = data["candidates"][0]["content"]["parts"][0]["text"]
            print(f"{model}: OK -> {txt.strip()[:50]}")
            any_ok = True
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:180]
            print(f"{model}: HTTP {e.code} -> {err}")
        except Exception as e:
            print(f"{model}: FAIL -> {e}")

    print()
    if any_ok:
        print("YORUM: Key CALISIYOR. 503 = Google kotasi/yogunluk, yeni key sart degil.")
        return 0
    print("YORUM: Hic model yanitmadi. AI Studio kotasini kontrol et veya biraz bekle.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
