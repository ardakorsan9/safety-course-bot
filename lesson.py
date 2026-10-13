"""Lesson / genel aktivite: süre bekle → Next → End. Sol menü kullanılmaz."""
from __future__ import annotations

import re
import time

from playwright.sync_api import Page

from browser import STOP, body_text, click_text, cmid_from_page, is_parallel, log, settle

NEXT_PATTERNS = [r"^next page\s*[»›>]*$", r"^next\s*[»›>]*$", r"^continue\s*[»›>]*$"]
END_PATTERNS = [r"^end of lesson$", r"^end$", r"^finish$"]
STATUS_RE = re.compile(r"^\s*(Done|To do|Todo|Failed)\s*:\s*(.+?)\s*$", re.I | re.M)
SPEND_RE = re.compile(r"spend\s+at\s+least\s+(\d+(?:[.,]\d+)?)\s*(minutes?|mins?|hours?)", re.I)


def completion_items(page: Page) -> list[tuple[str, str]]:
    """[('done'|'todo'|'failed', 'Spend at least 24 minutes...'), ...]"""
    text = ""
    try:
        text = page.evaluate(
            """() => {
              const el = document.querySelector('[data-region="activity-information"], .activity-information, .completion-info, [data-region="completionrequirements"]');
              return el ? el.innerText : '';
            }"""
        ) or ""
    except Exception:
        pass
    if not text:
        text = body_text(page)[:4000]
    out = []
    for m in STATUS_RE.finditer(text):
        st = m.group(1).lower().replace(" ", "")
        out.append(("todo" if st in ("todo",) else st, m.group(2)))
    return out


def all_done(page: Page) -> bool:
    items = completion_items(page)
    return bool(items) and all(s == "done" for s, _ in items)


def _spend_minutes(s: str) -> float | None:
    m = SPEND_RE.search(s)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v * 60 if m.group(2).lower().startswith("hour") else v


def countdown(seconds: float) -> None:
    """Paralelde iki kursun sayacı karışmasın diye her dakika ayrı satır yazar."""
    rem = int(max(0, seconds))
    while rem > 0 and not STOP.is_set():
        h, r = divmod(rem, 3600)
        mi, s = divmod(r, 60)
        if is_parallel():
            if rem % 60 == 0 or rem == int(seconds):
                log(f"  Bekleniyor {h:02d}:{mi:02d}:{s:02d}")
        else:
            print(f"\r  Bekleniyor {h:02d}:{mi:02d}:{s:02d}   ", end="", flush=True)
        time.sleep(1)
        rem -= 1
    if is_parallel():
        log("  Bekleme bitti.")
    else:
        print("\r  Bekleme bitti.                 ", flush=True)


class LessonRunner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.waited: dict[str, int] = {}  # cmid → bekleme turu
        self.after_ms = int(cfg.get("after_click_wait_ms") or 1600)

    def spend_wait(self, page: Page) -> bool:
        """Gerekirse bekler. Bekleme yaptıysa True."""
        cmid = cmid_from_page(page) or page.url
        sc = self.cfg.get("wait_scale")
        scale = 1.0 if sc is None else float(sc)
        ex = self.cfg.get("wait_extra_minutes")
        extra = (1.0 if ex is None else float(ex)) * 60

        status, minutes = None, None
        for st, txt in completion_items(page):
            mins = _spend_minutes(txt)
            if mins:
                status, minutes = st, mins
                break
        if minutes is None:
            mins = _spend_minutes(body_text(page)[:4000])
            if mins and cmid not in self.waited:
                status, minutes = "todo", mins
        if not minutes or status == "done":
            return False

        rounds = self.waited.get(cmid, 0)
        if rounds == 0:
            secs = (minutes * 60 + extra) * scale
            log(f"  Süre şartı: {minutes:.0f} dk → {secs/60:.1f} dk bekleniyor")
        elif rounds <= 5:
            secs = 60 * scale
            log("  Süre hâlâ 'To do' → +1 dk")
        else:
            log("  Süre şartı 5 ek turdan sonra hâlâ bitmedi — devam ediliyor")
            return False
        countdown(secs)
        self.waited[cmid] = rounds + 1
        try:
            page.reload(wait_until="domcontentloaded", timeout=120000)
        except Exception:
            pass
        settle(page, 1000)
        return True

    def step(self, page: Page) -> str:
        """Tek aksiyon. Dönüş: 'waited' | 'clicked' | 'done'."""
        if self.spend_wait(page):
            return "waited"

        txt = body_text(page)
        if re.search(r"start at the last page you saw", txt, re.I):
            hit = click_text(page, [r"^yes$"], "#region-main")
            if hit:
                log("  → Yes (kaldığı sayfadan devam)")
                settle(page, self.after_ms)
                return "clicked"

        hit = click_text(page, NEXT_PATTERNS, "#region-main")
        if hit:
            log(f"  → {hit}")
            settle(page, self.after_ms)
            return "clicked"

        hit = click_text(page, END_PATTERNS, "#region-main")
        if hit:
            log(f"  → {hit}")
            settle(page, self.after_ms)
            return "clicked"

        return "done"
