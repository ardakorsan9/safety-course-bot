"""
BOT v5 — URL tabanlı durum makinesi (ole.unibz.it Moodle)

  /mod/lesson/        → süre bekle → Next → End → sonraki aktivite
  /mod/quiz/view      → geçtiyse sonraki aktivite, değilse Attempt / Re-attempt
  /mod/quiz/attempt   → hafıza → Gemini → işaretle → Next page / Finish attempt
  /mod/quiz/summary   → Submit all and finish → modal onayı
  /mod/quiz/review    → doğru cevapları answers_cache.json'a yaz → view
  /course/view.php    → ilk tamamlanmamış, erişilebilir aktivite
  diğer /mod/         → süre varsa bekle → sonraki aktivite

Sonraki aktivite: önce sayfa altındaki Moodle linki, yoksa kurs ana sayfası.
Gemini yalnızca quiz'de ve hafızada olmayan sorular için çağrılır.
"""
from __future__ import annotations

import argparse
import threading
import time
import traceback
import urllib.parse

from playwright.sync_api import Page, sync_playwright

from browser import (
    STOP,
    cmid_from_page,
    connect,
    find_course_page,
    set_log_prefix,
    set_parallel,
    course_url_from_page,
    goto,
    load_config,
    load_secrets,
    log,
    next_activity_href,
    pause,
    pick_page,
    settle,
)
from lesson import LessonRunner, all_done, completion_items
from quiz import AnswerCache, QuizRunner, open_cache

VERSION = "v5.2-parallel"


def route(url: str) -> str:
    u = urllib.parse.urlparse(url or "")
    path = u.path.lower()
    if "ole.unibz.it" not in (u.netloc or "").lower():
        return "OTHER"
    if "/mod/quiz/attempt.php" in path:
        return "QUIZ_ATTEMPT"
    if "/mod/quiz/summary.php" in path:
        return "QUIZ_SUMMARY"
    if "/mod/quiz/review.php" in path:
        return "QUIZ_REVIEW"
    if "/mod/quiz/" in path:
        return "QUIZ_VIEW"
    if "/mod/lesson/" in path:
        return "LESSON"
    if "/course/view.php" in path:
        return "COURSE"
    if "/mod/" in path:
        return "ACTIVITY"
    return "HOME"


# Sol menü (courseindex). Kilit ikonu Moodle'da HER satırda vardır ve CSS ile
# gizlenir; bu yüzden ikonun varlığına değil görünürlüğüne / .restrictions'a bakılır.
# cm_completion data-value: 0 yapılmadı, 1 tamam, 2 tamam+geçti, 3 tamam+kaldı.
_INDEX_JS = r"""() => {
  const shown = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && !el.classList.contains('d-none');
  };
  const green = (it) => {
    for (const n of it.querySelectorAll('[data-for="cm_completion"] *, .completioninfo *, i, svg')) {
      const m = (getComputedStyle(n).color || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      if (m && +m[2] >= 110 && +m[2] > +m[1] + 30 && +m[2] > +m[3] + 20) return true;
    }
    return false;
  };
  const out = [];
  document.querySelectorAll('#courseindex [data-for="cm"], .courseindex [data-for="cm"]').forEach(it => {
    if (out.some(o => o.el === it)) return;
    const a = it.querySelector('a.courseindex-link[href], a[href*="/mod/"]');
    const name = ((it.querySelector('.courseindex-link') || it).textContent || '').replace(/\s+/g, ' ').trim();
    const restricted = it.classList.contains('restrictions') || shown(it.querySelector('.courseindex-locked')) || !a;
    const c = it.querySelector('[data-for="cm_completion"]');
    const val = c ? (c.getAttribute('data-value') || '') : '';
    const tracked = /^[0-3]$/.test(val);
    const done = tracked ? (val === '1' || val === '2') : green(it);
    out.push({ el: it, name, href: a ? a.href : '', restricted, val, tracked, done });
  });
  return out.map(({el, ...r}) => r);
}"""

# Ana içerik yedeği (sol menü yoksa). Moodle 4: "Done" düğmesi (iki nokta yok).
_COURSE_JS = r"""() => {
  document.querySelectorAll('.course-content .collapse:not(.show)').forEach(el => el.classList.add('show'));
  const out = [];
  document.querySelectorAll('li.activity').forEach(li => {
    const a = li.querySelector('a.aalink, .activityname a, a[href*="/mod/"]');
    const name = ((li.querySelector('.instancename, .activityname') || a || {}).textContent || '').replace(/\s+/g, ' ').trim();
    const comp = li.querySelector('[data-region="completion-info"], .activity-completion, .completion-info');
    const ct = ((comp || {}).textContent || '').replace(/\s+/g, ' ');
    const tracked = /\b(Done|To do|Failed|Mark as done)\b/i.test(ct);
    const done = tracked && /\bDone\b/i.test(ct) && !/\b(To do|Failed)\b/i.test(ct);
    const restricted = !a || /Not available unless/i.test(li.textContent || '');
    out.push({ name, href: a ? a.href : '', restricted, tracked, done, val: '' });
  });
  return out;
}"""


def scan_activities(page: Page) -> tuple[str, list[dict]]:
    """('index'|'content', aktiviteler) — sıra kurs sırasıdır."""
    for _ in range(10):
        try:
            acts = page.evaluate(_INDEX_JS) or []
        except Exception:
            acts = []
        if acts:
            return "index", acts
        page.wait_for_timeout(500)
    try:
        return "content", page.evaluate(_COURSE_JS) or []
    except Exception:
        return "content", []


def pick_activity(page: Page) -> tuple[str | None, dict]:
    """İlk: takip edilen + tamamlanmamış + kilitsiz aktivite. href ile ayırt edilir."""
    src, acts = scan_activities(page)
    seen: set[str] = set()
    stats = {"src": src, "total": 0, "done": 0, "locked": 0}
    target = None
    for a in acts:
        if not a["href"] and not a["restricted"]:
            continue
        key = a["href"] or a["name"]
        if key in seen:
            continue
        seen.add(key)
        if not a["tracked"]:
            continue
        stats["total"] += 1
        if a["done"]:
            stats["done"] += 1
        elif a["restricted"]:
            stats["locked"] += 1
        elif target is None:
            target = a
    if target:
        extra = " (kaldı, tekrar)" if target.get("val") == "3" else ""
        log(f"  Sıradaki açık aktivite [{src}]: {target['name']}{extra}")
        return target["href"], stats
    return None, stats


class Bot:
    def __init__(self, cfg: dict, cache: AnswerCache | None = None, course_url: str | None = None):
        self.cfg = cfg
        self.lesson = LessonRunner(cfg)
        self.quiz = QuizRunner(cfg, cache)
        self.fixed_course = bool(course_url)
        self.course_url: str | None = course_url or cfg.get("course_url") or None
        self.last_key = ""
        self.repeat = 0
        self.locked_waits = 0

    def go_next(self, page: Page, why: str) -> None:
        # 1) Sol menü: ilk tamamlanmamış + kilitsiz (sıralı kural)
        href, _ = pick_activity(page)
        here = cmid_from_page(page)
        there = (urllib.parse.parse_qs(urllib.parse.urlparse(href or "").query).get("id") or [""])[0]
        if href and not (here and there == here):
            log(f"  {why} → {href}")
            goto(page, href)
            return
        # 2) Moodle'ın sayfa altındaki 'sonraki aktivite' linki
        href = next_activity_href(page)
        if href:
            log(f"  {why} → sonraki aktivite linki")
            goto(page, href)
            return
        if self.course_url:
            log(f"  {why} → kurs ana sayfası (sonraki link yok)")
            goto(page, self.course_url)
            return
        log("  Sonraki aktivite linki ve kurs adresi yok")

    def step(self, page: Page) -> bool:
        """False = dur."""
        url = page.url or ""
        state = route(url)
        log(f"\n[{state}] {url}")

        if state not in ("HOME", "OTHER") and not self.fixed_course:
            cu = course_url_from_page(page)
            if cu:
                self.course_url = cu

        key = f"{state}|{url}"
        self.repeat = self.repeat + 1 if key == self.last_key else 0
        self.last_key = key
        if self.repeat == 6:
            log("  Aynı sayfada takıldı — yenileniyor")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            settle(page, 1500)
            return True
        if self.repeat >= 12:
            pause("  İlerleyemiyorum. Sayfayı elle düzelt, ENTER ile devam…")
            self.repeat = 0
            return True

        if state == "LESSON":
            if self.repeat == 0 and all_done(page) and not self.lesson.waited.get(cmid_from_page(page) or ""):
                self.go_next(page, "Ders zaten tamam")
                return True
            r = self.lesson.step(page)
            if r == "done":
                self.go_next(page, "Ders bitti")
            elif r == "waited":
                self.repeat = 0
            return True

        if state == "QUIZ_VIEW":
            r = self.quiz.handle_view(page)
            if r == "passed":
                self.go_next(page, "Quiz geçildi")
            elif r == "blocked":
                pause("  Deneme hakkı bitti. Elle bak, ENTER…")
            elif r == "unavailable":
                if self.course_url:
                    goto(page, self.course_url)
                else:
                    page.wait_for_timeout(3000)
            return True

        if state == "QUIZ_ATTEMPT":
            self.quiz.handle_attempt(page)
            return True

        if state == "QUIZ_SUMMARY":
            self.quiz.handle_summary(page)
            return True

        if state == "QUIZ_REVIEW":
            self.quiz.handle_review(page)
            return True

        if state == "ACTIVITY":
            if self.lesson.spend_wait(page):
                self.repeat = 0
                return True
            items = completion_items(page)
            if not items or all_done(page):
                self.go_next(page, "Aktivite tamam")
            elif self.repeat >= 2:
                todo = [t for s, t in items if s != "done"]
                log(f"  Bitmemiş şart: {todo} — yine de ilerleniyor")
                self.go_next(page, "Aktivite")
            else:
                page.wait_for_timeout(2000)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            return True

        if state == "COURSE":
            href, st = pick_activity(page)
            if href:
                self.locked_waits = 0
                goto(page, href)
                return True
            log(f"  Aktivite özeti [{st['src']}]: toplam {st['total']}, yeşil {st['done']}, kilitli {st['locked']}")
            if st["total"] and st["done"] == st["total"]:
                log("  Tüm aktiviteler yeşil — KURS BİTTİ")
                return False
            if st["total"] == 0:
                pause("  Aktivite listesi okunamadı. Sol menüyü aç / sayfayı yenile, ENTER…")
                return True
            self.locked_waits += 1
            if self.locked_waits <= 5:
                log("  Açık aktivite yok, kilitliler var — kilidin açılması bekleniyor (10 sn, yenile)")
                page.wait_for_timeout(10000)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                settle(page, 1500)
                return True
            pause("  Kilitler açılmadı. Elle kontrol et, ENTER ile devam…")
            self.locked_waits = 0
            return True

        # HOME / OTHER
        if self.course_url:
            goto(page, self.course_url)
        else:
            log("  Kurs adresi bilinmiyor — Chrome'da kursu veya bir dersi aç (3 sn sonra tekrar bakılır)")
            page.wait_for_timeout(3000)
        return True


def course_id_of(url: str) -> str:
    return (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("id") or [""])[0]


def run_course(cfg: dict, cdp: str, cache: AnswerCache, course_url: str | None, label: str) -> str:
    """Tek kurs döngüsü. Paralelde her kurs kendi iş parçacığında, kendi CDP bağlantısı ve sekmesiyle."""
    set_log_prefix(f"[{label}] " if label else "")
    result = "durdu"
    with sync_playwright() as pw:
        try:
            _, context = connect(pw, cdp)
        except Exception as e:
            log(f"Chrome'a bağlanamadı ({e}). Önce 1_chrome_ac.bat")
            return "bağlantı yok"

        cid = course_id_of(course_url) if course_url else ""

        def get_page() -> Page | None:
            if not cid:
                return pick_page(context)
            p = find_course_page(context, cid)
            if p:
                return p
            log(f"  Kurs {cid} için sekme yok — yeni sekme açılıyor")
            p = context.new_page()
            goto(p, course_url)
            return p

        bot = Bot(cfg, cache, course_url)
        page = get_page()
        if page is not None:
            log(f"Sekme: {page.url}")
        try:
            for _ in range(int(cfg.get("max_steps") or 5000)):
                if STOP.is_set():
                    break
                if page is None or page.is_closed():
                    page = get_page()
                    if page is None:
                        log("Açık sekme yok.")
                        break
                if not bot.step(page):
                    result = "TAMAMLANDI"
                    break
        except Exception as e:
            if not STOP.is_set():
                traceback.print_exc()
                log(f"HATA: {e}")
                result = f"hata: {e}"
    log(f"Kurs döngüsü bitti: {result}")
    return result


def main() -> int:
    load_secrets()
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--scale", type=float, help="Bekleme çarpanı (test: 0.01)")
    ap.add_argument("--course", action="append", help="Kurs URL'si; iki kez verilirse paralel")
    ap.add_argument("--attach", action="store_true", help="(eski bat uyumu; her zaman CDP)")
    ap.add_argument("--url", help="(eski bat uyumu; kullanılmıyor)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.scale is not None:
        cfg["wait_scale"] = args.scale

    courses = [c for c in (args.course or cfg.get("courses") or []) if c]
    if not courses and cfg.get("course_url"):
        courses = [cfg["course_url"]]

    log("=" * 56)
    log(f" BOT {VERSION}  — URL durum makinesi")
    log(f" Kurs sayısı: {len(courses) or 1}{' (paralel)' if len(courses) > 1 else ''}")
    log("=" * 56)
    cdp = (cfg.get("cdp_url") or "http://127.0.0.1:9222").rstrip("/")
    cache = open_cache(cfg)

    if len(courses) <= 1:
        try:
            run_course(cfg, cdp, cache, courses[0] if courses else None, "")
        except KeyboardInterrupt:
            STOP.set()
            log("\nDurduruldu (Ctrl+C).")
    else:
        set_parallel(True)
        labels = "ABCDEFGH"
        results: dict[str, str] = {}

        def worker(i: int, url: str) -> None:
            results[labels[i]] = run_course(cfg, cdp, cache, url, labels[i])

        threads = []
        for i, url in enumerate(courses[: len(labels)]):
            log(f" [{labels[i]}] {url}")
            t = threading.Thread(target=worker, args=(i, url), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(2)  # sekme açılışları çakışmasın
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            STOP.set()
            log("\nDurduruluyor (Ctrl+C)…")
            for t in threads:
                t.join(timeout=10)
        for k, v in results.items():
            log(f" [{k}] {v}")

    log("Chrome açık bırakıldı.")
    pause("Bitti. ENTER…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
