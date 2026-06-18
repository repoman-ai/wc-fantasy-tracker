# ⚽ Bobby & Friends — Fantasy World Cup 2026 Tracker

A free, fully-automated tracker for a [tournamentsoccer.us](https://www.tournamentsoccer.us)
fantasy soccer pool. A GitHub Action scrapes the leaderboard and every player's
prediction sheet on a schedule, commits the data as JSON, and a single-file
website (hosted on GitHub Pages) turns it into a live, ESPN-style standings board
with per-player charts and a full prediction breakdown.

**No servers, no database, no cost** — just GitHub Actions (free & unlimited on
public repos) + GitHub Pages.

---

## What's in here

| File | Purpose |
|------|---------|
| `scraper.py` | Scrapes the leaderboard + per-player pages → writes `data/*.json` |
| `requirements.txt` | Python deps (`requests`, `beautifulsoup4`, `playwright`) |
| `.github/workflows/scrape-pool.yml` | The scheduled Action (cron in UTC, retry/backoff, change-only commits) |
| `index.html` | The whole website (embedded CSS/JS, Chart.js from CDN) |
| `data/rankings.json` | Current leaderboard snapshot |
| `data/history.json` | Append-only log of every snapshot (for trends & rank-change) |
| `data/predictions.json` | Per-player fixture/bonus breakdown, keyed by `team_id` |
| `sample-*.html` | The ground-truth HTML samples the parser was built/tested against |

The `data/` files are pre-seeded from the samples so the site shows content the
moment Pages goes live; the first real Action run overwrites them with live data.

---

## How it works

1. **`scraper.py`** fetches the pool page, parses `<div id="table-rounds">` for the
   standings (skipping ad rows), then visits each player's
   `…/team/{id}/{slug}/stage/1196-grand-total` page and parses every fixture,
   matchday subtotal, and bonus question.
   - Player names/rows are **never hardcoded** — they're extracted each run, so the
     pool can gain/lose players freely.
   - Kickoff times are converted to **UTC** in the JSON, so the website can display
     them in each viewer's local timezone.
   - **Retry/backoff:** if the leaderboard returns zero rows or errors, the whole
     run retries up to 3 times with a 30-minute gap (configurable via
     `MAX_ATTEMPTS` / `RETRY_GAP_SECONDS`). A single failing player page doesn't
     abort the run — that player keeps their previous data.
   - **403 avoidance:** the fast `requests` fetch warms up through public site
     pages first, keeps cookies/referers in one session, and adds a small
     delay+jitter between player pages. If a bot-protection 403/503 still appears,
     `FETCH_BACKEND=auto` (the GitHub Action default) switches to the browser
     fallback for the rest of the run. The browser path is paced the same way, so
     it doesn't burst either if the requests path is blocked from the first hit.
   - Files are written **only when valid data is present**. `history.json` only gets
     a new entry when the standings actually changed (no duplicate snapshots).
     Before any JSON is written, the scraper also fails closed if the leaderboard
     has duplicate/malformed rows, player predictions are missing, too many
     players had to use stale previous prediction data, or a prediction sheet is
     suspiciously thinner than the rest of the scrape.

2. **The workflow** runs on a cron schedule, installs deps, runs the scraper, and
   commits `data/*.json` **only if something changed** (no empty/duplicate commits).
   When the browser fallback kicks in it launches the runner's installed Chrome
   (`BROWSER_CHANNEL=chrome`) instead of downloading Playwright's bundled Chromium
   on every run. A failed scrape commits nothing.

3. **`index.html`** fetches the three JSON files and renders:
   - **Layer 1 — Standings:** ranked cards with flags, points, exact-score count,
     ▲▼ rank-movement vs the previous snapshot, green highlight for climbers, a
     "top climber / biggest drop / leader" strip, and gold/silver/bronze podium.
     A **📅 Full Schedule** button beside the heading (and a link under the
     recent-results strip) opens every match — played and upcoming — grouped by
     round in kickoff order, with an **All / Upcoming / Completed** segmented
     filter and each row showing its kickoff date + time.
   - **Layer 2 — Player view** (tap any row): points-over-time, rank-over-time, and
     exact-predictions-over-time charts, plus the full prediction sheet grouped by
     matchday — completed *and* upcoming matches — with a "🎯 perfect call" badge on
     exact scores, matchday subtotals, the bonus questions, and
     **Completed / Upcoming / Today-only** filters that combine.
   - **Layer 3 — Match view** (tap any match — in the live strip, the recent-results
     cards, the Full Schedule list, or any row on a player's sheet): a popup showing
     **every player's prediction for that one match** — their picked score (or, in
     the knockout rounds, the two teams they expect) and, once the game is
     live/finished, the points it earned them. Players sort by points for that game
     (or rank / A–Z), with a consensus header (how many nailed the exact score, who
     scored most, the crowd-favourite scoreline) and a pick-distribution chart that
     highlights the actual result. Opened from inside another view, it shows a
     **‹ Back** button that returns you exactly where you were.

   Every clock on the site (kickoff times, "updated" stamp, schedule rows)
   renders in one chosen **timezone**: it defaults to the viewer's auto-detected
   zone, but a picker in the footer (and in the Full Schedule toolbar) lets them
   switch to any common zone — the choice is saved in `localStorage` and applied
   site-wide so the clocks never disagree.

---

## One-time setup

### 1. Create the repo (public)

```bash
cd /path/to/this/folder            # the folder containing index.html, scraper.py, …
git init
git add .
git commit -m "Initial commit: fantasy WC tracker"
gh repo create wc-fantasy-tracker --public --source=. --push
```

No `gh` CLI? Create an empty **public** repo on github.com, then:

```bash
git init && git add . && git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<YOUR-USERNAME>/wc-fantasy-tracker.git
git push -u origin main
```

### 2. Allow the Action to commit

GitHub → your repo → **Settings → Actions → General → Workflow permissions** →
select **“Read and write permissions”** → **Save**.
(The workflow also requests this via `permissions: contents: write`, but this repo
setting must not be more restrictive.)

### 3. Enable GitHub Pages

GitHub → **Settings → Pages** →
**Source: “Deploy from a branch”** → **Branch: `main`**, **Folder: `/ (root)`** → **Save**.

After ~1 minute your site is live at:
`https://<YOUR-USERNAME>.github.io/wc-fantasy-tracker/`

### 4. Trigger the first scrape

GitHub → **Actions** tab → **“Scrape pool & update data”** → **Run workflow**
(green button on the right). Watch it run; on success it commits fresh
`data/*.json` and the site updates automatically.

---

## Setting your match-based cron times

Open `.github/workflows/scrape-pool.yml`. Cron is **always UTC**. Replace the
placeholder line with your own triggers (one `- cron:` line per trigger):

```yaml
schedule:
  - cron: "10 19 11 6 *"   # 2026-06-11 19:10 UTC
  - cron: "40 21 11 6 *"   # 2026-06-11 21:40 UTC
```

Format is `minute hour day month day-of-week`. A common pattern is one run shortly
after each kickoff and one ~2.5 h later (full-time) so points update promptly.
Until you customize, it runs every 6 hours. You can always hit **Run workflow**
manually too.

> Tip: GitHub's scheduled runs can be delayed a few minutes under load, and only
> run on the default branch.

---

## Run it locally

```bash
pip install -r requirements.txt

python scraper.py --samples --out data   # parse the bundled sample HTML (offline)
python scraper.py --out data             # real live scrape
FETCH_BACKEND=browser python scraper.py --out data  # skip requests and use Chrome

# preview the site (fetch() needs http://, not file://)
python -m http.server 8000               # then open http://localhost:8000
```

`--samples` mode only has full prediction data for the sample player (JJ); other
players show an empty prediction sheet until a real run fills them in.

---

## Customizing

- **Different pool:** edit `POOL_ID` / `POOL_SLUG` at the top of `scraper.py`.
- **Retry behavior:** `MAX_ATTEMPTS` / `RETRY_GAP_SECONDS` env vars (set in the workflow).
- **Scrape politeness / 403 avoidance:** `REQUESTS_WARMUP` toggles the public-page
  warm-up, `REQUEST_WARMUP_URLS` overrides the comma-separated warm-up path list,
  and `REQUEST_DELAY_SECONDS` / `REQUEST_JITTER_SECONDS` control pacing between
  page requests. `FETCH_BACKEND=browser` skips the requests backend entirely.
  `BROWSER_CHANNEL=chrome` or `BROWSER_EXECUTABLE_PATH=/path/to/chrome` lets
  Playwright use an already-installed browser instead of a downloaded one.
- **Health checks:** `MIN_PREDICTION_COVERAGE` controls how many leaderboard
  players must have prediction records, `MAX_STALE_PREDICTION_RATIO` limits how
  many records can come only from previous saved data, and
  `MIN_FIXTURE_COVERAGE_RATIO` rejects prediction sheets that are much thinner
  than the richest sheet in the same scrape.
- **Colors/branding:** the CSS variables at the top of `index.html` (`:root { … }`).
