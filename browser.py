"""CDP bağlantısı ve güvenli sayfa yardımcıları."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import yaml
from playwright.sync_api import Browser, BrowserContext, Page, Playwright

ROOT = Path(__file__).resolve().parent

# Windows konsolu (cp1254) basamadığı karakterde çökmesin
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_secrets() -> None:
    for name in ("secrets.env", ".env"):
        p = ROOT / name
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and not os.environ.get(k):
                os.environ[k] = v
        return


STOP = threading.Event()  # Ctrl+C → tüm kurs döngüleri durur
_tls = threading.local()
_print_lock = threading.Lock()
_parallel = False


def set_parallel(on: bool) -> None:
    global _parallel
    _parallel = on


def is_parallel() -> bool:
    return _parallel


def set_log_prefix(prefix: str) -> None:
    _tls.prefix = prefix


def log(msg: str) -> None:
    prefix = getattr(_tls, "prefix", "")
    if prefix:
        lead = len(msg) - len(msg.lstrip("\n"))
        msg = "\n" * lead + prefix + msg[lead:]
    with _print_lock:
        print(msg, flush=True)


def keep_alive(page: Page | None) -> None:
    """Uzun beklemede sekme uyumasın: küçük scroll."""
    if page is None:
        return
    try:
        page.evaluate("() => { window.scrollBy(0, 1); window.scrollBy(0, -1); }")
    except Exception:
        pass


def connect(pw: Playwright, cdp_url: str) -> tuple[Browser, BrowserContext]:
    browser = pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    return browser, context


def pick_page(context: BrowserContext) -> Page | None:
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        return None
    for key in ("/mod/quiz/", "/mod/lesson/", "/mod/", "/course/view.php", "ole.unibz.it"):
        for p in pages:
            if key in (p.url or ""):
                return p
    return pages[-1]


def page_course_id(page: Page) -> str | None:
    """Sekmenin hangi kursa ait olduğu (URL veya <body class="course-123">)."""
    url = page.url or ""
    if "/course/view.php" in url:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if q.get("id"):
            return q["id"][0]
    try:
        cid = page.evaluate(
            """() => { const m = (document.body.className || '').match(/\\bcourse-(\\d+)\\b/); return m ? m[1] : null; }"""
        )
    except Exception:
        return None
    return cid if cid and cid != "1" else None


def find_course_page(context: BrowserContext, course_id: str) -> Page | None:
    for p in context.pages:
        if not p.is_closed() and page_course_id(p) == course_id:
            return p
    return None


def body_text(page: Page) -> str:
    for sel in ("#region-main", "main", "body"):
        try:
            return page.locator(sel).first.inner_text(timeout=3000)
        except Exception:
            continue
    return ""


def settle(page: Page, ms: int = 800) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def wait_url_change(page: Page, old_url: str, timeout_s: float = 20) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        if (page.url or "") != old_url:
            settle(page, 600)
            return True
        page.wait_for_timeout(300)
    return False


def goto(page: Page, url: str) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        settle(page, 800)
        return True
    except Exception as e:
        log(f"  goto hata: {e}")
        return False


_CLICK_JS = """([patterns, scopeSel, doClick]) => {
  const res = patterns.map(p => new RegExp(p, 'i'));
  const forbid = /^\\s*(back|previous|previous page|cancel|log ?out|home|dashboard|flag question)\\s*$/i;
  const root = (scopeSel && document.querySelector(scopeSel)) || document;
  const els = root.querySelectorAll('input[type="submit"], input[type="button"], button, a.btn, a[role="button"], a');
  for (const re of res) {
    for (const el of els) {
      const t = (el.value || el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      if (!t || forbid.test(t) || !re.test(t)) continue;
      if (el.disabled) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      if (doClick) {
        el.scrollIntoView({block: 'center'});
        el.click();
      }
      return t;
    }
  }
  return null;
}"""


def click_text(page: Page, patterns: list[str], scope: str | None = None) -> str | None:
    """Regex listesindeki sırayla ilk görünür butonu JS ile tıkla."""
    try:
        hit = page.evaluate(_CLICK_JS, [patterns, scope, True])
    except Exception:
        return None
    return str(hit) if hit else None


def has_text_button(page: Page, patterns: list[str], scope: str | None = None) -> bool:
    try:
        return bool(page.evaluate(_CLICK_JS, [patterns, scope, False]))
    except Exception:
        return False


def js_click_selector(page: Page, selector: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """(sel) => {
                  const el = document.querySelector(sel);
                  if (!el || el.disabled) return false;
                  el.scrollIntoView({block: 'center'});
                  el.click();
                  return true;
                }""",
                selector,
            )
        )
    except Exception:
        return False


def cmid_from_page(page: Page) -> str | None:
    url = page.url or ""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "/view.php" in url and q.get("id"):
        return q["id"][0]
    if q.get("cmid"):
        return q["cmid"][0]
    try:
        return page.evaluate(
            """() => {
              const m = (document.body.className || '').match(/cmid-(\\d+)/);
              return m ? m[1] : null;
            }"""
        )
    except Exception:
        return None


def course_url_from_page(page: Page) -> str | None:
    try:
        cid = page.evaluate(
            """() => {
              const m = (document.body.className || '').match(/course-(\\d+)/);
              if (m && m[1] !== '1') return m[1];
              const a = document.querySelector('.breadcrumb a[href*="/course/view.php?id="], a[href*="/course/view.php?id="]');
              if (!a) return null;
              const mm = a.href.match(/id=(\\d+)/);
              return mm ? mm[1] : null;
            }"""
        )
    except Exception:
        cid = None
    return f"https://ole.unibz.it/course/view.php?id={cid}" if cid else None


def next_activity_href(page: Page) -> str | None:
    """Moodle 'sonraki aktivite' linki (sayfa altı)."""
    try:
        return page.evaluate(
            """() => {
              const el = document.querySelector('#next-activity-link, .activity-navigation a#next-activity-link, .activity-navigation .next a');
              return el && el.href ? el.href : null;
            }"""
        )
    except Exception:
        return None


_input_lock = threading.Lock()


def pause(msg: str) -> None:
    """Paralelde iki kurs aynı anda soru sormasın: sırayla, kurs etiketiyle."""
    with _input_lock:
        log(msg)
        try:
            if sys.stdin.isatty() and not STOP.is_set():
                input()
        except EOFError:
            pass
