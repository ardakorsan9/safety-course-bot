"""Quiz: cache (soru metni → doğru şık METNİ), Gemini fallback, review'dan öğrenme.

Cache'e asla harf/index yazılmaz — Moodle şıkları her denemede karıştırabilir.
"""
from __future__ import annotations

import difflib
import html
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from browser import ROOT, body_text, click_text, goto, log, wait_url_change
from lesson import completion_items

# ---------------------------------------------------------------- normalize


def norm(s: str) -> str:
    s = html.unescape(s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[a-z]\s*[.)]\s+", "", s)  # "a. metin" → "metin"
    s = s.strip(" \t'\"`")
    s = re.sub(r"[\s.;:,!?]+$", "", s)
    return s


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ---------------------------------------------------------------- cache


class AnswerCache:
    """İki kurs da aynı nesneyi paylaşır; okuma/yazma kilitle korunur."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, Any]] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.data = raw.get("questions", {}) if isinstance(raw, dict) else {}
            except Exception as e:
                log(f"  Cache okunamadı ({e}) — boş başlıyor")

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "questions": self.data}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    @staticmethod
    def key(q: dict) -> str:
        k = norm(q.get("qtext") or "")
        if not k:
            k = "opts:" + "|".join(sorted(norm(o["text"]) for o in q.get("options") or []))
        return k

    def lookup(self, q: dict) -> dict | None:
        k = self.key(q)
        with self.lock:
            if k in self.data:
                return dict(self.data[k])
            best, best_r = None, 0.0
            for kk, v in self.data.items():
                r = difflib.SequenceMatcher(None, k, kk).ratio()
                if r > best_r:
                    best, best_r = v, r
            return dict(best) if best is not None and best_r >= 0.92 else None

    def learn(self, q: dict, correct: list[str], wrong: list[str]) -> None:
        k = self.key(q)
        with self.lock:
            e = self.data.setdefault(
                k, {"question": q.get("qtext") or "", "type": q.get("type"), "correct": [], "wrong": []}
            )
            if correct:
                e["correct"] = correct
            for w in wrong:
                if w not in e["wrong"] and w not in e["correct"]:
                    e["wrong"].append(w)

    def __len__(self) -> int:
        with self.lock:
            return len(self.data)


# ---------------------------------------------------------------- DOM

_EXTRACT_JS = r"""() => {
  const out = [];
  document.querySelectorAll('.que').forEach((que, qi) => {
    const qt = que.querySelector('.qtext');
    const qtext = (qt ? qt.innerText : '').replace(/\s+/g, ' ').trim();
    const inputs = Array.from(que.querySelectorAll('.answer input[type=radio], .answer input[type=checkbox]'));
    const options = inputs.map(inp => {
      let lab = '';
      const lb = inp.getAttribute('aria-labelledby');
      if (lb) lab = lb.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
      if (!lab && inp.id) {
        const l = document.querySelector('label[for="' + CSS.escape(inp.id) + '"]');
        if (l) lab = l.innerText;
      }
      const row = inp.closest('.answer > *') || inp.parentElement;
      if (!lab && row) lab = row.innerText;
      lab = (lab || '').replace(/\s+/g, ' ').trim();
      const num = ((row && row.querySelector('.answernumber')) || {}).innerText || '';
      const n = num.trim();
      if (n && lab.startsWith(n)) lab = lab.slice(n.length).trim();
      const rc = (row && row.className) || '';
      return {
        id: inp.id, text: lab, checked: inp.checked, disabled: inp.disabled,
        correct: /\bcorrect\b/.test(rc), incorrect: /\bincorrect\b/.test(rc)
      };
    });
    const qc = que.className || '';
    const state = /\bpartiallycorrect\b/.test(qc) ? 'partial'
      : /\bincorrect\b/.test(qc) ? 'incorrect'
      : /\bcorrect\b/.test(qc) ? 'correct' : '';
    out.push({
      qi, qtext,
      type: inputs.length ? inputs[0].type : 'other',
      options,
      right: ((que.querySelector('.rightanswer') || {}).innerText || '').replace(/\s+/g, ' ').trim(),
      state
    });
  });
  return out;
}"""


def extract(page: Page) -> list[dict]:
    try:
        return page.evaluate(_EXTRACT_JS) or []
    except Exception as e:
        log(f"  Soru okunamadı: {e}")
        return []


def set_choices(page: Page, q: dict, want_ids: set[str]) -> bool:
    """Radio: istenen id'ye tıkla. Checkbox: her kutuyu istenen duruma getir."""
    return bool(
        page.evaluate(
            """([ids, type, all]) => {
              const want = new Set(ids);
              for (const id of all) {
                const inp = document.getElementById(id);
                if (!inp) continue;
                const should = want.has(id);
                if (type === 'radio') {
                  if (should && !inp.checked) inp.click();
                } else if (inp.checked !== should) {
                  inp.click();
                }
              }
              return all.filter(id => want.has(id)).every(id => document.getElementById(id)?.checked);
            }""",
            [list(want_ids), q["type"], [o["id"] for o in q["options"]]],
        )
    )


def match_texts(q: dict, texts: list[str]) -> list[str]:
    """Cache/Gemini metinlerini bu sayfadaki şık id'lerine eşle."""
    ids: list[str] = []
    for t in texts:
        nt = norm(t)
        exact = [o for o in q["options"] if norm(o["text"]) == nt]
        if exact:
            ids.append(exact[0]["id"])
            continue
        scored = sorted(((similar(o["text"], t), o) for o in q["options"]), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.85:
            ids.append(scored[0][1]["id"])
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------- Gemini

QUIZ_SYSTEM = (
    "You are an expert on Italian/EU workplace health and safety training (D.Lgs 81/08): "
    "VDU ergonomics, electrical risk, safety signs, chemicals/CLP, ladders, manual handling, "
    "work stress, fire prevention, emergency management. "
    "For each question pick the correct option number(s) from the given list. "
    "For 'multiple': true questions, return every correct option. "
    "Never pick an option listed in 'known_wrong'. "
    'Reply JSON only: {"answers":[{"id":0,"options":[2]}]}'
)


def _gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


_GEMINI_LOCK = threading.Lock()  # iki kurs aynı anda istek atıp 429 almasın


def gemini_answers(cfg: dict, qs: list[dict], cache: AnswerCache) -> dict[int, list[str]]:
    with _GEMINI_LOCK:
        return _gemini_answers(cfg, qs, cache)


def _gemini_answers(cfg: dict, qs: list[dict], cache: AnswerCache) -> dict[int, list[str]]:
    """{qi: [şık metni, ...]} — numara sadece bu istekte geçerli, hemen metne çevrilir."""
    key = _gemini_key()
    if not key or not qs:
        if not key:
            log("  GEMINI_API_KEY yok — Gemini atlandı")
        return {}

    payload = []
    for q in qs:
        e = cache.lookup(q) or {}
        payload.append(
            {
                "id": q["qi"],
                "question": q["qtext"],
                "multiple": q["type"] == "checkbox",
                "options": [{"n": i + 1, "text": o["text"]} for i, o in enumerate(q["options"])],
                "known_wrong": e.get("wrong", []),
            }
        )

    models: list[str] = []
    for m in (
        cfg.get("quiz_openai_model"),
        cfg.get("openai_model"),
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ):
        if m and m not in models:
            models.append(m)

    body = {
        "systemInstruction": {"parts": [{"text": QUIZ_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps({"questions": payload}, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }

    data = None
    deadline = time.time() + float(cfg.get("gemini_max_seconds") or 120)
    for model in models:
        if time.time() > deadline:
            log("  Gemini süre sınırı doldu — tahminle devam")
            break
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={urllib.parse.quote(key)}"
        )
        for attempt in range(3):
            if time.time() > deadline:
                break
            log(f"  Gemini: {model} (deneme {attempt + 1})")
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                text = out["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{[\s\S]*\}", text)
                data = json.loads(m.group(0) if m else text)
                break
            except urllib.error.HTTPError as e:
                wait = 15 * (attempt + 1) if e.code in (429, 503) else 0
                log(f"  Gemini HTTP {e.code}" + (f" — {wait}s bekle" if wait else ""))
                if not wait:
                    break
                time.sleep(max(0, min(wait, deadline - time.time())))
            except Exception as e:
                log(f"  Gemini hata: {e}")
                time.sleep(3)
        if data:
            break

    result: dict[int, list[str]] = {}
    for a in (data or {}).get("answers", []) if isinstance(data, dict) else []:
        try:
            qi = int(a.get("id"))
        except Exception:
            continue
        q = next((x for x in qs if x["qi"] == qi), None)
        if not q:
            continue
        texts = []
        for n in a.get("options") or []:
            try:
                i = int(n) - 1
            except Exception:
                continue
            if 0 <= i < len(q["options"]):
                texts.append(q["options"][i]["text"])
        if q["type"] == "radio":
            texts = texts[:1]
        if texts:
            result[qi] = texts
    return result


# ---------------------------------------------------------------- akış


def _qs(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)


def _attempt_id(url: str) -> str:
    return (_qs(url).get("attempt") or [""])[0]


def _cmid(url: str) -> str:
    q = _qs(url)
    return (q.get("cmid") or q.get("id") or [""])[0]


START_PATTERNS = [
    r"^continue (your|the last) attempt$",
    r"^continue your attempt$",
    r"^re-?attempt quiz$",
    r"^attempt quiz( now)?$",
]


def open_cache(cfg: dict) -> AnswerCache:
    return AnswerCache(ROOT / (cfg.get("answers_cache") or "answers_cache.json"))


class QuizRunner:
    def __init__(self, cfg: dict, cache: AnswerCache | None = None):
        self.cfg = cfg
        self.cache = cache or open_cache(cfg)
        self.learned: set[str] = set()
        self.submit_tries: dict[str, int] = {}
        self.after_ms = int(cfg.get("after_click_wait_ms") or 1600)
        log(f"  Cevap hafızası: {len(self.cache)} soru")

    # ---- view.php
    def handle_view(self, page: Page) -> str:
        """'passed' | 'started' | 'review' | 'blocked' | 'unavailable'"""
        text = body_text(page)
        items = completion_items(page)
        pass_items = [s for s, t in items if "passing grade" in t.lower()]
        if pass_items:
            if pass_items[0] == "done":
                log("  Quiz geçildi (Done: passing grade)")
                return "passed"
        elif items and all(s == "done" for s, _ in items):
            log("  Quiz tamam (tüm şartlar Done)")
            return "passed"

        # Öğrenilmemiş deneme review'ı varsa önce oradan öğren
        try:
            links = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/mod/quiz/review.php?attempt="]')).map(a => a.href)"""
            ) or []
        except Exception:
            links = []
        for href in links:
            aid = _attempt_id(href)
            if aid and aid not in self.learned:
                log(f"  Deneme {aid} review'ından öğrenilecek")
                goto(page, href)
                return "review"

        if re.search(r"no more attempts are allowed", text, re.I):
            log("  Deneme hakkı bitti!")
            return "blocked"

        old = page.url
        hit = click_text(page, START_PATTERNS, "#region-main")
        if not hit:
            log("  Attempt butonu yok (quiz kilitli olabilir)")
            return "unavailable"
        log(f"  → {hit}")
        page.wait_for_timeout(1500)
        # Süreli/şifreli quiz: 'Start attempt' modalı
        if "attempt.php" not in (page.url or ""):
            hit2 = click_text(page, [r"^start attempt$"])
            if hit2:
                log(f"  → {hit2}")
        wait_url_change(page, old, 20)
        return "started"

    # ---- attempt.php
    def handle_attempt(self, page: Page) -> None:
        qs = [q for q in extract(page) if q["type"] in ("radio", "checkbox") and q["options"]]
        log(f"  Bu sayfada {len(qs)} soru")

        chosen: dict[int, list[str]] = {}
        need_ai: list[dict] = []
        for q in qs:
            e = self.cache.lookup(q)
            ids = match_texts(q, e["correct"]) if e and e.get("correct") else []
            if ids:
                chosen[q["qi"]] = ids
                log(f"  Q{q['qi']+1} [hafıza] → {_labels(q, ids)}")
            else:
                need_ai.append(q)

        if need_ai:
            log(f"  {len(need_ai)} soru hafızada yok → Gemini")
            try:
                ai = gemini_answers(self.cfg, need_ai, self.cache)
            except Exception as e:
                log(f"  Gemini beklenmeyen hata ({e}) — tahminle devam")
                ai = {}
            for qi, texts in ai.items():
                q = next(x for x in need_ai if x["qi"] == qi)
                ids = match_texts(q, texts)
                if ids:
                    chosen[qi] = ids
                    log(f"  Q{qi+1} [Gemini] → {_labels(q, ids)}")

        for q in qs:
            if q["qi"] in chosen:
                continue
            e = self.cache.lookup(q) or {}
            wrong = {norm(w) for w in e.get("wrong", [])}
            pool = [o for o in q["options"] if norm(o["text"]) not in wrong] or q["options"]
            chosen[q["qi"]] = [pool[0]["id"]]
            log(f"  Q{q['qi']+1} [TAHMİN — bilinen yanlışlar hariç] → {pool[0]['text'][:70]}")

        for q in qs:
            if not set_choices(page, q, set(chosen[q["qi"]])):
                log(f"  Q{q['qi']+1} işaretlenemedi!")
        page.wait_for_timeout(400)

        old = page.url
        nav = None
        try:
            nav = page.evaluate(
                """() => {
                  const el = document.querySelector('#mod_quiz-next-nav, input[name="next"]');
                  if (!el) return null;
                  el.click();
                  return el.value || el.innerText || 'next';
                }"""
            )
        except Exception:
            pass
        if not nav:
            nav = click_text(page, [r"^finish attempt", r"^next page$"], "#region-main")
        log(f"  → {nav or 'Next/Finish bulunamadı'}")
        if nav:
            wait_url_change(page, old, 20)

    # ---- summary.php
    def handle_summary(self, page: Page) -> None:
        aid = _attempt_id(page.url)
        n = self.submit_tries.get(aid, 0) + 1
        self.submit_tries[aid] = n
        if n > 3:
            log("  Summary 3 kez denendi — quiz sayfasına dönülüyor")
            cm = _cmid(page.url)
            if cm:
                goto(page, f"https://ole.unibz.it/mod/quiz/view.php?id={cm}")
            return

        old = page.url
        hit = click_text(page, [r"^submit all and finish$"], "#region-main")
        log(f"  → {hit or 'Submit butonu yok'}")
        if not hit:
            return
        if self._confirm_modal(page):
            wait_url_change(page, old, 25)
        else:
            wait_url_change(page, old, 10)

    def _confirm_modal(self, page: Page) -> bool:
        """Modal tam açılınca (.show) içindeki onayı JS ile tıkla."""
        start = time.time()
        end = start + 10
        while time.time() < end:
            # Arka plan sekmesinde animasyon durabilir: 1.5 sn sonra opaklık şartı aranmaz
            relaxed = time.time() - start > 1.5
            try:
                ok = page.evaluate(
                    """(relaxed) => {
                      const m = Array.from(document.querySelectorAll('.modal.show, .moodle-dialogue-base:not(.moodle-dialogue-hidden)'))
                        .find(x => getComputedStyle(x).display !== 'none' && (relaxed || getComputedStyle(x).opacity === '1'));
                      if (!m) return null;
                      const btn = m.querySelector('[data-action="save"]') ||
                        Array.from(m.querySelectorAll('button, input[type=submit]'))
                          .find(b => /submit all and finish/i.test(b.innerText || b.value || '')) ||
                        m.querySelector('.btn-primary');
                      if (!btn) return null;
                      btn.click();
                      return (btn.innerText || btn.value || 'ok').trim();
                    }""",
                    relaxed,
                )
            except Exception:
                ok = None
            if ok:
                log(f"  → onay penceresi: {ok}")
                return True
            page.wait_for_timeout(300)
        log("  Onay penceresi çıkmadı")
        return False

    # ---- review.php
    def handle_review(self, page: Page) -> None:
        aid = _attempt_id(page.url)
        if aid and aid not in self.learned:
            self._learn(page)
            self.learned.add(aid)
        cm = _cmid(page.url)
        if cm:
            goto(page, f"https://ole.unibz.it/mod/quiz/view.php?id={cm}")
        else:
            old = page.url
            if click_text(page, [r"^finish review$"]):
                wait_url_change(page, old, 15)

    def _learn(self, page: Page) -> None:
        qs = [q for q in extract(page) if q["options"]]
        learned_ok = 0
        for q in qs:
            correct: list[str] = []
            if q["right"]:
                body = norm(re.sub(r"^\s*the correct answers? (is|are)\s*:?", "", q["right"], flags=re.I))
                hits = [o for o in q["options"] if norm(o["text"]) and norm(o["text"]) in body]
                if q["type"] == "radio" and len(hits) > 1:
                    hits = [max(hits, key=lambda o: similar(o["text"], body))]
                correct = [o["text"] for o in hits]
            if not correct:
                correct = [o["text"] for o in q["options"] if o["correct"]]
            if not correct and q["state"] == "correct":
                correct = [o["text"] for o in q["options"] if o["checked"]]
            wrong = [o["text"] for o in q["options"] if o["incorrect"]]
            if q["type"] == "radio" and q["state"] == "incorrect":
                wrong += [o["text"] for o in q["options"] if o["checked"]]
            if correct or wrong:
                self.cache.learn(q, correct, wrong)
            if correct:
                learned_ok += 1
        self.cache.save()
        log(f"  Review: {learned_ok}/{len(qs)} sorunun doğru cevabı hafızaya yazıldı")


def _labels(q: dict, ids: list[str]) -> str:
    return " + ".join(o["text"][:60] for o in q["options"] if o["id"] in ids)
