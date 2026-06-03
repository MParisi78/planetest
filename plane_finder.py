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

# Craigslist serves RSS; we parse it with html.parser, so silence bs4's nag.
try:
    import warnings
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

# =============================================================================
# CONFIG  -- everything you'd want to tweak lives here
# =============================================================================
CONFIG = {
    # --- search criteria ---
    # Year floor widened to 1956 (the 172's first year) so vintage classics like
    # straight-tails show up. Override with PF_MIN_YEAR. Desirable model-years are
    # flagged + score-boosted via _highlight(), independent of this floor.
    "min_year": int(os.environ.get("PF_MIN_YEAR", "1956")),
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
        "highlight": 2.0,      # a collectible/desirable 172 variant or engine-era
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
        # `or` (not the get-default) so an empty secret still falls back to Gmail;
        # strip whitespace/scheme a user might paste in by mistake.
        "host": (os.environ.get("PF_IMAP_HOST") or "imap.gmail.com").strip()
                 .replace("https://", "").replace("http://", "").strip("/"),
        "port": int(os.environ.get("PF_IMAP_PORT") or "993"),
        "user": os.environ.get("PF_IMAP_USER", "").strip(),
        "password": os.environ.get("PF_IMAP_PASS", "").strip(),  # app password, not your login
        "folder": (os.environ.get("PF_IMAP_FOLDER") or "INBOX").strip(),
        "since_days": int(os.environ.get("PF_IMAP_SINCE_DAYS") or "7"),
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
    make: str = ""
    model: str = ""
    seats: int | None = None
    price: int | None = None
    total_time: int | None = None      # airframe hours
    engine_smoh: int | None = None      # hours since major overhaul
    damage_history: bool | None = None  # True = has damage, False = clean, None = unknown
    ifr_ready: bool = False
    url: str = ""
    location: str = ""
    score: float = 0.0
    unicorn: bool = False
    highlight: str = ""        # desirable variant/year badge, e.g. "Straight-tail classic"
    from_alert: bool = False   # came from a saved-search alert email (shown unfiltered)
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
    """Engine hours since major overhaul: '635 hours SMOH' or 'SMOH877'."""
    low = (text or "").lower()
    # label-first form first ('SMOH877'), so a preceding TT number isn't grabbed
    m = re.search(r"(?:smoh|stoh)\s*([\d,]{1,5})", low)
    if not m:
        m = re.search(
            r"([\d,]{1,5})\s*(?:hrs?\.?\s*)?(?:hours?\s*)?"
            r"(?:since\s+(?:major\s+)?overhaul|smoh|stoh)", low)
    return _num(m.group(1)) if m else None


def _extract_tt(text: str) -> int | None:
    """Airframe total time: '3,150 TT', '7,770 Hours Total Time', or 'TTAF3250'."""
    low = (text or "").lower()
    m = re.search(
        r"([\d,]{2,6})\s*(?:hrs?|hours?)?\s*"
        r"(?:total time|tt(?:sn|af)?|ttaf|airframe|time since new)", low)
    if not m:  # label-first form, e.g. 'TTAF3250'
        m = re.search(r"(?:ttaf|ttsn|tt)\s*([\d,]{2,6})", low)
    return _num(m.group(1)) if m else None


# --- make / model / seats -----------------------------------------------------
_MAKES = [
    "cessna", "beechcraft", "beech", "piper", "mooney", "cirrus", "diamond",
    "grumman", "bellanca", "american champion", "champion", "aeronca", "maule",
    "aviat", "commander", "navion", "stinson", "luscombe", "taylorcraft",
    "ercoupe", "globe", "lake", "socata", "tecnam", "vans", "van's", "lancair",
    "glasair", "rockwell", "north american", "ryan", "waco",
]
# rough seat counts for common GA models (used when the listing doesn't say)
_SEATS_BY_MODEL = {
    "150": 2, "152": 2, "120": 2, "140": 2, "j3": 2, "j-3": 2, "cub": 2,
    "162": 2, "ercoupe": 2, "luscombe": 2,
    "170": 4, "172": 4, "175": 4, "177": 4, "180": 4, "182": 4, "p210": 6,
    "190": 5, "195": 5, "skylane": 4, "skyhawk": 4, "cardinal": 4,
    "cherokee": 4, "archer": 4, "arrow": 4, "warrior": 4, "dakota": 4,
    "comanche": 4, "bonanza": 4, "debonair": 4, "musketeer": 4, "sundowner": 4,
    "sr20": 4, "sr22": 4, "da40": 4, "m20": 4, "mooney": 4,
    "206": 6, "207": 6, "210": 6, "saratoga": 6, "lance": 6, "206h": 6,
    "baron": 6, "a55": 6, "b55": 6, "55": 6, "58": 6, "310": 6, "da42": 4,
}


def _make_model(title: str, default_make: str = "") -> tuple[str, str]:
    """Pull (make, model) from a listing title like 'BEECHCRAFT A55 BARON'."""
    low = title.lower()
    for mk in _MAKES:
        if mk in low:
            make = mk.title()
            after = title[low.index(mk) + len(mk):].strip()
            # stop the model at a registration (N1234), a 4-digit year, or big gap
            model = re.split(r"\s{2,}|\bN\d{2,}|\b(?:19|20)\d{2}\b", after)[0]
            model = " ".join(model.strip(" -|,").split()[:3])
            return make, model
    # no known make: fall back to a numeric designation if present
    mm = re.search(r"\b([12][0-9]{2}[A-Za-z]?)\b", title)
    return default_make, (mm.group(1) if mm else "")


def _seats(text: str, model: str = "") -> int | None:
    """Seat count: explicit '5 seat'/'4 place' if stated, else infer from model."""
    m = re.search(r"\b(\d)\s*(?:-|\s)?(?:seat|seats|place|passenger)\b", (text or "").lower())
    if m:
        return int(m.group(1))
    ml = (model or "").lower()
    for key, n in _SEATS_BY_MODEL.items():
        if key in ml:
            return n
    return None


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

    make, model = _make_model(title)
    if not model:
        model = _model_variant(title)
    return Listing(
        source=source, title=title, year=year, make=make, model=model,
        seats=_seats(body, model),
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
    make = str(ga("aircraftManufacturer") or "Cessna").title()
    model = _model_variant(name, desig if desig.startswith("172") else desig)

    return Listing(
        source="GlobalAir", title=name, year=year, make=make, model=model,
        seats=_seats(f"{name} {history}", model),
        price=price, total_time=_num(str(ga("totalTime"))),
        engine_smoh=_extract_smoh(str(ga("engineDetails")) + " " + str(ga("propellerDetails"))),
        damage_history=_detect_damage(history),
        ifr_ready=_detect_ifr(avionics),
        url=url, location=str(ga("aircraftLocation")),
    )


def parse_globalair(list_html: str,
                    base: str = "https://www.globalair.com/aircraft-for-sale/cessna-172"
                    ) -> list[Listing]:
    out: list[Listing] = []
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
        # the page is the Cessna category, so accept any model (year-stamped title)
        if re.search(r"/classified-\d+", href) and re.search(r"\b(19|20)\d{2}\b", text):
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
    seen = set()
    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        low = text.lower()
        # any make + a year in the link text = looks like a real listing
        if (len(text) >= 8 and re.search(r"\b(19|20)\d{2}\b", text)
                and any(mk in low for mk in _MAKES)):
            href = a.get("href", "")
            if href.startswith("/"):
                href = base_url.rstrip("/") + href
            if (text, href) in seen:
                continue
            seen.add((text, href))
            out.append(_listing_from_text(source, text, text, href))
    return out


# --- eBay Motors aircraft search (server-rendered results) --------------------
def parse_ebay(html: str) -> list[Listing]:
    out: list[Listing] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    for li in soup.select("li.s-item, li.s-card, div.s-item"):
        t = li.select_one(".s-item__title, .s-card__title, [role=heading]")
        a = li.select_one("a.s-item__link, a.s-card__link, a[href]")
        p = li.select_one(".s-item__price, .s-card__price")
        if not t or not a:
            continue
        title = re.sub(r"^\s*new listing\s*", "", t.get_text(" ", strip=True), flags=re.I).strip()
        low = title.lower()
        if not title or low.startswith("shop on ebay") or not any(mk in low for mk in _MAKES):
            continue
        body = f"{title} {p.get_text(' ', strip=True) if p else ''}"
        out.append(_listing_from_text("eBay", title, body, a.get("href", "")))
    return out


# --- Craigslist regional RSS feeds (no auth, sparse but free) -----------------
def parse_craigslist(rss: str) -> list[Listing]:
    out: list[Listing] = []
    if not rss:
        return out
    soup = BeautifulSoup(rss, "html.parser")   # html.parser lowercases the tags
    for item in soup.find_all("item"):
        te = item.find("title")
        title = te.get_text(strip=True) if te else ""
        de = item.find("description")
        desc = de.get_text(" ", strip=True) if de else ""
        low = (title + " " + desc).lower()
        if not title or not any(mk in low for mk in _MAKES):
            continue
        href = item.get("rdf:about", "")
        out.append(_listing_from_text("Craigslist", title, f"{title} {desc}", href))
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


def _is_title_line(line: str) -> bool:
    """A listing title line in an alert email, e.g. 'CESSNA 195B' / 'BEECHCRAFT A55 BARON'."""
    s = line.strip()
    if not (3 <= len(s) <= 60) or s != s.upper():
        return False
    low = s.lower()
    return any(low.startswith(mk) or f" {mk} " in f" {low} " for mk in _MAKES)


def _parse_alert_email(source: str, base: str, html: str) -> list[Listing]:
    """Pull ALL aircraft listings out of one saved-search alert email.

    Trade-A-Plane alerts format each listing as an ALL-CAPS make/model title
    line followed by a free-text description (and sometimes a link), so we parse
    by blocks rather than by anchor tags. Make/model/seats are extracted too.
    """
    out: list[Listing] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    # collect anchors so we can attach a detail URL to each block
    links = [(a.get_text(" ", strip=True), a["href"])
             for a in soup.find_all("a", href=True)]
    text = soup.get_text("\n")

    lines = [ln.strip() for ln in text.splitlines()]
    # focus on the listings region, between the header and the footer
    try:
        start = next(i for i, ln in enumerate(lines)
                     if re.search(r"new .*listings", ln, re.I))
    except StopIteration:
        start = 0
    end = len(lines)
    for i, ln in enumerate(lines[start + 1:], start + 1):
        if re.search(r"to view all|not getting this|manage your email|copyright", ln, re.I):
            end = i
            break

    # split into (title, body-lines) blocks
    blocks, cur = [], None
    for ln in lines[start + 1:end]:
        if _is_title_line(ln):
            cur = {"title": ln, "body": []}
            blocks.append(cur)
        elif cur is not None and ln:
            cur["body"].append(ln)

    seen = set()
    for b in blocks:
        title = b["title"].strip()
        body = " ".join(b["body"])
        full = f"{title} {body}"
        # detail URL: a link in the body, or an anchor whose text sits in the block
        urlm = re.search(r"https?://\S+", body)
        url = urlm.group(0).rstrip(".,)") if urlm else ""
        if not url:
            for ltext, href in links:
                if ltext and ltext in full:
                    url = href
                    break
        l = _listing_from_text(source, title.title(), full, url)
        l.make, l.model = _make_model(title)
        l.seats = _seats(full, l.model)
        l.from_alert = True
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


# GlobalAir model search pages to walk (proven /aircraft-for-sale/{make}-{model}
# pattern). Broadened beyond the 172 to pull a real pool. Extend via PF_GA_MODELS.
GLOBALAIR_MODELS = (os.environ.get("PF_GA_MODELS") or
                    "cessna-172,cessna-182,cessna-150,cessna-206,"
                    "piper-cherokee,beechcraft-bonanza").split(",")

# Craigslist regions to sweep (best-effort RSS). Extend via PF_CL_REGIONS.
CRAIGSLIST_REGIONS = (os.environ.get("PF_CL_REGIONS") or
                      "sfbay,losangeles,newyork,chicago,dallas,atlanta,"
                      "denver,seattle,phoenix,miami").split(",")
CRAIGSLIST_QUERY = os.environ.get("PF_CL_QUERY", "cessna")


# Search URLs to hit each day. US-based marketplaces.
# GlobalAir and Barnstormers parse reliably; Trade-A-Plane / Controller / ASO /
# eBay / GovPlanet often block automated requests (HTTP 403) — they're kept as
# targets so they light up automatically whenever they're reachable. See README.
SEARCH_TARGETS = [
    ("GlobalAir",
     f"https://www.globalair.com/aircraft-for-sale/{slug.strip()}",
     (lambda h, b=f"https://www.globalair.com/aircraft-for-sale/{slug.strip()}":
      parse_globalair(h, b)))
    for slug in GLOBALAIR_MODELS if slug.strip()
] + [
    ("Barnstormers",
     "https://www.barnstormers.com/category-17352-Cessna.html",
     parse_barnstormers),
    # Broadened to all single-engine piston (was make=CESSNA&model_group=172).
    ("Trade-A-Plane",
     "https://www.trade-a-plane.com/search?category_level1=Single+Engine+Piston"
     "&s-type=aircraft",
     lambda h: parse_generic("Trade-A-Plane", h, "https://www.trade-a-plane.com")),
    ("Controller",
     "https://www.controller.com/listings/for-sale/aircraft/single-engine-piston",
     lambda h: parse_generic("Controller", h, "https://www.controller.com")),
    ("ASO",
     "https://www.aso.com/listings/aircraft-for-sale/Single-Engine-Piston",
     lambda h: parse_generic("ASO", h, "https://www.aso.com")),
    # eBay Motors: airplanes category (26429), keyword-broad.
    ("eBay",
     "https://www.ebay.com/sch/i.html?_sacat=26429&_nkw=cessna&_ipg=120",
     parse_ebay),
    # Government surplus aircraft lots (best-effort; often JS/blocked).
    ("GovPlanet",
     "https://www.govplanet.com/c/aircraft",
     lambda h: parse_generic("GovPlanet", h, "https://www.govplanet.com")),
] + [
    ("Craigslist",
     f"https://{r.strip()}.craigslist.org/search/sss?format=rss&query={CRAIGSLIST_QUERY}",
     parse_craigslist)
    for r in CRAIGSLIST_REGIONS if r.strip()
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


def _highlight(l: Listing) -> tuple[str, str] | None:
    """Flag collectible/desirable Cessna 172 variants & engine eras.

    Returns (badge, why) or None. Encodes the type community's lore; tweak freely.
    """
    md = (l.model or "").upper()
    yr = l.year
    title = (l.title or "").lower()
    is172 = "172" in md or "skyhawk" in title

    if "XP" in md or "R172K" in md:
        return ("Hawk XP", "195hp fuel-injected Continental + constant-speed prop — the hot-rod 172")
    if "RG" in md or "CUTLASS" in md:
        return ("172RG Cutlass", "Retractable gear, 180hp — notably faster cruiser")
    if md in ("172R", "172S") or (is172 and yr and yr >= 1996):
        return ("Restart R/S", "Fuel-injected IO-360, modern airframe — often glass-panel")
    if not is172:
        return None
    if yr and 1956 <= yr <= 1959:
        return ("Straight-tail classic", f"{yr} straight-tail 172 — collectible; smooth Continental O-300 six")
    if yr and 1968 <= yr <= 1976:
        return ("Pre-H2AD O-320", "'68–'76 Lycoming O-320-E2D — the bulletproof pre-H2AD engine years")
    if md == "172P" or (yr and 1981 <= yr <= 1986):
        return ("172P era", "160hp O-320-D2J, 28-gal fuel, higher useful load — refined last of the line")
    return None


def _caution(l: Listing) -> str | None:
    """A buyer's heads-up for the one 172 era worth scrutinizing."""
    md = (l.model or "").upper()
    yr = l.year
    title = (l.title or "").lower()
    is172 = "172" in md or "skyhawk" in title
    is_xp = "XP" in md or "R172K" in md or "hawk xp" in title
    if is172 and not is_xp and yr and 1977 <= yr <= 1980:
        return "1977–80 172N: original O-320-H2AD had cam/lifter issues — confirm the SB fix or an engine upgrade"
    return None


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

    hl = _highlight(l)
    if hl:
        l.highlight = hl[0]
        s += w["highlight"]
        reasons.insert(0, f"★ {hl[0]}: {hl[1]}")
    caution = _caution(l)
    if caution:
        reasons.append(f"⚠ {caution}")

    # express the score as a whole-number percentage of the best possible score
    l.score = round(s / max_score * 100) if max_score else 0
    l.reasons = reasons

    # --- unicorn detection ---
    is_clean = l.damage_history is False
    if is_clean and l.price is not None and l.price <= CONFIG["price_ceiling"] \
            and l.year and l.year >= CONFIG["min_year"] \
            and l.total_time is not None and l.total_time < 4000:
        l.unicorn = True
        l.reasons.insert(0, "UNICORN: clean, low-time, AND under budget")
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
<title>Plane Finder</title>
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
  input[type=search] { width: 100%; max-width: 320px; padding: 8px 12px; margin: 0 8px 12px 0;
                       border: 1px solid #ccc; border-radius: 8px; font-size: 14px; }
  select { padding: 8px 10px; margin: 0 8px 12px 0; border: 1px solid #ccc;
           border-radius: 8px; font-size: 14px; background: #fff; }
  @media (prefers-color-scheme: dark) { select { background: #1e1e1e; color: #e8e8e8; border-color: #444; } }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e2e2e2; }
  @media (prefers-color-scheme: dark) { th, td { border-color: #303030; } }
  th { background: #f0f0f0; cursor: pointer; user-select: none; position: sticky; top: 0; }
  th[aria-sort] { font-weight: 800; }
  tr:hover td { background: #f3f7ff; }
  .reasons { color: #777; font-size: 12px; }
  .pill { display: inline-block; background: #b8860b; color: #fff; border-radius: 6px;
          padding: 1px 6px; font-size: 11px; margin-left: 6px; }
  .star { display: inline-block; background: #6f42c1; color: #fff; border-radius: 6px;
          padding: 1px 6px; font-size: 11px; margin-left: 6px; }
  label.chk { font-size: 14px; color: #555; margin-left: 4px; cursor: pointer; user-select: none; }
  @media (prefers-color-scheme: dark) { label.chk { color: #bbb; } }
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
<h1>&#9992;&#65039; Plane Finder</h1>
<p class="sub">Updated <b id="updated"></b> &middot;
   <span id="crit"></span></p>

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
  <input type="search" id="filter" placeholder="Filter by make, model, location...">
  <select id="fMake"><option value="">All makes</option></select>
  <select id="fModel"><option value="">All models</option></select>
  <select id="fSeats"><option value="">Any seats</option></select>
  <input type="checkbox" id="fStar"><label class="chk" for="fStar">★ Highlights only</label>
  <table id="tbl">
    <thead><tr>
      <th data-k="rank">#</th>
      <th data-k="score">Score</th>
      <th data-k="title">Listing</th>
      <th data-k="price">Price</th>
      <th data-k="total_time">TT</th>
      <th data-k="make">Make</th>
      <th data-k="model">Model</th>
      <th data-k="seats">Seats</th>
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
  <input type="search" id="filterA" placeholder="Filter auctions by make, model, location...">
  <select id="fMakeA"><option value="">All makes</option></select>
  <select id="fModelA"><option value="">All models</option></select>
  <select id="fSeatsA"><option value="">Any seats</option></select>
  <input type="checkbox" id="fStarA"><label class="chk" for="fStarA">★ Highlights only</label>
  <table id="tblA">
    <thead><tr>
      <th data-k="rank">#</th>
      <th data-k="matches">Match</th>
      <th data-k="price">Current bid</th>
      <th data-k="bids">Bids</th>
      <th data-k="title">Lot</th>
      <th data-k="total_time">TT</th>
      <th data-k="make">Make</th>
      <th data-k="model">Model</th>
      <th data-k="seats">Seats</th>
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
    "scraped: " + D.min_year + "+, no damage, US, under $" + D.ceiling.toLocaleString()
    + " · alerts: all aircraft (filter below)";
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
  function controller(tblSel, filterSel, data, rowFn, opts) {
    opts = opts || {};
    const rows = data.map((l, i) => ({ ...l, rank: i + 1 }));
    const tbl = document.querySelector(tblSel);
    const input = document.querySelector(filterSel);
    const cols = tbl.querySelectorAll("th").length;
    let sortK = "rank", asc = true;
    function draw() {
      const f = input.value.toLowerCase();
      let list = rows.filter(l =>
        (l.title + " " + (l.make||"") + " " + l.model + " " + (l.highlight||"") + " " + l.source + " " + (l.location||""))
          .toLowerCase().includes(f) && (!opts.extra || opts.extra(l)));
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
    (opts.controls || []).forEach(sel => {
      const el = document.querySelector(sel);
      if (el) el.addEventListener("change", draw);
    });
    draw();
  }

  const link = (l) => `<a href="${l.url || '#'}" target="_blank" rel="noopener">${esc(l.title)}</a>`;
  const cell = (v) => v || v === 0 ? v.toLocaleString() : "&mdash;";

  // build a tab's make / seats dropdowns from the makes & seat counts present
  const addOpt = (sel, val, label) => {
    const o = document.createElement("option");
    o.value = val; o.textContent = label; sel.appendChild(o);
  };
  function fillFilters(data, makeId, modelId, seatsId) {
    const mk = document.getElementById(makeId), md = document.getElementById(modelId),
          st = document.getElementById(seatsId);
    [...new Set(data.map(l => l.make).filter(Boolean))].sort()
      .forEach(m => addOpt(mk, m, m));
    [...new Set(data.map(l => l.model).filter(Boolean))].sort()
      .forEach(m => addOpt(md, m, m));
    [...new Set(data.map(l => l.seats).filter(s => s != null))].sort((a, b) => a - b)
      .forEach(s => addOpt(st, s, s + "+ seats"));
  }
  fillFilters(D.top, "fMake", "fModel", "fSeats");
  fillFilters(D.auctions, "fMakeA", "fModelA", "fSeatsA");

  controller("#tbl", "#filter", D.top, l => `
    <tr>
      <td>${l.rank}${l.unicorn ? '<span class="pill">UNICORN</span>' : ''}${l.highlight ? '<span class="star">★ '+esc(l.highlight)+'</span>' : ''}</td>
      <td>${l.score}%</td>
      <td>${link(l)}<div class="reasons">${esc((l.reasons||[]).join("; "))}</div></td>
      <td>${l.price ? "$" + l.price.toLocaleString() : "&mdash;"}</td>
      <td>${cell(l.total_time)}</td>
      <td>${esc(l.make) || "&mdash;"}</td>
      <td>${esc(l.model)}</td>
      <td>${l.seats ?? "&mdash;"}</td>
      <td>${l.year ?? "&mdash;"}</td>
      <td>${esc(l.location) || "&mdash;"}</td>
      <td>${esc(l.source)}</td>
    </tr>`, {
      controls: ["#fMake", "#fModel", "#fSeats", "#fStar"],
      extra: l => {
        const mk = document.getElementById("fMake").value,
              md = document.getElementById("fModel").value,
              st = document.getElementById("fSeats").value;
        if (mk && (l.make || "") !== mk) return false;
        if (md && (l.model || "") !== md) return false;
        if (st && !(l.seats != null && l.seats >= +st)) return false;
        if (document.getElementById("fStar").checked && !l.highlight) return false;
        return true;
      }
    });

  controller("#tblA", "#filterA", D.auctions, l => `
    <tr>
      <td>${l.rank}</td>
      <td>${l.matches ? '<span class="match">✓ matches</span>' : '&mdash;'}${l.highlight ? '<span class="star">★ '+esc(l.highlight)+'</span>' : ''}</td>
      <td>${l.price ? "$" + l.price.toLocaleString() : "no bid yet"}</td>
      <td>${l.bids ?? 0}</td>
      <td>${link(l)}</td>
      <td>${cell(l.total_time)}</td>
      <td>${esc(l.make) || "&mdash;"}</td>
      <td>${esc(l.model)}</td>
      <td>${l.seats ?? "&mdash;"}</td>
      <td>${l.year ?? "&mdash;"}</td>
      <td>${esc(l.location) || "&mdash;"}</td>
      <td>${esc(l.status) || "&mdash;"}</td>
    </tr>`, {
      controls: ["#fMakeA", "#fModelA", "#fSeatsA", "#fStarA"],
      extra: l => {
        const mk = document.getElementById("fMakeA").value,
              md = document.getElementById("fModelA").value,
              st = document.getElementById("fSeatsA").value;
        if (document.getElementById("fStarA").checked && !l.highlight) return false;
        if (mk && (l.make || "") !== mk) return false;
        if (md && (l.model || "") !== md) return false;
        if (st && !(l.seats != null && l.seats >= +st)) return false;
        return true;
      }
    });

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

    # filter + score. Scraped sources are held to the buy criteria; alert-email
    # listings are consumed in full (you narrow them with the dashboard filters).
    kept = [l for l in uniq if l.from_alert or passes_hard_filters(l)]
    for l in kept:
        score_listing(l)
    kept.sort(key=lambda x: x.score, reverse=True)

    top = kept[:CONFIG["top_n"]]          # capped list for the email/issue digest
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
            f.write(build_dashboard_html(kept, unicorns, history, auctions))
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


def test_imap() -> None:
    """Dry-run the IMAP alert-email connection and report what it would ingest.

    Run with:  python plane_finder.py --test-imap
    Prints connection status and how many alert listings parse — never prints
    credentials, never touches the dashboard or the daily state.
    """
    from collections import Counter
    cfg = CONFIG["imap"]
    if not (cfg["user"] and cfg["password"]):
        print("IMAP not configured: set PF_IMAP_USER and PF_IMAP_PASS "
              "(see README → 'Getting Trade-A-Plane & Controller in').")
        return
    import imaplib
    import email as email_mod

    print(f"Connecting to {cfg['host']}:{cfg['port']} as {cfg['user']} ...")
    try:
        M = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    except OSError as e:
        print(f"  [fail] could not reach host '{cfg['host']}': {e}")
        print("  Fix PF_IMAP_HOST — for Gmail it must be exactly 'imap.gmail.com' "
              "(no spaces, no https://). If you set it wrong, delete the secret: "
              "an unset host now defaults to Gmail.")
        return
    try:
        M.login(cfg["user"], cfg["password"])
    except imaplib.IMAP4.error as e:
        print(f"  [fail] login rejected: {e}")
        print("  PF_IMAP_USER must be the full address and PF_IMAP_PASS a 16-char "
              "Gmail App Password (2-Step Verification on) — not your normal login.")
        return
    except OSError as e:  # noqa: BLE001
        print(f"  [fail] connection error during login: {e}")
        return
    print("  [ok] login succeeded")
    M.select(cfg["folder"])
    since = (dt.date.today() - dt.timedelta(days=cfg["since_days"])).strftime("%d-%b-%Y")
    typ, data = M.search(None, f"(SINCE {since})")
    ids = data[0].split() if data and data[0] else []
    print(f"  {len(ids)} message(s) in '{cfg['folder']}' since {since}")

    by_source: Counter = Counter()
    for num in ids[-300:]:
        typ, msgdata = M.fetch(num, "(RFC822)")
        if not msgdata or not msgdata[0]:
            continue
        msg = email_mod.message_from_bytes(msgdata[0][1])
        frm = (msg.get("From") or "").lower()
        match = next((v for dom, v in ALERT_SOURCES.items() if dom in frm), None)
        if not match:
            continue
        source, base = match
        by_source[source] += len(_parse_alert_email(source, base, _email_html(msg)))
    M.logout()

    total = sum(by_source.values())
    print(f"  parsed {total} listing(s) from alert emails:")
    for s, n in by_source.most_common():
        print(f"    {s}: {n}")
    if total == 0:
        print("  (Nothing parsed yet. Make sure saved-search email alerts are ON and "
              "have arrived in this mailbox.\n"
              "   If alerts ARE there but parse as 0, send me one and I'll tune the parser.)")


if __name__ == "__main__":
    if "--test-imap" in sys.argv:
        test_imap()
    else:
        main(debug="--debug" in sys.argv)
