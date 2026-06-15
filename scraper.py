#!/usr/bin/env python3
"""
Fantasy Soccer World Cup 2026 — pool scraper.

Scrapes:
  1. The pool leaderboard table  (<div id="table-rounds">)
  2. Each player's full prediction page, using the stage-specific links from the leaderboard

Outputs (written only when a scrape succeeds and has data):
  rankings.json     current leaderboard snapshot
  history.json      append-only log of leaderboard snapshots (deduped on no change)
  predictions.json  per-player fixture/bonus breakdown, keyed by team_id

Design notes
------------
* No hardcoded player names / row counts — everything is extracted per run.
* Player set can grow/shrink between runs; history.json keys everything by team_id
  so the UI can chart a varying set of players over time.
* Network is fetched with a persistent `requests` session by default. If the site
  returns a bot-protection 403/503, the live scraper can automatically fall back to
  Playwright/Chromium while leaving the parser unchanged.
* Retries: the whole run is retried up to MAX_ATTEMPTS times with RETRY_GAP_SECONDS
  between attempts if the leaderboard yields zero rows or throws. A single player page
  failing does NOT abort the run — that player keeps their previously-scraped data.

Usage
-----
  python scraper.py                       # live scrape
  python scraper.py --samples             # parse the local sample HTML files (offline test)
  python scraper.py --out ./data          # write JSON into ./data instead of repo root
  MAX_ATTEMPTS=4 RETRY_GAP_SECONDS=1800 python scraper.py
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE = "https://www.tournamentsoccer.us"
POOL_ID = "34193"
POOL_SLUG = "bobby-and-friends"
LEADERBOARD_URL = f"{BASE}/fantasy-soccer-world-cup/2026/pool/{POOL_ID}/{POOL_SLUG}/"

# Flag SVGs reuse the source CDN (the codes match exactly, e.g. FRA/ESP/RSA).
FLAG_URL_TMPL = "https://assets.tournamentsoccer.us/flags/4x3/{code}.svg"

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "4"))          # 1 try + 3 retries
RETRY_GAP_SECONDS = int(os.environ.get("RETRY_GAP_SECONDS", "1800"))  # 30 minutes
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
CF_CHALLENGE_TIMEOUT = int(os.environ.get("CF_CHALLENGE_TIMEOUT", "25"))
FETCH_BACKEND = os.environ.get("FETCH_BACKEND", "auto").lower()  # auto, requests, browser
BROWSER_HEADLESS = os.environ.get("BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"}
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Referer": "https://www.tournamentsoccer.us/fantasy-soccer-world-cup/2026/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

FLAG_RE = re.compile(r"flags/4x3/([A-Za-z0-9]+)\.svg")
TEAM_HREF_RE = re.compile(r"/team/(\d+)/([^/]+)/")
TEAM_PAGE_RE = re.compile(r"/team/(\d+)/")
STAGE_RE = re.compile(r"/stage/([^/?#\"']+)")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def to_int(text: str | None):
    """Return an int if `text` is a clean integer, else None ('-' -> None)."""
    t = clean(text)
    if not t or t == "-":
        return None
    m = re.search(r"-?\d+", t.replace(",", ""))
    return int(m.group()) if m else None


def flag_code_from_style(style: str | None):
    if not style:
        return None
    m = FLAG_RE.search(style)
    return m.group(1).upper() if m else None


def to_utc_iso(data_datetime: str | None, tz_name: str | None):
    """Interpret 'YYYY-MM-DD HH:MM' as wall-clock time in `tz_name`, return UTC ISO 8601."""
    if not data_datetime:
        return None
    try:
        naive = datetime.strptime(data_datetime.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if ZoneInfo and tz_name:
        try:
            aware = naive.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            aware = naive.replace(tzinfo=timezone.utc)
    else:
        aware = naive.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
REQUEST_SESSION = requests.Session()
REQUEST_SESSION.headers.update(HTTP_HEADERS)
_BROWSER_FETCHER = None
_USING_BROWSER_FETCH = False


def _response_excerpt(text: str) -> str:
    return clean(text[:500])


def _looks_like_cloudflare_challenge(html: str) -> bool:
    sample = html[:2000].lower()
    return "just a moment" in sample and "challenges.cloudflare.com" in sample


def _looks_like_scrape_page(html: str) -> bool:
    return "id=\"table-rounds\"" in html or "data-fixture-number=" in html


class BrowserFetcher:
    def __init__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright fallback is required after an HTTP block, but the "
                "`playwright` package is not installed. Run `pip install -r requirements.txt`."
            ) from exc

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=BROWSER_HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            locale="en-US",
            extra_http_headers={
                "Accept-Language": HTTP_HEADERS["Accept-Language"],
            },
        )
        self._page = self._context.new_page()
        self._leaderboard_url = None

    def fetch_page(self, url: str) -> tuple[str, str]:
        if "/team/" in url and self._leaderboard_url:
            try:
                return self._fetch_team_page_from_leaderboard(url)
            except Exception as exc:
                print(f"  http: leaderboard-click fetch failed, trying direct URL: {exc}", file=sys.stderr)

        response = self._page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=REQUEST_TIMEOUT * 1000,
        )
        self._wait_for_page_or_challenge()
        status = response.status if response else None
        html = self._page.content()
        final_url = self._page.url

        if status in {403, 503} and _looks_like_cloudflare_challenge(html):
            print(f"  http: waiting for Cloudflare challenge at {final_url}", file=sys.stderr)
            self._wait_for_cloudflare_clearance()
            html = self._page.content()
            final_url = self._page.url

        if status and status >= 400 and _looks_like_cloudflare_challenge(html):
            raise RuntimeError(
                f"Browser fetch could not clear Cloudflare challenge for {final_url}; "
                f"body starts: {_response_excerpt(html)}"
            )
        if status and status >= 400 and _looks_like_scrape_page(html):
            if "id=\"table-rounds\"" in html:
                self._leaderboard_url = final_url
            return html, final_url
        if status and status >= 400:
            raise RuntimeError(
                f"Browser fetch got HTTP {status} for {final_url}; body starts: {_response_excerpt(html)}"
            )
        if "id=\"table-rounds\"" in html:
            self._leaderboard_url = final_url
        return html, final_url

    def _fetch_team_page_from_leaderboard(self, url: str) -> tuple[str, str]:
        team_match = TEAM_PAGE_RE.search(url)
        if not team_match:
            raise RuntimeError("could not extract team id from URL")

        team_id = team_match.group(1)
        if "id=\"table-rounds\"" not in self._page.content():
            self._page.goto(
                self._leaderboard_url,
                wait_until="domcontentloaded",
                timeout=REQUEST_TIMEOUT * 1000,
            )
            self._wait_for_page_or_challenge()

        locator = self._page.locator(f'a[href*="/team/{team_id}/"]').first
        if locator.count() == 0:
            raise RuntimeError(f"team link {team_id} not found on leaderboard")

        locator.click(no_wait_after=True)
        try:
            self._page.wait_for_url(
                re.compile(rf"/team/{re.escape(team_id)}/"),
                wait_until="domcontentloaded",
                timeout=min(REQUEST_TIMEOUT, 10) * 1000,
            )
        except Exception as exc:
            raise RuntimeError(f"click did not navigate to team {team_id}") from exc

        self._wait_for_page_or_challenge()
        html = self._page.content()
        final_url = self._page.url
        if _looks_like_cloudflare_challenge(html):
            print(f"  http: waiting for Cloudflare challenge at {final_url}", file=sys.stderr)
            self._wait_for_cloudflare_clearance()
            html = self._page.content()
            final_url = self._page.url

        if _looks_like_cloudflare_challenge(html):
            raise RuntimeError(
                f"Browser click could not clear Cloudflare challenge for {final_url}; "
                f"body starts: {_response_excerpt(html)}"
            )
        if not _looks_like_scrape_page(html):
            raise RuntimeError(f"player page did not contain prediction rows; body starts: {_response_excerpt(html)}")
        return html, final_url

    def _wait_for_page_or_challenge(self):
        # The page is server-rendered, but analytics/ad requests can keep Chromium
        # from ever reaching "networkidle". Wait for either useful HTML or a known
        # Cloudflare challenge, then let the caller decide what to do.
        try:
            self._page.wait_for_function(
                """
                () => document.querySelector('#table-rounds, tr[data-fixture-number]')
                  || (document.title || '').match(/just a moment/i)
                """,
                timeout=REQUEST_TIMEOUT * 1000,
            )
        except Exception:
            self._page.wait_for_timeout(1500)

    def _wait_for_cloudflare_clearance(self):
        try:
            self._page.wait_for_function(
                """
                () => !(document.title || '').match(/just a moment/i)
                  && !document.documentElement.innerText.match(/checking if the site connection is secure/i)
                """,
                timeout=CF_CHALLENGE_TIMEOUT * 1000,
            )
            self._page.wait_for_timeout(1500)
        except Exception as exc:
            raise RuntimeError(
                f"Timed out after {CF_CHALLENGE_TIMEOUT}s waiting for Cloudflare challenge to clear"
            ) from exc

    def close(self):
        for obj in (self._context, self._browser):
            try:
                obj.close()
            except Exception:
                pass
        try:
            self._pw.stop()
        except Exception:
            pass


def _get_browser_fetcher() -> BrowserFetcher:
    global _BROWSER_FETCHER
    if _BROWSER_FETCHER is None:
        print("  http: starting Playwright browser fallback", file=sys.stderr)
        _BROWSER_FETCHER = BrowserFetcher()
        atexit.register(_BROWSER_FETCHER.close)
    return _BROWSER_FETCHER


def _fetch_page_with_requests(url: str) -> tuple[str, str]:
    resp = REQUEST_SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    if resp.status_code in {403, 503}:
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for {resp.url}; body starts: {_response_excerpt(resp.text)}",
            response=resp,
        )
    resp.raise_for_status()
    return resp.text, resp.url


def fetch_page(url: str) -> tuple[str, str]:
    global _USING_BROWSER_FETCH

    if FETCH_BACKEND == "browser" or _USING_BROWSER_FETCH:
        return _get_browser_fetcher().fetch_page(url)
    if FETCH_BACKEND != "auto" and FETCH_BACKEND != "requests":
        raise RuntimeError("FETCH_BACKEND must be one of: auto, requests, browser")

    try:
        return _fetch_page_with_requests(url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if FETCH_BACKEND == "auto" and status in {403, 503}:
            print(f"  http: requests got {status}; switching to browser fetch", file=sys.stderr)
            _USING_BROWSER_FETCH = True
            return _get_browser_fetcher().fetch_page(url)
        raise


def fetch(url: str) -> str:
    return fetch_page(url)[0]


def extract_stage(url: str | None) -> str | None:
    if not url:
        return None
    m = STAGE_RE.search(url)
    return m.group(1) if m else None


def _same_url(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (
        pa.scheme,
        pa.netloc,
        pa.path.rstrip("/"),
        pa.query,
    ) == (
        pb.scheme,
        pb.netloc,
        pb.path.rstrip("/"),
        pb.query,
    )


def _last_known_stage(rankings_path: Path, predictions_path: Path) -> str | None:
    rankings = load_json(rankings_path, {})
    stage = rankings.get("source", {}).get("stage") if isinstance(rankings, dict) else None
    if stage:
        return stage

    urls = []
    if isinstance(rankings, dict):
        urls.extend(p.get("player_url") for p in rankings.get("players", []) if isinstance(p, dict))

    predictions = load_json(predictions_path, {})
    if isinstance(predictions, dict):
        urls.extend(
            p.get("player_url")
            for p in predictions.get("players", {}).values()
            if isinstance(p, dict)
        )

    for url in urls:
        stage = extract_stage(url)
        if stage:
            return stage
    return None


def resolve_leaderboard(last_known_stage: str | None = None) -> tuple[str, dict]:
    """Fetch the current leaderboard HTML, following redirects and stage changes."""
    tried = []
    seen = set()

    def add(url: str | None):
        if not url:
            return
        absolute = urljoin(LEADERBOARD_URL, url)
        key = absolute.rstrip("/")
        if key not in seen:
            seen.add(key)
            tried.append(absolute)

    add(LEADERBOARD_URL)
    add(f"{LEADERBOARD_URL}ranking")
    if last_known_stage:
        add(f"{LEADERBOARD_URL}ranking/stage/{last_known_stage}")

    for candidate in tried:
        html, final_url = fetch_page(candidate)
        players = parse_leaderboard(html)
        if players:
            stage = extract_stage(final_url)
            if not stage:
                stage = next(
                    (
                        player_stage
                        for p in players
                        if (player_stage := extract_stage(p.get("player_url")))
                    ),
                    None,
                )
            source = {
                "leaderboard_url": final_url,
                "requested_url": candidate,
                "stage": stage,
            }
            print(f"  leaderboard: resolved to {final_url}", file=sys.stderr)
            if stage:
                print(f"  stage: {stage}", file=sys.stderr)
            return html, source

        if not _same_url(candidate, final_url):
            add(final_url)

    raise RuntimeError("Leaderboard produced 0 rows from all resolved candidates")


# --------------------------------------------------------------------------- #
# Leaderboard parsing
# --------------------------------------------------------------------------- #
def parse_leaderboard(html: str) -> list[dict]:
    """Return a list of player dicts in rank order. Ad rows (<tr> without id) are skipped."""
    soup = soup_of(html)
    container = soup.find(id="table-rounds")
    scope = container if container else soup
    table = scope.find("table")
    if not table:
        return []

    players = []
    for tr in table.select("tbody > tr"):
        row_id = tr.get("id")
        if not row_id:  # ad row
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue

        rank = to_int(tds[0].get_text())

        link = tds[1].find("a", href=True)
        if not link:
            continue
        name = clean(link.get_text())
        href = link["href"]
        m = TEAM_HREF_RE.search(href)
        if not m:
            continue
        team_id, slug = m.group(1), m.group(2)
        player_url = urljoin(BASE, href)

        flag_span = tds[1].select_one("span.flag-icon")
        flag = flag_code_from_style(flag_span.get("style")) if flag_span else None

        exact = to_int(tds[2].get_text()) or 0
        points = to_int(tds[3].get_text()) or 0

        players.append(
            {
                "team_id": team_id,
                "slug": slug,
                "player_url": player_url,
                "name": name,
                "flag": flag,
                "rank": rank,
                "exact": exact,
                "points": points,
            }
        )
    return players


# --------------------------------------------------------------------------- #
# Player-page parsing
# --------------------------------------------------------------------------- #
def _team_from_col(col) -> dict:
    """Extract {code, name, hidden} from a .col div (flag+name, or eye-slash = hidden)."""
    if col is None:
        return {"code": None, "name": None, "hidden": False}
    if col.select_one(".fa-eye-slash"):
        return {"code": None, "name": None, "hidden": True}
    flag = col.select_one(".flag-icon")
    code = flag_code_from_style(flag.get("style")) if flag else None
    # Prefer the long (desktop) name span, else the short, else whatever text remains.
    long_name = col.select_one(".d-none.d-md-inline-block")
    short_name = col.select_one(".d-md-none")
    name = clean(long_name.get_text()) if long_name else (
        clean(short_name.get_text()) if short_name else clean(col.get_text())
    )
    return {"code": code, "name": name or None, "hidden": False}


def _parse_group_fixture(tds, fixture_number, round_name) -> dict:
    date_td, match_td, pred_td, score_td, pts_td = tds[0], tds[1], tds[2], tds[3], tds[4]

    badge = date_td.select_one(".badge")
    jstz = date_td.select_one(".js-tz")
    dt_raw = jstz.get("data-datetime") if jstz else None
    tz_name = jstz.get("data-timezone") if jstz else None

    cols = match_td.find_all("div", class_="col")
    home = _team_from_col(cols[0]) if len(cols) > 0 else {"code": None, "name": None, "hidden": False}
    away = _team_from_col(cols[1]) if len(cols) > 1 else {"code": None, "name": None, "hidden": False}

    predicted_score = clean(pred_td.get_text())
    actual_score = clean(score_td.get_text())
    points = to_int(pts_td.select_one(".dotted_underline").get_text()) if pts_td.select_one(".dotted_underline") else to_int(pts_td.get_text())

    status = clean(badge.get_text()) if badge else (actual_score or None)

    return {
        "fixture_number": fixture_number,
        "round": round_name,
        "stage_type": "group",
        "status": status,
        "kickoff_utc": to_utc_iso(dt_raw, tz_name),
        "kickoff_local": dt_raw,
        "kickoff_timezone": tz_name,
        "hidden": False,
        "match": {"home": home, "away": away},
        "predicted": {"type": "score", "score": predicted_score or None, "hidden": False},
        "actual_score": actual_score or None,
        "points": points,
        "is_exact": points == 90,
    }


def _parse_knockout_fixture(tds, fixture_number, round_name) -> dict:
    date_td = tds[0]
    match_td = tds[1]      # composite: "Predicted" teams + "Score" (bracket slots)
    score_td = tds[2]      # actual result (NS / real score)
    pts_td = tds[3]        # Countries pts + Score pts

    badge = date_td.select_one(".badge")
    jstz = date_td.select_one(".js-tz")
    dt_raw = jstz.get("data-datetime") if jstz else None
    tz_name = jstz.get("data-timezone") if jstz else None

    # Split the composite cell into labelled sections, preferring desktop rows.
    sections: dict[str, dict] = {}
    current = None
    for child in match_td.find_all("div", recursive=False):
        cls = child.get("class", [])
        if "small" in cls:
            current = clean(child.get_text())
            sections.setdefault(current, {"desktop": [], "mobile": []})
        elif "row" in cls:
            sections.setdefault(current, {"desktop": [], "mobile": []})
            bucket = "mobile" if "d-md-none" in cls else "desktop"
            sections[current][bucket].append(child)

    def pick(label):
        s = sections.get(label)
        if not s:
            return None
        rows = s["desktop"] or s["mobile"]
        return rows[0] if rows else None

    # Player's predicted teams (or hidden) live under "Predicted".
    pred_row = pick("Predicted")
    pred_cols = pred_row.find_all("div", class_="col") if pred_row else []
    p_home = _team_from_col(pred_cols[0]) if len(pred_cols) > 0 else {"code": None, "name": None, "hidden": True}
    p_away = _team_from_col(pred_cols[1]) if len(pred_cols) > 1 else {"code": None, "name": None, "hidden": True}
    hidden = bool(p_home.get("hidden") or p_away.get("hidden"))

    # The actual matchup identity (bracket slot labels like "2A"/"W73") lives under "Score".
    slot_row = pick("Score")
    slot_cols = slot_row.find_all("div", class_="col") if slot_row else []
    m_home = {"code": None, "name": clean(slot_cols[0].get_text()) or None, "hidden": False} if len(slot_cols) > 0 else {"code": None, "name": None, "hidden": False}
    m_away = {"code": None, "name": clean(slot_cols[1].get_text()) or None, "hidden": False} if len(slot_cols) > 1 else {"code": None, "name": None, "hidden": False}

    # Actual result
    actual_link = score_td.find("a")
    actual_score = clean(actual_link.get_text()) if actual_link else clean(score_td.get_text())

    # Points split into Countries + Score components.
    du = pts_td.select_one(".dotted_underline")
    pts_countries = to_int(du.get_text()) if du else None
    # The score-component points are the trailing text after the second label.
    pts_text_nodes = [clean(t) for t in pts_td.find_all(string=True) if clean(t) and clean(t) not in ("Countries", "Score")]
    pts_score = None
    if pts_text_nodes:
        # last meaningful node is the score points ('-' or a number)
        pts_score = to_int(pts_text_nodes[-1])
    total = (pts_countries or 0) + (pts_score or 0)
    if pts_countries is None and pts_score is None:
        total = None

    status = clean(badge.get_text()) if badge else (actual_score or None)

    return {
        "fixture_number": fixture_number,
        "round": round_name,
        "stage_type": "knockout",
        "status": status,
        "kickoff_utc": to_utc_iso(dt_raw, tz_name),
        "kickoff_local": dt_raw,
        "kickoff_timezone": tz_name,
        "hidden": hidden,
        "match": {"home": m_home, "away": m_away},
        "predicted": {
            "type": "teams",
            "hidden": hidden,
            "home": None if hidden else p_home,
            "away": None if hidden else p_away,
        },
        "actual_score": actual_score or None,
        "points": total,
        "points_breakdown": {"countries": pts_countries, "score": pts_score},
        "is_exact": False,
    }


def _parse_fixture(tr, round_name):
    fixture_number = to_int(tr.get("data-fixture-number"))
    tds = tr.find_all("td", recursive=False)
    try:
        if len(tds) >= 5:
            return _parse_group_fixture(tds, fixture_number, round_name)
        if len(tds) == 4:
            return _parse_knockout_fixture(tds, fixture_number, round_name)
    except Exception as exc:  # never let one weird row kill the page
        return {
            "fixture_number": fixture_number,
            "round": round_name,
            "stage_type": "unknown",
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return None


def _pref_text(cell) -> str:
    """Prefer the desktop (long) variant of a cell that has mobile/desktop spans."""
    long = cell.select_one(".d-none.d-md-inline-block")
    short = cell.select_one(".d-md-none")
    if long:
        return clean(long.get_text())
    if short:
        return clean(short.get_text())
    return clean(cell.get_text())


def _parse_bonus(nested_table) -> dict:
    out = {"questions": [], "subtotal": None}
    body = nested_table.find("tbody") or nested_table
    for tr in body.find_all("tr", recursive=False):
        if tr.find("th"):  # "Bonus Questions" header
            continue
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        label = clean(tds[0].get_text())
        last_val = to_int(tds[-1].get_text())
        if label.lower() == "subtotal":
            out["subtotal"] = last_val
            continue
        if label.lower() == "total":
            out["grand_total"] = last_val
            continue
        if len(tds) < 4:
            continue
        pred_cell, actual_cell = tds[1], tds[2]
        pred_flag = pred_cell.select_one(".flag-icon")
        actual_flag = actual_cell.select_one(".flag-icon")
        out["questions"].append(
            {
                "question": label,
                "prediction": {
                    "text": _pref_text(pred_cell) or None,
                    "flag": flag_code_from_style(pred_flag.get("style")) if pred_flag else None,
                },
                "actual": {
                    "text": _pref_text(actual_cell) or None,
                    "flag": flag_code_from_style(actual_flag.get("style")) if actual_flag else None,
                },
                "points": last_val,
            }
        )
    return out


def parse_player_page(html: str) -> dict:
    """Return {matchdays:[...], bonus:{...}, grand_total, totals}."""
    soup = soup_of(html)
    # The outer matches table is the first .table-sm (the bonus table is nested inside it).
    outer = soup.find("table", class_="table-sm")
    if not outer:
        return {"matchdays": [], "bonus": {"questions": []}, "grand_total": None}
    tbody = outer.find("tbody")
    rows = tbody.find_all("tr", recursive=False) if tbody else []

    matchdays: list[dict] = []
    current = None
    bonus = {"questions": []}
    grand_total = None

    for tr in rows:
        cls = tr.get("class", [])
        if "d-md-none" in cls:
            continue  # mobile-only duplicate

        # Round / matchday header row
        th = tr.find("th", recursive=False)
        if th is not None:
            current = {"round": clean(th.get_text()), "subtotal": None, "fixtures": []}
            matchdays.append(current)
            continue

        # Bonus block: a row whose cell contains a nested table
        nested = tr.find("table")
        if nested is not None:
            bonus = _parse_bonus(nested)
            grand_total = bonus.pop("grand_total", None)
            continue

        # Fixture row
        if tr.get("data-fixture-number") is not None:
            fixture = _parse_fixture(tr, current["round"] if current else None)
            if fixture:
                if current is None:
                    current = {"round": None, "subtotal": None, "fixtures": []}
                    matchdays.append(current)
                current["fixtures"].append(fixture)
            continue

        # Subtotal row (outer table)
        tds = tr.find_all("td", recursive=False)
        if tds and clean(tds[0].get_text()).lower() == "subtotal":
            if current is not None:
                current["subtotal"] = to_int(tds[-1].get_text())
            continue

    return {
        "matchdays": matchdays,
        "bonus": bonus,
        "grand_total": grand_total,
    }


def prediction_has_data(prediction: dict | None) -> bool:
    if not isinstance(prediction, dict):
        return False
    return any(
        md.get("fixtures")
        for md in prediction.get("matchdays", [])
        if isinstance(md, dict)
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def scrape_once(get_leaderboard_html, get_player_html, prev_predictions: dict) -> tuple[list[dict], dict, dict]:
    """Run a single scrape pass. Returns (players, predictions_by_team_id, stats)."""
    leaderboard_html = get_leaderboard_html()
    players = parse_leaderboard(leaderboard_html)
    if not players:
        raise RuntimeError("Leaderboard produced 0 rows")

    predictions = {}
    stats = {"fresh": 0, "kept": 0, "failed": 0}
    for p in players:
        tid = p["team_id"]
        try:
            if not p.get("player_url"):
                raise RuntimeError("missing player URL in leaderboard row")
            html = get_player_html(p)
            parsed = parse_player_page(html)
            if not prediction_has_data(parsed):
                raise RuntimeError("player page produced 0 parsed fixtures")
            predictions[tid] = {
                "team_id": tid,
                "name": p["name"],
                "slug": p["slug"],
                "player_url": p.get("player_url"),
                "flag": p["flag"],
                "rank": p["rank"],
                "points": p["points"],
                "exact": p["exact"],
                "grand_total": parsed["grand_total"],
                "matchdays": parsed["matchdays"],
                "bonus": parsed["bonus"],
            }
            stats["fresh"] += 1
        except Exception as exc:
            stats["failed"] += 1
            print(f"  ! player {tid} ({p['name']}) failed: {exc}", file=sys.stderr)
            # Keep previous data for this player rather than wiping it.
            if tid in prev_predictions and prediction_has_data(prev_predictions[tid]):
                kept = dict(prev_predictions[tid])
                kept.update(
                    {
                        "rank": p["rank"],
                        "points": p["points"],
                        "exact": p["exact"],
                        "name": p["name"],
                        "slug": p["slug"],
                        "player_url": p.get("player_url"),
                        "flag": p["flag"],
                    }
                )
                predictions[tid] = kept
                stats["kept"] += 1

    if players and stats["fresh"] == 0:
        raise RuntimeError(
            f"All {len(players)} player prediction pages failed; kept {stats['kept']} previous records"
        )

    return players, predictions, stats


def build_rankings(players: list[dict], source: dict | None = None) -> dict:
    return {
        "updated": now_iso(),
        "pool": {"id": POOL_ID, "slug": POOL_SLUG, "name": POOL_SLUG.replace("-", " ").title()},
        "source": source or {},
        "players": [
            {
                "team_id": p["team_id"],
                "slug": p["slug"],
                "player_url": p.get("player_url"),
                "name": p["name"],
                "flag": p["flag"],
                "rank": p["rank"],
                "points": p["points"],
                "exact": p["exact"],
            }
            for p in players
        ],
    }


def snapshot_signature(players: list[dict]) -> str:
    """Stable signature of a leaderboard snapshot for dedup (ignores timestamp)."""
    rows = sorted(
        ((p["team_id"], p["rank"], p["points"], p["exact"]) for p in players),
        key=lambda r: r[0],
    )
    return json.dumps(rows)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def run(out_dir: Path, samples: bool):
    out_dir.mkdir(parents=True, exist_ok=True)
    rankings_path = out_dir / "rankings.json"
    history_path = out_dir / "history.json"
    predictions_path = out_dir / "predictions.json"

    prev_predictions = load_json(predictions_path, {}).get("players", {})

    if samples:
        here = Path(__file__).resolve().parent
        lb_html = (here / "sample-leaderboard.html").read_text()
        player_html = (here / "sample-player-page.html").read_text()
        source_info = {
            "leaderboard_url": "sample-leaderboard.html",
            "requested_url": "sample-leaderboard.html",
            "stage": extract_stage(lb_html),
        }
        get_lb = lambda: lb_html
        # Only JJ (247528) has a sample page; others fall back gracefully.
        get_player = lambda p: player_html if p["team_id"] == "247528" else "<html></html>"
        attempts = 1
    else:
        source_info = {}

        def get_lb():
            html, source = resolve_leaderboard(_last_known_stage(rankings_path, predictions_path))
            source_info.clear()
            source_info.update(source)
            return html

        get_player = lambda p: fetch(p["player_url"])
        attempts = MAX_ATTEMPTS

    players, predictions, stats = [], {}, {}
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            print(f"Attempt {attempt}/{attempts} …", file=sys.stderr)
            players, predictions, stats = scrape_once(get_lb, get_player, prev_predictions)
            print(
                "  ok: "
                f"{len(players)} players, "
                f"{stats['fresh']} fresh prediction pages, "
                f"{stats['kept']} kept previous",
                file=sys.stderr,
            )
            break
        except Exception as exc:
            last_err = exc
            print(f"  attempt failed: {exc}", file=sys.stderr)
            if attempt < attempts:
                print(f"  waiting {RETRY_GAP_SECONDS}s before retry …", file=sys.stderr)
                time.sleep(RETRY_GAP_SECONDS)

    if not players:
        print(f"FATAL: all attempts failed; not writing files. Last error: {last_err}", file=sys.stderr)
        return 1

    # rankings.json
    rankings = build_rankings(players, source_info)
    write_json(rankings_path, rankings)

    # history.json — append only if the snapshot actually changed.
    history = load_json(history_path, [])
    sig = snapshot_signature(players)
    last_sig = snapshot_signature(history[-1]["players"]) if history else None
    if sig != last_sig:
        history.append(
            {
                "timestamp": rankings["updated"],
                "players": [
                    {
                        "team_id": p["team_id"],
                        "name": p["name"],
                        "slug": p["slug"],
                        "player_url": p.get("player_url"),
                        "flag": p["flag"],
                        "rank": p["rank"],
                        "points": p["points"],
                        "exact": p["exact"],
                    }
                    for p in players
                ],
            }
        )
        write_json(history_path, history)
        print(f"  history: appended snapshot ({len(history)} total)", file=sys.stderr)
    else:
        print("  history: unchanged since last snapshot, not appending", file=sys.stderr)

    # predictions.json
    write_json(predictions_path, {"updated": rankings["updated"], "players": predictions})

    print("Done.", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Scrape the fantasy soccer pool.")
    ap.add_argument("--samples", action="store_true", help="parse local sample HTML (offline test)")
    ap.add_argument("--out", default=".", help="output directory for JSON files (default: repo root)")
    args = ap.parse_args()
    sys.exit(run(Path(args.out).resolve(), args.samples))


if __name__ == "__main__":
    main()
