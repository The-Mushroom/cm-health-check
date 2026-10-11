#!/usr/bin/env python3
"""
Creative Mushroom Website Health Check
======================================

Crawls a website from the outside, scores it out of 100 against the rules in
scoring.json, and renders a branded A4 PDF report.

The report is diagnostic, not prescriptive: it says WHAT is wrong and what it
costs the owner. The only remedy it ever gives is one "free fix".

Quick start (see README.md for setup):
    python3 health_check.py https://www.example.co.uk --name "Example Ltd"

Useful options:
    --business-type hospitality   adds hospitality notes (menus, booking, sign-ups)
    --gbp match|mismatch|none|na  Google Business Profile result you checked by hand
    --rank "text"                 a note about their Google position, shown as written
    --free-fix auto|<key>         pick the free fix, or let the tool suggest one
    --pages 25                    how many pages to crawl
"""

import argparse
import configparser
import json
import os
import random
import re
import socket
import ssl
import string
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.parse import urljoin, urlparse, urldefrag, parse_qs
from urllib import robotparser

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = Path(__file__).resolve().parent
VERSION = "2.0"
UA_BOT = ("Mozilla/5.0 (compatible; CreativeMushroomHealthCheck/%s; "
          "+https://creativemushroom.co.uk/free-website-health-check/)" % VERSION)
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1 CreativeMushroomHealthCheck/" + VERSION)

PHONE_RE = re.compile(r"(?:(?:\+44\s?\(?0?\)?\s?)|\b0)(?:\d[\s-]?){9,10}\b")
PLACEHOLDER_RE = re.compile(r"lorem ipsum|hello world!?|sample page|just another wordpress site|coming soon|under construction", re.I)
LEGACY_MAP_PATTERNS = ["maps.google.com/maps?", "maps.google.co.uk/maps?", "google.com/maps/ms?",
                       "maps.googleapis.com/maps/api/staticmap"]
MENU_RE = re.compile(r"(menu|carte|specials|wine\s*list|drinks|tariff|rates|brochure|price\s*list)", re.I)
BOOKING_RE = re.compile(r"(book\s*(now|direct|a\s*table|online|your\s*stay)?|reserve|reservation|check\s*availability)", re.I)
SIGNUP_RE = re.compile(r"(newsletter|subscribe|sign\s*up|mailing\s*list|join\s*our\s*list|keep\s*in\s*touch)", re.I)
BOOKING_HOSTS = ("opentable", "resdiary", "designmynight", "sevenrooms", "booking.com", "tablein", "collinsbookings", "freetobook",
                 "eviivo", "beds24", "littlehotelier", "bookatable", "exploretock", "quandoo", "thefork", "cloudbeds", "guestline",
                 "nobeds", "siteminder", "superbreak", "tablebooker", "dojo.app")
SPAM_PROTECTION_RE = re.compile(r"(g-recaptcha|recaptcha/api|h-captcha|hcaptcha|cf-turnstile|challenges\.cloudflare\.com/turnstile|"
                                r"gfield--type-honeypot|gform_validation_container|wpcf7-|akismet|honeypot|hp-field|friendly-captcha|frc-captcha)", re.I)
SCHEMA_TYPES = {"organization", "localbusiness", "restaurant", "hotel", "lodgingbusiness", "store", "professionalservice",
                "foodestablishment", "barorpub", "cafeorcoffeeshop", "bedandbreakfast", "campground", "homeandconstructionbusiness",
                "legalservice", "accountingservice", "medicalbusiness", "autorepair", "corporation", "onlinestore", "ngo"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings():
    cfg = configparser.ConfigParser()
    path = HERE / "settings.ini"
    if path.exists():
        cfg.read(path)
    key = os.environ.get("PSI_API_KEY") or cfg.get("google", "pagespeed_api_key", fallback="").strip()
    return {"psi_key": key or None}


def load_rules():
    with open(HERE / "scoring.json", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    status: int
    final_url: str
    headers: dict
    html: str
    bytes: int
    redirects: list = field(default_factory=list)

    _soup: object = None

    @property
    def soup(self):
        if self._soup is None:
            self._soup = BeautifulSoup(self.html, "html.parser")
        return self._soup


@dataclass
class Result:
    """One scored check."""
    category: str
    key: str
    label: str
    points: float
    earned: object          # float, or None when not measured
    value: object = None    # what was measured, for the report
    detail: str = ""        # what we found
    impact: str = ""        # what it costs the owner (only shown when points are lost)


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------

def score_value(rule, value, source=None):
    """Turn a measurement into points using the rule from scoring.json."""
    if value is None:
        return None
    t = rule["type"]
    if t == "map":
        return float(rule["map"].get(value, 0))
    if t == "ratio_band":
        full, zero = rule["full_at"], rule["zero_below"]
        if value >= full:
            return float(rule["points"])
        if value < zero or full == zero:
            return 0.0
        return round(rule["points"] * (value - zero) / (full - zero) * 2) / 2
    tiers = rule["tiers_by_source"][source] if "tiers_by_source" in rule else rule["tiers"]
    for threshold, pts in tiers:
        if (t == "max" and value <= threshold) or (t == "min" and value >= threshold):
            return float(pts)
    return 0.0


class Scorer:
    def __init__(self, rules):
        self.rules = rules
        self.results = []

    def add(self, category, key, value, detail="", impact="", source=None, display=None):
        rule = self.rules["categories"][category]["checks"][key]
        earned = score_value(rule, value, source)
        self.results.append(Result(category, key, rule["label"], rule["points"], earned,
                                   display if display is not None else value, detail, impact))

    def category_scores(self):
        out = []
        for ckey, cat in self.rules["categories"].items():
            rs = [r for r in self.results if r.category == ckey]
            measured = [r for r in rs if r.earned is not None]
            avail = sum(r.points for r in measured)
            earned = sum(r.earned for r in measured)
            scaled = cat["points"] * earned / avail if avail else 0
            out.append({"key": ckey, "label": cat["label"], "max": cat["points"], "score": round(scaled),
                        "raw_earned": earned, "raw_available": avail,
                        "unmeasured": [r.label for r in rs if r.earned is None], "results": rs})
        return out

    def band(self, total):
        for floor, name, blurb in self.rules["bands"]:
            if total >= floor:
                return name, blurb
        return self.rules["bands"][-1][1], self.rules["bands"][-1][2]


# ---------------------------------------------------------------------------
# Crawling
# ---------------------------------------------------------------------------

def norm_host(h):
    return (h or "").lower().removeprefix("www.")


class Crawler:
    def __init__(self, start, max_pages, delay, timeout, log):
        self.start = start
        self.host = norm_host(urlparse(start).hostname)
        self.max_pages, self.delay, self.timeout, self.log = max_pages, delay, timeout, log
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA_BOT, "Accept-Language": "en-GB,en;q=0.9"})
        self.pages, self.links, self.images = [], {}, {}
        self.robots = robotparser.RobotFileParser()
        self.robots_txt = ""
        self.last = 0.0

    def polite(self):
        wait = self.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()

    def internal(self, url):
        p = urlparse(url)
        return p.scheme in ("http", "https") and norm_host(p.hostname) == self.host

    def get(self, url, **kw):
        self.polite()
        return self.s.get(url, timeout=self.timeout, allow_redirects=kw.pop("allow_redirects", True), **kw)

    def load_robots(self):
        root = "{0.scheme}://{0.netloc}".format(urlparse(self.start))
        try:
            r = self.get(root + "/robots.txt")
            self.robots_txt = r.text if r.status_code == 200 else ""
        except requests.RequestException:
            self.robots_txt = ""
        self.robots.parse(self.robots_txt.splitlines())

    def allowed(self, url):
        try:
            return self.robots.can_fetch(UA_BOT, url)
        except Exception:
            return True

    def crawl(self):
        self.load_robots()
        queue, seen = deque([self.start]), {self.start}
        while queue and len(self.pages) < self.max_pages:
            url = queue.popleft()
            if not self.allowed(url) and url != self.start:
                continue
            try:
                r = self.get(url)
            except requests.RequestException as e:
                self.log(f"  could not load {url}: {e.__class__.__name__}")
                continue
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype:
                continue
            page = Page(url, r.status_code, r.url, dict(r.headers), r.text, len(r.content),
                        [h.url for h in r.history])
            self.pages.append(page)
            self.log(f"  {r.status_code} {url}")
            if r.status_code >= 400:
                continue
            for a in page.soup.find_all("a", href=True):
                href = urldefrag(urljoin(page.final_url, a["href"].strip()))[0]
                if not self.internal(href):
                    continue
                self.links.setdefault(href, set()).add(page.final_url)
                low = href.lower()
                if re.search(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mp3)$", low) or "/wp-admin" in low or "wp-login" in low:
                    continue
                if href not in seen:
                    seen.add(href)
                    queue.append(href)
            for img in page.soup.find_all("img"):
                src = img.get("src") or img.get("data-src")
                if src and not src.startswith("data:"):
                    self.images.setdefault(urljoin(page.final_url, src), page.final_url)
        return self.pages

    def status_of(self, url):
        """Status code plus number of redirect hops, without downloading big files."""
        try:
            self.polite()
            r = self.s.head(url, timeout=self.timeout, allow_redirects=True)
            if r.status_code in (403, 405, 501):
                self.polite()
                r = self.s.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
                r.close()
            return r.status_code, len(r.history), r.headers
        except requests.RequestException:
            return None, 0, {}


# ---------------------------------------------------------------------------
# Speed: PageSpeed Insights, with a local fallback
# ---------------------------------------------------------------------------

def psi(url, key, log):
    params = [("url", url), ("strategy", "mobile"), ("category", "performance"), ("category", "accessibility")]
    if key:
        params.append(("key", key))
    try:
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=120)
        if r.status_code != 200:
            log(f"  PageSpeed Insights returned {r.status_code}" + ("" if key else " (no API key set)"))
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"  PageSpeed Insights unavailable: {e.__class__.__name__}")
        return None
    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    cats = lh.get("categories", {})
    field_m = (data.get("loadingExperience") or {}).get("metrics") or {}

    def fv(name):
        m = field_m.get(name)
        return m.get("percentile") if m else None

    def av(name):
        a = audits.get(name) or {}
        return a.get("numericValue")

    out = {"source": "lab", "perf": None, "a11y": None}
    if cats.get("performance", {}).get("score") is not None:
        out["perf"] = round(cats["performance"]["score"] * 100)
    if cats.get("accessibility", {}).get("score") is not None:
        out["a11y"] = round(cats["accessibility"]["score"] * 100)
    f_lcp, f_inp, f_cls = fv("LARGEST_CONTENTFUL_PAINT_MS"), fv("INTERACTION_TO_NEXT_PAINT"), fv("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    if f_lcp is not None and f_inp is not None and f_cls is not None:
        out.update(source="field", lcp=f_lcp, resp=f_inp, resp_kind="inp", cls=f_cls / 100)
    else:
        out.update(lcp=av("largest-contentful-paint"), resp=av("total-blocking-time"), resp_kind="tbt",
                   cls=av("cumulative-layout-shift"))
    out["weight"] = av("total-byte-weight")
    return out


# ---------------------------------------------------------------------------
# Browser checks (Playwright): mobile layout, fallback speed, 404 page
# ---------------------------------------------------------------------------

MOBILE_JS = r"""
() => {
  const vw = Math.min(window.innerWidth, screen.width || 390);
  const hasVp = !!document.querySelector('meta[name=viewport][content*="device-width"]');
  const overflow = hasVp && document.documentElement.scrollWidth > vw + 1;
  const inline = el => { if (el.tagName !== 'A') return false; const b = el.closest('p, li, td, dd, blockquote, figcaption');
    return !!b && !b.matches('nav li, nav p') && b.textContent.trim().length > el.textContent.trim().length + 15; };
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const targets = [...document.querySelectorAll('a[href], button, input[type=submit], input[type=button], [role=button]')].filter(vis).filter(el => !inline(el));
  // WCAG 2.5.8: a target passes if it is at least 24 by 24, or if a 24px circle on its centre touches no other target
  const rects = targets.map(el => el.getBoundingClientRect());
  const gap = (c, r) => { const dx = Math.max(r.left - c.x, 0, c.x - r.right), dy = Math.max(r.top - c.y, 0, c.y - r.bottom); return Math.hypot(dx, dy); };
  const bigEnough = rects.filter((r, i) => {
    if (r.width >= 24 && r.height >= 24) return true;
    const c = {x: r.left + r.width / 2, y: r.top + r.height / 2};
    return rects.every((o, j) => { if (j === i) return true;
      if (gap(c, o) < 12) return false;
      const small = o.width < 24 || o.height < 24;
      return !small || Math.hypot(c.x - (o.left + o.width / 2), c.y - (o.top + o.height / 2)) >= 24; });
  }).length;
  const paras = [...document.querySelectorAll('p, li')].filter(el => vis(el) && el.textContent.trim().length > 40);
  const sizes = paras.map(el => parseFloat(getComputedStyle(el).fontSize)).sort((a,b)=>a-b);
  const med = sizes.length ? sizes[Math.floor(sizes.length/2)] : null;
  const imgs = [...document.images].filter(i => vis(i) && i.naturalWidth > 0 && i.getBoundingClientRect().width >= 40);
  const okImgs = imgs.filter(i => i.srcset || i.currentSrc !== i.src || i.naturalWidth <= Math.min(i.getBoundingClientRect().width, vw) * 2.2).length;
  return {overflow, targets: targets.length, bigEnough, fontSize: med, images: imgs.length, okImages: okImgs};
}
"""

PERF_INIT_JS = r"""
window.__cm = {lcp: null, cls: 0, tbt: 0};
try { new PerformanceObserver(l => { const e = l.getEntries(); if (e.length) window.__cm.lcp = e[e.length-1].startTime; })
  .observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cm.cls += e.value; })
  .observe({type: 'layout-shift', buffered: true}); } catch (e) {}
try { new PerformanceObserver(l => { for (const e of l.getEntries()) window.__cm.tbt += Math.max(0, e.duration - 50); })
  .observe({type: 'longtask', buffered: true}); } catch (e) {}
"""


class Browser:
    def __init__(self, log):
        from playwright.sync_api import sync_playwright
        self.log = log
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()

    def close(self):
        self.browser.close()
        self.pw.stop()

    def mobile_context(self):
        return self.browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                        is_mobile=True, has_touch=True, user_agent=UA_MOBILE)

    def mobile_checks(self, urls):
        ctx = self.mobile_context()
        page = ctx.new_page()
        out = []
        for u in urls:
            try:
                page.goto(u, wait_until="load", timeout=45000)
                page.wait_for_timeout(800)
                out.append(page.evaluate(MOBILE_JS))
            except Exception as e:
                self.log(f"  mobile check skipped for {u}: {e.__class__.__name__}")
        ctx.close()
        return out

    def local_speed(self, url):
        """Rough lab measurement on a throttled phone profile, used when PageSpeed Insights is unavailable."""
        ctx = self.mobile_context()
        page = ctx.new_page()
        total = {"bytes": 0}
        page.on("response", lambda r: total.__setitem__("bytes", total["bytes"] + int(r.headers.get("content-length", 0) or 0)))
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 150,
                                                      "downloadThroughput": 1.6 * 1024 * 1024 / 8,
                                                      "uploadThroughput": 750 * 1024 / 8})
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
        page.add_init_script(PERF_INIT_JS)
        try:
            page.goto(url, wait_until="load", timeout=90000)
            page.wait_for_timeout(3000)
            m = page.evaluate("window.__cm")
        except Exception as e:
            self.log(f"  local speed check failed: {e.__class__.__name__}")
            m = None
        ctx.close()
        if not m:
            return None
        return {"source": "local", "lcp": m["lcp"], "resp": m["tbt"], "resp_kind": "tbt", "cls": m["cls"],
                "perf": None, "a11y": None, "weight": total["bytes"] or None}

    def page_404(self, url):
        ctx = self.browser.new_context()
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="load", timeout=30000)
            status = resp.status if resp else None
            links = page.evaluate("document.querySelectorAll('a[href]').length")
            words = page.evaluate("document.body ? document.body.innerText.split(/\\s+/).length : 0")
            final = page.url
        except Exception:
            status, links, words, final = None, 0, 0, url
        ctx.close()
        return status, links, words, final

    def pdf(self, html_path, pdf_path, footer=""):
        ctx = self.browser.new_context()
        page = ctx.new_page()
        page.goto(Path(html_path).resolve().as_uri(), wait_until="networkidle")
        page.wait_for_timeout(500)
        foot = ('<div style="width:100%;margin:0 18mm;display:flex;justify-content:space-between;'
                'font-family:Poppins,Arial,sans-serif;font-size:7pt;color:#5A7282">'
                f'<span>Creative Mushroom &middot; creativemushroom.co.uk</span><span>{footer}</span>'
                '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')
        page.pdf(path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True,
                 display_header_footer=True, header_template="<span></span>", footer_template=foot)
        ctx.close()


# ---------------------------------------------------------------------------
# Plain English: what each lost point costs the owner (never how to fix it)
# ---------------------------------------------------------------------------

IMPACT = {
    "lcp": "Visitors on phones wait for your main content to appear. Over half of mobile visitors give up on a page that takes more than three seconds.",
    "responsiveness": "Taps and clicks feel sluggish, so the site feels broken even when it is not.",
    "cls": "Things jump around while the page loads, so people tap the wrong thing.",
    "performance": "Google's own speed test rates this page poorly, which counts against you in search results.",
    "weight": "The page is heavy to download, which is slow and costly on a phone signal.",
    "overflow": "Some pages are wider than a phone screen, so visitors have to scroll sideways.",
    "viewport": "Phones show a shrunken desktop version of the site, which is hard to read and use.",
    "tap_targets": "Visitors tap the wrong thing or have to zoom in first, and many simply give up.",
    "accessibility": "Parts of the site are hard to use for people with sight or mobility difficulties, and Google notices.",
    "font_size": "Body text is small on a phone, so visitors have to pinch and zoom.",
    "tel_links": "Phone numbers cannot be tapped to call. On a phone, every extra step loses callers.",
    "images_sized": "Images are much bigger than the screen needs, which slows every page.",
    "titles": "Page titles are missing, repeated or the wrong length, which weakens how you appear in Google.",
    "indexable": "Important pages are hidden from Google, so they cannot appear in search results at all.",
    "meta_descriptions": "Google is writing your search result descriptions for you, often badly.",
    "h1": "Pages lack one clear main heading, which makes them harder for people and Google to understand.",
    "sitemap": "There is no clear map of your pages for search engines to follow.",
    "schema": "Search engines are not given your business details in the format they look for.",
    "alt_text": "Images have no descriptions, which hurts accessibility and image search.",
    "gbp": "Your Google Business Profile is missing or does not match the website, which costs you in local search and maps.",
    "https": "The connection is not fully secure, and browsers may warn visitors away.",
    "headers": "Basic browser security settings are missing, leaving visitors less protected.",
    "cms_version": "The site runs an out of date version of WordPress, a common way sites get hacked.",
    "plugins": "Some add-ons are out of date. Old plugins are one of the most common ways WordPress sites get hacked.",
    "certificate": "The security certificate expires soon. When it does, browsers show a full-page warning.",
    "mixed_content": "Some files load insecurely, which can break the padlock in the address bar.",
    "forms": "Forms have no spam protection, so junk and malicious entries reach your inbox.",
    "privacy": "There is no privacy policy, which is a legal requirement when you collect personal details.",
    "broken_links": "Visitors hit dead ends, and Google sees a neglected site.",
    "oversized_images": "Some image files are far heavier than they need to be, slowing pages down.",
    "redirect_chains": "Some links bounce through several redirects, which slows pages and wastes search ranking.",
    "page_404": "When a page is missing, visitors get no help finding their way back.",
    "leftovers": "Placeholder text or dead embeds make the site look unfinished or abandoned.",
    "copyright": "An out of date copyright year makes the site look neglected.",
}

FREE_FIXES = {
    "meta_descriptions": ("Write your own search descriptions",
                          "In your website's page settings (or your SEO plugin), add a one or two sentence description to each main page, about 150 characters, saying what the page offers and where. Google often shows this under your link."),
    "alt_text": ("Describe your images",
                 "In your media library, add a short plain description to each photo, for example \"Sea view double room at The Blue Anchor\". It helps people using screen readers, and image search."),
    "gbp": ("Claim and complete your Google Business Profile",
            "Search for your business on Google Maps. If there is no profile, create one at business.google.com. Make sure the name, address and phone number exactly match your website, and add photos."),
    "tel_links": ("Make your phone number tappable",
                  "Wherever your number appears, link it so phones can call it in one tap. In most editors you add a link to the number and use tel:+44 followed by the number without the first zero."),
    "privacy": ("Link to a privacy policy",
                "Publish a privacy policy page and link to it in your site footer, so it appears on every page. The ICO website has guidance for small businesses."),
    "copyright": ("Update your copyright year",
                  "Change the year in your footer to the current year, so the site does not look abandoned."),
    "titles": ("Give every page its own title",
               "In your page settings, give each page a unique title of 30 to 60 characters that says what it is and where, for example \"Sea View Rooms in St Ives | The Blue Anchor\"."),
    "menu_pdf": ("Put your menu on a web page",
                 "Type your menu onto a normal page of your website instead of a PDF. It loads faster on phones, Google can read it, and you can update prices in seconds."),
}


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def visible_text(soup):
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s(["script", "style", "noscript", "template"]):
        t.decompose()
    return " ".join(s.get_text(" ").split())


def run_checks(start, args, rules, settings, log):
    crawl_cfg = rules["crawl"]
    scorer = Scorer(rules)
    notes, facts = [], {}

    log("Crawling...")
    cr = Crawler(start, args.pages or crawl_cfg["max_pages"], crawl_cfg["delay_seconds"], crawl_cfg["timeout_seconds"], log)
    pages = cr.crawl()
    if not pages:
        raise SystemExit("Could not load the site at all. Check the address and try again.")
    ok_pages, seen = [], set()
    for p in pages:  # a redirected URL and its destination are the same page; count it once
        if p.status < 400 and p.final_url.rstrip("/") not in seen:
            seen.add(p.final_url.rstrip("/"))
            ok_pages.append(p)
    home = pages[0]
    facts["pages_crawled"] = len(ok_pages)

    browser = Browser(log)
    try:
        # ---------------- Speed ----------------
        log("Measuring speed...")
        speed_urls = [home.final_url]
        sp = psi(home.final_url, settings["psi_key"], log)
        if sp is None:
            sp = browser.local_speed(home.final_url)
        facts["speed_source"] = (sp or {}).get("source")
        src_note = {"field": "real visitor data from Google", "lab": "Google's lab test on a simulated phone",
                    "local": "our own throttled phone test (an estimate)"}.get(facts["speed_source"], "not measured")
        if sp:
            lcp = sp.get("lcp")
            scorer.add("speed", "lcp", lcp, f"Main content appeared after {lcp/1000:.1f} seconds ({src_note})." if lcp else "", IMPACT["lcp"],
                       display=f"{lcp/1000:.1f}s" if lcp else None)
            resp, kind = sp.get("resp"), sp.get("resp_kind")
            label = "Interaction delay" if kind == "inp" else "Time the page was too busy to respond"
            scorer.add("speed", "responsiveness", resp, f"{label}: {resp:.0f}ms ({src_note})." if resp is not None else "",
                       IMPACT["responsiveness"], source=kind, display=f"{resp:.0f}ms" if resp is not None else None)
            cls = sp.get("cls")
            scorer.add("speed", "cls", cls, f"Layout shift score {cls:.2f}." if cls is not None else "", IMPACT["cls"],
                       display=f"{cls:.2f}" if cls is not None else None)
            scorer.add("speed", "performance", sp.get("perf"), f"Lighthouse performance score {sp.get('perf')} out of 100." if sp.get("perf") is not None else "",
                       IMPACT["performance"])
            w = sp.get("weight") or home.bytes
            scorer.add("speed", "weight", w, f"The homepage downloads about {w/1_000_000:.1f} MB.", IMPACT["weight"], display=f"{w/1_000_000:.1f} MB")
        else:
            for k in ("lcp", "responsiveness", "cls", "performance"):
                scorer.add("speed", k, None, source="tbt")
            scorer.add("speed", "weight", home.bytes, f"The homepage HTML alone is {home.bytes/1000:.0f} KB.", IMPACT["weight"])

        # ---------------- Mobile ----------------
        log("Checking mobile layout...")
        mob_urls = [p.final_url for p in ok_pages[: crawl_cfg["mobile_pages"]]]
        mob = browser.mobile_checks(mob_urls)
        if mob:
            fit = sum(1 for m in mob if not m["overflow"]) / len(mob)
            scorer.add("mobile", "overflow", fit, f"{sum(1 for m in mob if m['overflow'])} of {len(mob)} pages are wider than a phone screen.",
                       IMPACT["overflow"], display=f"{fit:.0%}")
            t_all, t_ok = sum(m["targets"] for m in mob), sum(m["bigEnough"] for m in mob)
            ratio = t_ok / t_all if t_all else None
            scorer.add("mobile", "tap_targets", ratio, f"{t_all - t_ok} of {t_all} links and buttons are too small or too close together to tap reliably." if t_all else "",
                       IMPACT["tap_targets"], display=f"{ratio:.0%}" if ratio is not None else None)
            sizes = [m["fontSize"] for m in mob if m["fontSize"]]
            fs = median(sizes) if sizes else None
            scorer.add("mobile", "font_size", fs, f"Typical body text is {fs:.0f}px on a phone." if fs else "", IMPACT["font_size"],
                       display=f"{fs:.0f}px" if fs else None)
            i_all, i_ok = sum(m["images"] for m in mob), sum(m["okImages"] for m in mob)
            ir = i_ok / i_all if i_all else 1.0
            scorer.add("mobile", "images_sized", ir, f"{i_all - i_ok} of {i_all} images are much larger than the screen needs." if i_all else "No content images found.",
                       IMPACT["images_sized"], display=f"{ir:.0%}")
        else:
            for k in ("overflow", "tap_targets", "font_size", "images_sized"):
                scorer.add("mobile", k, None)
        def _vp_ok(pg):
            vp = pg.soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
            return bool(vp and "width=device-width" in (vp.get("content") or "").replace(" ", "").lower())
        no_vp = [pg for pg in ok_pages if not _vp_ok(pg)]
        scorer.add("mobile", "viewport", "missing" if no_vp else "ok",
                   f"{len(no_vp)} of {len(ok_pages)} pages are not set up for phone screens, for example {no_vp[0].final_url}." if no_vp else "Every page is set up for phone screens.",
                   IMPACT["viewport"])
        scorer.add("mobile", "accessibility", (sp or {}).get("a11y"),
                   f"Lighthouse accessibility score {(sp or {}).get('a11y')} out of 100." if (sp or {}).get("a11y") is not None else "", IMPACT["accessibility"])
        numbers, tel_pages, number_pages = 0, 0, 0
        for p in ok_pages:
            txt = visible_text(p.soup)
            has_num = bool(PHONE_RE.search(txt))
            has_tel = bool(p.soup.select('a[href^="tel:"]'))
            if has_num:
                number_pages += 1
                tel_pages += 1 if has_tel else 0
        if number_pages == 0:
            tel = "na"
        elif tel_pages == number_pages:
            tel = "all"
        elif tel_pages:
            tel = "some"
        else:
            tel = "none"
        scorer.add("mobile", "tel_links", tel,
                   {"na": "No phone number is shown on the site.", "all": "Phone numbers are tappable.",
                    "some": f"Phone numbers are tappable on {tel_pages} of {number_pages} pages that show one.",
                    "none": "Phone numbers are shown as plain text that cannot be tapped."}[tel], IMPACT["tel_links"])

        # ---------------- Findability ----------------
        log("Checking findability...")
        titles = [(p.soup.title.get_text(strip=True) if p.soup.title else "") for p in ok_pages]
        good_titles = [t for t in titles if 15 <= len(t) <= 65 and titles.count(t) == 1]
        tr = len(good_titles) / len(titles) if titles else None
        scorer.add("findability", "titles", tr, f"{len(titles) - len(good_titles)} of {len(titles)} pages have a missing, repeated or badly sized title.",
                   IMPACT["titles"], display=f"{tr:.0%}" if tr is not None else None)
        descs = []
        for p in ok_pages:
            m = p.soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
            descs.append((m.get("content") or "").strip() if m else "")
        good_descs = [d for d in descs if 50 <= len(d) <= 160 and descs.count(d) == 1]
        dr = len(good_descs) / len(descs) if descs else None
        scorer.add("findability", "meta_descriptions", dr, f"{len(descs) - len(good_descs)} of {len(descs)} pages have a missing, repeated or badly sized description.",
                   IMPACT["meta_descriptions"], display=f"{dr:.0%}" if dr is not None else None)
        h1_ok = sum(1 for p in ok_pages if len(p.soup.find_all("h1")) == 1)
        hr = h1_ok / len(ok_pages) if ok_pages else None
        scorer.add("findability", "h1", hr, f"{len(ok_pages) - h1_ok} of {len(ok_pages)} pages do not have exactly one main heading.", IMPACT["h1"],
                   display=f"{hr:.0%}" if hr is not None else None)
        blocked = []
        key_pages = [home] + [p for p in ok_pages[1:6]]
        for p in key_pages:
            robots_meta = p.soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
            noindex = (robots_meta and "noindex" in (robots_meta.get("content") or "").lower()) or \
                      "noindex" in (p.headers.get("X-Robots-Tag", "") or "").lower()
            if noindex or not cr.allowed(p.final_url):
                blocked.append(p.final_url)
        scorer.add("findability", "indexable", "blocked" if blocked else "ok",
                   f"{len(blocked)} important page(s) are hidden from Google, including {blocked[0]}." if blocked else "Key pages are open to Google.",
                   IMPACT["indexable"])
        root = "{0.scheme}://{0.netloc}".format(urlparse(home.final_url))
        listed = [l.split(":", 1)[1].strip() for l in cr.robots_txt.splitlines() if l.lower().startswith("sitemap:")]
        found = None
        for cand in listed + [root + "/sitemap.xml", root + "/sitemap_index.xml", root + "/wp-sitemap.xml"]:
            st, _, h = cr.status_of(cand)
            if st == 200:
                found = cand
                break
        sm = "listed" if (found and listed) else ("found" if found else "none")
        scorer.add("findability", "sitemap", sm, {"listed": "A sitemap was found and is listed in robots.txt.",
                                                    "found": "A sitemap was found but is not listed in robots.txt.",
                                                    "none": "No XML sitemap was found."}[sm], IMPACT["sitemap"])
        schema_state, schema_types = "none", []
        for p in [home] + ok_pages[1:3]:
            for s in p.soup.find_all("script", attrs={"type": "application/ld+json"}):
                try:
                    data = json.loads(s.string or "")
                except (ValueError, TypeError):
                    schema_state = "errors" if schema_state == "none" else schema_state
                    continue
                items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    types = it.get("@type", [])
                    types = types if isinstance(types, list) else [types]
                    for t in types:
                        if str(t).lower() in SCHEMA_TYPES:
                            schema_types.append(t)
                            schema_state = "valid" if it.get("name") else ("errors" if schema_state != "valid" else schema_state)
        scorer.add("findability", "schema", schema_state,
                   {"valid": f"Business details are marked up ({', '.join(sorted(set(map(str, schema_types))))}).",
                    "errors": "Business details markup was found but is incomplete or broken.",
                    "none": "No business details markup was found."}[schema_state], IMPACT["schema"])
        imgs = [img for p in ok_pages for img in p.soup.find_all("img") if not (img.get("width") and str(img.get("width")).isdigit() and int(img.get("width")) < 40)]
        with_alt = [i for i in imgs if i.has_attr("alt")]
        ar = len(with_alt) / len(imgs) if imgs else 1.0
        scorer.add("findability", "alt_text", ar, f"{len(imgs) - len(with_alt)} of {len(imgs)} images have no description." if imgs else "No content images found.",
                   IMPACT["alt_text"], display=f"{ar:.0%}")
        gbp = args.gbp
        scorer.add("findability", "gbp", gbp, {"match": "A Google Business Profile was found and matches the website.",
                                               "mismatch": "A Google Business Profile was found, but its details do not match the website.",
                                               "none": "No Google Business Profile was found.",
                                               "na": "Not applicable for this business."}.get(gbp, "Not checked yet."), IMPACT["gbp"])

        # ---------------- Trust and security ----------------
        log("Checking trust and security...")
        host = urlparse(home.final_url).hostname
        https_state = "none"
        try:
            r = cr.get(f"https://{host}/")
            if r.status_code < 400:
                https_state = "no_redirect"
                r2 = cr.get(f"http://{host}/")
                if r2.url.startswith("https://"):
                    https_state = "enforced"
        except requests.RequestException:
            pass
        scorer.add("trust", "https", https_state, {"enforced": "Secure HTTPS is used and enforced.",
                                                     "no_redirect": "HTTPS works, but the insecure http address does not redirect to it.",
                                                     "none": "The site does not load securely over HTTPS."}[https_state], IMPACT["https"])
        days = None
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    exp = datetime.strptime(ss.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    days = (exp - datetime.now(timezone.utc)).days
        except Exception:
            days = None
        scorer.add("trust", "certificate", days, f"The security certificate has {days} days left." if days is not None else ("No certificate, see Secure connection." if https_state == "none" else ""), IMPACT["certificate"])
        hdr = {k.lower(): v for k, v in home.headers.items()}
        present = [n for n, ok in [("HSTS", "strict-transport-security" in hdr), ("X-Content-Type-Options", "x-content-type-options" in hdr),
                                   ("Referrer-Policy", "referrer-policy" in hdr),
                                   ("Frame protection", "x-frame-options" in hdr or "frame-ancestors" in hdr.get("content-security-policy", ""))] if ok]
        scorer.add("trust", "headers", len(present), f"{len(present)} of 4 recommended security headers are set" + (f" ({', '.join(present)})." if present else "."),
                   IMPACT["headers"], display=f"{len(present)} of 4")
        is_wp = "/wp-content/" in home.html or "/wp-includes/" in home.html
        gen = home.soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
        wp_ver = None
        if gen and "wordpress" in (gen.get("content") or "").lower():
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", gen["content"])
            wp_ver = m.group(1) if m else None
        cms_state, cms_detail = ("not_wordpress", "The site does not appear to run WordPress.") if not is_wp else ("hidden", "The WordPress version is not shown publicly.")
        latest_wp = None
        if is_wp:
            try:
                latest_wp = requests.get("https://api.wordpress.org/core/version-check/1.7/", timeout=15).json()["offers"][0]["current"]
            except Exception:
                latest_wp = None
        if is_wp and wp_ver:
            if latest_wp:
                lv, cv = [int(x) for x in latest_wp.split(".")[:2]], [int(x) for x in wp_ver.split(".")[:2]]
                behind = (lv[0] - cv[0]) * 10 + (lv[1] - cv[1])
                cms_state = "current" if behind <= 0 else ("one_behind" if behind == 1 else "older")
                cms_detail = f"WordPress {wp_ver} is shown publicly; the latest is {latest_wp}."
            else:
                cms_state, cms_detail = None, f"WordPress {wp_ver} is shown publicly (latest version could not be checked)."
        scorer.add("trust", "cms_version", cms_state, cms_detail, IMPACT["cms_version"])
        plugins = {}
        for p in ok_pages[:5]:
            for m in re.finditer(r"/wp-content/plugins/([a-z0-9\-_]+)/[^\"'\s>]*?\?ver=([0-9][0-9.]*)", p.html):
                plugins.setdefault(m.group(1), m.group(2))
        outdated, looked_up = [], 0
        for slug, ver in list(plugins.items())[:15]:
            try:
                info = requests.get("https://api.wordpress.org/plugins/info/1.2/",
                                    params={"action": "plugin_information", "request[slug]": slug, "request[fields][sections]": 0},
                                    timeout=15).json()
                latest = info.get("version")
                looked_up += 1 if latest else 0
                if latest and _vtuple(ver) < _vtuple(latest) and _vtuple(ver)[:1] != (0,):
                    outdated.append(f"{slug} {ver} (latest {latest})")
            except Exception:
                continue
        if not is_wp or not plugins:
            pl_state, pl_detail = "none_visible", "No plugin versions are visible from outside."
        elif not looked_up:
            pl_state, pl_detail = None, f"{len(plugins)} plugins are visible (latest versions could not be checked)."
        else:
            pl_state = "current" if not outdated else ("one_outdated" if len(outdated) == 1 else "outdated")
            pl_detail = f"{len(plugins)} plugins are visible; {len(outdated)} look out of date" + (f": {', '.join(outdated[:4])}." if outdated else ".")
        scorer.add("trust", "plugins", pl_state, pl_detail, IMPACT["plugins"])
        mixed = []
        for p in ok_pages:
            if not p.final_url.startswith("https://"):
                continue
            for tag, attr in (("img", "src"), ("script", "src"), ("link", "href"), ("iframe", "src"), ("source", "src")):
                for el in p.soup.find_all(tag):
                    v = el.get(attr) or ""
                    if v.startswith("http://") and (tag != "link" or "stylesheet" in (el.get("rel") or [])):
                        mixed.append(v)
        scorer.add("trust", "mixed_content", "found" if mixed else "ok",
                   f"{len(mixed)} file(s) load over an insecure connection." if mixed else "Everything loads securely.", IMPACT["mixed_content"])
        form_pages = [p for p in ok_pages if p.soup.find("form") and not all(_is_search_form(f) for f in p.soup.find_all("form"))]
        if not form_pages:
            fstate, fdetail = "no_forms", "No contact or enquiry forms were found."
        else:
            unprotected = [p.final_url for p in form_pages if not SPAM_PROTECTION_RE.search(p.html)]
            fstate = "unprotected" if unprotected else "protected"
            fdetail = f"{len(unprotected)} of {len(form_pages)} pages with forms show no sign of spam protection." if unprotected else "Forms show signs of spam protection."
        scorer.add("trust", "forms", fstate, fdetail, IMPACT["forms"])
        priv = None
        for a in home.soup.find_all("a", href=True):
            if re.search(r"privacy", a.get_text(" ") + " " + a["href"], re.I):
                priv = urljoin(home.final_url, a["href"])
                break
        priv_ok = False
        if priv:
            st, _, _ = cr.status_of(priv)
            priv_ok = st == 200
        scorer.add("trust", "privacy", "ok" if priv_ok else "missing",
                   "A privacy policy is linked and loads." if priv_ok else ("A privacy policy link was found but the page does not load." if priv else "No privacy policy link was found."),
                   IMPACT["privacy"])

        # ---------------- Housekeeping ----------------
        log("Checking links and housekeeping...")
        broken, chains = [], []
        for i, (url, sources) in enumerate(cr.links.items()):
            if i >= crawl_cfg["max_link_checks"]:
                break
            known = next((p for p in pages if p.url == url), None)
            if known:
                st, hops = known.status, len(known.redirects)
            else:
                st, hops, _ = cr.status_of(url)
            if st is None or st >= 400:
                broken.append((url, sorted(sources)[0]))
            if hops > 1:
                chains.append(url)
        scorer.add("housekeeping", "broken_links", len(broken),
                   f"{len(broken)} broken internal link(s) found" + (f", for example {broken[0][0]} on {broken[0][1]}." if broken else "."),
                   IMPACT["broken_links"])
        scorer.add("housekeeping", "redirect_chains", len(chains), f"{len(chains)} link(s) pass through more than one redirect.", IMPACT["redirect_chains"])
        heavy = []
        for i, (src, on) in enumerate(cr.images.items()):
            if i >= crawl_cfg["max_image_checks"]:
                break
            if not cr.internal(src):
                continue
            st, _, h = cr.status_of(src)
            size = int(h.get("Content-Length", 0) or 0) if h else 0
            if size > 300_000:
                heavy.append(f"{src.rsplit('/', 1)[-1]} ({size/1000:.0f} KB)")
        scorer.add("housekeeping", "oversized_images", len(heavy),
                   f"{len(heavy)} image(s) are over 300 KB" + (f", including {heavy[0]}." if heavy else "."), IMPACT["oversized_images"])
        token = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        st404, links404, words404, final404 = browser.page_404(f"{root}/cm-health-check-{token}/")
        if st404 == 404:
            p404 = "good" if (links404 >= 3 and words404 >= 15) else "bare"
        else:
            p404 = "bad"
        scorer.add("housekeeping", "page_404", p404,
                   {"good": "Missing pages return a proper, helpful not found page.",
                    "bare": "Missing pages return the right status, but the page offers little help.",
                    "bad": f"A missing page returned status {st404} instead of 404" + (" and was sent to another address." if final404.rstrip('/') != f"{root}/cm-health-check-{token}" else ".")}[p404],
                   IMPACT["page_404"])
        left = []
        for p in ok_pages:
            txt = visible_text(p.soup)
            m = PLACEHOLDER_RE.search(txt)
            if m:
                left.append(f"\"{m.group(0)}\" on {p.final_url}")
            for pat in LEGACY_MAP_PATTERNS:
                if pat in p.html:
                    left.append(f"an old Google Maps embed on {p.final_url}")
                    break
        scorer.add("housekeeping", "leftovers", len(left), f"Found {len(left)}: {'; '.join(left[:3])}." if left else "No leftover placeholders or dead embeds found.",
                   IMPACT["leftovers"])
        year = datetime.now().year
        foot_txt = visible_text(home.soup.find("footer") or home.soup)
        years = [int(y) for pair in re.findall(r"(?:©|&copy;|copyright)[^0-9]{0,20}((?:19|20)\d\d)(?:\s*(?:-|to|–)\s*((?:19|20)\d\d))?", foot_txt, re.I) for y in pair if y]
        cr_state = "none" if not years else ("current" if max(years) >= year else "old")
        scorer.add("housekeeping", "copyright", cr_state, {"none": "No copyright year shown.", "current": f"Copyright year is up to date ({max(years) if years else ''}).",
                                                            "old": f"Copyright year shows {max(years) if years else ''}."}[cr_state], IMPACT["copyright"])

        # ---------------- Hospitality notes ----------------
        if args.business_type == "hospitality":
            pdf_menus = []
            for url in cr.links:
                if url.lower().endswith(".pdf") and MENU_RE.search(url.rsplit("/", 1)[-1]):
                    pdf_menus.append(url.rsplit("/", 1)[-1])
            for p in ok_pages:
                for a in p.soup.find_all("a", href=True):
                    if a["href"].lower().split("?")[0].endswith(".pdf") and MENU_RE.search(a.get_text(" ")):
                        pdf_menus.append(a.get_text(" ", strip=True)[:40])
            if pdf_menus:
                notes.append(("Your menu or prices are a PDF", "Menus and price lists sit in PDF files, which are slow and awkward on phones and hard for Google to read. Diners decide fast."))
                facts["menu_pdf"] = True
            # Look at links, buttons and forms, not body copy ("call us to book a table" is not a way to book)
            actions = [(el.get_text(" ", strip=True), el.get("href", "") or "", el.get("value", "") or "")
                       for p in ok_pages for el in p.soup.find_all(["a", "button", "input"])]
            has_booking = any(BOOKING_RE.search(t + " " + v) or any(h in href.lower() for h in BOOKING_HOSTS) for t, href, v in actions) \
                or any(any(h in (f.get("src", "") or "").lower() for h in BOOKING_HOSTS) for p in ok_pages for f in p.soup.find_all(["iframe", "script"]))
            has_signup = any(SIGNUP_RE.search(t + " " + v + " " + href) for t, href, v in actions) or any(
                f.find("input", attrs={"type": "email"}) and not f.find("textarea") and SIGNUP_RE.search(f.get_text(" ") + " " + str(f.get("class", "")) + " " + (f.get("action") or ""))
                for p in ok_pages for f in p.soup.find_all("form"))
            if not has_booking:
                notes.append(("No obvious way to book", "We could not find a clear \"Book now\" or \"Reserve\" button. Guests who cannot book quickly often book with a competitor or through a commission-taking site."))
            if not has_signup:
                notes.append(("No way to stay in touch with guests", "There is no newsletter or offers sign-up, so past guests have no easy way to hear about your next season or special offers."))
            if any(pat in p.html for p in ok_pages for pat in LEGACY_MAP_PATTERNS):
                notes.append(("Your map may not be working", "An old style Google Map embed was found. Many of these now show a grey box or a warning instead of a map."))
    finally:
        browser.close()

    return scorer, notes, facts


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _is_search_form(f):
    names = " ".join((i.get("name") or "") + " " + (i.get("type") or "") for i in f.find_all("input"))
    return bool(re.search(r"\bs\b|search", names)) and not f.find("textarea")


# ---------------------------------------------------------------------------
# Free fix choice
# ---------------------------------------------------------------------------

def choose_free_fix(scorer, facts, requested):
    if requested and requested != "auto":
        if requested not in FREE_FIXES:
            raise SystemExit(f"Unknown free fix '{requested}'. Options: auto, {', '.join(FREE_FIXES)}")
        return requested
    if facts.get("menu_pdf"):
        return "menu_pdf"
    best, best_lost = None, 0
    for r in scorer.results:
        if r.key in FREE_FIXES and r.earned is not None:
            lost = r.points - r.earned
            if lost > best_lost:
                best, best_lost = r.key, lost
    return best


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(start, args, scorer, notes, facts, out_dir, log):
    cats = scorer.category_scores()
    total = sum(c["score"] for c in cats)
    band, band_blurb = scorer.band(total)
    fix_key = choose_free_fix(scorer, facts, args.free_fix)
    fix = FREE_FIXES.get(fix_key) if fix_key else None
    host = urlparse(start).hostname.removeprefix("www.")
    findings = []
    for c in cats:
        lost = [r for r in c["results"] if r.earned is not None and r.earned < r.points]
        lost.sort(key=lambda r: r.points - r.earned, reverse=True)
        c["lost"] = lost
        c["good"] = [r for r in c["results"] if r.earned is not None and r.earned >= r.points]
        c["pct"] = round(100 * c["score"] / c["max"]) if c["max"] else 0
        findings.extend(lost)
    context = {
        "name": args.name or host, "host": host, "url": start,
        "date": datetime.now().strftime("%-d %B %Y") if os.name != "nt" else datetime.now().strftime("%d %B %Y"),
        "total": total, "band": band, "band_blurb": band_blurb, "cats": cats,
        "fix": {"title": fix[0], "body": fix[1]} if fix else None,
        "notes": notes, "rank": args.rank, "facts": facts,
        "speed_source": {"field": "real visitor data from Google", "lab": "Google's PageSpeed lab test",
                         "local": "our own throttled phone test"}.get(facts.get("speed_source"), "not measured"),
        "version": VERSION,
    }
    env = Environment(loader=FileSystemLoader(str(HERE)), autoescape=select_autoescape(["html"]))
    html = env.get_template("template.html").render(**context)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{host}-{datetime.now():%Y-%m-%d}"
    html_path, pdf_path, json_path = out_dir / f"{stem}.html", out_dir / f"{stem}.pdf", out_dir / f"{stem}.json"
    html_path.write_text(html, encoding="utf-8")
    json_path.write_text(json.dumps({"site": start, "name": context["name"], "date": context["date"], "total": total, "band": band,
                                     "categories": [{"key": c["key"], "score": c["score"], "max": c["max"], "unmeasured": c["unmeasured"]} for c in cats],
                                     "results": [asdict(r) for r in scorer.results], "free_fix": fix_key, "notes": notes, "facts": facts},
                                    indent=2, default=str), encoding="utf-8")
    log("Rendering PDF...")
    b = Browser(log)
    try:
        import html as _html
        b.pdf(html_path, pdf_path, footer=_html.escape(f"{context['name']} \u00b7 {context['date']}"))
    finally:
        b.close()
    return total, band, pdf_path, json_path


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Creative Mushroom Website Health Check")
    ap.add_argument("url", help="the website to check, e.g. https://www.example.co.uk")
    ap.add_argument("--name", help="business name for the report cover")
    ap.add_argument("--business-type", default="other", choices=["hospitality", "other"], help="hospitality adds extra notes")
    ap.add_argument("--gbp", choices=["match", "mismatch", "none", "na"], help="Google Business Profile result, checked by hand")
    ap.add_argument("--rank", help="a note about their Google position, shown in the report as written")
    ap.add_argument("--free-fix", default="auto", help="auto, or one of: " + ", ".join(FREE_FIXES))
    ap.add_argument("--pages", type=int, help="maximum pages to crawl (default from scoring.json)")
    ap.add_argument("--out", default=str(HERE / "reports"), help="folder for the PDF and data")
    args = ap.parse_args()

    start = args.url.strip()
    if not re.match(r"^https?://", start):
        try:  # try the secure address first, fall back to plain http if the site has no certificate
            requests.head("https://" + start, timeout=15, allow_redirects=True)
            start = "https://" + start
        except requests.RequestException:
            start = "http://" + start
    log = lambda m: print(m, flush=True)
    rules, settings = load_rules(), load_settings()
    if not settings["psi_key"]:
        log("Note: no PageSpeed Insights API key found in settings.ini, so speed may use the rougher local test.")
    log(f"Checking {start}")
    scorer, notes, facts = run_checks(start, args, rules, settings, log)
    total, band, pdf_path, json_path = build_report(start, args, scorer, notes, facts, Path(args.out), log)
    log(f"\nScore: {total}/100 ({band})")
    for c in scorer.category_scores():
        extra = f"  [not measured: {', '.join(c['unmeasured'])}]" if c["unmeasured"] else ""
        log(f"  {c['label']:<20} {c['score']:>3}/{c['max']}{extra}")
    if not args.gbp:
        log("Reminder: Google Business Profile was not checked. Look it up and re-run with --gbp match|mismatch|none|na.")
    log(f"\nReport: {pdf_path}\nData:   {json_path}")


if __name__ == "__main__":
    main()
