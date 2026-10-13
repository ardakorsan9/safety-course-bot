"""
BOT v4 — Moodle ders botu (sol menü yukarı→aşağı)

Akış (sırayla):
  1) Sol TOC: üstten alta ilk YEŞİL OLMAYAN maddeyi aç
  2) Lesson: "spend at least X min" varsa bekle (+1 dk) → Next… → End
  3) Quiz: Attempt → Gemini cevapla → Finish attempt → Submit
     geçmezse Re-attempt; yeşil olunca sonraki madde
  4) Contents II / III… aynı şekilde devam

Gemini SADECE quiz'de. Back yok. Ana sayfaya kaçma yok.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from playwright.sync_api import Frame, Locator, Page, sync_playwright

VERSION = "v4.3-flow"
UNIT = {"minute": 60, "minutes": 60, "min": 60, "mins": 60, "dakika": 60, "dk": 60}


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_secrets() -> None:
    for name in ("secrets.env", ".env"):
        p = Path(__file__).resolve().parent / name
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and not os.environ.get(k):
                os.environ[k] = v
        print(f"Ayar: {name}")
        break


def pause(msg: str = "\nEnter...") -> None:
    try:
        if sys.stdin.isatty():
            input(msg)
    except EOFError:
        pass


def scopes(page: Page) -> list[Page | Frame]:
    out: list[Page | Frame] = [page]
    for fr in page.frames:
        if fr != page.main_frame:
            out.append(fr)
    return out


def body_text(page: Page) -> str:
    try:
        return page.locator("#region-main, main, body").first.inner_text(timeout=3000)
    except Exception:
        try:
            return page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""


def parse_spend_sec(text: str, patterns: list[str]) -> float | None:
    best = None
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            try:
                val = float(m.group("value").replace(",", "."))
            except Exception:
                continue
            sec = val * UNIT.get(m.group("unit").lower(), 0)
            if sec > 0 and (best is None or sec > best):
                best = sec
    return best


def gemini_key(cfg: dict) -> str:
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or (cfg.get("gemini_api_key") or "")
        or ""
    )


def llm_json(
    cfg: dict,
    system: str,
    user: str,
    retries: int = 3,
    *,
    prefer_model: str | None = None,
) -> dict[str, Any] | None:
    key = gemini_key(cfg)
    if not key:
        print("GEMINI_API_KEY yok")
        return None
    # Once lite (429 az); flash yedek
    models = [
        prefer_model or cfg.get("quiz_openai_model") or cfg.get("openai_model") or "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
    ]
    seen: set[str] = set()
    model_list = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            model_list.append(m)

    def _parse_content(content: str) -> dict[str, Any] | None:
        text = (content or "").strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if fence:
            text = fence.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text) or re.search(r"\[[\s\S]*\]", text)
            if not m:
                return None
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, list):
                return {"answers": parsed}
            return parsed

    last_err: Any = None
    for model in model_list:
        for use_mime in (True, False):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={urllib.parse.quote(key)}"
            )
            gen: dict[str, Any] = {"temperature": 0}
            if use_mime:
                gen["responseMimeType"] = "application/json"
            body = {
                "systemInstruction": {"parts": [{"text": system + "\nReply JSON only."}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": gen,
            }
            for attempt in range(1, retries + 1):
                print(
                    f"  Gemini çağrı: model={model} mime={use_mime} "
                    f"deneme={attempt}/{retries} …"
                )
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    content = payload["candidates"][0]["content"]["parts"][0]["text"]
                    parsed = _parse_content(content)
                    if parsed is None:
                        print(f"  Gemini JSON parse edilemedi: {str(content)[:120]}")
                        last_err = "json_parse"
                        continue
                    # answers listesini normalize et
                    if "answers" not in parsed and isinstance(parsed.get("answer"), list):
                        parsed = {"answers": parsed["answer"]}
                    print(f"  Gemini OK ({model})")
                    return parsed
                except Exception as e:
                    last_err = e
                    wait = min(6, attempt * 2)
                    print(f"  Gemini hata: {e} — {wait}s")
                    time.sleep(wait)
    print(f"Gemini vazgeçildi: {last_err}")
    return None


def scroll_all(page: Page) -> None:
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.mouse.wheel(0, 1800)
    except Exception:
        pass
    page.wait_for_timeout(350)


def click_by_labels(page: Page, labels: list[str]) -> str | None:
    """Sadece görünür Next/End/Attempt quiz vb. — reload spam YOK."""
    scroll_all(page)
    for label in labels:
        pat = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
        for scope in scopes(page):
            for loc in (
                scope.locator(f'input[type="submit"][value="{label}"]'),
                scope.get_by_role("button", name=pat),
                scope.get_by_role("link", name=pat),
                scope.get_by_text(pat, exact=True),
            ):
                try:
                    t = loc.first
                    if t.count() == 0 or not t.is_visible(timeout=350):
                        continue
                    name = (t.get_attribute("value") or t.inner_text(timeout=500) or label).strip()
                    try:
                        t.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    box = t.bounding_box()
                    if box:
                        page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                            delay=40,
                        )
                    else:
                        t.click(timeout=8000)
                    return re.sub(r"\s+", " ", name)[:80]
                except Exception:
                    continue
    # JS fallback (ana + frame)
    js = """(labels) => {
      const want = labels.map(l => l.trim().toLowerCase());
      for (const el of document.querySelectorAll('a,button,input[type="submit"],input[type="button"]')) {
        const t = (el.innerText || el.value || '').trim().replace(/\\s+/g,' ');
        if (!t || t.length > 48) continue;
        if (!want.includes(t.toLowerCase())) continue;
        if (el.type === 'hidden') continue;
        const st = getComputedStyle(el);
        if (st.display==='none'||st.visibility==='hidden') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        el.scrollIntoView({block:'center'});
        el.click();
        return t;
      }
      return null;
    }"""
    for scope in scopes(page):
        try:
            hit = scope.evaluate(js, labels)
            if hit:
                return str(hit)
        except Exception:
            continue
    return None


def has_button(page: Page, labels: list[str]) -> bool:
    scroll_all(page)
    for label in labels:
        pat = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
        for scope in scopes(page):
            for loc in (
                scope.locator(f'input[type="submit"][value="{label}"]'),
                scope.get_by_role("button", name=pat),
                scope.get_by_role("link", name=pat),
                scope.get_by_text(pat, exact=True),
            ):
                try:
                    t = loc.first
                    if t.count() and t.is_visible(timeout=250):
                        return True
                except Exception:
                    continue
    return False


# ---------- TOC ----------

@dataclass
class TocItem:
    text: str
    done: bool
    current: bool
    is_quiz: bool
    locked: bool = False
    href: str = ""


def list_toc(page: Page, cfg: dict) -> list[TocItem]:
    """Sol menü: yukarıdan aşağı, kurs sırası."""
    selectors = cfg.get("sidebar_selectors") or ["#courseindex", ".courseindex"]
    raw = page.evaluate(
        """(selectors) => {
          function isGreen(el) {
            if (!el) return false;
            const row = el.closest('.courseindex-item') || el.parentElement || el;
            const nodes = [el, el.parentElement, row].filter(Boolean);
            row.querySelectorAll('i,svg,span,[class*="icon"],[class*="completion"]').forEach(n => nodes.push(n));
            for (const n of nodes.slice(0, 20)) {
              const cls = ((n.className && n.className.toString)?n.className.toString():'').toLowerCase();
              if (/incomplete|notcomplete|not-complete|fail/.test(cls)) continue;
              if (/(?:^|\\s|-)(?:complete|completed|completion_complete|success|text-success)(?:\\s|$|-)/.test(cls)) return true;
              if (n.getAttribute && /\\bcompleted\\b/i.test(n.getAttribute('title')||n.getAttribute('aria-label')||'')) return true;
              let st; try { st = getComputedStyle(n); } catch(e) { continue; }
              for (const prop of ['color','fill']) {
                const m = (st[prop]||'').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (!m) continue;
                const r=+m[1],g=+m[2],b=+m[3];
                if (g>=130 && g>r+40 && g>b+40) return true;
              }
            }
            return false;
          }
          function ownLock(el) {
            // Sadece bu satırın içeriği — alt/üst section kilidini alma
            const row = el.closest('.courseindex-item');
            if (!row) return false;
            const content = row.querySelector(':scope > .courseindex-item-content') || row;
            // nested courseindex-item içindeki kilitleri sayma
            for (const ic of content.querySelectorAll('i.fa-lock, .fa-lock, [data-flex-icon="lock"]')) {
              if (ic.closest('.courseindex-item') === row) return true;
            }
            const t = ((el.getAttribute('aria-label')||'')+' '+(el.getAttribute('title')||'')).toLowerCase();
            if (/\\blocked\\b|not available/.test(t)) return true;
            return el.getAttribute('aria-disabled') === 'true';
          }
          function isSectionOnly(text) {
            if (/^[IVXLCDM]+\\.\\s/i.test(text)) return true;
            if (/^\\d+[.)]\\s/.test(text)) return false;
            if (/\\bquiz\\b|final test/i.test(text)) return false;
            if (/^(fundamentals|the vdu workstation|health and work|hazards|prevention|definitions|signs|fires|stress|emergency management)/i.test(text)) return true;
            return false;
          }
          const roots = [];
          for (const sel of selectors) document.querySelectorAll(sel).forEach(el => roots.push(el));
          document.querySelectorAll('.courseindex, #courseindex, [data-region="courseindex"]').forEach(el => {
            if (!roots.includes(el)) roots.push(el);
          });
          const seen = new Set();
          const items = [];
          const scan = (root) => {
            for (const el of root.querySelectorAll('a.courseindex-link, .courseindex-item a, a')) {
              let text = (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g,' ');
              if (!text || text.length > 100) continue;
              const low = text.toLowerCase();
              if (seen.has(low)) continue;
              if (/^(home|dashboard|my courses|side panel|more|close|user menu)/i.test(text)) continue;
              if (text.length > 70 && !/\\bquiz\\b/i.test(text) && !/^\\d+[.)]\\s/.test(text)) continue;
              const ok = /^\\d+[.)\\s]/.test(text) ||
                /(contents|fundamentals|workstation|health|quiz|vdu|hazard|electric|prevention|signs|fires|stress|ladder|transport|emergency|final test)/i.test(text);
              if (!ok) continue;
              if (isSectionOnly(text)) continue;
              seen.add(low);
              const cls = ((el.className||'')+' '+(el.parentElement?.className||'')).toLowerCase();
              const aria = (el.getAttribute('aria-current')||'').toLowerCase();
              const current = aria==='page'||aria==='true'||cls.includes('active')||cls.includes('current');
              let href = (el.href || '').trim();
              if (href.startsWith('javascript:')) href = '';
              // Gerçek aktivite linki varsa kilit ikonunu yok say (tıklanabilir)
              const hasMod = /\\/mod\\/(quiz|lesson|resource|page|book|url)\\//i.test(href);
              let locked = ownLock(el) && !hasMod;
              if (current) locked = false;
              items.push({
                text,
                done: isGreen(el) || isGreen(el.parentElement),
                current,
                isQuiz: /\\bquiz\\b|final test/i.test(text),
                locked,
                href
              });
              if (items.length >= 80) return;
            }
          };
          (roots.length ? roots : [document.body]).forEach(scan);
          return items;
        }""",
        selectors,
    )
    return [
        TocItem(
            text=it["text"],
            done=bool(it.get("done")),
            current=bool(it.get("current")),
            is_quiz=bool(it.get("isQuiz")),
            locked=bool(it.get("locked")),
            href=str(it.get("href") or ""),
        )
        for it in (raw or [])
    ]


def first_todo(items: list[TocItem]) -> TocItem | None:
    """İlk yapılmamış madde. Öncekiler yeşilse sıradaki (quiz) kilitli görünse de alınır."""
    for it in items:
        if it.current and not it.done:
            return it
    for it in items:
        if not it.done and not it.locked:
            return it
    # 1..N yeşil → sıradaki Quiz(I) vb.
    for i, it in enumerate(items):
        if it.done:
            continue
        if i == 0 or all(x.done for x in items[:i]):
            return it
        if it.href and "/mod/" in it.href:
            return it
    return None


def spend_still_required(text: str, patterns: list[str]) -> float | None:
    """Spend X dk isteniyor ve henüz Done değilse saniye döner."""
    spend = parse_spend_sec(text, patterns)
    if not spend:
        return None
    # Completion: "Spend at least … Done" / yeşil tik
    if re.search(
        r"(?:spend\s+at\s+least|at\s+least\s+\d+\s+min).{0,120}\bDone\b",
        text,
        re.I | re.S,
    ):
        return None
    if re.search(r"\bDone\b.{0,80}(?:spend\s+at\s+least|minutes?\s+on\s+this)", text, re.I | re.S):
        return None
    return spend


def quiz_failed_on_view(text: str) -> bool:
    if re.search(r"\bFailed\b", text, re.I) and re.search(
        r"passing grade|grade to pass", text, re.I
    ):
        return True
    return False


def quiz_passed_on_view(text: str) -> bool:
    if quiz_failed_on_view(text):
        return False
    if re.search(r"congratulations|passed|you have completed", text, re.I):
        return True
    # "Receive a passing grade" + Done (Failed yok)
    if re.search(r"Receive a passing grade", text, re.I) and re.search(
        r"Receive a passing grade.{0,80}\bDone\b|\bDone\b.{0,40}passing grade",
        text,
        re.I | re.S,
    ):
        return True
    return False


def open_toc(page: Page, text: str, href: str = "") -> bool:
    """Önce href ile git (mod/quiz|lesson); yoksa courseindex tam isim tıkla. Home YOK."""
    h = (href or "").strip()
    if h and re.search(r"/mod/(quiz|lesson|resource|page|book)/", h, re.I):
        if is_site_home(h):
            return False
        try:
            print(f"  → href: {h[:90]}")
            page.goto(h, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
            return not is_site_home(page.url or "")
        except Exception as e:
            print(f"  href goto hata: {e}")

    return bool(
        page.evaluate(
            """(want) => {
              const w = want.trim().toLowerCase().replace(/\\s+/g,' ');
              if (!w) return false;
              const ban = /^(home|dashboard|my courses|site home|log in|log out|user menu|side panel|more|close)$/i;
              const roots = Array.from(document.querySelectorAll(
                '#courseindex, .courseindex, [data-region="courseindex"], .courseindex-content'
              ));
              if (!roots.length) return false;
              let best = null, bestSc = 0, bestHref = '';
              for (const root of roots) {
                for (const el of root.querySelectorAll('a.courseindex-link, .courseindex-item a, a')) {
                  const t = (el.innerText||el.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').toLowerCase();
                  if (!t || ban.test(t)) continue;
                  let sc = 0;
                  if (t === w) sc = 100;
                  else if (t.length >= 8 && w.length >= 8 && (t === w || t.startsWith(w) || w.startsWith(t))) sc = 80;
                  if (sc > bestSc) { bestSc = sc; best = el; bestHref = el.href || ''; }
                }
              }
              if (!best || bestSc < 80) return false;
              if (/ole\\.unibz\\.it\\/?$/i.test(bestHref) || /\\/(home|login)/i.test(bestHref)) return false;
              best.scrollIntoView({block:'center'});
              best.click();
              return true;
            }""",
            text,
        )
    )


def is_site_home(url: str) -> bool:
    path = urllib.parse.urlparse(url or "").path or "/"
    host = (urllib.parse.urlparse(url or "").netloc or "").lower()
    return "ole.unibz.it" in host and path in ("", "/")


def lesson_has_nav(page: Page, cfg: dict) -> bool:
    labs = list(cfg.get("next_labels") or []) + list(cfg.get("end_labels") or [])
    return has_button(page, labs)


def is_real_quiz_page(page: Page, current: TocItem | None) -> bool:
    """Messaging drawer / rastgele checkbox = quiz DEĞİL."""
    url = (page.url or "").lower()
    if "/mod/quiz/" in url:
        return True
    if has_button(page, ["Attempt quiz", "Re-attempt quiz", "Continue your attempt"]):
        return True
    # Sol menüde Quiz seçili + ana alanda quiz giriş/soru
    if current and current.is_quiz:
        return True
    try:
        # Moodle quiz soruları genelde #responseform veya .que içinde
        if page.locator("#responseform, .que, form#responseform").count() > 0:
            return True
    except Exception:
        pass
    return False


def countdown(seconds: float) -> None:
    """Tam bekle — Next göründü diye erken çıkma."""
    rem = int(max(0, seconds))
    while rem > 0:
        h, r = divmod(rem, 3600)
        m, s = divmod(r, 60)
        print(f"\r  Bekleniyor {h:02d}:{m:02d}:{s:02d}   ", end="", flush=True)
        time.sleep(1)
        rem -= 1
    if seconds > 0:
        print("\r  Bekleme bitti.                 ")


def do_lesson_clicks(page: Page, cfg: dict) -> int:
    """Next oldukça Next; End varsa bir kez End. End yoksa sorun değil."""
    n = 0
    after = int(cfg.get("after_click_wait_ms") or 1600)
    for _ in range(40):
        hit = click_by_labels(page, cfg.get("next_labels") or ["Next", "Next page", "Continue"])
        if not hit:
            break
        print(f"  → Next ({hit})")
        n += 1
        page.wait_for_timeout(after)
    end = click_by_labels(page, cfg.get("end_labels") or ["End", "End of lesson", "Finish"])
    if end:
        print(f"  → End ({end})")
        n += 1
        page.wait_for_timeout(after)
    else:
        print("  → End yok (normal olabilir)")
    return n


FORBIDDEN_QUIZ_CLICKS = {
    "back",
    "previous",
    "cancel",
    "flag question",
    "home",
    "dashboard",
    "log out",
    "logout",
}


def extract_quiz_questions(page: Page) -> list[dict[str, Any]]:
    """Sadece Moodle .que soru kutuları — Back/nav yok."""
    return page.evaluate(
        """() => {
          const labOf = (inp) => {
            const byFor = inp.id ? document.querySelector('label[for=\"'+inp.id+'\"]') : null;
            let lab = (inp.closest('label')?.innerText || byFor?.innerText || '').trim();
            if (!lab) {
              const row = inp.closest('[class*=\"r0\"],[class*=\"r1\"],.answer,.d-flex') || inp.parentElement;
              if (row) {
                const num = (row.querySelector('.answernumber')?.innerText || '').trim();
                let fill = (row.querySelector('.flex-fill')?.innerText || '').trim();
                if (!fill) fill = (row.innerText || '').replace(num, '').trim();
                lab = (num + ' ' + fill).trim();
              }
            }
            if (!lab) lab = String(inp.value || '');
            return lab.replace(/\\s+/g, ' ').trim().slice(0, 200);
          };
          const root = document.querySelector('#responseform, form#responseform, #region-main') || document.body;
          const ques = [];
          root.querySelectorAll('.que').forEach((que, qi) => {
            const qtext = (que.querySelector('.qtext')?.innerText || que.innerText || '')
              .trim().replace(/\\s+/g, ' ').slice(0, 500);
            const opts = [];
            que.querySelectorAll('input[type="radio"], input[type="checkbox"]').forEach((inp, oi) => {
              if (inp.disabled) return;
              let lab = labOf(inp);
              if (!lab) return;
              // value-only (0/1/2) ise harf öneki ekle
              if (/^\\d+$/.test(lab)) {
                lab = String.fromCharCode(97 + oi) + '. ' + lab;
              }
              opts.push({
                name: inp.name || '', value: inp.value || '', label: lab,
                type: inp.type, index: oi
              });
            });
            if (opts.length) ques.push({ qid: qi, question: qtext.slice(0, 400), options: opts });
          });
          return ques;
        }"""
    ) or []


def apply_quiz_choice(page: Page, qid: int, choice_label: str) -> bool:
    """Şık bas: tam metin, harf (a/b/c), anahtar kelime veya index."""
    return bool(
        page.evaluate(
            """([qid, choice]) => {
              const wantRaw = (choice||'').trim();
              const want = wantRaw.toLowerCase();
              const ques = document.querySelectorAll('.que');
              const que = ques[qid];
              if (!que) return false;
              const inputs = Array.from(que.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
              const labOf = (inp) => {
                const byFor = inp.id ? document.querySelector('label[for=\"'+inp.id+'\"]') : null;
                let lab = (inp.closest('label')?.innerText || byFor?.innerText || '').trim();
                if (!lab) {
                  const row = inp.closest('[class*=\"r0\"],[class*=\"r1\"],.answer,.d-flex') || inp.parentElement;
                  if (row) {
                    const num = (row.querySelector('.answernumber')?.innerText || '').trim();
                    let fill = (row.querySelector('.flex-fill')?.innerText || '').trim();
                    if (!fill) fill = (row.innerText || '').replace(num, '').trim();
                    lab = (num + ' ' + fill).trim();
                  }
                }
                return (lab || String(inp.value||'')).replace(/\\s+/g,' ').trim();
              };
              const strip = (s) => s.replace(/^[a-d][\\.\\)\\:]\\s*/i, '').trim();
              const content = strip(want);
              // 1) metin / kelime skoru once
              if (content && content.length > 2) {
                for (const inp of inputs) {
                  const lab = labOf(inp);
                  const low = lab.toLowerCase();
                  const sc = strip(low);
                  if (sc && (sc === content || sc.includes(content) || content.includes(sc))) {
                    inp.click(); return true;
                  }
                  if (/\\btrue\\b/.test(want) && /\\btrue\\b/i.test(lab)) { inp.click(); return true; }
                  if (/\\bfalse\\b/.test(want) && /\\bfalse\\b/i.test(lab)) { inp.click(); return true; }
                }
                const words = content.split(/[^a-z0-9]+/).filter(w => w.length > 3);
                let best = null, bestSc = 0;
                for (const inp of inputs) {
                  const low = strip(labOf(inp).toLowerCase());
                  let sc = 0;
                  for (const w of words) if (low.includes(w)) sc++;
                  if (sc > bestSc) { bestSc = sc; best = inp; }
                }
                if (best && bestSc >= 2) { best.click(); return true; }
              }
              // 2) harf / index
              const letterM = want.match(/^([a-d])(?:[\\.\\)\\:]\\s*|\\s+)/) || want.match(/^([a-d])$/);
              if (letterM) {
                const letter = letterM[1];
                const idx = letter.charCodeAt(0) - 97;
                for (const inp of inputs) {
                  const low = labOf(inp).toLowerCase();
                  if (low.startsWith(letter + '.') || low.startsWith(letter + ')') || low.startsWith(letter + ' ')) {
                    inp.click(); return true;
                  }
                }
                if (idx >= 0 && idx < inputs.length) { inputs[idx].click(); return true; }
              }
              return false;
            }""",
            [qid, choice_label],
        )
    )


# VDU Quiz(I) bilinen doğru anahtarlar — Gemini metni uymasa bile şık skorla
VDU_LOCAL_HINTS: list[tuple[str, list[str]]] = [
    (r"hands on the keyboard|position of the hands", ["horizontal", "relaxed", "shoulder"]),
    (r"problems can arise|working at the VDU", ["visual", "musculoskeletal", "mental"]),
    (r"who must ensure|workplace is ergonomic", ["employer"]),
    (r"headset|telephone|phone", ["headset"]),
    (r"health surveillance|worker.*VDU|must.*worker", ["surveillance", "training", "information"]),
    (r"seat|chair|backrest", ["independently", "independent", "height"]),
    (r"gaze|window|screen.*window|direction", ["parallel"]),
    (r"non-ergonomic|incorrect use|frequent causes|disorders", ["true"]),
]


def local_pick_option(q: dict[str, Any]) -> str | None:
    """Soru metnine göre şık skorla — paraphrase'a bağımlı değil."""
    qtext = (q.get("question") or "").lower()
    opts = q.get("options") or []
    if not opts:
        return None
    for pat, keys in VDU_LOCAL_HINTS:
        if not re.search(pat, qtext, re.I):
            continue
        best_lab, best_sc = None, -1
        for opt in opts:
            lab = (opt.get("label") or "").lower()
            sc = sum(1 for k in keys if k in lab)
            # True/False
            if "true" in keys and re.search(r"\btrue\b", lab):
                sc += 2
            if sc > best_sc:
                best_sc, best_lab = sc, opt.get("label")
        if best_lab and best_sc > 0:
            return best_lab
    return None


def resolve_choice_label(q: dict[str, Any], choice: str) -> str | None:
    """Gemini cevabını Moodle şıkkına çevir: önce METİN, sonra harf."""
    choice = (choice or "").strip()
    if not choice:
        return None
    low = choice.lower()
    if low in FORBIDDEN_QUIZ_CLICKS or low in ("back", "previous", "flag"):
        return None
    opts = q.get("options") or []

    # "0"/"1" kör value eşlemesi YOK
    if re.fullmatch(r"\d+", choice):
        return None

    def _strip_letter(s: str) -> str:
        return re.sub(r"^[a-d][\.\)\:]\s*", "", s, flags=re.I).strip()

    content = _strip_letter(low)

    # 1) metin / anahtar kelime (harften bagimsiz — yanlis harf + dogru metin olur)
    if content and not re.fullmatch(r"[a-d]", content):
        for opt in opts:
            lab = (opt.get("label") or "").strip()
            ll = lab.lower()
            sc = _strip_letter(ll)
            if not sc:
                continue
            if sc == content or content in sc or sc in content:
                return lab
        words = [w for w in re.split(r"[^a-z0-9]+", content) if len(w) > 3]
        best_lab, best_sc = None, 0
        for opt in opts:
            lab = (opt.get("label") or "").strip()
            ll = _strip_letter(lab.lower())
            sc = sum(1 for w in words if w in ll)
            if sc > best_sc:
                best_sc, best_lab = sc, lab
        if best_lab and best_sc >= 2:
            return best_lab

    # 2) sadece harf veya metin tutmazsa harf index
    m = re.match(r"^([a-d])(?:[\.\)\:]\s*|\s+|$)", low)
    if m:
        letter = m.group(1)
        for opt in opts:
            lab = (opt.get("label") or "").strip()
            ll = lab.lower()
            if ll.startswith(f"{letter}.") or ll.startswith(f"{letter})") or ll.startswith(
                f"{letter} "
            ):
                return lab
        idx = ord(letter) - ord("a")
        if 0 <= idx < len(opts):
            return opts[idx].get("label") or None
    return None


def click_moodle_quiz_nav(page: Page, prefer_finish: bool = True) -> str | None:
    """
    Moodle quiz alt navigasyon:
      Finish attempt... / Next page / Next
    Ellipsis (... / …) ve value= ile bul.
    """
    scroll_all(page)
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(400)
    except Exception:
        pass

    hit = page.evaluate(
        """(preferFinish) => {
          const nodes = Array.from(document.querySelectorAll(
            'input[type="submit"], button, a.btn, a.mod_quiz-next-nav, #mod_quiz-next-nav, .submitbtns input, .submitbtns button'
          ));
          const score = (t) => {
            const s = t.toLowerCase();
            if (/finish\\s*attempt/.test(s)) return 100;
            if (/submit\\s*all\\s*and\\s*finish/.test(s)) return 95;
            if (/next\\s*page/.test(s)) return 50;
            if (/^next$/.test(s.trim())) return 40;
            return 0;
          };
          let best = null, bestScore = 0;
          for (const el of nodes) {
            const t = (el.value || el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g,' ');
            if (!t) continue;
            // Back asla
            if (/^back$/i.test(t) || /previous/i.test(t)) continue;
            const st = getComputedStyle(el);
            if (st.display==='none'||st.visibility==='hidden') continue;
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) continue;
            let sc = score(t);
            if (!preferFinish && sc >= 90) sc = 10; // finish'i ertele
            if (sc > bestScore) { bestScore = sc; best = el; }
          }
          if (!best) return null;
          best.scrollIntoView({block:'center'});
          best.click();
          return (best.value || best.innerText || '').trim().replace(/\\s+/g,' ');
        }""",
        prefer_finish,
    )
    return str(hit) if hit else None


def _click_submit_confirm_once(page: Page) -> str | None:
    """Tek bir onay butonu bas (modal veya sayfa)."""
    clicked = page.evaluate(
        """() => {
          const roots = [
            ...document.querySelectorAll('.modal.show, .modal.in, .moodle-dialogue, [role="dialog"], .confirmation-dialogue, .modal-content, .modal')
          ];
          roots.push(document.body);
          for (const root of roots) {
            for (const el of root.querySelectorAll('input[type="submit"], button, a.btn')) {
              const t = (el.value || el.innerText || '').trim().replace(/\\s+/g,' ');
              if (!/submit\\s*all\\s*and\\s*finish|\\byes\\b|\\bconfirm\\b/i.test(t)) continue;
              if (/cancel|\\bno\\b|close/i.test(t)) continue;
              const st = getComputedStyle(el);
              if (st.display==='none'||st.visibility==='hidden') continue;
              const r = el.getBoundingClientRect();
              if (r.width < 2 || r.height < 2) continue;
              el.click();
              return t;
            }
          }
          return null;
        }"""
    )
    if clicked:
        return str(clicked)
    for lab in (
        "Submit all and finish",
        "Submit all and finish...",
        "Submit all and finish…",
        "Yes",
    ):
        if click_by_labels(page, [lab]):
            return lab
    return None


def confirm_submit_modal(page: Page, max_rounds: int = 3) -> bool:
    """Moodle onay penceresi — kullanıcıya sormadan; iç içe 2. diyalog da basılır."""
    any_ok = False
    for _ in range(max_rounds):
        hit = _click_submit_confirm_once(page)
        if not hit:
            break
        any_ok = True
        print(f"  → onay (otomatik): {hit}")
        page.wait_for_timeout(1200)
    return any_ok


def _attempt_id(url: str) -> str:
    m = re.search(r"attempt=(\d+)", url or "", re.I)
    return m.group(1) if m else (url or "")


def finish_or_next_quiz(page: Page, cfg: dict) -> bool:
    """Cevaplardan sonra: Finish attempt → otomatik onay. Döngü yok."""
    after = int(cfg.get("after_click_wait_ms") or 1600)
    url = (page.url or "").lower()

    # Zaten summary: Submit + onaylar + review beklenmesi
    if "summary.php" in url:
        print("  Summary — Submit all and finish (otomatik, senden onay yok)…")
        hit = click_moodle_quiz_nav(page, prefer_finish=True)
        if hit:
            print(f"  → {hit}")
            page.wait_for_timeout(after)
        else:
            for lab in ("Submit all and finish", "Submit all and finish...", "Finish attempt"):
                if click_by_labels(page, [lab]):
                    print(f"  → {lab}")
                    page.wait_for_timeout(after)
                    break
        confirm_submit_modal(page, max_rounds=3)
        # review'e düşmesini bekle
        for _ in range(12):
            page.wait_for_timeout(500)
            u = (page.url or "").lower()
            if "review.php" in u or ("view.php" in u and "summary" not in u):
                print(f"  → quiz bitti: {page.url}")
                return True
            confirm_submit_modal(page, max_rounds=1)
        return True

    # review / view = quiz bitmiş
    if "review.php" in url or ("view.php" in url and "attempt=" not in url):
        print("  Quiz bitmiş görünüyor (review/view)")
        return False

    print("  Finish attempt / Next aranıyor…")
    hit = click_moodle_quiz_nav(page, prefer_finish=True)
    if hit and re.search(r"finish|submit\s*all", hit, re.I):
        print(f"  → {hit}")
        page.wait_for_timeout(after)
        confirm_submit_modal(page, max_rounds=3)
        return True

    for lab in (
        "Finish attempt…",
        "Finish attempt...",
        "Finish attempt",
        "Submit all and finish…",
        "Submit all and finish...",
        "Submit all and finish",
    ):
        if click_by_labels(page, [lab]):
            print(f"  → {lab}")
            page.wait_for_timeout(after)
            confirm_submit_modal(page, max_rounds=3)
            return True

    hit = click_moodle_quiz_nav(page, prefer_finish=False)
    if hit:
        print(f"  → {hit}")
        page.wait_for_timeout(after)
        return True
    for lab in ("Next page", "Next"):
        if click_by_labels(page, [lab]):
            print(f"  → {lab}")
            page.wait_for_timeout(after)
            return True

    print("  Finish/Next bulunamadı")
    return False


QUIZ_SYSTEM = """You are an expert examiner for Italian/EU workplace safety e-learning (VDU, D.Lgs 81/08, ergonomics).
For EACH question choose the SINGLE best option. Return the FULL option label text EXACTLY as in options (e.g. "a. ...").
FORBIDDEN: "0","1","2", bare letters alone without text, always picking the first option, Back/Previous/Cancel/Flag.
Read EVERY option; pick the scientifically correct one — not the first listed.

Domain facts (use these when options conflict):
- Keyboard hands: wrists/articulations HORIZONTAL (not flexed up/down); fingers relaxed; shoulders NOT raised.
- Screen vs window: gaze direction PARALLEL to the window (reduces glare) — NOT facing the window, NOT with window behind screen if that causes glare conflict in options preferring parallel.
- Chair: seat HEIGHT adjustable INDEPENDENTLY of the backrest; backrest supports pelvis + lumbar + shoulders.
- Employer must ensure the workstation is ergonomic / compliant.
- Frequent causes of VDU disorders: non-ergonomic workstations AND incorrect use of equipment.
- Prefer neutral posture, adjustable furniture, break micro-pauses, reduced glare/contrast issues.

JSON only:
{"answers":[{"qid":0,"choice":"a. exact full label"},{"qid":1,"choice":"c. exact full label"}],"reason":"one line"}"""

def do_quiz(page: Page, cfg: dict) -> bool:
    """Soruları cevapla → Finish attempt. Back'e ASLA basma."""
    after = int(cfg.get("after_click_wait_ms") or 1600)

    # Giriş / devam
    if "/mod/quiz/view.php" in (page.url or "") or has_button(
        page, ["Attempt quiz", "Continue your attempt", "Re-attempt quiz"]
    ):
        for lab in ("Continue your attempt", "Attempt quiz", "Re-attempt quiz", "Start attempt"):
            if click_by_labels(page, [lab]):
                print(f"  → {lab}")
                page.wait_for_timeout(after)
                click_by_labels(page, ["Start attempt", "Continue", "OK", "Begin"])
                page.wait_for_timeout(after)
                break

    if "/mod/quiz/attempt.php" not in (page.url or "") and not page.locator(".que").count():
        # özet / review sayfası olabilir
        if "/mod/quiz/" in (page.url or ""):
            return finish_or_next_quiz(page, cfg)
        return False

    questions = extract_quiz_questions(page)
    print(f"  Sayfadaki soru: {len(questions)}")
    if not questions:
        print("  Soru kutusu (.que) yok — Finish/Next deneniyor")
        return finish_or_next_quiz(page, cfg)

    for q in questions[:3]:
        opts_preview = " | ".join((o.get("label") or "")[:40] for o in (q.get("options") or [])[:3])
        print(f"  örnek Q{q.get('qid')}: {(q.get('question') or '')[:70]}…")
        print(f"    şıklar: {opts_preview}")

    prefer = str(cfg.get("quiz_openai_model") or "gemini-3.5-flash-lite")
    print("  Sorular Gemini'ye gönderiliyor (cevap bekleniyor)…")
    data = llm_json(
        cfg,
        system=QUIZ_SYSTEM,
        user=json.dumps({"questions": questions}, ensure_ascii=False),
        prefer_model=prefer,
    )

    answered = 0
    done_ids: set[int] = set()

    def _apply_answers(blob: dict[str, Any] | None, tag: str) -> int:
        n = 0
        if not blob or not blob.get("answers"):
            return 0
        if blob.get("reason"):
            print(f"  Gemini gerekçe ({tag}): {str(blob.get('reason'))[:160]}")
        for ans in blob["answers"]:
            try:
                qid = int(ans.get("qid"))
            except Exception:
                continue
            if qid in done_ids:
                continue
            raw = str(
                ans.get("choice") or ans.get("label") or ans.get("answer") or ""
            ).strip()
            q = next((x for x in questions if int(x["qid"]) == qid), None)
            if not q:
                continue
            choice = resolve_choice_label(q, raw)
            if not choice:
                # ham harf ile dene
                choice = raw
            if apply_quiz_choice(page, qid, choice):
                print(f"  Q{qid} → {choice[:90]}")
                n += 1
                done_ids.add(qid)
                page.wait_for_timeout(200)
            else:
                print(f"  Q{qid} şık bulunamadı: {raw!r}")
                labs = [o.get("label") for o in (q.get("options") or [])]
                print(f"    mevcut: {labs}")
        return n

    if data and data.get("answers"):
        answered += _apply_answers(data, "1")
    else:
        print("  Gemini cevap gelmedi")

    # Yerel VDU anahtarları (eksikler)
    for q in questions:
        qid = int(q["qid"])
        if qid in done_ids:
            continue
        pick = local_pick_option(q)
        if pick and apply_quiz_choice(page, qid, pick):
            print(f"  Q{qid} (yerel) → {pick[:90]}")
            answered += 1
            done_ids.add(qid)

    # T/F yedek
    for q in questions:
        qid = int(q["qid"])
        if qid in done_ids:
            continue
        labels = [o["label"] for o in q.get("options") or []]
        if len(labels) == 2 and all(re.search(r"\b(true|false)\b", lab, re.I) for lab in labels):
            pick = next((lab for lab in labels if re.search(r"\btrue\b", lab, re.I)), labels[0])
            if apply_quiz_choice(page, qid, pick):
                print(f"  Q{qid} (yedek T/F) → {pick}")
                answered += 1
                done_ids.add(qid)

    for q in questions:
        qid = int(q["qid"])
        if qid not in done_ids:
            print(f"  Q{qid} cevapsız")

    print(f"  Cevaplanan: {answered}/{len(questions)}")
    if answered < max(1, len(questions) - 1):
        print("  Yetersiz cevap — Finish YOK, tekrar denenecek")
        return False

    page.wait_for_timeout(800)
    print("  Şimdi Finish attempt (otomatik, onay senden istenmez)…")
    moved = finish_or_next_quiz(page, cfg)
    if not moved:
        page.wait_for_timeout(1000)
        moved = finish_or_next_quiz(page, cfg)
    return answered > 0 or moved

def pick_page(context, _hint: str) -> Page | None:
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        return None
    print("Sekmeler:")
    for i, p in enumerate(pages):
        print(f"  [{i}] {p.url}")
    for p in pages:
        u = (p.url or "").lower()
        if "mod/quiz" in u or "mod/lesson" in u or "ole.unibz.it" in u:
            return p
    return pages[-1]


def _goto_quiz_view(page: Page) -> bool:
    m = re.search(r"cmid=(\d+)", page.url or "")
    if not m:
        return False
    try:
        page.goto(
            f"https://ole.unibz.it/mod/quiz/view.php?id={m.group(1)}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        print(f"  view.php hata: {e}")
        return False


def run(cfg: dict, start_url: str) -> int:
    print("=" * 50)
    print(f" BOT {VERSION}")
    print(" Akış: TOC yukarı→aşağı | bekle | Next/End | Quiz→Gemini→Finish")
    print("=" * 50)

    attach = bool(cfg.get("attach_to_chrome", True))
    cdp = (cfg.get("cdp_url") or "http://127.0.0.1:9222").rstrip("/")
    after = int(cfg.get("after_click_wait_ms") or 1600)
    waited: set[str] = set()
    patterns = cfg.get("spend_patterns") or []
    submitted_attempts: set[str] = set()

    with sync_playwright() as p:
        browser = None
        if attach:
            print(f"Chrome bağlanıyor: {cdp}")
            try:
                browser = p.chromium.connect_over_cdp(cdp)
            except Exception as e:
                print(f"Bağlanamadı: {e}\nÖnce 1_chrome_ac.bat")
                return 1
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = pick_page(context, start_url) or context.new_page()
        else:
            browser = p.chromium.launch(channel="chrome", headless=False)
            context = browser.new_context(no_viewport=True)
            page = context.new_page()
            page.goto(start_url, wait_until="domcontentloaded", timeout=120000)

        print(f"Sekme: {page.url}")
        print("Gemini sadece QUIZ sayfasında çalışır; lesson'da kapalı.")
        while True:
            pause("Ders veya Quiz sekmesini aç (ana sayfa değil), ENTER…")
            page = pick_page(context, start_url) or page
            if not is_site_home(page.url or "") and (
                "/mod/lesson/" in (page.url or "")
                or "/mod/quiz/" in (page.url or "")
                or list_toc(page, cfg)
            ):
                break
            print("  Hâlâ ana sayfa / TOC yok — dersi açıp tekrar ENTER.")

        items = list_toc(page, cfg)
        print("\nSol menü (yukarı→aşağı):")
        for it in items:
            tag = (
                "YEŞİL"
                if it.done
                else ("KİLİT" if it.locked else ("QUIZ" if it.is_quiz else "AÇIK"))
            )
            print(f"  [{tag}] {it.text}{' *' if it.current else ''}")
        if not items:
            print("  (TOC yok — bu sayfada Next/End)")

        stagnant = 0
        last_url = ""
        last_good_url = page.url or ""
        for step in range(1, int(cfg.get("max_steps") or 300) + 1):
            page.wait_for_timeout(350)
            url_now = page.url or ""
            url_l = url_now.lower()

            if is_site_home(url_now):
                print(f"\n--- Adım {step} | {url_now}")
                print("  Ana sayfa — geri dön")
                try:
                    if last_good_url and not is_site_home(last_good_url):
                        page.goto(
                            last_good_url, wait_until="domcontentloaded", timeout=60000
                        )
                    else:
                        page.go_back(wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    print(f"  Geri hata: {e}")
                    stagnant += 1
                continue

            if "/mod/" in url_l:
                last_good_url = url_now

            items = list_toc(page, cfg)
            current = next((x for x in items if x.current), None)
            todo = first_todo(items)
            text = body_text(page)

            print(f"\n--- Adım {step} | {page.url}")
            if current:
                print(f"  Şu an: {current.text}")
            if todo:
                print(f"  Sıradaki: {todo.text}{' (quiz→Gemini)' if todo.is_quiz else ''}")
            else:
                print("  TOC sıradaki yok")

            # ---------- 1) LESSON önce (Gemini YOK) ----------
            if "/mod/lesson/" in url_l:
                spend = spend_still_required(text, patterns)
                if spend:
                    key = f"{re.sub(r'[?#].*', '', page.url)}|{int(spend)}"
                    if key not in waited:
                        wait_s = (
                            spend + float(cfg.get("wait_extra_minutes") or 1) * 60
                        ) * float(cfg.get("wait_scale") or 1)
                        print(f"  Spend bekle: {wait_s/60:.1f} dk (Gemini kapalı)")
                        countdown(wait_s)
                        waited.add(key)
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=120000)
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        stagnant = 0
                        continue

                print("  Lesson: Next / End (Gemini kapalı)")
                clicked = do_lesson_clicks(page, cfg)
                if clicked > 0:
                    stagnant = 0
                    last_url = page.url
                    continue

                # Next yok → sıradaki TOC (quiz olabilir)
                items = list_toc(page, cfg)
                nxt = first_todo(items)
                if nxt and (not current or current.text != nxt.text or current.done):
                    print(f"  Sonraki maddeye geç: {nxt.text}")
                    before = page.url
                    ok = open_toc(page, nxt.text, nxt.href)
                    page.wait_for_timeout(after)
                    if is_site_home(page.url or ""):
                        print("  Home'a düştü — geri, lesson'da kal")
                        try:
                            page.goto(before, wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            pass
                        stagnant += 1
                    elif ok:
                        stagnant = 0
                    else:
                        stagnant += 1
                    continue

                stagnant += 1
                if stagnant >= int(cfg.get("max_stagnant_steps") or 15):
                    print("Lesson takıldı.")
                    break
                continue

            # ---------- 2) QUIZ (Gemini sadece burada) ----------
            if "/mod/quiz/" in url_l or has_button(
                page, ["Attempt quiz", "Re-attempt quiz", "Continue your attempt"]
            ):
                aid = _attempt_id(page.url or "")

                if "review.php" in url_l:
                    print("  Quiz review → view")
                    if aid:
                        submitted_attempts.add(aid)
                    _goto_quiz_view(page)
                    stagnant = 0
                    continue

                if "summary.php" in url_l:
                    if aid and aid in submitted_attempts:
                        confirm_submit_modal(page, max_rounds=2)
                        _goto_quiz_view(page)
                    else:
                        print("  Summary → Submit")
                        if aid:
                            submitted_attempts.add(aid)
                        finish_or_next_quiz(page, cfg)
                    stagnant = 0
                    continue

                items = list_toc(page, cfg)
                cur = next((x for x in items if x.current), None)
                if (cur and cur.done) or (
                    quiz_passed_on_view(text) and not quiz_failed_on_view(text)
                ):
                    print("  Quiz geçildi → sonraki")
                    nxt = first_todo(items)
                    if nxt:
                        open_toc(page, nxt.text, nxt.href)
                        page.wait_for_timeout(after)
                    stagnant = 0
                    continue

                if has_button(
                    page, ["Continue your attempt", "Re-attempt quiz", "Attempt quiz"]
                ):
                    print("  Quiz Attempt / Re-attempt")
                    for lab in (
                        "Continue your attempt",
                        "Re-attempt quiz",
                        "Attempt quiz",
                        "Start attempt",
                    ):
                        if click_by_labels(page, [lab]):
                            print(f"  → {lab}")
                            page.wait_for_timeout(after)
                            click_by_labels(
                                page, ["Start attempt", "Continue", "OK", "Begin"]
                            )
                            page.wait_for_timeout(after)
                            break
                    stagnant = 0
                    continue

                if "/mod/quiz/attempt.php" in url_l or page.locator(".que").count():
                    print("  >>> Gemini quiz çözüyor")
                    if do_quiz(page, cfg):
                        if "summary.php" in (page.url or "").lower():
                            submitted_attempts.add(_attempt_id(page.url or ""))
                        stagnant = 0
                    else:
                        stagnant += 1
                    continue

                stagnant += 1
                continue

            # ---------- 3) Diğer: TOC ----------
            if todo:
                print(f"  Menüden aç: {todo.text}")
                before = page.url
                open_toc(page, todo.text, todo.href)
                page.wait_for_timeout(after)
                if is_site_home(page.url or ""):
                    try:
                        page.goto(before, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    stagnant += 1
                else:
                    stagnant = 0
                continue

            stagnant += 1
            last_url = page.url
            if stagnant >= int(cfg.get("max_stagnant_steps") or 15):
                print("Takıldı — çıkılıyor.")
                break

        pause("Bitti. Enter…")
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        if attach:
            print("Chrome açık bırakıldı.")
    return 0


def main() -> int:
    load_secrets()
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--attach", action="store_true")
    ap.add_argument("--no-attach", action="store_true")
    args = ap.parse_args()
    cfg = load_yaml(Path(args.config))
    if args.scale is not None:
        cfg["wait_scale"] = args.scale
    if args.attach:
        cfg["attach_to_chrome"] = True
    if args.no_attach:
        cfg["attach_to_chrome"] = False
    url = (args.url or os.environ.get("START_URL") or "").strip() or "https://ole.unibz.it/"
    print(f"Gemini key: {'VAR' if gemini_key(cfg) else 'YOK'}")
    try:
        return run(cfg, url)
    except Exception as e:
        print(f"CRASH: {e}")
        import traceback
        traceback.print_exc()
        pause()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
