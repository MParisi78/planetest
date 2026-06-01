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

    # --- politeness ---
    "request_timeout": 20,
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
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
def _get(url: str) -> str | None:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": CONFIG["user_agent"]},
            timeout=CONFIG["request_timeout"],
        )
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
    m = re.search(r"[\d,]+", text.replace(".", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_trade_a_plane(html: str) -> list[Listing]:
    """
    Parser for Trade-A-Plane search results.
    NOTE: selectors below are illustrative and WILL need to be matched to the
    live page structure the first time you run this. Run with --debug to dump
    the HTML and adjust the CSS selectors.
    """
    out: list[Listing] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".result, .listing, [class*=listing]")
    for c in cards:
        title_el = c.select_one("a, .title, h2, h3")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        href = title_el.get("href", "")
        if href and href.startswith("/"):
            href = "https://www.trade-a-plane.com" + href
        body = c.get_text(" ", strip=True)

        out.append(_listing_from_text("Trade-A-Plane", title, body, href))
    return out


def parse_generic(source: str, html: str, base_url: str) -> list[Listing]:
    """A best-effort generic parser for sites we haven't hand-tuned."""
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


def _listing_from_text(source: str, title: str, body: str, url: str) -> Listing:
    """Extract structured fields from free-text listing copy."""
    body_l = body.lower()

    year = None
    ym = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", title) or re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", body)
    if ym:
        year = int(ym.group(0))

    price = None
    pm = re.search(r"\$\s?([\d,]{4,})", body)
    if pm:
        price = _num(pm.group(1))

    tt = None
    ttm = re.search(r"([\d,]{2,6})\s*(?:hrs?\s*)?(?:tt|total time|ttaf|airframe)", body_l)
    if ttm:
        tt = _num(ttm.group(1))

    smoh = None
    sm = re.search(r"([\d,]{1,5})\s*(?:hrs?\s*)?(?:smoh|since (?:major )?overhaul|stoh)", body_l)
    if sm:
        smoh = _num(sm.group(1))

    # damage: look for explicit clean vs explicit damage
    damage = None
    if re.search(r"no (?:known )?damage|damage[- ]free|no accident|clean history", body_l):
        damage = False
    elif re.search(r"\bdamage history\b|\bsalvage\b|\bwrecked\b|prop strike|gear up", body_l):
        damage = True

    ifr = bool(re.search(r"\bifr\b|garmin|g5|gtn|gns|ads-?b|glass|g1000", body_l))

    model = "172"
    mm = re.search(r"172\s?([a-z]{1,2})\b", title.lower())
    if mm:
        model = "172" + mm.group(1).upper()

    return Listing(
        source=source, title=title, year=year, model=model, price=price,
        total_time=tt, engine_smoh=smoh, damage_history=damage,
        ifr_ready=ifr, url=url,
    )


# Search URLs to hit each day. Add/adjust freely.
SEARCH_TARGETS = [
    ("Trade-A-Plane",
     "https://www.trade-a-plane.com/search?make=CESSNA&model_group=CESSNA+172+SERIES&s-type=aircraft",
     parse_trade_a_plane),
    ("Controller",
     "https://www.controller.com/listings/for-sale/cessna/172/aircraft",
     lambda h: parse_generic("Controller", h, "https://www.controller.com")),
    ("GlobalAir",
     "https://www.globalair.com/aircraft-for-sale/cessna-172",
     lambda h: parse_generic("GlobalAir", h, "https://www.globalair.com")),
]


# =============================================================================
# FILTER + SCORE
# =============================================================================
def passes_hard_filters(l: Listing) -> bool:
    if l.year is not None and l.year < CONFIG["min_year"]:
        return False
    if CONFIG["require_no_damage"] and l.damage_history is True:
        return False
    # price: allow if under ceiling, OR unknown (we'll surface it), OR potential unicorn
    if l.price is not None and l.price > CONFIG["unicorn_price_stretch"]:
        return False
    return True


def score_listing(l: Listing) -> None:
    w = CONFIG["weights"]
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

    l.score = round(s, 2)
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
            f"(score {l.score})<br>"
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
        lines.append(
            f"{i}. **[{l.title}]({l.url})** (score {l.score})  \n"
            f"   {price} · {tt} · {l.model} · {l.source}  \n"
            f"   {'; '.join(l.reasons) or 'partial data'}"
        )
    lines.append("")
    lines.append("---")
    lines.append("_Auto-generated by `plane_finder.py`. Verify damage, logs, and "
                 "hours directly with the seller and a pre-buy inspection._")
    return "\n".join(lines)


def build_dashboard_html(top: list[Listing], unicorns: list[Listing],
                         history: list) -> str:
    """Self-contained static dashboard (one HTML file) for GitHub Pages.

    Data is embedded as JSON and rendered client-side so the table is
    sortable/filterable with no external dependencies.
    """
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "updated": updated,
        "ceiling": CONFIG["price_ceiling"],
        "min_year": CONFIG["min_year"],
        "top": [asdict(l) for l in top],
        "unicorns": [asdict(u) for u in unicorns],
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
  footer { margin-top: 32px; color: #999; font-size: 12px; }
</style>
</head>
<body>
<h1>&#9992;&#65039; Cessna 172 Finder</h1>
<p class="sub">Updated <b id="updated"></b> &middot;
   target: <span id="crit"></span></p>

<div id="unicorns"></div>

<div class="stats">
  <div class="card"><div class="n" id="stat-top">0</div><div class="l">Top matches</div></div>
  <div class="card"><div class="n" id="stat-uni">0</div><div class="l">Unicorns</div></div>
  <div class="card">
    <div class="l">Matches / day</div>
    <div class="spark" id="spark"></div>
  </div>
</div>

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
    <th data-k="source">Source</th>
  </tr></thead>
  <tbody></tbody>
</table>

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
  document.getElementById("stat-top").textContent = D.top.length;
  document.getElementById("stat-uni").textContent = D.unicorns.length;

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
  const max = Math.max(1, ...hist.map(h => h.kept || 0));
  document.getElementById("spark").innerHTML = hist.map(h =>
    `<span style="height:${Math.round((h.kept||0)/max*100)}%" title="${h.date}: ${h.kept} matches"></span>`).join("");

  // table with embedded rank
  let rows = D.top.map((l, i) => ({ ...l, rank: i + 1 }));
  let sortK = "rank", asc = true;

  function esc(s){ return String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c])); }

  function render(list) {
    document.querySelector("#tbl tbody").innerHTML = list.map(l => `
      <tr>
        <td>${l.rank}${l.unicorn ? '<span class="pill">UNICORN</span>' : ''}</td>
        <td>${l.score}</td>
        <td><a href="${l.url}" target="_blank" rel="noopener">${esc(l.title)}</a>
            <div class="reasons">${esc((l.reasons||[]).join("; "))}</div></td>
        <td>${l.price ? "$" + l.price.toLocaleString() : "&mdash;"}</td>
        <td>${l.total_time ? l.total_time.toLocaleString() : "&mdash;"}</td>
        <td>${esc(l.model)}</td>
        <td>${l.year ?? "&mdash;"}</td>
        <td>${esc(l.source)}</td>
      </tr>`).join("") || '<tr><td colspan="8">No matches this run.</td></tr>';
  }

  function sortAndRender() {
    const f = document.getElementById("filter").value.toLowerCase();
    let list = rows.filter(l =>
      (l.title + " " + l.model + " " + l.source).toLowerCase().includes(f));
    list.sort((a, b) => {
      const x = a[sortK], y = b[sortK];
      const v = (x == null) - (y == null) ||
        (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y)));
      return asc ? v : -v;
    });
    render(list);
    document.querySelectorAll("th").forEach(th =>
      th.removeAttribute("aria-sort"));
    const th = document.querySelector(`th[data-k="${sortK}"]`);
    if (th) th.setAttribute("aria-sort", asc ? "ascending" : "descending");
  }

  document.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.k;
    if (sortK === k) asc = !asc; else { sortK = k; asc = k === "rank"; }
    sortAndRender();
  }));
  document.getElementById("filter").addEventListener("input", sortAndRender);
  sortAndRender();
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
            f.write(build_dashboard_html(top, unicorns, history))
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
        print(f"  {i:2}. [{l.score:5.2f}] {l.title} | {price} | {l.source}")
    print("=" * 60)


if __name__ == "__main__":
    main(debug="--debug" in sys.argv)
