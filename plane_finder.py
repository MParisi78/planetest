#!/usr/bin/env python3
"""
plane_finder.py
================
Daily Cessna 172 hunter. Pulls current listings, filters them to YOUR criteria,
scores and ranks the top 10, flags any "unicorn", and emails you the digest.

YOUR CRITERIA (edit in the CONFIG block below):
  - Cessna 172 (any variant)
  - Year >= 1975
  - No reported damage history
  - Low total time preferred
  - Price <= $75,000  ... UNLESS it's a unicorn worth stretching for

UNICORN = a standout that breaks the normal rules, e.g.:
  - A 1975+ clean, low-time 172 that somehow lists at/under $75k (rare), OR
  - A late-model 172R/172S with very low time + no damage (the "won't outgrow it" plane)

------------------------------------------------------------------------------
IMPORTANT HONESTY NOTE
------------------------------------------------------------------------------
Trade-A-Plane / Controller / etc. do not all offer clean public APIs, and some
actively discourage scraping in their Terms of Service. This script is built
defensively: it uses a polite request rate, a real User-Agent, and is structured
so you can plug in official feeds where they exist. If a site changes its HTML
or blocks requests, the PARSER for that site is the only thing you need to fix --
everything else (scoring, ranking, email, scheduling) keeps working.

Treat this as a personal-use research tool. Respect each site's robots.txt and ToS.
------------------------------------------------------------------------------
"""

import os
import re
import sys
import json
import time
import smtplib
import datetime as dt
from dataclasses import dataclass, field, asdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# =============================================================================
# CONFIG  -- everything you'd want to tweak lives here
# =============================================================================
CONFIG = {
    # --- search criteria ---
    "min_year": 1975,
    "price_ceiling": 75_000,        # your hard budget
    "unicorn_price_stretch": 140_000,  # a true unicorn may justify going this high
    "max_total_time": 6_000,        # hours; above this we down-score (not exclude)
    "require_no_damage": True,      # drop listings that mention damage history

    # --- ranking weights (higher = matters more) ---
    "weights": {
        "price": 3.0,          # lower price scores higher
        "total_time": 2.0,     # lower airframe time scores higher
        "engine_smoh": 2.0,    # lower hours since major overhaul scores higher
        "ifr_ready": 1.5,      # already IFR / Garmin / ADS-B
        "no_damage": 2.5,      # clean history
        "year": 1.0,           # newer scores higher
    },

    # --- how many to send ---
    "top_n": 10,

    # --- email settings (use env vars, NOT hardcoded passwords) ---
    "email": {
        "enabled": True,
        "smtp_host": os.environ.get("PF_SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("PF_SMTP_PORT", "587")),
        "username": os.environ.get("PF_SMTP_USER", ""),     # your email
        "password": os.environ.get("PF_SMTP_PASS", ""),     # an APP PASSWORD, not your login
        "to_addr":  os.environ.get("PF_TO_ADDR", ""),       # where the digest goes
    },

    # --- saved-search alert ingestion (IMAP) ---
    # Sites that block scraping (Trade-A-Plane, Controller) still send free
    # saved-search ALERT emails server-side. Point this at the mailbox that
    # receives them and the finder folds those listings in. Enabled when
    # PF_IMAP_USER is set.
    "imap": {
        "host": os.environ.get("PF_IMAP_HOST", "imap.gmail.com"),
        "port": int(os.environ.get("PF_IMAP_PORT", "993")),
        "user": os.environ.get("PF_IMAP_USER", ""),
        "password": os.environ.get("PF_IMAP_PASS", ""),  # app password, not your login
        "folder": os.environ.get("PF_IMAP_FOLDER", "INBOX"),
        "since_days": int(os.environ.get("PF_IMAP_SINCE_DAYS", "7")),
    },

    # --- US-only ---
    # Drop listings whose location is clearly outside the US (keep unknowns).
    "us_only": True,

    # --- politeness / crawl limits ---
    "request_timeout": 20,
    # Per-source cap on how many detail pages we fetch (prices/specs live there).
    "max_detail_fetches": int(os.environ.get("PF_MAX_DETAILS", "40")),
    "max_pages": int(os.environ.get("PF_MAX_PAGES", "4")),  # search-result pages per source
    "fetch_delay": float(os.environ.get("PF_FETCH_DELAY", "0.6")),  # seconds between requests
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/123.0 Safari/537.36",
    # On GitHub Actions we want this in the repo so it can be committed between
    # runs. Locally it falls back to your home directory. Override with PF_STATE.
    "state_file": os.environ.get(
        "PF_STATE",
        "plane_finder_seen.json" if os.environ.get("GITHUB_ACTIONS")
        else os.path.expanduser("~/.plane_finder_seen.json"),
    ),
}


# =============================================================================
# DATA MODEL
# =============================================================================
@dataclass
class Listing:
    source: str
    title: str
    year: int | None = None
    model: str = ""
    price: int | None = None
    total_time: int | None = None      # airframe hours
    engine_smoh: int | None = None      # hours since major overhaul
    damage_history: bool | None = None  # True = has damage, False = clean, None = unknown
    ifr_ready: bool = False
    url: str = ""
    location: str = ""
    score: float = 0.0
    unicorn: bool = False
    # auction-specific (AircraftBidder)
    auction: bool = False
    bids: int | None = None
    status: str = ""
    matches: bool = False   # for auction lots: does it meet the buy criteria?
    reasons: list = field(default_factory=list)

    @property
    def uid(self) -> str:
        """Stable id so we don't re-alert on the same plane every day."""
        base = (self.url or f"{self.source}-{self.title}-{self.price}").lower()
        return re.sub(r"\s+", "", base)


# =============================================================================
# PARSERS  -- one function per site. THESE are the brittle part.
# If a site changes layout, fix only the matching parser below.
# =============================================================================
def _get(url: str, referer: str | None = None) -> str | None:
    headers = {
        "User-Agent": CONFIG["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=CONFIG["request_timeout"])
        if r.status_code == 200:
            return r.text
        print(f"  [warn] {url} returned HTTP {r.status_code}")
    except requests.RequestException as e:
        print(f"  [warn] request failed for {url}: {e}")
    return None


def _num(text: str) -> int | None:
    """Pull the first integer out of a messy string like '$74,500' or '3,150 TT'."""
    if not text:
        return None
    m = re.search(r"[\d,]+", str(text).replace(".", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


# --- US vs non-US location detection ------------------------------------------
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
_NON_US = re.compile(
    r"\b("
    # North America (non-US) + provinces
    r"canada|quebec|ontario|alberta|manitoba|saskatchewan|british columbia|"
    r"nova scotia|newfoundland|new brunswick|prince edward|"
    # Europe
    r"united kingdom|england|scotland|wales|ireland|germany|france|spain|"
    r"italy|netherlands|holland|belgium|luxembourg|switzerland|austria|sweden|"
    r"norway|denmark|finland|iceland|poland|czech|slovakia|hungary|romania|"
    r"bulgaria|serbia|croatia|slovenia|portugal|greece|cyprus|malta|estonia|"
    r"latvia|lithuania|ukraine|belarus|moldova|"
    # Americas (non-US)
    r"mexico|brazil|argentina|chile|colombia|peru|ecuador|bolivia|paraguay|"
    r"uruguay|venezuela|guatemala|panama|costa rica|dominican|bahamas|"
    # Africa / Middle East
    r"morocco|egypt|south africa|kenya|nigeria|ghana|tunisia|algeria|"
    r"uae|united arab|dubai|abu dhabi|saudi|qatar|kuwait|bahrain|oman|israel|"
    r"jordan|lebanon|turkey|"
    # Asia / Oceania
    r"australia|new zealand|japan|china|hong kong|taiwan|south korea|"
    r"india|pakistan|bangladesh|sri lanka|thailand|vietnam|malaysia|singapore|"
    r"indonesia|philippines|"
    r"russia"
    r")\b", re.I)


def _is_us(location: str | None) -> bool | None:
    """True if clearly US, False if clearly non-US, None if unknown."""
    if not location:
        return None
    low = location.lower()
    if _NON_US.search(low):
        return False
    if "usa" in low or "u.s.a" in low or "united states" in low:
        return True
    if any(st in low for st in _US_STATE_NAMES):
        return True
    if any(tok in _US_STATE_ABBR for tok in re.findall(r"\b[A-Z]{2}\b", location)):
        return True
    return None


def _extract_smoh(text: str) -> int | None:
    """Engine hours since major overhaul, from free text like '635 hours SMOH'."""
    m = re.search(
        r"([\d,]{1,5})\s*(?:hrs?\.?\s*)?(?:hours?\s*)?"
        r"(?:since\s+(?:major\s+)?overhaul|smoh|stoh)",
        (text or "").lower())
    return _num(m.group(1)) if m else None


def _extract_tt(text: str) -> int | None:
    """Airframe total time from free text: '3,150 TT', '7,770 Hours Total Time'."""
    m = re.search(
        r"([\d,]{2,6})\s*(?:hrs?|hours?)?\s*"
        r"(?:total time|tt(?:sn|af)?|ttaf|airframe|time since new)",
        (text or "").lower())
    return _num(m.group(1)) if m else None


def _detect_damage(text: str) -> bool | None:
    """False = explicitly clean, True = damage mentioned, None = unknown.

    Checks the "clean" phrasing first so a "Damage History: No" field (or a nav
    link to a Salvage category) is never mistaken for actual damage.
    """
    low = (text or "").lower()
    if re.search(
            r"no (?:known |reported )?damage(?: history)?|damage[- ]free|"
            r"no accident|accident[ /]*free|"
            r"damage history[:\s]*\b(?:no|none|nil)\b|"
            r"clean (?:accident|incident|damage)?[ /]*(?:history|record)", low):
        return False
    if re.search(
            r"\bsalvage(?:d| title| project)\b|\bwrecked\b|prop(?:eller)? strike|"
            r"gear[- ]?up landing|substantial damage|sustained damage|"
            r"\bhas damage\b|damage history[:\s]*\byes\b", low):
        return True
    return None


def _detect_ifr(text: str) -> bool:
    return bool(re.search(
        r"\bifr\b|garmin|g5\b|gtn|gns|ads-?b|glass|g1000|gfc|waas|aspen|avidyne",
        (text or "").lower()))


def _model_variant(title: str, fallback: str = "172") -> str:
    mm = re.search(r"172\s?([a-z]{1,2})\b", title.lower())
    return "172" + mm.group(1).upper() if mm else fallback


def _listing_from_text(source: str, title: str, body: str, url: str,
                       location: str = "") -> Listing:
    """Extract structured fields from free-text listing copy."""
    year = None
    ym = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", title) or re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", body)
    if ym:
        year = int(ym.group(0))
        if year > dt.date.today().year + 1:   # guard against stray future dates
            year = None

    price = None
    pm = re.search(r"\$\s?([\d,]{4,})", body)
    if pm:
        price = _num(pm.group(1))

    return Listing(
        source=source, title=title, year=year, model=_model_variant(title),
        price=price, total_time=_extract_tt(body), engine_smoh=_extract_smoh(body),
        damage_history=_detect_damage(body), ifr_ready=_detect_ifr(body),
        url=url, location=location,
    )


# --- GlobalAir: detail pages carry rich schema.org JSON-LD --------------------
def _globalair_item(html: str) -> dict | None:
    """Return the schema.org aircraft item dict from a GlobalAir detail page."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", {"type": "application/ld+json"}):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("@type") == "ItemList":
            for el in obj.get("itemListElement", []):
                if isinstance(el, dict) and isinstance(el.get("item"), dict):
                    return el["item"]
    return None


def _listing_from_globalair(item: dict, url: str) -> Listing:
    def ga(key, default=""):
        return item.get("ga:" + key, default) or ""

    name = item.get("name") or "Cessna 172"

    year = item.get("vehicleModelDate") or ga("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    if year and year > dt.date.today().year + 1:
        year = None

    price = None
    offers = item.get("offers") or {}
    if isinstance(offers, dict) and offers.get("price"):
        price = _num(str(offers["price"]))
    if price is None:
        price = _num(str(ga("aircraftPrice")))

    avionics = f"{ga('avionicsPackage')} {ga('avionicsDetails')}"
    history = f"{ga('aircraftMaintenance')} {ga('airframeDetails')} {ga('shortSummary')}"
    desig = str(ga("aircraftDesignation") or "172")

    return Listing(
        source="GlobalAir", title=name, year=year,
        model=_model_variant(name, desig if desig.startswith("172") else "172"),
        price=price, total_time=_num(str(ga("totalTime"))),
        engine_smoh=_extract_smoh(str(ga("engineDetails")) + " " + str(ga("propellerDetails"))),
        damage_history=_detect_damage(history),
        ifr_ready=_detect_ifr(avionics),
        url=url, location=str(ga("aircraftLocation")),
    )


def parse_globalair(list_html: str) -> list[Listing]:
    out: list[Listing] = []
    base = "https://www.globalair.com/aircraft-for-sale/cessna-172"
    pages = [list_html] if list_html else []
    # walk a few result pages so we have a real pool to rank, not just page 1
    for p in range(2, CONFIG["max_pages"] + 1):
        h = _get(f"{base}?page={p}", referer=base)
        time.sleep(CONFIG["fetch_delay"])
        if not h:
            break
        pages.append(h)

    seen, detail_urls = set(), []
    for html in pages:
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            href = a["href"]
            if "listing-detail" in href:
                if href.startswith("/"):
                    href = "https://www.globalair.com" + href
                if href not in seen:
                    seen.add(href)
                    detail_urls.append(href)

    for url in detail_urls[:CONFIG["max_detail_fetches"]]:
        html = _get(url, referer=base)
        time.sleep(CONFIG["fetch_delay"])
        item = _globalair_item(html)
        if item:
            out.append(_listing_from_globalair(item, url))
    return out


# --- Barnstormers: category page -> per-listing detail pages (free text) ------
def parse_barnstormers(list_html: str) -> list[Listing]:
    out: list[Listing] = []
    if not list_html:
        return out
    soup = BeautifulSoup(list_html, "html.parser")
    seen, details = set(), []
    for a in soup.find_all("a", href=True):
        href, text = a["href"], a.get_text(" ", strip=True)
        if re.search(r"/classified-\d+", href) and "172" in text:
            if href.startswith("/"):
                href = "https://www.barnstormers.com" + href
            href = href.split("?")[0]
            if href not in seen:
                seen.add(href)
                details.append((href, text))
    ref = "https://www.barnstormers.com/category-17352-Cessna.html"
    for href, title in details[:CONFIG["max_detail_fetches"]]:
        html = _get(href, referer=ref)
        time.sleep(CONFIG["fetch_delay"])
        if not html:
            out.append(_listing_from_text("Barnstormers", title, title, href))
            continue
        soup2 = BeautifulSoup(html, "html.parser")
        # Parse specs from the ad body only — the site-wide nav lists a
        # "Salvage" category that would otherwise trip damage detection.
        container = soup2.find("div", class_="listings")
        ad = container.get_text(" ", strip=True) if container else title
        page_text = soup2.get_text(" ", strip=True)
        loc = ""
        lm = re.search(r"located\s+([A-Za-z][A-Za-z .,'\-]{2,40})", page_text)
        if lm:
            loc = lm.group(1).strip(" .,")
        out.append(_listing_from_text("Barnstormers", title, ad, href, location=loc))
    return out


# --- AircraftBidder: online aircraft auctions ---------------------------------
AIRCRAFTBIDDER_BROWSE = "https://auction.aircraftbidder.com/Browse/C161397/Aircraft"


def parse_aircraftbidder(list_html: str) -> list[Listing]:
    """Cessna auction lots. price = current/starting bid; marks auction=True."""
    out: list[Listing] = []
    if not list_html:
        return out
    base = "https://auction.aircraftbidder.com"
    soup = BeautifulSoup(list_html, "html.parser")
    seen, lots = set(), []
    for card in soup.select("div.galleryUnit"):
        anchors = card.find_all("a", href=re.compile(r"/Listing/Details/"))
        if not anchors:
            continue
        # the image link has no text; use the anchor that carries the title
        a = max(anchors, key=lambda x: len(x.get_text(strip=True)))
        title = a.get_text(" ", strip=True)
        if "cessna" not in title.lower():
            continue
        href = a["href"]
        if href.startswith("/"):
            href = base + href
        if href in seen:
            continue
        seen.add(href)
        lots.append((href, title, card.get_text(" ", strip=True)))

    for href, raw_title, card_text in lots[:CONFIG["max_detail_fetches"]]:
        title = re.sub(r"^\s*\d+\s*Bid\(s\)\s*", "", raw_title).strip()
        bm = re.search(r"(\d+)\s*Bid\(s\)", raw_title) or re.search(r"(\d+)\s*Bid\(s\)", card_text)
        bids = int(bm.group(1)) if bm else None

        html = _get(href, referer=AIRCRAFTBIDDER_BROWSE)
        time.sleep(CONFIG["fetch_delay"])
        body = BeautifulSoup(html, "html.parser").get_text(" ", strip=True) if html else card_text

        l = _listing_from_text("AircraftBidder", title, body, href)
        l.auction = True
        l.bids = bids
        # current/starting bid for the price column
        bidm = re.search(r"current bid[:\s]*\$?\s?([\d,]+)", body, re.I) \
            or re.search(r"starting bid[:\s]*\$?\s?([\d,]+)", body, re.I) \
            or re.search(r"\$\s?([\d,]{4,})", card_text)
        if bidm:
            l.price = _num(bidm.group(1))
        if re.search(r"reserve (?:price )?not met", body, re.I):
            l.status = "Reserve not met"
        elif re.search(r"\bcompleted\b", card_text, re.I):
            l.status = "Completed"
        elif re.search(r"reserve met|meets reserve", body + card_text, re.I):
            l.status = "Reserve met"
        lm = re.search(r"\b([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+)*,\s*[A-Z]{2})\b", body)
        if lm:
            l.location = lm.group(1).strip()
        out.append(l)
    return out


def parse_generic(source: str, html: str, base_url: str) -> list[Listing]:
    """Best-effort parser for sites we haven't hand-tuned (or that block us)."""
    out: list[Listing] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        if "172" in text and re.search(r"\b(19|20)\d{2}\b", text):
            href = a.get("href", "")
            if href.startswith("/"):
                href = base_url.rstrip("/") + href
            out.append(_listing_from_text(source, text, text, href))
    return out


# =============================================================================
# EMAIL ALERT INGEST  -- read saved-search alert emails for sites that block
# scraping (Trade-A-Plane, Controller). This is the legitimate, never-blocked
# path: you enable the site's own saved-search email alerts, and we parse them.
# =============================================================================
# Map a sender domain -> (source label, base url for relative links).
ALERT_SOURCES = {
    "trade-a-plane.com": ("Trade-A-Plane", "https://www.trade-a-plane.com"),
    "controller.com": ("Controller", "https://www.controller.com"),
    "aso.com": ("ASO", "https://www.aso.com"),
    "barnstormers.com": ("Barnstormers", "https://www.barnstormers.com"),
}


def _email_html(msg) -> str:
    """Best HTML (or text) body out of an email.message.Message."""
    html, text = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            chunk = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, ValueError):
            continue
        if ctype == "text/html":
            html += chunk
        else:
            text += chunk
    return html or text


def _parse_alert_email(source: str, base: str, html: str) -> list[Listing]:
    """Pull Cessna 172 listings out of one saved-search alert email."""
    out: list[Listing] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    year_re = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")
    seen = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if "172" not in text or not year_re.search(text):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = base.rstrip("/") + href
        # find the smallest enclosing cell/row/item that holds just THIS listing,
        # so a neighbouring listing's price doesn't bleed in
        container, node = None, a
        for _ in range(5):
            node = node.parent if node is not None else None
            if node is None:
                break
            if node.name in ("td", "tr", "li", "article", "div"):
                n = sum(1 for x in node.find_all("a")
                        if "172" in x.get_text() and year_re.search(x.get_text()))
                if n <= 1:
                    container = node
                    break
        body = (container or a.parent or a).get_text(" ", strip=True)
        l = _listing_from_text(source, text, body, href)
        lm = re.search(r"\b([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+)*,\s*[A-Z]{2})\b", body)
        if lm:
            l.location = lm.group(1).strip()
        if l.uid in seen:
            continue
        seen.add(l.uid)
        out.append(l)
    return out


def fetch_email_alerts() -> list[Listing]:
    """Read saved-search alert emails over IMAP and parse out listings."""
    cfg = CONFIG["imap"]
    if not (cfg["user"] and cfg["password"]):
        return []
    import imaplib
    import email as email_mod

    out: list[Listing] = []
    try:
        M = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
        M.login(cfg["user"], cfg["password"])
        M.select(cfg["folder"])
        since = (dt.date.today() - dt.timedelta(days=cfg["since_days"])).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split() if data and data[0] else []
        for num in ids[-300:]:  # cap how many messages we scan
            typ, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            msg = email_mod.message_from_bytes(msgdata[0][1])
            frm = (msg.get("From") or "").lower()
            match = next((v for dom, v in ALERT_SOURCES.items() if dom in frm), None)
            if not match:
                continue
            source, base = match
            out.extend(_parse_alert_email(source, base, _email_html(msg)))
        M.logout()
        print(f"    parsed {len(out)} listing(s) from alert emails")
    except Exception as e:  # noqa: BLE001 - never let mail issues kill the run
        print(f"  [warn] email alert ingest failed: {e}")
    return out


# Search URLs to hit each day. US-based marketplaces.
# GlobalAir and Barnstormers parse reliably; Trade-A-Plane / Controller / ASO
# often block automated requests (HTTP 403) — they're kept as targets so they
# light up automatically whenever they're reachable. See README.
SEARCH_TARGETS = [
    ("GlobalAir",
     "https://www.globalair.com/aircraft-for-sale/cessna-172",
     parse_globalair),
    ("Barnstormers",
     "https://www.barnstormers.com/category-17352-Cessna.html",
     parse_barnstormers),
    ("Trade-A-Plane",
     "https://www.trade-a-plane.com/search?category_level1=Single+Engine+Piston"
     "&make=CESSNA&model_group=172+SERIES&s-type=aircraft",
     lambda h: parse_generic("Trade-A-Plane", h, "https://www.trade-a-plane.com")),
    ("Controller",
     "https://www.controller.com/listings/for-sale/cessna/172/aircraft",
     lambda h: parse_generic("Controller", h, "https://www.controller.com")),
    ("ASO",
     "https://www.aso.com/listings/aircraft-for-sale/Cessna/172",
     lambda h: parse_generic("ASO", h, "https://www.aso.com")),
]


# =============================================================================
# FILTER + SCORE
# =============================================================================
def passes_hard_filters(l: Listing) -> bool:
    # drop ads that aren't actually a plane for sale
    if re.search(r"\b(rental|for rent|lease|wanted|seeking|partnership|"
                 r"fractional|time ?build|flying club|hangar for)\b", l.title.lower()):
        return False
    if l.year is not None and l.year < CONFIG["min_year"]:
        return False
    if CONFIG["require_no_damage"] and l.damage_history is True:
        return False
    # keep US listings and unknowns; drop the ones clearly located abroad
    if CONFIG["us_only"] and _is_us(l.location) is False:
        return False
    # price: allow if under ceiling, OR unknown (we'll surface it), OR potential unicorn
    if l.price is not None and l.price > CONFIG["unicorn_price_stretch"]:
        return False
    return True


def score_listing(l: Listing) -> None:
    w = CONFIG["weights"]
    max_score = sum(w.values())  # full marks on every weighted factor
    s = 0.0
    reasons = []

    if l.price is not None:
        # full marks at/under ceiling, sliding down to the stretch limit
        ceil, stretch = CONFIG["price_ceiling"], CONFIG["unicorn_price_stretch"]
        if l.price <= ceil:
            pscore = 1.0
            reasons.append(f"Under ${ceil:,} budget")
        else:
            pscore = max(0.0, 1 - (l.price - ceil) / (stretch - ceil))
        s += w["price"] * pscore

    if l.total_time is not None:
        tscore = max(0.0, 1 - l.total_time / CONFIG["max_total_time"])
        s += w["total_time"] * tscore
        if l.total_time < 3000:
            reasons.append(f"Low airframe time ({l.total_time:,} hrs)")

    if l.engine_smoh is not None:
        escore = max(0.0, 1 - l.engine_smoh / 2000)  # TBO ~2000
        s += w["engine_smoh"] * escore
        if l.engine_smoh < 500:
            reasons.append(f"Fresh engine ({l.engine_smoh} SMOH)")

    if l.ifr_ready:
        s += w["ifr_ready"]
        reasons.append("IFR / modern avionics")

    if l.damage_history is False:
        s += w["no_damage"]
        reasons.append("No damage history")

    if l.year:
        yscore = max(0.0, min(1.0, (l.year - CONFIG["min_year"]) / (2010 - CONFIG["min_year"])))
        s += w["year"] * yscore

    # express the score as a whole-number percentage of the best possible score
    l.score = round(s / max_score * 100) if max_score else 0
    l.reasons = reasons

    # --- unicorn detection ---
    is_clean = l.damage_history is False
    if is_clean and l.price is not None and l.price <= CONFIG["price_ceiling"] \
            and l.year and l.year >= CONFIG["min_year"] \
            and l.total_time is not None and l.total_time < 4000:
        l.unicorn = True
        l.reasons.insert(0, "UNICORN: 1975+, clean, low-time, AND under budget")
    elif is_clean and l.model in ("172R", "172S") \
            and l.total_time is not None and l.total_time < 2000:
        l.unicorn = True
        l.reasons.insert(0, "UNICORN: late-model R/S, very low time, clean")


# =============================================================================
# STATE (so we only alert on new unicorns once)
# =============================================================================
def load_seen() -> set:
    try:
        with open(CONFIG["state_file"]) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set) -> None:
    with open(CONFIG["state_file"], "w") as f:
        json.dump(sorted(seen), f)


def load_history(path: str) -> list:
    try:
        with open(path) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(path: str, history: list) -> None:
    with open(path, "w") as f:
        json.dump(history, f, indent=2)


# =============================================================================
# EMAIL
# =============================================================================
def build_email_html(top: list[Listing], unicorns: list[Listing]) -> str:
    today = dt.date.today().strftime("%A, %B %d, %Y")
    parts = [f"<h2>Cessna 172 Daily Digest &mdash; {today}</h2>"]

    if unicorns:
        parts.append("<h3 style='color:#b8860b'>&#129412; UNICORN ALERT</h3><ul>")
        for u in unicorns:
            parts.append(
                f"<li><b><a href='{u.url}'>{u.title}</a></b> &mdash; "
                f"{'$'+format(u.price,',') if u.price else 'price n/a'} "
                f"&middot; {u.total_time or '?'} TT &middot; {u.source}<br>"
                f"<i>{'; '.join(u.reasons)}</i></li>"
            )
        parts.append("</ul><hr>")

    parts.append(f"<h3>Top {len(top)} matches</h3><ol>")
    for l in top:
        price = f"${l.price:,}" if l.price else "price n/a"
        tt = f"{l.total_time:,} TT" if l.total_time else "TT ?"
        parts.append(
            f"<li><b><a href='{l.url}'>{l.title}</a></b> "
            f"(score {l.score}%)<br>"
            f"{price} &middot; {tt} &middot; {l.model} &middot; {l.source}<br>"
            f"<span style='color:#555'>{'; '.join(l.reasons) or 'partial data'}</span></li>"
        )
    parts.append("</ol>")
    parts.append("<p style='color:#999;font-size:12px'>Auto-generated by plane_finder.py. "
                 "Verify all details (damage, logs, hours) directly with the seller and a pre-buy inspection.</p>")
    return "\n".join(parts)


def build_digest_markdown(top: list[Listing], unicorns: list[Listing]) -> str:
    """Markdown version of the digest, for posting as a GitHub Issue."""
    today = dt.date.today().strftime("%A, %B %d, %Y")
    lines = [f"## Cessna 172 Daily Digest — {today}", ""]

    if unicorns:
        lines.append("### 🦄 UNICORN ALERT")
        for u in unicorns:
            price = f"${u.price:,}" if u.price else "price n/a"
            lines.append(
                f"- **[{u.title}]({u.url})** — {price} · "
                f"{u.total_time or '?'} TT · {u.source}  \n"
                f"  _{'; '.join(u.reasons)}_"
            )
        lines.append("")

    lines.append(f"### Top {len(top)} matches")
    if not top:
        lines.append("_No listings passed the filters today "
                     "(often a site blocked the request — see README)._")
    for i, l in enumerate(top, 1):
        price = f"${l.price:,}" if l.price else "price n/a"
        tt = f"{l.total_time:,} TT" if l.total_time else "TT ?"
        loc = f" · {l.location}" if l.location else ""
        lines.append(
            f"{i}. **[{l.title}]({l.url})** (score {l.score}%)  \n"
            f"   {price} · {tt} · {l.model}{loc} · {l.source}  \n"
            f"   {'; '.join(l.reasons) or 'partial data'}"
        )
    lines.append("")
    lines.append("---")
    lines.append("_Auto-generated by `plane_finder.py`. Verify damage, logs, and "
                 "hours directly with the seller and a pre-buy inspection._")
    return "\n".join(lines)


def build_dashboard_html(top: list[Listing], unicorns: list[Listing],
                         history: list, auctions: list[Listing] | None = None) -> str:
    """Self-contained static dashboard (one HTML file) for GitHub Pages.

    Data is embedded as JSON and rendered client-side so the tables are
    sortable/filterable with no external dependencies. Two tabs: fixed-price
    listings ("For Sale") and AircraftBidder auctions ("Auctions").
    """
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "updated": updated,
        "ceiling": CONFIG["price_ceiling"],
        "min_year": CONFIG["min_year"],
        "top": [asdict(l) for l in top],
        "unicorns": [asdict(u) for u in unicorns],
        "auctions": [asdict(a) for a in (auctions or [])],
        "history": history,
    }
    # Guard against </script> breaking out of the inline script tag.
    data_json = json.dumps(payload).replace("</", "<\\/")

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cessna 172 Finder</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; padding: 0 16px 48px; max-width: 1100px; margin-inline: auto;
         color: #1a1a1a; background: #fafafa; }
  @media (prefers-color-scheme: dark) {
    body { color: #e8e8e8; background: #161616; }
    th { background: #222 !important; }
    tr:hover td { background: #1f1f1f !important; }
    .card { background: #1e1e1e !important; border-color: #333 !important; }
    a { color: #6db3ff; }
  }
  h1 { margin: 24px 0 4px; }
  .sub { color: #888; margin: 0 0 20px; }
  .unicorn { border: 2px solid #b8860b; background: #fff8e6; border-radius: 10px;
             padding: 12px 16px; margin: 0 0 20px; }
  @media (prefers-color-scheme: dark) { .unicorn { background: #2a2410; } }
  .unicorn h2 { margin: 0 0 8px; color: #b8860b; }
  .stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 0 0 20px; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 10px 16px; background: #fff; }
  .card .n { font-size: 26px; font-weight: 700; }
  .card .l { color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  input[type=search] { width: 100%; max-width: 320px; padding: 8px 12px; margin: 0 0 12px;
                       border: 1px solid #ccc; border-radius: 8px; font-size: 14px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e2e2e2; }
  @media (prefers-color-scheme: dark) { th, td { border-color: #303030; } }
  th { background: #f0f0f0; cursor: pointer; user-select: none; position: sticky; top: 0; }
  th[aria-sort] { font-weight: 800; }
  tr:hover td { background: #f3f7ff; }
  .reasons { color: #777; font-size: 12px; }
  .pill { display: inline-block; background: #b8860b; color: #fff; border-radius: 6px;
          padding: 1px 6px; font-size: 11px; margin-left: 6px; }
  .spark { display: flex; align-items: flex-end; gap: 3px; height: 48px; margin-top: 6px; }
  .spark span { width: 8px; background: #6db3ff; border-radius: 2px 2px 0 0; }
  .tabs { display: flex; gap: 6px; margin: 8px 0 16px; border-bottom: 1px solid #ddd; }
  .tab { background: none; border: none; padding: 8px 14px; font: inherit; cursor: pointer;
         color: #888; border-bottom: 2px solid transparent; }
  .tab.active { color: inherit; border-bottom-color: #6db3ff; font-weight: 700; }
  .hidden { display: none; }
  .match { display: inline-block; background: #1a7f37; color: #fff; border-radius: 6px;
           padding: 1px 6px; font-size: 11px; }
  footer { margin-top: 32px; color: #999; font-size: 12px; }
</style>
</head>
<body>
<h1>&#9992;&#65039; Cessna 172 Finder</h1>
<p class="sub">Updated <b id="updated"></b> &middot;
   target: <span id="crit"></span></p>

<div id="unicorns"></div>

<div class="stats">
  <div class="card"><div class="n" id="stat-top">0</div><div class="l">For sale</div></div>
  <div class="card"><div class="n" id="stat-uni">0</div><div class="l">Unicorns</div></div>
  <div class="card"><div class="n" id="stat-auc">0</div><div class="l">Auction matches</div></div>
  <div class="card">
    <div class="l">Matches / day</div>
    <div class="spark" id="spark"></div>
  </div>
</div>

<div class="tabs">
  <button class="tab active" data-tab="sale">For Sale (<span id="n-sale">0</span>)</button>
  <button class="tab" data-tab="auction">Auctions (<span id="n-auction">0</span>)</button>
</div>

<section id="tab-sale">
  <input type="search" id="filter" placeholder="Filter by title, model, source...">
  <table id="tbl">
    <thead><tr>
      <th data-k="rank">#</th>
      <th data-k="score">Score</th>
      <th data-k="title">Listing</th>
      <th data-k="price">Price</th>
      <th data-k="total_time">TT</th>
      <th data-k="model">Model</th>
      <th data-k="year">Year</th>
      <th data-k="location">Location</th>
      <th data-k="source">Source</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</section>

<section id="tab-auction" class="hidden">
  <p class="sub">Live & recent Cessna lots on
    <a href="https://auction.aircraftbidder.com/" target="_blank" rel="noopener">AircraftBidder</a>.
    Bids are legally binding and a buyer's premium is added on top — inspect before bidding.</p>
  <input type="search" id="filterA" placeholder="Filter auctions by title, model, location...">
  <table id="tblA">
    <thead><tr>
      <th data-k="rank">#</th>
      <th data-k="matches">Match</th>
      <th data-k="price">Current bid</th>
      <th data-k="bids">Bids</th>
      <th data-k="title">Lot</th>
      <th data-k="total_time">TT</th>
      <th data-k="model">Model</th>
      <th data-k="year">Year</th>
      <th data-k="location">Location</th>
      <th data-k="status">Status</th>
    </tr></thead>
    <tbody></tbody>
  </table>
</section>

<footer>
  Auto-generated by <code>plane_finder.py</code>. Listing sites sometimes block
  automated requests; an empty table usually means a site returned HTTP 403, not a
  bug. Always verify damage, logs, and hours with the seller and a pre-buy inspection.
</footer>

<script id="data" type="application/json">__DATA__</script>
<script>
  const D = JSON.parse(document.getElementById("data").textContent);
  document.getElementById("updated").textContent = D.updated;
  document.getElementById("crit").textContent =
    D.min_year + "+ · no damage · under $" + D.ceiling.toLocaleString();
  D.auctions = D.auctions || [];
  document.getElementById("stat-top").textContent = D.top.length;
  document.getElementById("stat-uni").textContent = D.unicorns.length;
  document.getElementById("stat-auc").textContent = D.auctions.filter(a => a.matches).length;
  document.getElementById("n-sale").textContent = D.top.length;
  document.getElementById("n-auction").textContent = D.auctions.length;

  function esc(s){ return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c])); }

  // unicorn callouts
  if (D.unicorns.length) {
    const box = document.createElement("div");
    box.className = "unicorn";
    box.innerHTML = "<h2>🦄 Unicorn alert</h2>" + D.unicorns.map(u =>
      `<div><a href="${u.url}" target="_blank" rel="noopener"><b>${esc(u.title)}</b></a> &mdash; `
      + (u.price ? "$" + u.price.toLocaleString() : "price n/a")
      + ` &middot; ${u.total_time ?? "?"} TT &middot; ${esc(u.source)}<br>`
      + `<span class="reasons">${esc((u.reasons||[]).join("; "))}</span></div>`).join("");
    document.getElementById("unicorns").appendChild(box);
  }

  // sparkline of matches/day
  const hist = D.history.slice(-30);
  const hmax = Math.max(1, ...hist.map(h => h.kept || 0));
  document.getElementById("spark").innerHTML = hist.map(h =>
    `<span style="height:${Math.round((h.kept||0)/hmax*100)}%" title="${h.date}: ${h.kept} matches"></span>`).join("");

  // reusable sortable/filterable table controller
  function controller(tblSel, filterSel, data, rowFn) {
    const rows = data.map((l, i) => ({ ...l, rank: i + 1 }));
    const tbl = document.querySelector(tblSel);
    const input = document.querySelector(filterSel);
    const cols = tbl.querySelectorAll("th").length;
    let sortK = "rank", asc = true;
    function draw() {
      const f = input.value.toLowerCase();
      let list = rows.filter(l =>
        (l.title + " " + l.model + " " + l.source + " " + (l.location||"")).toLowerCase().includes(f));
      list.sort((a, b) => {
        const x = a[sortK], y = b[sortK];
        const v = (x == null) - (y == null) ||
          (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y)));
        return asc ? v : -v;
      });
      tbl.querySelector("tbody").innerHTML = list.map(rowFn).join("")
        || `<tr><td colspan="${cols}">Nothing here this run.</td></tr>`;
      tbl.querySelectorAll("th").forEach(th => th.removeAttribute("aria-sort"));
      const th = tbl.querySelector(`th[data-k="${sortK}"]`);
      if (th) th.setAttribute("aria-sort", asc ? "ascending" : "descending");
    }
    tbl.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (sortK === k) asc = !asc; else { sortK = k; asc = (k === "rank"); }
      draw();
    }));
    input.addEventListener("input", draw);
    draw();
  }

  const link = (l) => `<a href="${l.url}" target="_blank" rel="noopener">${esc(l.title)}</a>`;
  const cell = (v) => v || v === 0 ? v.toLocaleString() : "&mdash;";

  controller("#tbl", "#filter", D.top, l => `
    <tr>
      <td>${l.rank}${l.unicorn ? '<span class="pill">UNICORN</span>' : ''}</td>
      <td>${l.score}%</td>
      <td>${link(l)}<div class="reasons">${esc((l.reasons||[]).join("; "))}</div></td>
      <td>${l.price ? "$" + l.price.toLocaleString() : "&mdash;"}</td>
      <td>${cell(l.total_time)}</td>
      <td>${esc(l.model)}</td>
      <td>${l.year ?? "&mdash;"}</td>
      <td>${esc(l.location) || "&mdash;"}</td>
      <td>${esc(l.source)}</td>
    </tr>`);

  controller("#tblA", "#filterA", D.auctions, l => `
    <tr>
      <td>${l.rank}</td>
      <td>${l.matches ? '<span class="match">✓ matches</span>' : '&mdash;'}</td>
      <td>${l.price ? "$" + l.price.toLocaleString() : "no bid yet"}</td>
      <td>${l.bids ?? 0}</td>
      <td>${link(l)}</td>
      <td>${cell(l.total_time)}</td>
      <td>${esc(l.model)}</td>
      <td>${l.year ?? "&mdash;"}</td>
      <td>${esc(l.location) || "&mdash;"}</td>
      <td>${esc(l.status) || "&mdash;"}</td>
    </tr>`);

  // tab switching
  document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    b.classList.add("active");
    document.getElementById("tab-sale").classList.toggle("hidden", b.dataset.tab !== "sale");
    document.getElementById("tab-auction").classList.toggle("hidden", b.dataset.tab !== "auction");
  }));
</script>
</body>
</html>
""".replace("__DATA__", data_json)


def send_email(html: str) -> None:
    cfg = CONFIG["email"]
    if not cfg["enabled"]:
        print("  [info] email disabled; skipping send")
        return
    if not (cfg["username"] and cfg["password"] and cfg["to_addr"]):
        print("  [warn] email credentials missing (set PF_SMTP_USER / PF_SMTP_PASS / PF_TO_ADDR). "
              "Skipping send; digest printed below instead.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Plane Finder] Cessna 172 digest {dt.date.today()}"
    msg["From"] = cfg["username"]
    msg["To"] = cfg["to_addr"]
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
        server.starttls()
        server.login(cfg["username"], cfg["password"])
        server.sendmail(cfg["username"], [cfg["to_addr"]], msg.as_string())
    print(f"  [ok] digest emailed to {cfg['to_addr']}")


# =============================================================================
# MAIN
# =============================================================================
def main(debug: bool = False) -> None:
    print(f"Plane Finder run @ {dt.datetime.now():%Y-%m-%d %H:%M}")
    all_listings: list[Listing] = []

    for name, url, parser in SEARCH_TARGETS:
        print(f"  fetching {name} ...")
        html = _get(url)
        if debug and html:
            with open(f"debug_{name}.html", "w") as f:
                f.write(html)
            print(f"    [debug] saved debug_{name}.html ({len(html)} bytes)")
        listings = parser(html) if html else []
        print(f"    parsed {len(listings)} raw listings")
        all_listings.extend(listings)

    # saved-search alert emails (Trade-A-Plane / Controller / ASO that block scraping)
    if CONFIG["imap"]["user"]:
        print("  reading saved-search alert emails ...")
        all_listings.extend(fetch_email_alerts())

    # dedupe by uid
    uniq = {l.uid: l for l in all_listings}.values()

    # filter + score
    kept = [l for l in uniq if passes_hard_filters(l)]
    for l in kept:
        score_listing(l)
    kept.sort(key=lambda x: x.score, reverse=True)

    top = kept[:CONFIG["top_n"]]
    unicorns = [l for l in kept if l.unicorn]

    # only alert on NEW unicorns
    seen = load_seen()
    new_unicorns = [u for u in unicorns if u.uid not in seen]
    seen.update(u.uid for u in unicorns)
    save_seen(seen)

    print(f"\n  {len(kept)} listings passed filters; "
          f"{len(unicorns)} unicorn(s) ({len(new_unicorns)} new)")

    # --- auctions (AircraftBidder): show ALL Cessna lots, flag the ones that
    #     meet the buy criteria. These are kept separate from the for-sale list.
    print("  fetching AircraftBidder (auctions) ...")
    ab_html = _get(AIRCRAFTBIDDER_BROWSE)
    auctions = parse_aircraftbidder(ab_html) if ab_html else []
    for a in auctions:
        score_listing(a)
        a.matches = passes_hard_filters(a)
    # matching lots first, then by score
    auctions.sort(key=lambda x: (x.matches, x.score), reverse=True)
    print(f"    {len(auctions)} Cessna auction lot(s); "
          f"{sum(a.matches for a in auctions)} match criteria")

    html = build_email_html(top, new_unicorns)
    send_email(html)

    # Optional: write a Markdown digest to a file (used by the GitHub Action to
    # open an issue). Set PF_DIGEST_MD to the target path to enable.
    digest_path = os.environ.get("PF_DIGEST_MD")
    if digest_path:
        with open(digest_path, "w") as f:
            f.write(build_digest_markdown(top, new_unicorns))
        print(f"  [ok] wrote markdown digest to {digest_path}")

    # Optional: maintain a small day-over-day history (for the dashboard trend).
    history_path = os.environ.get("PF_HISTORY")
    history: list = []
    if history_path:
        history = load_history(history_path)
        today = dt.date.today().isoformat()
        entry = {"date": today, "kept": len(kept), "unicorns": len(unicorns)}
        # replace any existing entry for today so repeated runs don't pile up
        history = [h for h in history if h.get("date") != today] + [entry]
        history = history[-60:]
        save_history(history_path, history)

    # Optional: render the static dashboard (for GitHub Pages). Set PF_SITE_DIR.
    site_dir = os.environ.get("PF_SITE_DIR")
    if site_dir:
        os.makedirs(site_dir, exist_ok=True)
        with open(os.path.join(site_dir, "index.html"), "w") as f:
            f.write(build_dashboard_html(top, unicorns, history, auctions))
        # Tell GitHub Pages to serve the artifact as-is (skip Jekyll), so the
        # dashboard is never mistaken for a Jekyll site / README homepage.
        open(os.path.join(site_dir, ".nojekyll"), "w").close()
        print(f"  [ok] wrote dashboard to {os.path.join(site_dir, 'index.html')}")

    # always print a plaintext fallback so a manual run is still useful
    print("\n" + "=" * 60)
    if new_unicorns:
        print("UNICORNS:")
        for u in new_unicorns:
            print(f"  * {u.title} | {u.price} | {u.url}")
    print(f"TOP {len(top)}:")
    for i, l in enumerate(top, 1):
        price = f"${l.price:,}" if l.price else "n/a"
        print(f"  {i:2}. [{l.score:3d}%] {l.title} | {price} | {l.source}")
    print("=" * 60)


if __name__ == "__main__":
    main(debug="--debug" in sys.argv)
