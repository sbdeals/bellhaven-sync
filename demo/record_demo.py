"""Record the Bellhaven review-app demo video with Playwright (sync API).

Run from the bellhaven-sync project root with both review apps already running:

    BELLHAVEN_REVIEW_PORT=8001 uv run bellhaven-sync review --snapshot tests/fixtures/crm   # snapshot app
    uv run bellhaven-sync review                                                          # live app (8000)

    uv run --with playwright python demo/record_demo.py                 # the real recording
    uv run --with playwright python demo/record_demo.py --rehearse --out <dir>

Part 1 replays the day-one queue against the CRM snapshot (port 8001). Part 2 uses the live
app (port 8000) and makes exactly ONE live CRM write: approving the chow_out proposal for
Bellhaven of Sandusky. `--rehearse` points Part 2 at the snapshot app as well, so nothing
live is touched.

Safety: every approve/reject/reopen POST goes through a route guard that only lets the four
planned decisions through, each at most once; anything else is aborted. JS confirm dialogs
are accepted only right before a planned Approve click and dismissed otherwise.

Outputs (in --out, default demo/output): bellhaven-demo.webm, bellhaven-demo.mp4,
frames/NN-*.png (one per scene start plus a few result frames), timestamps.json,
live-run.log (output of the read-only pipeline run started in scene 10).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SNAP = "http://127.0.0.1:8001"
LIVE = "http://127.0.0.1:8000"
W, H = 1280, 800

NOTE_TIFFIN = "SOP applies: revenue and open AR, keep the Cedar Trail account"
NOTE_ERIE = "Not sold on inactivating yet; billing to confirm"
NOTE_SANDUSKY = "Confirmed sold to Millstone; keep billing history, set CHOW pointer"
NOTE_HARBOR = "Leave the parent record active for now"

# Fake cursor: Playwright's video has no pointer. An 18px dot follows mousemove and shrinks
# on mousedown. window.__demoCursorTo(x, y) re-places it after a navigation.
CURSOR_JS = r"""
(() => {
  if (window.__demoCursorInstalled) return;
  window.__demoCursorInstalled = true;
  let el = null, last = null;
  const ensure = () => {
    if (el && el.isConnected) return el;
    const root = document.body || document.documentElement;
    if (!root) return null;
    el = document.createElement('div');
    el.id = '__demo_cursor';
    el.style.cssText = [
      'position:fixed', 'left:0', 'top:0', 'width:18px', 'height:18px', 'margin:-9px 0 0 -9px',
      'border-radius:50%', 'background:rgba(220,38,38,.6)', 'border:2px solid #fff',
      'box-shadow:0 0 0 1px rgba(0,0,0,.45),0 2px 6px rgba(0,0,0,.35)', 'box-sizing:border-box',
      'z-index:2147483647', 'pointer-events:none', 'opacity:0', 'transition:transform 90ms ease-out'
    ].join(';');
    root.appendChild(el);
    if (last) { el.style.left = last[0] + 'px'; el.style.top = last[1] + 'px'; el.style.opacity = '1'; }
    return el;
  };
  const place = (x, y) => {
    last = [x, y];
    const c = ensure();
    if (!c) return;
    c.style.left = x + 'px'; c.style.top = y + 'px'; c.style.opacity = '1';
  };
  window.__demoCursorTo = place;
  window.addEventListener('mousemove', e => place(e.clientX, e.clientY), true);
  window.addEventListener('mousedown', () => { const c = ensure(); if (c) c.style.transform = 'scale(0.55)'; }, true);
  window.addEventListener('mouseup', () => { const c = ensure(); if (c) c.style.transform = 'scale(1)'; }, true);
  document.addEventListener('DOMContentLoaded', ensure);
})();
"""


def api(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=20) as r:
        return json.load(r)


def find_fp(base: str, kind: str, title_part: str) -> str:
    hits = [p for p in api(base, "/api/proposals?status=pending") if p["kind"] == kind and title_part in p["title"]]
    if len(hits) != 1:
        raise SystemExit(f"expected one pending {kind!r} containing {title_part!r} on {base}, found {len(hits)}")
    return hits[0]["fingerprint"]


def mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


class Demo:
    def __init__(self, page: Page, frames: Path):
        self.page = page
        self.frames = frames
        self.t0 = time.monotonic()
        self.cx, self.cy = W / 2, H / 2
        self.events: list[dict] = []
        self.n = 0
        self.extra = 0
        self.expect_confirm = False

    # ------------------------------------------------------------- timeline
    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def scene(self, name: str) -> None:
        self.n += 1
        self.extra = 0
        t = self.now()
        self.events.append({"type": "scene", "n": self.n, "name": name, "t": t})
        print(f"[{mmss(t)}] scene {self.n:02d}  {name}", flush=True)
        self.page.screenshot(path=str(self.frames / f"{self.n:02d}-{slug(name)}.png"))

    def mark(self, name: str, shot: bool = False) -> None:
        t = self.now()
        self.events.append({"type": "mark", "scene": self.n, "name": name, "t": t})
        print(f"[{mmss(t)}]    - {name}", flush=True)
        if shot:
            self.extra += 1
            self.page.screenshot(path=str(self.frames / f"{self.n:02d}{chr(96 + self.extra)}-{slug(name)}.png"))

    # ------------------------------------------------------------- pointer
    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def sync_cursor(self) -> None:
        self.page.evaluate("([x, y]) => window.__demoCursorTo && window.__demoCursorTo(x, y)", [self.cx, self.cy])

    def move(self, x: float, y: float, steps: int = 25) -> None:
        self.page.mouse.move(x, y, steps=steps)
        self.cx, self.cy = x, y

    def center(self, loc: Locator) -> tuple[float, float]:
        """Center of the element once its position has stopped changing (wheel scrolls animate)."""
        # Aim at the first line box: a wrapped inline link's bounding-box center can be blank space.
        js = "e => { const r = e.getClientRects()[0] || e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }"
        prev = None
        for _ in range(30):
            x, y = loc.evaluate(js)
            if prev is not None and abs(y - prev[1]) < 0.5 and abs(x - prev[0]) < 0.5:
                break
            prev = (x, y)
            self.page.wait_for_timeout(60)
        return x, y

    def hits(self, loc: Locator, x: float, y: float) -> bool:
        return bool(loc.evaluate("(e, [x, y]) => { const t = document.elementFromPoint(x, y); return !!t && (e === t || e.contains(t)); }", [x, y]))

    def hover(self, loc: Locator, hold: int = 800) -> None:
        self.into_view(loc)
        self.move(*self.center(loc), steps=15)
        self.wait(hold)

    def hover_if(self, loc: Locator, hold: int = 800) -> None:
        if loc.count():
            self.hover(loc, hold)
        else:
            print("    (skipped hover: element not on this page)", flush=True)

    # ------------------------------------------------------------- scrolling
    def scroll_y(self) -> float:
        return self.page.evaluate("window.scrollY")

    def max_y(self) -> float:
        return self.page.evaluate("document.documentElement.scrollHeight - window.innerHeight")

    def scroll_to(self, target: float, step: int = 18, pause: int = 35) -> None:
        """Scroll with many small wheel ticks so the video shows a smooth scroll."""
        target = max(0.0, min(float(target), self.max_y()))
        dy = target - self.scroll_y()
        if abs(dy) < 2:
            return
        n = max(1, int(abs(dy) // step))
        for _ in range(n):
            self.page.mouse.wheel(0, dy / n)
            self.wait(pause)
        self.wait(200)

    def into_view(self, loc: Locator) -> None:
        loc.wait_for(state="visible")
        b = loc.bounding_box()
        assert b is not None
        if b["y"] < 0 or b["y"] + b["height"] > H - 40:
            self.scroll_to(self.scroll_y() + b["y"] - 300, step=40, pause=25)

    # ------------------------------------------------------------- clicking
    def outline(self, loc: Locator) -> None:
        loc.evaluate("e => { e.style.outline = '2px solid #f59e0b'; e.style.outlineOffset = '2px'; }")
        self.wait(400)

    def press(self) -> None:
        self.page.mouse.down()
        self.wait(110)
        self.page.mouse.up()

    def click(self, loc: Locator, *, outline: bool = False, nav: bool = True) -> None:
        self.into_view(loc)
        x, y = self.center(loc)
        self.move(x, y)
        if not self.hits(loc, x, y):  # still settling: re-aim once, then refuse to click blind
            self.wait(300)
            x, y = self.center(loc)
            self.move(x, y, steps=8)
            if not self.hits(loc, x, y):
                raise RuntimeError(f"click target not under the pointer at ({x:.0f}, {y:.0f})")
        if outline:
            self.outline(loc)
        else:
            self.wait(250)
        if nav:
            with self.page.expect_navigation(wait_until="load", timeout=60_000):
                self.press()
            self.sync_cursor()
        else:
            self.press()

    def type_note(self, text: str) -> None:
        ta = self.page.locator("form.decision-form textarea")
        self.click(ta, nav=False)
        ta.press_sequentially(text, delay=40)
        self.wait(600)

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="load")
        self.sync_cursor()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "demo" / "output"), help="output directory")
    ap.add_argument("--rehearse", action="store_true", help="run Part 2 against the snapshot app too (no live writes)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    live = SNAP if args.rehearse else LIVE

    for base in sorted({SNAP, live}):
        print(base, api(base, "/health"))

    # Resolve every fingerprint at runtime (never hardcoded).
    fp_tiffin = find_fp(SNAP, "chow_reparent", "Tiffin")
    fp_union = find_fp(SNAP, "create", "Union Square")
    fp_erie = find_fp(SNAP, "duplicate", "Erie")
    fp_sandusky = find_fp(live, "chow_out", "Sandusky")
    fp_harbor = find_fp(live, "parent_acquired", "Harborview")
    fp_ashtabula = find_fp(live, "confirmed", "Ashtabula")

    allowed = {
        ("approve", SNAP, fp_tiffin),
        ("reject", SNAP, fp_erie),
        ("approve", live, fp_sandusky),  # the only live CRM write (when not rehearsing)
        ("reject", live, fp_harbor),
    }
    used: set[tuple[str, str, str]] = set()
    blocked: list[str] = []

    def guard(route) -> None:
        req = route.request
        u = urlsplit(req.url)
        m = re.fullmatch(r"/proposals/([0-9a-f]+)/(approve|reject|reopen)", u.path)
        key = (m.group(2), f"{u.scheme}://{u.netloc}", m.group(1)) if m else None
        if req.method == "POST" and key in allowed and key not in used:
            used.add(key)
            print(f"    guard: allowing {key[0]} {key[1]} {key[2][:12]}", flush=True)
            route.continue_()
        else:
            blocked.append(f"{req.method} {req.url}")
            print(f"    guard: BLOCKED {req.method} {req.url}", flush=True)
            route.abort()

    run_cmd = ["uv", "run", "bellhaven-sync", "run"] + (["--snapshot", "tests/fixtures/crm"] if args.rehearse else [])
    run_proc: subprocess.Popen | None = None
    run_log = out / "live-run.log"
    run_log_f = None
    video_path: str | None = None
    ok = False
    total = 0.0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(out),
            record_video_size={"width": W, "height": H},
        )
        context.add_init_script(CURSOR_JS)
        context.route(re.compile(r"^http://127\.0\.0\.1:\d+/proposals/[^/?#]+/(approve|reject|reopen)"), guard)
        page = context.new_page()
        d = Demo(page, frames)  # t0 == start of the video
        video_path = page.video.path() if page.video else None

        def on_dialog(dialog) -> None:
            if d.expect_confirm:
                d.expect_confirm = False
                print(f"    dialog accepted: {dialog.message[:90]!r}", flush=True)
                dialog.accept()
            else:
                print(f"    dialog DISMISSED (unexpected): {dialog.message[:90]!r}", flush=True)
                dialog.dismiss()

        page.on("dialog", on_dialog)
        q = page.locator

        def nav_link(text: str) -> Locator:
            return q("nav.topnav a", has_text=text)

        def title_link(fp: str) -> Locator:
            return q(f'a.qtitle[href="/proposals/{fp}"]')

        def row_cells(label: str) -> Locator:
            return q("table.compare tbody tr", has=q("th.rowlabel", has_text=label)).first.locator("td")

        try:
            # ======================= PART 1: snapshot mode (day-one queue) =======================
            d.goto(SNAP + "/")
            d.scene("Snapshot queue")
            d.wait(1500)
            for key in ("pending", "applied", "confirmed"):
                d.hover(q(f"nav.tabs a.tab-{key}"), 400)
            for label in ("CHOW reparent", "Duplicate"):
                d.hover(q(".chips a.chip", has_text=label), 450)
            d.wait(900)

            # --- 2. Tiffin chow_reparent: SOP applies, approve (snapshot write only)
            d.click(title_link(fp_tiffin))
            d.scene("Tiffin chow_reparent")
            d.wait(1000)
            d.hover(q(".detail-head .rule"), 900)
            d.scroll_to(260)
            d.hover(q("table.compare th.role-old .colname"), 700)
            d.hover(q(".sop .sop-figs").first, 1600)
            d.hover(q("ol.actions .act-head").nth(0), 700)
            d.scroll_to(10_000)
            d.hover(q("ol.actions dl.kv dt", has_text="chow_current_account").first, 1400)
            d.scroll_to(0, step=60)
            d.type_note(NOTE_TIFFIN)
            d.mark("Tiffin: click Approve")
            d.expect_confirm = True
            d.click(q("form.decision-form button.btn-approve"), outline=True)
            d.expect_confirm = False
            d.mark("Tiffin: applied result card", shot=True)
            d.hover(q(".result .created"), 1500)
            d.hover(q(".result ol.steps > li").nth(0), 1200)
            d.hover(q(".result ol.steps > li").nth(1), 1200)
            d.wait(1100)
            st = api(SNAP, f"/api/proposals/{fp_tiffin}")
            print(f"    Tiffin (snapshot) status={st['status']} ok={(st.get('apply_result') or {}).get('ok')}", flush=True)

            # --- 3. Union Square create: possibly-related Juniper Point account, no decision
            d.click(nav_link("Queue"))
            d.click(title_link(fp_union))
            d.scene("Union Square create")
            d.wait(1200)
            d.hover(q(".detail-head .rule"), 1000)
            d.scroll_to(300)
            d.hover(q("table.compare th.role-related .colname"), 1200)
            d.hover(q("ul.notes li").nth(1), 2500)
            d.wait(500)
            d.click(nav_link("Queue"))
            d.mark("Union Square: back to queue, no decision")

            # --- 4. Erie duplicate: survivor rule, reject with a note
            d.wait(400)
            d.click(q(".chips a.chip", has_text="Duplicate"))
            d.mark("Duplicate filter")
            d.wait(600)
            d.click(title_link(fp_erie))
            d.scene("Erie duplicate")
            d.wait(1000)
            d.hover(q(".detail-head .rule"), 1300)
            d.scroll_to(230)
            d.hover(q("table.compare th.role-loser .colname"), 1000)
            d.hover(q("table.compare th.role-survivor .colname"), 1100)
            d.scroll_to(10_000)
            d.hover(q("ul.notes li").nth(1), 1800)
            d.scroll_to(0, step=60)
            d.type_note(NOTE_ERIE)
            d.mark("Erie: click Reject")
            d.click(q("form.decision-form button.btn-reject"), outline=True)
            d.mark("Erie: rejected, back on the queue", shot=True)
            d.hover(q(".flash"), 2500)
            d.hover(q("nav.tabs a.tab-rejected"), 2000)

            # --- 5. Runs page (snapshot)
            d.click(nav_link("Runs"))
            d.scene("Snapshot runs")
            d.wait(1000)
            d.hover(q("table.grid tbody tr").first, 4500)

            # ======================= PART 2: live CRM =======================
            d.goto(live + "/")
            d.scene("Live queue")
            d.wait(1200)
            d.hover(q("nav.tabs a.tab-pending"), 800)
            d.hover(title_link(fp_sandusky), 1300)
            d.hover(title_link(fp_harbor), 1300)
            d.hover(q("nav.tabs a.tab-confirmed"), 1200)

            # --- 7. Sandusky chow_out: the one live write
            d.click(title_link(fp_sandusky))
            d.scene("Sandusky chow_out (live)")
            d.wait(1000)
            d.hover(q(".detail-head .rule"), 1000)
            d.scroll_to(330)
            d.hover(q("table.compare th.role-new_owner .colname"), 900)
            d.hover(row_cells("Street").nth(1), 900)
            d.hover(q(".sop .sop-figs").first, 1400)
            d.hover_if(row_cells("Existing note").nth(0), 1600)  # note left by the earlier live review
            d.hover(q("ol.actions dl.kv dt", has_text="chow_current_account").first, 1000)
            d.scroll_to(0, step=60)
            d.type_note(NOTE_SANDUSKY)

            pre = api(live, f"/api/proposals/{fp_sandusky}")
            if not (
                pre["kind"] == "chow_out" and pre["status"] == "pending" and "Sandusky" in pre["title"]
                and page.url.split("?")[0] == f"{live}/proposals/{fp_sandusky}"
            ):
                raise RuntimeError(f"pre-approve check failed: {pre['kind']} {pre['status']} {page.url}")
            d.mark("Sandusky: click Approve (live write)")
            d.expect_confirm = True
            d.click(q("form.decision-form button.btn-approve"), outline=True)
            d.expect_confirm = False
            post = api(live, f"/api/proposals/{fp_sandusky}")
            res = post.get("apply_result") or {}
            print(f"    Sandusky status={post['status']} ok={res.get('ok')} error={res.get('error')}", flush=True)
            if post["status"] != "applied" or not res.get("ok"):
                raise RuntimeError(f"Sandusky approve did not apply: status={post['status']} result={res}")
            d.mark("Sandusky: applied result card", shot=True)
            d.hover(q(".result ol.steps > li").first, 2200)
            d.hover(q(".result .openlinks"), 2000)
            d.wait(1000)

            # --- 8. CRM browser page for Bellhaven of Sandusky (same page, not a new tab)
            link = q(".result .openlinks a", has_text="Bellhaven of Sandusky").first
            d.into_view(link)
            d.move(*d.center(link))
            d.outline(link)
            href = link.get_attribute("href")
            assert href and "/accounts/" in href
            d.goto(href)
            d.scene("CRM browser: Bellhaven of Sandusky")
            d.wait(1500)
            try:
                chow = q("text=/chow current account/i").first
                b = chow.bounding_box()
                d.move(b["x"] + 70, b["y"] + b["height"] + 16)
                d.wait(2500)
                note = q("text=/^\\s*note\\s*$/i").first
                b = note.bounding_box()
                d.move(b["x"] + 120, b["y"] + b["height"] + 30)
                d.wait(2500)
            except Exception as exc:  # external page: fall back to fixed coordinates
                print(f"    CRM page hover fallback ({exc})")
                d.move(830, 332)
                d.wait(2500)
                d.move(600, 400)
                d.wait(2500)
            d.wait(1500)
            d.page.go_back(wait_until="load")
            d.sync_cursor()
            d.mark("Back in the review app")
            d.wait(800)

            # --- 9. Harborview parent_acquired: reject with a note (local only)
            d.click(nav_link("Queue"))
            d.click(title_link(fp_harbor))
            d.scene("Harborview parent_acquired (live)")
            d.wait(1000)
            d.hover(q(".detail-head .rule"), 1000)
            d.scroll_to(420)
            dup = row_cells("Duplicate of")
            d.hover(dup.nth(1) if dup.count() else row_cells("Parent").nth(1), 1200)
            d.scroll_to(0, step=60)
            d.type_note(NOTE_HARBOR)
            d.mark("Harborview: click Reject")
            d.click(q("form.decision-form button.btn-reject"), outline=True)
            d.mark("Harborview: rejected, back on the queue", shot=True)
            # Every decision is made: start the read-only daily run in the background now, so
            # scene 11 can show its result without sitting on dead air while it scrapes.
            env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
            run_log_f = open(run_log, "w", encoding="utf-8")
            run_proc = subprocess.Popen(run_cmd, cwd=ROOT, stdout=run_log_f, stderr=subprocess.STDOUT, env=env)
            d.mark(f"pipeline started: {' '.join(run_cmd)}")
            d.hover(q(".flash"), 2500)
            d.hover(q("nav.tabs a.tab-applied"), 1300)
            d.hover(q("nav.tabs a.tab-rejected"), 1300)

            # --- 10. Confirmed tab + Ashtabula
            d.click(q("nav.tabs a.tab-confirmed"))
            d.scene("Confirmed tab")
            d.wait(1500)
            d.hover(q(".page-head h1"), 1500)
            d.hover(q("ul.queue li.qrow").first, 2000)
            d.click(title_link(fp_ashtabula))
            d.mark("Ashtabula confirmed detail", shot=True)
            d.wait(800)
            d.hover(row_cells("Street").nth(1), 1000)
            d.scroll_to(10_000)
            d.hover(q("table.score tr", has_text="Phone"), 1200)
            d.hover(q("table.score tr", has_text="Administrator"), 1200)
            d.hover(q(".flagchip.warn").first, 1500)
            d.hover(q("ul.notes li").nth(1), 2200)

            # --- 11. Re-run results on the live Runs page
            rc = run_proc.wait(timeout=300)
            run_log_f.close()
            d.mark(f"pipeline finished rc={rc}")
            if rc != 0:
                print(run_log.read_text(encoding="utf-8")[-2000:])
                raise RuntimeError(f"pipeline run exited {rc}")
            d.scroll_to(0, step=60)
            d.click(nav_link("Runs"))
            d.scene("Live runs after re-run")
            d.wait(1000)
            top = q("table.grid tbody tr").first
            d.hover(top.locator("td").nth(5), 1500)  # New
            d.hover(top.locator("td").nth(7), 2500)  # Previously decided
            d.hover(top.locator("td").nth(9), 1000)  # Notes
            d.wait(500)
            d.mark("end")
            ok = True
        finally:
            if run_proc is not None and run_proc.poll() is None:
                print("    waiting for the pipeline subprocess to finish ...", flush=True)
                run_proc.wait(timeout=300)
            if run_log_f is not None and not run_log_f.closed:
                run_log_f.close()
            total = d.now()
            context.close()  # flushes the video
            browser.close()

    print(f"script elapsed {total:.1f}s; blocked requests: {blocked or 'none'}")

    duration = None
    if video_path and Path(video_path).exists():
        webm = out / "bellhaven-demo.webm"
        shutil.move(video_path, webm)
        mp4 = out / "bellhaven-demo.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(mp4)],
            check=True,
        )
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
            capture_output=True, text=True, check=True,
        )
        duration = float(probe.stdout.strip())
        print(f"video: {webm}\nvideo: {mp4}  ({duration:.1f}s)")

    # The video starts a little after the page is created (the first screencast frame), and the
    # gap is constant, so video time = script time - (script elapsed - video duration).
    offset = round(total - duration, 2) if duration else 0.0
    for e in d.events:
        e["video_t"] = round(max(0.0, e["t"] - offset), 2)
    (out / "timestamps.json").write_text(
        json.dumps({"elapsed": total, "video_duration": duration, "offset": offset, "ok": ok, "events": d.events}, indent=2),
        encoding="utf-8",
    )
    print(f"\nScene timestamps (video time; script clock minus {offset:.2f}s):")
    for e in d.events:
        if e["type"] == "scene":
            print(f"  {mmss(e['video_t'])}  {e['n']:02d} {e['name']}")
        else:
            print(f"  {mmss(e['video_t'])}       - {e['name']}")
    return 0 if ok and not blocked else 1


if __name__ == "__main__":
    sys.exit(main())
