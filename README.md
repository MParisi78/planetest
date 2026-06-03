# Plaintest — Daily Cessna 172 Finder

Automatically searches aircraft listing sites once a day, ranks the best Cessna 172
matches against my criteria, flags any "unicorn," and publishes a web dashboard to
GitHub Pages (plus a GitHub Issue alert for new unicorns).
Runs free on GitHub Actions — no computer needs to be on.

## What it looks for
- Aircraft **1956 or newer** (the 172's first year — so vintage straight-tails show up too; override with `PF_MIN_YEAR`)
- **US-based** sellers (listings located abroad are dropped)
- **No reported damage history**
- **Low total time** preferred
- **Under $75,000** — unless it's a unicorn worth stretching for (hard ceiling $140k)

### ★ Desirable model-years (auto-flagged)
The finder tags collectible / sought-after Cessna 172 variants with a purple **★ badge**,
gives them a score bump, and you can show only them with the **★ Highlights only**
toggle on either tab. Current rules (`_highlight()` in `plane_finder.py` — tweak freely):

| Badge | What it catches | Why it's prized |
|-------|-----------------|-----------------|
| **Straight-tail classic** | 1956–1959 172 or 182 | Collectible vertical-tail original; smooth Continental six |
| **Pre-H2AD O-320** | 172, 1968–1976 (incl. the '74 172M) | Bulletproof Lycoming O-320-E2D engine years |
| **172P era** | 172, 1981–1986 / 172P | 160hp, 28-gal fuel, higher useful load — refined last of the line |
| **Hawk XP** | R172K | 195hp fuel-injected Continental + constant-speed prop |
| **172RG Cutlass** | 172RG | Retractable gear, 180hp — faster cruiser |
| **Restart R/S** | 172R / 172S (1996+) | Fuel-injected IO-360, modern airframe, often glass |
| **182P sweet spot** | 182, 1972–1976 / 182P | Well-equipped, strong useful load — the popular Skylane years |
| **182R era** | 182, 1981–1986 / 182R | Most refined carbureted O-470, big useful load |
| **Skylane RG** | R182 / TR182 | Retractable (often turbo) — markedly faster |
| **Restart S/T** | 182S / 182T (1997+) | Fuel-injected IO-540, modern airframe, often G1000 glass |

It also adds a **⚠ caution** note to **1977–1980 172N** listings — the original
O-320-**H2AD** had camshaft/lifter issues; verify the service-bulletin fix or an engine upgrade.

A *unicorn* = either a clean, low-time 172 that somehow lists at/under $75k,
or a late-model 172R/172S with very low time and no damage (the "won't outgrow it" plane).

Each match gets a **score from 0–100%** (a whole-number percentage of the best
possible score across price, total time, engine SMOH, avionics, damage, and year).
All criteria, price ceiling, and scoring weights live in the `CONFIG` block at the
top of `plane_finder.py` — edit anytime.

## Sources it checks
US-based aircraft marketplaces, in priority order:

| Source | Status | Notes |
|--------|--------|-------|
| **GlobalAir** | ✅ reliable | Fixed-price. Reads structured schema.org data — exact price, total time, engine SMOH, location, avionics. Walks several model search pages (`PF_GA_MODELS`), not just the 172. |
| **Barnstormers** | ✅ reliable | Fixed-price. Parses each classified's ad text for price, specs, and location (all Cessna models). |
| **AircraftBidder** | ✅ reliable | **Auctions.** Cessna lots with current/starting bid, bid count, time/status, and specs. Shown on the dashboard's **Auctions** tab. |
| Trade-A-Plane | ⚠️ often blocked | All single-engine piston. Frequently returns HTTP 403; parses automatically whenever reachable. |
| Controller | ⚠️ often blocked | JavaScript-rendered / bot-walled. |
| Aircraft Shopper Online (ASO) | ⚠️ often blocked | Returns HTTP 403 to automated requests. |
| eBay (Motors → Airplanes) | ⚠️ often blocked | Server-rendered search; commonly rate-limits/403s automated requests. |
| Craigslist | ⚠️ best-effort | Per-region RSS sweep (`PF_CL_REGIONS`, `PF_CL_QUERY`). Sparse and noisy; no auth. |
| GovPlanet / gov surplus | ⚠️ best-effort | Government surplus aircraft lots; often JS-rendered. |

> **Facebook Marketplace is intentionally not included.** It requires a
> logged-in session and renders listings with JavaScript, blocks bots
> aggressively, and scraping it violates Facebook's Terms of Service — none of
> which this scraper (plain HTTP + HTML parsing on a CI runner) can or should do.
> The reliable way to pull in other makes/models is the broadened scrapers above
> plus your saved-search **email alerts**, which are never blocked.

The dashboard has two tabs: **For Sale** (fixed-price listings, ranked) and
**Auctions** (every Cessna lot on AircraftBidder, with the ones meeting your
buy criteria flagged **✓ matches**). Auction bids are legally binding and a
buyer's premium is added on top — always inspect before bidding.

Tuning knobs (env vars, also in `CONFIG`): `PF_MAX_DETAILS`, `PF_MAX_PAGES`,
`PF_FETCH_DELAY`. The script fetches each listing's detail page (where price and
specs live), so it crawls politely with a delay between requests.

### Getting Trade-A-Plane & Controller in (without scraping)
Those sites sit behind an anti-bot wall (CloudFront JS challenge) that returns
HTTP 403 to *any* automated request — there's no header trick, and bypassing it
would violate their Terms of Service. The legitimate path is their **own
saved-search email alerts**, which are server-side and never blocked:

1. On Trade-A-Plane (and Controller), save your Cessna 172 search and turn on
   **email alerts** to an inbox you control.
2. Give the finder read-only IMAP access to that inbox via three secrets
   (use an **app password**, not your login):

   | Secret | Value |
   |--------|-------|
   | `PF_IMAP_HOST` | e.g. `imap.gmail.com` (default) |
   | `PF_IMAP_USER` | the mailbox address |
   | `PF_IMAP_PASS` | a mail **app password** |

   Optional: `PF_IMAP_FOLDER` (default `INBOX`), `PF_IMAP_SINCE_DAYS` (default `7`).

The finder reads alert emails from known senders (Trade-A-Plane, Controller, ASO,
Barnstormers) and ingests **every aircraft** in them — not just Cessna 172s —
parsing out **make, model, year, price, total time, and seats**. These land on
the **For Sale** tab (shown in full, bypassing the 172/price hard filters), where
you narrow them with the dashboard's **make** dropdown, **seats** dropdown, and
text filter. Enabled automatically when `PF_IMAP_USER` is set.

> Scraped sources (GlobalAir, Barnstormers, auctions) stay Cessna-172-focused;
> your email alerts are what bring in other makes/models per your saved search.

**Verify it works:** Actions tab → **Test IMAP** → **Run workflow**. It connects
read-only and prints "login succeeded" plus how many alert listings it parsed —
without deploying anything. (Locally: `python plane_finder.py --test-imap`.)

## Files
- `plane_finder.py` — search, scoring, unicorn-detection, dashboard + digest output
- `.github/workflows/daily-plane-finder.yml` — daily schedule + Pages deploy (runs on GitHub's servers)
- `requirements.txt` — Python dependencies

## How you get the results
- **Web dashboard (primary):** every run publishes a sortable, filterable dashboard
  to **GitHub Pages** — the latest top matches, any unicorns, and a matches-per-day
  trend. Once Pages is enabled (below), it lives at:

  **https://mparisi78.github.io/plaintest/**

- **Unicorn alerts:** the workflow opens a **GitHub Issue** _only when a new unicorn
  appears_, so you get a notification for the rare standout without daily noise.

Both use the built-in `GITHUB_TOKEN` — no secrets required.

## One-time setup

### 1. Enable GitHub Pages
**Settings → Pages → Build and deployment → Source: GitHub Actions.**
(Pages on a **private** repo requires GitHub Pro/Team; public repos are free.)

### 2. Allow the workflow to write
**Settings → Actions → General → Workflow permissions → Read and write permissions.**
This lets it open issues and commit the state/history files.

### 3. Test it
**Actions** tab → **Daily Plane Finder** → **Run workflow**. Watch the log; you'll
see it fetch each site, score listings, build the dashboard, deploy to Pages, and
(if there's a new unicorn) open an issue. The dashboard link prints in the
**deploy** step output.

### 4. It's now automatic
The schedule runs daily at **12:00 UTC** (7 AM US Central / 8 AM Eastern). To change
the time, edit the `cron:` line in the workflow — format is `minute hour day month weekday`,
always in UTC.

## Optional: email delivery
The script can also email the digest over SMTP instead of (or in addition to)
the issue. Add these five secrets under **Settings → Secrets and variables →
Actions** and wire them into the workflow's "Run plane finder" step as env vars
(use a Gmail **App Password**, not your normal password):

| Secret name      | Value                                  |
|------------------|----------------------------------------|
| `PF_SMTP_HOST`   | `smtp.gmail.com`                       |
| `PF_SMTP_PORT`   | `587`                                  |
| `PF_SMTP_USER`   | your Gmail address                     |
| `PF_SMTP_PASS`   | your 16-character Gmail App Password    |
| `PF_TO_ADDR`     | where the digest should be sent        |

Gmail App Password: Google Account → Security → 2-Step Verification → App passwords.

## Honest expectations
- Listing sites sometimes **block automated requests (HTTP 403)**. If a run shows
  "parsed 0 listings," that's the site blocking, not a logic bug. Run locally with
  `python plane_finder.py --debug` to dump the HTML and adjust the parser's CSS selectors.
- **Back this up with the sites' own saved-search email alerts** (Trade-A-Plane, Controller).
  Those run server-side and never get blocked. This repo adds the ranking + unicorn layer
  on top; the saved searches guarantee nothing slips past.
- Keep this to personal use and respect each site's robots.txt / Terms of Service.

## Always verify
Confirm damage history, logs, and hours directly with the seller and a pre-buy inspection.
This tool surfaces candidates; it does not replace due diligence.
