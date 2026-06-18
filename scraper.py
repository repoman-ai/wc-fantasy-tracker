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
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
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
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "").strip() or None
BROWSER_EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
REQUESTS_WARMUP = os.environ.get("REQUESTS_WARMUP", "1").lower() not in {"0", "false", "no"}

# Site-specific knobs for the cheap requests backend. The goal is to enter the
# site like a normal browser session before opening pool/team URLs, with pacing
# that does not look like a burst of isolated script hits from a CI runner.
DEFAULT_WARMUP_URLS = [
    f"{BASE}/",
    f"{BASE}/fantasy-soccer-world-cup/2026/",
]
REQUEST_WARMUP_URLS = [
    u.strip()
    for u in os.environ.get("REQUEST_WARMUP_URLS", ",".join(DEFAULT_WARMUP_URLS)).split(",")
    if u.strip()
]
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "1.25"))
REQUEST_JITTER_SECONDS = float(os.environ.get("REQUEST_JITTER_SECONDS", "0.75"))
MIN_PREDICTION_COVERAGE = float(os.environ.get("MIN_PREDICTION_COVERAGE", "1.0"))
MAX_STALE_PREDICTION_RATIO = float(os.environ.get("MAX_STALE_PREDICTION_RATIO", "0.25"))
MIN_FIXTURE_COVERAGE_RATIO = float(os.environ.get("MIN_FIXTURE_COVERAGE_RATIO", "0.80"))
BROWSER_LOCALE = os.environ.get("BROWSER_LOCALE", "en-US")
BROWSER_TIMEZONE = os.environ.get("BROWSER_TIMEZONE", "America/New_York")
BROWSER_VIEWPORT_WIDTH = int(os.environ.get("BROWSER_VIEWPORT_WIDTH", "1365"))
BROWSER_VIEWPORT_HEIGHT = int(os.environ.get("BROWSER_VIEWPORT_HEIGHT", "768"))
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    # Modern Chrome client hints — Cloudflare's bot heuristics expect these from a
    # browser claiming to be Chrome, and their absence is a cheap tell.
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
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


# Authoritative group-stage kickoffs from the official FIFA 2026 schedule
# (Eastern Time). The upstream pool site has published a few group kickoffs an
# hour or two off (and occasionally with no time at all), which threw off the
# clock-based "live" state. Keyed by the unordered pair of team codes — unique
# in the group stage — so it overrides the scraped time regardless of which side
# the source lists as home. Times below are stored as UTC (ET + 4h, EDT).
# (homeCode, awayCode) order-independent -> (kickoff_utc, kickoff_local ET)
OFFICIAL_GROUP_KICKOFFS = {
    frozenset(("MEX", "RSA")): ("2026-06-11T19:00:00Z", "2026-06-11 15:00"),
    frozenset(("CZE", "KOR")): ("2026-06-12T02:00:00Z", "2026-06-11 22:00"),
    frozenset(("BIH", "CAN")): ("2026-06-12T19:00:00Z", "2026-06-12 15:00"),
    frozenset(("PAR", "USA")): ("2026-06-13T01:00:00Z", "2026-06-12 21:00"),
    frozenset(("QAT", "SUI")): ("2026-06-13T19:00:00Z", "2026-06-13 15:00"),
    frozenset(("BRA", "MAR")): ("2026-06-13T22:00:00Z", "2026-06-13 18:00"),
    frozenset(("HAI", "SCO")): ("2026-06-14T01:00:00Z", "2026-06-13 21:00"),
    frozenset(("AUS", "TUR")): ("2026-06-14T04:00:00Z", "2026-06-14 00:00"),
    frozenset(("CUW", "GER")): ("2026-06-14T17:00:00Z", "2026-06-14 13:00"),
    frozenset(("JPN", "NED")): ("2026-06-14T20:00:00Z", "2026-06-14 16:00"),
    frozenset(("CIV", "ECU")): ("2026-06-14T23:00:00Z", "2026-06-14 19:00"),
    frozenset(("SWE", "TUN")): ("2026-06-15T02:00:00Z", "2026-06-14 22:00"),
    frozenset(("CPV", "ESP")): ("2026-06-15T16:00:00Z", "2026-06-15 12:00"),
    frozenset(("BEL", "EGY")): ("2026-06-15T19:00:00Z", "2026-06-15 15:00"),
    frozenset(("KSA", "URU")): ("2026-06-15T22:00:00Z", "2026-06-15 18:00"),
    frozenset(("IRN", "NZL")): ("2026-06-16T01:00:00Z", "2026-06-15 21:00"),
    frozenset(("FRA", "SEN")): ("2026-06-16T19:00:00Z", "2026-06-16 15:00"),
    frozenset(("IRQ", "NOR")): ("2026-06-16T22:00:00Z", "2026-06-16 18:00"),
    frozenset(("ALG", "ARG")): ("2026-06-17T01:00:00Z", "2026-06-16 21:00"),
    frozenset(("AUT", "JOR")): ("2026-06-17T04:00:00Z", "2026-06-17 00:00"),
    frozenset(("COD", "POR")): ("2026-06-17T17:00:00Z", "2026-06-17 13:00"),
    frozenset(("CRO", "ENG")): ("2026-06-17T20:00:00Z", "2026-06-17 16:00"),
    frozenset(("GHA", "PAN")): ("2026-06-17T23:00:00Z", "2026-06-17 19:00"),
    frozenset(("COL", "UZB")): ("2026-06-18T02:00:00Z", "2026-06-17 22:00"),
    frozenset(("CZE", "RSA")): ("2026-06-18T16:00:00Z", "2026-06-18 12:00"),
    frozenset(("BIH", "SUI")): ("2026-06-18T19:00:00Z", "2026-06-18 15:00"),
    frozenset(("CAN", "QAT")): ("2026-06-18T22:00:00Z", "2026-06-18 18:00"),
    frozenset(("KOR", "MEX")): ("2026-06-19T01:00:00Z", "2026-06-18 21:00"),
    frozenset(("AUS", "USA")): ("2026-06-19T19:00:00Z", "2026-06-19 15:00"),
    frozenset(("MAR", "SCO")): ("2026-06-19T22:00:00Z", "2026-06-19 18:00"),
    frozenset(("BRA", "HAI")): ("2026-06-20T00:30:00Z", "2026-06-19 20:30"),
    frozenset(("PAR", "TUR")): ("2026-06-20T03:00:00Z", "2026-06-19 23:00"),
    frozenset(("NED", "SWE")): ("2026-06-20T17:00:00Z", "2026-06-20 13:00"),
    frozenset(("CIV", "GER")): ("2026-06-20T20:00:00Z", "2026-06-20 16:00"),
    frozenset(("CUW", "ECU")): ("2026-06-21T00:00:00Z", "2026-06-20 20:00"),
    frozenset(("JPN", "TUN")): ("2026-06-21T04:00:00Z", "2026-06-21 00:00"),
    frozenset(("ESP", "KSA")): ("2026-06-21T16:00:00Z", "2026-06-21 12:00"),
    frozenset(("BEL", "IRN")): ("2026-06-21T19:00:00Z", "2026-06-21 15:00"),
    frozenset(("CPV", "URU")): ("2026-06-21T22:00:00Z", "2026-06-21 18:00"),
    frozenset(("EGY", "NZL")): ("2026-06-22T01:00:00Z", "2026-06-21 21:00"),
    frozenset(("ARG", "AUT")): ("2026-06-22T17:00:00Z", "2026-06-22 13:00"),
    frozenset(("FRA", "IRQ")): ("2026-06-22T21:00:00Z", "2026-06-22 17:00"),
    frozenset(("NOR", "SEN")): ("2026-06-23T00:00:00Z", "2026-06-22 20:00"),
    frozenset(("ALG", "JOR")): ("2026-06-23T03:00:00Z", "2026-06-22 23:00"),
    frozenset(("POR", "UZB")): ("2026-06-23T17:00:00Z", "2026-06-23 13:00"),
    frozenset(("ENG", "GHA")): ("2026-06-23T20:00:00Z", "2026-06-23 16:00"),
    frozenset(("CRO", "PAN")): ("2026-06-23T23:00:00Z", "2026-06-23 19:00"),
    frozenset(("COD", "COL")): ("2026-06-24T02:00:00Z", "2026-06-23 22:00"),
    frozenset(("CAN", "SUI")): ("2026-06-24T19:00:00Z", "2026-06-24 15:00"),
    frozenset(("BIH", "QAT")): ("2026-06-24T19:00:00Z", "2026-06-24 15:00"),
    frozenset(("BRA", "SCO")): ("2026-06-24T22:00:00Z", "2026-06-24 18:00"),
    frozenset(("HAI", "MAR")): ("2026-06-24T22:00:00Z", "2026-06-24 18:00"),
    frozenset(("CZE", "MEX")): ("2026-06-25T01:00:00Z", "2026-06-24 21:00"),
    frozenset(("KOR", "RSA")): ("2026-06-25T01:00:00Z", "2026-06-24 21:00"),
    frozenset(("CIV", "CUW")): ("2026-06-25T20:00:00Z", "2026-06-25 16:00"),
    frozenset(("ECU", "GER")): ("2026-06-25T20:00:00Z", "2026-06-25 16:00"),
    frozenset(("NED", "TUN")): ("2026-06-25T23:00:00Z", "2026-06-25 19:00"),
    frozenset(("JPN", "SWE")): ("2026-06-25T23:00:00Z", "2026-06-25 19:00"),
    frozenset(("TUR", "USA")): ("2026-06-26T02:00:00Z", "2026-06-25 22:00"),
    frozenset(("AUS", "PAR")): ("2026-06-26T02:00:00Z", "2026-06-25 22:00"),
    frozenset(("FRA", "NOR")): ("2026-06-26T19:00:00Z", "2026-06-26 15:00"),
    frozenset(("IRQ", "SEN")): ("2026-06-26T19:00:00Z", "2026-06-26 15:00"),
    frozenset(("CPV", "KSA")): ("2026-06-27T00:00:00Z", "2026-06-26 20:00"),
    frozenset(("ESP", "URU")): ("2026-06-27T00:00:00Z", "2026-06-26 20:00"),
    frozenset(("EGY", "IRN")): ("2026-06-27T03:00:00Z", "2026-06-26 23:00"),
    frozenset(("BEL", "NZL")): ("2026-06-27T03:00:00Z", "2026-06-26 23:00"),
    frozenset(("ENG", "PAN")): ("2026-06-27T21:00:00Z", "2026-06-27 17:00"),
    frozenset(("CRO", "GHA")): ("2026-06-27T21:00:00Z", "2026-06-27 17:00"),
    frozenset(("COL", "POR")): ("2026-06-27T23:30:00Z", "2026-06-27 19:30"),
    frozenset(("COD", "UZB")): ("2026-06-27T23:30:00Z", "2026-06-27 19:30"),
    frozenset(("ALG", "AUT")): ("2026-06-28T02:00:00Z", "2026-06-27 22:00"),
    frozenset(("ARG", "JOR")): ("2026-06-28T02:00:00Z", "2026-06-27 22:00"),
}


def official_group_kickoff(home_code, away_code):
    """Return (kickoff_utc, kickoff_local_ET) from the authoritative FIFA
    schedule for this matchup, or None if not a known group pairing."""
    if not home_code or not away_code:
        return None
    return OFFICIAL_GROUP_KICKOFFS.get(frozenset((home_code, away_code)))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# "Match in progress" detection
#
# The upstream site updates a player's points live while a match is being
# played, so a snapshot captured mid-match can hold a not-yet-final total that
# later settles to a different (often lower) value. We tag every history
# snapshot with `settled` — True only when no match was actually in progress at
# capture time — so the front-end can build the per-player charts and the
# top-climber / biggest-drop movers from settled data alone. The live
# leaderboard keeps using the always-current rankings.json untouched.
#
# Constants mirror the front-end's clock-based live window so both ends agree on
# when a match is "on".
# --------------------------------------------------------------------------- #
LIVE_WINDOW_MINUTES = int(os.environ.get("LIVE_WINDOW_MINUTES", "125"))      # 90' + HT + stoppage
LIVE_HARD_CAP_MINUTES = int(os.environ.get("LIVE_HARD_CAP_MINUTES", "130"))  # done by ~2h10m after KO
# Historical snapshots store only rankings, not the fixture state at capture
# time, so they can only be judged by their timestamp against the real fixture
# schedule. That schedule comes from the scraped predictions.json itself (every
# fixture's kickoff_utc, group and knockout alike) — no hand-maintained table.
# A slightly wider window than LIVE_WINDOW_MINUTES keeps a late-settling total
# from sneaking in as "settled"; losing a borderline-but-genuine reading is
# harmless (charts just step once less often).
SETTLE_BACKFILL_WINDOW_MINUTES = int(os.environ.get("SETTLE_BACKFILL_WINDOW_MINUTES", "150"))

_FIXTURE_MINUTE_RE = re.compile(r"\d+'")


def _parse_utc(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _status_is_final(status: str | None) -> bool:
    return "FT" in (status or "").upper()


def _status_is_live(status: str | None) -> bool:
    s = (status or "").upper()
    return "LIVE" in s or "HT" in s or bool(_FIXTURE_MINUTE_RE.search(s))


def fixture_is_live(fx: dict, now: datetime) -> bool:
    """Whether a single fixture is in progress at `now`.

    Mirrors the front-end ``isLive``: a final badge (FT) is never live; past the
    hard cap a match is over regardless of a stuck badge; otherwise a LIVE/HT/
    minute badge — or the clock sitting inside [kickoff, kickoff+window) — counts
    as in progress.
    """
    if not isinstance(fx, dict):
        return False
    status = fx.get("status")
    if _status_is_final(status):
        return False
    ko = _parse_utc(fx.get("kickoff_utc"))
    if ko is not None and now >= ko + timedelta(minutes=LIVE_HARD_CAP_MINUTES):
        return False
    if _status_is_live(status):
        return True
    if ko is not None and ko <= now < ko + timedelta(minutes=LIVE_WINDOW_MINUTES):
        return True
    return False


def any_match_in_progress(predictions: dict, now: datetime) -> bool:
    """True if any fixture across all players' prediction pages is live at `now`."""
    for p in predictions.values():
        if not isinstance(p, dict):
            continue
        for md in p.get("matchdays", []):
            if not isinstance(md, dict):
                continue
            for fx in md.get("fixtures", []):
                if fixture_is_live(fx, now):
                    return True
    return False


def _all_fixture_kickoffs(predictions: dict) -> list[datetime]:
    """Every distinct fixture kickoff in the scraped schedule (group + knockout).

    Drives the backfill window check off real data rather than a hand-maintained
    table, so newly added knockout rounds are covered automatically. Group
    kickoffs in predictions.json are already corrected against the authoritative
    table when fixtures are parsed (see ``official_group_kickoff``).
    """
    seen = set()
    if not isinstance(predictions, dict):
        return []
    for p in predictions.values():
        if not isinstance(p, dict):
            continue
        for md in p.get("matchdays", []):
            if not isinstance(md, dict):
                continue
            for fx in md.get("fixtures", []):
                if not isinstance(fx, dict):
                    continue
                ko = _parse_utc(fx.get("kickoff_utc"))
                if ko is not None:
                    seen.add(ko)
    return sorted(seen)


def _timestamp_settled(ts: str | None, kickoffs: list[datetime]) -> bool:
    """Whether a snapshot timestamp falls outside every scheduled match window."""
    t = _parse_utc(ts)
    if t is None:
        return True  # unparseable -> assume settled rather than hide it
    window = timedelta(minutes=SETTLE_BACKFILL_WINDOW_MINUTES)
    for ko in kickoffs:
        if ko <= t < ko + window:
            return False
    return True


def backfill_settled(history: list, predictions: dict) -> bool:
    """Tag any history entry lacking a `settled` flag using the real schedule.

    Kickoffs come from the scraped ``predictions.json`` fixtures, so the check
    covers whatever rounds have been published. Returns True if any entry was
    updated, so the caller can persist the file even on a run that appends no new
    snapshot. If the schedule is empty (e.g. a failed scrape) nothing is tagged
    rather than guessing every untagged entry settled.
    """
    if not isinstance(history, list):
        return False
    kickoffs = _all_fixture_kickoffs(predictions)
    if not kickoffs:
        return False
    changed = False
    for entry in history:
        if isinstance(entry, dict) and "settled" not in entry:
            entry["settled"] = _timestamp_settled(entry.get("timestamp"), kickoffs)
            changed = True
    return changed


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
REQUEST_SESSION = requests.Session()
REQUEST_SESSION.headers.update(HTTP_HEADERS)
_BROWSER_FETCHER = None
_USING_BROWSER_FETCH = False
_REQUESTS_WARMED = False
_LAST_SUCCESSFUL_REQUEST_URL = None


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
        launch_options = {
            "headless": BROWSER_HEADLESS,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if BROWSER_EXECUTABLE_PATH:
            launch_options["executable_path"] = BROWSER_EXECUTABLE_PATH
        elif BROWSER_CHANNEL:
            launch_options["channel"] = BROWSER_CHANNEL
        self._browser = self._pw.chromium.launch(**launch_options)
        self._context = self._browser.new_context(
            user_agent=HTTP_HEADERS["User-Agent"],
            locale=BROWSER_LOCALE,
            timezone_id=BROWSER_TIMEZONE,
            viewport={"width": BROWSER_VIEWPORT_WIDTH, "height": BROWSER_VIEWPORT_HEIGHT},
            extra_http_headers={
                "Accept-Language": HTTP_HEADERS["Accept-Language"],
            },
        )
        self._page = self._context.new_page()
        self._leaderboard_url = None

    def fetch_page(self, url: str) -> tuple[str, str]:
        # Pace browser navigations too. The requests backend has its own pacing,
        # but in CI we run browser-only, and hitting 16+ player pages back-to-back
        # is the same burst pattern that trips Cloudflare's rate limits.
        global _LAST_REQUEST_AT
        _polite_delay()
        try:
            return self._fetch_page(url)
        finally:
            _LAST_REQUEST_AT = time.monotonic()

    def _fetch_page(self, url: str) -> tuple[str, str]:
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


_LAST_REQUEST_AT = 0.0


def _same_origin(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def _navigation_headers(url: str, referer: str | None = None) -> dict:
    headers = dict(HTTP_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin" if _same_origin(url, referer) else "cross-site"
    else:
        headers["Sec-Fetch-Site"] = "none"
    headers["Sec-Fetch-User"] = "?1"
    return headers


def _polite_delay():
    if REQUEST_DELAY_SECONDS > 0 and _LAST_REQUEST_AT:
        target_gap = REQUEST_DELAY_SECONDS + random.uniform(0, max(0.0, REQUEST_JITTER_SECONDS))
        wait = target_gap - (time.monotonic() - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)


def _fetch_page_with_requests(url: str, referer: str | None = None) -> tuple[str, str]:
    global _LAST_REQUEST_AT, _LAST_SUCCESSFUL_REQUEST_URL
    _polite_delay()
    resp = REQUEST_SESSION.get(
        url,
        headers=_navigation_headers(url, referer),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    _LAST_REQUEST_AT = time.monotonic()
    if resp.status_code in {403, 503}:
        raise requests.HTTPError(
            f"{resp.status_code} {resp.reason} for {resp.url}; body starts: {_response_excerpt(resp.text)}",
            response=resp,
        )
    resp.raise_for_status()
    _LAST_SUCCESSFUL_REQUEST_URL = resp.url
    return resp.text, resp.url


def _warm_up_requests_session(target_url: str):
    global _REQUESTS_WARMED
    if _REQUESTS_WARMED or not REQUESTS_WARMUP:
        return

    referer = None
    for warmup_url in REQUEST_WARMUP_URLS:
        absolute = urljoin(target_url, warmup_url)
        if not _same_origin(absolute, target_url):
            continue
        try:
            _, referer = _fetch_page_with_requests(absolute, referer=referer)
            print(f"  http: warmed requests session at {referer}", file=sys.stderr)
        except requests.HTTPError:
            raise
        except Exception as exc:
            print(f"  http: requests warmup skipped {absolute}: {exc}", file=sys.stderr)
            break
    _REQUESTS_WARMED = True


def fetch_page(url: str) -> tuple[str, str]:
    global _USING_BROWSER_FETCH

    if FETCH_BACKEND == "browser" or _USING_BROWSER_FETCH:
        return _get_browser_fetcher().fetch_page(url)
    if FETCH_BACKEND != "auto" and FETCH_BACKEND != "requests":
        raise RuntimeError("FETCH_BACKEND must be one of: auto, requests, browser")

    try:
        _warm_up_requests_session(url)
        return _fetch_page_with_requests(url, referer=_LAST_SUCCESSFUL_REQUEST_URL)
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

    # Prefer the authoritative FIFA kickoff for this matchup; fall back to the
    # scraped time only for pairings we don't have on file.
    official = official_group_kickoff(home.get("code"), away.get("code"))
    if official:
        kickoff_utc, kickoff_local, kickoff_tz = official[0], official[1], "America/New_York"
    else:
        kickoff_utc, kickoff_local, kickoff_tz = to_utc_iso(dt_raw, tz_name), dt_raw, tz_name

    return {
        "fixture_number": fixture_number,
        "round": round_name,
        "stage_type": "group",
        "status": status,
        "kickoff_utc": kickoff_utc,
        "kickoff_local": kickoff_local,
        "kickoff_timezone": kickoff_tz,
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

    # The actual matchup identity (bracket slot labels like "2A"/"W73") lives
    # under the second section — labelled "Result" on the live site (older
    # markup used "Score").
    slot_row = pick("Result") or pick("Score")
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
    pts_text_nodes = [clean(t) for t in pts_td.find_all(string=True) if clean(t) and clean(t) not in ("Countries", "Score", "Result")]
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
    # Live rows carry a leading fixture-number column (a bare integer matching
    # data-fixture-number). Drop it so the cell layout matches the group
    # (date, match, predicted, actual, points) and knockout (date, composite,
    # actual, points) parsers below. Pages without that column are unaffected.
    if tds and fixture_number is not None and to_int(tds[0].get_text()) == fixture_number:
        tds = tds[1:]
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


def _round_name_for(container) -> str | None:
    """Round name from the heading that precedes a round's table container.

    On the live site each round is a separate ``<div class="table-responsive">``
    immediately preceded by an ``<h3>`` ("Matchday 1", "Round of 32", "Bonus
    Questions", ...). Fall back to the closest preceding heading in document
    order if it isn't a direct sibling.
    """
    headings = ("h1", "h2", "h3", "h4", "h5", "h6")
    sib = container.find_previous_sibling(headings)
    if sib is None:
        sib = container.find_previous(headings)
    return clean(sib.get_text()) if sib else None


def _parse_round_table(table, round_name) -> dict:
    """Parse one round's fixtures table into {round, subtotal, fixtures}.

    Fixtures are deduped by fixture number: the page sometimes renders the same
    fixture more than once (mobile/desktop variants, JS-cloned rows) and only the
    ``d-md-none`` ones are reliably class-marked, so a number-level guard keeps a
    round from doubling up.
    """
    md = {"round": round_name, "subtotal": None, "fixtures": []}
    seen_numbers = set()
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr", recursive=False):
        if "d-md-none" in tr.get("class", []):
            continue  # mobile-only duplicate of a desktop row

        if tr.get("data-fixture-number") is not None:
            num = to_int(tr.get("data-fixture-number"))
            if num is not None and num in seen_numbers:
                continue
            fixture = _parse_fixture(tr, round_name)
            if fixture:
                md["fixtures"].append(fixture)
                if num is not None:
                    seen_numbers.add(num)
            continue

        tds = tr.find_all("td", recursive=False)
        if tds and clean(tds[0].get_text()).lower() == "subtotal":
            md["subtotal"] = to_int(tds[-1].get_text())
    return md


def parse_player_page(html: str) -> dict:
    """Return {matchdays:[...], bonus:{...}}.

    The live page renders one ``<table class="table-sm">`` per round, each in its
    own ``<div class="table-responsive">`` with the round name in a preceding
    ``<h3>``; a final table holds the bonus questions. (The grand total is taken
    from the leaderboard, not parsed here.)

    Defensive against unstable markup (the browser-rendered fallback can emit
    extra JS-built fixture tables with no heading, or repeat a round): tables
    without a round name are skipped, and rounds are coalesced by name keeping the
    richest copy, so the result is always one clean entry per named round.
    """
    soup = soup_of(html)
    rounds: dict[str, dict] = {}
    order: list[str] = []
    bonus = {"questions": []}

    for container in soup.select("div.table-responsive"):
        table = container.find("table", class_="table-sm")
        if table is None:
            continue
        round_name = _round_name_for(container)

        if round_name and "bonus" in round_name.lower():
            parsed = _parse_bonus(table)
            parsed.pop("grand_total", None)
            if parsed.get("questions"):
                bonus = parsed
            continue

        # A round table is identified by having at least one fixture row.
        if table.find("tr", attrs={"data-fixture-number": True}) is None:
            continue
        if not round_name:
            # No heading -> almost certainly a JS-generated duplicate table.
            print("  ! skipping a fixtures table with no round heading", file=sys.stderr)
            continue

        md = _parse_round_table(table, round_name)
        if not md["fixtures"]:
            continue
        existing = rounds.get(round_name)
        if existing is None:
            rounds[round_name] = md
            order.append(round_name)
        elif len(md["fixtures"]) > len(existing["fixtures"]):
            # Keep the richer copy but don't lose an already-parsed subtotal.
            md["subtotal"] = md["subtotal"] if md["subtotal"] is not None else existing["subtotal"]
            rounds[round_name] = md

    matchdays = [rounds[name] for name in order]

    return {
        "matchdays": matchdays,
        "bonus": bonus,
    }


def _fixture_count(prediction: dict | None) -> int:
    if not isinstance(prediction, dict):
        return 0
    return sum(
        len(md.get("fixtures") or [])
        for md in prediction.get("matchdays", [])
        if isinstance(md, dict)
    )


def prediction_has_data(prediction: dict | None) -> bool:
    return _fixture_count(prediction) > 0


def _merge_rounds(fresh_matchdays, prev_matchdays) -> tuple[list[dict], bool]:
    """Merge a fresh parse with previously-saved rounds, round by round.

    The anonymous scraper sometimes can't see every fixture in a round (e.g.
    knockout matchups are gated behind login/membership until their deadline),
    so a fresh round can come back with fewer fixtures than we captured before.
    For each round we keep the fresh version unless a previously-saved round of
    the same name had MORE fixtures, in which case we retain the richer previous
    round. Previous rounds missing entirely from the fresh parse are appended.

    Returns (merged_matchdays, patched) where ``patched`` is True if any previous
    round/fixtures were retained because the fresh scrape was thinner.
    """
    prev_by_round = {
        md["round"]: md
        for md in (prev_matchdays or [])
        if isinstance(md, dict) and md.get("round")
    }
    merged: list[dict] = []
    patched = False
    seen = set()
    for md in fresh_matchdays:
        rnd = md.get("round")
        if not rnd:
            continue  # never carry an unnamed (phantom) round forward
        seen.add(rnd)
        prev = prev_by_round.get(rnd)
        if prev and len(prev.get("fixtures") or []) > len(md.get("fixtures") or []):
            merged.append(prev)
            patched = True
        else:
            merged.append(md)
    for md in (prev_matchdays or []):
        rnd = md.get("round") if isinstance(md, dict) else None
        if rnd and rnd not in seen:
            merged.append(md)
            patched = True
    return merged, patched


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
    stats = {"fresh": 0, "patched": 0, "kept": 0, "failed": 0}
    for p in players:
        tid = p["team_id"]
        prev = prev_predictions.get(tid)
        try:
            if not p.get("player_url"):
                raise RuntimeError("missing player URL in leaderboard row")
            html = get_player_html(p)
            parsed = parse_player_page(html)
            if _fixture_count(parsed) == 0:
                raise RuntimeError("player page produced 0 parsed fixtures")
            # Fill any rounds the fresh scrape couldn't see (e.g. login-gated
            # knockout fixtures) from the last known-good data, while keeping
            # fresh group scores/subtotals. Bonus falls back the same way.
            prev_dict = prev if isinstance(prev, dict) else None
            matchdays, patched = _merge_rounds(
                parsed["matchdays"], prev_dict.get("matchdays") if prev_dict else None
            )
            bonus = parsed["bonus"]
            if not bonus.get("questions") and prev_dict and prev_dict.get("bonus", {}).get("questions"):
                bonus = prev_dict["bonus"]
                patched = True
            predictions[tid] = {
                "team_id": tid,
                "name": p["name"],
                "slug": p["slug"],
                "player_url": p.get("player_url"),
                "flag": p["flag"],
                "rank": p["rank"],
                "points": p["points"],
                "exact": p["exact"],
                # Grand total is the authoritative leaderboard points value.
                "grand_total": p["points"],
                "matchdays": matchdays,
                "bonus": bonus,
            }
            if patched:
                stats["patched"] += 1
                print(f"  ~ player {tid} ({p['name']}): partial scrape, retained previous rounds/fixtures", file=sys.stderr)
            else:
                stats["fresh"] += 1
        except Exception as exc:
            stats["failed"] += 1
            print(f"  ! player {tid} ({p['name']}) failed: {exc}", file=sys.stderr)
            # Keep previous data for this player rather than wiping it.
            if prediction_has_data(prev):
                kept = dict(prev)
                kept.update(
                    {
                        "rank": p["rank"],
                        "points": p["points"],
                        "exact": p["exact"],
                        "grand_total": p["points"],
                        "name": p["name"],
                        "slug": p["slug"],
                        "player_url": p.get("player_url"),
                        "flag": p["flag"],
                    }
                )
                predictions[tid] = kept
                stats["kept"] += 1

    if players and (stats["fresh"] + stats["patched"]) == 0:
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


def _pct(n: int | float, d: int | float) -> str:
    return "0%" if not d else f"{(n / d) * 100:.0f}%"


def scrape_health_errors(
    players: list[dict],
    predictions: dict,
    stats: dict,
    *,
    strict_predictions: bool,
) -> list[str]:
    """Return fatal data-quality issues that should stop JSON writes."""
    errors: list[str] = []
    if not players:
        return ["leaderboard has no players"]

    team_ids = [p.get("team_id") for p in players]
    duplicate_ids = sorted({tid for tid in team_ids if tid and team_ids.count(tid) > 1})
    if duplicate_ids:
        errors.append(f"duplicate leaderboard team ids: {', '.join(duplicate_ids[:5])}")

    missing_core = [
        str(p.get("team_id") or "?")
        for p in players
        if not p.get("team_id")
        or not p.get("name")
        or not p.get("slug")
        or not p.get("player_url")
        or p.get("rank") is None
        or p.get("points") is None
        or p.get("exact") is None
    ]
    if missing_core:
        errors.append(f"leaderboard rows missing required fields: {', '.join(missing_core[:8])}")

    if not strict_predictions:
        return errors

    expected_ids = {str(tid) for tid in team_ids if tid}
    prediction_ids = {str(tid) for tid in predictions.keys()}
    missing_predictions = sorted(expected_ids - prediction_ids)
    extra_predictions = sorted(prediction_ids - expected_ids)
    if missing_predictions:
        errors.append(
            f"missing prediction records for {len(missing_predictions)}/{len(players)} players: "
            f"{', '.join(missing_predictions[:8])}"
        )
    if extra_predictions:
        errors.append(f"prediction records not present on leaderboard: {', '.join(extra_predictions[:8])}")

    coverage = (len(expected_ids & prediction_ids) / len(expected_ids)) if expected_ids else 0
    if coverage < MIN_PREDICTION_COVERAGE:
        errors.append(
            f"prediction coverage {_pct(len(expected_ids & prediction_ids), len(expected_ids))} "
            f"is below MIN_PREDICTION_COVERAGE={MIN_PREDICTION_COVERAGE:.2f}"
        )

    kept = int(stats.get("kept") or 0)
    stale_ratio = kept / len(players)
    if stale_ratio > MAX_STALE_PREDICTION_RATIO:
        errors.append(
            f"kept {kept}/{len(players)} stale prediction records "
            f"({_pct(kept, len(players))}), above MAX_STALE_PREDICTION_RATIO={MAX_STALE_PREDICTION_RATIO:.2f}"
        )

    fixture_counts = {
        tid: _fixture_count(pred)
        for tid, pred in predictions.items()
        if str(tid) in expected_ids
    }
    zero_fixture_ids = sorted(str(tid) for tid, count in fixture_counts.items() if count == 0)
    if zero_fixture_ids:
        errors.append(f"prediction records with zero fixtures: {', '.join(zero_fixture_ids[:8])}")

    positive_counts = [count for count in fixture_counts.values() if count > 0]
    if positive_counts:
        expected_fixture_count = max(positive_counts)
        min_fixture_count = max(1, int(expected_fixture_count * MIN_FIXTURE_COVERAGE_RATIO))
        thin_ids = sorted(
            f"{tid} ({count}/{expected_fixture_count})"
            for tid, count in fixture_counts.items()
            if 0 < count < min_fixture_count
        )
        if thin_ids:
            errors.append(
                f"prediction records with suspiciously thin fixture data: {', '.join(thin_ids[:8])}"
            )

    return errors


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
                f"{stats.get('patched', 0)} partially patched, "
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

    # Sanity guard against a partial parse. The leaderboard points column shows
    # "-" (parsed as 0) when the page is caught mid-render — most often while
    # matches are live — so a scrape can return the full roster with ranks and
    # exact-counts intact but every `points` zeroed. Cumulative points never
    # legitimately fall back to zero once earned, so if the fresh scrape totals
    # zero while the previous good scrape had points, treat it as a failed
    # scrape and leave every file untouched rather than overwriting good data.
    new_total = sum(int(p.get("points") or 0) for p in players)
    prev_rankings = load_json(rankings_path, {})
    prev_players = prev_rankings.get("players", []) if isinstance(prev_rankings, dict) else []
    prev_total = sum(int(p.get("points") or 0) for p in prev_players)
    if new_total == 0 and prev_total > 0:
        print(
            f"FATAL: leaderboard points all zero across {len(players)} players "
            f"(previous scrape totalled {prev_total}); likely a mid-render parse "
            "failure — not writing files.",
            file=sys.stderr,
        )
        return 1

    health_errors = scrape_health_errors(
        players,
        predictions,
        stats,
        strict_predictions=not samples,
    )
    if health_errors:
        print("FATAL: scrape health checks failed; not writing files.", file=sys.stderr)
        for err in health_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # rankings.json
    rankings = build_rankings(players, source_info)
    write_json(rankings_path, rankings)

    # history.json — append only if the snapshot actually changed. Each entry is
    # tagged `settled` (no match in progress at capture time) so the front-end
    # can chart / rank movers off settled data and ignore mid-match wobble.
    history = load_json(history_path, [])
    backfilled = backfill_settled(history, predictions)  # heal untagged entries

    captured_at = _parse_utc(rankings["updated"]) or datetime.now(timezone.utc)
    in_progress = any_match_in_progress(predictions, captured_at)
    settled = not in_progress

    sig = snapshot_signature(players)
    last_sig = snapshot_signature(history[-1]["players"]) if history else None
    appended = False
    if sig != last_sig:
        history.append(
            {
                "timestamp": rankings["updated"],
                "settled": settled,
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
        appended = True

    if appended or backfilled:
        write_json(history_path, history)
        if appended:
            state = "settled" if settled else "mid-match (live, excluded from charts)"
            print(f"  history: appended {state} snapshot ({len(history)} total)", file=sys.stderr)
        if backfilled:
            print("  history: backfilled settled flags on existing snapshots", file=sys.stderr)
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
